#!/usr/bin/env python3
"""Standalone HTTP API for PTZ control through a DVRIP NVR."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from controller import DVRIPPTZController, PTZBusy, PTZError


ROOT = Path(__file__).resolve().parent
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

DOCS_HTML = """<!doctype html>
<html lang="id">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PTZ API</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.11.0/swagger-ui.css">
<style>body{margin:0;background:#fafafa}</style>
</head>
<body>
<div id="swagger-ui"></div>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.11.0/swagger-ui-bundle.js"></script>
<script src="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5.11.0/swagger-ui-standalone-preset.js"></script>
<script>
window.onload = () => {
  window.ui = SwaggerUIBundle({
    url: "/openapi.json",
    dom_id: "#swagger-ui",
    deepLinking: true,
    persistAuthorization: true,
    presets: [SwaggerUIBundle.presets.apis, SwaggerUIStandalonePreset],
    layout: "BaseLayout"
  });
};
</script>
</body>
</html>"""


def openapi_spec(requires_auth: bool = False) -> dict[str, object]:
    error = {"type": "object", "properties": {"error": {"type": "string"}}}
    direction = {"type": "string", "enum": ["left", "right", "up", "down", "zoom_in", "zoom_out"]}
    spec = {
        "openapi": "3.0.3",
        "info": {
            "title": "PTZ API",
            "version": "1.0.0",
            "description": (
                "Untuk kontrol tekan-tahan: kirim `start` saat pointer ditekan, "
                "kirim `renew` berkala menggunakan token dari respons start, lalu "
                "kirim `stop` saat pointer dilepas atau halaman kehilangan fokus."
            ),
        },
        "paths": {
            "/api/health": {"get": {
                "summary": "Cek status API",
                "responses": {"200": {"description": "API aktif"},
                              "401": {"description": "Tidak diizinkan"}},
            }},
            "/api/ptz/102": {
                "get": {
                    "summary": "Cek kesiapan kontrol PTZ",
                    "responses": {"200": {"description": "Kontrol siap"},
                                  "401": {"description": "Tidak diizinkan"},
                                  "502": {"description": "NVR tidak merespons"}},
                },
                "post": {
                    "summary": "Kontrol PTZ",
                    "requestBody": {"required": True, "content": {"application/json": {
                        "schema": {
                            "type": "object",
                            "required": ["action"],
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["start", "renew", "stop", "center",
                                             "left", "right", "up", "down", "zoom_in", "zoom_out"],
                                    "default": "start",
                                    "example": "start",
                                    "description": "start/renew/stop untuk tekan-tahan; start baru otomatis menghentikan gerakan lama, center untuk preset tengah, atau arah untuk gerak singkat",
                                },
                                "direction": {**direction, "default": "left", "example": "left"},
                                "token": {"type": "string", "example": "TOKEN_DARI_START"},
                                "speed": {"type": "number", "minimum": 0.05, "maximum": 0.35,
                                          "default": 0.15, "example": 0.15},
                                "duration": {"type": "number", "minimum": 0.1, "maximum": 0.5,
                                             "default": 0.35, "example": 0.35},
                            },
                        },
                        "example": {"action": "start", "direction": "left", "speed": 0.15},
                        "examples": {
                            "Start tekan-tahan": {"value": {"action": "start", "direction": "left", "speed": 0.15}},
                            "Renew gerakan": {"value": {"action": "renew", "token": "TOKEN_DARI_START"}},
                            "Stop gerakan": {"value": {"action": "stop", "token": "TOKEN_DARI_START"}},
                            "Gerak singkat": {"value": {"action": "right", "speed": 0.15, "duration": 0.35}},
                            "Kembali ke tengah": {"value": {"action": "center"}},
                        },
                    }}},
                    "responses": {"200": {"description": "Perintah diterima"},
                                  "400": {"description": "Perintah tidak valid"},
                                  "401": {"description": "Tidak diizinkan"},
                                  "403": {"description": "Origin ditolak"},
                                  "409": {"description": "Gerakan sedang aktif"},
                                  "415": {"description": "Content-Type salah"},
                                  "502": {"description": "NVR tidak merespons"},
                                  "503": {"description": "Preset tengah belum dikonfigurasi"}},
                },
            },
        },
        "components": {"schemas": {"Error": error}},
    }
    if requires_auth:
        spec["components"]["securitySchemes"] = {
            "ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-PTZ-API-Key"}
        }
        spec["security"] = [{"ApiKeyAuth": []}]
    return spec


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            os.environ.setdefault(key, value)


class PTZHandler(BaseHTTPRequestHandler):
    server_version = "PTZAPI/1.0"

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(f"[{self.log_date_time_string()}] PTZ API request\n")

    def origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            origin_host = urlsplit(origin).hostname or ""
        except ValueError:
            return False
        return bool(self.server.api_key) or origin_host in LOCAL_HOSTS or origin == f"http://{self.headers.get('Host')}"

    def end_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin and self.origin_allowed():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-PTZ-API-Key")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Vary", "Origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authorized(self) -> bool:
        key = self.server.api_key
        if key:
            if hmac.compare_digest(self.headers.get("X-PTZ-API-Key", ""), key):
                return True
        else:
            try:
                host = urlsplit(f"http://{self.headers.get('Host', '')}")
                if host.hostname in LOCAL_HOSTS and host.port == self.server.server_port:
                    return True
            except ValueError:
                pass
        self.send_json({"error": "Permintaan tidak diizinkan."}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_OPTIONS(self) -> None:
        if not self.origin_allowed():
            self.send_json({"error": "Origin frontend tidak diizinkan."}, HTTPStatus.FORBIDDEN)
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self.end_headers()

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/docs":
            self.send_html(DOCS_HTML)
            return
        if path == "/openapi.json":
            self.send_json(openapi_spec(bool(self.server.api_key)))
            return
        if not self.authorized():
            return
        if path == "/api/health":
            self.send_json({"ok": True})
        elif path == "/api/ptz/102":
            try:
                info = self.server.controller.info()
                info["center_available"] = self.server.center_preset is not None
                self.send_json(info)
            except PTZError as error:
                self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)
        else:
            self.send_json({"error": "Endpoint tidak ditemukan."}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.authorized():
            return
        if urlsplit(self.path).path != "/api/ptz/102":
            self.send_json({"error": "Endpoint tidak ditemukan."}, HTTPStatus.NOT_FOUND)
            return
        if not self.origin_allowed():
            self.send_json({"error": "Permintaan kontrol ditolak."}, HTTPStatus.FORBIDDEN)
            return
        if self.headers.get_content_type() != "application/json":
            self.send_json({"error": "Kontrol memerlukan JSON."}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 2048:
                raise ValueError
            command = json.loads(self.rfile.read(length))
            if not isinstance(command, dict) or not isinstance(command.get("action"), str):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            self.send_json({"error": "Perintah PTZ tidak valid."}, HTTPStatus.BAD_REQUEST)
            return
        controller = self.server.controller
        if command["action"] == "center" and self.server.center_preset is None:
            self.send_json({"error": "Preset tengah belum diatur."}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        try:
            if command["action"] == "center":
                result = controller.center(self.server.center_preset)
            elif command["action"] == "start":
                direction, speed = command.get("direction"), command.get("speed", 0.15)
                try:
                    result = controller.start(direction, speed)
                except PTZBusy:
                    controller.stop()
                    result = controller.start(direction, speed)
                    result["replaced_previous"] = True
            elif command["action"] == "renew":
                result = controller.renew(command.get("token"))
            elif command["action"] == "stop":
                result = controller.stop(command.get("token"))
            else:
                result = controller.pulse(command["action"], command.get("speed", 0.15),
                                          command.get("duration", 0.35))
            self.send_json(result)
        except ValueError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except PTZBusy as error:
            self.send_json({"error": str(error)}, HTTPStatus.CONFLICT)
        except PTZError as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_GATEWAY)


class PTZServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], controller: DVRIPPTZController,
                 api_key: str = "", center_preset: int | None = None):
        self.controller = controller
        self.api_key = api_key
        self.center_preset = center_preset
        super().__init__(address, PTZHandler)


def main() -> None:
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="API PTZ mandiri via NVR DVRIP")
    parser.add_argument("--host", default=os.environ.get("PTZ_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PTZ_API_PORT", "8091")))
    args = parser.parse_args()
    api_key = os.environ.get("PTZ_API_KEY", "")
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not api_key:
        parser.error("PTZ_API_KEY wajib diisi jika API dibuka ke jaringan.")
    if not 1 <= args.port <= 65535:
        parser.error("Port API harus antara 1 dan 65535.")
    nvr_url = os.environ.get("RTSP_NETSURVEILLANCE_91_URL", "").strip()
    if not nvr_url or "YOUR_USERNAME" in nvr_url or "YOUR_PASSWORD" in nvr_url:
        parser.error("RTSP_NETSURVEILLANCE_91_URL belum diatur.")
    try:
        channel = int(os.environ.get("PTZ_NVR_CHANNEL", "1"))
        controller = DVRIPPTZController(nvr_url, channel)
        center_value = os.environ.get("PTZ_CENTER_PRESET", "").strip()
        center_preset = int(center_value) if center_value else None
        if center_preset is not None and not 1 <= center_preset <= 126:
            raise ValueError
    except (ValueError, PTZError):
        parser.error("Konfigurasi NVR PTZ atau preset tengah tidak valid.")
    server = PTZServer((args.host, args.port), controller, api_key, center_preset)

    def stop_server(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_server)
    print(f"PTZ API: http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("\nPTZ API dihentikan.", flush=True)
    finally:
        try:
            controller.stop()
        except PTZError as error:
            sys.stderr.write(f"Stop PTZ gagal: {error}\n")
        server.server_close()


if __name__ == "__main__":
    main()
