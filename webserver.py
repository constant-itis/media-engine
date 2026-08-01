#!/usr/bin/env python3
"""HTTP + web-UI face over the media engine.

Third face alongside cli.py (CLI) and server.py (MCP) — all three drive the same
OPERATIONS registry, so an operation added once appears everywhere.

Zero new dependencies: stdlib http.server + a single self-contained HTML page.
Upload-based — the browser uploads the input file(s), the server runs the op in a
temp workdir and streams the result back for download. Nothing depends on the
user's own filesystem paths, so a downloaded build works with no setup.

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
import tempfile
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.operations import OPERATIONS, run_operation

HERE = os.path.dirname(os.path.abspath(__file__))
INPUT_FIELD = "__input__"  # the primary input file part name

_MIME = {
    "mp4": "video/mp4", "webm": "video/webm", "gif": "image/gif",
    "webp": "image/webp", "png": "image/png", "jpg": "image/jpeg",
    "jpeg": "image/jpeg", "bmp": "image/bmp", "tiff": "image/tiff",
    "mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac",
    "ogg": "audio/ogg", "opus": "audio/ogg", "aac": "audio/aac",
    "m4a": "audio/mp4", "aiff": "audio/aiff",
}


def operations_json() -> list[dict]:
    out = []
    for op in OPERATIONS.values():
        out.append({
            "id": op.id,
            "category": op.category,
            "description": op.description,
            "params": [{
                "name": p.name, "kind": p.kind, "default": p.default,
                "choices": p.choices, "min": p.min, "max": p.max,
                "required": p.required, "help": p.help,
            } for p in op.params],
        })
    return out


def parse_multipart(content_type: str, body: bytes):
    """Return (fields: dict[str,str], files: dict[str,(filename,bytes)])."""
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


class Handler(BaseHTTPRequestHandler):
    server_version = "media-engine-web"

    def log_message(self, *a):  # keep the console quiet
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

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
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
        else:
            self._json(404, {"ok": False, "error": "not found"})

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

        work = tempfile.mkdtemp(prefix="medeng-")
        try:
            # primary input, keep its extension so ffprobe/format detection works
            in_name, in_bytes = files[INPUT_FIELD]
            in_ext = os.path.splitext(in_name)[1] or ".bin"
            input_path = os.path.join(work, "input" + in_ext)
            with open(input_path, "wb") as f:
                f.write(in_bytes)

            # build params: text fields + any file-backed 'path' params
            params = dict(fields)
            for p in op.params:
                if p.kind == "path" and p.name in files:
                    fn, data = files[p.name]
                    aux = os.path.join(work, "aux_" + os.path.basename(fn or p.name))
                    with open(aux, "wb") as f:
                        f.write(data)
                    params[p.name] = aux
                elif p.name in params and params[p.name] == "":
                    del params[p.name]  # let defaults apply for blank fields

            # validate to resolve the real output extension
            valid = op.validate({k: v for k, v in params.items()
                                 if k in {pp.name for pp in op.params}})
            ext = op.output_ext(valid)
            out_name = f"{os.path.splitext(in_name)[0]}_{op_id}.{ext}"
            output_path = os.path.join(work, "output." + ext)

            run_operation(op_id, input_path, output_path, params)

            with open(output_path, "rb") as f:
                data = f.read()
            mime = _MIME.get(ext.lower(), "application/octet-stream")
            self._send(200, data, mime, {
                "Content-Disposition": f'attachment; filename="{out_name}"',
                "X-Output-Filename": out_name,
            })
        except Exception as e:  # noqa: BLE001
            self._json(400, {"ok": False, "error": str(e)})
        finally:
            shutil.rmtree(work, ignore_errors=True)


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
