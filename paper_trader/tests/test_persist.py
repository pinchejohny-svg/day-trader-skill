"""Ledger durability. Not executed during authoring."""

import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import paper_trader.tests.support as support
from paper_trader.engine import run_bars
from paper_trader.errors import (
    ConcurrentRunRefused,
    ReplayConflict,
    StateCorrupt,
    StateExists,
)
from paper_trader.feed import load_fixture
from paper_trader.jsonutil import parse
from paper_trader.persist import StateStore, package
from paper_trader.report import build_report
from paper_trader.spec import STRATEGY_ID


def _bars():
    return load_fixture(support.fixture("synthetic_round_trip.json"))["bars"]


def _approval():
    return support.approval(["SYN"], ["2026-11-02"])


class Persistence(unittest.TestCase):
    def test_resume_replays_without_a_second_fill(self):
        directory = support.workdir("resume")
        path = os.path.join(directory, "state.json")
        bars = _bars()
        with StateStore(path) as store:
            first = store.create(bars, _approval())
        with StateStore(path) as store:
            second = store.resume(bars, _approval())
        self.assertEqual(first, second)
        self.assertEqual(len(support.of_type(second, "entry")), 1)
        self.assertEqual(len(support.of_type(second, "exit")), 1)
        self.assertEqual(second["cash"], "10096.200")
        again = package(run_bars(bars, _approval()))
        self.assertEqual(again, second)

    def test_resume_rejects_an_earlier_bar(self):
        directory = support.workdir("earlier")
        path = os.path.join(directory, "state.json")
        bars = _bars()
        with StateStore(path) as store:
            store.create(bars, _approval())
        before = open(path, "r", encoding="utf-8").read()
        extra = support.bar("SYN", "2026-11-02T09:30:00", "100.00", "102.00", "99.00", "101.00")
        with StateStore(path) as store:
            with self.assertRaises(ReplayConflict):
                store.resume(bars + [extra], _approval())
        self.assertEqual(open(path, "r", encoding="utf-8").read(), before)

    def test_truncated_and_empty_files_fail_closed(self):
        directory = support.workdir("truncated")
        path = os.path.join(directory, "state.json")
        for text in ("{", "", "   "):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
            with StateStore(path) as store:
                with self.assertRaises(StateCorrupt):
                    store.load()
            self.assertEqual(open(path, "r", encoding="utf-8").read(), text)
            with StateStore(path) as store:
                with self.assertRaises(StateExists):
                    store.create(_bars(), _approval())
            self.assertEqual(open(path, "r", encoding="utf-8").read(), text)

    def test_edited_cash_and_assumptions_fail_closed(self):
        directory = support.workdir("edited")
        path = os.path.join(directory, "state.json")
        with StateStore(path) as store:
            store.create([], support.approval(["NODATA"], ["2026-11-02"]))
        original = open(path, "r", encoding="utf-8").read()
        edited = original.replace('"cash": "10000.000"', '"cash": "1.000"', 1)
        self.assertNotEqual(edited, original)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(edited)
        with StateStore(path) as store:
            with self.assertRaises(StateCorrupt):
                store.load()
        self.assertEqual(open(path, "r", encoding="utf-8").read(), edited)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(original)
        tweaked = original.replace(
            '"starting_cash": "10000.000"', '"starting_cash": "10000.001"', 1
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(tweaked)
        with StateStore(path) as store:
            with self.assertRaises(StateCorrupt):
                store.load()
        self.assertEqual(open(path, "r", encoding="utf-8").read(), tweaked)

    def test_duplicate_json_key_is_rejected(self):
        directory = support.workdir("duplicate-key")
        path = os.path.join(directory, "state.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"schema": 1, "schema": 1}\n')
        with StateStore(path) as store:
            with self.assertRaises(StateCorrupt):
                store.load()

    def test_stray_temp_file_is_not_the_ledger(self):
        directory = support.workdir("stray-tmp")
        path = os.path.join(directory, "state.json")
        with StateStore(path) as store:
            created = store.create(_bars(), _approval())
        with open(path + ".tmp", "w", encoding="utf-8") as handle:
            handle.write('{"cash": "1.000"}\n')
        with StateStore(path) as store:
            loaded = store.load()
        self.assertEqual(loaded["cash"], created["cash"])
        self.assertEqual(loaded["cash"], "10096.200")

    def test_second_writer_in_this_process_is_refused(self):
        directory = support.workdir("lock-thread")
        path = os.path.join(directory, "state.json")
        with StateStore(path) as _store:
            with self.assertRaises(ConcurrentRunRefused):
                with StateStore(path):
                    self.fail("second writer entered")

    def test_second_process_is_refused(self):
        directory = support.workdir("lock-process")
        path = os.path.join(directory, "state.json")
        code = (
            "import os, sys\n"
            "os.environ['PYTHONDONTWRITEBYTECODE'] = '1'\n"
            "sys.dont_write_bytecode = True\n"
            "sys.path.insert(0, %r)\n"
            "from paper_trader.errors import ConcurrentRunRefused\n"
            "from paper_trader.persist import StateStore\n"
            "try:\n"
            "    with StateStore(%r):\n"
            "        raise SystemExit(2)\n"
            "except ConcurrentRunRefused:\n"
            "    raise SystemExit(0)\n"
        ) % (support.REPO, path)
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        with StateStore(path) as _store:
            proc = subprocess.run(
                [sys.executable, "-B", "-c", code],
                env=env,
                cwd=support.REPO,
                capture_output=True,
                text=True,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_cli_writes_only_the_requested_state(self):
        from paper_trader.__main__ import run

        directory = support.workdir("cli")
        path = os.path.join(directory, "state.json")
        before = _outside_files()
        out = io.StringIO()
        err = io.StringIO()
        argv = [
            "--new",
            "--fixture",
            support.fixture("synthetic_round_trip.json"),
            "--state",
            path,
            "--label",
            "synthetic",
            "--sessions",
            "2026-11-02",
            "--symbols",
            "SYN",
            "--approve",
            STRATEGY_ID,
            "--acknowledge-simulated",
        ]
        with redirect_stdout(out), redirect_stderr(err):
            code = run(argv)
        self.assertEqual(code, 0, err.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual(report["cash"], "10096.200")
        self.assertFalse(report["profitability_claim"])
        self.assertTrue(os.path.commonpath([os.path.abspath(path), support.PACKAGE]) == os.path.abspath(support.PACKAGE))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(run(argv), 3)
        argv[0] = "--resume"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(run(argv), 0)
        refused = [arg for arg in argv if arg != "--acknowledge-simulated"]
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(run(refused), 2)
        self.assertFalse(os.path.exists(os.path.join(directory, "nope.json")))
        self.assertEqual(_outside_files(), before)
        saved = parse(open(path, "r", encoding="utf-8").read(), ValueError)
        self.assertEqual(build_report(saved)["cash"], "10096.200")


def _outside_files():
    found = []
    for dirpath, dirnames, filenames in os.walk(support.REPO):
        dirnames[:] = [name for name in dirnames if name not in (".git", "paper_trader")]
        if os.path.basename(dirpath) == "__pycache__":
            found.append(dirpath)
        for name in filenames:
            if name.endswith(".pyc"):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


if __name__ == "__main__":
    unittest.main()
