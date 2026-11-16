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


def _request(
    method: str,
    url: str,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 30,
):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
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


def _refresh_guided_host(config: dict, tailscale_ip: str | None) -> dict:
    """Refresh a guided host's advertised/bound address when Tailscale is up."""
    refreshed = dict(config)
    if refreshed.get("guided", True) and tailscale_ip:
        port = int(refreshed.get("port", 8765))
        refreshed["listen"] = tailscale_ip
        refreshed["address"] = f"http://{tailscale_ip}:{port}"
    return refreshed


def _verify_host(address: str) -> None:
    status, body = _request("GET", address.rstrip("/") + "/health", timeout=5)
    if status != 200:
        raise ConnectionError(f"el servidor respondió HTTP {status}")
    try:
        health = json.loads(body)
    except ValueError as exc:
        raise ConnectionError("el servidor respondió algo distinto a mblanco") from exc
    if health.get("ok") is not True:
        raise ConnectionError("el servidor no confirmó que mblanco está listo")


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
            "address": address, "name": getpass.getuser(), "guided": True,
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
        self.ui = None

    def notify(self, message: str, kind: str = "system") -> None:
        if self.ui is not None:
            self.ui.write_line(message, kind)
        else:
            print(message)

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
            self.notify(f"Conflicto: {name} ya existe en la otra carpeta; no se sobrescribió.", "error")
            return False
        if status != 200:
            self.notify(f"No se pudo subir {name}: HTTP {status} {body.decode(errors='replace')}", "error")
            return False
        self.notify(f"Archivo disponible para ambas personas: {name}", "available")
        return True

    async def download(self, name: str):
        local_path = self.shared / name
        local_path.parent.mkdir(parents=True, exist_ok=True)
        await self.send_event({"type": "transfer_started", "path": name})
        self.notify(f"Recibiendo: {name}…", "progress")
        status, body = await asyncio.to_thread(_request, "GET", self.base + "/api/files/" + _url_path(name))
        if status != 200:
            self.notify(f"No se pudo recibir {name}: HTTP {status}", "error")
            return False
        if local_path.exists():
            self.notify(f"Conflicto: {name} ya existe localmente; no se sobrescribió.", "error")
            return False
        tmp = local_path.with_name(local_path.name + ".mblanco-tmp")
        try:
            with tmp.open("xb") as output:
                output.write(body)
            # Atomic creation without overwriting a file created during download.
            try:
                os.link(tmp, local_path)
            except FileExistsError:
                self.notify(f"Conflicto: {name} apareció durante la transferencia; no se sobrescribió.", "error")
                return False
        finally:
            tmp.unlink(missing_ok=True)
        self.notify(f"Archivo recibido: {name}", "available")
        return True

    async def reconcile(self, confirm: bool, confirmer=None):
        local = await asyncio.to_thread(manifest, self.shared)
        remote = await asyncio.to_thread(_http_manifest, self.base)
        uploads, downloads, conflicts = _pending(local, remote)
        self.notify(f"Pendientes: ↑ {len(uploads)} por subir · ↓ {len(downloads)} por recibir · conflictos {len(conflicts)}")
        if conflicts:
            self.notify("Conflictos (sin sobrescribir): " + ", ".join(conflicts), "error")
        self.initial_pending_handled = True
        if not uploads and not downloads:
            self.auto_sync_enabled = True
            return
        if confirm:
            if confirmer is None:
                answer = await asyncio.to_thread(input, "¿Transferir estos archivos? [s/N] ")
                approved = answer.strip().lower() in ("s", "sí", "si")
            else:
                approved = await confirmer(uploads, downloads)
            if not approved:
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
                self.notify(f"{event.get('sender', 'colega')}: {event.get('text', '')}", "chat")
            elif kind == "transfer_started":
                self.notify(f"Transfiriendo: {event.get('path', '')}…", "progress")
            elif kind == "file_available":
                self.notify(f"Disponible en la sala: {event.get('path', '')}", "available")

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
                        self.notify(f"Conflicto: {name} existe con contenido distinto; no se sobrescribió.", "error")
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

    async def run(self, banner: str = ""):
        self.shared.mkdir(parents=True, exist_ok=True)
        try:
            # Check reachability quickly; do not hash the full shared folder before showing the TUI.
            await asyncio.to_thread(_verify_host, self.base)
        except Exception as exc:
            print(
                "No pude alcanzar al anfitrión en 5 segundos; no revisé archivos remotos. "
                f"Comprueba que la sala siga abierta y que la dirección use la IP Tailscale y el puerto correctos. ({exc})"
            )
            return
        try:
            async with websockets.connect(self.ws_url, ping_interval=20, open_timeout=5) as socket:
                self.socket = socket
                self._reported_conflicts: set[str] = set()
                from .ui import MblancoApp

                self.ui = MblancoApp(self, banner)
                try:
                    await self.ui.run_async()
                finally:
                    self.ui = None
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
        tailscale_ip = _tailscale_ip()
        config = _refresh_guided_host(config, tailscale_ip)
        listen = config.get("listen", "127.0.0.1")
        port = int(config.get("port", 8765))
        if config.get("guided", True) and tailscale_ip:
            _save_workspace_config(project_root, config)
        try:
            server, thread = _start_host(listen, port, shared)
        except RuntimeError as exc:
            print(f"No pude iniciar la sala en {listen}:{port}. Puede que el puerto ya esté ocupado. {exc}")
            return
        local_address = "127.0.0.1" if listen in ("0.0.0.0", "::") else listen
        address = config.get("address", f"http://{local_address}:{port}")
        try:
            _verify_host(f"http://{local_address}:{port}")
        except (OSError, ConnectionError) as exc:
            server.should_exit = True
            thread.join(timeout=3)
            print(f"La sala arrancó, pero no pude verificarla localmente: {exc}")
            return
        if address.startswith(("http://127.0.0.1", "http://localhost")) and listen in ("127.0.0.1", "localhost"):
            banner = "Servidor verificado localmente · Sin IP Tailscale; solo acepta conexiones locales"
        elif address.startswith(("http://127.0.0.1", "http://localhost")):
            banner = "Servidor verificado · Sin IP Tailscale detectada; comparte una IP de esta computadora"
        else:
            banner = f"Servidor verificado ✓ · Pásale a tu colega: mblanco join {address}"
        try:
            asyncio.run(Chat(f"http://{local_address}:{port}", shared, username).run(
                banner=banner
            ))
        except KeyboardInterrupt:
            pass
        finally:
            server.should_exit = True
            thread.join(timeout=3)
    elif config.get("role") == "join":
        asyncio.run(Chat(config["address"], shared, username).run(
            banner=f"Conectado · {project_root}"
        ))


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
        tailscale_ip = _tailscale_ip()
        config = {
            "role": "host", "listen": listen, "port": args.port,
            "address": (
                f"http://{tailscale_ip}:{args.port}"
                if listen in ("0.0.0.0", "::") and tailscale_ip
                else f"http://{local_address}:{args.port}"
            ),
            "name": getpass.getuser(), "guided": False,
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
