"""A stand-in for the RAGFlow and Dify knowledge APIs, for development and tests without a real engine (T-030).

    python scripts/fake_ragflow.py            # listens on 0.0.0.0:9380, key "fake-ragflow-key"

RAGFlow lives under ``/api/v1``, Dify under ``/v1`` (use ``http://host:9380/v1`` as DIFY_BASE_URL); both accept the
same Bearer key. Only the dataset endpoints BeeWork's clients use are implemented (datasets: create, list, delete;
documents: upload, status, chunks, delete). Parsing "takes" FAKE_PARSE_SECONDS (default 1); a file whose name
contains "fail" ends in the engine's failure state. RAGFlow uses its documented envelope
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
import re
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

API_KEY = os.environ.get("FAKE_RAGFLOW_KEY", "fake-ragflow-key")
PORT = int(os.environ.get("FAKE_RAGFLOW_PORT", "9380"))
CHUNK_METHODS = {"naive", "book", "qa", "manual", "paper", "laws", "presentation", "table", "one", "picture", "email"}

PARSE_SECONDS = float(os.environ.get("FAKE_PARSE_SECONDS", "1"))
CHUNK_SIZE = 200

lock = threading.Lock()
datasets: dict[str, dict] = {}
dify_datasets: dict[str, dict] = {}
documents: dict[str, dict] = {}  # both engines: id -> {engine, dataset, name, text, started, fail, batch}
fail_next = 0


def parse_multipart(content_type: str, body: bytes) -> dict[str, tuple[str | None, bytes]]:
    """name -> (filename, bytes) for each part of a multipart/form-data body."""
    message = BytesParser(policy=email_policy).parsebytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
    parts: dict[str, tuple[str | None, bytes]] = {}
    for part in message.iter_parts():
        name = part.get_param("name", header="content-disposition")
        parts[str(name)] = (part.get_filename(), part.get_payload(decode=True) or b"")
    return parts


def new_document(engine: str, dataset_id: str, filename: str, data: bytes) -> dict:
    text = data.decode("utf-8", errors="replace")
    document = {
        "id": uuid.uuid4().hex if engine == "ragflow" else str(uuid.uuid4()), "engine": engine, "dataset": dataset_id,
        "name": filename, "text": text, "size": len(data), "started": None, "batch": str(uuid.uuid4()),
        "fail": "fail" in filename.lower(),
    }
    documents[document["id"]] = document
    return document


def document_progress(document: dict) -> str:
    """not_started | running | done | failed, from how long ago parsing started."""
    if document["started"] is None:
        return "not_started"
    if time.monotonic() - document["started"] < PARSE_SECONDS:
        return "running"
    return "failed" if document["fail"] else "done"


def similarity(query: str, text: str) -> float:
    """Share of the query's words / CJK character pairs that occur in the text (0..1); a stand-in for embeddings."""
    def terms(value: str) -> set[str]:
        value = value.lower()
        found = set(re.findall(r"[a-z0-9]+", value))
        for run in re.findall(r"[\u4e00-\u9fff]+", value):
            found.update(run[i : i + 2] for i in range(len(run) - 1))
        return found

    wanted = terms(query)
    return len(wanted & terms(text)) / len(wanted) if wanted else 0.0


def search_chunks(dataset_ids: list[str], query: str, threshold: float, top_k: int) -> list[dict]:
    """Chunks of every *indexed* document in the given datasets scored against the query, best first."""
    hits = []
    for document in documents.values():
        if document["dataset"] not in dataset_ids or document_progress(document) != "done":
            continue
        for number, text in enumerate(chunks_of(document)):
            score = similarity(query, text)
            if score >= threshold:
                hits.append({
                    "dataset": document["dataset"], "document": document, "id": f"{document['id']}-{number}",
                    "content": text, "score": score,
                })
    return sorted(hits, key=lambda hit: hit["score"], reverse=True)[:top_k]


def chunks_of(document: dict) -> list[str]:
    text = document["text"] or " "
    return [text[i : i + CHUNK_SIZE] for i in range(0, len(text), CHUNK_SIZE)] or [" "]


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

        def _ragflow_documents(self, method: str, route: re.Match, query: dict) -> None:
            dataset_id, kind, doc_id = route.group(1), route.group(2), route.group(3)
            with lock:
                if dataset_id not in datasets:
                    return self._error("You don't own the dataset.")
                if kind == "documents" and doc_id is None and method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
                    filename, data = files.get("file", (None, b""))
                    if not filename:
                        return self._error("No file part!")
                    document = new_document("ragflow", dataset_id, filename, data)
                    datasets[dataset_id]["document_count"] += 1
                    return self._send({"code": 0, "data": [{"id": document["id"], "name": filename, "size": document["size"]}]})
                if kind == "documents" and doc_id is None and method == "GET":
                    found = [
                        d for d in documents.values()
                        if d["engine"] == "ragflow" and d["dataset"] == dataset_id and query.get("id") in (None, d["id"])
                    ]
                    docs = []
                    for d in found:
                        state = document_progress(d)
                        docs.append({
                            "id": d["id"], "name": d["name"], "size": d["size"],
                            "run": {"not_started": "UNSTART", "running": "RUNNING", "done": "DONE", "failed": "FAIL"}[state],
                            "progress": 1.0 if state == "done" else 0.0,
                            "chunk_count": len(chunks_of(d)) if state == "done" else 0,
                            "progress_msg": "Unsupported content" if state == "failed" else "",
                        })
                    return self._send({"code": 0, "data": {"docs": docs, "total": len(docs)}})
                if kind == "documents" and doc_id is None and method == "DELETE":
                    ids = self._body().get("ids") or []
                    missing = [i for i in ids if i not in documents or documents[i]["dataset"] != dataset_id]
                    if missing:
                        return self._error(f"Document {missing[0]} not found")
                    for i in ids:
                        del documents[i]
                        datasets[dataset_id]["document_count"] -= 1
                    return self._send({"code": 0})
                if kind == "chunks" and doc_id is None and method == "POST":  # start parsing
                    for i in self._body().get("document_ids") or []:
                        if i in documents:
                            documents[i]["started"] = time.monotonic()
                    return self._send({"code": 0})
                if kind == "documents" and doc_id and method == "GET":
                    document = documents.get(doc_id)
                    if not document or document["dataset"] != dataset_id:
                        return self._error("Document not found")
                    chunks = chunks_of(document) if document_progress(document) == "done" else []
                    page, size = int(query.get("page", "1")), int(query.get("page_size", "30"))
                    return self._send({"code": 0, "data": {
                        "chunks": [{"id": f"{doc_id}-{n}", "content": c} for n, c in enumerate(chunks[(page - 1) * size : page * size])],
                        "total": len(chunks),
                    }})
            self._send({"code": 405, "message": "Method not allowed", "status": 405}, 405)

        def _dify_documents(self, method: str, parts: list[str], query: dict) -> None:
            dataset_id = parts[2]
            with lock:
                if dataset_id not in dify_datasets:
                    return self._send({"code": "not_found", "message": "Dataset not found.", "status": 404}, 404)
                if parts[3:] == ["retrieve"] and method == "POST":
                    body = self._body()
                    model = body.get("retrieval_model") or {}
                    hits = search_chunks([dataset_id], str(body.get("query") or ""),
                                         float(model.get("score_threshold") or 0), int(model.get("top_k") or 4))
                    return self._send({"query": {"content": body.get("query")}, "records": [
                        {"segment": {"id": h["id"], "content": h["content"], "document": {"id": h["document"]["id"], "name": h["document"]["name"]}},
                         "score": h["score"]} for h in hits]})
                if parts[3:] == ["document", "create-by-file"] and method == "POST":
                    length = int(self.headers.get("Content-Length") or 0)
                    files = parse_multipart(self.headers.get("Content-Type", ""), self.rfile.read(length))
                    filename, data = files.get("file", (None, b""))
                    if not filename:
                        return self._send({"code": "no_file_uploaded", "message": "Please upload your file.", "status": 400}, 400)
                    try:
                        options = json.loads(files.get("data", (None, b"{}"))[1] or b"{}")
                    except ValueError:
                        options = {}
                    if options.get("process_rule", {}).get("mode") not in ("automatic", "custom", "hierarchical"):
                        return self._send({"code": "invalid_param", "message": "process_rule is required.", "status": 400}, 400)
                    document = new_document("dify", dataset_id, filename, data)
                    document["started"] = time.monotonic()  # Dify indexes on its own
                    dify_datasets[dataset_id]["document_count"] += 1
                    return self._send({"document": {"id": document["id"], "name": filename, "indexing_status": "waiting"}, "batch": document["batch"]})
                if len(parts) == 6 and parts[3] == "documents" and parts[5] == "indexing-status" and method == "GET":
                    found = [d for d in documents.values() if d["engine"] == "dify" and d["dataset"] == dataset_id and d["batch"] == parts[4]]
                    rows = []
                    for d in found:
                        state = document_progress(d)
                        total = len(chunks_of(d))
                        rows.append({
                            "id": d["id"], "indexing_status": {"not_started": "waiting", "running": "indexing", "done": "completed", "failed": "error"}[state],
                            "completed_segments": total if state == "done" else 0, "total_segments": total if state == "done" else 0,
                            "error": "Unsupported content" if state == "failed" else None,
                        })
                    return self._send({"data": rows})
                if len(parts) == 6 and parts[3] == "documents" and parts[5] == "segments" and method == "GET":
                    document = documents.get(parts[4])
                    if not document or document["dataset"] != dataset_id:
                        return self._send({"code": "not_found", "message": "Document not found.", "status": 404}, 404)
                    chunks = chunks_of(document) if document_progress(document) == "done" else []
                    page, limit = int(query.get("page", "1")), int(query.get("limit", "20"))
                    return self._send({
                        "data": [{"id": f"{parts[4]}-{n}", "position": n + 1, "content": c} for n, c in enumerate(chunks[(page - 1) * limit : page * limit])],
                        "total": len(chunks), "page": page, "limit": limit, "has_more": len(chunks) > page * limit,
                    })
                if len(parts) == 5 and parts[3] == "documents" and method == "DELETE":
                    document = documents.get(parts[4])
                    if not document or document["dataset"] != dataset_id:
                        return self._send({"code": "not_found", "message": "Document Not Exists.", "status": 404}, 404)
                    del documents[parts[4]]
                    dify_datasets[dataset_id]["document_count"] -= 1
                    self.send_response(204)
                    self.end_headers()
                    return
            self._send({"code": "method_not_allowed", "message": "Method not allowed", "status": 405}, 405)

        def _dify(self, method: str, path: str, query: dict) -> None:
            global fail_next
            if self.headers.get("Authorization") != f"Bearer {API_KEY}":
                return self._send({"code": "unauthorized", "message": "Access token is invalid", "status": 401}, 401)
            with lock:
                if fail_next > 0:
                    fail_next -= 1
                    return self._send({"code": "internal_error", "message": "Injected failure", "status": 500}, 500)
            parts = path.strip("/").split("/")  # ["v1", "datasets", id?]
            if parts[:2] == ["v1", "datasets"] and len(parts) > 3:
                return self._dify_documents(method, parts, query)
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
                    for key in [k for k, d in documents.items() if d["dataset"] == parts[2]]:
                        del documents[key]
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
            if url.path == "/api/v1/retrieval" and method == "POST":
                body = self._body()
                with lock:
                    ids = body.get("dataset_ids") or []
                    missing = [i for i in ids if i not in datasets]
                    if missing:
                        return self._error(f"You don't own the dataset {missing[0]}")
                    hits = search_chunks(ids, str(body.get("question") or ""), float(body.get("similarity_threshold") or 0),
                                         int(body.get("top_k") or 8))
                    return self._send({"code": 0, "data": {"chunks": [
                        {"id": h["id"], "content": h["content"], "document_id": h["document"]["id"],
                         "document_keyword": h["document"]["name"], "dataset_id": h["dataset"], "similarity": h["score"]}
                        for h in hits], "total": len(hits)}})
            doc_route = re.fullmatch(r"/api/v1/datasets/([^/]+)/(documents|chunks)(?:/([^/]+)/chunks)?", url.path)
            if doc_route:
                return self._ragflow_documents(method, doc_route, query)
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
                        for key in [k for k, d in documents.items() if d["dataset"] == item]:
                            del documents[key]
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
