"""Public CLI/operation tests. All credentials are synthetic and temporary."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import socketserver
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging/woodpecker/app/scripts/runtime.py"
SPEC = importlib.util.spec_from_file_location("woodpecker_runtime", SCRIPT)
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wp-runtime-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.etc = self.base / "etc"
        self.etc.mkdir(mode=0o700)
        self.input = self.etc / "input.json"
        self.var, self.target = self.base / "var", self.base / "target"
        self.config = dict(page_origin="https://nas.example.com", webhook_origin="",
                           github_admin="test-admin", github_client="test-client-id",
                           github_secret="synthetic-oauth-secret")

    def cli(self, *args, data=None, expected=0):
        if data is not None:
            self.input.write_text(json.dumps(data), encoding="utf-8")
            self.input.chmod(0o600)
        result = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def configure(self, data=None, *extra, expected=0):
        return self.cli("configure", "--input", self.input, "--etc", self.etc,
                        "--var", self.var, "--target", self.target, *extra,
                        data=self.config if data is None else data, expected=expected)

    def env(self, name="server.env", revision="current"):
        path = self.etc / revision / name
        return dict(line.split("=", 1) for line in path.read_text().splitlines())

    def test_configuration_public_contract_and_permissions(self):
        result = self.configure()
        revision = result.stdout.strip()
        self.assertRegex(revision, r"^\d{8}T\d{6}Z-[a-f0-9]{16}$")
        self.assertEqual(os.readlink(self.etc / "current"), "revisions/" + revision)
        self.assertEqual(set(p.name for p in (self.etc / "current").iterdir()),
                         {"server.env", "agent.env", "nginx.conf", "metadata.json"})
        for path in (self.etc, self.etc / "revisions", self.etc / "current"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for name in ("server.env", "agent.env", "metadata.json", "nginx.conf"):
            path = self.etc / "current" / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644 if name == "nginx.conf" else 0o600)
        server, agent = self.env(), self.env("agent.env")
        self.assertEqual(server["WOODPECKER_HOST"], "https://nas.example.com/app/woodpecker")
        self.assertEqual(server["WOODPECKER_EXPERT_WEBHOOK_HOST"], server["WOODPECKER_HOST"])
        self.assertEqual(server["WOODPECKER_AGENT_SECRET"], agent["WOODPECKER_AGENT_SECRET"])
        self.assertGreaterEqual(len(server["WOODPECKER_AGENT_SECRET"]), 64)
        self.assertEqual(server["WOODPECKER_OPEN"], "false")
        self.assertEqual(server["WOODPECKER_REPO_OWNERS"], "test-admin")
        self.assertEqual(agent["WOODPECKER_MAX_WORKFLOWS"], "1")
        self.assertEqual(agent["WOODPECKER_BACKEND_DOCKER_LIMIT_MEM"], "1610612736")
        metadata = self.cli("metadata", "--etc", self.etc)
        self.assertNotIn(self.config["github_secret"], metadata.stdout + result.stdout + result.stderr)
        self.assertNotIn(server["WOODPECKER_AGENT_SECRET"], metadata.stdout)
        self.assertNotIn("github_secret", json.loads(metadata.stdout))
        self.assertEqual(json.loads(metadata.stdout)["server_image"],
                         "woodpeckerci/woodpecker-server:v3.20.1")
        self.assertEqual(self.cli("metadata", "--etc", self.etc, "--field", "revision").stdout.strip(), revision)
        self.cli("metadata", "--etc", self.etc, "--field", "github_secret", expected=1)
        # Directory ownership and data initialization belong to the lifecycle.
        self.assertFalse(self.var.exists())
        self.assertFalse(self.target.exists())

    def test_secret_special_characters_are_literal_not_shell_code(self):
        marker = self.base / "SHOULD_NOT_EXIST"
        special = "  # 'quote' \"double\" \\ $HOME ${UNSET} $(touch " + str(marker) + ") `id` = tail  "
        result = self.configure(dict(self.config, github_secret=special))
        self.assertEqual(self.env()["WOODPECKER_GITHUB_SECRET"], special)
        self.assertFalse(marker.exists())
        self.assertNotIn(special, result.stdout + result.stderr)
        metadata = (self.etc / "current/metadata.json").read_text()
        nginx = (self.etc / "current/nginx.conf").read_text()
        self.assertNotIn(special, metadata + nginx)

    def test_blank_secret_initially_fails_and_update_preserves_both_secrets(self):
        self.configure(dict(self.config, github_secret=""), expected=1)
        self.assertFalse((self.etc / "current").exists())
        first = self.configure().stdout.strip()
        before = self.env()
        second = self.configure(dict(self.config, github_secret="", page_origin="https://new.example.com",
                                     webhook_origin="https://hooks.example.com")).stdout.strip()
        self.assertNotEqual(first, second)
        self.assertTrue((self.etc / "revisions" / first / "server.env").exists())
        after = self.env()
        for name in ("WOODPECKER_GITHUB_SECRET", "WOODPECKER_AGENT_SECRET"):
            self.assertEqual(before[name], after[name])
        self.assertEqual(after["WOODPECKER_HOST"], "https://new.example.com/app/woodpecker")
        self.assertEqual(after["WOODPECKER_EXPERT_WEBHOOK_HOST"], "https://hooks.example.com/app/woodpecker")
        self.configure(dict(self.config, github_secret="replacement-literal-secret"))
        self.assertEqual(self.env()["WOODPECKER_GITHUB_SECRET"], "replacement-literal-secret")
        self.assertEqual(self.env()["WOODPECKER_AGENT_SECRET"], before["WOODPECKER_AGENT_SECRET"])

    def test_invalid_input_and_write_failure_keep_previous_configuration(self):
        self.configure()
        previous = os.readlink(self.etc / "current")
        original = (self.etc / "current/server.env").read_bytes()
        self.configure(dict(self.config, page_origin="https://bad.example.com/app/woodpecker"), expected=1)
        with mock.patch.object(runtime.os, "rename", side_effect=OSError("synthetic write failure")):
            with self.assertRaises(OSError):
                runtime.configure(self.config, self.etc, self.var, self.target)
        self.assertEqual(os.readlink(self.etc / "current"), previous)
        self.assertEqual((self.etc / "current/server.env").read_bytes(), original)
        self.assertFalse(list((self.etc / "revisions").glob(".pending-*")))
        with mock.patch.object(runtime.os, "replace", side_effect=OSError("synthetic switch failure")):
            with self.assertRaises(OSError):
                runtime.configure(self.config, self.etc, self.var, self.target)
        self.assertEqual(os.readlink(self.etc / "current"), previous)
        self.assertFalse(list(self.etc.glob(".current-*")))

    def test_urls_are_strict_and_error_output_does_not_echo_values(self):
        bad = [
            "https://user:synthetic-password@example.com", "https://user@example.com",
            "https://nas.example.com/app/woodpecker", "https://nas.example.com//",
            "https://nas.example.com/?access_token=synthetic-token", "https://nas.example.com?",
            "https://nas.example.com#", "https://nas.example.com/#fragment",
            "https://nas.example.com\n", "https://nas.example.com\x00", "https://nas.example.com\t",
            "https://nas.example.com\u0085", "https://nas.example.com\\evil",
            "https://example.com:0", "https://example.com:65536", "https://example.com:",
            "https://[::1]evil", "https://evil;injection.example", "https://$hostname",
            "https://bad_name.example", "https://127.0.0.1/%2fapp", "//nas.example.com", "ftp://example.com",
            "http://example.com", "http://localhost", "http://127.0.0.1", "http://[::1]",
        ]
        for field in ("page_origin", "webhook_origin"):
            for value in bad:
                with self.subTest(field=field, value=repr(value)):
                    result = self.cli("validate", "--input", self.input,
                                      data=dict(self.config, **{field: value}), expected=1)
                    self.assertNotIn(value, result.stdout + result.stderr)
        for origin in ("http://localhost:8000", "http://127.0.0.1:8000", "http://[::1]:8000"):
            self.cli("validate", "--input", self.input, "--allow-http", data=dict(self.config, page_origin=origin))
        self.cli("validate", "--input", self.input, "--allow-http",
                 data=dict(self.config, page_origin="http://example.com"), expected=1)

    def test_control_characters_and_unknown_shared_secret_are_rejected(self):
        for field in ("github_secret", "github_client", "github_admin"):
            for control in ("\n", "\r", "\x00", "\x1b", "\u2028"):
                with self.subTest(field=field, control=repr(control)):
                    self.cli("validate", "--input", self.input,
                             data=dict(self.config, **{field: "synthetic" + control + "value"}), expected=1)
        self.configure(dict(self.config, shared_secret="user-must-not-supply-this"), expected=1)
        self.input.write_text('{"github_secret": "synthetic-secret", INVALID}', encoding="utf-8")
        result = self.cli("validate", "--input", self.input, expected=1)
        self.assertNotIn("synthetic-secret", result.stderr)

    def test_origin_normalization_and_loopback_development(self):
        self.configure(dict(self.config, page_origin="https://NAS.Example.com:8443/"))
        self.assertEqual(self.env()["WOODPECKER_HOST"], "https://nas.example.com:8443/app/woodpecker")
        self.configure(dict(self.config, page_origin="http://[::1]:8080"), "--allow-http")
        nginx = (self.etc / "current/nginx.conf").read_text()
        self.assertIn("proxy_set_header Host [::1]:8080;", nginx)
        self.assertIn("proxy_set_header X-Forwarded-Proto http;", nginx)

    def test_proxy_routes_preserve_prefix_queries_and_trusted_headers(self):
        self.configure(dict(self.config, webhook_origin="https://hooks.example.com"))
        nginx = (self.etc / "current/nginx.conf").read_text()
        self.assertIn("events {", nginx)
        self.assertIn("http {", nginx)
        self.assertIn("resolver 127.0.0.11", nginx)
        self.assertIn("zone woodpecker_backend", nginx)
        self.assertIn("server server:8000 resolve;", nginx)
        page, hook = nginx.split("listen unix:/fn-target/run/woodpecker.sock;", 1)[1].split("listen 8081;", 1)
        self.assertIn("location = /app/woodpecker", page)
        self.assertIn("^(GET|HEAD)$", page)
        self.assertIn("return 308 /app/woodpecker/$is_args$args;", page)
        self.assertIn("location ^~ /app/woodpecker/", page)
        self.assertIn("location = /app/woodpecker/api/hook", hook)
        self.assertIn("$request_method != POST", hook)
        self.assertIn("proxy_next_upstream off;", hook)
        self.assertIn("location / { return 404; }", hook)
        self.assertNotIn("healthz", hook)
        self.assertNotIn("return 308", hook)
        self.assertNotIn("proxy_pass http://woodpecker_backend/", nginx)
        self.assertNotIn("rewrite ", nginx)
        for meaningful in ("proxy_set_header Host nas.example.com;", "X-Forwarded-Proto https;",
                           "proxy_cookie_path ~^/$ /app/woodpecker/;", "proxy_http_version 1.1;",
                           "proxy_set_header Upgrade $http_upgrade;", "proxy_buffering off;",
                           "access_log off;", "error_log /dev/null;"):
            self.assertIn(meaningful, nginx)

    def test_rollback_and_metadata_reject_traversal_incomplete_and_symlink_revisions(self):
        first = self.configure().stdout.strip()
        second = self.configure(dict(self.config, github_secret="changed-synthetic-secret")).stdout.strip()
        self.assertEqual(self.cli("rollback", "--etc", self.etc, "--revision", first).stdout.strip(), first)
        self.assertEqual(self.env()["WOODPECKER_GITHUB_SECRET"], self.config["github_secret"])
        self.assertTrue((self.etc / "revisions" / second).is_dir())
        previous = os.readlink(self.etc / "current")
        for invalid in ("../current", "../../outside", str(self.base), "", first + "/../" + second,
                        first + "\n", "20000101T000000Z-0000000000000000"):
            with self.subTest(revision=repr(invalid)):
                self.cli("rollback", "--etc", self.etc, "--revision", invalid, expected=1)
                self.assertEqual(os.readlink(self.etc / "current"), previous)
        (self.etc / "revisions" / second / "nginx.conf").unlink()
        self.cli("rollback", "--etc", self.etc, "--revision", second, expected=1)
        self.assertEqual(os.readlink(self.etc / "current"), previous)
        link_revision = "20000101T000000Z-1111111111111111"
        (self.etc / "revisions" / link_revision).symlink_to(self.etc / "revisions" / first, target_is_directory=True)
        self.cli("rollback", "--etc", self.etc, "--revision", link_revision, expected=1)
        self.cli("metadata", "--etc", self.etc, "--revision", "../current", expected=1)

    def test_unexpected_current_and_internal_symlinks_are_not_followed(self):
        external = self.base / "external"
        external.mkdir()
        (self.etc / "revisions").symlink_to(external, target_is_directory=True)
        self.configure(expected=1)
        self.assertEqual(list(external.iterdir()), [])
        (self.etc / "revisions").unlink()
        (self.etc / "current").write_text("existing unrelated file")
        self.configure(expected=1)
        self.assertEqual((self.etc / "current").read_text(), "existing unrelated file")

    @contextmanager
    def health_server(self, response):
        requests = []

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                lines = []
                while True:
                    line = self.rfile.readline()
                    lines.append(line)
                    if line in (b"\r\n", b""):
                        break
                requests.append(b"".join(lines))
                self.wfile.write(response)

        path = self.base / "health.sock"
        with socketserver.UnixStreamServer(str(path), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                yield path, requests
            finally:
                server.shutdown()
                thread.join(timeout=2)
        path.unlink()

    def test_status_requires_exact_server_health_contract(self):
        with self.health_server(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n") as (path, requests):
            self.cli("status", "--socket", path)
            self.assertTrue(requests[0].startswith(b"GET /app/woodpecker/api/healthz HTTP/1.1\r\n"))
        bad = [
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK",
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 13\r\n\r\n<h1>Home</h1>",
            b"HTTP/1.1 302 Found\r\nLocation: /login\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 500 Error\r\nContent-Length: 0\r\n\r\n",
            b"HTTP/1.1 204 No Content\r\nLocation: /login\r\n\r\n",
            b"HTTP/1.1 204 No Content\r\nContent-Length: 9\r\n\r\n",
            b"HTTP/1.1 204 No Content\r\nContent-Type: text/html\r\n\r\n",
            b"not HTTP\r\n\r\n",
        ]
        for response in bad:
            with self.subTest(response=response):
                with self.health_server(response) as (path, _):
                    self.cli("status", "--socket", path, expected=3)
        self.cli("status", "--socket", self.base / "missing.sock", expected=3)
        with self.health_server(b"HTTP/1.1 204 No Content\r\n\r\n") as (path, _):
            self.cli("status", "--socket", path)


if __name__ == "__main__":
    unittest.main()
