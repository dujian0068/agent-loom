import threading
import time
from contextlib import contextmanager

from ..configuration.json_documents import copy_document
from ..errors import InfrastructureError
from .base import StoreQueries


class _MemoryTransaction:
    def __init__(self, data):
        self.data = data
        self.pending = {}
        self.now = time.time()

    def get(self, kind, key):
        key = (kind, key)
        return copy_document(self.pending.get(key, self.data.get(key)))

    def read_path(self, kind, key, path):
        pair = (kind, key)
        value = self.pending.get(pair, self.data.get(pair))
        for part in path:
            if value is None:
                return None
            value = value.get(part)
        return copy_document(value)

    def put(self, kind, key, value):
        self.pending[(kind, key)] = copy_document(value)

    def delete(self, kind, key):
        self.pending[(kind, key)] = None

    def list(self, kind):
        merged = self.data | self.pending
        return [copy_document(v) for (k, _), v in merged.items() if k == kind and v is not None]


class MemoryRuntimeStore(StoreQueries):
    """Atomic rollback and fencing within this process; not a durable backend."""

    def __init__(self):
        self._data = {}
        self._lock = threading.RLock()
        self._held = threading.local()

    @contextmanager
    def transaction(self, namespace):
        with self._lock:
            held = getattr(self._held, "namespaces", set())
            if namespace in held:
                raise InfrastructureError("STORE_TRANSACTION_NESTED")
            self._held.namespaces = held
            held.add(namespace)
            try:
                data = self._data.setdefault(namespace, {})
                tx = _MemoryTransaction(data)
                yield tx
                for key, value in tx.pending.items():
                    if value is None:
                        data.pop(key, None)
                    else:
                        data[key] = value
            finally:
                held.remove(namespace)

    def close(self):
        pass

    def read_path(self, namespace, run_id, path):
        with self._lock:
            value = self._data.get(namespace, {}).get(("runs", run_id))
            for key in path:
                if value is None:
                    return None
                value = value.get(key)
            return copy_document(value)
