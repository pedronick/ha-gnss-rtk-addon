"""
Mini NTRIP caster (ICY/NTRIP v1-style handshake, supported by nearly all
NTRIP clients) built into the add-on: rovers connect directly to this
add-on instead of to an external caster.

RTCM3 bytes arrive from str2str via an extra output
"-out tcpcli://127.0.0.1:INTERNAL_RELAY_PORT" (str2str connects to us and
forwards the stream to us), which is then re-distributed here to all
connected rovers. No process reads the serial port more than once: it's
always str2str that does it, consistent with the same principle used for
external casters and for the raw log of the PPP campaign.
"""

import base64
import socket
import threading
import time

INTERNAL_RELAY_PORT = 28101
CASTER_PORT = 2101

# Minimal hardening against mountpoint password brute-forcing: after too
# many failed attempts in a short time, the IP is blocked for a cooldown
# period (not a substitute for TLS/VPN if the caster is exposed beyond
# the LAN, see the README).
MAX_AUTH_FAILURES = 5
AUTH_FAILURE_WINDOW_S = 60
AUTH_BLOCK_DURATION_S = 300


class Broadcaster:
    def __init__(self):
        self._lock = threading.Lock()
        self._clients = []

    def add_client(self, sock):
        with self._lock:
            self._clients.append(sock)

    def num_clients(self):
        with self._lock:
            return len(self._clients)

    def broadcast(self, data):
        with self._lock:
            dead = []
            for sock in self._clients:
                try:
                    sock.sendall(data)
                except OSError:
                    dead.append(sock)
            for sock in dead:
                self._clients.remove(sock)
                try:
                    sock.close()
                except OSError:
                    pass


def run_relay_receiver(broadcaster):
    """Accepts the connection from str2str and forwards every byte
    received to the connected rovers. If str2str restarts (watchdog), it
    accepts a new connection without needing to restart this service."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", INTERNAL_RELAY_PORT))
    srv.listen(1)
    print(f"[caster] internal relay listening on 127.0.0.1:{INTERNAL_RELAY_PORT}", flush=True)
    while True:
        conn, _ = srv.accept()
        print("[caster] str2str connected to the internal relay", flush=True)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                broadcaster.broadcast(data)
        finally:
            conn.close()
            print("[caster] str2str disconnected from the internal relay, waiting for reconnection", flush=True)


class AuthRateLimiter:
    """Temporarily blocks an IP after too many failed authentication
    attempts within a time window, to make a brute-force attack on the
    mountpoint password impractical. No persistence: counters reset if
    the add-on restarts."""

    def __init__(self, max_failures=MAX_AUTH_FAILURES, window_s=AUTH_FAILURE_WINDOW_S,
                 block_duration_s=AUTH_BLOCK_DURATION_S):
        self._lock = threading.Lock()
        self._failures = {}       # ip -> [timestamp, ...]
        self._blocked_until = {}  # ip -> timestamp
        self.max_failures = max_failures
        self.window_s = window_s
        self.block_duration_s = block_duration_s

    def is_blocked(self, ip):
        with self._lock:
            until = self._blocked_until.get(ip)
            if until is None:
                return False
            if time.time() < until:
                return True
            del self._blocked_until[ip]
            return False

    def record_failure(self, ip):
        with self._lock:
            now = time.time()
            attempts = [t for t in self._failures.get(ip, []) if now - t < self.window_s]
            attempts.append(now)
            if len(attempts) >= self.max_failures:
                self._blocked_until[ip] = now + self.block_duration_s
                attempts = []
            self._failures[ip] = attempts


def _check_auth(headers, user, password):
    if not user:
        return True
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    return decoded == f"{user}:{password}"


def _read_request(conn):
    data = b""
    conn.settimeout(5)
    try:
        while b"\r\n\r\n" not in data and len(data) < 8192:
            chunk = conn.recv(1024)
            if not chunk:
                break
            data += chunk
    except socket.timeout:
        pass
    conn.settimeout(None)
    lines = data.decode(errors="replace").split("\r\n")
    if not lines or not lines[0]:
        return None, {}
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return lines[0], headers


def build_sourcetable(mountpoint):
    entry = (f"STR;{mountpoint};{mountpoint};RTCM 3.3;1077,1087,1097,1127,1005;2;"
             f"GPS+GLO+GAL+BDS;GNSSBASE;NONE;0.00;0.00;0;0;RTK Base add-on;none;N;N;0;")
    return f"SOURCETABLE 200 OK\r\n\r\n{entry}\r\nENDSOURCETABLE\r\n"


def run_caster_server(broadcaster, mountpoint, user, password, port=CASTER_PORT, max_clients=None, limiter=None):
    if limiter is None:
        limiter = AuthRateLimiter()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(8)
    print(f"[caster] NTRIP caster listening on 0.0.0.0:{port}, mountpoint /{mountpoint}", flush=True)
    while True:
        conn, addr = srv.accept()
        threading.Thread(
            target=_handle_rover,
            args=(conn, addr, broadcaster, mountpoint, user, password, limiter, max_clients),
            daemon=True,
        ).start()


def _handle_rover(conn, addr, broadcaster, mountpoint, user, password, limiter, max_clients):
    ip = addr[0]
    if limiter.is_blocked(ip):
        print(f"[caster] connection from {ip} rejected: too many recent failed authentication attempts",
              flush=True)
        try:
            conn.sendall(b"401 Unauthorized\r\n\r\n")
        except OSError:
            pass
        conn.close()
        return

    request_line, headers = _read_request(conn)
    if not request_line:
        conn.close()
        return
    parts = request_line.split(" ")
    if len(parts) < 2:
        conn.close()
        return
    path = parts[1].lstrip("/")

    if not path or path != mountpoint:
        try:
            conn.sendall(build_sourcetable(mountpoint).encode())
        except OSError:
            pass
        conn.close()
        return

    if not _check_auth(headers, user, password):
        limiter.record_failure(ip)
        try:
            conn.sendall(b"401 Unauthorized\r\n\r\n")
        except OSError:
            pass
        conn.close()
        return

    if max_clients and broadcaster.num_clients() >= max_clients:
        print(f"[caster] connection from {ip} rejected: limit of {max_clients} rovers reached", flush=True)
        try:
            conn.sendall(b"503 Service Unavailable\r\n\r\n")
        except OSError:
            pass
        conn.close()
        return

    try:
        conn.sendall(b"ICY 200 OK\r\n\r\n")
    except OSError:
        conn.close()
        return
    print(f"[caster] rover connected from {addr}", flush=True)
    broadcaster.add_client(conn)
