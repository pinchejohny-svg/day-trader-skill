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
from paper_trader.forward import run_forward
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
        called = []
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
                transport=lambda _url: called.append("called") or "{}",
                create=True,
            )
        self.assertEqual(called, [])
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

        def transport(url):
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
            network_enabled=True,
            runtime_approval=RUNTIME_APPROVAL,
            timeout_seconds=5,
            transport=transport,
            acquired_at=STAMP,
            create=True,
        )
        self.assertEqual(seen[0].split("?", 1)[0], "https://api.massive.com/v2/aggs/ticker/SYN/range/5/minute/2026-11-02/2026-11-02")
        self.assertEqual(view["clocks"], [STAMP])
        self.assertNotIn("09:35:00", view["clocks"][0])
        self.assertEqual(view["adjustment"]["SYN"]["adjustment_basis"], "massive_unadjusted")
        self.assertEqual(view["network"], "explicit_acquire_path")
        self.assertEqual(view["persistence"], "created")
        self.assertEqual(view["orders"], [])
        self.assertFalse(view["running"])
        missing = dict(raw)
        missing.pop("adjusted", None)
        other = "forward-unverified.json"
        _clear(other)
        unverified = run_forward(
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_kind="regular",
            early_close="none",
            state_name=other,
            mode="fresh_forward",
            acquire=True,
            dry_run=False,
            network_enabled=True,
            runtime_approval=RUNTIME_APPROVAL,
            transport=lambda _url: json.dumps(missing),
            acquired_at=STAMP,
            create=True,
        )
        self.assertEqual(unverified["adjustment"]["SYN"]["adjustment_basis"], "unverified")
        self.assertEqual(unverified["clocks"], [STAMP])

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
                transport=lambda _url: called.append("called"),
                acquired_at=STAMP,
                create=True,
            )
        self.assertEqual(str(caught.exception), "symbol budget exhausted")
        self.assertEqual(called, [])
