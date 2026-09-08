from types import SimpleNamespace

import pytest

from state import SharedState
from webui import make_handler


def _handler_class():
    return make_handler(SharedState(), lambda q: "Fix", lambda hours: {}, lambda: {}, lambda t, p: None)


def test_send_json_survives_a_broken_pipe():
    """Regression: a client giving up on a slow /api/sky_heatmap call
    (its own shorter timeout, or a browser tab closed/navigated away)
    before the add-on finished computing used to dump a full
    BrokenPipeError traceback into the add-on's own log - harmless (the
    server keeps running) but noisy. Found from a real diagnostic query
    whose client-side timeout was shorter than the request actually
    took."""
    Handler = _handler_class()

    class DeadWfile:
        def write(self, data):
            raise BrokenPipeError("client gone")

    fake_self = SimpleNamespace(
        wfile=DeadWfile(),
        send_response=lambda status: None,
        send_header=lambda k, v: None,
        end_headers=lambda: None,
    )

    Handler._send_json(fake_self, {"ok": True})  # must not raise


def test_send_json_still_raises_for_an_unrelated_error():
    """The BrokenPipeError/ConnectionResetError handling must not become a
    blanket try/except that also hides a real bug in this method."""
    Handler = _handler_class()
    fake_self = SimpleNamespace(
        wfile=None,
        send_response=lambda status: (_ for _ in ()).throw(TypeError("boom")),
        send_header=lambda k, v: None,
        end_headers=lambda: None,
    )

    with pytest.raises(TypeError, match="boom"):
        Handler._send_json(fake_self, {"ok": True})
