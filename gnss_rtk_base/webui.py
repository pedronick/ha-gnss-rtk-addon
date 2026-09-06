"""Minimal HTTP server for the skyplot page, designed to run behind Home
Assistant Ingress (dynamic path, so the page only uses relative URLs). No
external dependencies: just http.server from the stdlib."""

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

WWW_DIR = Path(__file__).parent / "www"


def make_handler(state, fix_label_fn, sky_heatmap_fn, mqtt_state_fn, command_fn):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            pass  # silence BaseHTTPRequestHandler's default logging

        def do_GET(self):
            split = urlsplit(self.path)
            path = split.path
            if path in ("/", "/index.html"):
                self._serve_file(WWW_DIR / "index.html")
            elif path == "/api/state":
                self._send_json(state.snapshot(fix_label_fn))
            elif path == "/api/mqtt_state":
                # Same states published via MQTT Discovery (see
                # App.web_state_snapshot), for the page's controls/status
                # section - lets it mirror Home Assistant without a
                # second MQTT connection from the browser.
                self._send_json(mqtt_state_fn())
            elif path == "/api/sky_heatmap":
                # Synchronous and can take a few seconds for a large
                # "hours" window (convbin + rnx2rtkp on real data) -
                # ThreadingHTTPServer runs each request in its own thread,
                # so this doesn't block /api/state polling from other tabs.
                try:
                    hours = float(parse_qs(split.query).get("hours", ["6"])[0])
                except ValueError:
                    hours = 6.0
                hours = max(0.1, min(hours, 720))
                self._send_json(sky_heatmap_fn(hours))
            else:
                # any other static assets under www/
                safe_path = (WWW_DIR / path.lstrip("/")).resolve()
                if WWW_DIR in safe_path.parents and safe_path.exists():
                    self._serve_file(safe_path)
                else:
                    self.send_error(404)

        def do_POST(self):
            # The only mutating endpoint: deliberately POST (not GET),
            # since a GET can be triggered by prefetching/retries/link
            # previews - not something you want accidentally starting a
            # PPP campaign or clearing the raw log buffer.
            if self.path != "/api/command":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
                # Same dispatch a real MQTT button/number triggers (see
                # App.handle_web_command) - one implementation, not a
                # second one that could drift from it.
                command_fn(data["topic"], data.get("payload", ""))
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=400)

        def _send_json(self, obj, status=200):
            payload = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

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


def start_webserver(state, fix_label_fn, port, sky_heatmap_fn, mqtt_state_fn, command_fn):
    server = ThreadingHTTPServer(
        ("0.0.0.0", port), make_handler(state, fix_label_fn, sky_heatmap_fn, mqtt_state_fn, command_fn))
    print(f"[webui] skyplot available on port {port}", flush=True)
    server.serve_forever()
