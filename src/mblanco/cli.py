from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import uvicorn
import websockets

from .core import file_hash, manifest
from .server import create_app


def _request(method: str, url: str, data: bytes | None = None, headers: dict | None = None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _http_manifest(base: str) -> dict:
    status, body = _request("GET", base + "/api/files")
    if status != 200:
        raise ConnectionError(f"servidor respondió HTTP {status}")
    return json.loads(body)["files"]


def _pending(local: dict, remote: dict):
    uploads, downloads, conflicts = [], [], []
    for name, info in local.items():
        if name not in remote:
            uploads.append(name)
        elif info["sha256"] != remote[name]["sha256"]:
            conflicts.append(name)
    for name in remote:
        if name not in local:
            downloads.append(name)
    return uploads, downloads, conflicts


def _url_path(name: str) -> str:
    return "/".join(urllib.parse.quote(part, safe="") for part in name.split("/"))


def _workspace_config_path(project_root: Path) -> Path:
    """Keep room settings outside the project; only compartido/ is shared."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    identity = hashlib.sha256(str(project_root.resolve()).encode()).hexdigest()
    return config_home / "mblanco" / "workspaces" / f"{identity}.json"


def _load_workspace_config(project_root: Path) -> dict | None:
    config_path = _workspace_config_path(project_root)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if config.get("project_root") != str(project_root.resolve()):
        return None
    return config


def _save_workspace_config(project_root: Path, config: dict) -> None:
    config_path = _workspace_config_path(project_root)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**config, "project_root": str(project_root.resolve())}
    fd, temp_name = tempfile.mkstemp(prefix=".mblanco-config-", dir=config_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.replace(temp_name, config_path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _tailscale_ip() -> str | None:
    if not shutil.which("tailscale"):
        return None
    try:
        result = subprocess.run(
            ["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=3, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)


def _setup_workspace(project_root: Path, parser: argparse.ArgumentParser, reconfigure: bool = False) -> dict | None:
    shared = project_root / "compartido"
    if shared.is_symlink():
        parser.error("compartido/ no puede ser un enlace simbólico")

    existing = _load_workspace_config(project_root)
    print(f"Carpeta del proyecto: {project_root}")
    print(f"mblanco solo accederá a: {shared}")
    print("Los demás archivos de esta carpeta no se comparten.")
    if existing is None:
        trusted = input("¿Confías en esta carpeta y quieres configurarla? [s/N] ").strip().lower()
        if trusted not in ("s", "sí", "si"):
            print("Configuración cancelada.")
            return None
    elif not reconfigure:
        return existing
    else:
        print("Esta carpeta ya estaba autorizada; vamos a cambiar su sala.")

    shared.mkdir(parents=True, exist_ok=True)
    print("¿Qué quieres hacer?")
    print("  1) Crear una sala aquí (anfitrión)")
    print("  2) Unirme a la sala de otra persona")
    choice = input("Elige 1 o 2: ").strip()
    if choice == "1":
        port_text = input("Puerto [8765]: ").strip()
        try:
            port = int(port_text) if port_text else 8765
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            parser.error("el puerto debe estar entre 1 y 65535")
        tailscale_ip = _tailscale_ip()
        if tailscale_ip:
            print(f"IP Tailscale detectada: {tailscale_ip}")
            listen = tailscale_ip
            address = f"http://{tailscale_ip}:{port}"
            print("La sala escuchará en esa IP privada de Tailscale.")
        else:
            listen = "127.0.0.1"
            address = f"http://127.0.0.1:{port}"
            print("No detecté Tailscale; esta sala aceptará conexiones locales solamente.")
        config = {
            "role": "host", "listen": listen, "port": port,
            "address": address, "name": getpass.getuser(),
        }
    elif choice == "2":
        address = input("Dirección de la anfitriona (ej. http://100.x.y.z:8765): ").strip().rstrip("/")
        parsed = urllib.parse.urlparse(address)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            parser.error("escribe una dirección completa, por ejemplo http://100.x.y.z:8765")
        name = input(f"Tu nombre [{getpass.getuser()}]: ").strip() or getpass.getuser()
        config = {"role": "join", "address": address, "name": name}
    else:
        parser.error("elige 1 para crear la sala o 2 para unirte")
    _save_workspace_config(project_root, config)
    print("Configuración guardada para esta carpeta; la próxima vez bastará con escribir mblanco.")
    return config


class Chat:
    def __init__(self, base: str, shared: Path, username: str):
        self.base = base.rstrip("/")
        self.ws_url = self.base.replace("https://", "wss://").replace("http://", "ws://") + "/ws"
        self.shared = shared
        self.username = username
        self.socket = None
        self.initial_pending_handled = False
        self.auto_sync_enabled = False
        self._stable_seen: dict[str, dict] = {}

    async def send_event(self, event: dict):
        if self.socket:
            await self.socket.send(json.dumps(event))

    async def upload(self, name: str):
        path = self.shared / name
        digest = file_hash(path)
        await self.send_event({"type": "transfer_started", "path": name})
        status, body = await asyncio.to_thread(
            _request, "PUT", self.base + "/api/files/" + _url_path(name),
            path.read_bytes(), {"Content-Type": "application/octet-stream", "X-Content-SHA256": digest},
        )
        if status == 409:
            print(f"\nConflicto: {name} ya existe en la otra carpeta; no se sobrescribió.")
            return False
        if status != 200:
            print(f"\nNo se pudo subir {name}: HTTP {status} {body.decode(errors='replace')}")
            return False
        print(f"\nArchivo disponible para ambas personas: {name}")
        return True

    async def download(self, name: str):
        local_path = self.shared / name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        await self.send_event({"type": "transfer_started", "path": name})
        status, body = await asyncio.to_thread(_request, "GET", self.base + "/api/files/" + _url_path(name))
        if status != 200:
            print(f"\nNo se pudo recibir {name}: HTTP {status}")
            return False
        if local_path.exists():
            print(f"\nConflicto: {name} ya existe localmente; no se sobrescribió.")
            return False
        tmp = local_path.with_name(local_path.name + ".mblanco-tmp")
        try:
            with tmp.open("xb") as output:
                output.write(body)
            # Atomic creation without overwriting a file created during download.
            try:
                os.link(tmp, local_path)
            except FileExistsError:
                print(f"\nConflicto: {name} apareció durante la transferencia; no se sobrescribió.")
                return False
        finally:
            tmp.unlink(missing_ok=True)
        print(f"\nArchivo disponible: {name}")
        return True

    async def reconcile(self, confirm: bool):
        local = await asyncio.to_thread(manifest, self.shared)
        remote = await asyncio.to_thread(_http_manifest, self.base)
        uploads, downloads, conflicts = _pending(local, remote)
        print(f"Pendientes: ↑ {len(uploads)} por subir · ↓ {len(downloads)} por recibir · conflictos {len(conflicts)}")
        if conflicts:
            print("Conflictos (sin sobrescribir): " + ", ".join(conflicts))
        self.initial_pending_handled = True
        if not uploads and not downloads:
            self.auto_sync_enabled = True
            return
        if confirm:
            print("Por subir: " + (", ".join(uploads) if uploads else "ninguno"))
            print("Por recibir: " + (", ".join(downloads) if downloads else "ninguno"))
            answer = await asyncio.to_thread(input, "¿Transferir estos archivos? [s/N] ")
            if answer.strip().lower() not in ("s", "sí", "si"):
                self.auto_sync_enabled = False
                return
        self.auto_sync_enabled = True
        # Send first; after uploading, the server manifest contains the same file.
        for name in uploads:
            await self.upload(name)
        for name in downloads:
            await self.download(name)

    async def receive(self):
        async for raw in self.socket:
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                continue
            kind = event.get("type")
            if kind == "chat":
                print(f"\n{event.get('sender', 'colega')}: {event.get('text', '')}")
            elif kind == "transfer_started":
                print(f"\nTransfiriendo: {event.get('path', '')}…")
            elif kind == "file_available":
                print(f"\nArchivo disponible: {event.get('path', '')}")

    async def auto_sync(self):
        while True:
            await asyncio.sleep(2)
            if not self.initial_pending_handled or not self.auto_sync_enabled:
                continue
            try:
                local = await asyncio.to_thread(manifest, self.shared)
                remote = await asyncio.to_thread(_http_manifest, self.base)
                uploads, downloads, conflicts = _pending(local, remote)
                stable_uploads = [name for name in uploads if self._stable_seen.get(name) == local[name]]
                for name in uploads:
                    self._stable_seen[name] = local[name]
                for name in conflicts:
                    # Avoid printing the same stable conflict every scan.
                    if name not in self._reported_conflicts:
                        print(f"\nConflicto: {name} existe con contenido distinto; no se sobrescribió.")
                        self._reported_conflicts.add(name)
                for name in stable_uploads:
                    await self.upload(name)
                # Refresh after uploads so our own new file is not considered a download.
                remote = await asyncio.to_thread(_http_manifest, self.base)
                local = await asyncio.to_thread(manifest, self.shared)
                _, downloads, _ = _pending(local, remote)
                for name in downloads:
                    await self.download(name)
            except (OSError, ConnectionError, urllib.error.URLError):
                continue

    async def command_loop(self):
        print("mblanco conectado. F2 Archivos · F3 Sincronizar · F10 Salir")
        while True:
            try:
                line = await asyncio.to_thread(input, "F2 Archivos · F3 Sincronizar · F10 Salir\nmblanco> ")
            except (EOFError, KeyboardInterrupt):
                return
            text = line.strip()
            if not text:
                continue
            if text.lower() in ("/quit", "/salir", "f10"):
                return
            if text.lower() in ("/files", "/archivos", "f2"):
                local = await asyncio.to_thread(manifest, self.shared)
                print("Archivos en compartido/:" if local else "compartido/ está vacío.")
                for name, info in sorted(local.items()):
                    print(f"  {name} ({info['size']} bytes)")
                continue
            if text.lower() in ("/sync", "/sincronizar", "f3"):
                try:
                    await self.reconcile(confirm=True)
                except (OSError, ConnectionError, urllib.error.URLError) as exc:
                    print(f"No se pudo revisar la sala: {exc}")
                continue
            await self.send_event({"type": "chat", "text": text, "sender": self.username})

    async def run(self):
        self.shared.mkdir(parents=True, exist_ok=True)
        try:
            # Probe first so an offline host is not mistaken for an empty remote folder.
            await asyncio.to_thread(_http_manifest, self.base)
        except Exception as exc:
            print(f"La anfitriona no está disponible; no se revisaron archivos remotos ({exc}).")
            return
        try:
            async with websockets.connect(self.ws_url, ping_interval=20) as socket:
                self.socket = socket
                self._reported_conflicts: set[str] = set()
                try:
                    await self.reconcile(confirm=True)
                except (OSError, ConnectionError, urllib.error.URLError) as exc:
                    print(f"No se pudieron revisar pendientes: {exc}")
                    return
                receiver = asyncio.create_task(self.receive())
                syncer = asyncio.create_task(self.auto_sync())
                try:
                    await self.command_loop()
                finally:
                    receiver.cancel()
                    syncer.cancel()
        except (OSError, websockets.WebSocketException) as exc:
            print(f"No se pudo conectar al chat: {exc}")


def _start_host(listen: str, port: int, shared: Path):
    config = uvicorn.Config(create_app(shared), host=listen, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.started:
            return server, thread
        if not thread.is_alive():
            raise RuntimeError("Uvicorn terminó antes de iniciar")
        time.sleep(0.05)
    raise TimeoutError("Uvicorn no inició a tiempo")


def _launch(config: dict, project_root: Path):
    shared = project_root / "compartido"
    shared.mkdir(parents=True, exist_ok=True)
    username = config.get("name") or getpass.getuser()
    if config.get("role") == "host":
        listen = config.get("listen", "127.0.0.1")
        port = int(config.get("port", 8765))
        server, thread = _start_host(listen, port, shared)
        local_address = "127.0.0.1" if listen in ("0.0.0.0", "::") else listen
        print(f"Sala abierta para {project_root}")
        print(f"Carpeta compartida: {shared}")
        print(f"Dirección para unirse: {config.get('address', f'http://{local_address}:{port}')}")
        print("Mantén esta terminal abierta mientras tu amigo se conecta.")
        try:
            asyncio.run(Chat(f"http://{local_address}:{port}", shared, username).run())
        except KeyboardInterrupt:
            pass
        finally:
            server.should_exit = True
            thread.join(timeout=3)
    elif config.get("role") == "join":
        print(f"Proyecto local: {project_root}")
        print(f"Carpeta compartida: {shared}")
        asyncio.run(Chat(config["address"], shared, username).run())


def main():
    parser = argparse.ArgumentParser(prog="mblanco", description="Chat y archivos para un proyecto compartido")
    sub = parser.add_subparsers(dest="command")
    host = sub.add_parser("host", help="crear una sala en esta computadora")
    host.add_argument("--listen", default="127.0.0.1", help="dirección de escucha (por defecto localhost)")
    host.add_argument("--port", type=int, default=8765)
    join = sub.add_parser("join", help="conectarse a una sala existente")
    join.add_argument("address", help="dirección HTTP de la anfitriona, por ejemplo http://127.0.0.1:8765")
    join.add_argument("--name", default=getpass.getuser())
    sub.add_parser("setup", help="configurar o cambiar la sala de esta carpeta")
    args = parser.parse_args()
    project_root = Path.cwd().resolve()
    shared = project_root / "compartido"
    if shared.is_symlink():
        parser.error("compartido/ no puede ser un enlace simbólico")
    shared.mkdir(exist_ok=True)
    if args.command == "host":
        listen = args.listen
        local_address = "127.0.0.1" if listen in ("0.0.0.0", "::") else listen
        config = {
            "role": "host", "listen": listen, "port": args.port,
            "address": f"http://{local_address}:{args.port}", "name": getpass.getuser(),
        }
        _save_workspace_config(project_root, config)
        _launch(config, project_root)
    elif args.command == "join":
        config = {"role": "join", "address": args.address.rstrip("/"), "name": args.name}
        _save_workspace_config(project_root, config)
        _launch(config, project_root)
    else:
        config = _setup_workspace(project_root, parser, reconfigure=args.command == "setup")
        if config:
            _launch(config, project_root)


if __name__ == "__main__":
    main()
