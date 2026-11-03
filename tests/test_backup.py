import io
import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest import mock


SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.app = self.root / "app"
        (self.app / "etc/revisions/demo").mkdir(parents=True)
        (self.app / "etc/current").symlink_to("revisions/demo")
        (self.app / "etc/revisions/demo/server.env").write_text("SECRET=test-value\n")
        (self.app / "etc/revisions/demo/server.env").chmod(0o600)
        for name in ("server", "agent"):
            (self.app / "var" / name).mkdir(parents=True)
        (self.app / "var/server/database").write_bytes(b"database-snapshot")
        (self.app / "var/agent/agent-id").write_bytes(b"agent-identity")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.docker = self.bin / "docker"
        self.docker.write_text("#!/bin/sh\nexit 0\n")
        self.docker.chmod(0o755)

    def run_tool(self, name, *args):
        environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"])
        return subprocess.run([sys.executable, str(SCRIPTS / name), *map(str, args)],
                              env=environment, capture_output=True, text=True)

    def backup(self):
        archive = self.root / "backup.tar.gz"
        result = self.run_tool("backup-data", "--app-root", self.app, "--output", archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        return archive

    def prepare(self, archive, operation="upgrade", app=None):
        return self.run_tool("prepare-maintenance", "--app-root", app or self.app,
                             "--backup", archive, "--operation", operation)

    def assert_rejected(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("test-value", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_stopped_backup_restores_only_to_new_offline_directory(self):
        archive = self.root / "backup.tar.gz"
        result = self.run_tool("backup-data", "--app-root", self.app, "--output", archive)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("test-value", result.stdout + result.stderr)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        restored = self.root / "offline"
        result = self.run_tool("restore-data", "--archive", archive, "--destination", restored)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((restored / "var/server/database").read_bytes(), b"database-snapshot")
        self.assertEqual((restored / "etc/current/server.env").stat().st_mode & 0o777, 0o600)
        self.assertEqual((restored / "backup-manifest.json").stat().st_mode & 0o777, 0o600)
        for path in (restored, restored / "etc", restored / "var", restored / "var/server"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertIn("not production ready", result.stdout)
        self.assertNotEqual(self.run_tool("restore-data", "--archive", archive,
                                         "--destination", restored).returncode, 0)

    def test_backup_refuses_live_application(self):
        self.docker.write_text("#!/bin/sh\nprintf 'running-container\\n'\n")
        archive = self.root / "backup.tar.gz"
        result = self.run_tool("backup-data", "--app-root", self.app, "--output", archive)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(archive.exists())

    def test_manifest_binds_instance_revision_and_all_regular_files(self):
        archive = self.backup()
        with tarfile.open(archive) as handle:
            manifest = json.load(handle.extractfile("backup-manifest.json"))
            files = {item.name for item in handle.getmembers()
                     if item.isfile() and item.name != "backup-manifest.json"}
        self.assertEqual(manifest["revision"], "demo")
        self.assertEqual(manifest["current"], "revisions/demo")
        self.assertEqual(set(manifest["files"]), files)
        self.assertEqual(manifest["files"]["var/server/database"], {
            "size": len(b"database-snapshot"),
            "sha256": hashlib.sha256(b"database-snapshot").hexdigest(),
        })
        self.assertNotIn("test-value", json.dumps(manifest))

    def test_current_backup_writes_lifecycle_compatible_permit(self):
        archive = self.backup()
        for operation in ("upgrade", "uninstall"):
            result = self.prepare(archive, operation)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("test-value", result.stdout + result.stderr)
            path = self.app / "etc/.maintenance.json"
            permit = json.loads(path.read_text())
            self.assertEqual(permit["operation"], operation)
            self.assertEqual(permit["revision"], "demo")
            self.assertIs(permit["no_running_tasks"], True)
            self.assertEqual(permit["backup_sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("15 minutes", result.stdout)

    def test_maintenance_rejects_legacy_and_empty_archives(self):
        archive = self.backup()
        legacy = self.root / "legacy.tar.gz"
        with tarfile.open(archive) as source, tarfile.open(legacy, "w:gz") as target:
            for item in source.getmembers():
                if item.name != "backup-manifest.json":
                    target.addfile(item, source.extractfile(item) if item.isfile() else None)
        self.assert_rejected(self.prepare(legacy))
        with tarfile.open(legacy, "w:gz"):
            pass
        self.assert_rejected(self.prepare(legacy))
        self.assertFalse((self.app / "etc/.maintenance.json").exists())

    def test_maintenance_rejects_changed_content_even_with_same_size(self):
        archive = self.backup()
        for relative in ("var/server/database", "var/agent/agent-id", "etc/revisions/demo/server.env"):
            path = self.app / relative
            original = path.read_bytes()
            path.write_bytes(b"x" * len(original))
            with self.subTest(relative=relative):
                self.assert_rejected(self.prepare(archive))
            path.write_bytes(original)
        extra = self.app / "var/server/new-wal"
        extra.write_bytes(b"new-data")
        self.assert_rejected(self.prepare(archive))
        extra.unlink()
        (self.app / "var/agent/agent-id").unlink()
        self.assert_rejected(self.prepare(archive))

    def test_failed_reverification_revokes_previous_permit(self):
        archive = self.backup()
        self.assertEqual(self.prepare(archive).returncode, 0)
        (self.app / "var/server/database").write_bytes(b"changed")
        self.assert_rejected(self.prepare(archive))
        self.assertFalse((self.app / "etc/.maintenance.json").exists())

    def test_maintenance_rejects_different_revision_or_instance(self):
        archive = self.backup()
        current = self.app / "etc/current"
        (self.app / "etc/revisions/new").mkdir()
        (self.app / "etc/revisions/new/server.env").write_text("SECRET=test-value\n")
        current.unlink()
        current.symlink_to("revisions/new")
        self.assert_rejected(self.prepare(archive))
        current.unlink()
        current.symlink_to("revisions/demo")
        # Identical bytes under different resolved instance paths still fail.
        import shutil
        clone = self.root / "another-app"
        shutil.copytree(self.app, clone, symlinks=True)
        self.assert_rejected(self.prepare(archive, app=clone))

    def test_maintenance_rejects_running_tasks(self):
        archive = self.backup()
        self.docker.write_text('#!/bin/sh\ncase "$*" in *label=wp_uuid*) printf "task\\n";; esac\n')
        self.assert_rejected(self.prepare(archive))
        self.assertFalse((self.app / "etc/.maintenance.json").exists())
        self.assert_rejected(self.run_tool("backup-data", "--app-root", self.app,
                                          "--output", self.root / "task-backup.tar.gz"))

    def test_archive_payload_tampering_is_rejected(self):
        archive = self.backup()
        tampered = self.root / "tampered.tar.gz"
        with tarfile.open(archive) as source, tarfile.open(tampered, "w:gz") as target:
            for item in source.getmembers():
                payload = source.extractfile(item) if item.isfile() else None
                if item.name == "var/server/database":
                    payload = io.BytesIO(b"x" * item.size)
                target.addfile(item, payload)
        self.assert_rejected(self.prepare(tampered))
        destination = self.root / "tampered-output"
        self.assert_rejected(self.run_tool("restore-data", "--archive", tampered,
                                          "--destination", destination))
        self.assertFalse(destination.exists())

    def test_output_parent_alias_cannot_write_inside_application(self):
        alias = self.root / "data-alias"
        alias.symlink_to(self.app / "var", target_is_directory=True)
        for output in (alias / "forbidden.tar.gz", alias / "new-parent/forbidden.tar.gz"):
            with self.subTest(output=output):
                self.assert_rejected(self.run_tool("backup-data", "--app-root", self.app,
                                                  "--output", output))
                self.assertFalse(output.exists())

    def test_backup_rejects_unexpected_source_symlinks(self):
        for relative in ("var/server/leak", "var/agent/leak", "etc/revisions/demo/leak"):
            path = self.app / relative
            path.symlink_to(self.app / "etc/revisions/demo/server.env")
            with self.subTest(relative=relative):
                archive = self.root / "rejected.tar.gz"
                self.assert_rejected(self.run_tool("backup-data", "--app-root", self.app,
                                                  "--output", archive))
                self.assertFalse(archive.exists())
            path.unlink()

    def test_backup_rejects_source_change_between_snapshot_passes(self):
        tool = runpy.run_path(str(SCRIPTS / "backup-data"))
        main = tool["main"]
        original = main.__globals__["snapshot"]
        calls = []

        def changing_snapshot(*args, **kwargs):
            result = original(*args, **kwargs)
            calls.append(True)
            if len(calls) == 1:
                (self.app / "var/server/database").write_bytes(b"new-database-data")
            return result

        archive = self.root / "inconsistent.tar.gz"
        with mock.patch.dict(main.__globals__, snapshot=changing_snapshot, stopped=lambda: None), \
                mock.patch.object(sys, "argv", ["backup-data", "--app-root", str(self.app),
                                               "--output", str(archive)]):
            with self.assertRaises(ValueError):
                main()
        self.assertEqual(len(calls), 2)
        self.assertFalse(archive.exists())

    def test_restore_rejects_all_other_links_duplicates_and_unsafe_entries(self):
        def info(name, kind=tarfile.REGTYPE, link=""):
            item = tarfile.TarInfo(name)
            item.type, item.linkname = kind, link
            return item

        cases = [
            [info("/etc/escape")], [info("etc/../escape")], [info("etc//escape")],
            [info("etc/./escape")], [info("etc/file"), info("etc/file")],
            [info("etc", tarfile.DIRTYPE), info("etc/", tarfile.DIRTYPE)],
            [info("etc/file", tarfile.LNKTYPE, "etc/other")],
            [info("var/server/link", tarfile.SYMTYPE, "../../etc")],
            [info("etc/current", tarfile.SYMTYPE, "revisions/demo"), info("etc/current/leak")],
            [info("etc/file"), info("etc/file/child")], [info("etc/fifo", tarfile.FIFOTYPE)],
            [info("etc/device", tarfile.CHRTYPE)], [info("var/unexpected/file")],
            [info("backup-manifest.json/child")],
            [info("etc/current", tarfile.SYMTYPE, "revisions/demo")],
        ]
        for index, entries in enumerate(cases):
            with self.subTest(index=index):
                archive = self.root / "malicious.tar.gz"
                with tarfile.open(archive, "w:gz") as handle:
                    for item in entries:
                        handle.addfile(item)
                restored = self.root / "malicious-output"
                self.assert_rejected(self.run_tool("restore-data", "--archive", archive,
                                                  "--destination", restored))
                self.assertFalse(restored.exists())

    def test_restore_never_applies_public_directory_modes_or_archive_owner(self):
        archive = self.root / "permissive.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            for name in ("etc", "etc/revisions", "etc/revisions/demo", "var/server"):
                item = tarfile.TarInfo(name)
                item.type, item.mode, item.uid, item.gid = tarfile.DIRTYPE, 0o7777, 12345, 12345
                handle.addfile(item)
            item = tarfile.TarInfo("var/server/database")
            item.mode, item.size, item.uid = 0o6666, 1, 12345
            handle.addfile(item, io.BytesIO(b"x"))
        destination = self.root / "private-output"
        result = self.run_tool("restore-data", "--archive", archive, "--destination", destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path in destination.rglob("*"):
            self.assertEqual(path.stat().st_uid, os.getuid())
            self.assertEqual(path.stat().st_mode & 0o7777, 0o700 if path.is_dir() else 0o600)

    def test_restore_rejects_traversal_and_link_escape(self):
        for name, link in (("../escape", None), ("etc/current", "/etc")):
            archive = self.root / "unsafe.tar.gz"
            with tarfile.open(archive, "w:gz") as handle:
                info = tarfile.TarInfo(name)
                if link:
                    info.type = tarfile.SYMTYPE
                    info.linkname = link
                    handle.addfile(info)
                else:
                    info.size = 1
                    handle.addfile(info, io.BytesIO(b"x"))
            restored = self.root / "unsafe-output"
            result = self.run_tool("restore-data", "--archive", archive, "--destination", restored)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(restored.exists())
            self.assertFalse((self.root / "escape").exists())


if __name__ == "__main__":
    unittest.main()
