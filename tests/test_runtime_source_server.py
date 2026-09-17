from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from elt_taskgen.runtime.source_server import (
    SourceFixtureError,
    load_routes,
    make_handler,
    read_rest_records,
)


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    rendered = tmp_path / "rendered"
    rest = rendered / "rest" / "events"
    files = rendered / "files"
    rest.mkdir(parents=True)
    files.mkdir(parents=True)
    (rest / "index.json").write_text(
        json.dumps(
            {
                "table": "events",
                "row_count": 3,
                "page_size": 2,
                "pages": ["page_0001.json", "page_0002.json"],
            }
        ),
        encoding="utf-8",
    )
    (rest / "page_0001.json").write_text(
        json.dumps({"data": [{"id": 1}, {"id": 2}]}), encoding="utf-8"
    )
    (rest / "page_0002.json").write_text(
        json.dumps({"data": [{"id": 3}]}), encoding="utf-8"
    )
    (files / "users.csv").write_text("id,name\n1,Ada\n", encoding="utf-8")
    manifest = tmp_path / "sources_serving.json"
    manifest.write_text(
        json.dumps(
            {
                "tables": {
                    "events": {
                        "backend": "rest",
                        "route": "/demo/events",
                        "rendered_dir": "rest/events/",
                    },
                    "users": {
                        "backend": "files",
                        "url": "http://elt-files:8080/demo/users.csv",
                        "rendered_file": "files/users.csv",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return manifest, rendered


class RuntimeSourceServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_rest_pages_are_concatenated_in_index_order(self) -> None:
        manifest, rendered = _fixture(self.root)
        routes = load_routes(manifest, rendered)
        self.assertEqual(
            read_rest_records(routes["/demo/events"].artifact),
            [{"id": 1}, {"id": 2}, {"id": 3}],
        )

    def test_server_slices_bare_rest_array_and_serves_exact_csv(self) -> None:
        manifest, rendered = _fixture(self.root)
        server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(load_routes(manifest, rendered))
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(
                f"{base}/demo/events?offset=1&limit=1", timeout=2
            ) as response:
                self.assertEqual(response.headers["Content-Type"], "application/json")
                self.assertEqual(json.loads(response.read()), [{"id": 2}])
            with urllib.request.urlopen(f"{base}/demo/events", timeout=2) as response:
                self.assertEqual(
                    json.loads(response.read()), [{"id": 1}, {"id": 2}, {"id": 3}]
                )
            with urllib.request.urlopen(f"{base}/demo/users.csv", timeout=2) as response:
                self.assertEqual(response.read(), b"id,name\n1,Ada\n")
            request = urllib.request.Request(f"{base}/demo/users.csv", method="HEAD")
            with urllib.request.urlopen(request, timeout=2) as response:
                self.assertEqual(
                    int(response.headers["Content-Length"]), len(b"id,name\n1,Ada\n")
                )
                self.assertEqual(response.read(), b"")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_manifest_rejects_missing_and_escaping_artifacts(self) -> None:
        manifest, rendered = _fixture(self.root)
        raw = json.loads(manifest.read_text(encoding="utf-8"))
        raw["tables"]["users"]["rendered_file"] = "../secret.csv"
        manifest.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(SourceFixtureError, "escapes rendered root"):
            load_routes(manifest, rendered)

    def test_rest_index_row_count_is_enforced(self) -> None:
        manifest, rendered = _fixture(self.root)
        index = rendered / "rest" / "events" / "index.json"
        raw = json.loads(index.read_text(encoding="utf-8"))
        raw["row_count"] = 99
        index.write_text(json.dumps(raw), encoding="utf-8")
        routes = load_routes(manifest, rendered)
        with self.assertRaisesRegex(SourceFixtureError, "rendered pages contain 3"):
            read_rest_records(routes["/demo/events"].artifact)


if __name__ == "__main__":
    unittest.main()

