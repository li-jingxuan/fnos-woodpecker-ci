#!/usr/bin/env python3
"""Render private, atomic Woodpecker configuration; never invoke a shell or Docker.

Compose must read server.env and agent.env with env_file format: raw. configure
and rollback only switch files: the application lifecycle must recreate containers.
validate checks input syntax; configure additionally requires a secret on first use.
status checks the gateway and Server HTTP health, not Agent/gRPC readiness.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import stat
import sys
import tempfile
import unicodedata
from urllib.parse import urlsplit


PREFIX = "/app/woodpecker"
VERSION = "v3.20.1"
REVISION_RE = r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{16}"
FIELDS = {"page_origin", "webhook_origin", "github_admin", "github_client", "github_secret"}


class ConfigError(ValueError):
    """An error safe to report without including input values or credentials."""


def _text(value, field, *, empty=False):
    if not isinstance(value, str) or len(value) > 16384:
        raise ConfigError(field + " must be a single-line string")
    if any(unicodedata.category(c)[0] == "C" or c in "\u2028\u2029" for c in value):
        raise ConfigError(field + " contains forbidden control characters")
    if not empty and not value.strip():
        raise ConfigError(field + " is required")
    return value


def _origin(value, field, allow_http):
    value = _text(value, field)
    if any(c.isspace() for c in value) or any(c in value for c in "\\@?#%"):
        raise ConfigError(field + " must be an origin without credentials, query or fragment")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if not host or parsed.path not in ("", "/") or parsed.scheme not in ("https", "http"):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
            host = host.encode("idna").decode("ascii").lower()
            if len(host) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ):
                raise ValueError
        else:
            host = address.compressed
        loopback = host == "localhost" or (address is not None and address.is_loopback)
        if parsed.scheme == "http" and not (allow_http and loopback):
            raise ValueError
        authority = "[" + host + "]" if address is not None and address.version == 6 else host
        if port is not None:
            authority += ":" + str(port)
        # Reject parser-tolerated authority syntax, e.g. an empty port or excess brackets.
        if parsed.netloc.endswith(":") or (address is None and ":" in host):
            raise ValueError
        if address is not None and address.version == 6:
            if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parsed.netloc):
                raise ValueError
        elif not re.fullmatch(r"[^:]+(?::[0-9]+)?", parsed.netloc):
            raise ValueError
        return parsed.scheme + "://" + authority
    except (ValueError, UnicodeError):
        raise ConfigError(field + " must be HTTPS origin (HTTP requires --allow-http and loopback)") from None


def validate_config(data, *, allow_http=False):
    """Return normalized known fields. A blank OAuth secret means retain on update."""
    if not isinstance(data, dict) or set(data) - FIELDS:
        raise ConfigError("configuration contains unsupported fields")
    page = _origin(data.get("page_origin"), "page_origin", allow_http)
    webhook = _text(data.get("webhook_origin", ""), "webhook_origin", empty=True)
    if webhook:
        webhook = _origin(webhook, "webhook_origin", allow_http)
    admin = _text(data.get("github_admin"), "github_admin")
    if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", admin) or "--" in admin:
        raise ConfigError("github_admin must be one GitHub login")
    client = _text(data.get("github_client"), "github_client")
    secret = _text(data.get("github_secret", ""), "github_secret", empty=True)
    if secret and not secret.strip():
        raise ConfigError("github_secret must not contain only whitespace")
    return dict(page_origin=page, webhook_origin=webhook, github_admin=admin,
                github_client=client, github_secret=secret)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (ValueError, UnicodeError):
        raise ConfigError("invalid configuration JSON") from None


def _path(value):
    path = Path(_text(os.fspath(value), "directory"))
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise ConfigError("directories must be absolute application paths without traversal")
    return path


def _private_dir(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ConfigError("configuration directories must be private real directories (0700)")


@contextmanager
def _locked(etc):
    # Resolve the caller's platform alias once; refuse symlinks inside this private root.
    etc = _path(etc).resolve()
    _private_dir(etc)
    fd = os.open(etc / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ConfigError("invalid configuration lock")
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield etc
    finally:
        os.close(fd)


def _revision_path(etc, revision):
    if not isinstance(revision, str) or not re.fullmatch(REVISION_RE, revision):
        raise ConfigError("invalid revision identifier")
    root = etc / "revisions"
    if not stat.S_ISDIR(root.lstat().st_mode):
        raise ConfigError("invalid revisions directory")
    path = root / revision
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise ConfigError("invalid revision directory")
    return path


def _current_revision(etc):
    current = etc / "current"
    if not os.path.lexists(current):
        return None
    if not current.is_symlink():
        raise ConfigError("current must be a revision symlink")
    target = os.readlink(current)
    match = re.fullmatch("revisions/(" + REVISION_RE + ")", target)
    if match is None:
        raise ConfigError("current points outside the revision store")
    return match.group(1)


def _regular_file(path):
    if not stat.S_ISREG(path.lstat().st_mode):
        raise ConfigError("revision files must be regular files")
    return path


def _read_env(path):
    result = {}
    for line in _regular_file(path).read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if not sep or key in result or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ConfigError("invalid stored environment file")
        result[key] = value
    return result


def read_metadata(etc, revision=None):
    """Read non-secret revision metadata; reject traversal and unexpected fields."""
    etc = _path(etc).resolve()
    if revision is None:
        revision = _current_revision(etc)
    path = _revision_path(etc, revision)
    data = _read_json(_regular_file(path / "metadata.json"))
    allowed = (FIELDS - {"github_secret"}) | {"revision", "prefix", "server_image", "agent_image",
                                                 "var_dir", "target_dir"}
    if not isinstance(data, dict) or set(data) - allowed or data.get("revision") != revision:
        raise ConfigError("invalid revision metadata")
    return data


def _nginx(config):
    page = urlsplit(config["page_origin"])
    # Only validated authority and scheme enter this template; credentials never do.
    return """user nginx;
worker_processes 1;
pid /tmp/nginx.pid;
# Nginx error lines can contain OAuth codes/hook tokens in request URIs.
error_log /dev/null;
events { worker_connections 512; }
http {
    access_log off;
    server_tokens off;
    absolute_redirect off;
    resolver 127.0.0.11 valid=10s ipv6=off;
    resolver_timeout 5s;
    upstream woodpecker_backend {
        zone woodpecker_backend 64k;
        server server:8000 resolve;
    }
    map $http_upgrade $connection_upgrade {
        default upgrade;
        '' close;
    }
    limit_req_zone $binary_remote_addr zone=webhooks:1m rate=10r/s;
    client_max_body_size 5m;
    client_header_timeout 10s;
    client_body_timeout 30s;
    send_timeout 30s;
    proxy_http_version 1.1;
    proxy_set_header Host __HOST__;
    proxy_set_header X-Forwarded-Host __HOST__;
    proxy_set_header X-Forwarded-Proto __SCHEME__;
    proxy_set_header X-Forwarded-For $remote_addr;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_buffering off;
    proxy_request_buffering off;
    proxy_connect_timeout 5s;
    proxy_cookie_path ~^/$ /app/woodpecker/;
    server {
        listen unix:/fn-target/run/woodpecker.sock;
        server_name _;
        location = /app/woodpecker {
            if ($request_method !~ ^(GET|HEAD)$) { return 405; }
            return 308 /app/woodpecker/$is_args$args;
        }
        location ^~ /app/woodpecker/ {
            proxy_read_timeout 3600s;
            proxy_pass http://woodpecker_backend;
        }
        location / { return 404; }
    }
    server {
        listen 8081;
        server_name _;
        location = /app/woodpecker/api/hook {
            if ($request_method != POST) { return 404; }
            if ($request_uri !~ "^/app/woodpecker/api/hook([?]|$)") { return 404; }
            limit_req zone=webhooks burst=20 nodelay;
            limit_req_status 429;
            proxy_read_timeout 30s;
            proxy_next_upstream off;
            proxy_pass http://woodpecker_backend;
        }
        location / { return 404; }
    }
}
""".replace("__HOST__", page.netloc).replace("__SCHEME__", page.scheme)


def _environments(config, shared):
    server = {
        "WOODPECKER_HOST": config["page_origin"] + PREFIX,
        "WOODPECKER_EXPERT_WEBHOOK_HOST": (config["webhook_origin"] or config["page_origin"]) + PREFIX,
        "WOODPECKER_SERVER_ADDR": ":8000",
        "WOODPECKER_GRPC_ADDR": ":9000",
        "WOODPECKER_GITHUB": "true",
        "WOODPECKER_GITHUB_CLIENT": config["github_client"],
        "WOODPECKER_GITHUB_SECRET": config["github_secret"],
        "WOODPECKER_ADMIN": config["github_admin"],
        "WOODPECKER_REPO_OWNERS": config["github_admin"],
        "WOODPECKER_OPEN": "false",
        "WOODPECKER_DEFAULT_ALLOW_PULL_REQUESTS": "false",
        "WOODPECKER_DISABLE_USER_AGENT_REGISTRATION": "true",
        "WOODPECKER_AGENT_SECRET": shared,
        "WOODPECKER_DATABASE_DRIVER": "sqlite3",
        "WOODPECKER_DATABASE_DATASOURCE": "/var/lib/woodpecker/woodpecker.sqlite",
        "WOODPECKER_FORCE_IGNORE_SERVICE_FAILURE": "false",
        "WOODPECKER_LOG_LEVEL": "info",
    }
    agent = {
        "WOODPECKER_SERVER": "server:9000",
        "WOODPECKER_AGENT_SECRET": shared,
        "WOODPECKER_BACKEND": "docker",
        "WOODPECKER_MAX_WORKFLOWS": "1",
        "WOODPECKER_BACKEND_DOCKER_LIMIT_MEM": "1610612736",
        "WOODPECKER_BACKEND_DOCKER_LIMIT_MEM_SWAP": "1610612736",
        "WOODPECKER_BACKEND_DOCKER_LIMIT_CPU_QUOTA": "200000",
        "WOODPECKER_BACKEND_DOCKER_LIMIT_SHM_SIZE": "67108864",
        "WOODPECKER_LOG_LEVEL": "info",
    }
    return {name: "".join(key + "=" + value + "\n" for key, value in values.items())
            for name, values in (("server.env", server), ("agent.env", agent))}


def _write(path, content, mode=0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        os.fchmod(handle.fileno(), mode)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _switch(etc, revision):
    temporary = etc / (".current-" + secrets.token_hex(8))
    try:
        temporary.symlink_to("revisions/" + revision)
        os.replace(temporary, etc / "current")
    finally:
        temporary.unlink(missing_ok=True)


def configure(data, etc, var, target, *, allow_http=False):
    """Create an immutable revision and atomically select it; return its identifier."""
    config = validate_config(data, allow_http=allow_http)
    var, target = _path(var), _path(target)
    with _locked(etc) as etc:
        revisions = etc / "revisions"
        _private_dir(revisions)
        previous = _current_revision(etc)
        if previous:
            old = _revision_path(etc, previous)
            server = _read_env(old / "server.env")
            agent = _read_env(old / "agent.env")
            shared = _text(server.get("WOODPECKER_AGENT_SECRET"), "stored shared secret")
            if shared != agent.get("WOODPECKER_AGENT_SECRET"):
                raise ConfigError("stored Server and Agent secrets do not match")
            if not config["github_secret"]:
                config["github_secret"] = _text(server.get("WOODPECKER_GITHUB_SECRET"), "stored OAuth secret")
        else:
            if not config["github_secret"]:
                raise ConfigError("github_secret is required on first configuration")
            shared = secrets.token_hex(32)
        revision = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + secrets.token_hex(8)
        metadata = {key: value for key, value in config.items() if key != "github_secret"}
        metadata.update(revision=revision, prefix=PREFIX, var_dir=str(var), target_dir=str(target),
                        server_image="woodpeckerci/woodpecker-server:" + VERSION,
                        agent_image="woodpeckerci/woodpecker-agent:" + VERSION)
        stage = Path(tempfile.mkdtemp(prefix=".pending-", dir=revisions))
        try:
            for name, content in _environments(config, shared).items():
                _write(stage / name, content)
            _write(stage / "nginx.conf", _nginx(config), 0o644)
            _write(stage / "metadata.json", json.dumps(metadata, ensure_ascii=True, indent=2) + "\n")
            os.rename(stage, revisions / revision)
            _switch(etc, revision)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
        return revision


def rollback(etc, revision):
    """Select an existing complete revision; containers still need recreation."""
    with _locked(etc) as etc:
        path = _revision_path(etc, revision)
        read_metadata(etc, revision)
        server, agent = _read_env(path / "server.env"), _read_env(path / "agent.env")
        if not server.get("WOODPECKER_GITHUB_SECRET") or not server.get("WOODPECKER_AGENT_SECRET"):
            raise ConfigError("incomplete stored credentials")
        if server["WOODPECKER_AGENT_SECRET"] != agent.get("WOODPECKER_AGENT_SECRET"):
            raise ConfigError("stored Server and Agent secrets do not match")
        _regular_file(path / "nginx.conf")
        _current_revision(etc)  # Do not overwrite an unexpected current object.
        _switch(etc, revision)
        return revision


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.path = os.fspath(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def healthy(path, *, timeout=3):
    """Require v3.20.1 Health's 204/empty response, never follow a redirect.

    Upstream contract: server/api/z.go Health returns 204 after database Ping.
    This deliberately makes no claim about authenticated Agent connectivity.
    """
    connection = _UnixHTTPConnection(path, timeout)
    try:
        connection.request("GET", PREFIX + "/api/healthz", headers={"Connection": "close"})
        response = connection.getresponse()
        return (response.status == 204 and response.getheader("Location") is None
                and response.getheader("Content-Length") in (None, "0")
                and response.getheader("Transfer-Encoding") is None
                and "html" not in response.getheader("Content-Type", "").lower()
                and response.read(1) == b"")
    except (OSError, http.client.HTTPException, ValueError):
        return False
    finally:
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("configure", "validate"):
        command = commands.add_parser(name)
        command.add_argument("--input", required=True, help="private JSON input; never pass secrets as arguments")
        command.add_argument("--allow-http", action="store_true", help="development only: allow loopback HTTP")
        if name == "configure":
            for flag in ("etc", "var", "target"):
                command.add_argument("--" + flag, required=True)
    command = commands.add_parser("rollback")
    command.add_argument("--etc", required=True)
    command.add_argument("--revision", required=True)
    command = commands.add_parser("metadata")
    command.add_argument("--etc", required=True)
    command.add_argument("--revision")
    command.add_argument("--field", help="one non-secret top-level metadata field")
    command = commands.add_parser("status", help="Server HTTP health only; Agent readiness requires a separate check")
    command.add_argument("--socket", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "configure":
            print(configure(_read_json(args.input), args.etc, args.var, args.target, allow_http=args.allow_http))
        elif args.command == "validate":
            validate_config(_read_json(args.input), allow_http=args.allow_http)
        elif args.command == "rollback":
            print(rollback(args.etc, args.revision))
        elif args.command == "metadata":
            metadata = read_metadata(args.etc, args.revision)
            if args.field:
                if args.field not in metadata:
                    raise ConfigError("unknown metadata field")
                print(metadata[args.field])
            else:
                print(json.dumps(metadata, ensure_ascii=True))
        else:
            return 0 if healthy(args.socket) else 3
    except ConfigError as error:
        print("runtime: " + str(error), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("runtime: cannot read or write application configuration", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
