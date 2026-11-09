"""Causal offline replay. No clocks, no network, no skill imports.

Signal and fill are different bars. A fixture is sorted by bar start, so
file order cannot peek ahead. Resume may only append strictly later bars.

Within one completed bar the order is fixed: open-known exits of positions
already held, then new entries at that open, then intrabar stop and target
checks. The close is never a fill.
"""

import copy
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_FLOOR

from paper_trader.errors import DataError, EngineInvariant, RunBoundExceeded, ScopeError
from paper_trader.jsonutil import canonical
from paper_trader.spec import (
    ACK,
    ASSUMPTIONS,
    CENT,
    COMMISSION,
    DAILY_LOSS,
    MAX_BARS,
    MAX_ENTRIES_PER_DAY,
    MAX_POSITION_PCT,
    MIN_RISK_PCT,
    RISK_PCT,
    RUN_LOSS,
    SLIPPAGE,
    STARTING_CASH,
    STRATEGY_ID,
    add_minutes,
    q2,
    q2_floor,
    q3,
    s2,
    s3,
    valid_stamp,
    valid_start,
    normalize_approval,
)


def shares_for(budget, per_share):
    if budget <= 0 or per_share <= 0:
        return 0
    return int((budget / per_share).to_integral_value(rounding=ROUND_FLOOR))


def slip_exit(raw):
    """Adverse one cent, but never a non-positive invented price."""
    raw = q2(raw)
    fill = raw - SLIPPAGE
    if fill >= CENT:
        return q2(fill), False
    return raw, True


def size_one(equity, cash, entry, stop):
    """Whole shares. equity and cash are ledger figures, not marked prices."""
    entry = q2(entry)
    stop = q2(stop)
    base = {
        "bound_by": None,
        "cash_available": s3(cash),
        "cost": "0.000",
        "entry": s2(entry),
        "equity": s3(equity),
        "fee": "0.000",
        "planned_risk": s3(equity * RISK_PCT),
        "qty": 0,
        "shares_cap": 0,
        "shares_cash": 0,
        "shares_risk": 0,
        "stop": s2(stop),
        "target": None,
    }
    if entry <= stop:
        base["bound_by"] = "stop_not_below_entry"
        return base
    price_risk = entry - stop
    if price_risk < entry * MIN_RISK_PCT / Decimal("100"):
        base["bound_by"] = "risk_too_tight"
        return base
    risk_dist = entry - (stop - SLIPPAGE) + (COMMISSION * 2)
    unit = entry + COMMISSION
    planned = equity * RISK_PCT
    cap = equity * MAX_POSITION_PCT
    shares_risk = shares_for(planned, risk_dist)
    shares_cap = shares_for(cap, unit)
    shares_cash = shares_for(cash, unit)
    qty = min(shares_risk, shares_cap, shares_cash)
    if qty < 0:
        qty = 0
    bound = None
    for name, count in (
        ("risk", shares_risk),
        ("position_cap", shares_cap),
        ("cash", shares_cash),
    ):
        if count == qty:
            bound = name
            break
    target = q2(entry + (price_risk * 2))
    cost = q3(qty * entry + qty * COMMISSION)
    base.update(
        {
            "bound_by": bound,
            "cost": s3(cost),
            "fee": s3(qty * COMMISSION),
            "qty": qty,
            "shares_cap": shares_cap,
            "shares_cash": shares_cash,
            "shares_risk": shares_risk,
            "target": s2(target),
        }
    )
    return base


def size_batch(candidates, equity, cash, entries_used):
    """Rank, then reserve cash and entry slots in that order.

    candidates items need symbol, entry, stop, pct (Decimal). pct is only a
    rank key. A zero-share plan does not consume a daily entry slot.
    """
    ranked = sorted(candidates, key=lambda item: (-item["pct"], item["symbol"]))
    reserved = q3(0)
    slots = entries_used
    plans = []
    for index, cand in enumerate(ranked, start=1):
        if slots >= MAX_ENTRIES_PER_DAY:
            plans.append(
                {
                    "action": "skip",
                    "bound_by": "max_entries_per_day",
                    "candidate": cand,
                    "qty": 0,
                    "rank": index,
                    "reason": "max_entries_per_day",
                }
            )
            continue
        plan = size_one(equity, q3(cash - reserved), cand["entry"], cand["stop"])
        plan["action"] = "enter" if plan["qty"] > 0 else "skip"
        plan["candidate"] = cand
        plan["rank"] = index
        plan["reason"] = "orb_next_open" if plan["qty"] > 0 else plan["bound_by"]
        if plan["qty"] > 0:
            reserved = q3(reserved + Decimal(plan["cost"]))
            slots += 1
        plans.append(plan)
    return plans


def run_bars(inputs, approval):
    """Replay inputs under one approved scope. Returns a JSON-ready ledger."""
    approval = normalize_approval(approval)
    state = _fold(inputs, approval)
    return _freeze(state)


def fingerprint_of(frozen):
    body = {key: value for key, value in frozen.items() if key != "fingerprint"}
    return hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()


def _fold(inputs, approval):
    if not isinstance(inputs, list):
        raise DataError("inputs must be a list of bars")
    if len(inputs) > MAX_BARS:
        raise RunBoundExceeded("at most %d bars in one bounded run" % MAX_BARS)
    copied = []
    approved_symbols = set(approval["symbols"])
    approved_dates = set(approval["session_dates"])
    for bar in inputs:
        if not isinstance(bar, dict):
            raise DataError("each bar must be an object")
        symbol = bar.get("symbol")
        if not isinstance(symbol, str):
            raise DataError("each bar needs a symbol string")
        if symbol not in approved_symbols:
            raise ScopeError("symbol %s is outside the approved scope" % symbol)
        start = bar.get("start")
        if isinstance(start, str) and valid_stamp(start) and start[:10] not in approved_dates:
            raise ScopeError("bar date %s is outside the approved scope" % start[:10])
        source = bar.get("source")
        if source != approval["label"]:
            raise ScopeError("bar source does not match the approved label")
        copied.append(dict(bar))

    state = {
        "approval": approval,
        "as_of": None,
        "cash": STARTING_CASH,
        "coverage": {symbol: [] for symbol in approval["symbols"]},
        "event_ids": {},
        "events": [],
        "inputs": copied,
        "peak": STARTING_CASH,
        "positions": {},
        "realized": q3(0),
        "max_drawdown": q3(0),
        "symbol_status": {},
        "warnings": [],
        "warning_ids": set(),
    }
    pending = {}
    fired = set()
    cursor = {}
    ordered = sorted(copied, key=lambda bar: (_sort_start(bar), str(bar.get("symbol"))))
    groups = []
    for bar in ordered:
        if not groups or groups[-1][0] != bar.get("start"):
            groups.append((bar.get("start"), [bar]))
        else:
            groups[-1][1].append(bar)

    for start, group in groups:
        accepted = []
        rejected = {}
        group_prior = {}
        for bar in group:
            symbol = bar.get("symbol")
            if symbol not in group_prior:
                group_prior[symbol] = copy.deepcopy(cursor.get(symbol))
            decision, working = _consider(state, cursor, bar)
            if decision == "accept":
                accepted.append(working)
                _remember(cursor, working)
            elif decision == "ignore":
                _warn(state, symbol, bar.get("start"), "duplicate_ignored", "info")
            elif decision == "already_blocked":
                continue
            else:
                if decision == "conflict":
                    prior = group_prior.get(symbol)
                    if prior is None:
                        cursor.pop(symbol, None)
                    else:
                        cursor[symbol] = prior
                    accepted = [item for item in accepted if item["symbol"] != symbol]
                rejected[symbol] = decision
                _warn(state, symbol, bar.get("start"), decision, "block")
                _block(state, symbol, decision, decision)
                _note_late_position(state, symbol, decision)

        accepted_by = {bar["symbol"]: bar for bar in accepted}
        _pause_unresolved(state, pending, start, accepted_by, rejected)
        _open_exits(state, accepted_by)
        _entries(state, pending, fired, accepted_by, start)
        _intrabar_exits(state, accepted_by)
        _marks(state, accepted_by)
        _signals(state, pending, fired, cursor, accepted)
        if accepted:
            state["as_of"] = add_minutes(start, 5)
        _refresh_marks(state)
        _drawdown(state)
        _check_ledger(state)

    for symbol in approval["symbols"]:
        if symbol not in state["symbol_status"]:
            state["symbol_status"][symbol] = {
                "entry_blocked": True,
                "reason": "no_bars",
                "status": "no_data",
            }
    _refresh_marks(state)
    _drawdown(state)
    _check_ledger(state)
    return state


def _sort_start(bar):
    start = bar.get("start")
    if isinstance(start, str):
        return start
    return ""


def _consider(state, cursor, raw):
    symbol = raw.get("symbol")
    start = raw.get("start")
    if symbol in state["symbol_status"] and state["symbol_status"][symbol].get("entry_blocked"):
        # A hole already failed this symbol. Later bars are not a new tape
        # and are not used to invent an exit.
        return "already_blocked", None
    if not isinstance(start, str) or not valid_start(start):
        return "off_grid", None
    observed = raw.get("observed_at")
    end = add_minutes(start, 5)
    if observed is None:
        observed = end
    elif not valid_stamp(observed):
        return "bad_observed_at", None
    if observed < end:
        return "not_closed", None
    if observed > add_minutes(end, 5):
        return "late_bar", None
    prices = _prices(raw)
    if prices is None:
        return "malformed_bar", None
    working = {
        "close": prices["close"],
        "end": end,
        "high": prices["high"],
        "low": prices["low"],
        "observed_at": observed,
        "open": prices["open"],
        "source": raw.get("source"),
        "start": start,
        "symbol": symbol,
    }
    info = cursor.get(symbol)
    if info is None:
        if start[11:] != "09:30:00":
            return "session_not_anchored", None
        return "accept", working
    if start[:10] != info["date"]:
        position = state["positions"].get(symbol)
        if position is not None and start[:10] > position["session_date"]:
            position["exit_blocked"] = True
            position["mark_status"] = "carried_incomplete"
            position["stale_hold"] = True
            _block(state, symbol, "carried", "later_session_ignored")
            return "carried_ignored", None
        if start[:10] < info["date"] or start < info["last_start"]:
            return "out_of_order", None
        if start[11:] != "09:30:00":
            return "session_not_anchored", None
        info["tape"] = []
        info["date"] = start[:10]
        info["last_start"] = None
        return "accept", working
    if start < info["last_start"]:
        return "out_of_order", None
    if start == info["last_start"]:
        prev = info["tape"][-1]
        if _same_prices(prev, working):
            return "ignore", None
        return "conflict", None
    expected = add_minutes(info["last_start"], 5)
    if start != expected:
        return "gapped", None
    if info.get("observed_at") and observed < info["observed_at"]:
        return "out_of_order", None
    return "accept", working


def _prices(raw):
    from decimal import Decimal, InvalidOperation

    parsed = {}
    for key in ("open", "high", "low", "close"):
        value = raw.get(key)
        if not isinstance(value, str):
            return None
        try:
            number = Decimal(value)
        except InvalidOperation:
            return None
        if not number.is_finite() or number <= 0:
            return None
        parsed[key] = q2(number)
    if parsed["low"] > parsed["open"] or parsed["open"] > parsed["high"]:
        return None
    if parsed["low"] > parsed["close"] or parsed["close"] > parsed["high"]:
        return None
    if parsed["low"] > parsed["high"]:
        return None
    if "volume" in raw and raw["volume"] is not None:
        volume = raw["volume"]
        if isinstance(volume, bool) or not isinstance(volume, int) or volume < 0:
            return None
    return parsed


def _same_prices(left, right):
    return all(left[key] == right[key] for key in ("open", "high", "low", "close"))


def _remember(cursor, working):
    symbol = working["symbol"]
    info = cursor.get(symbol)
    if info is None or info["date"] != working["start"][:10]:
        info = {
            "date": working["start"][:10],
            "last_start": None,
            "observed_at": None,
            "tape": [],
        }
        cursor[symbol] = info
    info["tape"].append(working)
    info["last_start"] = working["start"]
    info["observed_at"] = working["observed_at"]
    info["date"] = working["start"][:10]


_HOLES = {
    "bad_observed_at",
    "conflict",
    "gapped",
    "late_bar",
    "malformed_bar",
    "not_closed",
    "off_grid",
    "out_of_order",
    "session_not_anchored",
}


def _block(state, symbol, status, reason):
    position = state["positions"].get(symbol)
    if position is not None and status in _HOLES:
        position["exit_blocked"] = True
        position["stale_hold"] = True
        if position.get("mark_status") != "carried_incomplete":
            position["mark_status"] = "stale"
    current = state["symbol_status"].get(symbol)
    if current and current.get("entry_blocked"):
        return
    state["symbol_status"][symbol] = {
        "entry_blocked": True,
        "reason": reason,
        "status": status,
    }


def _ensure_ok(state, symbol):
    current = state["symbol_status"].get(symbol)
    if current is None:
        state["symbol_status"][symbol] = {
            "entry_blocked": False,
            "reason": "",
            "status": "ok",
        }


def _note_late_position(state, symbol, decision):
    if decision != "late_bar":
        return
    position = state["positions"].get(symbol)
    if position is None:
        return
    position["mark_status"] = "stale"
    position["stale_hold"] = True


def _warn(state, symbol, start, reason, severity):
    if not isinstance(start, str):
        start = ""
    ident = "warn:%s:%s:%s" % (symbol, start, reason)
    if ident in state["warning_ids"]:
        return
    state["warning_ids"].add(ident)
    state["warnings"].append(
        {
            "id": ident,
            "reason": reason,
            "severity": severity,
            "start": start,
            "symbol": symbol,
        }
    )


def _event(state, event):
    ident = event["id"]
    previous = state["event_ids"].get(ident)
    if previous is not None:
        if previous != event:
            raise EngineInvariant("event id %s reused with a different body" % ident)
        return
    state["event_ids"][ident] = event
    state["events"].append(event)


def _pause_unresolved(state, pending, start, accepted_by, rejected):
    """Withhold a fill when the next bar is missing or stale. Never an exit."""
    if not isinstance(start, str):
        for symbol, signal in list(pending.items()):
            _pause(state, pending, signal, "missing_bar")
        return
    for symbol, signal in list(pending.items()):
        if signal["fill_start"] < start:
            _pause(state, pending, signal, "missing_bar")
        elif signal["fill_start"] == start and symbol not in accepted_by:
            reason = {
                "late_bar": "stale_bar",
                "not_closed": "not_closed",
                "gapped": "missing_bar",
            }.get(rejected.get(symbol), "missing_bar")
            _pause(state, pending, signal, reason)


def _pause(state, pending, signal, reason):
    _event(
        state,
        {
            "bar_end": signal["signal_end"],
            "bar_start": signal["signal_start"],
            "decision_time": signal["signal_end"],
            "event_time": signal["signal_end"],
            "fill": "paused",
            "id": "pause:%s:%s:%s" % (signal["symbol"], signal["signal_start"], reason),
            "observed_at": signal["signal_end"],
            "reason": reason,
            "session_date": signal["session_date"],
            "symbol": signal["symbol"],
            "type": "pause",
        },
    )
    pending.pop(signal["symbol"], None)


def _open_exits(state, accepted_by):
    for symbol in sorted(list(state["positions"])):
        position = state["positions"].get(symbol)
        bar = accepted_by.get(symbol)
        if position is None or bar is None or position.get("exit_blocked"):
            continue
        if bar["start"][:10] != position["session_date"]:
            continue
        kind, raw = _open_exit(position, bar)
        if kind is not None:
            _apply_exit(state, position, bar, kind, raw)


def _entries(state, pending, fired, accepted_by, start):
    if not isinstance(start, str):
        return
    session_date = start[:10]
    ready = []
    for symbol, signal in pending.items():
        if signal["fill_start"] != start:
            continue
        bar = accepted_by.get(symbol)
        if bar is None:
            continue
        if _blocked(state, symbol):
            _skip(state, signal, bar, "symbol_feed_blocked")
            pending.pop(symbol, None)
            fired.add((symbol, signal["session_date"]))
            continue
        ready.append((signal, bar))
    block = _entry_block_reason(state, session_date)
    if block is not None:
        for signal, bar in ready:
            _skip(state, signal, bar, block)
            pending.pop(signal["symbol"], None)
            fired.add((signal["symbol"], signal["session_date"]))
        return
    candidates = []
    bars_by_symbol = {}
    for signal, bar in ready:
        entry = q2(bar["open"] + SLIPPAGE)
        candidates.append(
            {
                "entry": entry,
                "or_high": signal["or_high"],
                "or_low": signal["or_low"],
                "pct": signal["pct"],
                "raw_open": bar["open"],
                "session_date": signal["session_date"],
                "signal_close": signal["signal_close"],
                "signal_end": signal["signal_end"],
                "signal_start": signal["signal_start"],
                "source": signal["source"],
                "stop": signal["stop"],
                "symbol": signal["symbol"],
            }
        )
        bars_by_symbol[signal["symbol"]] = bar
    equity = _ledger(state)
    plans = size_batch(candidates, equity, state["cash"], _entries_on(state, session_date))
    for plan in plans:
        cand = plan["candidate"]
        bar = bars_by_symbol[cand["symbol"]]
        pending.pop(cand["symbol"], None)
        fired.add((cand["symbol"], cand["session_date"]))
        if plan["action"] != "enter":
            _skip(state, cand, bar, plan["reason"], plan)
            continue
        _apply_entry(state, cand, bar, plan)


def _intrabar_exits(state, accepted_by):
    for symbol in sorted(list(state["positions"])):
        position = state["positions"].get(symbol)
        bar = accepted_by.get(symbol)
        if position is None or bar is None or position.get("exit_blocked"):
            continue
        if bar["start"][:10] != position["session_date"]:
            continue
        if position.get("opened_at") == bar["observed_at"] and position.get("opened_start") == bar["start"]:
            # The open of this bar was the entry. Intrabar levels can still
            # stop the new position out; the open itself was already accepted.
            pass
        kind, raw = _intrabar_exit(position, bar)
        if kind is not None:
            _apply_exit(state, position, bar, kind, raw)


def _marks(state, accepted_by):
    for symbol, bar in accepted_by.items():
        _ensure_ok(state, symbol)
        state["coverage"][symbol].append(bar["start"])
        position = state["positions"].get(symbol)
        if position is None or position.get("exit_blocked") or position.get("stale_hold"):
            continue
        if bar["start"][:10] != position["session_date"]:
            continue
        position["last_close"] = bar["close"]
        position["last_close_at"] = bar["end"]
        position["mark_status"] = "fresh"


def _signals(state, pending, fired, cursor, accepted):
    for bar in accepted:
        symbol = bar["symbol"]
        if symbol in state["positions"] or symbol in pending:
            continue
        if _blocked(state, symbol):
            continue
        if (symbol, bar["start"][:10]) in fired:
            continue
        signal = _signal_from(cursor[symbol]["tape"], bar)
        if signal is None:
            continue
        fired.add((symbol, signal["session_date"]))
        pending[symbol] = signal
        _event(
            state,
            {
                "bar_end": signal["signal_end"],
                "bar_start": signal["signal_start"],
                "decision_time": signal["signal_end"],
                "event_time": signal["signal_end"],
                "id": "signal:%s:%s" % (symbol, signal["signal_start"]),
                "observed_at": bar["observed_at"],
                "or_high": s2(signal["or_high"]),
                "or_low": s2(signal["or_low"]),
                "pct_over": format(signal["pct"], "f"),
                "reason": "close_above_opening_range_high",
                "session_date": signal["session_date"],
                "signal_close": s2(signal["signal_close"]),
                "stop": s2(signal["stop"]),
                "symbol": symbol,
                "type": "signal",
            },
        )


def _signal_from(tape, bar):
    from decimal import Decimal

    session_date = bar["start"][:10]
    if bar["end"] > session_date + "T15:30:00":
        return None
    if bar["start"] <= session_date + "T09:40:00":
        return None
    wanted = [
        session_date + "T09:30:00",
        session_date + "T09:35:00",
        session_date + "T09:40:00",
    ]
    have = [item["start"] for item in tape]
    if have[:3] != wanted:
        return None
    if have != _contiguous(have[0], len(have)):
        return None
    range_bars = tape[:3]
    or_high = max(item["high"] for item in range_bars)
    or_low = min(item["low"] for item in range_bars)
    if or_high <= or_low:
        return None
    later = tape[3:]
    earliest = None
    for item in later:
        if item["close"] > or_high:
            earliest = item
            break
    if earliest is None or earliest["start"] != bar["start"]:
        return None
    stop = q2_floor((or_high + or_low) / Decimal("2"))
    if stop <= 0:
        return None
    pct = (bar["close"] - or_high) / or_high
    return {
        "fill_start": bar["end"],
        "or_high": or_high,
        "or_low": or_low,
        "pct": pct,
        "session_date": session_date,
        "signal_close": bar["close"],
        "signal_end": bar["end"],
        "signal_start": bar["start"],
        "source": bar["source"],
        "stop": stop,
        "symbol": bar["symbol"],
    }


def _contiguous(first, count):
    starts = []
    cursor = first
    for _ in range(count):
        starts.append(cursor)
        cursor = add_minutes(cursor, 5)
    return starts


def _open_exit(position, bar):
    if bar["open"] <= position["stop"]:
        return "stop_gap", bar["open"]
    if bar["open"] >= position["target"]:
        return "target_gap", bar["open"]
    if bar["start"][11:] >= "15:55:00":
        return "flat", bar["open"]
    return None, None


def _intrabar_exit(position, bar):
    touched_stop = bar["low"] <= position["stop"]
    touched_target = bar["high"] >= position["target"]
    if touched_stop and touched_target:
        return "stop_first", position["stop"]
    if touched_stop:
        return "stop", position["stop"]
    if touched_target:
        return "target", position["target"]
    return None, None


def _apply_entry(state, cand, bar, plan):
    from decimal import Decimal

    qty = plan["qty"]
    entry = q2(Decimal(plan["entry"]))
    fee = q3(Decimal(plan["fee"]))
    cost = q3(Decimal(plan["cost"]))
    if cost > state["cash"]:
        raise EngineInvariant("entry cost exceeds cash")
    state["cash"] = q3(state["cash"] - cost)
    position = {
        "cost_basis": cost,
        "entry_event_id": "entry:%s:%s" % (cand["symbol"], cand["signal_start"]),
        "entry_price": entry,
        "exit_blocked": False,
        "last_close": None,
        "last_close_at": None,
        "mark_status": "unpriced",
        "opened_at": bar["observed_at"],
        "opened_start": bar["start"],
        "session_date": cand["session_date"],
        "signal_start": cand["signal_start"],
        "source": cand["source"],
        "stale_hold": False,
        "stop": q2(cand["stop"]),
        "target": q2(Decimal(plan["target"])),
        "qty": qty,
    }
    state["positions"][cand["symbol"]] = position
    _event(
        state,
        {
            "bar_end": bar["end"],
            "bar_start": bar["start"],
            "bound_by": plan["bound_by"],
            "cash_after": s3(state["cash"]),
            "cost": plan["cost"],
            "decision_time": cand["signal_end"],
            "entry_price": plan["entry"],
            "event_time": bar["observed_at"],
            "fee": plan["fee"],
            "id": position["entry_event_id"],
            "observed_at": bar["observed_at"],
            "price_source": "next_bar_open",
            "qty": qty,
            "rank": plan["rank"],
            "raw_open": s2(cand["raw_open"]),
            "reason": "orb_next_open",
            "session_date": cand["session_date"],
            "shares_cap": plan["shares_cap"],
            "shares_cash": plan["shares_cash"],
            "shares_risk": plan["shares_risk"],
            "signal_close": s2(cand["signal_close"]),
            "signal_start": cand["signal_start"],
            "sizing_cash": plan["cash_available"],
            "sizing_equity": plan["equity"],
            "slippage": s2(SLIPPAGE),
            "source": cand["source"],
            "stop": s2(position["stop"]),
            "symbol": cand["symbol"],
            "target": plan["target"],
            "type": "entry",
        },
    )


def _skip(state, signal, bar, reason, plan=None):
    payload = {
        "bar_end": bar["end"] if isinstance(bar, dict) and "end" in bar else None,
        "bar_start": bar["start"] if isinstance(bar, dict) else None,
        "decision_time": signal.get("signal_end"),
        "event_time": bar.get("observed_at") if isinstance(bar, dict) else None,
        "id": "skip:%s:%s:%s" % (signal["symbol"], signal["signal_start"], reason),
        "observed_at": bar.get("observed_at") if isinstance(bar, dict) else None,
        "qty": 0,
        "reason": reason,
        "session_date": signal["session_date"],
        "symbol": signal["symbol"],
        "type": "skip",
    }
    if plan is not None and "rank" in plan:
        payload["rank"] = plan["rank"]
        if plan.get("cash_available") is not None:
            payload["sizing_cash"] = plan["cash_available"]
        if plan.get("bound_by") is not None:
            payload["bound_by"] = plan["bound_by"]
    _event(state, payload)


def _apply_exit(state, position, bar, kind, raw):
    symbol = None
    for name, held in state["positions"].items():
        if held is position:
            symbol = name
            break
    if symbol is None:
        raise EngineInvariant("exit without a position")
    fill, capped = slip_exit(raw)
    qty = position["qty"]
    fee = q3(qty * COMMISSION)
    proceeds = q3(qty * fill - fee)
    pnl = q3(proceeds - position["cost_basis"])
    state["cash"] = q3(state["cash"] + proceeds)
    state["realized"] = q3(state["realized"] + pnl)
    _event(
        state,
        {
            "bar_end": bar["end"],
            "bar_start": bar["start"],
            "cash_after": s3(state["cash"]),
            "decision_time": bar["end"],
            "event_time": bar["observed_at"],
            "fee": s3(fee),
            "id": "exit:%s:%s" % (symbol, position["signal_start"]),
            "observed_at": bar["observed_at"],
            "pnl": s3(pnl),
            "price": s2(fill),
            "price_source": kind,
            "proceeds": s3(proceeds),
            "qty": qty,
            "raw_price": s2(raw),
            "reason": kind,
            "session_date": bar["start"][:10],
            "slippage": s2(SLIPPAGE),
            "slippage_capped": capped,
            "source": position["source"],
            "symbol": symbol,
            "type": "exit",
        },
    )
    del state["positions"][symbol]


def _entry_block_reason(state, session_date):
    if _realized_on(state, session_date) <= -DAILY_LOSS:
        return "daily_realized_loss_stop"
    if state["realized"] <= -RUN_LOSS:
        return "run_realized_loss_stop"
    return None


def _realized_on(state, session_date):
    from decimal import Decimal

    total = q3(0)
    for event in state["events"]:
        if event["type"] == "exit" and event["session_date"] == session_date:
            total = q3(total + Decimal(event["pnl"]))
    return total


def _entries_on(state, session_date):
    return sum(
        1
        for event in state["events"]
        if event["type"] == "entry" and event["session_date"] == session_date
    )


def _blocked(state, symbol):
    current = state["symbol_status"].get(symbol)
    return bool(current and current.get("entry_blocked"))


def _ledger(state):
    total = state["cash"]
    for position in state["positions"].values():
        total += position["cost_basis"]
    return q3(total)


def _check_ledger(state):
    if _ledger(state) != q3(STARTING_CASH + state["realized"]):
        raise EngineInvariant("cash plus open cost bases left the starting cash plus realized pnl")
    if state["cash"] < 0:
        raise EngineInvariant("cash went negative")


def _refresh_marks(state):
    as_of = state["as_of"]
    for position in state["positions"].values():
        if position.get("stale_hold") or position.get("mark_status") == "carried_incomplete":
            if position.get("mark_status") != "carried_incomplete":
                position["mark_status"] = "stale"
            continue
        if as_of is None or position.get("last_close_at") is None:
            position["mark_status"] = "unpriced"
            continue
        if position["last_close_at"] >= add_minutes(as_of, -5):
            position["mark_status"] = "fresh"
        else:
            position["mark_status"] = "stale"


def _drawdown(state):
    equity = _equity(state)
    if equity is None:
        return
    if equity > state["peak"]:
        state["peak"] = equity
    drawdown = q3(state["peak"] - equity)
    if drawdown > state["max_drawdown"]:
        state["max_drawdown"] = drawdown


def _equity(state):
    if not state["positions"]:
        return q3(state["cash"])
    total = state["cash"]
    for position in state["positions"].values():
        if position.get("mark_status") != "fresh" or position.get("last_close") is None:
            return None
        total += position["qty"] * position["last_close"]
    return q3(total)


def _freeze(state):
    positions = {}
    for symbol in sorted(state["positions"]):
        position = state["positions"][symbol]
        positions[symbol] = {
            "cost_basis": s3(position["cost_basis"]),
            "entry_event_id": position["entry_event_id"],
            "entry_price": s2(position["entry_price"]),
            "exit_blocked": bool(position["exit_blocked"]),
            "last_close": None if position["last_close"] is None else s2(position["last_close"]),
            "last_close_at": position["last_close_at"],
            "mark_status": position["mark_status"],
            "opened_at": position["opened_at"],
            "qty": position["qty"],
            "session_date": position["session_date"],
            "signal_start": position["signal_start"],
            "source": position["source"],
            "stale_hold": bool(position["stale_hold"]),
            "stop": s2(position["stop"]),
            "target": s2(position["target"]),
        }
    coverage = {}
    for symbol in sorted(state["coverage"]):
        coverage[symbol] = list(state["coverage"][symbol])
    return {
        "acknowledge": ACK,
        "approval": state["approval"],
        "as_of": state["as_of"],
        "assumptions": ASSUMPTIONS,
        "cash": s3(state["cash"]),
        "coverage": coverage,
        "architecture": "simulation",
        "events": list(state["events"]),
        "inputs": state["inputs"],
        "label": state["approval"]["label"],
        "max_drawdown": s3(state["max_drawdown"]),
        "mode": "offline_replay",
        "peak_equity": s3(state["peak"]),
        "positions": positions,
        "realized_pnl": s3(state["realized"]),
        "schema": 1,
        "strategy_id": STRATEGY_ID,
        "symbol_status": {
            symbol: state["symbol_status"][symbol] for symbol in sorted(state["symbol_status"])
        },
        "warnings": list(state["warnings"]),
    }
