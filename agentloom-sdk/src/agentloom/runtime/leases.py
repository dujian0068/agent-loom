"""Renew ownership independently of synchronous provider work and the event loop."""

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

    def check(self):
        """Fail closed after a renewal error, even before the old TTL expires."""
        if self._error is not None:
            raise self._error

    def stop(self):
        """Drain the renewal thread before terminal commit or store shutdown."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
        if exc_type is None:
            self.check()
