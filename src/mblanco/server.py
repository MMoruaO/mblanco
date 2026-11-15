from __future__ import annotations

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from .core import file_hash, manifest, safe_path


class Room:
    def __init__(self, shared: Path, interval: float = 2.0):
        self.shared = shared
        self.interval = interval
        self.clients: set[WebSocket] = set()
        self.known: dict = {}
        self.candidates: dict = {}

    async def broadcast(self, event: dict) -> None:
        stale = []
        for socket in tuple(self.clients):
            try:
                await socket.send_json(event)
            except Exception:
                stale.append(socket)
        for socket in stale:
            self.clients.discard(socket)

    async def watcher(self) -> None:
        self.known = manifest(self.shared)
        while True:
            await asyncio.sleep(self.interval)
            current = manifest(self.shared)
            for name, info in current.items():
                if name not in self.known:
                    if self.candidates.get(name) == info:
                        await self.broadcast({"type": "file_available", "path": name, **info})
                        self.known[name] = info
                        self.candidates.pop(name, None)
                    else:
                        self.candidates[name] = info


def create_app(shared: Path, interval: float = 2.0) -> FastAPI:
    if shared.is_symlink():
        raise ValueError("compartido/ no puede ser un enlace simbólico")
    shared = shared.resolve()
    shared.mkdir(parents=True, exist_ok=True)
    room = Room(shared, interval)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        room.known = manifest(shared)
        task = asyncio.create_task(room.watcher())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="mblanco", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"ok": True, "protocol": 1}

    @app.get("/api/files")
    async def files():
        return {"files": manifest(shared)}

    @app.get("/api/files/{relative:path}")
    async def download(relative: str):
        try:
            target = safe_path(shared, relative)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.is_file() or target.is_symlink():
            raise HTTPException(status_code=404, detail="archivo no encontrado")
        return FileResponse(target, filename=target.name)

    @app.put("/api/files/{relative:path}")
    async def upload(relative: str, request: Request):
        try:
            target = safe_path(shared, relative)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.is_file() and file_hash(target) == request.headers.get("x-content-sha256"):
                return {"status": "already_present", "sha256": file_hash(target)}
            raise HTTPException(status_code=409, detail="conflicto: la ruta ya existe")
        fd, temp_name = tempfile.mkstemp(prefix=".mblanco-", dir=target.parent)
        temp = Path(temp_name)
        digest = __import__("hashlib").sha256()
        try:
            with os.fdopen(fd, "wb") as output:
                async for chunk in request.stream():
                    digest.update(chunk)
                    output.write(chunk)
            actual = digest.hexdigest()
            expected = request.headers.get("x-content-sha256")
            if expected and actual != expected:
                raise HTTPException(status_code=400, detail="el hash del archivo no coincide")
            # Recheck just before replace to avoid clobbering a concurrent new file.
            if target.exists():
                raise HTTPException(status_code=409, detail="conflicto: la ruta ya existe")
            os.replace(temp, target)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        room.known[relative] = {"size": target.stat().st_size, "sha256": actual}
        await room.broadcast({"type": "file_available", "path": relative, "size": target.stat().st_size, "sha256": actual})
        return {"status": "available", "sha256": actual}

    @app.websocket("/ws")
    async def websocket(socket: WebSocket):
        await socket.accept()
        room.clients.add(socket)
        try:
            while True:
                event = await socket.receive_json()
                kind = event.get("type")
                if kind == "chat" and isinstance(event.get("text"), str):
                    await room.broadcast({"type": "chat", "text": event["text"], "sender": str(event.get("sender", "colega"))[:40]})
                elif kind == "transfer_started":
                    await room.broadcast({"type": "transfer_started", "path": str(event.get("path", ""))})
        except WebSocketDisconnect:
            pass
        finally:
            room.clients.discard(socket)

    return app
