from __future__ import annotations

import asyncio
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Static

from .core import manifest


class PendingTransferScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "keep_pending", "Dejar pendiente")]

    def __init__(self, uploads: list[str], downloads: list[str]):
        super().__init__()
        self.uploads = uploads
        self.downloads = downloads

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Hay archivos pendientes", id="pending-title"),
            Static("Por subir:\n" + ("\n".join(f"  ↑ {name}" for name in self.uploads) or "  ninguno")),
            Static("Por recibir:\n" + ("\n".join(f"  ↓ {name}" for name in self.downloads) or "  ninguno")),
            Static("¿Quieres transferirlos ahora?"),
            Horizontal(
                Button("Transferir", variant="primary", id="transfer"),
                Button("Dejar pendiente", id="later"),
                id="pending-buttons",
            ),
            id="pending-dialog",
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "transfer")

    def action_keep_pending(self) -> None:
        self.dismiss(False)


class FileListScreen(ModalScreen[None]):
    BINDINGS = [("escape", "close", "Volver")]

    def __init__(self, shared: Path):
        super().__init__()
        self.shared = shared

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Archivos en compartido/", id="files-title"),
            DataTable(id="file-table", cursor_type="row"),
            Static("Esc para volver", id="files-hint"),
            id="files-dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Ruta", "Tamaño")
        files = manifest(self.shared)
        for name, info in sorted(files.items()):
            table.add_row(name, _human_size(int(info["size"])))
        if not files:
            table.add_row("(vacío)", "")
        table.focus()

    def action_close(self) -> None:
        self.dismiss(None)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


class MblancoApp(App[None]):
    TITLE = "mblanco"
    SUB_TITLE = "Chat de proyecto"
    BINDINGS = [
        ("f2", "files", "Archivos"),
        ("f3", "sync", "Sincronizar"),
        ("f10", "quit", "Salir"),
    ]
    CSS = """
    Screen {
        background: #101216;
        color: #e5e7eb;
    }
    Header {
        background: #1b1f27;
        color: #f3f4f6;
    }
    #room-status {
        height: 1;
        padding: 0 1;
        background: #242933;
        color: #a8b0bd;
    }
    #conversation {
        height: 1fr;
        margin: 0 1;
        padding: 1 2;
        border: round #333b49;
        background: #14171c;
    }
    #composer {
        margin: 0 1;
        border: tall #4b5563;
    }
    Footer {
        background: #1b1f27;
    }
    #pending-dialog, #files-dialog {
        width: 80%;
        max-width: 90;
        max-height: 85%;
        padding: 1 2;
        border: round #536176;
        background: #1b1f27;
        align: center middle;
    }
    #pending-title, #files-title {
        width: 1fr;
        text-style: bold;
        color: #f3f4f6;
        margin-bottom: 1;
    }
    #pending-buttons {
        height: 3;
        align: right middle;
        margin-top: 1;
    }
    #pending-buttons Button {
        margin-left: 1;
    }
    #file-table {
        height: 1fr;
    }
    #files-hint {
        height: 1;
        color: #9ca3af;
        margin-top: 1;
    }
    """

    def __init__(self, chat, banner: str = ""):
        super().__init__()
        self.chat = chat
        self.banner = banner or f"Sala: {chat.base}"
        self._network_tasks: list[asyncio.Task] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield Static(self.banner, id="room-status")
        yield RichLog(id="conversation", wrap=True, markup=False, auto_scroll=True)
        yield Input(placeholder="Escribe un mensaje y presiona Enter…", id="composer")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one(Input).focus()
        self.write_line("Conectado. Los archivos se comparten solo desde compartido/.", "system")
        self._network_tasks = [
            asyncio.create_task(self.chat.receive()),
            asyncio.create_task(self.chat.auto_sync()),
        ]
        self.run_worker(self._initial_sync(), name="initial-sync", group="sync")

    def on_unmount(self) -> None:
        for task in self._network_tasks:
            task.cancel()

    async def _initial_sync(self) -> None:
        try:
            await self.chat.reconcile(confirm=True, confirmer=self.confirm_pending)
        except (OSError, ConnectionError):
            self.write_line("No se pudieron revisar los pendientes de la sala.", "error")

    async def confirm_pending(self, uploads: list[str], downloads: list[str]) -> bool:
        return bool(await self.push_screen_wait(PendingTransferScreen(uploads, downloads)))

    def write_line(self, message: str, kind: str = "system") -> None:
        if not self.is_mounted:
            return
        log = self.query_one(RichLog)
        styles = {
            "chat": "#e5e7eb",
            "progress": "#f0b35c",
            "available": "#82c99a",
            "error": "bold #f08080",
            "system": "#8fa3bf",
        }
        log.write(Text(message, style=styles.get(kind, "#e5e7eb")))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        message = event.value.strip()
        event.input.value = ""
        if not message:
            return
        command = message.lower()
        if command in ("/files", "/archivos", "f2"):
            await self.action_files()
        elif command in ("/sync", "/sincronizar", "f3"):
            self.action_sync()
        elif command in ("/quit", "/salir", "f10"):
            self.exit()
        else:
            await self.chat.send_event({"type": "chat", "text": message, "sender": self.chat.username})

    async def action_files(self) -> None:
        await self.push_screen(FileListScreen(self.chat.shared))

    def action_sync(self) -> None:
        self.run_worker(self._manual_sync(), name="manual-sync", group="sync")

    async def _manual_sync(self) -> None:
        try:
            await self.chat.reconcile(confirm=True, confirmer=self.confirm_pending)
        except (OSError, ConnectionError):
            self.write_line("No se pudo revisar la sala; comprueba la conexión con la anfitriona.", "error")

