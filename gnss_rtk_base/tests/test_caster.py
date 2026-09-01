import base64
import socket
import struct
import threading
import time

import caster


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_caster(mountpoint="TEST", user="", password="", max_clients=None, limiter=None):
    """Starts a fake "str2str" TCP server on relay_port (run_relay_receiver
    is a client that connects to it, see caster.py's module docstring for
    why the roles are reversed from what you'd expect) plus the real
    caster_server under test."""
    relay_port = _free_port()
    caster_port = _free_port()
    caster.INTERNAL_RELAY_PORT = relay_port
    broadcaster = caster.Broadcaster()

    fake_str2str_srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    fake_str2str_srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    fake_str2str_srv.bind(("127.0.0.1", relay_port))
    fake_str2str_srv.listen(1)

    threading.Thread(target=caster.run_relay_receiver, args=(broadcaster,), daemon=True).start()
    threading.Thread(
        target=caster.run_caster_server,
        args=(broadcaster, mountpoint, user, password, caster_port, max_clients, limiter),
        daemon=True,
    ).start()
    time.sleep(0.2)
    return broadcaster, relay_port, caster_port, fake_str2str_srv


def test_wrong_mountpoint_returns_sourcetable():
    _, _, caster_port, _ = _start_caster(mountpoint="GNSSBASE")
    s = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    resp = s.recv(4096)
    s.close()
    assert b"SOURCETABLE 200 OK" in resp
    assert b"GNSSBASE" in resp


def test_missing_auth_returns_401():
    _, _, caster_port, _ = _start_caster(mountpoint="GNSSBASE", user="user", password="pass")
    s = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    s.sendall(b"GET /GNSSBASE HTTP/1.1\r\nHost: x\r\n\r\n")
    resp = s.recv(4096)
    s.close()
    assert b"401" in resp


def test_correct_auth_relays_rtcm_bytes():
    broadcaster, relay_port, caster_port, fake_str2str_srv = _start_caster(
        mountpoint="GNSSBASE", user="user", password="pass")

    rover = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    auth = base64.b64encode(b"user:pass").decode()
    rover.sendall(f"GET /GNSSBASE HTTP/1.1\r\nHost: x\r\nAuthorization: Basic {auth}\r\n\r\n".encode())
    resp = rover.recv(4096)
    assert b"ICY 200 OK" in resp

    time.sleep(0.2)
    assert broadcaster.num_clients() == 1

    # run_relay_receiver already connected in to fake_str2str_srv as a
    # client (see its module docstring for why the roles are reversed).
    fake_str2str_srv.settimeout(2)
    upstream, _ = fake_str2str_srv.accept()
    payload = b"\xd3\x00\x13FAKE_RTCM_PAYLOAD_1234"
    upstream.sendall(payload)
    time.sleep(0.2)
    assert rover.recv(4096) == payload

    rover.close()
    upstream.close()


def test_relay_receiver_reconnects_after_abrupt_disconnect(monkeypatch):
    """Regression: an unhandled ConnectionResetError from recv() (e.g. the
    str2str side dying/restarting mid-stream, not a clean close) used to
    kill run_relay_receiver's thread silently - it never reconnected
    again. Simulates that by closing str2str's end of the connection
    (RST-like from the receiver's point of view), and checks the relay
    reconnects on its own and keeps working."""
    monkeypatch.setattr(caster, "RELAY_RETRY_INTERVAL_S", 0.2)
    relay_port = _free_port()
    caster.INTERNAL_RELAY_PORT = relay_port
    broadcaster = caster.Broadcaster()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", relay_port))
    srv.listen(1)
    srv.settimeout(5)
    threading.Thread(target=caster.run_relay_receiver, args=(broadcaster,), daemon=True).start()

    first, _ = srv.accept()
    first.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    first.close()  # abrupt: triggers RST instead of a clean FIN

    second, _ = srv.accept()  # run_relay_receiver must reconnect by itself
    rover_pub, rover_priv = socket.socketpair()
    broadcaster.add_client(rover_pub)
    second.sendall(b"still working after reconnect")
    time.sleep(0.3)
    assert rover_priv.recv(4096) == b"still working after reconnect"

    second.close()
    rover_pub.close()
    rover_priv.close()


def test_no_auth_required_when_credentials_empty():
    _, _, caster_port, _ = _start_caster(mountpoint="GNSSBASE", user="", password="")
    s = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    s.sendall(b"GET /GNSSBASE HTTP/1.1\r\nHost: x\r\n\r\n")
    resp = s.recv(4096)
    s.close()
    assert b"ICY 200 OK" in resp


def _auth_get(caster_port, mountpoint, user=None, password=None, timeout=2):
    s = socket.create_connection(("127.0.0.1", caster_port), timeout=timeout)
    req = f"GET /{mountpoint} HTTP/1.1\r\nHost: x\r\n"
    if user is not None:
        auth = base64.b64encode(f"{user}:{password}".encode()).decode()
        req += f"Authorization: Basic {auth}\r\n"
    req += "\r\n"
    s.sendall(req.encode())
    resp = s.recv(4096)
    s.close()
    return resp


def test_auth_rate_limiter_blocks_after_max_failures():
    limiter = caster.AuthRateLimiter(max_failures=3, window_s=10, block_duration_s=5)
    for _ in range(2):
        limiter.record_failure("1.2.3.4")
        assert not limiter.is_blocked("1.2.3.4")
    limiter.record_failure("1.2.3.4")  # third failure: reaches the threshold
    assert limiter.is_blocked("1.2.3.4")


def test_auth_rate_limiter_unblocks_after_duration():
    limiter = caster.AuthRateLimiter(max_failures=1, window_s=10, block_duration_s=0.3)
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4")
    time.sleep(0.4)
    assert not limiter.is_blocked("1.2.3.4")


def test_auth_rate_limiter_ignores_other_ips():
    limiter = caster.AuthRateLimiter(max_failures=1, window_s=10, block_duration_s=5)
    limiter.record_failure("1.2.3.4")
    assert limiter.is_blocked("1.2.3.4")
    assert not limiter.is_blocked("5.6.7.8")


def test_repeated_failed_auth_gets_blocked_end_to_end():
    """Simulates a brute-force attempt: after max_failures wrong
    passwords from the same connection (same IP, 127.0.0.1 in tests),
    further attempts are rejected immediately, even with the correct
    password in the meantime."""
    limiter = caster.AuthRateLimiter(max_failures=2, window_s=10, block_duration_s=5)
    _, _, caster_port, _ = _start_caster(mountpoint="GNSSBASE", user="user", password="pass", limiter=limiter)

    for _ in range(2):
        resp = _auth_get(caster_port, "GNSSBASE", user="user", password="wrong")
        assert b"401" in resp

    # the IP is now blocked: even the correct password gets rejected
    resp = _auth_get(caster_port, "GNSSBASE", user="user", password="pass")
    assert b"401" in resp


def test_max_clients_limit_rejects_extra_connections():
    _, _, caster_port, _ = _start_caster(mountpoint="GNSSBASE", max_clients=1)

    first = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    first.sendall(b"GET /GNSSBASE HTTP/1.1\r\nHost: x\r\n\r\n")
    resp1 = first.recv(4096)
    assert b"ICY 200 OK" in resp1
    time.sleep(0.2)

    second = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    second.sendall(b"GET /GNSSBASE HTTP/1.1\r\nHost: x\r\n\r\n")
    resp2 = second.recv(4096)
    assert b"503" in resp2

    first.close()
    second.close()
