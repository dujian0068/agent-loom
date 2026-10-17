"""Keep synchronous environment work fenced while the event loop cannot renew."""

import threading


class LeaseHeartbeat:
    def __init__(self, renew, ttl, *, name):
        self.renew, self.ttl, self.name = renew, ttl, name
        self._stop = threading.Event()
        self._thread = None
        self._error = None

    def _heartbeat(self):
        while not self._stop.wait(self.ttl / 3):
            try:
                self.renew()
            except Exception as exc:
                # Renewal verifies the original epoch. Never claim a new lease.
                self._error = exc
                return

    def __enter__(self):
        self.renew()
        self._thread = threading.Thread(target=self._heartbeat, name=self.name, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        self._thread.join()
        if exc_type is None and self._error is not None:
            raise self._error
