import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


PACKAGE = Path(__file__).resolve().parent.parent / "packaging" / "woodpecker"


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.target, self.etc, self.var = [self.root / name for name in ("target", "etc", "var")]
        (self.target / "scripts").mkdir(parents=True)
        shutil.copyfile(PACKAGE / "app/scripts/runtime.py", self.target / "scripts/runtime.py")
        self.environment = dict(os.environ, TRIM_APPDEST=str(self.target), TRIM_PKGETC=str(self.etc),
                                TRIM_PKGVAR=str(self.var), wizard_page_origin="https://nas.example.com",
                                wizard_webhook_origin="", wizard_github_admin="example-user",
                                wizard_github_client="test-client", wizard_github_secret="test-secret")

    def run_callback(self, name, **changes):
        return subprocess.run([str(PACKAGE / "cmd" / name)],
                              env=dict(self.environment, **changes), capture_output=True, text=True)

    def install(self):
        result = self.run_callback("install_callback")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_initial_install_and_config_preserve_existing_credentials(self):
        self.install()
        previous = (self.etc / "current/server.env").read_text()
        self.assertEqual(self.run_callback("install_callback").returncode, 0)
        result = self.run_callback("config_callback", wizard_github_secret="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.etc / "current/server.env").read_text(), previous)
        self.assertFalse(list(self.etc.glob(".wizard-*")))
        self.assertNotIn("test-secret", result.stdout + result.stderr)

    def test_invalid_configuration_does_not_replace_current(self):
        self.install()
        previous = os.readlink(self.etc / "current")
        result = self.run_callback("config_callback", wizard_page_origin="https://nas.example.com/other")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(os.readlink(self.etc / "current"), previous)
        self.assertFalse(list(self.etc.glob(".wizard-*")))

    def test_upgrade_requires_fresh_matching_maintenance_record(self):
        self.install()
        result = self.run_callback("upgrade_init")
        self.assertNotEqual(result.returncode, 0)
        revision = Path(os.readlink(self.etc / "current")).name
        record = {"operation": "upgrade", "revision": revision, "created": time.time(),
                  "no_running_tasks": True, "backup_sha256": "a" * 64}
        (self.etc / ".maintenance.json").write_text(json.dumps(record))
        result = self.run_callback("upgrade_init")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.etc / ".maintenance.json").exists())
        secret = (self.etc / "current/server.env").read_text()
        result = self.run_callback("upgrade_callback")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(Path(os.readlink(self.etc / "current")).name, revision)
        self.assertEqual((self.etc / "current/server.env").read_text(), secret)

    def test_uninstall_callback_does_not_remove_persistent_data(self):
        self.install()
        database = self.var / "server/test-database"
        database.write_text("keep")
        result = self.run_callback("uninstall_callback")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(database.read_text(), "keep")

    def test_start_revokes_stopped_instance_maintenance_permission(self):
        self.install()
        permit = self.etc / ".maintenance.json"
        permit.write_text("{}")
        result = subprocess.run([str(PACKAGE / "cmd/main"), "start"],
                                env=self.environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(permit.exists())


if __name__ == "__main__":
    unittest.main()
