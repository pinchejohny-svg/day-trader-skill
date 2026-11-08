"""Confine demo files to this package. No symlink following and no `..`.

State files live only under `paper_trader/demo-state`. Shipped fixtures are
read only from `paper_trader/fixtures`. A path that leaves those directories,
or that touches a symlink, is refused before any open or replace.
"""

import os
import re
import stat

from paper_trader.errors import DataError, PathRefused

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")


def package_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.islink(__file__) or os.path.islink(here):
        raise PathRefused("the paper_trader directory must not be reached through a symlink")
    if not os.path.isdir(here):
        raise PathRefused("the paper_trader directory is missing")
    return here


def demo_dir():
    root = package_dir()
    demo = os.path.join(root, "demo-state")
    if os.path.islink(demo):
        raise PathRefused("demo-state is a symlink")
    if not os.path.exists(demo):
        os.mkdir(demo, 0o700)
    if not os.path.isdir(demo) or os.path.islink(demo):
        raise PathRefused("demo-state is not a real directory")
    return demo


def _parts(relative):
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        raise PathRefused("empty path")
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
    if os.path.islink(current) or not os.path.isdir(current):
        raise PathRefused("root is not a real directory")
    for part in parts:
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise PathRefused("symlink is refused")
        if os.path.commonpath([os.path.abspath(root), os.path.abspath(current)]) != os.path.abspath(root):
            raise PathRefused("path escapes the demo directory")
    return os.path.abspath(current)


def demo_subdir(name):
    parts = _parts(name)
    if len(parts) != 1:
        raise PathRefused("demo subdirectory must be a single name")
    demo = demo_dir()
    path = _walk(demo, parts)
    if os.path.lexists(path):
        if os.path.islink(path) or not os.path.isdir(path):
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
            if os.path.islink(parent) or not os.path.isdir(parent):
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
    if not isinstance(path, str) or "\x00" in path or "\\" in path or ".." in path.split("/"):
        raise PathRefused("path traversal is refused")
    absolute = os.path.abspath(path)
    root_abs = os.path.abspath(root)
    if os.path.commonpath([root_abs, absolute]) != root_abs or absolute == root_abs:
        raise PathRefused("file is outside %s" % root_name)
    relative = os.path.relpath(absolute, root_abs)
    leaf = _walk(root_abs, relative.split(os.sep))
    if os.path.islink(leaf) or not os.path.isfile(leaf):
        raise DataError("file is missing or not a regular file")
    return leaf


def read_text(path):
    """Read a regular file. A symlink at the final component fails closed."""
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
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if stat.S_ISREG(info.st_mode):
        return "file"
    if stat.S_ISDIR(info.st_mode):
        return "dir"
    return "other"
