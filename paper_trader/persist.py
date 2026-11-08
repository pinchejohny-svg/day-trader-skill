"""Single-writer ledger inside paper_trader/demo-state.

The lock is an in-process set plus one OS lock on the first byte of the
lock file. POSIX uses `fcntl.flock` with `LOCK_EX | LOCK_NB`, imported only
when taking the lock. Windows uses `msvcrt.locking` with `LK_NBLCK` for one
byte and `LK_UNLCK` to release, which is the stdlib non-blocking lock. A
second writer is refused. The lock is not stolen.

Writes use a temp file in that same directory, `os.fsync` (`_commit` on
Windows), then `os.replace`. POSIX also fsyncs the directory. Windows has no
stdlib directory fsync, so the replaced file is opened and fsynced again.
That commits file bytes, not the directory entry. A reparse point, a hard
link, a path outside demo-state, or a corrupt file fails closed. `--new`
never replaces an existing file. Resume replaces only the same regular file
it just loaded.
"""

import os
import sys
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
from paper_trader.paths import demo_dir, kind, read_text, state_path, windows_lexical_hazard
from paper_trader.spec import ASSUMPTIONS, normalize_approval

_GUARD = threading.Lock()
_HELD = set()
_UNSAFE = ("symlink", "reparse", "hardlink")


def _lock_fd(fd):
    """Non-blocking exclusive lock. Imports the platform module only here."""
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd):
    if sys.platform == "win32":
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


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
        seen = kind(self.path)
        if seen in _UNSAFE:
            raise PathRefused("state path is a %s" % seen)
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
        if kind(lock_path) in _UNSAFE:
            self._release()
            raise PathRefused("lock path is not a regular file")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(lock_path, flags, 0o600)
            self._fh = os.fdopen(fd, "a+", encoding="utf-8")
            _lock_fd(self._fh.fileno())
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
                _unlock_fd(self._fh.fileno())
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
        if seen in _UNSAFE:
            raise PathRefused("refusing to replace a %s" % seen)
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
        if seen in _UNSAFE:
            raise PathRefused("refusing to read a %s" % seen)
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
    if not isinstance(path, str) or "\x00" in path or ".." in path.split("/") or ".." in path.split("\\"):
        raise PathRefused("path traversal is refused")
    hazard = windows_lexical_hazard(path)
    if hazard is not None:
        raise PathRefused(hazard)
    demo = os.path.abspath(demo_dir())
    absolute = os.path.abspath(path)
    root_n = os.path.normcase(demo)
    leaf_n = os.path.normcase(absolute)
    try:
        contained = os.path.commonpath([root_n, leaf_n]) == root_n and leaf_n != root_n
    except ValueError:
        contained = False
    if not contained:
        raise PathRefused("state file must stay inside demo-state")
    relative = os.path.relpath(absolute, demo)
    return state_path(relative.replace("\\", "/"))


def _sync_after_replace(directory, path):
    """Commit the new file. Directory fsync exists only on POSIX.

    Windows `os.fsync` is the C `_commit` of a file descriptor. `_commit`
    fails with EBADF on a read-only descriptor, so the replaced file is
    opened `O_RDWR` and no bytes are written. `O_BINARY` is added when the
    interpreter defines it. There is no stdlib call that fsyncs a directory
    on Windows. A crash can still lose the directory entry even though the
    file bytes were committed.
    """
    if sys.platform == "win32":
        if kind(path) != "file":
            raise PathRefused("refusing to flush a non-regular file")
        flags = os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(path, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return
    dirfd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def _atomic_write(path, text, replace_existing):
    seen = kind(path)
    if seen in _UNSAFE:
        raise PathRefused("refusing to replace a %s" % seen)
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
    replaced = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if kind(path) in _UNSAFE:
            raise PathRefused("refusing to replace a reparse point or hard link")
        os.replace(tmp, path)
        replaced = True
        _sync_after_replace(directory, path)
    except Exception:
        if not replaced:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
