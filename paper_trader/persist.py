"""Single-writer ledger. Atomic replace, fail closed on a bad file.

The lock is fcntl.flock plus an in-process set. A second process or a second
store in this process is refused. The lock is not stolen from a live holder.
A crash releases the flock. A leftover lock file is not a license to trade.

Temp files are created in the state file's directory, not a shared temp root.
"""

import fcntl
import os
import tempfile
import threading

from paper_trader.engine import fingerprint_of, run_bars
from paper_trader.errors import (
    ConcurrentRunRefused,
    PaperTraderError,
    ReplayConflict,
    StateCorrupt,
    StateExists,
    StateMissing,
)
from paper_trader.jsonutil import parse, pretty
from paper_trader.spec import ASSUMPTIONS, normalize_approval

_GUARD = threading.Lock()
_HELD = set()


def package(frozen):
    out = dict(frozen)
    out["fingerprint"] = fingerprint_of(frozen)
    return out


class StateStore(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)
        self._fh = None
        self._held = False

    def __enter__(self):
        directory = os.path.dirname(self.path)
        os.makedirs(directory, exist_ok=True)
        with _GUARD:
            if self.path in _HELD:
                raise ConcurrentRunRefused(
                    "this process already holds the writer lock for %s" % self.path
                )
            _HELD.add(self.path)
            self._held = True
        try:
            self._fh = open(self.path + ".lock", "a+", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._release()
            raise ConcurrentRunRefused(
                "another writer holds %s; this run will not trade" % self.path
            )
        except Exception:
            self._release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        self._release()
        return False

    def _release(self):
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        if self._held:
            with _GUARD:
                _HELD.discard(self.path)
            self._held = False

    def create(self, inputs, approval):
        if os.path.exists(self.path):
            raise StateExists(
                "state file already exists; resume it or choose a new path"
            )
        packed = package(run_bars(inputs, approval))
        _atomic_write(self.path, pretty(packed))
        return packed

    def resume(self, inputs, approval):
        old = self.load()
        approval = normalize_approval(approval)
        if old["approval"] != approval:
            raise ReplayConflict("approval does not match the stored run")
        if inputs[: len(old["inputs"])] != old["inputs"]:
            raise ReplayConflict("fixture prefix does not match stored inputs")
        suffix = inputs[len(old["inputs"]) :]
        if suffix and old["inputs"]:
            watermark = max(
                bar["start"]
                for bar in old["inputs"]
                if isinstance(bar, dict) and isinstance(bar.get("start"), str)
            )
            for bar in suffix:
                if not isinstance(bar, dict) or not isinstance(bar.get("start"), str):
                    raise ReplayConflict("suffix bar is missing a start")
                if bar["start"] <= watermark:
                    raise ReplayConflict(
                        "suffix bar %s is not strictly after the stored tape" % bar["start"]
                    )
        packed = package(run_bars(inputs, approval))
        _atomic_write(self.path, pretty(packed))
        return packed

    def load(self):
        if not os.path.exists(self.path):
            raise StateMissing("no state file at %s" % self.path)
        with open(self.path, "r", encoding="utf-8") as handle:
            text = handle.read()
        if not text.strip():
            raise StateCorrupt("state file is empty")
        doc = parse(text, StateCorrupt)
        if not isinstance(doc, dict):
            raise StateCorrupt("state file is not an object")
        if doc.get("schema") != 1:
            raise StateCorrupt("state schema is not 1")
        if doc.get("assumptions") != ASSUMPTIONS:
            raise StateCorrupt("state assumptions do not match this engine")
        if "inputs" not in doc or "fingerprint" not in doc:
            raise StateCorrupt("state is missing inputs or fingerprint")
        try:
            normalize_approval(doc.get("approval"))
            recomputed = package(run_bars(doc["inputs"], doc["approval"]))
        except StateCorrupt:
            raise
        except PaperTraderError as exc:
            raise StateCorrupt("state inputs cannot be replayed: %s" % exc)
        if recomputed != doc:
            raise StateCorrupt("state does not match a replay of its inputs")
        if doc.get("fingerprint") != fingerprint_of(
            {key: value for key, value in doc.items() if key != "fingerprint"}
        ):
            raise StateCorrupt("state fingerprint does not match its body")
        return doc


def _atomic_write(path, text):
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        dirfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
