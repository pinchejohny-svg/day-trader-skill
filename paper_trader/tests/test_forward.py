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
from paper_trader.errors import StateExists
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


def _ledger(name):
    from paper_trader.jsonutil import parse
    from paper_trader.paths import state_path

    with open(state_path(name), "r", encoding="utf-8") as handle:
        return parse(handle.read(), ValueError)


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
        second = run_forward(create=False, **common)
        self.assertEqual(first["persistence"], "created")
        self.assertEqual(second["persistence"], "resumed")
        self.assertEqual(first["cash"], "10096.200")
        self.assertEqual(first["label"], "synthetic")
        self.assertEqual(first["provenance"], ["synthetic"])
        self.assertEqual(first["bar_source"], ["synthetic"])
        self.assertEqual(first["adjustment_basis"], ["synthetic"])
        self.assertFalse(first["data_verified"])
        ledger = _ledger(name)
        self.assertEqual(ledger["label"], "synthetic")
        self.assertEqual(ledger["approval"]["label"], first["label"])
        for bar in ledger["inputs"]:
            self.assertEqual(bar["source"], "synthetic")
            self.assertEqual(bar["provenance"], "synthetic")
            self.assertEqual(bar["adjustment_basis"], "synthetic")
            self.assertIn("SYNTHETIC", bar["note"].upper())
            self.assertNotEqual(bar["adjustment_basis"], "massive_unadjusted")
        self.assertEqual(second["cash"], first["cash"])
        self.assertEqual(second["equity"], first["equity"])
        self.assertEqual(second["fingerprint"], first["fingerprint"])
        self.assertEqual(second["orders"], [])
        self.assertTrue(any(item["reason"] == "restart_duplicate" for item in second["rejections"]))
        self.assertIn("bar_end_for_recorded_replay_not_a_live_fill", second["freshness"]["assumptions"])
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
        self.assertEqual(third["cash"], "10096.200")
        self.assertEqual(third["fingerprint"], first["fingerprint"])
        self.assertTrue(any(item["reason"] == "correction_not_applied" for item in third["rejections"]))
        with self.assertRaises(StateExists):
            run_forward(create=True, **common)

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
        clocks = [
            "2026-11-02T09:35:00",
            "2026-11-02T09:40:00",
            "2026-11-02T09:45:00",
            "2026-11-02T09:50:00",
            "2026-11-02T09:55:00",
            "2026-11-02T10:00:00",
            "2026-11-02T10:05:00",
        ]
        waits = []

        def between(previous, clock):
            waits.append((previous, clock))

        def pages(_symbol, _clock):
            return page

        common = dict(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name=name,
            clocks=clocks,
            max_iterations=8,
            max_requests=8,
            max_runtime_seconds=1800,
            injected_pages=pages,
            between=between,
        )
        first = run_session(create=True, **common)
        self.assertEqual(first["stop"], "clocks_exhausted")
        self.assertEqual(first["iterations"], 7)
        self.assertEqual(first["requests"], 7)
        self.assertEqual(len(waits), 6)
        self.assertEqual(first["cash"], "10096.200")
        self.assertEqual(first["invocation"], "finite_session")
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
        second = run_session(create=False, **common)
        self.assertEqual(second["cash"], "10096.200")
        self.assertEqual(second["fingerprint"], first["fingerprint"])
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
            clocks=clocks,
            max_iterations=2,
            max_requests=8,
            max_runtime_seconds=1800,
            injected_pages=pages,
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
            clocks=clocks,
            max_iterations=8,
            max_requests=1,
            max_runtime_seconds=1800,
            injected_pages=pages,
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
            clocks=["2026-11-02T09:35:00", "2026-11-02T09:40:00"],
            max_iterations=8,
            max_requests=8,
            max_runtime_seconds=60,
            injected_pages=pages,
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
            clocks=["2026-11-02T16:00:01"],
            max_iterations=8,
            max_requests=8,
            max_runtime_seconds=1,
            injected_pages=counting,
            create=True,
        )
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
