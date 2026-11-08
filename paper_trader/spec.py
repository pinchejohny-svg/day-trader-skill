"""Fixed rules for the offline opening-range paper engine.

Nothing here is a market claim, a broker fee schedule, or a data feed.
Constants are the whole strategy. A saved ledger stores this dictionary and
refuses to open if the engine's copy has changed.
"""

import datetime
import re
from decimal import Decimal, ROUND_FLOOR, ROUND_HALF_UP

from paper_trader.errors import ApprovalRequired, RunBoundExceeded

STRATEGY_ID = "orb15-5m-long-next-open-v1"
ACK = "simulated_offline_replay_only"
PROVENANCE = (
    "Reimplemented from trader-scanner/scan.py orb_hit (close above the "
    "opening-range high), trader-entry/entry.py evaluate (strict close, "
    "closed bars only), and trader-exit/exit.py compute (midpoint stop "
    "floored to the cent, 2R target half-up). Those modules are not imported."
)
FMT = "%Y-%m-%dT%H:%M:%S"
STAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,9}")

CENT = Decimal("0.01")
MILL = Decimal("0.001")
MAX_SYMBOLS = 20
MAX_BARS = 10000

# Strings so the saved assumptions document is exact and JSON-stable.
ASSUMPTIONS = {
    "bar_size": "5min",
    "cash_only": "true",
    "commission_per_share": "0.005",
    "daily_realized_loss_stop": "150.000",
    "exit_tie": "stop_first_if_both_levels_touched_after_the_open",
    "fill": "next_closed_bar_open_plus_slippage",
    "flat_by": "15:55",
    "gap": "open_through_a_level_uses_that_open_then_slippage",
    "intrabar_fill": "resting_stop_or_target_not_the_bar_extreme",
    "long_only": "true",
    "max_bars": "10000",
    "max_entries_per_day": "3",
    "max_position_pct_of_ledger_equity": "0.25",
    "max_symbols": "20",
    "min_risk_note": "0.05 percent of the fill, the same threshold as trader-exit MIN_RISK_PCT; that module is not imported",
    "min_risk_pct_of_entry": "0.05",
    "no_entries_after": "15:30",
    "opening_range": "15min_three_closed_5min_bars",
    "provenance": PROVENANCE,
    "ranking": "higher_signal_close_pct_over_range_high_then_symbol_ascending",
    "risk_pct_of_ledger_equity": "0.005",
    "run_realized_loss_stop": "300.000",
    "session_clock": "America/New_York_wall_clock_strings_without_a_timezone_database",
    "sizing_equity": "cash_plus_open_cost_bases_not_marks",
    "slippage_per_share": "0.01",
    "starting_cash": "10000.000",
    "stop": "opening_range_midpoint_floored_to_the_cent",
    "strategy_id": STRATEGY_ID,
    "target": "2R_half_up_from_slipped_fill_and_unslipped_stop",
    "whole_shares": "true",
}

STARTING_CASH = Decimal(ASSUMPTIONS["starting_cash"])
RISK_PCT = Decimal(ASSUMPTIONS["risk_pct_of_ledger_equity"])
MAX_POSITION_PCT = Decimal(ASSUMPTIONS["max_position_pct_of_ledger_equity"])
MAX_ENTRIES_PER_DAY = int(ASSUMPTIONS["max_entries_per_day"])
DAILY_LOSS = Decimal(ASSUMPTIONS["daily_realized_loss_stop"])
RUN_LOSS = Decimal(ASSUMPTIONS["run_realized_loss_stop"])
SLIPPAGE = Decimal(ASSUMPTIONS["slippage_per_share"])
COMMISSION = Decimal(ASSUMPTIONS["commission_per_share"])
MIN_RISK_PCT = Decimal(ASSUMPTIONS["min_risk_pct_of_entry"])


def q2(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def q2_floor(value):
    return Decimal(value).quantize(CENT, rounding=ROUND_FLOOR)


def q3(value):
    return Decimal(value).quantize(MILL, rounding=ROUND_HALF_UP)


def s2(value):
    return format(q2(value), "f")


def s3(value):
    return format(q3(value), "f")


def valid_stamp(stamp):
    if not isinstance(stamp, str) or STAMP_RE.fullmatch(stamp) is None:
        return False
    try:
        datetime.datetime.strptime(stamp, FMT)
    except ValueError:
        return False
    return True


def valid_date(text):
    if not isinstance(text, str):
        return False
    try:
        datetime.datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def valid_start(start):
    """A regular-session 5-minute bar start, 09:30 through 15:55 inclusive."""
    if not valid_stamp(start):
        return False
    dt = datetime.datetime.strptime(start, FMT)
    if dt.second != 0 or dt.minute % 5 != 0:
        return False
    clock = dt.time()
    if clock < datetime.time(9, 30) or clock > datetime.time(15, 55):
        return False
    return True


def add_minutes(stamp, minutes):
    dt = datetime.datetime.strptime(stamp, FMT)
    return (dt + datetime.timedelta(minutes=minutes)).strftime(FMT)


def normalize_approval(raw):
    """One scope approval. This is not an approval of any individual fill."""
    if not isinstance(raw, dict):
        raise ApprovalRequired("an approval record is required")
    if raw.get("strategy_id") != STRATEGY_ID:
        raise ApprovalRequired("strategy_id must be %s" % STRATEGY_ID)
    if raw.get("acknowledge") != ACK:
        raise ApprovalRequired(
            "acknowledge must be exactly %r" % ACK
        )
    if raw.get("per_trade_approval") is not False:
        raise ApprovalRequired(
            "set per_trade_approval to false; the human approves the scope, not each simulated fill"
        )
    label = raw.get("label")
    if label not in ("synthetic", "historical"):
        raise ApprovalRequired(
            "label must be synthetic or historical; forward paper mode is disabled"
        )
    dates = raw.get("session_dates")
    symbols = raw.get("symbols")
    if not isinstance(dates, list) or not dates:
        raise ApprovalRequired("session_dates must be a non-empty list")
    if not isinstance(symbols, list) or not symbols:
        raise ApprovalRequired("symbols must be a non-empty list")
    if len(symbols) > MAX_SYMBOLS:
        raise RunBoundExceeded("at most %d symbols in one bounded run" % MAX_SYMBOLS)
    norm_dates = []
    for item in dates:
        if not valid_date(item):
            raise ApprovalRequired("bad session date: %r" % (item,))
        norm_dates.append(item)
    if len(set(norm_dates)) != len(norm_dates):
        raise ApprovalRequired("duplicate session date")
    norm_symbols = []
    for item in symbols:
        if not isinstance(item, str) or SYMBOL_RE.fullmatch(item) is None:
            raise ApprovalRequired("bad symbol: %r" % (item,))
        norm_symbols.append(item)
    if len(set(norm_symbols)) != len(norm_symbols):
        raise ApprovalRequired("duplicate symbol")
    return {
        "acknowledge": ACK,
        "label": label,
        "per_trade_approval": False,
        "session_dates": sorted(norm_dates),
        "strategy_id": STRATEGY_ID,
        "symbols": sorted(norm_symbols),
    }
