"""Bounded local replay of Massive-shaped pages through the existing engine.

This module does not open a socket, does not read a credential, and does not
write a ledger. It does not place orders. A file that already exists is not
opened and is not reset. Restart is a full replay of the stored bars plus a
strictly later suffix, which is what the existing store already requires.
There is no streaming cursor and no fresh-forward mode.

The holiday and early-close calendars are not implemented. A weekend is
refused. Any other date is refused unless the caller asserts
``human_asserts_regular_nyse_session``. That assertion is not a check
against the NYSE calendar. The engine's incomplete flags are copied through
and are never forced to a full session.

Fill prices, stops, targets, and cash are whatever ``run_bars`` already
computes. This module does not change them.
"""

import datetime
import hashlib

from paper_trader.engine import fingerprint_of, run_bars
from paper_trader.massive import (
    FULL_MARKET_UNIVERSE,
    RAW_STORAGE_POLICY,
    FeedRejected,
    parse_response,
    translate_results,
)
from paper_trader.report import build_report
from paper_trader.spec import ACK, STRATEGY_ID

SESSION_ASSERTION = "human_asserts_regular_nyse_session"


def assert_regular_weekday(session_dates, session_assertion):
    """Refuse a closed weekend and any date the human did not assert."""
    if session_assertion != SESSION_ASSERTION:
        raise FeedRejected("regular session was not asserted")
    if not isinstance(session_dates, list) or not session_dates:
        raise FeedRejected("no session date")
    for day in session_dates:
        if not isinstance(day, str):
            raise FeedRejected("session date is not text")
        try:
            parsed = datetime.date.fromisoformat(day)
        except ValueError:
            raise FeedRejected("session date is not a calendar day")
        if parsed.weekday() >= 5:
            raise FeedRejected("weekend session is refused")
    return {
        "early_close": "not_detected_engine_incomplete_flags_remain",
        "holiday_calendar": "not_implemented",
        "session_assertion": SESSION_ASSERTION,
    }


def plan_resume(stored_inputs, additions):
    """Keep the stored prefix. Append only a strictly later, new suffix.

    A repeated bar is dropped. A different bar with the same start is a
    logged correction and is not applied. Nothing is written.
    """
    if not isinstance(stored_inputs, list):
        raise FeedRejected("stored inputs are not a list")
    kept = [dict(bar) for bar in stored_inputs]
    if not additions:
        return kept, []
    watermark = ""
    for bar in kept:
        start = bar.get("start") if isinstance(bar, dict) else None
        if isinstance(start, str) and start > watermark:
            watermark = start
    seen = {}
    for bar in kept:
        if isinstance(bar, dict):
            seen[(bar.get("symbol"), bar.get("start"))] = _price_key(bar)
    suffix = []
    rejections = []
    for bar in additions:
        if not isinstance(bar, dict) or not isinstance(bar.get("start"), str):
            rejections.append({"reason": "suffix_bar_missing_start"})
            continue
        ident = (bar.get("symbol"), bar.get("start"))
        digest = _price_key(bar)
        if ident in seen:
            if seen[ident] == digest:
                rejections.append({"reason": "restart_duplicate", "symbol": bar.get("symbol")})
            else:
                rejections.append({
                    "reason": "correction_not_applied",
                    "replacement_hash": digest,
                    "symbol": bar.get("symbol"),
                })
            continue
        if watermark and bar["start"] <= watermark:
            rejections.append({"reason": "not_after_watermark", "symbol": bar.get("symbol")})
            continue
        seen[ident] = digest
        suffix.append(dict(bar))
    return kept + suffix, rejections


def _price_key(bar):
    text = "|".join(
        str(bar.get(key)) for key in ("symbol", "start", "open", "high", "low", "close")
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_recorded(bodies_by_symbol, *, symbols, session_dates, session_assertion,
                 mode="recorded_batch_replay", ingested_at=None, prior_inputs=None):
    """Translate fixture bodies and replay them. Network is not called."""
    session = assert_regular_weekday(session_dates, session_assertion)
    if not isinstance(bodies_by_symbol, dict):
        raise FeedRejected("bodies must be a mapping of symbol to pages")
    if set(bodies_by_symbol) - set(symbols):
        raise FeedRejected("page symbol is outside the predeclared universe")
    bars = []
    rejections = []
    assumptions = []
    adjusted = {}
    for symbol in symbols:
        pages = bodies_by_symbol.get(symbol, [])
        if isinstance(pages, str):
            pages = [pages]
        if not isinstance(pages, list):
            raise FeedRejected("pages for a symbol must be a list")
        for page in pages:
            parsed = parse_response(page)
            if parsed.get("next_url"):
                rejections.append({
                    "reason": "pagination_not_followed_offline",
                    "symbol": symbol,
                })
                continue
            translated, rejected, notes = translate_results(
                parsed["results"],
                symbol=symbol,
                mode=mode,
                source="synthetic",
                ingested_at=ingested_at,
            )
            bars.extend(translated)
            rejections.extend(rejected)
            assumptions.extend(notes)
            adjusted[symbol] = {
                "request_adjusted": parsed["request_adjusted"],
                "response_adjusted_field": parsed["adjusted_field"],
            }
    resume_rejections = []
    if prior_inputs is not None:
        bars, resume_rejections = plan_resume(prior_inputs, bars)
        rejections.extend(resume_rejections)
    approval = {
        "acknowledge": ACK,
        "label": "synthetic",
        "per_trade_approval": False,
        "session_dates": list(session_dates),
        "strategy_id": STRATEGY_ID,
        "symbols": list(symbols),
    }
    frozen = run_bars(bars, approval)
    report = build_report(frozen)
    return _view(frozen, report, rejections, assumptions, adjusted, session, mode)


def _view(frozen, report, rejections, assumptions, adjusted, session, mode):
    selection = []
    for event in frozen["events"]:
        if event.get("type") not in ("signal", "entry", "exit", "skip", "pause"):
            continue
        selection.append({
            "reason": event.get("reason"),
            "symbol": event.get("symbol"),
            "type": event.get("type"),
        })
    return {
        "adjustment": adjusted,
        "broker": "absent",
        "cash": report["cash"],
        "equity": report["equity_value"],
        "fill_math": "unchanged_existing_engine",
        "fills_are": report["fills_are"],
        "freshness": {
            "assumptions": sorted(set(assumptions)),
            "bar_finality": "not_guaranteed",
            "late_or_stale_warnings": [
                item["reason"] for item in report["warnings"] if item["reason"] in ("late_bar", "stale_bar", "not_closed")
            ],
            "mode": mode,
            "observed_at_is": "bar_end_for_recorded_replay" if mode == "recorded_batch_replay" else "ingested_at",
            "simulated_fill_time_is": "next_bar_open_inside_the_engine",
        },
        "full_market_universe": FULL_MARKET_UNIVERSE,
        "incomplete": report["incomplete"],
        "incomplete_reasons": report["incomplete_reasons"],
        "licensed": "not_asserted",
        "network": "not_called",
        "orders": [],
        "positions": report["open_positions"],
        "profitability_claim": False,
        "raw_storage": RAW_STORAGE_POLICY,
        "label": "synthetic",
        "market_data": False,
        "ready": "inert_source_present_network_disabled",
        "request": request_fields(),
        "realized_pnl": report["realized_pnl"],
        "rejections": rejections,
        "running": False,
        "scan": {
            "bars_submitted": len(frozen["inputs"]),
            "full_market_universe": FULL_MARKET_UNIVERSE,
            "symbols": list(frozen["approval"]["symbols"]),
        },
        "selection": selection,
        "session": session,
        "streaming": "not_implemented_batch_replay_of_stored_prefix_plus_later_suffix",
        "warnings": report["warnings"],
        "fingerprint": fingerprint_of(frozen),
    }


def request_fields():
    """Stable provenance fields. No secret and no URL."""
    return {"adjusted": "false", "limit": "5000", "sort": "asc", "timespan": "minute", "multiplier": "5"}
