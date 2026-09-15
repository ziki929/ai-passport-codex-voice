"""AI Passport: Wi-Fi PCM to virtual microphone and Codex native dictation."""
import ctypes
import hashlib
import hmac
import json
import logging
import queue
import secrets
import socket
import threading
import time
from passport_bridge import ROOT, Parser, OkGesture, ChatCycle, hotkey, codex_focused, dictation_key, MicrophoneRoute


def read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Peer disconnected")
        data.extend(chunk)
    return bytes(data)


def authenticate(sock, key):
    challenge = secrets.token_bytes(32)
    sock.sendall(challenge)
    expected = hmac.digest(key.encode(), challenge + b"D", hashlib.sha256)
    if not hmac.compare_digest(read_exact(sock, 32), expected):
        raise ConnectionError("Device authentication failed")
    sock.sendall(hmac.digest(key.encode(), challenge + b"P", hashlib.sha256))


class WifiOkGesture(OkGesture):
    DOUBLE_SECONDS = 0.30

    def cancel(self):
        super().cancel()
        self.send_at = 0
        self.second = False

    def tick(self, now):
        if super().tick(now) == "start":
            self.send_at = 0
            self.second = False
            return "start"
        if self.pressed_at is None and self.send_at and now >= self.send_at:
            self.send_at = 0
            return "send"

    def event(self, released, now):
        if not released:
            if self.pressed_at is None:
                self.second = bool(self.send_at and now <= self.send_at)
                self.send_at = 0
                self.pressed_at = now
            return
        if self.pressed_at is None:
            return
        held = now - self.pressed_at
        long_started, second = self.long_started, self.second
        self.cancel()
        if long_started:
            return "stop"
        if held >= self.HOLD_SECONDS:
            return
        if second:
            return "interrupt"
        self.send_at = now + self.DOUBLE_SECONDS


class NavGesture:
    def __init__(self):
        self.cancel()

    def cancel(self):
        self.key = None
        self.started = self.next_scroll = 0

    def event(self, key, released, now):
        if not released:
            if self.key is None:
                self.key, self.started = key, now
                self.next_scroll = now + 0.45
            return
        if key != self.key:
            return
        short = now - self.started < 0.45
        self.cancel()
        return key if short else None

    def tick(self, now):
        if self.key is not None and now >= self.next_scroll:
            if now - self.started >= 30:  # lost release safety limit
                self.cancel()
                return
            self.next_scroll = now + 0.12
            return 120 if self.key == 0 else -120


def scroll_wheel(delta):
    ctypes.windll.user32.mouse_event(0x0800, 0, 0, ctypes.c_ulong(delta).value, 0)


class AudioSink:
    def __init__(self):
        self.queue = queue.Queue(maxsize=12)
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()
        if not self.ready.wait(10) or self.error:
            raise RuntimeError("Virtual microphone failed: " + str(self.error))

    def run(self):
        try:
            import numpy as np
            import sounddevice as sd
            from scipy.signal import resample_poly
            device = next(i for i, d in enumerate(sd.query_devices())
                          if "Steam Streaming Microphone" in d["name"] and d["max_output_channels"] >= 1
                          and sd.query_hostapis(d["hostapi"])["name"] == "Windows WASAPI")
            with sd.OutputStream(device=device, samplerate=48000, channels=1, dtype="float32", blocksize=960) as stream:
                self.ready.set()
                while not self.stopping.is_set():
                    try:
                        pcm = self.queue.get(timeout=0.02)
                    except queue.Empty:
                        pcm = bytes(640)
                    samples = resample_poly(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768, 3, 1)
                    stream.write(samples)
        except Exception as exc:
            self.error = exc
            self.ready.set()

    def push(self, pcm):
        if self.error:
            raise RuntimeError(str(self.error))
        self.queue.put(pcm, timeout=0.3)

    def clear(self):
        while True:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                return

    def close(self):
        self.stopping.set()
        self.worker.join(timeout=2)


def session(sock, audio):
    parser, gesture, cycle = Parser(), WifiOkGesture(), ChatCycle()
    nav = NavGesture()
    active = dictating = False
    stop_at = finish_at = end_deadline = 0
    started = heartbeat = 0
    sample_bytes = 0
    last_packet = time.monotonic()
    sock.settimeout(0.025)
    sock.sendall(b"HSI")
    audio.clear()
    logging.info("AI Passport Wi-Fi connected; Codex native dictation ready")
    try:
        while True:
            now = time.monotonic()
            focused = codex_focused()
            if audio.error:
                raise RuntimeError(str(audio.error))
            if cycle.deadline and (not focused or now >= cycle.deadline):
                cycle.finish()
            if not focused:
                nav.cancel()
                gesture.cancel()
                if dictating:
                    sock.sendall(b"SI")
                    dictation_key(False)
                    active = dictating = False
                    stop_at = finish_at = end_deadline = 0
                    audio.clear()
            if now - heartbeat >= 0.8:
                sock.sendall(b"H")
                heartbeat = now
            if focused and not dictating:
                delta = nav.tick(now)
                if delta is not None:
                    cycle.finish()
                    scroll_wheel(delta)
            action = gesture.tick(now) if focused and not dictating else None
            if action == "send":
                hotkey([0x0d])
                logging.info("Single OK: send")
            if action == "start":
                cycle.finish()
                audio.clear()
                active = dictating = True
                sample_bytes = 0
                started = now
                dictation_key(True)
                sock.sendall(b"R")
                logging.info("Codex native dictation ON")
            if active and ((stop_at and now >= stop_at) or now - started >= 30):
                sock.sendall(b"S")
                active = False
                stop_at = 0
                end_deadline = now + 2
            if end_deadline and now >= end_deadline:
                raise ConnectionError("Missing audio end marker")
            if finish_at and now >= finish_at:
                dictation_key(False)
                dictating = False
                sock.sendall(b"B")
                finish_at = 0
                logging.info("Codex dictation released after %.2fs audio", sample_bytes / 32000)
            try:
                data = sock.recv(4096)
                if not data:
                    raise ConnectionError("Device disconnected")
            except socket.timeout:
                if now - last_packet > 5:
                    raise ConnectionError("Device heartbeat timed out")
                continue
            for kind, seq, body in parser.feed(data):
                last_packet = time.monotonic()
                if kind == 1 and dictating:
                    audio.push(body)
                    sample_bytes += len(body)
                elif kind == 4 and dictating and end_deadline:
                    end_deadline = 0
                    finish_at = time.monotonic() + 0.4  # drain audio queue and capture tail before key-up
                elif kind == 2 and len(body) == 1:
                    key, released = body[0] & 0x7f, bool(body[0] & 0x80)
                    logging.info("Button %d %s", key, "UP" if released else "DOWN")
                    if not focused:
                        gesture.cancel()
                        continue
                    if key == 2:
                        nav.cancel()
                        cycle.finish()
                        action = gesture.event(released, time.monotonic())
                        if action == "send" and not dictating:
                            hotkey([0x0d])
                        elif action == "interrupt" and not dictating:
                            # Codex uses first Escape to arm stop, second to confirm.
                            hotkey([0x1b])
                            time.sleep(0.15)
                            hotkey([0x1b])
                            sock.sendall(b"X")
                            logging.info("Double OK: interrupt reply")
                        elif action == "stop" and active:
                            stop_at = time.monotonic() + 0.15
                    elif key in (0, 1) and not dictating and gesture.pressed_at is None:
                        gesture.cancel()
                        short_key = nav.event(key, released, time.monotonic())
                        if short_key is not None:
                            cycle.step(short_key == 0)
                elif kind == 3:
                    if not json.loads(body).get("audio_ok"):
                        raise RuntimeError("Device microphone failed")
    finally:
        cycle.finish()
        if dictating:
            dictation_key(False)
        audio.clear()
        try:
            sock.sendall(b"SI")
        except OSError:
            pass


def main():
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\AI-Passport-WiFi-Bridge")
    if ctypes.windll.kernel32.GetLastError() == 183:
        ctypes.windll.kernel32.CloseHandle(mutex)
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s",
                        handlers=[logging.FileHandler(ROOT / "passport-wifi.log", encoding="utf-8")])
    config = json.loads((ROOT / "passport-backup" / "wifi-host.json").read_text(encoding="utf-8"))
    route = MicrophoneRoute()
    audio = AudioSink()
    try:
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((config["host"], 8765))
            server.listen(2)
            server.settimeout(1)
            logging.info("Native dictation listening on %s:8765", config["host"])
            while not audio.error:
                try:
                    sock, peer = server.accept()
                except socket.timeout:
                    continue
                with sock:
                    try:
                        sock.settimeout(2)
                        authenticate(sock, config["key"])
                        route.enable()
                        session(sock, audio)
                    except (OSError, ValueError, RuntimeError, queue.Full) as exc:
                        logging.warning("Connection ended: %s", exc)
                    finally:
                        route.restore()
            raise RuntimeError(str(audio.error))
    finally:
        audio.close()
        route.restore()
        ctypes.windll.kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        with (ROOT / "passport-wifi-crash.log").open("a", encoding="utf-8") as report:
            report.write(time.strftime("\n%Y-%m-%d %H:%M:%S\n"))
            traceback.print_exc(file=report)
        raise
