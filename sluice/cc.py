"""Three answers to one question: how fast should I send?

All three controls here see exactly the same events — an ACK that advances the
window, a loss detected by duplicate ACKs, a timeout — and all three output one
number, the congestion window.  Nothing else in the engine knows which one is in
use.  That is what makes the comparison in the README a comparison and not a
demo.

The three were chosen because they disagree about *what a loss means*:

* **Reno** (RFC 5681) treats loss as the only signal.  Additive increase,
  multiplicative decrease: a sawtooth, one tooth per buffer overflow.
* **CUBIC** (RFC 8312, core only) treats loss as a checkpoint and then grows as
  a cubic function of the time since that checkpoint, so it recovers quickly at
  first and flattens near the old ceiling.
* **Vegas** treats *queueing delay* as the signal and loss as an accident.  It
  keeps a small standing queue and backs off before the buffer overflows — and
  pays for that politeness when it competes with a loss-based sender.

Every control is a pure state machine over (acked bytes, RTT, loss).  No clocks,
no packets, no sockets: given a scripted sequence of ACKs, each produces the
same window curve every time, which is why ``test_cc.py`` can pin the shapes
down without a network.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Type

MSS = 1000
INFINITE = float(1 << 30)


class CongestionControl:
    """The interface the sender talks to."""

    name = "base"

    def __init__(self, mss: int = MSS, initial_window: int = 1) -> None:
        if mss <= 0:
            raise ValueError("mss must be positive")
        if initial_window < 1:
            raise ValueError("initial window must be at least one segment")
        self.mss = mss
        self._cwnd = float(initial_window * mss)
        self._ssthresh = INFINITE
        self.losses = 0

    # -- state ----------------------------------------------------------
    @property
    def cwnd(self) -> int:
        return max(int(self._cwnd), self.mss)

    @property
    def ssthresh(self) -> int:
        return int(min(self._ssthresh, INFINITE))

    @property
    def in_slow_start(self) -> bool:
        return self._cwnd < self._ssthresh

    def snapshot(self) -> Dict[str, float]:
        return {"cwnd": self.cwnd, "ssthresh": self.ssthresh}

    # -- events ---------------------------------------------------------
    def on_ack(self, acked: int, now: float, rtt: Optional[float]) -> None:
        raise NotImplementedError

    def on_loss(self, now: float, timeout: bool = False) -> None:
        raise NotImplementedError

    # -- fast recovery hooks --------------------------------------------
    def inflate(self, amount: int) -> None:
        """Window inflation while duplicate ACKs keep arriving."""
        return None

    def deflate(self) -> None:
        """Collapse the inflated window once the recovery point is ACKed."""
        return None

    def on_partial_ack(self, acked: int) -> None:
        """A partial ACK inside fast recovery: another hole was found.

        Reno as originally specified waits for a timeout here, which is why a
        window with two losses stalls for a second.  NewReno repairs the next
        hole immediately and deflates by what was acknowledged.
        """
        return None

    # -- shared helpers -------------------------------------------------
    def _halve(self) -> None:
        self._ssthresh = max(self._cwnd / 2.0, 2.0 * self.mss)
        self._cwnd = self._ssthresh

    def __str__(self) -> str:  # pragma: no cover - presentation only
        return f"{self.name}(cwnd={self.cwnd}, ssthresh={self.ssthresh})"


class LossBased(CongestionControl):
    """Shared fast-recovery mechanics for the two loss-based controls."""

    def inflate(self, amount: int) -> None:
        self._cwnd += amount

    def deflate(self) -> None:
        self._cwnd = self._ssthresh

    def on_partial_ack(self, acked: int) -> None:
        self._cwnd = max(
            self._ssthresh + 3.0 * self.mss,
            self._cwnd - acked + self.mss,
        )


class Reno(LossBased):
    """Additive increase, multiplicative decrease, and nothing else."""

    name = "reno"

    def on_ack(self, acked: int, now: float, rtt: Optional[float]) -> None:
        if acked <= 0:
            return
        if self._cwnd < self._ssthresh:
            self._cwnd += acked
        else:
            self._cwnd += self.mss * acked / self._cwnd

    def on_loss(self, now: float, timeout: bool = False) -> None:
        self.losses += 1
        if timeout:
            self._ssthresh = max(self._cwnd / 2.0, 2.0 * self.mss)
            self._cwnd = float(self.mss)
        else:
            self._halve()


class Cubic(LossBased):
    """The core of RFC 8312, without the optional refinements.

    The window follows ``W(t) = C (t - K)^3 + W_max`` where ``K`` is the time it
    takes to climb back to the window that was lost.  Growth is therefore fast
    just after a loss and flat near the ceiling — the opposite curvature to
    Reno, which is the whole point.
    """

    name = "cubic"
    BETA = 0.7
    C = 0.4

    def __init__(self, mss: int = MSS, initial_window: int = 1) -> None:
        super().__init__(mss, initial_window)
        self._w_max = 0.0
        self._k = 0.0
        self._epoch: Optional[float] = None
        self._min_rtt: Optional[float] = None

    def on_ack(self, acked: int, now: float, rtt: Optional[float]) -> None:
        if acked <= 0:
            return
        if rtt is not None:
            self._min_rtt = rtt if self._min_rtt is None else min(self._min_rtt, rtt)
        if self._cwnd < self._ssthresh:
            self._cwnd += acked
            return
        if self._epoch is None:
            self._epoch = now
        if self._w_max <= 0:
            self._w_max = self._cwnd
        elapsed = now - self._epoch + (self._min_rtt or 0.0)
        target = self._target(elapsed)
        if target > self._cwnd:
            # One ACK closes a fraction of the gap.  Expressing it per byte
            # ACKed keeps it honest when the receiver ACKs every other segment.
            step = acked * self.mss / max(target - self._cwnd, 1.0)
            self._cwnd += min(step, target - self._cwnd)
        else:
            # TCP-friendly region: never grow slower than Reno would have.
            self._cwnd += self.mss * acked / self._cwnd

    def _target(self, elapsed: float) -> float:
        w_max_seg = self._w_max / self.mss
        k = self._k
        window_seg = self.C * (elapsed - k) ** 3 + w_max_seg
        return max(window_seg, 2.0) * self.mss

    def on_loss(self, now: float, timeout: bool = False) -> None:
        self.losses += 1
        self._w_max = self._cwnd
        if timeout:
            self._ssthresh = max(self._cwnd * self.BETA, 2.0 * self.mss)
            self._cwnd = float(self.mss)
        else:
            self._cwnd = max(self._cwnd * self.BETA, 2.0 * self.mss)
            self._ssthresh = self._cwnd
        # K is the time the cubic needs to return to W_max.
        self._k = ((self._w_max / self.mss) * (1.0 - self.BETA) / self.C) ** (1.0 / 3.0)
        self._epoch = now


class Vegas(CongestionControl):
    """Delay-based congestion avoidance.

    ``diff`` is the number of segments the sender has in the network beyond what
    the base RTT requires: zero means the path is empty, large means a queue is
    building.  Below ``alpha`` it adds a segment per RTT, above ``beta`` it
    removes one, and in between it does nothing at all.

    The difference from Reno is the size of the step, not its absence: a
    duplicate-ACK loss costs Vegas a quarter of the window, where Reno gives up
    half.  That is the "more gentle decrease" the paper describes, and it cuts
    both ways -- see the loss sweep, where the gentler sender keeps more of the
    link, and the bufferbloat table, where it is the only one that does not
    fill the buffer.
    """

    name = "vegas"
    ALPHA = 1.0
    BETA = 3.0
    GAMMA = 1.0

    def __init__(self, mss: int = MSS, initial_window: int = 1) -> None:
        super().__init__(mss, initial_window)
        self._base_rtt: Optional[float] = None
        self._last_check: Optional[float] = None

    def _diff(self, rtt: float) -> float:
        if self._base_rtt is None or rtt <= 0:
            return 0.0
        queued = self._cwnd * (rtt - self._base_rtt) / rtt
        return queued / self.mss

    def on_ack(self, acked: int, now: float, rtt: Optional[float]) -> None:
        if acked <= 0 or rtt is None:
            return
        self._base_rtt = rtt if self._base_rtt is None else min(self._base_rtt, rtt)
        diff = self._diff(rtt)
        if self._cwnd < self._ssthresh:
            if diff > self.GAMMA:
                # The path is already queueing; stop doubling.
                self._ssthresh = max(self._cwnd, 2.0 * self.mss)
            else:
                self._cwnd += acked
                return
        # Adjust at most once per round trip.
        if self._last_check is not None and now - self._last_check < self._base_rtt:
            return
        self._last_check = now
        if diff < self.ALPHA:
            self._cwnd += self.mss
        elif diff > self.BETA:
            self._cwnd = max(self._cwnd - self.mss, 2.0 * self.mss)

    def on_loss(self, now: float, timeout: bool = False) -> None:
        self.losses += 1
        if timeout:
            self._ssthresh = max(self._cwnd * 0.75, 2.0 * self.mss)
            self._cwnd = 2.0 * self.mss
            self._last_check = None
        else:
            self._cwnd = max(self._cwnd * 0.75, 2.0 * self.mss)


CONTROLS: Dict[str, Type[CongestionControl]] = {
    "reno": Reno,
    "cubic": Cubic,
    "vegas": Vegas,
}


def make(name: str, mss: int = MSS) -> CongestionControl:
    try:
        return CONTROLS[name.lower()](mss)
    except KeyError:
        raise ValueError(
            f"unknown congestion control {name!r}; choose from {sorted(CONTROLS)}"
        ) from None


def mathis(mss: int, rtt: float, loss: float) -> float:
    """The Mathis formula: throughput ~ MSS/RTT * 1.22 / sqrt(p).

    Used in the README as an independent prediction of what Reno should get on
    a lossy link, so the simulator is checked against theory rather than
    against itself.
    """
    if rtt <= 0 or loss <= 0:
        raise ValueError("rtt and loss must be positive")
    return mss / rtt * 1.22 / math.sqrt(loss)
