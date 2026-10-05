"""DVRIP PTZ control through the NVR, with bounded press-and-hold."""

from __future__ import annotations

import hashlib
import json
import math
import secrets
import socket
import struct
import threading
import time
from urllib.parse import unquote, urlsplit


class PTZError(Exception):
    pass


class PTZBusy(PTZError):
    pass


class DVRIPPTZController:
    LEASE_SECONDS = 0.9
    MAX_HOLD_SECONDS = 8.0
    DIRECTIONS = {
        "left": "DirectionLeft", "right": "DirectionRight",
        "up": "DirectionUp", "down": "DirectionDown",
        "zoom_in": "ZoomTile", "zoom_out": "ZoomWide",
    }

    def __init__(self, nvr_url: str, channel: int = 1):
        parsed = urlsplit(nvr_url)
        if parsed.scheme != "rtsp" or not parsed.hostname:
            raise PTZError("Alamat NVR untuk kontrol PTZ belum diatur.")
        if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 31:
            raise PTZError("Channel PTZ NVR tidak valid.")
        self.host = parsed.hostname
        self.username = unquote(parsed.username or "")
        self.password = unquote(parsed.password or "")
        self.channel = channel
        self._move_lock = threading.Lock()
        self._command_lock = threading.RLock()
        self._active_lock = threading.Lock()
        self._cancel_move = threading.Event()
        self._active_command = ""
        self._active_step = 1
        self._hold_token = ""
        self._hold_deadline = 0.0
        self._hold_limit = 0.0

    @staticmethod
    def _sofia_hash(password: str) -> str:
        digest = hashlib.md5(password.encode()).digest()
        result = []
        for index in range(0, 16, 2):
            value = (digest[index] + digest[index + 1]) % 62
            result.append(chr(value + (48 if value <= 9 else 55 if value <= 35 else 61)))
        return "".join(result)

    @staticmethod
    def _recv_exact(connection: socket.socket, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            if not chunk:
                raise PTZError("NVR menutup koneksi kontrol PTZ.")
            data.extend(chunk)
        return bytes(data)

    def _exchange(self, connection: socket.socket, session: int, sequence: int,
                  message: int, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        packet = struct.pack("<BB2xII2xHI", 255, 0, session, sequence, message, len(data) + 2)
        connection.sendall(packet + data + b"\x0a\x00")
        header = self._recv_exact(connection, 20)
        marker, _, response_session, _, _, length = struct.unpack("<BB2xII2xHI", header)
        if marker != 255 or not 2 <= length <= 256_000:
            raise PTZError("Respons kontrol PTZ dari NVR tidak valid.")
        raw = self._recv_exact(connection, length).rstrip(b"\x00\n")
        try:
            response = json.loads(raw.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PTZError("Respons kontrol PTZ dari NVR tidak dikenali.") from None
        if not isinstance(response, dict):
            raise PTZError("Respons kontrol PTZ dari NVR tidak dikenali.")
        return response_session, response

    def _call(self, message: int | None = None,
              payload: dict[str, object] | None = None) -> dict[str, object]:
        try:
            with socket.create_connection((self.host, 34567), timeout=3) as connection:
                connection.settimeout(3)
                session, login = self._exchange(connection, 0, 0, 1000, {
                    "EncryptType": "MD5", "LoginType": "DVRIP-Web",
                    "PassWord": self._sofia_hash(self.password), "UserName": self.username,
                })
                if login.get("Ret") != 100:
                    raise PTZError("Login kontrol PTZ ditolak oleh NVR.")
                if message is None:
                    return login
                request = dict(payload or {})
                request["SessionID"] = f"0x{session:08X}"
                _, response = self._exchange(connection, session, 1, message, request)
                if response.get("Ret") != 100:
                    raise PTZError(f"Perintah PTZ ditolak NVR (kode {response.get('Ret', 'tidak diketahui')}).")
                return response
        except PTZError:
            raise
        except (OSError, TimeoutError):
            raise PTZError("NVR PTZ tidak merespons. Periksa koneksi LAN.") from None

    def _send_ptz(self, command: str, step: int, start: bool) -> None:
        parameter = {
            "AUX": {"Number": 0, "Status": "On"}, "Channel": self.channel,
            "MenuOpts": "Enter", "POINT": {"bottom": 0, "left": 0, "right": 0, "top": 0},
            "Pattern": "SetBegin", "Preset": 65535 if start else -1, "Step": step, "Tour": 0,
        }
        self._call(1400, {
            "Name": "OPPTZControl",
            "OPPTZControl": {"Command": command, "Parameter": parameter},
        })

    def _preset_command(self, command: str, preset: int) -> None:
        if isinstance(preset, bool) or not isinstance(preset, int) or not 1 <= preset <= 126:
            raise ValueError("Nomor preset harus antara 1 dan 126.")
        if not self._move_lock.acquire(blocking=False):
            raise PTZBusy("Kamera sedang bergerak. Lepaskan tombol atau tekan Stop.")
        try:
            with self._command_lock:
                self._call(1400, {
                    "Name": "OPPTZControl",
                    "OPPTZControl": {
                        "Command": command,
                        "Parameter": {
                            "AUX": {"Number": 0, "Status": "On"}, "Channel": self.channel,
                            "MenuOpts": "Enter", "Pattern": "Start", "Preset": preset,
                            "Step": 5, "Tour": 0,
                        },
                    },
                })
        finally:
            self._move_lock.release()

    def set_preset(self, preset: int) -> None:
        self._preset_command("SetPreset", preset)

    def center(self, preset: int) -> dict[str, object]:
        self._preset_command("GotoPreset", preset)
        return {"ok": True, "action": "center", "preset": preset,
                "message": "Perintah kembali ke preset tengah dikirim melalui NVR."}

    def start(self, action: str, speed: float = 0.15) -> dict[str, object]:
        if not isinstance(action, str) or action not in self.DIRECTIONS:
            raise ValueError("Arah PTZ tidak dikenal.")
        if isinstance(speed, bool):
            raise ValueError("Kecepatan PTZ harus berupa angka.")
        try:
            speed = float(speed)
        except (TypeError, ValueError):
            raise ValueError("Kecepatan PTZ harus berupa angka.") from None
        if not math.isfinite(speed) or not 0.05 <= speed <= 0.35:
            raise ValueError("Kecepatan PTZ harus antara 0.05 dan 0.35.")
        if not self._move_lock.acquire(blocking=False):
            raise PTZBusy("Kamera sedang bergerak. Lepaskan tombol atau tekan Stop.")
        command = self.DIRECTIONS[action]
        step = round(1 + (speed - 0.05) / 0.3 * 7)
        started = False
        try:
            self._cancel_move.clear()
            with self._command_lock:
                if self._cancel_move.is_set():
                    raise PTZBusy("Gerakan dibatalkan oleh Stop.")
                token = secrets.token_urlsafe(16)
                with self._active_lock:
                    self._active_command, self._active_step = command, step
                self._hold_token = token
                try:
                    self._send_ptz(command, step, True)
                    now = time.monotonic()
                    self._hold_limit = now + self.MAX_HOLD_SECONDS
                    self._hold_deadline = min(now + self.LEASE_SECONDS, self._hold_limit)
                    threading.Thread(target=self._watch_hold, args=(token,), daemon=True).start()
                except Exception:
                    try:
                        self._send_ptz(command, step, False)
                    finally:
                        self._hold_token = ""
                        with self._active_lock:
                            self._active_command = ""
                    raise
                started = True
            return {"ok": True, "action": action, "token": token,
                    "lease": self.LEASE_SECONDS, "max_hold": self.MAX_HOLD_SECONDS,
                    "message": "Kamera bergerak selama tombol ditahan."}
        finally:
            if not started:
                self._move_lock.release()

    def renew(self, token: str) -> dict[str, object]:
        if not isinstance(token, str) or not token or len(token) > 128:
            raise ValueError("Token gerakan tidak valid.")
        with self._command_lock:
            if token != self._hold_token:
                raise PTZBusy("Gerakan sudah berhenti. Tekan tombol lagi.")
            now = time.monotonic()
            if now >= self._hold_deadline:
                self.stop(token)
                raise PTZBusy("Gerakan berhenti otomatis. Tekan tombol lagi.")
            self._hold_deadline = min(now + self.LEASE_SECONDS, self._hold_limit)
        return {"ok": True, "action": "renew"}

    def _watch_hold(self, token: str) -> None:
        while True:
            with self._command_lock:
                if token != self._hold_token:
                    return
                remaining = self._hold_deadline - time.monotonic()
                if remaining <= 0:
                    try:
                        self.stop(token)
                    except PTZError:
                        pass
                    else:
                        return
            time.sleep(max(0.05, min(remaining, 0.1)))

    def info(self) -> dict[str, object]:
        self._call()
        return {"ready": True, "pan_tilt": True, "zoom": True,
                "max_duration": 0.5, "default_speed": 0.15,
                "lease": self.LEASE_SECONDS, "max_hold": self.MAX_HOLD_SECONDS}

    def stop(self, token: str | None = None) -> dict[str, object]:
        if token is not None and (not isinstance(token, str) or not token or len(token) > 128):
            raise ValueError("Token gerakan tidak valid.")
        if token is None:
            self._cancel_move.set()
        with self._command_lock:
            if token is not None and token != self._hold_token:
                return {"ok": True, "action": "stop", "message": "Gerakan sebelumnya sudah berhenti."}
            self._cancel_move.set()
            with self._active_lock:
                command, step = self._active_command, self._active_step
            if command:
                self._send_ptz(command, step, False)
                with self._active_lock:
                    self._active_command = ""
            if self._hold_token:
                self._hold_token = ""
                self._move_lock.release()
        return {"ok": True, "action": "stop", "message": "Stop dikirim melalui NVR."}

    def pulse(self, action: str, speed: float = 0.15, duration: float = 0.35) -> dict[str, object]:
        if action not in self.DIRECTIONS:
            raise ValueError("Arah PTZ tidak dikenal.")
        if isinstance(speed, bool) or isinstance(duration, bool):
            raise ValueError("Kecepatan dan durasi harus berupa angka.")
        try:
            speed, duration = float(speed), float(duration)
        except (ValueError, TypeError):
            raise ValueError("Kecepatan dan durasi harus berupa angka.") from None
        if not math.isfinite(speed) or not 0.05 <= speed <= 0.35:
            raise ValueError("Kecepatan PTZ harus antara 0.05 dan 0.35.")
        if not math.isfinite(duration) or not 0.1 <= duration <= 0.5:
            raise ValueError("Durasi PTZ harus antara 0.1 dan 0.5 detik.")
        if not self._move_lock.acquire(blocking=False):
            raise PTZBusy("Kamera sedang bergerak. Tunggu atau tekan Stop.")
        command = self.DIRECTIONS[action]
        step = round(1 + (speed - 0.05) / 0.3 * 7)
        try:
            self._cancel_move.clear()
            with self._active_lock:
                self._active_command, self._active_step = command, step
            try:
                with self._command_lock:
                    if self._cancel_move.is_set():
                        return {"ok": True, "action": "stop", "message": "Gerakan dibatalkan oleh Stop."}
                    self._send_ptz(command, step, True)
                self._cancel_move.wait(duration)
            finally:
                try:
                    with self._command_lock:
                        self._send_ptz(command, step, False)
                finally:
                    with self._active_lock:
                        self._active_command = ""
            return {"ok": True, "action": action, "duration": duration,
                    "message": "Perintah gerak dan Stop selesai dikirim melalui NVR."}
        finally:
            self._move_lock.release()
