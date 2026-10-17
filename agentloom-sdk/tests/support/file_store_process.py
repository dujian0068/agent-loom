"""Exercise real file publication and locks across process boundaries."""

import os
import stat
import sys
import time
from pathlib import Path

from agentloom import FileRuntimeStore
from agentloom.storage import _files

root, mode = Path(sys.argv[1]), sys.argv[2]
store = FileRuntimeStore(root)
if mode == "increment":
    for _ in range(12):
        with store.transaction("default") as tx:
            value = tx.get("runs", "counter")["value"] + 1
            time.sleep(0.002)
            tx.put("runs", "counter", {"value": value})
            tx.put("commits", str(value), {"value": value})
elif mode == "lock":
    with store.transaction("default"):
        print("locked", flush=True)
        sys.stdin.readline()
else:
    replace = _files.os.replace
    sync = _files.os.fsync

    def interrupt_sync(fd):
        if mode == "record_sync" and stat.S_ISREG(os.fstat(fd).st_mode):
            os._exit(73)
        sync(fd)

    def interrupt(source, target, **kwargs):
        if target == "manifest.json" and mode == "before_publish":
            os._exit(73)
        replace(source, target, **kwargs)
        if target == "manifest.json" and mode == "after_publish":
            os._exit(73)

    _files.os.replace = interrupt
    _files.os.fsync = interrupt_sync
    with store.transaction("default") as tx:
        tx.put("runs", "counter", {"value": 1})
        tx.put("commits", "increment", {"value": 1})
    os._exit(73)
