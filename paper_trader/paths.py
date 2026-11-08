"""Confine demo files to this package.

State files live only under `paper_trader/demo-state`. Shipped fixtures are
read only from `paper_trader/fixtures`. Symlinks, junctions, other reparse
points, hard links, `..`, UNC paths, device paths, drive-relative paths,
reserved DOS device names, trailing dots or spaces, and alternate data
streams are refused before any open or replace.

`os.path.isjunction` is the stdlib junction test (false on POSIX). On
Windows, `os.lstat` also exposes `st_file_attributes` and `st_reparse_tag`;
any `FILE_ATTRIBUTE_REPARSE_POINT` is refused. Python's `os.O_NOFOLLOW` is
not part of the Windows stdlib surface, so a name swapped for a reparse
point between the check and `os.open` is still a race. The check is repeated
immediately before each open.
"""

import ntpath
import os
import re
import stat

from paper_trader.errors import DataError, PathRefused

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
_DEVICES = {"CON", "PRN", "AUX", "NUL"}
for _n in range(1, 10):
    _DEVICES.add("COM%d" % _n)
    _DEVICES.add("LPT%d" % _n)


def windows_lexical_hazard(path):
    """Return a reason to refuse a path, using Windows lexical rules.

    `ntpath.splitroot` is stdlib and does not touch the filesystem, so these
    checks are the same on Linux and Windows. A normal relative name such as
    `resume/state.json` is not a hazard.
    """
    if not isinstance(path, str):
        return "not a string"
    if "\x00" in path:
        return "NUL byte"
    drive, root, tail = ntpath.splitroot(path)
    folded = drive.replace("/", "\\").upper()
    if folded.startswith("\\\\?\\") or folded.startswith("\\\\.\\"):
        return "extended or device path"
    if folded.startswith("\\\\"):
        return "UNC path"
    if drive and not root:
        return "drive-relative path"
    if ":" in root or ":" in tail:
        return "alternate data stream"
    for part in tail.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return "traversal"
        if part.endswith(" ") or part.endswith(".") or part != part.strip():
            return "trailing dot or space"
        stem = part.split(".", 1)[0].upper()
        if stem in _DEVICES:
            return "reserved device name"
    return None


def _refuse_text(path):
    reason = windows_lexical_hazard(path)
    if reason is not None:
        raise PathRefused(reason)


def _is_reparse(path):
    """True for a symlink, a junction, or any other reparse point."""
    if os.path.islink(path) or os.path.isjunction(path):
        return True
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0) or 0
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if flag and attributes & flag:
        return True
    tag = getattr(info, "st_reparse_tag", None)
    mount = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    link = getattr(stat, "IO_REPARSE_TAG_SYMLINK", None)
    return bool(tag) and tag in (mount, link)


def _inside(root, leaf):
    root_n = os.path.normcase(os.path.abspath(root))
    leaf_n = os.path.normcase(os.path.abspath(leaf))
    if leaf_n == root_n:
        return False
    try:
        return os.path.commonpath([root_n, leaf_n]) == root_n
    except ValueError:
        return False


def package_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    if _is_reparse(__file__) or _is_reparse(here):
        raise PathRefused("the paper_trader directory must not be reached through a reparse point")
    if not os.path.isdir(here):
        raise PathRefused("the paper_trader directory is missing")
    return here


def demo_dir():
    root = package_dir()
    demo = os.path.join(root, "demo-state")
    if os.path.lexists(demo) and _is_reparse(demo):
        raise PathRefused("demo-state is a reparse point")
    if not os.path.exists(demo):
        os.mkdir(demo, 0o700)
    if not os.path.isdir(demo) or _is_reparse(demo):
        raise PathRefused("demo-state is not a real directory")
    return demo


def _parts(relative):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise PathRefused("empty path")
    _refuse_text(relative)
    if os.path.isabs(relative) or relative.startswith("~") or "\\" in relative:
        raise PathRefused("path must stay inside the demo directory")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise PathRefused("path traversal is refused")
    for part in parts:
        if _SEGMENT.fullmatch(part) is None:
            raise PathRefused("unsafe path segment")
    return parts


def _walk(root, parts):
    current = os.path.abspath(root)
    if _is_reparse(current) or not os.path.isdir(current):
        raise PathRefused("root is not a real directory")
    for part in parts:
        current = os.path.join(current, part)
        if os.path.lexists(current) and _is_reparse(current):
            raise PathRefused("reparse point is refused")
        if not _inside(root, current):
            raise PathRefused("path escapes the demo directory")
    return os.path.abspath(current)


def demo_subdir(name):
    parts = _parts(name)
    if len(parts) != 1:
        raise PathRefused("demo subdirectory must be a single name")
    demo = demo_dir()
    path = _walk(demo, parts)
    if os.path.lexists(path):
        if _is_reparse(path) or not os.path.isdir(path):
            raise PathRefused("demo subdirectory is not a real directory")
    else:
        os.mkdir(path, 0o700)
    return path


def state_path(relative):
    """A file inside demo-state, at most one directory down. Creates that directory."""
    parts = _parts(relative)
    if len(parts) > 2 or not parts[-1].endswith(".json"):
        raise PathRefused("state file must be a .json name inside demo-state")
    demo = demo_dir()
    if len(parts) == 2:
        parent = _walk(demo, parts[:1])
        if os.path.lexists(parent):
            if _is_reparse(parent) or not os.path.isdir(parent):
                raise PathRefused("state directory is not real")
        else:
            os.mkdir(parent, 0o700)
    return _walk(demo, parts)


def confine_existing(path, root_name):
    """An existing regular file under fixtures/ or demo-state/."""
    if root_name not in ("fixtures", "demo-state"):
        raise PathRefused("unknown read root")
    root = os.path.join(package_dir(), root_name)
    if root_name == "demo-state":
        root = demo_dir()
    _refuse_text(path)
    if ".." in path.split("/") or ".." in path.split("\\"):
        raise PathRefused("path traversal is refused")
    absolute = os.path.abspath(path)
    root_abs = os.path.abspath(root)
    if not _inside(root_abs, absolute):
        raise PathRefused("file is outside %s" % root_name)
    relative = os.path.relpath(absolute, root_abs)
    leaf = _walk(root_abs, relative.split(os.sep))
    if _is_reparse(leaf) or kind(leaf) != "file":
        raise DataError("file is missing or not a regular file")
    return leaf


def read_text(path):
    """Read a regular file. A reparse point at the final component fails closed.

    `os.O_NOFOLLOW` is applied when the interpreter provides it. Python does
    not document that flag for Windows, so the Windows defense is the lstat
    reparse check immediately before open, not `O_NOFOLLOW`.
    """
    if _is_reparse(path) or kind(path) != "file":
        raise PathRefused("refusing to read a non-regular path")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError:
        raise PathRefused("refusing to open a non-regular path")
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise PathRefused("refusing to read a non-regular file")
        chunks = []
        while True:
            block = os.read(fd, 65536)
            if not block:
                break
            chunks.append(block)
    finally:
        os.close(fd)
    return b"".join(chunks).decode("utf-8")


def kind(path):
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if _is_reparse(path):
        return "reparse"
    if stat.S_ISREG(info.st_mode):
        if info.st_nlink > 1:
            return "hardlink"
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    return "other"
