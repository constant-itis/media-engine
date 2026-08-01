#!/usr/bin/env python3
"""HTTP + web-UI face over the media engine.

Third face alongside cli.py (CLI) and server.py (MCP) — all three drive the same
OPERATIONS registry, so an operation added once appears everywhere.

Zero new dependencies: stdlib http.server + a single self-contained HTML page.
Upload-based — the browser uploads the input file(s), the op runs in a background
thread in a temp workdir, live progress streams back over Server-Sent Events, and
the result is downloaded when done. Nothing depends on the user's own filesystem
paths, so a downloaded build works with no setup.

Run:  python3 webserver.py [--host 127.0.0.1] [--port 8765]
Then open http://127.0.0.1:8765

Security: binds localhost by default. It runs ffmpeg on uploaded input; do not
expose it to an untrusted network.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import uuid
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.operations import OPERATIONS, run_operation

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_FIELD = "__input__"
JOB_TTL = 1800  # seconds; abandoned jobs are reaped after this

_MIME = {
    "mp4": "video/mp4", "webm": "video/webm", "gif": "image/gif",
    "webp": "image/webp", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "bmp": "image/bmp", "tiff": "image/tiff",
    "mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
    "ogg": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac",
    "m4a": "audio/mp4", "aiff": "audio/aiff",
}

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def operations_json() -> list[dict]:
    out = []
    for op in OPERATIONS.values():
        out.append({
            "id": op.id, "category": op.category, "description": op.description,
            "params": [{
                "name": p.name, "kind": p.kind, "default": p.default,
                "choices": p.choices, "min": p.min, "max": p.max,
                "required": p.required, "help": p.help,
            } for p in op.params],
        })
    return out


def parse_multipart(content_type: str, body: bytes):
    parsed = BytesParser(policy=email_policy).parsebytes(
        b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
    )
    fields, files = {}, {}
    for part in parsed.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""
        if filename:
            files[name] = (filename, payload)
        else:
            fields[name] = payload.decode("utf-8", "replace").strip()
    return fields, files


def reap_jobs():
    now = time.time()
    with JOBS_LOCK:
        stale = [k for k, j in JOBS.items() if now - j["created"] > JOB_TTL]
        for k in stale:
            shutil.rmtree(JOBS[k].get("workdir", ""), ignore_errors=True)
            del JOBS[k]


def _worker(job_id, op_id, input_path, output_path, params, workdir):
    def cb(label, done, total):
        pct = (done / total * 100) if total else None
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if j:
                j["progress"] = {"label": label, "done": round(done, 1),
                                 "total": round(total, 1) if total else None, "pct": pct}
    try:
        run_operation(op_id, input_path, output_path, params, progress=cb)
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id].update(status="done",
                                    progress={"label": "done", "pct": 100})
    except Exception as e:  # noqa: BLE001
        with JOBS_LOCK:
            if job_id in JOBS:
                JOBS[job_id].update(status="error", error=str(e))
        shutil.rmtree(workdir, ignore_errors=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "media-engine-web"

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/octet-stream", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj).encode(), "application/json")

    # ---- GET ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            fp = os.path.join(HERE, "web", "index.html")
            if os.path.isfile(fp):
                with open(fp, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            else:
                self._send(200, b"<h1>media-engine</h1><p>web/index.html missing</p>",
                           "text/html; charset=utf-8")
        elif path == "/api/operations":
            self._json(200, {"operations": operations_json()})
        elif path == "/health":
            self._json(200, {"ok": True})
        elif path.startswith("/api/progress/"):
            self._stream_progress(path[len("/api/progress/"):])
        elif path.startswith("/api/result/"):
            self._send_result(path[len("/api/result/"):])
        else:
            self._json(404, {"ok": False, "error": "not found"})

    def _stream_progress(self, job_id):
        with JOBS_LOCK:
            exists = job_id in JOBS
        if not exists:
            self._json(404, {"ok": False, "error": "unknown job"})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            while True:
                with JOBS_LOCK:
                    j = JOBS.get(job_id)
                    if not j:
                        break
                    payload = {"status": j["status"], **(j.get("progress") or {})}
                    if j["status"] == "error":
                        payload["error"] = j.get("error", "failed")
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
                if payload["status"] in ("done", "error"):
                    break
                time.sleep(0.2)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_result(self, job_id):
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            ready = j and j["status"] == "done"
        if not ready:
            self._json(404, {"ok": False, "error": "result not ready"})
            return
        try:
            with open(j["output_path"], "rb") as f:
                data = f.read()
            self._send(200, data, j["mime"], {
                "Content-Disposition": f'attachment; filename="{j["output_name"]}"',
                "X-Output-Filename": j["output_name"],
            })
        finally:
            shutil.rmtree(j.get("workdir", ""), ignore_errors=True)
            with JOBS_LOCK:
                JOBS.pop(job_id, None)

    # ---- POST ----
    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith("/api/run/"):
            self._json(404, {"ok": False, "error": "not found"})
            return
        op_id = path[len("/api/run/"):]
        if op_id not in OPERATIONS:
            self._json(404, {"ok": False, "error": f"unknown operation '{op_id}'"})
            return
        op = OPERATIONS[op_id]

        ctype = self.headers.get("Content-Type", "")
        length = int(self.headers.get("Content-Length", 0) or 0)
        if "multipart/form-data" not in ctype or length <= 0:
            self._json(400, {"ok": False, "error": "expected multipart/form-data upload"})
            return
        body = self.rfile.read(length)
        try:
            fields, files = parse_multipart(ctype, body)
        except Exception as e:  # noqa: BLE001
            self._json(400, {"ok": False, "error": f"could not parse upload: {e}"})
            return
        if INPUT_FIELD not in files:
            self._json(400, {"ok": False, "error": "no input file provided"})
            return

        reap_jobs()
        import tempfile
        work = tempfile.mkdtemp(prefix="medeng-")
        try:
            in_name, in_bytes = files[INPUT_FIELD]
            in_ext = os.path.splitext(in_name)[1] or ".bin"
            input_path = os.path.join(work, "input" + in_ext)
            with open(input_path, "wb") as f:
                f.write(in_bytes)

            params = dict(fields)
            for p in op.params:
                if p.kind == "path" and p.name in files:
                    fn, data = files[p.name]
                    aux = os.path.join(work, "aux_" + os.path.basename(fn or p.name))
                    with open(aux, "wb") as f:
                        f.write(data)
                    params[p.name] = aux
                elif p.name in params and params[p.name] == "":
                    del params[p.name]

            known = {pp.name for pp in op.params}
            valid = op.validate({k: v for k, v in params.items() if k in known})
            ext = op.output_ext(valid)
            out_name = f"{os.path.splitext(in_name)[0]}_{op_id}.{ext}"
            output_path = os.path.join(work, "output." + ext)
        except Exception as e:  # noqa: BLE001 - validation / IO before the job starts
            shutil.rmtree(work, ignore_errors=True)
            self._json(400, {"ok": False, "error": str(e)})
            return

        job_id = uuid.uuid4().hex
        with JOBS_LOCK:
            JOBS[job_id] = {
                "status": "running", "progress": {"label": "starting", "pct": None},
                "output_path": output_path, "output_name": out_name,
                "mime": _MIME.get(ext.lower(), "application/octet-stream"),
                "workdir": work, "created": time.time(),
            }
        threading.Thread(target=_worker, daemon=True,
                         args=(job_id, op_id, input_path, output_path, params, work)).start()
        self._json(200, {"job_id": job_id})


def main():
    ap = argparse.ArgumentParser(prog="media-engine-web")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"media-engine web UI  ->  http://{a.host}:{a.port}  ({len(OPERATIONS)} operations)")
    print("Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
