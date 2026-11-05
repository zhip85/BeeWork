"""A stand-in for RAGFlow's HTTP API, for development and tests when no real RAGFlow is available (T-030).

    python scripts/fake_ragflow.py            # listens on 0.0.0.0:9380, key "fake-ragflow-key"

It implements ONLY the dataset endpoints BeeWork's client uses, with RAGFlow's documented envelope
(``{"code": 0, "data": ...}``, errors as ``{"code": != 0, "message": ...}`` even on HTTP 200), Bearer auth,
duplicate-name rejection and ``name`` / ``id`` filters. State is in memory. It is NOT RAGFlow: nothing here has been
checked against a real instance, so passing against it proves BeeWork's side of the contract as written from the
documentation, not that RAGFlow behaves this way.

Test controls (not part of RAGFlow): ``POST /__fake/fail?count=N`` makes the next N requests answer with an
error; ``GET /__fake/state`` returns the datasets.
"""

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY = os.environ.get("FAKE_RAGFLOW_KEY", "fake-ragflow-key")
PORT = int(os.environ.get("FAKE_RAGFLOW_PORT", "9380"))
CHUNK_METHODS = {"naive", "book", "qa", "manual", "paper", "laws", "presentation", "table", "one", "picture", "email"}

lock = threading.Lock()
datasets: dict[str, dict] = {}
fail_next = 0


def make_handler() -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # quiet
            return

        def _send(self, payload: dict, status: int = 200) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length))
            except ValueError:
                return {}

        def _error(self, message: str, code: int = 102) -> None:
            self._send({"code": code, "message": message})

        def _handle(self, method: str) -> None:
            global fail_next
            url = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(url.query).items()}
            if url.path == "/__fake/state":
                with lock:
                    return self._send({"code": 0, "data": list(datasets.values())})
            if url.path == "/__fake/fail":
                with lock:
                    fail_next = int(query.get("count", "1"))
                return self._send({"code": 0, "data": {"fail_next": fail_next}})
            if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                return self._send({"code": 109, "message": "Authentication error: API key is invalid!"})
            with lock:
                if fail_next > 0:
                    fail_next -= 1
                    return self._error("Injected failure", 500)
            if url.path != "/api/v1/datasets":
                return self._send({"code": 404, "message": "Not found"}, 404)
            body = self._body() if method in ("POST", "DELETE", "PUT") else {}
            with lock:
                if method == "GET":
                    found = [
                        dataset for dataset in datasets.values()
                        if query.get("name") in (None, dataset["name"]) and query.get("id") in (None, dataset["id"])
                    ]
                    if (query.get("name") or query.get("id")) and not found:
                        return self._error("You don't own the dataset.", 102)
                    page, size = int(query.get("page", "1")), int(query.get("page_size", "30"))
                    return self._send({"code": 0, "data": found[(page - 1) * size : page * size]})
                if method == "POST":
                    name = str(body.get("name") or "").strip()
                    if not name or len(name) > 128:
                        return self._error("Dataset name must be 1-128 characters.")
                    if any(dataset["name"] == name for dataset in datasets.values()):
                        return self._error(f"Dataset name '{name}' already exists")
                    chunk_method = body.get("chunk_method", "naive")
                    if chunk_method not in CHUNK_METHODS:
                        return self._error(f"'{chunk_method}' is not a valid chunk method")
                    dataset = {
                        "id": uuid.uuid4().hex, "name": name, "chunk_method": chunk_method,
                        "embedding_model": body.get("embedding_model") or "BAAI/bge-large-zh-v1.5@BAAI",
                        "parser_config": body.get("parser_config") or {}, "document_count": 0, "chunk_count": 0,
                    }
                    datasets[dataset["id"]] = dataset
                    return self._send({"code": 0, "data": dataset})
                if method == "DELETE":
                    ids = body.get("ids")
                    if ids is None:
                        return self._error("`ids` is required.")  # real RAGFlow deletes everything on null: refuse
                    missing = [item for item in ids if item not in datasets]
                    if missing:
                        return self._error(f"You don't own the dataset {missing[0]}")
                    for item in ids:
                        del datasets[item]
                    return self._send({"code": 0})
            self._send({"code": 405, "message": "Method not allowed"}, 405)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

        def do_PUT(self) -> None:
            self._handle("PUT")

    return Handler


if __name__ == "__main__":
    print(f"fake RAGFlow on :{PORT} (key {API_KEY!r})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), make_handler()).serve_forever()
