"""Bounded replay into the existing engine and, when asked, the existing store.

Fill prices, stops, targets, and cash stay whatever ``run_bars`` already
computes. This module does not open a socket and does not place orders.
``StateStore.create`` is used only when the ledger file is absent.
``StateStore.resume`` is used when it exists. An existing file is not reset.

A weekend is a closed market. Any other date proceeds only when the run's
preapproved configuration says ``regular`` and ``early_close`` is ``none``.
That configuration is not an NYSE holiday calendar. Unknown sessions fail
closed. The symbol cap is enforced before any page is parsed.

Provider ``v`` is copied onto each bar as ``provider_volume``. The engine
does not rank or filter on volume, and this module does not claim that it
does. An injected or synthetic page stays ``synthetic`` on the ledger.
``adjusted: false`` is recorded and is not a verified provider basis.
A non-injected read stays ``unverified``. Missing or incompatible
provenance fails closed before a write.
"""

import datetime
import hashlib

from paper_trader.engine import fingerprint_of, run_bars
from paper_trader.errors import StateExists, StateMissing
from paper_trader.massive import (
    FULL_MARKET_UNIVERSE,
    MAX_SYMBOLS as FEED_SYMBOLS,
    RAW_STORAGE_POLICY,
    FeedRejected,
    ny_wall_from_unix_ms,
    parse_response,
    translate_results,
    validate_next_url,
)
from paper_trader.paths import kind, state_path
from paper_trader.persist import StateStore
from paper_trader.report import build_report
from paper_trader.spec import ACK, MAX_SYMBOLS as ENGINE_SYMBOLS, STRATEGY_ID, add_minutes


def bind_universe(symbols):
    """Refuse an empty or oversized list before any page or request."""
    if not isinstance(symbols, list) or not symbols:
        raise FeedRejected("symbol universe is empty")
    limit = FEED_SYMBOLS if FEED_SYMBOLS < ENGINE_SYMBOLS else ENGINE_SYMBOLS
    if len(symbols) > limit:
        raise FeedRejected("symbol budget exhausted")
    if len(set(symbols)) != len(symbols):
        raise FeedRejected("duplicate symbol")
    return limit


def resolve_session(session_dates, *, session_kind, early_close):
    """Fail closed unless this run was preapproved as a full regular session.

    Saturday and Sunday are closed. A weekday is not assumed to be a trading
    day. ``session_kind="regular"`` and ``early_close="none"`` are the
    caller's one-time bounded configuration, not a holiday table and not a
    per-trade confirmation. Anything else is an unknown session.
    """
    if not isinstance(session_dates, list) or not session_dates:
        raise FeedRejected("no session date")
    weekend = False
    for day in session_dates:
        if not isinstance(day, str):
            raise FeedRejected("session date is not text")
        try:
            parsed = datetime.date.fromisoformat(day)
        except ValueError:
            raise FeedRejected("session date is not a calendar day")
        if parsed.weekday() >= 5:
            weekend = True
    calendar = "not_a_full_calendar"
    if weekend:
        return {
            "early_close": "unknown_fail_closed",
            "holiday_calendar": calendar,
            "reason": "market_closed",
            "status": "waiting",
        }
    if session_kind != "regular" or early_close != "none":
        return {
            "early_close": "unknown_fail_closed",
            "holiday_calendar": calendar,
            "reason": "unknown_session",
            "status": "refused",
        }
    return {
        "early_close": "none_preapproved_not_a_calendar",
        "holiday_calendar": calendar,
        "reason": "preapproved_regular_session",
        "status": "proceed",
    }


def plan_resume(stored_inputs, additions):
    """Keep the stored prefix. Append only a strictly later, new suffix.

    A repeated bar is dropped. A different bar with the same start is a
    logged correction and is not applied. The stored row remains the one
    that will be replayed on the next resume.
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
        str(bar.get(key))
        for key in (
            "symbol", "start", "open", "high", "low", "close",
            "provider_volume", "provenance", "adjustment_basis",
            "note", "response_adjusted_field",
        )
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def assert_provenance(bars, label):
    """Refuse a write when durable provenance is missing or mixed."""
    if not isinstance(bars, list):
        raise FeedRejected("provenance is absent")
    for bar in bars:
        if not isinstance(bar, dict):
            raise FeedRejected("provenance is absent")
        provenance = bar.get("provenance")
        source = bar.get("source")
        basis = bar.get("adjustment_basis")
        note = bar.get("note")
        if provenance not in ("synthetic", "injected", "unverified_read"):
            raise FeedRejected("provenance is absent")
        if basis == "massive_unadjusted":
            raise FeedRejected("adjustment flag is not provenance")
        invented = provenance in ("synthetic", "injected")
        if invented:
            if source != "synthetic" or label != "synthetic" or basis != "synthetic":
                raise FeedRejected("invented provenance is incompatible")
            if note is not None and (not isinstance(note, str) or "SYNTHETIC" not in note.upper()):
                raise FeedRejected("invented provenance is incompatible")
        elif source != "historical" or label != "historical" or basis != "unverified":
            raise FeedRejected("read provenance is incompatible")


def rows_for_clock(results, clock, stored_starts):
    """Bars closed at ``clock`` and not yet late. Future bars are excluded.

    The window matches the engine: ``end <= clock <= end + 5 minutes``.
    A new ledger must include the 09:30 bar. A later poll does not resubmit
    a bar that has already fallen outside the window.
    """
    if not isinstance(clock, str):
        raise FeedRejected("clock is not a stamp")
    eligible = []
    rejected = []
    for row in results:
        if not isinstance(row, dict):
            rejected.append({"reason": "malformed_row"})
            continue
        try:
            start = ny_wall_from_unix_ms(row.get("t"))
        except FeedRejected as exc:
            rejected.append({"reason": str(exc)})
            continue
        end = add_minutes(start, 5)
        if clock < end:
            rejected.append({"reason": "not_closed", "start": start})
            continue
        if clock > add_minutes(end, 5):
            rejected.append({"reason": "late_at_clock", "start": start})
            continue
        eligible.append(row)
    if stored_starts:
        return eligible, rejected
    starts = []
    for row in eligible:
        starts.append(ny_wall_from_unix_ms(row.get("t"))[11:])
    if "09:30:00" not in starts:
        for row in eligible:
            rejected.append({
                "reason": "session_not_anchored_not_stored",
                "start": ny_wall_from_unix_ms(row.get("t")),
            })
        return [], rejected
    return eligible, rejected


def commit_ledger(state_name, bars, approval, *, create):
    """Create a missing ledger or resume the existing one. Never reset it.

    ``create`` is required to write a new file. An existing file is resumed
    and is not replaced by ``create``.
    """
    if not isinstance(state_name, str) or not state_name:
        raise FeedRejected("state name is required")
    path = state_path(state_name)
    seen = kind(path)
    if seen not in (None, "file"):
        raise FeedRejected("state path is not a new or regular ledger")
    if seen is None and not create:
        raise StateMissing("state file is absent")
    if seen == "file" and create:
        raise StateExists("state file already exists; resume it or choose a new name")
    assert_provenance(bars, approval["label"])
    with StateStore(path) as store:
        if seen is None:
            return store.create(bars, approval), [], "created"
        old = store.load()
        assert_provenance(old["inputs"], old["approval"]["label"])
        combined, rejected = plan_resume(old["inputs"], bars)
        assert_provenance(combined, approval["label"])
        frozen = store.resume(combined, approval)
        return frozen, rejected, "resumed"


def run_recorded(bodies_by_symbol, *, symbols, session_dates, session_kind,
                 early_close, mode="recorded_batch_replay", ingested_at=None,
                 clock_source="supplied", prior_inputs=None, state_name=None,
                 label="synthetic", create=False, provenance=None):
    """Translate pages and replay them. Pass ``state_name`` to persist.

    The operational entrypoint always passes ``state_name``. Calling this
    without one is an in-memory check, not a ledger. Network is not used.
    """
    bind_universe(symbols)
    session = resolve_session(session_dates, session_kind=session_kind, early_close=early_close)
    if session["status"] != "proceed":
        raise FeedRejected(session["reason"])
    if label not in ("synthetic", "historical"):
        raise FeedRejected("label is not recognized")
    if provenance is None:
        provenance = "synthetic" if label == "synthetic" else None
    if provenance is None:
        raise FeedRejected("provenance is absent")
    if provenance in ("synthetic", "injected") and label != "synthetic":
        raise FeedRejected("invented provenance is incompatible")
    if provenance == "unverified_read" and label != "historical":
        raise FeedRejected("read provenance is incompatible")
    if not isinstance(bodies_by_symbol, dict):
        raise FeedRejected("bodies must be a mapping of symbol to pages")
    if set(bodies_by_symbol) - set(symbols):
        raise FeedRejected("page symbol is outside the predeclared universe")
    bars = []
    rejections = []
    assumptions = []
    adjusted = {}
    clocks = set()
    dataset_complete = True
    for symbol in symbols:
        pages = bodies_by_symbol.get(symbol, [])
        if isinstance(pages, str):
            pages = [pages]
        if not isinstance(pages, list):
            raise FeedRejected("pages for a symbol must be a list")
        for page in pages:
            parsed = parse_response(page)
            if parsed.get("provenance") not in (None, provenance):
                raise FeedRejected("provenance is incompatible")
            note = parsed.get("note")
            if isinstance(note, str) and "SYNTHETIC" in note.upper() and provenance == "unverified_read":
                raise FeedRejected("invented provenance is incompatible")
            basis = "synthetic" if provenance in ("synthetic", "injected") else "unverified"
            translated, rejected, notes = translate_results(
                parsed["results"],
                symbol=symbol,
                mode=mode,
                source=label,
                ingested_at=ingested_at,
                adjustment_basis=basis,
                clock_source=clock_source,
                provenance=provenance,
                note=note,
                response_adjusted_field=parsed["adjusted_field"],
            )
            bars.extend(translated)
            rejections.extend(rejected)
            assumptions.extend(notes)
            for bar in translated:
                clocks.add(bar["observed_at"])
            adjusted[symbol] = {
                "adjustment_basis": basis,
                "request_adjusted": parsed["request_adjusted"],
                "response_adjusted_field": parsed["adjusted_field"],
            }
            nxt = parsed.get("next_url")
            if not nxt:
                continue
            dataset_complete = False
            try:
                validate_next_url(nxt)
            except FeedRejected:
                rejections.append({"reason": "next_url_rejected", "symbol": symbol})
            else:
                rejections.append({"reason": "pagination_not_followed", "symbol": symbol})
    if prior_inputs is not None:
        bars, resume_rejections = plan_resume(prior_inputs, bars)
        rejections.extend(resume_rejections)
    assert_provenance(bars, label)
    approval = _approval(symbols, session_dates, label)
    action = "memory"
    if state_name is not None:
        frozen, resume_rejections, action = commit_ledger(
            state_name, bars, approval, create=create,
        )
        rejections.extend(resume_rejections)
    else:
        frozen = run_bars(bars, approval)
    report = build_report(frozen)
    return _view(
        frozen, report, rejections, assumptions, adjusted, session, mode,
        action, dataset_complete, sorted(clocks),
    )


def _approval(symbols, session_dates, label):
    return {
        "acknowledge": ACK,
        "label": label,
        "per_trade_approval": False,
        "session_dates": list(session_dates),
        "strategy_id": STRATEGY_ID,
        "symbols": list(symbols),
    }


def _view(frozen, report, rejections, assumptions, adjusted, session, mode,
          action, dataset_complete, clocks):
    selection = []
    for event in frozen["events"]:
        if event.get("type") not in ("signal", "entry", "exit", "skip", "pause"):
            continue
        selection.append({
            "event_time": event.get("event_time"),
            "observed_at": event.get("observed_at"),
            "price_source": event.get("price_source"),
            "fee": event.get("fee"),
            "reason": event.get("reason"),
            "symbol": event.get("symbol"),
            "type": event.get("type"),
        })
    starts = [
        bar.get("start") for bar in frozen["inputs"]
        if isinstance(bar, dict) and isinstance(bar.get("start"), str)
    ]
    return {
        "adjustment": adjusted,
        "automated_selection": "existing_engine_rank_within_predeclared_symbols",
        "broker": "absent",
        "cash": report["cash"],
        "clocks": clocks,
        "cursor": max(starts) if starts else None,
        "dataset_complete": dataset_complete,
        "drawdown": report["drawdown"],
        "equity": report["equity_value"],
        "fill_math": "unchanged_existing_engine",
        "fills_are": report["fills_are"],
        "freshness": {
            "assumptions": sorted(set(assumptions)),
            "bar_finality": "not_guaranteed",
            "late_or_stale_warnings": [
                item["reason"] for item in report["warnings"]
                if item["reason"] in ("late_bar", "stale_bar", "not_closed")
            ],
            "mode": mode,
            "simulated_fill_time_is": "next_bar_open_inside_the_engine_not_a_live_print",
        },
        "full_market_contract": FULL_MARKET_UNIVERSE,
        "full_market_universe": False,
        "incomplete": report["incomplete"],
        "incomplete_reasons": report["incomplete_reasons"],
        "adjustment_basis": sorted({
            bar.get("adjustment_basis") for bar in frozen["inputs"] if isinstance(bar, dict)
        }),
        "bar_source": sorted({
            bar.get("source") for bar in frozen["inputs"] if isinstance(bar, dict)
        }),
        "data_verified": False,
        "invocation": "one_replay",
        "label": frozen["label"],
        "licensed": "not_asserted",
        "market_data": False,
        "provenance": sorted({
            bar.get("provenance") for bar in frozen["inputs"] if isinstance(bar, dict)
        }),
        "network": "not_called",
        "orders": [],
        "performance": {
            "cash": report["cash"],
            "drawdown": report["drawdown"],
            "equity": report["equity_value"],
            "realized_pnl": report["realized_pnl"],
        },
        "persistence": action,
        "positions": report["open_positions"],
        "profitability_claim": False,
        "raw_storage": RAW_STORAGE_POLICY,
        "ready": "wired_inert_network_disabled",
        "request": request_fields(),
        "realized_pnl": report["realized_pnl"],
        "rejections": rejections,
        "restart": "stored_prefix_plus_later_suffix",
        "running": False,
        "scan": {
            "bars_submitted": len(frozen["inputs"]),
            "dataset_complete": dataset_complete,
            "full_market_universe": False,
            "symbols": list(frozen["approval"]["symbols"]),
        },
        "selection": selection,
        "session": session,
        "streaming": "not_implemented_batch_replay_of_stored_prefix_plus_later_suffix",
        "volume_filter": False,
        "warnings": report["warnings"],
        "fingerprint": fingerprint_of(frozen),
    }


def request_fields():
    """Stable provenance fields. No secret and no URL."""
    return {"adjusted": "false", "limit": "5000", "sort": "asc", "timespan": "minute", "multiplier": "5"}


def waiting_report(session):
    """A closed or unknown session. No ledger write and no invented trades."""
    return {
        "broker": "absent",
        "cash": None,
        "dataset_complete": False,
        "equity": None,
        "full_market_universe": False,
        "network": "not_called",
        "orders": [],
        "persistence": "not_written",
        "positions": [],
        "profitability_claim": False,
        "running": False,
        "selection": [],
        "session": session,
        "status": session["status"],
        "volume_filter": False,
    }
