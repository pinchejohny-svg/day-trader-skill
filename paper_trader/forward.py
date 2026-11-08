"""Forward operation is not implemented.

There is no feed object, no poll method, and no broker pathway. This module
does not perform I/O. A verified closed-bar contract is not part of this demo.
"""

FORWARD_IMPLEMENTED = False
BLOCKER = (
    "forward mode is not implemented: no feed, no broker pathway, and no I/O. "
    "Provenance for the offline rule is paper_trader.spec.PROVENANCE."
)
