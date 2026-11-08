"""Offline simulated ledger for one approved opening-range scope.

This package does not import the day-trader skills, does not place orders,
and does not read a network. Forward paper mode is disabled.
"""

from paper_trader.engine import run_bars, size_batch, size_one
from paper_trader.forward import ForwardPaperAdapter
from paper_trader.spec import ACK, STRATEGY_ID

__version__ = "0.1.0"

__all__ = [
    "ACK",
    "ForwardPaperAdapter",
    "STRATEGY_ID",
    "run_bars",
    "size_batch",
    "size_one",
]
