"""Accounting report for one offline replay.

The label is whatever the human put on the run. This module does not check
that a historical label came from a market, and it never invents bars to
fill out a session.
"""

from decimal import Decimal

from paper_trader.spec import q3, s3


def build_report(frozen):
    positions = []
    unrealized = Decimal("0")
    market = Decimal("0")
    valuation_complete = True
    for symbol in sorted(frozen["positions"]):
        position = frozen["positions"][symbol]
        fresh = position["mark_status"] == "fresh" and position["last_close"] is not None
        if not fresh:
            valuation_complete = False
        else:
            qty = Decimal(position["qty"])
            last = Decimal(position["last_close"])
            basis = Decimal(position["cost_basis"])
            unrealized += qty * last - basis
            market += qty * last
        positions.append(
            {
                "cost_basis": position["cost_basis"],
                "entry_price": position["entry_price"],
                "last_close": position["last_close"],
                "last_close_at": position["last_close_at"],
                "mark_status": position["mark_status"],
                "qty": position["qty"],
                "session_date": position["session_date"],
                "source": position["source"],
                "stop": position["stop"],
                "symbol": symbol,
                "target": position["target"],
                "usable_for_valuation": fresh,
            }
        )
    cash = Decimal(frozen["cash"])
    if not frozen["positions"]:
        equity = cash
        unrealized_text = "0.000"
        equity_text = s3(equity)
        valuation_complete = True
    elif valuation_complete:
        equity = q3(cash + market)
        unrealized_text = s3(q3(unrealized))
        equity_text = s3(equity)
    else:
        equity = None
        unrealized_text = None
        equity_text = None
    peak = Decimal(frozen["peak_equity"])
    if equity is None:
        drawdown = None
        drawdown_status = "incomplete"
    else:
        drawdown = s3(q3(peak - equity))
        drawdown_status = "complete"
    reasons = _reasons(frozen, valuation_complete)
    return {
        "as_of": frozen["as_of"],
        "assumptions": frozen["assumptions"],
        "architecture": "simulation",
        "cash": frozen["cash"],
        "coverage": frozen["coverage"],
        "data_verified": False,
        "drawdown": drawdown,
        "drawdown_status": drawdown_status,
        "equity_value": equity_text,
        "fills_are": "simulated_next_bar_open",
        "full_session": _full_session(frozen),
        "incomplete": bool(reasons),
        "incomplete_reasons": reasons,
        "label": frozen["label"],
        "label_is_caller_supplied": True,
        "max_drawdown": frozen["max_drawdown"],
        "mode": "offline_replay",
        "open_positions": positions,
        "peak_equity": frozen["peak_equity"],
        "profitability_claim": False,
        "realized_pnl": frozen["realized_pnl"],
        "result_kind": "simulated_accounting_not_a_market_result",
        "unrealized_pnl": unrealized_text,
        "warnings": frozen["warnings"],
    }


def _reasons(frozen, valuation_complete):
    reasons = []
    if any(item["status"] == "no_data" for item in frozen["symbol_status"].values()):
        reasons.append("no_data")
    if any(item["severity"] == "block" for item in frozen["warnings"]):
        reasons.append("rejected_bars")
    if any(item["status"] == "carried" for item in frozen["symbol_status"].values()):
        reasons.append("carried_position")
    if frozen["positions"] and not valuation_complete:
        reasons.append("stale_or_unpriced_position")
    if frozen["positions"]:
        reasons.append("open_position")
    if not _reached_flat(frozen):
        reasons.append("flat_by_not_reached")
    return reasons


def _reached_flat(frozen):
    if any(item["status"] != "ok" for item in frozen["symbol_status"].values()):
        return False
    if any(item["severity"] == "block" for item in frozen["warnings"]):
        return False
    approved = frozen["approval"]["session_dates"]
    for symbol, status in frozen["symbol_status"].items():
        if status["status"] != "ok":
            return False
        starts = frozen["coverage"].get(symbol) or []
        for day in approved:
            if day + "T15:55:00" not in starts:
                return False
    return True


def _full_session(frozen):
    return _reached_flat(frozen) and not frozen["positions"]
