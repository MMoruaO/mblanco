import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mblanco.cli import _load_workspace_config, _save_workspace_config, _setup_workspace, _workspace_config_path


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "mi proyecto"
        self.project.mkdir()
        self.config_home = self.root / "config"
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config_home)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_room_config_is_saved_outside_the_project_and_reused(self):
        config = {"role": "join", "address": "http://100.64.0.1:8765", "name": "amiga"}
        _save_workspace_config(self.project, config)
        config_path = _workspace_config_path(self.project)
        self.assertNotIn(self.project, config_path.parents)
        self.assertEqual(_load_workspace_config(self.project)["role"], "join")
        self.assertFalse((self.project / ".mblanco").exists())

    def test_first_run_trusts_folder_then_configures_join(self):
        parser = argparse.ArgumentParser()
        with patch("builtins.input", side_effect=["s", "2", "http://100.64.0.1:8765", "colega"]):
            config = _setup_workspace(self.project, parser)
        self.assertEqual(config["role"], "join")
        self.assertEqual(config["name"], "colega")
        self.assertTrue((self.project / "compartido").is_dir())
        self.assertEqual(_load_workspace_config(self.project), config | {"project_root": str(self.project.resolve())})


if __name__ == "__main__":
    unittest.main()
