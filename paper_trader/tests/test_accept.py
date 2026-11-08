"""Acceptance checks for the offline ledger. Not executed during authoring."""

import datetime
import os
import sys
import unittest
from decimal import Decimal

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

import paper_trader.tests.support as support
from paper_trader.engine import run_bars, size_batch
from paper_trader.feed import load_fixture
from paper_trader.report import build_report


def replay(name, symbols, dates=("2026-11-02",)):
    loaded = load_fixture(support.fixture(name))
    doc = run_bars(loaded["bars"], support.approval(symbols, dates, loaded["label"]))
    return loaded, doc, build_report(doc)


class ReplayAcceptance(unittest.TestCase):
    def test_round_trip_scan_select_entry_exit_and_ledger(self):
        _loaded, doc, report = replay("synthetic_round_trip.json", ["SYN"])
        entries = support.of_type(doc, "entry")
        exits = support.of_type(doc, "exit")
        self.assertEqual(len(entries), 1)
        self.assertEqual(len(exits), 1)
        entry = entries[0]
        self.assertEqual(entry["symbol"], "SYN")
        self.assertEqual(entry["raw_open"], "105.20")
        self.assertEqual(entry["entry_price"], "105.21")
        self.assertNotEqual(entry["entry_price"], entry["signal_close"])
        self.assertEqual(entry["signal_close"], "105.00")
        self.assertEqual(entry["stop"], "101.50")
        self.assertEqual(entry["target"], "112.63")
        self.assertEqual(entry["qty"], 13)
        self.assertEqual(entry["fee"], "0.065")
        self.assertEqual(entry["slippage"], "0.01")
        self.assertEqual(entry["bound_by"], "risk")
        self.assertEqual(entry["shares_risk"], 13)
        self.assertEqual(entry["shares_cap"], 23)
        self.assertEqual(entry["shares_cash"], 95)
        self.assertEqual(entry["price_source"], "next_bar_open")
        exit_ = exits[0]
        self.assertEqual(exit_["reason"], "target")
        self.assertEqual(exit_["raw_price"], "112.63")
        self.assertEqual(exit_["price"], "112.62")
        self.assertEqual(exit_["pnl"], "96.200")
        self.assertEqual(doc["cash"], "10096.200")
        self.assertEqual(doc["realized_pnl"], "96.200")
        self.assertEqual(doc["max_drawdown"], "9.295")
        self.assertEqual(doc["peak_equity"], "10096.200")
        self.assertEqual(report["drawdown"], "0.000")
        self.assertFalse(report["full_session"])
        self.assertFalse(report["profitability_claim"])
        self.assertFalse(report["broker_fills"])
        self.assertFalse(report["data_verified"])
        self.assertEqual(report["label"], "synthetic")
        self.assertIn("flat_by_not_reached", report["incomplete_reasons"])

    def test_signal_bar_alone_does_not_fill(self):
        loaded = load_fixture(support.fixture("synthetic_round_trip.json"))
        early = [bar for bar in loaded["bars"] if bar["start"] <= "2026-11-02T09:45:00"]
        doc = run_bars(early, support.approval(["SYN"], ["2026-11-02"]))
        self.assertEqual(support.of_type(doc, "signal")[0]["signal_close"], "105.00")
        self.assertEqual(support.of_type(doc, "entry"), [])
        self.assertEqual(doc["cash"], "10000.000")
        self.assertEqual(doc["positions"], {})

    def test_reversed_file_order_does_not_change_fills(self):
        loaded = load_fixture(support.fixture("synthetic_round_trip.json"))
        forward = run_bars(loaded["bars"], support.approval(["SYN"], ["2026-11-02"]))
        backward = run_bars(list(reversed(loaded["bars"])), support.approval(["SYN"], ["2026-11-02"]))
        self.assertEqual(forward["cash"], backward["cash"])
        self.assertEqual(forward["events"], backward["events"])
        self.assertNotEqual(forward["inputs"], backward["inputs"])

    def test_morning_tape_stays_open_and_is_not_a_full_session(self):
        _loaded, doc, report = replay("synthetic_morning_incomplete.json", ["SYN"])
        self.assertEqual(support.of_type(doc, "exit"), [])
        self.assertIn("SYN", doc["positions"])
        position = doc["positions"]["SYN"]
        self.assertEqual(position["qty"], 13)
        self.assertEqual(position["last_close"], "105.20")
        self.assertEqual(position["mark_status"], "fresh")
        self.assertEqual(doc["cash"], "8632.205")
        self.assertEqual(doc["realized_pnl"], "0.000")
        self.assertEqual(report["unrealized_pnl"], "-0.195")
        self.assertEqual(report["equity_value"], "9999.805")
        self.assertEqual(report["max_drawdown"], "0.195")
        self.assertFalse(report["full_session"])
        self.assertIn("open_position", report["incomplete_reasons"])
        self.assertIn("flat_by_not_reached", report["incomplete_reasons"])

    def test_no_qualified_signal_trades_nothing(self):
        _loaded, doc, report = replay("synthetic_no_signal.json", ["SYN"])
        self.assertEqual(support.of_type(doc, "signal"), [])
        self.assertEqual(support.of_type(doc, "entry"), [])
        self.assertEqual(doc["cash"], "10000.000")
        self.assertEqual(doc["realized_pnl"], "0.000")
        self.assertEqual(report["open_positions"], [])

    def test_summary_metadata_cannot_create_a_trade(self):
        loaded = load_fixture(support.fixture("synthetic_lookahead_trap.json"))
        self.assertNotIn("summary", loaded)
        doc = run_bars(loaded["bars"], support.approval(["SYN"], ["2026-11-02"]))
        self.assertEqual(doc["cash"], "10000.000")
        self.assertEqual(support.of_type(doc, "entry"), [])
        self.assertNotIn("FAKE", doc["positions"])
        blob = repr(doc["events"])
        self.assertNotIn("FAKE", blob)

    def test_stop_first_when_both_levels_are_touched(self):
        _loaded, doc, report = replay("synthetic_stop_first.json", ["SYN"])
        exit_ = support.of_type(doc, "exit")[0]
        self.assertEqual(exit_["reason"], "stop_first")
        self.assertEqual(exit_["raw_price"], "101.50")
        self.assertEqual(exit_["price"], "101.49")
        self.assertNotEqual(exit_["price"], "112.62")
        self.assertEqual(exit_["pnl"], "-48.490")
        self.assertEqual(doc["cash"], "9951.510")
        self.assertEqual(doc["max_drawdown"], "48.490")
        self.assertEqual(report["drawdown"], "48.490")
        self.assertEqual(doc["positions"], {})

    def test_gap_fills_at_the_open_and_blocks_the_next_entry(self):
        _loaded, doc, _report = replay("synthetic_gap_loss_stop.json", ["GAP", "LATE"])
        gap_entry = support.of_type(doc, "entry")
        self.assertEqual([event["symbol"] for event in gap_entry], ["GAP"])
        self.assertEqual(gap_entry[0]["qty"], 49)
        self.assertEqual(gap_entry[0]["entry_price"], "50.01")
        self.assertEqual(gap_entry[0]["stop"], "49.95")
        self.assertEqual(gap_entry[0]["target"], "50.13")
        self.assertEqual(gap_entry[0]["shares_risk"], 625)
        self.assertEqual(gap_entry[0]["shares_cap"], 49)
        self.assertEqual(gap_entry[0]["shares_cash"], 199)
        exit_ = support.of_type(doc, "exit")[0]
        self.assertEqual(exit_["reason"], "stop_gap")
        self.assertEqual(exit_["raw_price"], "40.00")
        self.assertEqual(exit_["price"], "39.99")
        self.assertEqual(exit_["pnl"], "-491.470")
        self.assertEqual(doc["cash"], "9508.530")
        self.assertEqual(doc["max_drawdown"], "493.185")
        self.assertNotIn("LATE", doc["positions"])
        skips = [event for event in support.of_type(doc, "skip") if event["symbol"] == "LATE"]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["reason"], "daily_realized_loss_stop")
        self.assertTrue(Decimal(doc["realized_pnl"]) <= Decimal("-300.000"))

    def test_cash_reservation_and_entry_cap(self):
        _loaded, doc, _report = replay(
            "synthetic_cash_reservation.json", ["AAA", "BBB", "CCC", "DDD"]
        )
        entries = support.of_type(doc, "entry")
        self.assertEqual([event["symbol"] for event in entries], ["AAA", "BBB", "CCC"])
        self.assertEqual([event["shares_cash"] for event in entries], [4, 3, 2])
        self.assertEqual(
            [event["sizing_cash"] for event in entries],
            ["10000.000", "7599.985", "5199.970"],
        )
        self.assertEqual({event["qty"] for event in entries}, {1})
        self.assertEqual({event["bound_by"] for event in entries}, {"position_cap"})
        self.assertEqual(doc["cash"], "2799.955")
        skips = support.of_type(doc, "skip")
        self.assertEqual([event["symbol"] for event in skips], ["DDD"])
        self.assertEqual(skips[0]["reason"], "max_entries_per_day")
        self.assertNotIn("DDD", doc["positions"])

    def test_rank_uses_signal_close_not_symbol_order(self):
        bars = []
        for symbol, signal_close, signal_high in (
            ("AAA", "101.00", "101.20"),
            ("BBB", "110.00", "110.50"),
        ):
            bars.extend(
                [
                    support.bar(symbol, "2026-11-02T09:30:00", "99.40", "100.00", "99.00", "99.50"),
                    support.bar(symbol, "2026-11-02T09:35:00", "99.50", "99.80", "99.20", "99.60"),
                    support.bar(symbol, "2026-11-02T09:40:00", "99.60", "99.70", "99.30", "99.40"),
                    support.bar(symbol, "2026-11-02T09:45:00", "100.00", signal_high, "99.70", signal_close),
                    support.bar(symbol, "2026-11-02T09:50:00", "100.50", "101.00", "100.40", "100.80"),
                ]
            )
        doc = run_bars(bars, support.approval(["BBB", "AAA"], ["2026-11-02"]))
        self.assertEqual(
            [event["symbol"] for event in support.of_type(doc, "entry")],
            ["BBB", "AAA"],
        )

    def test_size_batch_second_name_is_cash_constrained(self):
        plans = size_batch(
            [
                {
                    "entry": Decimal("2400.01"),
                    "pct": Decimal("1"),
                    "stop": Decimal("2395.00"),
                    "symbol": "AAA",
                },
                {
                    "entry": Decimal("2400.01"),
                    "pct": Decimal("2"),
                    "stop": Decimal("2395.00"),
                    "symbol": "BBB",
                },
            ],
            Decimal("10000.000"),
            Decimal("2500.000"),
            0,
        )
        self.assertEqual(plans[0]["candidate"]["symbol"], "BBB")
        self.assertEqual(plans[0]["qty"], 1)
        self.assertEqual(plans[1]["candidate"]["symbol"], "AAA")
        self.assertEqual(plans[1]["qty"], 0)
        self.assertEqual(plans[1]["reason"], "cash")

    def test_full_session_without_a_breakout_is_flat(self):
        day = "2026-11-02"
        cursor = datetime.datetime.strptime(day + "T09:30:00", "%Y-%m-%dT%H:%M:%S")
        end = datetime.datetime.strptime(day + "T15:55:00", "%Y-%m-%dT%H:%M:%S")
        bars = []
        while cursor <= end:
            bars.append(
                support.bar(
                    "FLAT",
                    cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                    "10.00",
                    "10.10",
                    "9.90",
                    "10.00",
                )
            )
            cursor += datetime.timedelta(minutes=5)
        self.assertEqual(len(bars), 78)
        doc = run_bars(bars, support.approval(["FLAT"], [day]))
        report = build_report(doc)
        self.assertEqual(doc["events"], [])
        self.assertEqual(doc["cash"], "10000.000")
        self.assertTrue(report["full_session"])
        self.assertFalse(report["incomplete"])
        self.assertFalse(report["profitability_claim"])
        self.assertEqual(report["result_kind"], "simulated_accounting_not_a_market_result")

    def test_no_data_symbol_blocks_entries_without_a_synthetic_price(self):
        _loaded, doc, report = replay("synthetic_no_data.json", ["NODATA"])
        self.assertEqual(doc["cash"], "10000.000")
        self.assertEqual(doc["events"], [])
        self.assertEqual(doc["symbol_status"]["NODATA"]["status"], "no_data")
        self.assertIn("no_data", report["incomplete_reasons"])
        self.assertEqual(report["unrealized_pnl"], "0.000")

    def test_late_fill_bar_does_not_enter(self):
        loaded = load_fixture(support.fixture("synthetic_round_trip.json"))
        bars = []
        for item in loaded["bars"]:
            if item["start"] > "2026-11-02T09:50:00":
                continue
            copied = dict(item)
            if copied["start"] == "2026-11-02T09:50:00":
                copied["observed_at"] = "2026-11-02T12:00:00"
            bars.append(copied)
        doc = run_bars(bars, support.approval(["SYN"], ["2026-11-02"]))
        self.assertEqual(support.of_type(doc, "entry"), [])
        self.assertEqual(doc["positions"], {})
        expired = support.of_type(doc, "expire")
        self.assertEqual(expired[0]["reason"], "late_fill_bar")

    def test_late_bar_does_not_manufacture_an_exit(self):
        loaded = load_fixture(support.fixture("synthetic_morning_incomplete.json"))
        bars = list(loaded["bars"])
        bars.append(
            support.bar(
                "SYN",
                "2026-11-02T09:55:00",
                "100.00",
                "100.50",
                "90.00",
                "95.00",
                observed_at="2026-11-02T12:00:00",
            )
        )
        doc = run_bars(bars, support.approval(["SYN"], ["2026-11-02"]))
        report = build_report(doc)
        self.assertEqual(support.of_type(doc, "exit"), [])
        self.assertEqual(doc["positions"]["SYN"]["qty"], 13)
        self.assertEqual(doc["positions"]["SYN"]["mark_status"], "stale")
        self.assertIsNone(report["unrealized_pnl"])
        self.assertIsNone(report["equity_value"])
        self.assertIn("stale_or_unpriced_position", report["incomplete_reasons"])

    def test_unclosed_bar_does_not_fill(self):
        loaded = load_fixture(support.fixture("synthetic_round_trip.json"))
        bars = []
        for item in loaded["bars"]:
            if item["start"] > "2026-11-02T09:50:00":
                continue
            copied = dict(item)
            if copied["start"] == "2026-11-02T09:50:00":
                copied["observed_at"] = "2026-11-02T09:50:00"
            bars.append(copied)
        doc = run_bars(bars, support.approval(["SYN"], ["2026-11-02"]))
        self.assertEqual(support.of_type(doc, "entry"), [])
        self.assertEqual(support.of_type(doc, "expire")[0]["reason"], "not_closed")

    def test_conflicting_duplicate_does_not_use_the_second_close(self):
        loaded = load_fixture(support.fixture("synthetic_round_trip.json"))
        bars = [dict(item) for item in loaded["bars"] if item["start"] <= "2026-11-02T09:45:00"]
        bars.append(
            support.bar("SYN", "2026-11-02T09:45:00", "103.00", "104.00", "102.50", "103.00")
        )
        doc = run_bars(bars, support.approval(["SYN"], ["2026-11-02"]))
        self.assertEqual(support.of_type(doc, "signal"), [])
        self.assertEqual(support.of_type(doc, "entry"), [])
        self.assertEqual(doc["symbol_status"]["SYN"]["status"], "conflict")

    def test_missing_slot_does_not_fill_or_invent_the_path(self):
        loaded = load_fixture(support.fixture("synthetic_round_trip.json"))
        bars = [
            dict(item)
            for item in loaded["bars"]
            if item["start"] != "2026-11-02T09:50:00"
        ]
        doc = run_bars(bars, support.approval(["SYN"], ["2026-11-02"]))
        self.assertEqual(support.of_type(doc, "entry"), [])
        self.assertEqual(doc["positions"], {})
        self.assertEqual(doc["symbol_status"]["SYN"]["status"], "gapped")

    def test_open_through_the_stop_is_not_an_entry(self):
        bars = [
            support.bar("SYN", "2026-11-02T09:30:00", "100.00", "102.00", "99.00", "101.00"),
            support.bar("SYN", "2026-11-02T09:35:00", "101.00", "103.00", "100.00", "102.00"),
            support.bar("SYN", "2026-11-02T09:40:00", "102.00", "104.00", "101.00", "103.00"),
            support.bar("SYN", "2026-11-02T09:45:00", "103.00", "106.00", "102.50", "105.00"),
            support.bar("SYN", "2026-11-02T09:50:00", "101.00", "106.00", "100.50", "101.20"),
        ]
        doc = run_bars(bars, support.approval(["SYN"], ["2026-11-02"]))
        self.assertEqual(support.of_type(doc, "entry"), [])
        self.assertEqual(doc["positions"], {})
        self.assertEqual(support.of_type(doc, "skip")[0]["reason"], "stop_not_below_entry")

    def test_intrabar_stop_uses_the_stop_not_the_low_or_the_close(self):
        bars = [
            support.bar("SYN", "2026-11-02T09:30:00", "100.00", "102.00", "99.00", "101.00"),
            support.bar("SYN", "2026-11-02T09:35:00", "101.00", "103.00", "100.00", "102.00"),
            support.bar("SYN", "2026-11-02T09:40:00", "102.00", "104.00", "101.00", "103.00"),
            support.bar("SYN", "2026-11-02T09:45:00", "103.00", "106.00", "102.50", "105.00"),
            support.bar("SYN", "2026-11-02T09:50:00", "105.20", "113.00", "1.00", "1.00"),
        ]
        doc = run_bars(bars, support.approval(["SYN"], ["2026-11-02"]))
        entry = support.of_type(doc, "entry")[0]
        exit_ = support.of_type(doc, "exit")[0]
        self.assertEqual(entry["entry_price"], "105.21")
        self.assertEqual(exit_["reason"], "stop_first")
        self.assertEqual(exit_["price"], "101.49")
        self.assertNotEqual(exit_["price"], "1.00")

    def test_historical_label_is_not_verification(self):
        path = os_path_join_tmp("historical.json")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(
                '{"note":"NOT VERIFIED invented prices labeled historical. Not market data.",'
                '"label":"historical","bars":[]}\n'
            )
        loaded = load_fixture(path)
        doc = run_bars(loaded["bars"], support.approval(["NODATA"], ["2026-11-02"], "historical"))
        report = build_report(doc)
        self.assertEqual(report["label"], "historical")
        self.assertFalse(report["data_verified"])
        self.assertTrue(report["label_is_caller_supplied"])
        self.assertEqual(doc["cash"], "10000.000")

    def test_mixed_source_is_refused(self):
        from paper_trader.errors import ScopeError

        bars = [support.bar("SYN", "2026-11-02T09:30:00", "10.00", "10.10", "9.90", "10.00")]
        bars[0]["source"] = "synthetic"
        with self.assertRaises(ScopeError):
            run_bars(bars, support.approval(["SYN"], ["2026-11-02"], "historical"))


def os_path_join_tmp(name):
    import os

    directory = support.workdir("historical-label")
    return os.path.join(directory, name)


if __name__ == "__main__":
    unittest.main()
