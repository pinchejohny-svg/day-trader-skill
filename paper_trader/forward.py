"""Forward paper mode is disabled.

A later adapter would have to supply a verified contract that this package
does not implement and does not check against a live service:

- completed 5-minute bars only, never a partial bar
- an observation timestamp that is not implied by a download clock
- no revision of a bar the engine has already accepted
- a source name and a freshness bound the human has approved
- the same scope record the offline engine already requires

This class performs no I/O. It is not a paper-trading connection.
"""

from paper_trader.errors import ForwardDisabled


class ForwardPaperAdapter(object):
    def poll(self, *args, **kwargs):
        raise ForwardDisabled(
            "forward paper mode is disabled: no verified closed-bar data contract is implemented"
        )

    def stream(self, *args, **kwargs):
        raise ForwardDisabled(
            "forward paper mode is disabled: no verified closed-bar data contract is implemented"
        )
