"""Server HTTP minimale per la pagina skyplot, pensato per girare dietro
l'Ingress di Home Assistant (path dinamico, quindi la pagina usa solo URL
relativi). Nessuna dipendenza esterna: solo http.server della stdlib."""

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WWW_DIR = Path(__file__).parent / "www"


def make_handler(state, fix_label_fn):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # silenzia il log di default di BaseHTTPRequestHandler

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
                # eventuali altri asset statici sotto www/
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
    print(f"[webui] skyplot disponibile sulla porta {port}", flush=True)
    server.serve_forever()
