"""Offline simulation for one approved opening-range scope.

Importing this package does not import the day-trader skills, does not place
orders, and does not open a socket. There is no broker pathway. The forward
orchestrator loads with the package and stays dry until an explicit command.
"""

from paper_trader.engine import run_bars, size_batch, size_one
from paper_trader.forward import BLOCKER, FORWARD_IMPLEMENTED
from paper_trader.spec import ACK, STRATEGY_ID

__version__ = "0.1.0"

__all__ = [
    "ACK",
    "BLOCKER",
    "FORWARD_IMPLEMENTED",
    "STRATEGY_ID",
    "run_bars",
    "size_batch",
    "size_one",
]
