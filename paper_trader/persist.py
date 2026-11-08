"""Single-writer ledger inside paper_trader/demo-state.

fcntl.flock plus an in-process set. A second writer is refused. The lock is
not stolen. Writes use a temp file in that same directory, then os.replace.
A symlink, a path outside demo-state, or a corrupt file fails closed.
--new never replaces an existing file. Resume replaces only the same regular
file it just loaded.
"""

import fcntl
import os
import tempfile
import threading

from paper_trader.engine import fingerprint_of, run_bars
from paper_trader.errors import (
    ConcurrentRunRefused,
    PaperTraderError,
    PathRefused,
    ReplayConflict,
    StateCorrupt,
    StateExists,
    StateMissing,
)
from paper_trader.jsonutil import parse, pretty
from paper_trader.paths import demo_dir, kind, read_text, state_path
from paper_trader.spec import ASSUMPTIONS, normalize_approval

_GUARD = threading.Lock()
_HELD = set()


def package(frozen):
    out = dict(frozen)
    out["fingerprint"] = fingerprint_of(frozen)
    return out


class StateStore(object):
    def __init__(self, path):
        self.path = state_path(path) if _is_relative_name(path) else _absolute_state(path)
        self._fh = None
        self._held = False
        self._locked = False

    def __enter__(self):
        directory = os.path.dirname(self.path)
        if kind(directory) != "dir":
            raise PathRefused("state directory is not a real directory")
        with _GUARD:
            if self.path in _HELD:
                raise ConcurrentRunRefused(
                    "this process already holds the writer lock for %s" % self.path
                )
            _HELD.add(self.path)
            self._held = True
        lock_path = self.path + ".lock"
        if kind(lock_path) == "symlink":
            self._release()
            raise PathRefused("lock path is a symlink")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags, 0o600)
            self._fh = os.fdopen(fd, "a+", encoding="utf-8")
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except PathRefused:
            self._release()
            raise
        except OSError:
            self._release()
            raise ConcurrentRunRefused(
                "another writer holds %s; this run will not simulate" % self.path
            )
        except Exception:
            self._release()
            raise
        self._locked = True
        return self

    def __exit__(self, exc_type, exc, tb):
        self._release()
        return False

    def _release(self):
        self._locked = False
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

    def _require_lock(self):
        if not self._locked:
            raise ConcurrentRunRefused("the state directory lock is not held")

    def create(self, inputs, approval):
        self._require_lock()
        seen = kind(self.path)
        if seen == "symlink":
            raise PathRefused("refusing to replace a symlink")
        if seen is not None:
            raise StateExists("state file already exists; resume it or choose a new name")
        packed = package(run_bars(inputs, approval))
        _atomic_write(self.path, pretty(packed), replace_existing=False)
        return packed

    def resume(self, inputs, approval):
        self._require_lock()
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
        _atomic_write(self.path, pretty(packed), replace_existing=True)
        return packed

    def load(self):
        self._require_lock()
        seen = kind(self.path)
        if seen == "symlink":
            raise PathRefused("refusing to read a symlink")
        if seen is None:
            raise StateMissing("no state file at %s" % self.path)
        if seen != "file":
            raise StateCorrupt("state path is not a regular file")
        text = read_text(self.path)
        if not text.strip():
            raise StateCorrupt("state file is empty")
        doc = parse(text, StateCorrupt)
        if not isinstance(doc, dict):
            raise StateCorrupt("state file is not an object")
        if doc.get("schema") != 1:
            raise StateCorrupt("state schema is not 1")
        if doc.get("architecture") != "simulation":
            raise StateCorrupt("state is not a simulation ledger")
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


def _is_relative_name(path):
    return isinstance(path, str) and not os.path.isabs(path)


def _absolute_state(path):
    if not isinstance(path, str) or "\x00" in path or ".." in path.split("/") or ".." in path.split(os.sep):
        raise PathRefused("path traversal is refused")
    demo = os.path.abspath(demo_dir())
    absolute = os.path.abspath(path)
    if os.path.commonpath([demo, absolute]) != demo or absolute == demo:
        raise PathRefused("state file must stay inside demo-state")
    relative = os.path.relpath(absolute, demo)
    return state_path(relative.replace(os.sep, "/"))


def _atomic_write(path, text, replace_existing):
    seen = kind(path)
    if seen == "symlink":
        raise PathRefused("refusing to replace a symlink")
    if seen not in (None, "file"):
        raise PathRefused("state path is not a regular file")
    if seen == "file" and not replace_existing:
        raise StateExists("state file already exists")
    if seen is None and replace_existing:
        raise StateMissing("state file disappeared before replace")
    directory = os.path.dirname(path)
    if kind(directory) != "dir":
        raise PathRefused("state directory is not a real directory")
    fd, tmp = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if kind(path) == "symlink":
            raise PathRefused("refusing to replace a symlink")
        os.replace(tmp, path)
        dirfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
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
