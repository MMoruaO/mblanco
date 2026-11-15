import asyncio
import tempfile
import unittest
from pathlib import Path

from textual.widgets import DataTable, Footer, Input

from mblanco.ui import FileListScreen, MblancoApp, PendingTransferScreen


class FakeChat:
    def __init__(self, shared: Path, pending: bool = False):
        self.shared = shared
        self.base = "http://localhost:8765"
        self.username = "ana"
        self.pending = pending
        self.approved = None
        self.events = []

    async def receive(self):
        await asyncio.Future()

    async def auto_sync(self):
        await asyncio.Future()

    async def reconcile(self, confirm: bool, confirmer=None):
        if self.pending and confirm:
            self.approved = await confirmer(["subir.txt"], ["recibir.txt"])

    async def send_event(self, event):
        self.events.append(event)


class UiTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_screen_sends_messages_and_f2_opens_file_list(self):
        with tempfile.TemporaryDirectory() as temp:
            shared = Path(temp)
            (shared / "muestra.csv").write_text("id\n1\n", encoding="utf-8")
            chat = FakeChat(shared)
            app = MblancoApp(chat)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.query_one(Input), Input)
                self.assertIsInstance(app.query_one(Footer), Footer)
                app.query_one(Input).value = "hola"
                await pilot.press("enter")
                self.assertEqual(chat.events[0]["text"], "hola")

                await pilot.press("f2")
                await pilot.pause()
                self.assertIsInstance(app.screen, FileListScreen)
                self.assertEqual(app.screen.query_one(DataTable).row_count, 1)
                await pilot.press("escape")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, FileListScreen)

    async def test_pending_transfer_uses_a_confirmation_screen(self):
        with tempfile.TemporaryDirectory() as temp:
            chat = FakeChat(Path(temp), pending=True)
            app = MblancoApp(chat)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, PendingTransferScreen)
                await pilot.click("#transfer")
                await pilot.pause()
                self.assertTrue(chat.approved)


if __name__ == "__main__":
    unittest.main()
