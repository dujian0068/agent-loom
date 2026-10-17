"""File-backed transactions: immutable JSON records and one atomic manifest."""

import copy
import hashlib
import json
import math
import os
import re
import time
from contextlib import ExitStack, contextmanager

from ..configuration import canonical
from ..errors import ConfigurationError, InfrastructureError
from . import _files
from .base import StoreQueries

_HASH = re.compile(r"[0-9a-f]{64}")
_OBJECT = re.compile(r"[0-9a-f]{64}\.json")
_TEMP = re.compile(r"\.tmp-[0-9a-f]{32}")
_ENVELOPE = re.compile(rb'\{"checksum":"([0-9a-f]{64})","manifest":(.*)\}', re.DOTALL)
_DELETED = object()


def _unavailable():
    return InfrastructureError(
        "STORE_UNAVAILABLE",
        "File storage failed; query using the same stable IDs",
        category="infrastructure",
        phase="commit",
        external_outcome="unknown",
    )


def _corrupt():
    return InfrastructureError("STORE_CORRUPT", "Invalid or missing committed state")


def _key(value):
    if not isinstance(value, str):
        raise ConfigurationError("STORE_KEY_INVALID", "Namespace, kind and key must be strings")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ConfigurationError("STORE_KEY_INVALID", "Invalid Unicode key") from exc
    return value


def _references(records):
    return {reference for entries in records.values() for reference in entries.values()}


def _read_object(directory, reference):
    try:
        content = _files.read_file(directory, reference + ".json")
        if hashlib.sha256(content).hexdigest() != reference:
            raise _corrupt()
        return json.loads(content)
    except (FileNotFoundError, ValueError, UnicodeError) as exc:
        raise _corrupt() from exc
    except OSError as exc:
        raise _unavailable() from exc


class _FileTransaction:
    def __init__(self, objects, records):
        self.objects, self.records = objects, records
        self.pending = {}
        self.cache = {}
        # All processes use the same host clock, sampled after acquiring the lock.
        self.now = time.time()
        self.active = True

    def _check(self, kind, key=""):
        if not self.active:
            raise InfrastructureError("STORE_TRANSACTION_CLOSED")
        return _key(kind), _key(key)

    def get(self, kind, key):
        return copy.deepcopy(self._value(kind, key))

    def _value(self, kind, key):
        pair = self._check(kind, key)
        if pair in self.pending:
            pending = self.pending[pair]
            return None if pending is _DELETED else json.loads(pending)
        reference = self.records.get(kind, {}).get(key)
        if reference is None:
            return None
        if reference not in self.cache:
            self.cache[reference] = _read_object(self.objects, reference)
        return self.cache[reference]

    def read_path(self, kind, key, path):
        value = self._value(kind, key)
        for part in path:
            if value is None:
                return None
            value = value.get(part)
        return copy.deepcopy(value)

    def put(self, kind, key, value):
        pair = self._check(kind, key)
        self.pending[pair] = canonical(value).encode("utf-8")

    def delete(self, kind, key):
        self.pending[self._check(kind, key)] = _DELETED

    def list(self, kind):
        self._check(kind)
        keys = set(self.records.get(kind, {}))
        for (pending_kind, key), value in self.pending.items():
            if pending_kind == kind:
                if value is _DELETED:
                    keys.discard(key)
                else:
                    keys.add(key)
        return [self.get(kind, key) for key in sorted(keys)]


class FileRuntimeStore(StoreQueries):
    """Durable local POSIX storage, with no database dependency.

    A namespace lock serializes cross-process transactions. Synced immutable
    records become visible together through one atomic manifest replacement.
    Keep this directory outside disposable workspaces. NFS/multi-host storage
    is not supported. RuntimeEngine does not close injected stores.
    """

    def __init__(self, path=".agentloom/state", *, lock_timeout_s=10.0):
        if (
            isinstance(lock_timeout_s, bool)
            or not isinstance(lock_timeout_s, (int, float))
            or not math.isfinite(lock_timeout_s)
            or lock_timeout_s < 0
        ):
            raise ConfigurationError("STORE_LOCK_TIMEOUT_INVALID")
        self.lock_timeout_s = float(lock_timeout_s)
        self._closed = False
        try:
            self.path = _files.prepare_root(path)
        except OSError as exc:
            raise _unavailable() from exc

    @contextmanager
    def _open(self, namespace):
        if self._closed:
            raise InfrastructureError("STORE_CLOSED")
        _key(namespace)
        with ExitStack() as stack:
            try:
                directory, objects = stack.enter_context(
                    _files.namespace_files(self.path, namespace, self.lock_timeout_s)
                )
                manifest = self._read_manifest(directory, objects, namespace)
            except OSError as exc:
                raise _unavailable() from exc
            yield directory, objects, manifest

    def _read_manifest(self, directory, objects, namespace):
        try:
            content = _files.read_file(directory, "manifest.json")
        except FileNotFoundError:
            # Initialization happens before any data writes. A missing manifest
            # alongside objects cannot be treated as a new, empty namespace.
            if os.listdir(objects):
                raise _corrupt()
            manifest = {
                "format": "agentloom.file-store",
                "version": 1,
                "namespace": namespace,
                "generation": 0,
                "records": {},
            }
            self._write_manifest(directory, manifest)
            return manifest
        try:
            envelope = _ENVELOPE.fullmatch(content)
            if envelope is None or hashlib.sha256(envelope[2]).hexdigest().encode() != envelope[1]:
                raise _corrupt()
            # The writer stores canonical JSON. Verify its exact bytes instead
            # of re-canonicalizing every reference on every status read.
            manifest = json.loads(envelope[2])
            if manifest["format"] != "agentloom.file-store":
                raise _corrupt()
            if type(manifest["version"]) is not int or manifest["version"] != 1:
                raise InfrastructureError("STORE_VERSION_UNSUPPORTED")
            if (
                manifest["namespace"] != namespace
                or type(manifest["generation"]) is not int
                or manifest["generation"] < 0
                or not isinstance(manifest["records"], dict)
            ):
                raise _corrupt()
            for kind, entries in manifest["records"].items():
                _key(kind)
                if not isinstance(entries, dict):
                    raise _corrupt()
                for key, reference in entries.items():
                    _key(key)
                    if not isinstance(reference, str) or not _HASH.fullmatch(reference):
                        raise _corrupt()
            return manifest
        except (ValueError, UnicodeError, KeyError, TypeError, ConfigurationError) as exc:
            raise _corrupt() from exc

    def _write_manifest(self, directory, manifest):
        content = canonical(manifest).encode("utf-8")
        checksum = hashlib.sha256(content).hexdigest().encode()
        envelope = b'{"checksum":"' + checksum + b'","manifest":' + content + b"}"
        _files.write_file(directory, "manifest.json", envelope)
        os.fsync(directory)

    def _commit(self, directory, objects, manifest, tx):
        if not tx.pending:
            return
        records = {kind: dict(entries) for kind, entries in manifest["records"].items()}
        for (kind, key), content in tx.pending.items():
            if content is _DELETED:
                records.get(kind, {}).pop(key, None)
                if kind in records and not records[kind]:
                    del records[kind]
                continue
            reference = hashlib.sha256(content).hexdigest()
            if records.get(kind, {}).get(key) != reference:
                _files.write_file(objects, reference + ".json", content, immutable=True)
                records.setdefault(kind, {})[key] = reference
            else:
                # An idempotent put must not acknowledge a missing/corrupt file.
                _read_object(objects, reference)
        if records == manifest["records"]:
            return
        os.fsync(objects)
        self._write_manifest(
            directory,
            {
                **manifest,
                "generation": manifest["generation"] + 1,
                "records": records,
            },
        )
        # No reader can retain the old manifest across this namespace lock.
        # Cleanup is optional; failures after commit must not undo its success.
        try:
            for reference in _references(manifest["records"]) - _references(records):
                try:
                    os.unlink(reference + ".json", dir_fd=objects)
                except FileNotFoundError:
                    pass
            os.fsync(objects)
        except OSError:
            pass

    @contextmanager
    def transaction(self, namespace):
        with self._open(namespace) as (directory, objects, manifest):
            tx = _FileTransaction(objects, manifest["records"])
            try:
                yield tx
                try:
                    self._commit(directory, objects, manifest, tx)
                except OSError as exc:
                    raise _unavailable() from exc
            finally:
                tx.active = False

    def collect_garbage(self, namespace="default"):
        """Remove orphan records/staging files under the namespace lock.

        Superseded records are normally reclaimed at commit. This sweep handles
        leftovers from interrupted writes; it never deletes committed records.
        Returns the number of removed files.
        """
        removed = 0
        with self._open(namespace) as (directory, objects, manifest):
            live = _references(manifest["records"])
            for reference in live:
                _read_object(objects, reference)
            try:
                for fd in (objects, directory):
                    for name in os.listdir(fd):
                        if _TEMP.fullmatch(name) or (
                            fd == objects and _OBJECT.fullmatch(name) and name[:-5] not in live
                        ):
                            os.unlink(name, dir_fd=fd)
                            removed += 1
                    os.fsync(fd)
            except OSError as exc:
                raise _unavailable() from exc
        return removed

    def read_path(self, namespace, run_id, path):
        with self.transaction(namespace) as tx:
            return tx.read_path("runs", str(run_id), path)

    def close(self):
        # Descriptors are scoped to each transaction; in-flight work can finish.
        self._closed = True
