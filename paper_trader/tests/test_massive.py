"""Offline checks for the inert Massive adapter. Not executed during authoring.

These tests read synthetic fixture text and call pure functions. They do not
call https_get, do not open a socket, and do not read a credential from the
environment.
"""

import ast
import json
import os
import sys
import unittest

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import paper_trader.tests.support as support
from paper_trader.controller import SESSION_ASSERTION, plan_resume, run_recorded
from paper_trader.massive import (
    FULL_MARKET_UNIVERSE,
    CallBudget,
    FeedRejected,
    Offline,
    build_request_url,
    collect_pages,
    fetch_aggregates,
    ny_wall_from_unix_ms,
    parse_response,
    redact,
    translate_results,
    validate_next_url,
)

SECRET = "super-secret-value"


def _body(name):
    path = os.path.join(support.FIXTURES, "massive", name)
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def _run(name, **kwargs):
    settings = {
        "symbols": ["SYN"],
        "session_dates": ["2026-11-02"],
        "session_assertion": SESSION_ASSERTION,
    }
    settings.update(kwargs)
    return run_recorded({"SYN": [_body(name)]}, **settings)


class MassiveOffline(unittest.TestCase):
    def test_recorded_replay_uses_existing_fill_math(self):
        view = _run("synthetic_aggregates_round_trip.json")
        kinds = [item["type"] for item in view["selection"]]
        self.assertEqual(kinds.count("entry"), 1)
        self.assertEqual(kinds.count("exit"), 1)
        self.assertEqual(view["cash"], "10096.200")
        self.assertEqual(view["equity"], "10096.200")
        self.assertEqual(view["realized_pnl"], "96.200")
        self.assertEqual(view["positions"], [])
        self.assertTrue(view["incomplete"])
        self.assertIn("flat_by_not_reached", view["incomplete_reasons"])
        self.assertFalse(view["profitability_claim"])
        self.assertEqual(view["orders"], [])
        self.assertEqual(view["broker"], "absent")
        self.assertEqual(view["network"], "not_called")
        self.assertFalse(view["running"])
        self.assertEqual(view["full_market_universe"], FULL_MARKET_UNIVERSE)
        self.assertEqual(view["request"]["adjusted"], "false")
        self.assertEqual(view["adjustment"]["SYN"]["response_adjusted_field"], "absent")
        self.assertNotIn("results", view)

    def test_signal_without_the_next_bar_does_not_fill(self):
        view = _run("synthetic_aggregates_signal_only.json")
        kinds = [item["type"] for item in view["selection"]]
        self.assertIn("signal", kinds)
        self.assertNotIn("entry", kinds)
        self.assertEqual(view["cash"], "10000.000")
        self.assertEqual(view["orders"], [])

    def test_missing_bar_is_a_gap_and_not_a_fill(self):
        view = _run("synthetic_aggregates_gap.json")
        self.assertNotIn("entry", [item["type"] for item in view["selection"]])
        self.assertEqual(view["cash"], "10000.000")
        self.assertTrue(any(item["reason"] == "gapped" for item in view["warnings"]))

    def test_empty_results_add_no_bar(self):
        view = _run("synthetic_aggregates_empty.json")
        self.assertEqual(view["scan"]["bars_submitted"], 0)
        self.assertEqual(view["cash"], "10000.000")
        self.assertIn("no_data", view["incomplete_reasons"])
        self.assertEqual(view["orders"], [])

    def test_malformed_price_is_rejected(self):
        view = _run("synthetic_aggregates_malformed.json")
        self.assertEqual(view["scan"]["bars_submitted"], 0)
        self.assertTrue(view["rejections"])
        self.assertEqual(view["cash"], "10000.000")
        self.assertNotIn("NOPE", json.dumps(view["rejections"]))

    def test_revision_does_not_replace_the_kept_bar(self):
        view = _run("synthetic_aggregates_correction.json")
        self.assertTrue(any(item["reason"] == "correction_not_applied" for item in view["rejections"]))
        self.assertEqual(view["cash"], "10096.200")
        self.assertTrue(all("100.00" not in json.dumps(item) for item in view["rejections"]))

    def test_off_host_page_is_not_followed_or_echoed(self):
        view = _run("synthetic_aggregates_off_host_page.json")
        self.assertTrue(any(item["reason"] == "pagination_not_followed_offline" for item in view["rejections"]))
        self.assertEqual(view["scan"]["bars_submitted"], 0)
        self.assertNotIn("evil.example", json.dumps(view))

    def test_weekend_and_unasserted_weekday_fail_closed(self):
        with self.assertRaises(FeedRejected):
            run_recorded(
                {"SYN": [_body("synthetic_aggregates_empty.json")]},
                symbols=["SYN"],
                session_dates=["2026-11-08"],
                session_assertion=SESSION_ASSERTION,
            )
        with self.assertRaises(FeedRejected):
            run_recorded(
                {"SYN": [_body("synthetic_aggregates_empty.json")]},
                symbols=["SYN"],
                session_dates=["2026-11-09"],
                session_assertion="",
            )

    def test_session_flags_do_not_claim_a_calendar(self):
        view = _run("synthetic_aggregates_empty.json", session_dates=["2026-11-09"])
        self.assertEqual(view["session"]["holiday_calendar"], "not_implemented")
        self.assertIn("not_detected", view["session"]["early_close"])

    def test_bar_start_and_close_use_new_york_wall_time(self):
        self.assertEqual(ny_wall_from_unix_ms(1793629800000), "2026-11-02T09:30:00")
        self.assertEqual(ny_wall_from_unix_ms("1783344600000"), "2026-07-06T09:30:00")
        with self.assertRaises(FeedRejected):
            ny_wall_from_unix_ms(1793511000000)
        with self.assertRaises(FeedRejected):
            ny_wall_from_unix_ms(1793514600000)
        self.assertEqual(ny_wall_from_unix_ms(1793516400000), "2026-11-01T02:00:00")
        with self.assertRaises(FeedRejected):
            ny_wall_from_unix_ms(1162477800000)
        parsed = parse_response(_body("synthetic_aggregates_round_trip.json"))
        bars, _rejected, assumptions = translate_results(
            parsed["results"][:1],
            symbol="SYN",
            mode="recorded_batch_replay",
            source="synthetic",
        )
        self.assertEqual(bars[0]["start"], "2026-11-02T09:30:00")
        self.assertEqual(bars[0]["observed_at"], "2026-11-02T09:35:00")
        self.assertEqual(assumptions, ["bar_end_for_recorded_replay_not_a_live_fill"])
        self.assertNotIn("volume", bars[0])

    def test_late_and_unclosed_observations_do_not_fill(self):
        late = _run(
            "synthetic_aggregates_round_trip.json",
            mode="as_observed",
            ingested_at="2026-11-02T12:00:00",
        )
        early = _run(
            "synthetic_aggregates_round_trip.json",
            mode="as_observed",
            ingested_at="2026-11-02T09:30:00",
        )
        self.assertNotIn("entry", [item["type"] for item in late["selection"]])
        self.assertNotIn("entry", [item["type"] for item in early["selection"]])
        self.assertEqual(late["cash"], "10000.000")
        self.assertEqual(early["cash"], "10000.000")
        self.assertIn("late_bar", late["freshness"]["late_or_stale_warnings"])
        self.assertIn("not_closed", early["freshness"]["late_or_stale_warnings"])

    def test_off_grid_bar_is_not_a_fill(self):
        body = json.dumps({
            "note": "SYNTHETIC",
            "results": [{"t": 1793629860000, "o": "100.00", "h": "102.00", "l": "99.00", "c": "101.00"}],
        })
        view = run_recorded(
            {"SYN": [body]},
            symbols=["SYN"],
            session_dates=["2026-11-02"],
            session_assertion=SESSION_ASSERTION,
        )
        self.assertNotIn("entry", [item["type"] for item in view["selection"]])
        self.assertTrue(any(item["reason"] == "off_grid" for item in view["warnings"]))

    def test_pagination_stays_on_host_and_stops_at_the_budget(self):
        first = "https://api.massive.com/v2/aggs/ticker/SYN/range/5/minute/2026-11-02/2026-11-02?adjusted=false&sort=asc&limit=5000"
        second = first + "&cursor=2"
        pages = {
            first: json.dumps({"results": [], "next_url": second}),
            second: json.dumps({"results": []}),
        }
        seen = []

        def transport(url):
            seen.append(url)
            return pages[url]

        got = collect_pages(first, transport, CallBudget())
        self.assertEqual(seen, [first, second])
        self.assertEqual(len(got), 2)
        with self.assertRaises(FeedRejected):
            validate_next_url("https://evil.example/next")
        with self.assertRaises(FeedRejected) as userinfo:
            validate_next_url("https://user:%s@api.massive.com/v2/aggs" % SECRET)
        self.assertNotIn(SECRET, str(userinfo.exception))
        with self.assertRaises(FeedRejected) as caught:
            validate_next_url("https://api.massive.com/v2/aggs?apiKey=%s" % SECRET)
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertNotIn("apiKey", str(caught.exception))
        calls = []

        def boom(url):
            calls.append(url)
            return json.dumps({"results": [], "next_url": "https://evil.example/next"})

        with self.assertRaises(FeedRejected):
            collect_pages(first, boom, CallBudget())
        self.assertEqual(calls, [first])
        with self.assertRaises(FeedRejected):
            collect_pages(first, transport, CallBudget(max_calls=1, max_pages=2))

    def test_request_url_and_errors_do_not_carry_a_secret(self):
        url = build_request_url("SYN", "2026-11-02", "2026-11-02")
        self.assertTrue(url.startswith("https://api.massive.com/v2/aggs/ticker/SYN/range/5/minute/"))
        self.assertIn("adjusted=false", url)
        self.assertNotIn("apiKey", url)
        self.assertNotIn(SECRET, url)
        dirty = "https://user:%s@api.massive.com/v2/aggs?apiKey=%s" % (SECRET, SECRET)
        cleaned = redact(dirty, SECRET)
        self.assertNotIn(SECRET, cleaned)
        self.assertNotIn("user:", cleaned)
        with self.assertRaises(Offline) as caught:
            fetch_aggregates(
                "SYN",
                "2026-11-02",
                "2026-11-02",
                5,
                runtime_approval="massive_readonly_fetch_approved",
                api_key=SECRET,
                network_enabled=False,
            )
        self.assertNotIn(SECRET, str(caught.exception))
        with self.assertRaises(FeedRejected):
            fetch_aggregates(
                "SYN", "2026-11-02", "2026-11-02", 0,
                runtime_approval="nope", api_key=SECRET, network_enabled=True,
                transport=lambda _url: self.fail("transport called"),
            )

    def test_timeout_bounds(self):
        from paper_trader.massive import assert_timeout
        assert_timeout(1)
        assert_timeout(10)
        with self.assertRaises(FeedRejected):
            assert_timeout(0)
        with self.assertRaises(FeedRejected):
            assert_timeout(11)
        with self.assertRaises(FeedRejected):
            assert_timeout(True)

    def test_restart_duplicate_does_not_add_a_second_fill(self):
        parsed = parse_response(_body("synthetic_aggregates_round_trip.json"))
        bars, _rejected, _notes = translate_results(
            parsed["results"],
            symbol="SYN",
            mode="recorded_batch_replay",
            source="synthetic",
        )
        combined, rejections = plan_resume(bars, bars)
        self.assertEqual(combined, bars)
        self.assertTrue(any(item["reason"] == "restart_duplicate" for item in rejections))
        first = _run("synthetic_aggregates_round_trip.json")
        second = _run("synthetic_aggregates_round_trip.json", prior_inputs=bars)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertEqual(second["cash"], "10096.200")
        self.assertEqual(second["orders"], [])

    def test_split_adjusted_page_is_refused(self):
        with self.assertRaises(FeedRejected):
            parse_response('{"adjusted": true, "results": []}')

    def test_source_has_no_order_path_and_import_does_not_load_the_client(self):
        package = support.PACKAGE
        controller = open(os.path.join(package, "controller.py"), "r", encoding="utf-8").read()
        init = open(os.path.join(package, "__init__.py"), "r", encoding="utf-8").read()
        main = open(os.path.join(package, "__main__.py"), "r", encoding="utf-8").read()
        for source in (controller, init, main):
            lowered = source.lower()
            self.assertNotIn("https_get", source)
            self.assertNotIn("alpaca", lowered)
            self.assertNotIn("subprocess", source)
            self.assertNotIn("place_order", lowered)
        self.assertNotIn("massive", init)
        self.assertNotIn("urllib", init)
        tree = ast.parse(open(os.path.join(package, "massive.py"), "r", encoding="utf-8").read())
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            self.assertNotIn("urllib", names)
            self.assertNotIn("socket", names)
