import base64
import socket
import threading
import time

import caster


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_caster(mountpoint="TEST", user="", password="", max_clients=None, limiter=None):
    relay_port = _free_port()
    caster_port = _free_port()
    caster.INTERNAL_RELAY_PORT = relay_port
    broadcaster = caster.Broadcaster()
    threading.Thread(target=caster.run_relay_receiver, args=(broadcaster,), daemon=True).start()
    threading.Thread(
        target=caster.run_caster_server,
        args=(broadcaster, mountpoint, user, password, caster_port, max_clients, limiter),
        daemon=True,
    ).start()
    time.sleep(0.2)
    return broadcaster, relay_port, caster_port


def test_wrong_mountpoint_returns_sourcetable():
    _, _, caster_port = _start_caster(mountpoint="GNSSBASE")
    s = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    s.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
    resp = s.recv(4096)
    s.close()
    assert b"SOURCETABLE 200 OK" in resp
    assert b"GNSSBASE" in resp


def test_missing_auth_returns_401():
    _, _, caster_port = _start_caster(mountpoint="GNSSBASE", user="user", password="pass")
    s = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    s.sendall(b"GET /GNSSBASE HTTP/1.1\r\nHost: x\r\n\r\n")
    resp = s.recv(4096)
    s.close()
    assert b"401" in resp


def test_correct_auth_relays_rtcm_bytes():
    broadcaster, relay_port, caster_port = _start_caster(mountpoint="GNSSBASE", user="user", password="pass")

    rover = socket.create_connection(("127.0.0.1", caster_port), timeout=2)
    auth = base64.b64encode(b"user:pass").decode()
    rover.sendall(f"GET /GNSSBASE HTTP/1.1\r\nHost: x\r\nAuthorization: Basic {auth}\r\n\r\n".encode())
    resp = rover.recv(4096)
    assert b"ICY 200 OK" in resp

    time.sleep(0.2)
    assert broadcaster.num_clients() == 1

    upstream = socket.create_connection(("127.0.0.1", relay_port), timeout=2)
    payload = b"\xd3\x00\x13FAKE_RTCM_PAYLOAD_1234"
    upstream.sendall(payload)
    time.sleep(0.2)
    assert rover.recv(4096) == payload

    rover.close()
    upstream.close()


def test_no_auth_required_when_credentials_empty():
    _, _, caster_port = _start_caster(mountpoint="GNSSBASE", user="", password="")
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
    _, _, caster_port = _start_caster(mountpoint="GNSSBASE", user="user", password="pass", limiter=limiter)

    for _ in range(2):
        resp = _auth_get(caster_port, "GNSSBASE", user="user", password="wrong")
        assert b"401" in resp

    # the IP is now blocked: even the correct password gets rejected
    resp = _auth_get(caster_port, "GNSSBASE", user="user", password="pass")
    assert b"401" in resp


def test_max_clients_limit_rejects_extra_connections():
    _, _, caster_port = _start_caster(mountpoint="GNSSBASE", max_clients=1)

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
