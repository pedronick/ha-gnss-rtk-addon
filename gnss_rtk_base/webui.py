"""Minimal HTTP server for the skyplot page, designed to run behind Home
Assistant Ingress (dynamic path, so the page only uses relative URLs). No
external dependencies: just http.server from the stdlib."""

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WWW_DIR = Path(__file__).parent / "www"


def make_handler(state, fix_label_fn):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # silence BaseHTTPRequestHandler's default logging

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/", "/index.html"):
                self._serve_file(WWW_DIR / "index.html")
            elif path == "/api/state":
                payload = json.dumps(state.snapshot(fix_label_fn)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                # any other static assets under www/
                safe_path = (WWW_DIR / path.lstrip("/")).resolve()
                if WWW_DIR in safe_path.parents and safe_path.exists():
                    self._serve_file(safe_path)
                else:
                    self.send_error(404)

        def _serve_file(self, filepath):
            if not filepath.exists():
                self.send_error(404)
                return
            content_type, _ = mimetypes.guess_type(str(filepath))
            data = filepath.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def start_webserver(state, fix_label_fn, port):
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state, fix_label_fn))
    print(f"[webui] skyplot available on port {port}", flush=True)
    server.serve_forever()
