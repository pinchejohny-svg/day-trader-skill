"""Static checks. They read source text and do not import the skill scripts."""

import ast
import os
import sys
import unittest

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import paper_trader.tests.support as support
from paper_trader.forward import BLOCKER, FORWARD_IMPLEMENTED

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
                name for name in dirnames if name not in (".tmp-tests", "demo-state", "__pycache__")
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
                in_tests = os.sep + "tests" + os.sep in path
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and node.attr in ("now", "urlopen", "urlretrieve"):
                        self.fail("%s calls .%s" % (path, node.attr))
                    if isinstance(node, ast.Name) and node.id in ("urlopen", "sleep", "eval", "exec"):
                        self.fail("%s references %s" % (path, node.id))
                    if not in_tests and isinstance(node, ast.Attribute) and node.attr == "environ":
                        self.fail("%s reads the environment" % path)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                        "eval",
                        "exec",
                        "compile",
                        "__import__",
                    ):
                        self.fail("%s calls %s" % (path, node.func.id))

    def test_forward_module_has_no_callable_feed(self):
        self.assertFalse(FORWARD_IMPLEMENTED)
        self.assertIn("no broker pathway", BLOCKER)
        source = open(os.path.join(support.PACKAGE, "forward.py"), "r", encoding="utf-8").read()
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in ast.walk(tree)))

    def test_bytecode_flag_is_on(self):
        self.assertTrue(sys_dont_write())


def sys_dont_write():
    import sys

    return sys.dont_write_bytecode is True or os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"


if __name__ == "__main__":
    unittest.main()
