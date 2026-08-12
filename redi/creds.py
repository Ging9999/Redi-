"""Credentials, join URLs, and host classification (spec parts C3, C4, D).

Split by secrecy, per the spec:

* **Server token** lives next to the SQLite DB (``/data`` in Docker). Generated
  on first run if none is configured, so "drop it in and run" is true.
* **Client credentials** live at ``~/.config/redi/credentials`` (mode 0600),
  keyed by server host — never committed. ``.redi.toml`` (committed) carries the
  non-secret URL/mode.

The join URL is ``redi://<token>@<host>:<port>`` — one copy-pasteable string.
"""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import stat


# --- join URL ----------------------------------------------------------------


def parse_join_url(s: str) -> dict:
    """Parse ``redi://token@host:port`` (also accepts http(s):// and bare host).

    Returns ``{scheme, token, host, port, base_url}``. ``base_url`` is the HTTP
    URL the client actually calls (``http://host:port``; ``https`` if the input
    used it). Raises ``ValueError`` on something unparseable.
    """
    s = s.strip()
    if "://" not in s:
        s = "redi://" + s
    scheme, rest = s.split("://", 1)
    scheme = scheme.lower()
    token = None
    if "@" in rest:
        token, rest = rest.rsplit("@", 1)
        token = token or None
    hostport = rest.rstrip("/")
    if not hostport:
        raise ValueError("no host in join URL")
    if ":" in hostport:
        host, port_s = hostport.rsplit(":", 1)
        try:
            port = int(port_s)
        except ValueError:
            raise ValueError(f"bad port in join URL: {port_s!r}")
    else:
        host = hostport
        port = 8787
    if not host:
        raise ValueError("no host in join URL")
    http_scheme = "https" if scheme == "https" else "http"
    return {
        "scheme": scheme,
        "token": token,
        "host": host,
        "port": port,
        "base_url": f"{http_scheme}://{host}:{port}",
    }


def build_join_url(token: str, host: str, port: int) -> str:
    at = f"{token}@" if token else ""
    return f"redi://{at}{host}:{port}"


# --- host classification (for the plain-http warning, C4) --------------------


def is_loopback(host: str) -> bool:
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_private_host(host: str) -> bool:
    """True for addresses that don't need TLS: loopback, RFC1918/ULA, and the
    Tailscale CGNAT range (100.64/10), plus obvious internal hostnames."""
    if is_loopback(host):
        return True
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_link_local:
            return True
        # Tailscale / CGNAT 100.64.0.0/10
        if ip in ipaddress.ip_network("100.64.0.0/10"):
            return True
        return False
    except ValueError:
        # A hostname, not an IP. Treat internal-looking names as private.
        h = host.lower()
        if "." not in h:
            return True  # bare hostname, e.g. "redi" or a container name
        return h.endswith((".internal", ".local", ".lan", ".ts.net", ".tailnet"))


def warn_if_insecure(base_url: str, host: str) -> str:
    """Return a warning string if this is a plain-http connection to a public
    host, else ''. The client surfaces this on join/doctor (spec C4)."""
    if base_url.startswith("https://"):
        return ""
    if is_private_host(host):
        return ""
    return (
        f"WARNING: connecting to {host} over plain HTTP — your token and claims "
        f"are unencrypted on the wire. Use Tailscale or a TLS reverse proxy for "
        f"a public host (see docs/HOSTING.md)."
    )


# --- server token (next to the DB) -------------------------------------------


def server_data_dir() -> str:
    explicit = os.environ.get("COORD_DATA_DIR")
    if explicit:
        return explicit
    db = os.environ.get("COORD_DB", "coordinator.db")
    if db == ":memory:":
        return "."
    return os.path.dirname(os.path.abspath(db)) or "."


def _server_token_path() -> str:
    return os.path.join(server_data_dir(), "token")


def load_or_create_server_token() -> tuple[str, bool]:
    """Return ``(token, created)``. Uses ``COORD_TOKEN`` if set (created=False);
    otherwise reads/creates a persisted random token next to the DB."""
    configured = os.environ.get("COORD_TOKEN")
    if configured:
        return configured, False
    path = _server_token_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tok = fh.read().strip()
            if tok:
                return tok, False
    except OSError:
        pass
    token = secrets.token_urlsafe(12)
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(token)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass  # in-memory / read-only: token still works for this run
    return token, True


def external_host() -> str:
    """Best-effort resolution of the host to advertise in the join string."""
    for var in ("COORD_EXTERNAL_HOST", "COORD_HOST"):
        v = os.environ.get(var)
        if v and v not in ("0.0.0.0", "::", ""):
            return v
    import socket
    try:
        return socket.gethostname() or "localhost"
    except OSError:
        return "localhost"


# --- client credentials (~/.config/redi/credentials, 0600) -------------------


def credentials_path() -> str:
    base = os.environ.get("COORD_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "redi"
    )
    return os.path.join(base, "credentials")


def _load_credentials() -> dict:
    try:
        with open(credentials_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def get_credential(host: str) -> dict | None:
    """Return ``{token, base_url}`` saved for a host, or None."""
    return _load_credentials().get(host)


def save_credential(host: str, token: str, base_url: str) -> str:
    path = credentials_path()
    data = _load_credentials()
    data[host] = {"token": token, "base_url": base_url}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return path


def remove_credential(host: str) -> bool:
    data = _load_credentials()
    if host not in data:
        return False
    del data[host]
    path = credentials_path()
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return True
