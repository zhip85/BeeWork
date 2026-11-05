"""A stand-in for the RAGFlow and Dify knowledge APIs, for development and tests without a real engine (T-030).

    python scripts/fake_ragflow.py            # listens on 0.0.0.0:9380, key "fake-ragflow-key"

RAGFlow lives under ``/api/v1``, Dify under ``/v1`` (use ``http://host:9380/v1`` as DIFY_BASE_URL); both accept the
same Bearer key. Only the dataset endpoints BeeWork's clients use are implemented (Dify: create, list with
``keyword``, delete). RAGFlow uses its documented envelope
(``{"code": 0, "data": ...}``, errors as ``{"code": != 0, "message": ...}`` even on HTTP 200), Bearer auth,
duplicate-name rejection and ``name`` / ``id`` filters. State is in memory. It is NOT RAGFlow: nothing here has been
checked against a real instance, so passing against it proves BeeWork's side of the contract as written from the
documentation, not that RAGFlow behaves this way.

Dify answers with plain HTTP statuses: 401 ``unauthorized``, 400 ``invalid_param`` (names are limited to 40
characters), 409 ``dataset_name_duplicate``, 404 ``not_found``, 204 on delete.

Test controls (not part of either engine): ``POST /__fake/fail?count=N`` makes the next N requests answer with an
error; ``GET /__fake/state`` returns the RAGFlow datasets, ``GET /__fake/dify-state`` the Dify ones.
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
dify_datasets: dict[str, dict] = {}
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

        def _dify(self, method: str, path: str, query: dict) -> None:
            global fail_next
            if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                return self._send({"code": "unauthorized", "message": "Access token is invalid", "status": 401}, 401)
            with lock:
                if fail_next > 0:
                    fail_next -= 1
                    return self._send({"code": "internal_error", "message": "Injected failure", "status": 500}, 500)
            parts = path.strip("/").split("/")  # ["v1", "datasets", id?]
            if parts[:2] != ["v1", "datasets"] or len(parts) > 3:
                return self._send({"code": "not_found", "message": "Not found", "status": 404}, 404)
            body = self._body() if method == "POST" else {}
            with lock:
                if len(parts) == 2 and method == "GET":
                    keyword = query.get("keyword")
                    found = [d for d in dify_datasets.values() if keyword is None or keyword in d["name"]]
                    page, limit = int(query.get("page", "1")), int(query.get("limit", "20"))
                    return self._send({
                        "data": found[(page - 1) * limit : page * limit], "has_more": len(found) > page * limit,
                        "limit": limit, "total": len(found), "page": page,
                    })
                if len(parts) == 2 and method == "POST":
                    name = str(body.get("name") or "").strip()
                    if not name or len(name) > 40:
                        return self._send({"code": "invalid_param", "message": "Name must be 1-40 characters.", "status": 400}, 400)
                    if any(d["name"] == name for d in dify_datasets.values()):
                        return self._send({"code": "dataset_name_duplicate", "message": "The dataset name already exists. Please modify your dataset name.", "status": 409}, 409)
                    dataset = {
                        "id": str(uuid.uuid4()), "name": name, "indexing_technique": body.get("indexing_technique"),
                        "permission": body.get("permission", "only_me"),
                        "embedding_model": body.get("embedding_model"),
                        "embedding_model_provider": body.get("embedding_model_provider"), "document_count": 0,
                    }
                    dify_datasets[dataset["id"]] = dataset
                    return self._send(dataset)
                if len(parts) == 3 and method == "DELETE":
                    if parts[2] not in dify_datasets:
                        return self._send({"code": "not_found", "message": "Dataset not found.", "status": 404}, 404)
                    del dify_datasets[parts[2]]
                    self.send_response(204)
                    self.end_headers()
                    return
            self._send({"code": "method_not_allowed", "message": "Method not allowed", "status": 405}, 405)

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
            if url.path == "/__fake/dify-state":
                with lock:
                    return self._send({"code": 0, "data": list(dify_datasets.values())})
            if url.path.startswith("/v1/"):
                return self._dify(method, url.path, query)
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
