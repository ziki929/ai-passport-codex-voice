"""Local protocol tests: no device, keyboard injection, or network access."""
import hashlib
import hmac
import queue
import socket
import threading
import time
import unittest
from unittest.mock import patch, MagicMock
import passport_wifi_bridge as bridge
from passport_bridge import HEADER


def frame(kind, body=b""):
    return HEADER.pack(b"APV1", kind, 0, len(body), 1, kind + sum(body)) + body


class ProtocolTests(unittest.TestCase):
    def test_navigation_hold(self):
        nav = bridge.NavGesture()
        nav.event(0, False, 0)
        self.assertIsNone(nav.tick(0.4))
        self.assertEqual(nav.event(0, True, 0.42), 0)
        nav.event(1, False, 1)
        self.assertEqual(nav.tick(1.46), -120)
        self.assertIsNone(nav.tick(1.5))
        self.assertEqual(nav.tick(1.6), -120)
        self.assertIsNone(nav.event(1, True, 1.7))
        self.assertIsNone(nav.tick(2))
        nav.event(0, False, 3)
        self.assertEqual(nav.tick(3.5), 120)
        nav.cancel()
        self.assertIsNone(nav.tick(4))
        self.assertIsNone(nav.event(0, True, 4))
        nav.event(0, False, 5)
        self.assertIsNone(nav.tick(35))
        self.assertIsNone(nav.tick(36))

    def test_pairing(self):
        for valid in (True, False):
            with self.subTest(valid=valid):
                host, device = socket.socketpair()
                errors = []
                def authenticate():
                    try:
                        bridge.authenticate(host, "test-key")
                    except ConnectionError as exc:
                        errors.append(exc)
                worker = threading.Thread(target=authenticate)
                worker.start()
                challenge = bridge.read_exact(device, 32)
                response = hmac.digest(b"test-key" if valid else b"wrong", challenge+b"D", hashlib.sha256)
                device.sendall(response[:7]); device.sendall(response[7:])
                if valid:
                    self.assertEqual(bridge.read_exact(device, 32), hmac.digest(b"test-key", challenge+b"P", hashlib.sha256))
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(bool(errors), not valid)
                host.close(); device.close()

    def test_recording_barrier_and_text(self):
        host, device = socket.socketpair()
        audio = MagicMock(error=None)
        def run():
            try:
                bridge.session(host, audio)
            except ConnectionError:
                pass
        with patch.object(bridge, "codex_focused", return_value=True), patch.object(bridge, "dictation_key") as dictation, patch.object(bridge, "hotkey") as hotkey:
            worker = threading.Thread(target=run)
            worker.start()
            device.settimeout(2)
            device.sendall(frame(2, b"\x02"))
            commands = b""
            while b"R" not in commands:
                commands += device.recv(100)
            pcm = b"\x01\x00" * 4000
            for offset in range(0, len(pcm), 640):
                device.sendall(frame(1, pcm[offset:offset+640]))
            device.sendall(frame(2, b"\x82"))
            commands = b""
            while b"S" not in commands:
                commands += device.recv(100)
            dictation.assert_called_once_with(True)
            device.sendall(frame(4))
            deadline = time.monotonic() + 2
            while dictation.call_count < 2 and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertEqual([call.args[0] for call in dictation.call_args_list], [True, False])
            self.assertEqual(b"".join(call.args[0] for call in audio.push.call_args_list), pcm)
            hotkey.assert_not_called()  # long OK must never send Enter
            commands = b""
            while b"B" not in commands:
                commands += device.recv(100)
            # Double tap remains two Escapes and reports its display state.
            for _ in range(2):
                device.sendall(frame(2, b"\x02"))
                time.sleep(0.04)
                device.sendall(frame(2, b"\x82"))
                time.sleep(0.04)
            commands = b""
            while b"X" not in commands:
                commands += device.recv(100)
            self.assertEqual([call.args[0] for call in hotkey.call_args_list], [[0x1b], [0x1b]])
            device.shutdown(socket.SHUT_RDWR); device.close()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            host.close()


if __name__ == "__main__":
    unittest.main()
