"""Path and lock rules that differ by platform. Not executed during authoring."""

import ast
import os
import sys
import unittest

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import paper_trader.tests.support as support
from paper_trader.errors import PathRefused
from paper_trader.paths import windows_lexical_hazard
from paper_trader.persist import StateStore


class WindowsLexicalRules(unittest.TestCase):
    def test_ordinary_relative_name_is_allowed(self):
        self.assertIsNone(windows_lexical_hazard("resume/state.json"))
        self.assertIsNone(windows_lexical_hazard("synthetic-round-trip.json"))

    def test_unc_device_drive_ads_and_reserved_names_are_refused(self):
        refused = [
            r"\\server\share\demo-state\state.json",
            "//server/share/state.json",
            r"\\.\C:\demo-state\state.json",
            r"\\?\C:\demo-state\state.json",
            r"C:demo-state\state.json",
            r"demo-state\state.json:secret",
            r"C:\demo-state\state.json:stream",
            r"demo-state\NUL",
            r"demo-state\CON.json",
            r"demo-state\COM1.txt",
            r"demo-state\state.json.",
            r"demo-state\state.json ",
            "demo-state/../outside.json",
        ]
        for path in refused:
            self.assertIsNotNone(windows_lexical_hazard(path), path)

    def test_store_refuses_those_names_before_opening_them(self):
        samples = [
            r"\\server\share\state.json",
            "C:state.json",
            "state.json:secret",
            "NUL.json",
            "CON.json",
        ]
        for path in samples:
            with self.assertRaises(PathRefused):
                StateStore(path)

    def test_windows_post_replace_flush_opens_writable(self):
        """The Windows branch must not fsync a read-only descriptor.

        CPython maps os.fsync to the CRT _commit on Windows, and _commit
        fails with EBADF when the handle was opened only for reading. This
        inspects that branch; it does not mock the call.
        """
        path = os.path.join(support.PACKAGE, "persist.py")
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        body = None
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_sync_after_replace":
                for stmt in node.body:
                    test = stmt.test if isinstance(stmt, ast.If) else None
                    if (
                        isinstance(test, ast.Compare)
                        and isinstance(test.left, ast.Attribute)
                        and test.left.attr == "platform"
                        and test.comparators
                        and isinstance(test.comparators[0], ast.Constant)
                        and test.comparators[0].value == "win32"
                    ):
                        body = stmt.body
        self.assertIsNotNone(body)
        names = [node.attr for node in ast.walk(ast.Module(body=body, type_ignores=[])) if isinstance(node, ast.Attribute)]
        self.assertIn("O_RDWR", names)
        self.assertIn("fsync", names)
        self.assertNotIn("O_RDONLY", names)

    def test_junction_api_is_stdlib_and_this_package_is_not_one(self):
        self.assertTrue(callable(os.path.isjunction))
        self.assertFalse(os.path.isjunction(support.PACKAGE))
        self.assertFalse(os.path.isjunction(os.path.join(support.PACKAGE, "persist.py")))

    def test_windows_directory_symlink_is_a_reparse_point(self):
        if sys.platform != "win32":
            self.skipTest("creating a directory symlink is the Windows reparse check")
        directory = support.workdir("reparse")
        link = os.path.join(directory, "joined")
        os.symlink(directory, link, target_is_directory=True)
        self.assertTrue(os.path.islink(link) or os.path.isjunction(link))
        with self.assertRaises(PathRefused):
            StateStore(os.path.join(link, "state.json"))


if __name__ == "__main__":
    unittest.main()
