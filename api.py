#!/usr/bin/env python3
"""Standalone HTTP API for PTZ control through a DVRIP NVR."""

from __future__ import annotations

import argparse
import hmac
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import uvicorn
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from controller import DVRIPPTZController, PTZBusy, PTZError


ROOT = Path(__file__).resolve().parent
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
Direction = Literal["left", "right", "up", "down", "zoom_in", "zoom_out"]
Action = Literal["start", "renew", "stop", "center", "left", "right", "up", "down", "zoom_in", "zoom_out"]


class PTZCommand(BaseModel):
    model_config = ConfigDict(json_schema_extra={"example": {"action": "start", "direction": "left", "speed": 0.15}})

    action: Action
    direction: Direction | None = None
    token: str | None = None
    speed: float = Field(default=0.15, ge=0.05, le=0.35)
    duration: float = Field(default=0.35, ge=0.1, le=0.5)

    @field_validator("speed", "duration", mode="before")
    @classmethod
    def reject_boolean(cls, value):
        if isinstance(value, bool):
            raise ValueError("Kecepatan dan durasi harus berupa angka.")
        return value


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


def configured_controller() -> tuple[DVRIPPTZController, int | None]:
    nvr_url = os.environ.get("RTSP_NETSURVEILLANCE_91_URL", "").strip()
    if not nvr_url or "YOUR_USERNAME" in nvr_url or "YOUR_PASSWORD" in nvr_url:
        raise ValueError("RTSP_NETSURVEILLANCE_91_URL belum diatur.")
    channel = int(os.environ.get("PTZ_NVR_CHANNEL", "1"))
    controller = DVRIPPTZController(nvr_url, channel)
    center_value = os.environ.get("PTZ_CENTER_PRESET", "").strip()
    center_preset = int(center_value) if center_value else None
    if center_preset is not None and not 1 <= center_preset <= 126:
        raise ValueError("Preset tengah tidak valid.")
    return controller, center_preset


def create_app(controller: DVRIPPTZController, api_key: str = "", center_preset: int | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            try:
                controller.stop()
            except PTZError:
                pass

    app = FastAPI(
        title="PTZ API", version="1.0.0", lifespan=lifespan,
        description=("Untuk kontrol tekan-tahan: kirim `start` saat pointer ditekan, "
                     "kirim `renew` setiap sekitar 250 ms dengan token dari respons start, "
                     "lalu kirim `stop` saat dilepas atau halaman kehilangan fokus. "
                     "Lease 0,9 detik dan batas tahan 8 detik berlaku otomatis."),
    )

    def origin_allowed(origin: str | None) -> bool:
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
            return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and (
                bool(api_key) or parsed.hostname in LOCAL_HOSTS
            )
        except ValueError:
            return False

    @app.middleware("http")
    async def ptz_headers(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method in {"POST", "OPTIONS"} and not origin_allowed(origin):
            response = JSONResponse({"error": "Permintaan kontrol ditolak."}, status_code=403)
        elif request.method == "OPTIONS":
            response = Response(status_code=204)
        elif request.method == "POST" and request.url.path == "/api/ptz/102" and request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            response = JSONResponse({"error": "Kontrol memerlukan JSON."}, status_code=415)
        else:
            response = await call_next(request)
        if origin and origin_allowed(origin):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-PTZ-API-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Vary"] = "Origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError):
        return JSONResponse({"error": "Perintah PTZ tidak valid."}, status_code=400)

    if api_key:
        key_header = APIKeyHeader(name="X-PTZ-API-Key", auto_error=False)

        def authorized(supplied: str | None = Security(key_header)):
            if not hmac.compare_digest(supplied or "", api_key):
                raise HTTPException(401, detail="Permintaan tidak diizinkan.")
    else:
        def authorized(request: Request):
            if request.url.hostname not in LOCAL_HOSTS:
                raise HTTPException(401, detail="Permintaan tidak diizinkan.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException):
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    @app.get("/api/health", dependencies=[Depends(authorized)], summary="Cek status API")
    def health():
        return {"ok": True}

    @app.get("/api/ptz/102", dependencies=[Depends(authorized)], summary="Cek kesiapan kontrol PTZ")
    def readiness():
        try:
            info = controller.info()
            return {**info, "center_available": center_preset is not None}
        except PTZError as error:
            raise HTTPException(502, detail=str(error)) from error

    @app.post(
        "/api/ptz/102", dependencies=[Depends(authorized)], summary="Kontrol PTZ",
        description="Untuk tekan-tahan: start saat ditekan, renew dengan token selama ditahan, stop saat dilepas. "
                    "Start baru akan menghentikan gerakan lama sebelum mencoba ulang.",
        responses={400: {"description": "Perintah tidak valid"}, 401: {"description": "Tidak diizinkan"},
                   403: {"description": "Origin ditolak"}, 409: {"description": "Gerakan sedang aktif"},
                   502: {"description": "NVR tidak merespons"},
                   503: {"description": "Preset tengah belum dikonfigurasi"}},
    )
    def ptz(command: Annotated[PTZCommand, Body(openapi_examples={
        "start": {"summary": "Start tekan-tahan", "value": {"action": "start", "direction": "left", "speed": 0.15}},
        "renew": {"summary": "Renew gerakan", "value": {"action": "renew", "token": "TOKEN_DARI_START"}},
        "stop": {"summary": "Stop gerakan", "value": {"action": "stop", "token": "TOKEN_DARI_START"}},
        "pulse": {"summary": "Gerak singkat", "value": {"action": "right", "speed": 0.15, "duration": 0.35}},
        "center": {"summary": "Kembali ke tengah", "value": {"action": "center"}},
    })]):
        try:
            if command.action == "center":
                if center_preset is None:
                    raise HTTPException(503, detail="Preset tengah belum diatur.")
                return controller.center(center_preset)
            if command.action == "start":
                try:
                    return controller.start(command.direction, command.speed)
                except PTZBusy:
                    controller.stop()
                    result = controller.start(command.direction, command.speed)
                    result["replaced_previous"] = True
                    return result
            if command.action == "renew":
                return controller.renew(command.token)
            if command.action == "stop":
                return controller.stop(command.token)
            return controller.pulse(command.action, command.speed, command.duration)
        except ValueError as error:
            raise HTTPException(400, detail=str(error)) from error
        except PTZBusy as error:
            raise HTTPException(409, detail=str(error)) from error
        except PTZError as error:
            raise HTTPException(502, detail=str(error)) from error

    return app


def main() -> None:
    load_env(ROOT / ".env")
    parser = argparse.ArgumentParser(description="API PTZ mandiri via NVR DVRIP")
    parser.add_argument("--host", default=os.environ.get("PTZ_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PTZ_API_PORT", "8091")))
    args = parser.parse_args()
    api_key = os.environ.get("PTZ_API_KEY", "")
    if args.host not in LOCAL_HOSTS and not api_key:
        parser.error("PTZ_API_KEY wajib diisi jika API dibuka ke jaringan.")
    if not 1 <= args.port <= 65535:
        parser.error("Port API harus antara 1 dan 65535.")
    try:
        controller, center_preset = configured_controller()
    except (ValueError, PTZError):
        parser.error("Konfigurasi NVR PTZ atau preset tengah tidak valid.")
    uvicorn.run(create_app(controller, api_key, center_preset), host=args.host, port=args.port,
                access_log=False)


if __name__ == "__main__":
    main()
