"""Serve an Airbyte control plane to agents through ELT-Bench's username/password interface.

ELT-Bench's prompts, its trigger_job.md documentation and check_job_status.py
configure Airbyte with a username and password (HTTP Basic). Airbyte 2.x rejects
Basic authentication on its API and accepts only Bearer tokens issued for a
client-credential pair. The Terraform provider 0.6.5 authenticates a
client-credential pair through a hook that writes a shared map without a lock,
so parallel resource operations crash the provider with "concurrent map writes";
with Basic authentication that hook is not used.

The gateway accepts one Basic username/password pair, replaces it with a Bearer
token obtained from the client-credential pair, and forwards the request to the
control plane unchanged otherwise.
"""

from __future__ import annotations

import base64
import hmac
import http.client
import json
import secrets
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOP_BY_HOP_HEADERS = frozenset(
    {
        "authorization",
        "connection",
        "content-length",
        "host",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)
TOKEN_PATH = "/api/public/v1/applications/token"
DEFAULT_USERNAME = "airbyte"


class GatewayError(RuntimeError):
    """The gateway could not start or could not obtain an upstream token."""


@dataclass(frozen=True)
class BasicCredential:
    username: str
    password: str


def load_basic_credential(path: Path, *, create: bool = False) -> BasicCredential:
    """Read the Basic pair agents use; write a new random pair first when asked and absent."""

    credential_path = Path(path)
    if not credential_path.exists():
        if not create:
            raise GatewayError(f"gateway credential does not exist: {credential_path}")
        credential_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"username": DEFAULT_USERNAME, "password": secrets.token_urlsafe(24)}
        with credential_path.open("x", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        credential_path.chmod(0o600)
    data = json.loads(credential_path.read_text(encoding="utf-8"))
    username = data.get("username")
    password = data.get("password")
    if not isinstance(username, str) or not username:
        raise GatewayError("gateway credential needs a non-empty username")
    if not isinstance(password, str) or not password:
        raise GatewayError("gateway credential needs a non-empty password")
    return BasicCredential(username=username, password=password)


def _split_upstream(upstream: str) -> tuple[str, int]:
    parts = urlsplit(upstream)
    if parts.scheme != "http" or not parts.hostname:
        raise GatewayError(f"upstream must be an http:// URL: {upstream!r}")
    return parts.hostname, parts.port or 80


class TokenSource:
    """Obtain and cache the Bearer token for one client-credential pair."""

    def __init__(
        self,
        upstream: str,
        client_id: str,
        client_secret: str,
        *,
        refresh_margin: float = 60.0,
        timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not client_id or not client_secret:
            raise GatewayError("upstream credential needs client_id and client_secret")
        self._host, self._port = _split_upstream(upstream)
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_margin = refresh_margin
        self._timeout = timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            now = self._clock()
            if force or self._token is None or now >= self._expires_at:
                self._token, lifetime = self._request_token()
                self._expires_at = now + max(lifetime - self._refresh_margin, 1.0)
            return self._token

    def _request_token(self) -> tuple[str, float]:
        body = json.dumps(
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "grant-type": "client_credentials",
            }
        ).encode("utf-8")
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.request(
                "POST",
                TOKEN_PATH,
                body=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            response = connection.getresponse()
            payload = response.read()
        except OSError as exc:
            raise GatewayError(f"token request failed: {exc}") from exc
        finally:
            connection.close()
        if response.status != 200:
            raise GatewayError(f"token request returned HTTP {response.status}")
        data = json.loads(payload)
        token = data.get("access_token")
        if not isinstance(token, str) or not token:
            raise GatewayError("token response has no access_token")
        lifetime = data.get("expires_in")
        return token, float(lifetime) if isinstance(lifetime, (int, float)) else 180.0


@dataclass(frozen=True)
class ForwardResult:
    status: int
    reason: str
    headers: tuple[tuple[str, str], ...]
    body: bytes


class AirbyteGateway:
    """Check the Basic pair and forward one request upstream with a Bearer token."""

    def __init__(
        self,
        upstream: str,
        tokens: TokenSource,
        credential: BasicCredential,
        *,
        timeout: float = 900.0,
    ) -> None:
        self._host, self._port = _split_upstream(upstream)
        self._tokens = tokens
        self._timeout = timeout
        pair = f"{credential.username}:{credential.password}".encode("utf-8")
        self._expected = "Basic " + base64.b64encode(pair).decode("ascii")

    def authorized(self, header: str | None) -> bool:
        if not header:
            return False
        return hmac.compare_digest(header.strip().encode("utf-8"), self._expected.encode("utf-8"))

    def forward(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> ForwardResult:
        outbound = {
            name: value
            for name, value in headers.items()
            if name.lower() not in HOP_BY_HOP_HEADERS
        }
        result = self._send(method, path, outbound, body, self._tokens.token())
        if result.status == 401:
            result = self._send(method, path, outbound, body, self._tokens.token(force=True))
        return result

    def _send(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        token: str,
    ) -> ForwardResult:
        outbound = dict(headers)
        outbound["Authorization"] = f"Bearer {token}"
        if body or method in {"POST", "PUT", "PATCH"}:
            outbound["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            connection.request(method, path, body=body or None, headers=outbound)
            response = connection.getresponse()
            payload = response.read()
            response_headers = tuple(
                (name, value)
                for name, value in response.getheaders()
                if name.lower() not in HOP_BY_HOP_HEADERS
            )
            return ForwardResult(response.status, response.reason, response_headers, payload)
        finally:
            connection.close()


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    if handler.headers.get("Transfer-Encoding", "").lower() == "chunked":
        chunks: list[bytes] = []
        while True:
            size_line = handler.rfile.readline().strip()
            size = int(size_line.split(b";")[0], 16)
            if size == 0:
                handler.rfile.readline()
                break
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline()
        return b"".join(chunks)
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length > 0 else b""


def make_handler(gateway: AirbyteGateway, *, log: Callable[[str], None] | None = None):
    """Return a request handler class bound to one gateway."""

    class GatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reply(self, status: int, headers, body: bytes) -> None:
            self.send_response(status)
            for name, value in headers:
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            self.close_connection = True

        def _json_error(self, status: int, message: str, extra=()) -> None:
            body = json.dumps({"message": message}).encode("utf-8")
            self._reply(status, (("Content-Type", "application/json"), *extra), body)

        def _handle(self) -> None:
            body = _read_request_body(self)
            if not gateway.authorized(self.headers.get("Authorization")):
                self._json_error(401, "Unauthorized", (("WWW-Authenticate", 'Basic realm="airbyte"'),))
                return
            try:
                result = gateway.forward(self.command, self.path, dict(self.headers.items()), body)
            except (OSError, GatewayError, http.client.HTTPException) as exc:
                self._json_error(502, f"airbyte gateway: {exc}")
                return
            self._reply(result.status, result.headers, result.body)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _handle

        def log_message(self, fmt: str, *args) -> None:
            if log is not None:
                log(f"{self.address_string()} {fmt % args}")

    return GatewayHandler


def serve(
    gateway: AirbyteGateway,
    host: str,
    port: int,
    *,
    log: Callable[[str], None] | None = None,
) -> ThreadingHTTPServer:
    """Bind the gateway; the caller runs serve_forever on the returned server."""

    server = ThreadingHTTPServer((host, port), make_handler(gateway, log=log))
    server.daemon_threads = True
    return server


def log_to_stderr(line: str) -> None:
    sys.stderr.write(line + "\n")
    sys.stderr.flush()
