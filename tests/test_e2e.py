import asyncio
import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path

import uvicorn
import websockets

from mblanco.cli import Chat, _http_manifest, _pending, _verify_host
from mblanco.core import manifest
from mblanco.server import create_app


class EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.host_dir = self.root / "proyecto anfitriona" / "compartido"
        self.peer_dir = self.root / "proyecto colega distinto" / "compartido"
        self.host_dir.mkdir(parents=True)
        self.peer_dir.mkdir(parents=True)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        config = uvicorn.Config(create_app(self.host_dir, 0.1), host="127.0.0.1", port=self.port, log_level="error")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                break
            await asyncio.sleep(0.025)
        self.assertTrue(self.server.started)
        self.base = f"http://127.0.0.1:{self.port}"

    async def asyncTearDown(self):
        self.server.should_exit = True
        await asyncio.to_thread(self.thread.join, 3)
        self.temp.cleanup()

    async def test_chat_and_file_transfer_between_different_folders(self):
        ws_url = self.base.replace("http://", "ws://") + "/ws"
        async with websockets.connect(ws_url) as first, websockets.connect(ws_url) as second:
            await first.send(json.dumps({"type": "chat", "sender": "ana", "text": "hola"}))
            received = json.loads(await asyncio.wait_for(second.recv(), 2))
            self.assertEqual((received["sender"], received["text"]), ("ana", "hola"))

        source = self.peer_dir / "datos" / "muestra.csv"
        source.parent.mkdir()
        source.write_text("id,valor\n1,42\n", encoding="utf-8")
        peer = Chat(self.base, self.peer_dir, "colega")
        self.assertTrue(await peer.upload("datos/muestra.csv"))
        self.assertEqual((self.host_dir / "datos/muestra.csv").read_text(), "id,valor\n1,42\n")

        (self.host_dir / "salida.txt").write_text("respuesta", encoding="utf-8")
        self.assertTrue(await peer.download("salida.txt"))
        self.assertEqual((self.peer_dir / "salida.txt").read_text(), "respuesta")

    async def test_host_health_check_confirms_ready_server(self):
        await asyncio.to_thread(_verify_host, self.base)

    async def test_reconnect_pending_counts_and_conflict_does_not_overwrite(self):
        (self.peer_dir / "nuevo-local.txt").write_text("local", encoding="utf-8")
        (self.host_dir / "para-recibir-1.txt").write_text("uno", encoding="utf-8")
        (self.host_dir / "para-recibir-2.txt").write_text("dos", encoding="utf-8")
        uploads, downloads, conflicts = _pending(
            manifest(self.peer_dir), await asyncio.to_thread(_http_manifest, self.base)
        )
        self.assertEqual(uploads, ["nuevo-local.txt"])
        self.assertEqual(set(downloads), {"para-recibir-1.txt", "para-recibir-2.txt"})
        self.assertFalse(conflicts)

        (self.peer_dir / "choque.txt").write_text("versión local", encoding="utf-8")
        (self.host_dir / "choque.txt").write_text("versión remota", encoding="utf-8")
        local = manifest(self.peer_dir)
        remote = await asyncio.to_thread(_http_manifest, self.base)
        _, _, conflicts = _pending(local, remote)
        self.assertEqual(conflicts, ["choque.txt"])
        peer = Chat(self.base, self.peer_dir, "colega")
        self.assertFalse(await peer.upload("choque.txt"))
        self.assertEqual((self.host_dir / "choque.txt").read_text(), "versión remota")
        self.assertEqual((self.peer_dir / "choque.txt").read_text(), "versión local")

    async def test_requested_path_cannot_escape_shared(self):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(self.base + "/api/files/%2e%2e/secreto", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as error:
            await asyncio.to_thread(urllib.request.urlopen, request)
        self.assertEqual(error.exception.code, 400)

    async def test_incomplete_transfer_tempfiles_are_not_listed(self):
        (self.host_dir / ".mblanco-uploading").write_bytes(b"partial")
        (self.host_dir / "real.txt").write_bytes(b"complete")
        remote = await asyncio.to_thread(_http_manifest, self.base)
        self.assertEqual(list(remote), ["real.txt"])


if __name__ == "__main__":
    unittest.main()
