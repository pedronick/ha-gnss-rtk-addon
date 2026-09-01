"""
Mini NTRIP caster (ICY/NTRIP v1-style handshake, supported by nearly all
NTRIP clients) built into the add-on: rovers connect directly to this
add-on instead of to an external caster.

RTCM3 bytes arrive from str2str via an extra output
"-out tcpsvr://:INTERNAL_RELAY_PORT" (str2str listens, we connect to it
as a client and read), which is then re-distributed here to all
connected rovers. No process reads the serial port more than once: it's
always str2str that does it, consistent with the same principle used for
external casters and for the raw log of the PPP campaign.

Why str2str is the server and we're the client here, not the other way
around (which would arguably be the more obvious design): RTKLIB's
"tcpcli" (client) stream type crashes with a real SIGSEGV on Alpine/musl
(the base image used to build this add-on) - reproduced with a real
build and a debugger: gentcp()'s client branch in stream.c calls the
legacy, non-getaddrinfo gethostbyname() to resolve the target address,
even a plain numeric IP like 127.0.0.1, and that corrupts memory on
musl. "tcpsvr" (the server role) never calls gethostbyname() at all, so
it's unaffected - confirmed by reproducing the crash and the fix in a
real container from this project's own Dockerfile.
"""

import base64
import socket
import threading
import time

INTERNAL_RELAY_PORT = 28101
CASTER_PORT = 2101
RELAY_RETRY_INTERVAL_S = 2

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
    """Connects to str2str's own TCP server (str2str -out
    tcpsvr://:INTERNAL_RELAY_PORT, see the module docstring for why
    str2str is the server and we're the client here) and forwards every
    byte received to the connected rovers. Retries periodically if
    str2str isn't listening yet (still starting) or the connection drops
    (e.g. a restart via watchdog_str2str), without ever giving up.

    Captures INTERNAL_RELAY_PORT once, at the start, rather than
    re-reading the module global on every retry: this only matters for
    tests, which run multiple independent instances of this add-on in
    the same process and reassign the global each time via
    caster.INTERNAL_RELAY_PORT = ... - without this, an earlier test's
    still-running (daemon) retry loop would start reconnecting to a
    later test's port as soon as the global changed underneath it,
    fighting over the same accept() queue."""
    port = INTERNAL_RELAY_PORT
    while True:
        try:
            conn = socket.create_connection(("127.0.0.1", port), timeout=RELAY_RETRY_INTERVAL_S)
        except OSError:
            time.sleep(RELAY_RETRY_INTERVAL_S)
            continue
        print("[caster] connected to str2str's internal relay", flush=True)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    break
                broadcaster.broadcast(data)
        except OSError:
            pass  # e.g. connection reset by a str2str restart - just reconnect below
        finally:
            conn.close()
            print("[caster] disconnected from str2str's internal relay, retrying...", flush=True)
        time.sleep(RELAY_RETRY_INTERVAL_S)


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
