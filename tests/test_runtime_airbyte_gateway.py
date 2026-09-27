import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from elt_taskgen.runtime.airbyte_gateway import (
    AirbyteGateway,
    BasicCredential,
    GatewayError,
    TokenSource,
    load_basic_credential,
    serve,
)


class FakeAirbyte:
    def __init__(self) -> None:
        self.tokens_issued = 0
        self.valid_tokens: set[str] = set()
        self.requests: list[dict] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _reply(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                if self.path == "/api/public/v1/applications/token":
                    data = json.loads(body)
                    if (data.get("client_id"), data.get("client_secret")) != ("cid", "csecret"):
                        self._reply(401, {"message": "Unauthorized"})
                        return
                    owner.tokens_issued += 1
                    token = f"token-{owner.tokens_issued}"
                    owner.valid_tokens.add(token)
                    self._reply(200, {"access_token": token, "token_type": "Bearer", "expires_in": 900})
                    return
                auth = self.headers.get("Authorization", "")
                owner.requests.append(
                    {"method": self.command, "path": self.path, "auth": auth, "body": body}
                )
                if not auth.startswith("Bearer ") or auth[7:] not in owner.valid_tokens:
                    self._reply(401, {"message": "Unauthorized"})
                    return
                self._reply(200, {"method": self.command, "path": self.path, "body": body.decode("utf-8")})

            do_GET = do_POST = do_PATCH = do_DELETE = _handle

            def log_message(self, fmt, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class AirbyteGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.upstream = FakeAirbyte()
        self.tokens = TokenSource(self.upstream.url, "cid", "csecret")
        gateway = AirbyteGateway(
            self.upstream.url, self.tokens, BasicCredential("airbyte", "pw")
        )
        self.server = serve(gateway, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.upstream.close()

    def _call(self, method: str, path: str, *, user=("airbyte", "pw"), body: bytes | None = None):
        headers = {"Content-Type": "application/json"}
        if user is not None:
            headers["Authorization"] = "Basic " + base64.b64encode(":".join(user).encode()).decode()
        request = urllib.request.Request(self.url + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_basic_pair_is_forwarded_with_a_bearer_token(self) -> None:
        status, payload = self._call("POST", "/api/public/v1/jobs?x=1", body=b'{"jobType":"sync"}')
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"method": "POST", "path": "/api/public/v1/jobs?x=1", "body": '{"jobType":"sync"}'})
        self.assertEqual(self.upstream.requests[-1]["auth"], "Bearer token-1")

    def test_wrong_basic_pair_is_refused_without_reaching_airbyte(self) -> None:
        self.assertEqual(self._call("GET", "/api/public/v1/workspaces", user=("airbyte", "no"))[0], 401)
        self.assertEqual(self.upstream.requests, [])

    def test_request_without_credentials_reaches_airbyte_unchanged(self) -> None:
        self.assertEqual(self._call("GET", "/api/public/v1/workspaces", user=None)[0], 401)
        self.assertEqual(self.upstream.requests[-1]["auth"], "")

    def test_token_is_reused_and_replaced_after_airbyte_refuses_it(self) -> None:
        self._call("GET", "/api/public/v1/workspaces")
        self._call("GET", "/api/public/v1/workspaces")
        self.assertEqual(self.upstream.tokens_issued, 1)
        self.upstream.valid_tokens.clear()
        status, _ = self._call("GET", "/api/public/v1/workspaces")
        self.assertEqual(status, 200)
        self.assertEqual(self.upstream.tokens_issued, 2)

    def test_wrong_client_pair_fails_before_serving(self) -> None:
        with self.assertRaises(GatewayError):
            TokenSource(self.upstream.url, "cid", "wrong").token()


class BasicCredentialFileTests(unittest.TestCase):
    def test_created_once_then_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gateway.json"
            with self.assertRaises(GatewayError):
                load_basic_credential(path)
            first = load_basic_credential(path, create=True)
            second = load_basic_credential(path, create=True)
            self.assertEqual(first, second)
            self.assertEqual(first.username, "airbyte")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
