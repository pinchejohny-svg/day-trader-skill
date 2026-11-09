"""Integration checks for the wired paper run. Not executed during authoring.

These tests use synthetic pages and, where a read is opted in, an injected
transport. They do not call the live reader.
"""

import json
import os
import sys
import unittest

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import paper_trader.tests.support as support
from paper_trader.controller import commit_ledger
from paper_trader.errors import StateExists, StateMissing
from paper_trader.controller import rows_for_clock
from paper_trader.forward import run_forward, run_session
from paper_trader.massive import RUNTIME_APPROVAL, FeedRejected, Offline
from paper_trader.paths import demo_dir, kind

SECRET = "super-secret-value"
STAMP = "2026-11-02T10:05:00"


def _text(name):
    path = os.path.join(support.FIXTURES, "massive", name)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _clear(name):
    from paper_trader.paths import state_path

    path = state_path(name)
    if kind(path) == "file":
        os.unlink(path)
    return path


def _scripted_clock(stamps):
    pending = list(stamps)

    def clock():
        if not pending:
            return stamps[-1]
        return pending.pop(0)

    return clock


def _scripted_clock_numbers(values):
    pending = list(values)

    def read():
        if len(pending) == 1:
            return pending[0]
        return pending.pop(0)

    return read


def _stuck(value):
    def read():
        return value

    return read


def _raw(name):
    from paper_trader.paths import state_path

    with open(state_path(name), "r", encoding="utf-8") as handle:
        return handle.read()


def _ledger(name):
    from paper_trader.jsonutil import parse

    return parse(_raw(name), ValueError)


def _bars(close):
    rows = [
        (1793629800000, "100.00", "102.00", "99.00", "101.00"),
        (1793630100000, "101.00", "103.00", "100.00", "102.00"),
        (1793630400000, "102.00", "104.00", "101.00", "103.00"),
        (1793630700000, "103.00", "107.00", "102.50", close),
        (1793631000000, "105.20", "106.00", "105.00", "105.20"),
    ]
    return json.dumps({
        "note": "SYNTHETIC invented prices. Not market data.",
        "results": [
            {"t": t, "o": o, "h": h, "l": low, "c": c}
            for t, o, h, low, c in rows
        ],
    })


class ForwardIntegration(unittest.TestCase):
    def test_dry_default_does_not_fetch_when_a_key_is_present(self):
        with self.assertRaises(Offline) as caught:
            run_forward(
                symbols=["SYN"],
                session_dates=["2026-11-02"],
                session_kind="regular",
                early_close="none",
                state_name="forward-dry.json",
                mode="fresh_forward",
                acquire=True,
                dry_run=True,
                network_enabled=False,
                runtime_approval=RUNTIME_APPROVAL,
                api_key=SECRET,
                injected_body=None,
                create=True,
            )
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(demo_dir(), "forward-dry.json")))

    def test_historical_replay_persists_and_resume_does_not_rewrite(self):
        name = "forward-round.json"
        _clear(name)
        bodies = {"SYN": [_text("synthetic_aggregates_round_trip.json")]}
        common = dict(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name=name,
            mode="historical_replay",
            bodies_by_symbol=bodies,
        )
        first = run_forward(create=True, **common)
        created = _ledger(name)
        self.assertEqual(first["persistence"], "created")
        self.assertEqual(first["cash"], "10096.200")
        self.assertEqual(created["cash"], first["cash"])
        self.assertEqual(created["fingerprint"], first["fingerprint"])
        self.assertEqual(created["label"], "synthetic")
        self.assertEqual(created["approval"]["label"], first["label"])
        self.assertEqual(first["provenance"], ["synthetic"])
        self.assertEqual(first["bar_source"], ["synthetic"])
        self.assertEqual(first["adjustment_basis"], ["synthetic"])
        self.assertFalse(first["data_verified"])
        for bar in created["inputs"]:
            self.assertEqual(bar["source"], "synthetic")
            self.assertEqual(bar["provenance"], "synthetic")
            self.assertEqual(bar["adjustment_basis"], "synthetic")
            self.assertIn("SYNTHETIC", bar["note"].upper())
        second = run_forward(create=False, **common)
        resumed = _ledger(name)
        self.assertEqual(second["persistence"], "resumed")
        self.assertEqual(resumed["inputs"], created["inputs"])
        self.assertEqual(resumed["fingerprint"], first["fingerprint"])
        self.assertEqual(second["fingerprint"], resumed["fingerprint"])
        self.assertEqual(second["cash"], created["cash"])
        self.assertTrue(any(item["reason"] == "restart_duplicate" for item in second["rejections"]))
        revised = json.loads(bodies["SYN"][0])
        revised["results"][3]["c"] = "100.00"
        third = run_forward(
            create=False,
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name=name,
            mode="historical_replay",
            bodies_by_symbol={"SYN": [json.dumps(revised)]},
        )
        kept = _ledger(name)
        self.assertEqual(kept["inputs"], created["inputs"])
        self.assertEqual(kept["fingerprint"], created["fingerprint"])
        self.assertEqual(third["fingerprint"], kept["fingerprint"])
        self.assertTrue(any(item["reason"] == "correction_not_applied" for item in third["rejections"]))
        before = _raw(name)
        with self.assertRaises(StateExists):
            run_forward(create=True, **common)
        self.assertEqual(_raw(name), before)

    def test_commit_rejects_before_any_ledger_write(self):
        name = "forward-reject.json"
        _clear(name)
        approval = support.approval(["SYN"], ["2026-11-02"])
        with self.assertRaises(StateMissing):
            commit_ledger(name, [], approval, create=False)
        from paper_trader.paths import state_path
        self.assertFalse(os.path.exists(state_path(name)))
        bad = [{
            "adjustment_basis": "synthetic",
            "provenance": "synthetic",
            "source": "historical",
        }]
        with self.assertRaises(FeedRejected):
            commit_ledger(name, bad, approval, create=True)
        self.assertFalse(os.path.exists(state_path(name)))

    def test_defined_universe_is_ranked_by_the_existing_engine(self):
        name = "forward-rank.json"
        _clear(name)
        view = run_forward(
            symbols=["AAA", "BBB"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name=name,
            mode="historical_replay",
            bodies_by_symbol={"AAA": [_bars("105.00")], "BBB": [_bars("106.00")]},
            create=True,
        )
        entries = [item["symbol"] for item in view["selection"] if item["type"] == "entry"]
        self.assertEqual(entries[0], "BBB")
        self.assertIn("AAA", entries)
        self.assertFalse(view["full_market_universe"])
        self.assertEqual(view["automated_selection"], "existing_engine_rank_within_predeclared_symbols")
        self.assertGreater(len(view["positions"]) + len(entries), 0)

    def test_fresh_forward_uses_the_acquisition_clock_and_the_response_flag(self):
        name = "forward-fresh.json"
        _clear(name)
        raw = json.loads(_text("synthetic_aggregates_round_trip.json"))
        raw["adjusted"] = False
        seen = []

        def injected_body(url):
            seen.append(url)
            self.assertNotIn("apiKey", url)
            self.assertTrue(url.startswith("https://api.massive.com/"))
            return json.dumps(raw)

        view = run_forward(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name=name,
            mode="fresh_forward",
            acquire=True,
            dry_run=False,
            network_enabled=False,
            injected_body=injected_body,
            acquired_at=STAMP,
            create=True,
        )
        self.assertEqual(seen[0].split("?", 1)[0], "https://api.massive.com/v2/aggs/ticker/SYN/range/5/minute/2026-11-02/2026-11-02")
        self.assertEqual(view["network"], "injected_not_a_provider_read")
        self.assertEqual(view["label"], "synthetic")
        self.assertEqual(view["provenance"], ["injected"])
        self.assertEqual(view["persistence"], "not_written")
        self.assertEqual(view["orders"], [])
        self.assertEqual(view["selection"], [])
        self.assertFalse(view["data_verified"])
        self.assertFalse(view["running"])
        self.assertTrue(any(item["reason"] == "late_at_clock" for item in view["rejections"]))
        from paper_trader.paths import state_path
        self.assertFalse(os.path.exists(state_path(name)))
        self.assertNotIn("massive_unadjusted", json.dumps(view))
        self.assertNotIn("historical", json.dumps({
            "label": view["label"],
            "provenance": view["provenance"],
        }))

    def test_sunday_waits_and_an_unknown_session_does_not_trade(self):
        view = run_forward(
            symbols=["SYN"],
            session_dates=["2026-11-08"],
            session_kind="regular",
            early_close="none",
            state_name="forward-sunday.json",
            mode="historical_replay",
            bodies_by_symbol={"SYN": ["{"]},
            create=True,
        )
        self.assertEqual(view["status"], "waiting")
        self.assertEqual(view["session"]["reason"], "market_closed")
        self.assertEqual(view["orders"], [])
        self.assertEqual(view["selection"], [])
        self.assertEqual(view["persistence"], "not_written")
        self.assertFalse(os.path.exists(os.path.join(demo_dir(), "forward-sunday.json")))
        refused = run_forward(
            symbols=["SYN"],
            session_dates=["2026-11-09"],
            session_kind="regular",
            early_close="unknown",
            state_name="forward-unknown.json",
            mode="historical_replay",
            bodies_by_symbol={"SYN": ["{"]},
            create=True,
        )
        self.assertEqual(refused["status"], "refused")
        self.assertEqual(refused["session"]["reason"], "unknown_session")
        self.assertEqual(refused["orders"], [])
        self.assertEqual(refused["persistence"], "not_written")

    def test_clock_window_does_not_treat_old_bars_as_fresh(self):
        page = json.loads(_text("synthetic_aggregates_round_trip.json"))["results"]
        closed, _rejected = rows_for_clock(page, "2026-11-02T09:35:00", [])
        self.assertEqual(len(closed), 1)
        missed, rejected = rows_for_clock(page, "2026-11-02T09:40:01", [])
        self.assertEqual(missed, [])
        self.assertTrue(any(item["reason"] == "late_at_clock" for item in rejected))
        self.assertTrue(any(item["reason"] == "not_closed" for item in rejected))
        edge, _edge_rejected = rows_for_clock(page, "2026-11-02T09:40:00", ["2026-11-02T09:30:00"])
        self.assertEqual(len(edge), 2)

    def test_finite_session_polls_then_restarts_without_a_second_fill(self):
        name = "forward-session.json"
        _clear(name)
        page = _text("synthetic_aggregates_round_trip.json")
        stamps = [
            "2026-11-02T09:35:00",
            "2026-11-02T09:40:00",
            "2026-11-02T09:45:00",
            "2026-11-02T09:50:00",
            "2026-11-02T09:55:00",
            "2026-11-02T10:00:00",
            "2026-11-02T10:05:00",
        ]
        waits = []

        def waiter(seconds):
            waits.append(seconds)

        def pages(_symbol, _clock):
            return page

        common = dict(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name=name,
            max_iterations=7,
            max_requests=8,
            max_runtime_seconds=1800,
            poll_seconds=60,
            injected_pages=pages,
            clock=_scripted_clock(stamps),
            waiter=waiter,
            monotonic=_stuck(0),
        )
        first = run_session(create=True, **common)
        self.assertEqual(first["stop"], "max_iterations")
        self.assertEqual(first["clock"], "injected")
        self.assertEqual(first["wait"], "injected")
        self.assertEqual(first["iterations"], 7)
        self.assertEqual(first["requests"], 7)
        self.assertEqual(waits, [60, 60, 60, 60, 60, 60])
        self.assertEqual(first["cash"], "10096.200")
        self.assertEqual(first["invocation"], "finite_wall_clock_session")
        self.assertEqual(first["network"], "injected_not_a_provider_read")
        self.assertFalse(first["running"])
        self.assertEqual(first["orders"], [])
        self.assertEqual(first["label"], "synthetic")
        self.assertEqual(first["provenance"], ["injected"])
        self.assertEqual(first["bar_source"], ["synthetic"])
        self.assertEqual(first["adjustment_basis"], ["synthetic"])
        self.assertFalse(first["data_verified"])
        fees = [item["fee"] for item in first["selection"] if item["type"] == "entry"]
        self.assertEqual(fees, ["0.065"])
        ledger = _ledger(name)
        self.assertEqual(ledger["label"], first["label"])
        self.assertEqual(ledger["cash"], "10096.200")
        for bar in ledger["inputs"]:
            self.assertEqual(bar["provenance"], "injected")
            self.assertEqual(bar["source"], "synthetic")
            self.assertEqual(bar["adjustment_basis"], "synthetic")
            self.assertIn("SYNTHETIC", bar["note"].upper())
        self.assertNotIn("massive_unadjusted", json.dumps(ledger["inputs"]))
        second = run_session(
            create=False,
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name=name,
            max_iterations=7,
            max_requests=8,
            max_runtime_seconds=1800,
            poll_seconds=60,
            injected_pages=pages,
            clock=_scripted_clock(stamps),
            waiter=waiter,
            monotonic=_stuck(0),
        )
        self.assertEqual(second["cash"], "10096.200")
        self.assertEqual(second["fingerprint"], first["fingerprint"])
        again = _ledger(name)
        self.assertEqual(again["inputs"], ledger["inputs"])
        self.assertEqual(again["fingerprint"], ledger["fingerprint"])
        self.assertEqual(second["fingerprint"], again["fingerprint"])
        self.assertEqual(
            [item["type"] for item in second["selection"]].count("entry"),
            1,
        )
        _clear("forward-cap.json")
        capped = run_session(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name="forward-cap.json",
            max_iterations=2,
            max_requests=8,
            max_runtime_seconds=1800,
            injected_pages=pages,
            clock=_scripted_clock(stamps),
            waiter=waiter,
            monotonic=_stuck(0),
            create=True,
        )
        self.assertEqual(capped["stop"], "max_iterations")
        self.assertEqual(capped["iterations"], 2)
        self.assertNotIn("entry", [item["type"] for item in capped["selection"]])
        _clear("forward-requests.json")
        requested = run_session(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name="forward-requests.json",
            max_iterations=8,
            max_requests=1,
            max_runtime_seconds=1800,
            injected_pages=pages,
            clock=_scripted_clock(stamps),
            waiter=waiter,
            monotonic=_stuck(0),
            create=True,
        )
        self.assertEqual(requested["stop"], "max_requests")
        self.assertEqual(requested["requests"], 1)
        _clear("forward-runtime.json")
        timed = run_session(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name="forward-runtime.json",
            max_iterations=8,
            max_requests=8,
            max_runtime_seconds=60,
            injected_pages=pages,
            clock=_scripted_clock(stamps),
            waiter=waiter,
            monotonic=_scripted_clock_numbers([0, 0, 120]),
            create=True,
        )
        self.assertEqual(timed["stop"], "max_runtime")
        self.assertEqual(timed["requests"], 1)
        calls = []

        def counting(_symbol, _clock):
            calls.append("called")
            return page

        _clear("forward-cutoff.json")
        cutoff = run_session(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name="forward-cutoff.json",
            max_iterations=8,
            max_requests=8,
            max_runtime_seconds=1800,
            injected_pages=counting,
            clock=lambda: "2026-11-02T16:00:01",
            waiter=waiter,
            monotonic=_stuck(0),
            create=True,
        )
        interrupted = []

        def interrupt(_seconds):
            raise KeyboardInterrupt

        _clear("forward-interrupt.json")
        stopped = run_session(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name="forward-interrupt.json",
            max_iterations=4,
            max_requests=8,
            max_runtime_seconds=1800,
            injected_pages=pages,
            clock=_scripted_clock(stamps),
            waiter=interrupt,
            monotonic=_stuck(0),
            create=True,
        )
        interrupted.append(stopped["stop"])
        self.assertEqual(interrupted, ["interrupted"])
        self.assertEqual(stopped["iterations"], 1)
        self.assertEqual(stopped["requests"], 1)
        partial = _ledger("forward-interrupt.json")
        self.assertEqual(len(partial["inputs"]), 1)
        self.assertEqual(partial["inputs"][0]["provenance"], "injected")
        self.assertEqual(partial["inputs"][0]["source"], "synthetic")
        self.assertEqual(stopped["fingerprint"], partial["fingerprint"])
        self.assertEqual(cutoff["stop"], "session_cutoff")
        self.assertEqual(cutoff["requests"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(cutoff["orders"], [])

    def test_oversized_universe_does_not_call_transport(self):
        called = []
        with self.assertRaises(FeedRejected) as caught:
            run_forward(
                symbols=["S%02d" % index for index in range(21)],
                session_dates=["2026-11-02"],
                session_kind="regular",
                early_close="none",
                state_name="forward-too-many.json",
                mode="fresh_forward",
                acquire=True,
                dry_run=False,
                network_enabled=True,
                runtime_approval=RUNTIME_APPROVAL,
                injected_body=lambda _url: called.append("called"),
                acquired_at=STAMP,
                create=True,
            )
        self.assertEqual(str(caught.exception), "symbol budget exhausted")
        self.assertEqual(called, [])
