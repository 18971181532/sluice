"""A link is one server with a queue in front of it.

Serialisation is modelled exactly: a 1000-byte segment on a 1 Mbit/s link takes
8 ms to clock out, no matter what else is happening.  Queueing is drop-tail,
measured in bytes still waiting to start, which is the behaviour that makes a
full buffer show up as delay before it shows up as loss.

Nothing here knows about TCP.  A link moves opaque segments and reports when the
last bit arrives; whether that is a data segment or an ACK is somebody else's
problem.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import List, Optional, Sequence


class Link:
    """One hop: bandwidth, propagation delay, and a finite buffer."""

    __slots__ = ("name", "rate", "delay", "queue_bytes", "loss", "busy_until",
                 "sent", "dropped", "bytes", "peak_backlog", "_recent")

    def __init__(
        self,
        rate: float,
        delay: float = 0.0,
        queue_bytes: int = 1 << 20,
        loss: float = 0.0,
        name: str = "link",
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        if delay < 0:
            raise ValueError("delay must not be negative")
        if queue_bytes < 0:
            raise ValueError("queue must not be negative")
        if not 0.0 <= loss < 1.0:
            raise ValueError("loss must be a probability in [0, 1)")
        self.name = name
        self.rate = float(rate)
        self.delay = float(delay)
        self.queue_bytes = int(queue_bytes)
        self.loss = float(loss)
        self.busy_until = 0.0
        self.sent = 0
        self.dropped = 0
        self.bytes = 0
        self.peak_backlog = 0.0
        self._recent: List[float] = []

    # -- the only interesting method ------------------------------------
    def transmit(
        self, now: float, size: int, rng: Optional[random.Random] = None
    ) -> Optional[float]:
        """Offer ``size`` bytes at ``now``.

        Returns the time the last bit reaches the far end, or ``None`` if the
        segment was dropped.  A drop can happen for either of two reasons, and
        the counters keep them apart: the buffer was full, or the coin came up
        tails.
        """
        if size < 0:
            raise ValueError("size must not be negative")
        backlog = self.backlog(now)
        self.peak_backlog = max(self.peak_backlog, backlog)
        self._recent.append(backlog)
        if len(self._recent) > 4096:
            del self._recent[:2048]
        if backlog + size > self.queue_bytes:
            self.dropped += 1
            return None
        if self.loss and rng is not None and rng.random() < self.loss:
            self.dropped += 1
            return None
        start = max(now, self.busy_until)
        finish = start + size / self.rate
        self.busy_until = finish
        self.sent += 1
        self.bytes += size
        return finish + self.delay

    def backlog(self, now: float) -> float:
        """Bytes that would still be waiting if a packet arrived right now."""
        if self.busy_until <= now:
            return 0.0
        return (self.busy_until - now) * self.rate

    def queue_delay(self, now: float) -> float:
        return self.backlog(now) / self.rate

    def drain(self) -> None:
        """Pretend the link has been idle; used between experiments."""
        self.busy_until = 0.0

    def reset_stats(self) -> None:
        self.sent = 0
        self.dropped = 0
        self.bytes = 0
        self.peak_backlog = 0.0
        self._recent.clear()

    def __str__(self) -> str:  # pragma: no cover - presentation only
        return f"{self.name}({self.rate/125000:.2f} Mbps, {self.delay*1000:.0f} ms)"


class Path:
    """Forward and reverse sequences of links.

    A path is symmetric in structure but not in capacity: the reverse direction
    usually carries only ACKs, so bottlenecking it would be a modelling mistake.
    """

    def __init__(self, forward: Sequence[Link], reverse: Sequence[Link]) -> None:
        if not forward or not reverse:
            raise ValueError("both directions need at least one link")
        self.forward = tuple(forward)
        self.reverse = tuple(reverse)

    @property
    def propagation(self) -> float:
        """Round-trip propagation delay, not counting serialisation or queues."""
        return sum(link.delay for link in self.forward) + sum(
            link.delay for link in self.reverse
        )

    @property
    def bottleneck(self) -> Link:
        return min(self.forward, key=lambda link: link.rate)

    def transmit_forward(
        self, now: float, size: int, rng: Optional[random.Random] = None
    ) -> Optional[float]:
        return self._walk(self.forward, now, size, rng)

    def transmit_reverse(
        self, now: float, size: int, rng: Optional[random.Random] = None
    ) -> Optional[float]:
        return self._walk(self.reverse, now, size, rng)

    def _walk(
        self, links: Sequence[Link], now: float, size: int,
        rng: Optional[random.Random],
    ) -> Optional[float]:
        at = now
        for link in links:
            arrival = link.transmit(at, size, rng)
            if arrival is None:
                return None
            at = arrival
        return at

    def reset(self) -> None:
        for link in self.forward + self.reverse:
            link.drain()
            link.reset_stats()

    def max_queue_delay(self) -> float:
        return max(
            (link.peak_backlog / link.rate for link in self.forward + self.reverse),
            default=0.0,
        )


def dumbbell(
    mbps: float,
    rtt_ms: float,
    queue_packets: int,
    mss: int = 1000,
    loss: float = 0.0,
    access_ratio: float = 50.0,
) -> Path:
    """The classic topology: two access links and one shared bottleneck.

    ``queue_packets`` is the bottleneck buffer in segments, which is the number
    that decides whether the network drops or bloats.
    """
    rate = mbps * 1_000_000 / 8  # bytes per second
    half = rtt_ms / 2000.0
    bottleneck = Link(rate, 0.0, queue_packets * mss, loss=loss, name="bottleneck")
    access = Link(rate * access_ratio, half, 1 << 22, name="access")
    back = Link(rate * access_ratio, half, 1 << 22, name="return")
    fast = Link(rate * access_ratio, 0.0, 1 << 22, name="reverse")
    return Path([bottleneck, fast, access], [back, Link(rate * access_ratio, 0.0,
                                                        1 << 22, name="ack")])
