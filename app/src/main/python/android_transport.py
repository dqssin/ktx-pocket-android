"""Adapt pykorail's HTTP session protocol to Android HTTPS without curl-cffi."""
import json
import random
import threading
import time
from contextlib import contextmanager
from urllib.parse import urlencode

_gate = threading.Lock()
_last_finished = 0.0
_reservation_guard = None


class ReservationWindowClosed(Exception):
    """The booking request was not sent because its departure is no longer reachable."""


@contextmanager
def request_window(endpoint):
    """Share pacing and the last booking check across phone and desktop sessions."""
    global _last_finished
    with _gate:
        gap = random.uniform(3.0, 5.0)
        time.sleep(max(0.0, gap - (time.monotonic() - _last_finished)))
        if _reservation_guard is not None:
            from pykorail.constants import API_ENDPOINTS
            latest_start, stopped = _reservation_guard
            if endpoint == API_ENDPOINTS["reserve"] and (stopped() or time.time() >= latest_start):
                raise ReservationWindowClosed()
        try:
            yield
        finally:
            _last_finished = time.monotonic()


@contextmanager
def reservation_window(latest_start, stopped):
    global _reservation_guard
    previous = _reservation_guard
    _reservation_guard = latest_start, stopped
    try:
        yield
    finally:
        _reservation_guard = previous


class Response:
    def __init__(self, data):
        self.status_code = data["status_code"]
        self.text = data["text"]


class AndroidSession:
    def __init__(self, headers=None, impersonate=None):
        from java import jclass
        self.headers = dict(headers or {})
        self.native = jclass("kr.personal.ktxpocket.AndroidHttp")()

    def _request(self, method, url, params=None, data=None, headers=None, **kwargs):
        endpoint = url
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params, doseq=True)
        body = urlencode(data, doseq=True) if data is not None else ""
        combined = {**self.headers, **(headers or {})}
        with request_window(endpoint):
            try:
                result = self.native.exchange(url, method, json.dumps(combined), body)
                return Response(json.loads(str(result)))
            except Exception:
                from pykorail.exceptions import TransportError
                raise TransportError("Android HTTPS connection failed") from None

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def close(self):
        self.native = None


def install():
    import pykorail.transport
    import pykorail._sync.client
    import pykorail._sync.netfunnel
    # Both resources bind create_session at import time; replace those explicit seams too.
    pykorail.transport.create_session = AndroidSession
    pykorail._sync.client.create_session = AndroidSession
    pykorail._sync.netfunnel.create_session = AndroidSession
