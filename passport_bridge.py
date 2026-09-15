"""AI Passport USB audio + Codex keys. No audio leaves this PC except via Codex."""
import argparse
import ctypes
import json
import logging
import pathlib
import queue
import struct
import threading
import time

MAGIC = b"APV1"
HEADER = struct.Struct("<4sBBHII")
ROOT = pathlib.Path(__file__).resolve().parent


class OkGesture:
    HOLD_SECONDS = 0.45

    def __init__(self):
        self.cancel()

    def cancel(self):
        self.pressed_at = None
        self.long_started = False

    def tick(self, now):
        if self.pressed_at is not None and not self.long_started and now - self.pressed_at >= self.HOLD_SECONDS:
            self.long_started = True
            return "start"

    def event(self, released, now):
        if not released:
            if self.pressed_at is None:
                self.pressed_at = now
            return
        if self.pressed_at is None:
            return
        action = "stop" if self.long_started else ("send" if now - self.pressed_at < self.HOLD_SECONDS else None)
        self.cancel()
        return action


class Parser:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= HEADER.size:
            pos = self.buffer.find(MAGIC)
            if pos < 0:
                del self.buffer[:-3]
                return
            del self.buffer[:pos]
            if len(self.buffer) < HEADER.size:
                return
            _, kind, flags, size, seq, checksum = HEADER.unpack_from(self.buffer)
            if kind not in (1, 2, 3, 4) or size > 640 or flags:
                del self.buffer[0]
                continue
            if len(self.buffer) < HEADER.size + size:
                return
            body = bytes(self.buffer[HEADER.size:HEADER.size + size])
            if checksum != kind + sum(body):
                del self.buffer[0]
                continue
            del self.buffer[:HEADER.size + size]
            yield kind, seq, body


def self_test():
    def frame(kind, body):
        return HEADER.pack(MAGIC, kind, 0, len(body), 9, kind + sum(body)) + body
    p = Parser()
    data = b"boot log\r\n" + frame(2, b"\x02") + frame(1, b"\x00\xff")
    result = []
    for b in data:
        result.extend(p.feed(bytes([b])))
    assert result == [(2, 9, b"\x02"), (1, 9, b"\x00\xff")]
    bad = bytearray(frame(1, b"bad")); bad[-1] ^= 1
    assert list(p.feed(bad + frame(3, b"{}"))) == [(3, 9, b"{}")]
    assert not list(p.feed(b"x" * 10000)) and len(p.buffer) <= 3
    g = OkGesture()
    g.event(False, 0); assert g.event(True, 0.2) == "send"
    g.event(False, 1); assert g.tick(1.5) == "start"
    assert g.tick(2) is None and g.event(True, 2) == "stop"
    assert g.event(True, 2.1) is None  # duplicate release never sends
    g.event(False, 3); g.cancel(); assert g.event(True, 3.1) is None
    g.event(False, 4); assert g.event(True, 4.5) is None  # delayed loop cannot send a long press
    print("Protocol self-test: PASS")


def codex_focused():
    import psutil
    import win32gui
    import win32process
    try:
        _, pid = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())
        path = psutil.Process(pid).exe().lower()
        return "openai.codex_" in path and path.endswith("chatgpt.exe")
    except (psutil.Error, OSError):
        return False


def hotkey(keys):
    # keybd_event is a native Windows API; always release modifiers in reverse order.
    user32 = ctypes.windll.user32
    try:
        for key in keys:
            user32.keybd_event(key, 0, 1 if key in (0x21, 0x22) else 0, 0)
        time.sleep(0.04)
    finally:
        for key in reversed(keys):
            user32.keybd_event(key, 0, 3 if key in (0x21, 0x22) else 2, 0)


def dictation_key(pressed):
    keys = [0x11, 0x10, ord('D')]
    for key in keys if pressed else reversed(keys):
        ctypes.windll.user32.keybd_event(key, 0, 0 if pressed else 2, 0)


class ChatCycle:
    def __init__(self):
        self.deadline = 0

    def finish(self):
        if self.deadline:
            ctypes.windll.user32.keybd_event(0x11, 0, 2, 0)
            self.deadline = 0
            logging.info("Chat selection committed; Ctrl released")

    def step(self, previous):
        # Keep Ctrl held across a burst so Codex freezes the recent-chat order.
        if not self.deadline:
            ctypes.windll.user32.keybd_event(0x11, 0, 0, 0)
        self.deadline = time.monotonic() + 0.8
        # Dedicated bindings bypass Codex's Tab shortcuts, which prefer side panels.
        hotkey([0x76 if previous else 0x77])  # Ctrl+F7 / Ctrl+F8


class MicrophoneRoute:
    def __init__(self):
        from pycaw.pycaw import AudioUtilities
        from pycaw.constants import ERole
        self.audio, self.roles = AudioUtilities, list(ERole)[:3]
        self.target = next(d.id for d in AudioUtilities.GetAllDevices()
                           if "Steam Streaming Microphone" in d.FriendlyName and d.id.startswith("{0.0.1."))
        self.previous = {}

    def enable(self):
        if self.previous:
            return
        from pycaw.constants import EDataFlow
        enum = self.audio.GetDeviceEnumerator()
        self.previous = {r: enum.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, r.value).GetId()
                         for r in self.roles}
        (ROOT / "passport-previous-microphone.json").write_text(
            json.dumps({r.name: v for r, v in self.previous.items()}, indent=2), encoding="utf-8")
        self.audio.SetDefaultDevice(self.target, self.roles)

    def restore(self):
        from pycaw.constants import EDataFlow
        enum = self.audio.GetDeviceEnumerator()
        for role, device in self.previous.items():
            if enum.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, role.value).GetId() == self.target:
                self.audio.SetDefaultDevice(device, [role])
        self.previous.clear()


def main():
    args = argparse.ArgumentParser()
    args.add_argument("--self-test", action="store_true")
    args.add_argument("--record", type=float, default=0, help="bounded microphone diagnostic in seconds")
    args.add_argument("--dry-run", action="store_true", help="log buttons without injecting keys")
    args.add_argument("--diagnose", action="store_true", help="save at most 30 seconds of device audio for diagnosis")
    opt = args.parse_args()
    if opt.self_test:
        self_test(); return
    import numpy as np
    import serial
    from serial.tools import list_ports
    import sounddevice as sd
    from scipy.signal import resample_poly
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AI-Passport-Codex-Bridge")
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.kernel32.CloseHandle(mutex)
        return
    route = MicrophoneRoute()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / "passport-bridge.log", encoding="utf-8"), logging.StreamHandler()])
    devices = sd.query_devices()
    outputs = [i for i, d in enumerate(devices) if "Steam Streaming Microphone" in d["name"]
               and d["max_output_channels"] >= 1 and sd.query_hostapis(d["hostapi"])["name"] == "Windows WASAPI"]
    if not outputs:
        raise RuntimeError("Steam Streaming Microphone virtual audio output not found")
    audio_queue = queue.Queue(maxsize=12)
    stopping = threading.Event()
    def audio_worker():
        # Steam's driver copies samples between endpoints without format conversion.
        # Both Windows endpoints must use mono 48000 Hz / 16-bit (see saved format backup).
        with sd.OutputStream(device=outputs[0], samplerate=48000, channels=1, dtype="float32", blocksize=960) as stream:
            while not stopping.is_set():
                try:
                    pcm = audio_queue.get(timeout=0.02)
                except queue.Empty:
                    pcm = bytes(640)
                x = resample_poly(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768, 3, 1)
                stream.write(x)
    worker = threading.Thread(target=audio_worker, daemon=True)
    worker.start()
    dictating = False
    chat_cycle = ChatCycle()
    try:
        while worker.is_alive():
            ports = [p.device for p in list_ports.comports() if p.vid == 0x303A and p.pid == 0x1001
                     and (p.serial_number or "").replace(":", "").lower() == "4c11ae2f8364"]
            if not ports:
                if opt.record:
                    raise RuntimeError("AI Passport is not connected")
                time.sleep(1); continue
            try:
                port = serial.Serial()
                port.port, port.baudrate, port.timeout = ports[0], 921600, 0.05
                port.dtr = port.rts = False
                port.open()
                with port:
                    port.reset_input_buffer()
                    port.write(b"HS")
                    if not opt.record and not opt.dry_run: route.enable()
                    logging.info("Connected %s; microphone OFF", ports[0])
                    parser, active, heartbeat, received = Parser(), False, 0, time.monotonic()
                    finish_at = 0
                    gesture = OkGesture()
                    sample_count = sample_energy = sample_peak = 0
                    recording, start = [], time.monotonic()
                    if opt.record:
                        port.write(b"R"); active = True
                    def handle_gesture(action):
                        nonlocal active, finish_at, sample_count, sample_energy, sample_peak, recording, dictating
                        if action == "start" and not dictating and not finish_at:
                            route.enable(); active = True
                            sample_count = sample_energy = sample_peak = 0
                            recording = []
                            port.write(b"R")
                            time.sleep(0.15)
                            dictation_key(True); dictating = True
                            logging.info("Long OK: dictation ON")
                        elif action == "stop" and dictating:
                            finish_at = time.monotonic() + 0.4
                        elif action == "send" and not dictating and not finish_at:
                            hotkey([0x0D])
                            logging.info("Short OK: send")
                    while worker.is_alive():
                        now = time.monotonic()
                        focused = codex_focused()
                        if chat_cycle.deadline and (not focused or now >= chat_cycle.deadline):
                            chat_cycle.finish()
                        if not focused:
                            gesture.cancel()
                        elif not opt.record and not opt.dry_run:
                            handle_gesture(gesture.tick(now))
                        if finish_at and now >= finish_at:
                            port.write(b"S"); active = False
                            dictation_key(False); dictating = False
                            finish_at = 0
                            logging.info("Dictation released: %.2fs RMS %.1f peak %d", sample_count / 16000,
                                         (sample_energy / max(1, sample_count)) ** 0.5, sample_peak)
                            if opt.diagnose and recording:
                                import wave
                                with wave.open(str(ROOT / "passport-backup" / "last-device-mic.wav"), "wb") as wav:
                                    wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                                    wav.writeframes(b"".join(recording))
                                logging.info("Saved device diagnostic audio")
                        if now - heartbeat > 0.8:
                            port.write(b"H"); heartbeat = now
                        if active and not opt.record and not focused:
                            port.write(b"S"); active = False
                            if dictating: dictation_key(False); dictating = False
                            finish_at = 0
                            logging.info("Microphone OFF: Codex lost focus")
                        for kind, seq, body in parser.feed(port.read(port.in_waiting or 1)):
                            received = now
                            if kind == 1 and len(body) == 640:
                                if opt.record or (opt.diagnose and active and len(recording) < 1500):
                                    recording.append(body)
                                if active:
                                    values = np.frombuffer(body, dtype="<i2").astype(np.float64)
                                    sample_count += len(values)
                                    sample_energy += float(np.dot(values, values))
                                    sample_peak = max(sample_peak, int(np.max(np.abs(values))))
                                    try: audio_queue.put_nowait(body)
                                    except queue.Full:
                                        audio_queue.get_nowait(); audio_queue.put_nowait(body)
                            elif kind == 2 and len(body) == 1:
                                key, released = body[0] & 0x7F, bool(body[0] & 0x80)
                                logging.info("Button %d %s", key, "UP" if released else "DOWN")
                                if opt.dry_run or opt.record or not codex_focused():
                                    gesture.cancel()
                                    continue
                                if key == 2:
                                    chat_cycle.finish()
                                    handle_gesture(gesture.event(released, time.monotonic()))
                                elif not released and key in (0, 1) and not dictating and gesture.pressed_at is None:
                                    chat_cycle.step(key == 0)
                                    logging.info("%s recent chat (cycle)", "Previous" if key == 0 else "Next")
                            elif kind == 3:
                                try:
                                    state = json.loads(body)
                                    if not state.get("audio_ok"):
                                        raise RuntimeError("Device audio capture failed")
                                except json.JSONDecodeError:
                                    logging.warning("Bad status frame")
                        if now - received > 5:
                            raise serial.SerialException("Device heartbeat timed out")
                        if opt.record and now - start >= min(opt.record, 30):
                            port.write(b"S")
                            import wave
                            raw = b"".join(recording)
                            if not raw:
                                raise RuntimeError("No microphone samples received")
                            with wave.open(str(ROOT / "passport-mic-test.wav"), "wb") as wav:
                                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed")); wav.writeframes(raw)
                            samples = np.frombuffer(raw, dtype="<i2").astype(float)
                            logging.info("Microphone test: %.2fs RMS %.1f peak %.0f", len(samples)/16000,
                                         np.sqrt(np.mean(samples*samples)), np.max(np.abs(samples)))
                            return
            except (serial.SerialException, OSError):
                chat_cycle.finish()
                if dictating: dictation_key(False); dictating = False
                route.restore()
                if opt.record: raise
                logging.exception("USB disconnected; reconnecting")
                while not audio_queue.empty():
                    try: audio_queue.get_nowait()
                    except queue.Empty: break
                time.sleep(1)
    finally:
        chat_cycle.finish()
        if dictating: dictation_key(False)
        route.restore()
        stopping.set()
        worker.join(timeout=2)
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        with (ROOT / "passport-bridge-crash.log").open("a", encoding="utf-8") as report:
            report.write(time.strftime("\n%Y-%m-%d %H:%M:%S\n"))
            traceback.print_exc(file=report)
        raise
