"""Static checks. They read source text and do not import the skill scripts."""

import ast
import os
import sys
import unittest

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import paper_trader.tests.support as support
from paper_trader.errors import ForwardDisabled
from paper_trader.forward import ForwardPaperAdapter

_BANNED_MODULES = {
    "alpaca",
    "asyncio",
    "http",
    "importlib",
    "requests",
    "sched",
    "socket",
    "subprocess",
    "urllib",
    "yfinance",
}


def _modules(path):
    tree = ast.parse(open(path, "r", encoding="utf-8").read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module.split(".")[0])
    return found, tree


class StaticBoundaries(unittest.TestCase):
    def test_package_has_no_network_or_skill_imports(self):
        for dirpath, dirnames, filenames in os.walk(support.PACKAGE):
            dirnames[:] = [
                name for name in dirnames if name not in (".tmp-tests", "tests", "__pycache__")
            ]
            for name in filenames:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                modules, tree = _modules(path)
                for module in modules:
                    self.assertNotIn(module, _BANNED_MODULES, path)
                    self.assertFalse(module.startswith("trader"), path)
                if os.path.basename(path) != "persist.py":
                    self.assertNotIn("threading", modules, path)
                    self.assertNotIn("fcntl", modules, path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and node.attr in ("now", "urlopen", "urlretrieve"):
                        self.fail("%s calls .%s" % (path, node.attr))
                    if isinstance(node, ast.Name) and node.id in ("urlopen", "sleep"):
                        self.fail("%s references %s" % (path, node.id))

    def test_forward_adapter_does_not_poll(self):
        adapter = ForwardPaperAdapter()
        with self.assertRaises(ForwardDisabled):
            adapter.poll()
        with self.assertRaises(ForwardDisabled):
            adapter.stream()

    def test_bytecode_flag_is_on(self):
        self.assertTrue(sys_dont_write())


def sys_dont_write():
    import sys

    return sys.dont_write_bytecode is True or os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"


if __name__ == "__main__":
    unittest.main()
