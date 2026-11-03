"""Use the library's desktop session with the same request policy as Android."""
from android_transport import request_window, reservation_window, ReservationWindowClosed

_original_factory = None


class DesktopSession:
    def __init__(self, headers=None, **kwargs):
        self.session = _original_factory(headers=headers, **kwargs)
        self.headers = self.session.headers

    def _request(self, method, url, **kwargs):
        from pykorail.exceptions import TransportError
        with request_window(url):
            try:
                return getattr(self.session, method)(url, **{**kwargs, "timeout": 20})
            except Exception:
                raise TransportError("Desktop HTTPS connection failed") from None

    def get(self, url, **kwargs):
        return self._request("get", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("post", url, **kwargs)

    def close(self):
        self.session.close()


def install():
    global _original_factory
    import pykorail.transport
    import pykorail._sync.client
    import pykorail._sync.netfunnel
    if _original_factory is None:
        _original_factory = pykorail.transport.create_session
    pykorail.transport.create_session = DesktopSession
    pykorail._sync.client.create_session = DesktopSession
    pykorail._sync.netfunnel.create_session = DesktopSession
