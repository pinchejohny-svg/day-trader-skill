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
    with open(path, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
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
                base = os.path.basename(path)
                for module in modules:
                    if module == "urllib" and base == "massive.py":
                        continue
                    self.assertNotIn(module, _BANNED_MODULES, path)
                    self.assertFalse(module.startswith("trader"), path)
                if base not in ("persist.py", "test_persist.py"):
                    self.assertNotIn("threading", modules, path)
                if base != "persist.py":
                    self.assertNotIn("fcntl", modules, path)
                    self.assertNotIn("msvcrt", modules, path)
                in_tests = os.sep + "tests" + os.sep in path
                for node in ast.walk(tree):
                    if isinstance(node, ast.Attribute) and node.attr in ("now", "urlopen", "urlretrieve"):
                        self.fail("%s calls .%s" % (path, node.attr))
                    if isinstance(node, ast.Name) and node.id in ("urlopen", "sleep", "eval", "exec"):
                        self.fail("%s references %s" % (path, node.id))
                    if (
                        not in_tests
                        and isinstance(node, ast.Attribute)
                        and node.attr == "environ"
                        and base != "massive.py"
                    ):
                        self.fail("%s reads the environment" % path)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in (
                        "eval",
                        "exec",
                        "compile",
                        "__import__",
                    ):
                        self.fail("%s calls %s" % (path, node.func.id))

    def test_platform_locks_are_imported_only_while_locking(self):
        path = os.path.join(support.PACKAGE, "persist.py")
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            self.assertFalse(set(names) & {"fcntl", "msvcrt"}, path)

    def test_massive_client_stays_inside_its_helpers(self):
        path = os.path.join(support.PACKAGE, "massive.py")
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        allowed = {"redact", "validate_next_url", "https_get", "_redirect_handler"}
        for node in ast.walk(tree):
            imported = _imported_roots(node)
            if "urllib" in imported or "socket" in imported:
                self.assertIn(_enclosing_function(tree, node), allowed, path)
            if isinstance(node, ast.Attribute) and node.attr == "environ":
                self.assertEqual(_enclosing_function(tree, node), "resolve_api_key", path)
        for node in tree.body:
            self.assertFalse(set(_imported_roots(node)) & {"urllib", "socket"}, path)

    def test_forward_module_has_no_callable_feed(self):
        self.assertFalse(FORWARD_IMPLEMENTED)
        self.assertIn("no broker pathway", BLOCKER)
        with open(os.path.join(support.PACKAGE, "forward.py"), "r", encoding="utf-8") as handle:
            source = handle.read()
        tree = ast.parse(source)
        self.assertFalse(any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) for node in ast.walk(tree)))

    def test_bytecode_flag_is_on(self):
        self.assertTrue(sys_dont_write())


def _imported_roots(node):
    if isinstance(node, ast.Import):
        return [alias.name.split(".")[0] for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module.split(".")[0]]
    return []


def _enclosing_function(tree, target):
    found = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if child is target:
                    found = node.name
    return found


def sys_dont_write():
    import sys

    return sys.dont_write_bytecode is True or os.environ.get("PYTHONDONTWRITEBYTECODE") == "1"


if __name__ == "__main__":
    unittest.main()
