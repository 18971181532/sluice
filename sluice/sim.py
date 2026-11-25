"""A deterministic packet-level world.

Time is a heap.  Nothing happens between events, so a ten-second experiment
costs whatever its packets cost and not a microsecond more.

The important property is not speed, it is *reproducibility*: the same seed
produces the same packet order, the same drops, the same window curve.  A trace
from a failing run can be replayed exactly, which is what makes the crash-style
debugging in this project possible at all.

Every delivered byte is checked against the source as it lands, not at the end
of the run.  A receiver that repeats a byte, skips one, or reorders two raises
:class:`IntegrityError` at the segment that caused it -- the trace points at the
culprit instead of at the finish line.
"""

from __future__ import annotations

import heapq
import random
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from . import endpoint
from . import packet as pk
from . import seq
from .cc import CongestionControl
from .endpoint import Receiver, Sender, Sample, Timer
from .link import Path

# A fixed, incompressible-ish block used to generate and verify the byte stream
# without ever holding it in memory.
BLOCK = random.Random(2024).randbytes(65_536)
LOOKAHEAD = 256 * 1024
CHUNK = 64 * 1024


class IntegrityError(RuntimeError):
    """The received stream diverged from the sent one."""


def pattern(offset: int, length: int) -> bytes:
    """The bytes the application should have received at ``offset``."""
    out = bytearray()
    at = offset
    while len(out) < length:
        start = at % len(BLOCK)
        take = min(length - len(out), len(BLOCK) - start)
        out += BLOCK[start : start + take]
        at += take
    return bytes(out)


class _Timer:
    __slots__ = ("entry",)

    def __init__(self, entry: list) -> None:
        self.entry = entry

    def cancel(self) -> None:
        self.entry[3] = False


class Sim:
    """The event loop and the shared random source."""

    def __init__(self, seed: int = 0) -> None:
        self.time = 0.0
        self.seed = seed
        self.rng = random.Random(seed)
        self.flows: List["Flow"] = []
        self._heap: List[list] = []
        self._order = 0
        self.trace: Optional[List[Tuple[float, str, int, int, int, int]]] = None

    @property
    def now(self) -> float:
        return self.time

    def after(self, delay: float, callback: Callable[[], None]) -> Timer:
        entry = [self.time + max(delay, 0.0), self._order, callback, True]
        self._order += 1
        heapq.heappush(self._heap, entry)
        return _Timer(entry)

    def add(self, flow: "Flow") -> "Flow":
        self.flows.append(flow)
        return flow

    def run_for(self, seconds: float) -> None:
        self.run_until(self.time + seconds)

    def run_until(self, target: float) -> None:
        while self._heap and self._heap[0][0] <= target:
            when, _, callback, alive = heapq.heappop(self._heap)
            self.time = when
            if alive:
                callback()
        self.time = max(self.time, target)

    def record(self, segment: pk.Segment, direction: str) -> None:
        if self.trace is not None:
            self.trace.append(
                (round(self.time, 6), direction, segment.seq, segment.ack,
                 segment.flags, len(segment.payload))
            )

    def digest(self) -> str:
        """A cheap fingerprint of a recorded run."""
        import hashlib

        shadow = hashlib.sha256()
        for row in self.trace or []:
            shadow.update(repr(row).encode())
        return shadow.hexdigest()[:16]


@dataclass
class FlowResult:
    name: str
    control: str
    bytes_acked: int
    seconds: float
    throughput: float          # bytes per second
    retransmits: int
    timeouts: int
    fast_retransmits: int
    segments: int
    srtt: float
    rto: float
    avg_queue_delay: float
    max_queue_delay: float
    samples: List[Sample] = field(default_factory=list)
    rto_history: List[float] = field(default_factory=list)

    @property
    def mbps(self) -> float:
        return self.throughput * 8 / 1e6


class Flow:
    """One connection crossing one path, plus the ledger that watches it."""

    def __init__(
        self,
        sim: Sim,
        path: Path,
        control: CongestionControl,
        name: str = "flow",
        sport: int = 4000,
        dport: int = 80,
        mss: int = 1000,
        rcvbuf: int = 65_535,
        iss: int = 1,
        limit_bytes: Optional[int] = None,
        delayed_ack: float = endpoint.DELAYED_ACK,
        drop: Optional[Callable[[pk.Segment], bool]] = None,
    ) -> None:
        self.sim = sim
        self.path = path
        self.name = name
        self.sport = sport
        self.dport = dport
        self.mss = mss
        self.limit_bytes = limit_bytes
        self.written = 0
        self.absolute = 0          # bytes the ledger has verified and dropped
        self.dropped = 0
        self.drop = drop
        self.queue_samples: List[float] = []
        self.sender = Sender(
            self, sport, dport, control, mss=mss, iss=iss, rcvbuf=rcvbuf,
            delayed_ack=delayed_ack,
        )
        self.receiver = Receiver(
            self, dport, sport, iss=(iss + 7) % seq.MOD, rcvbuf=rcvbuf, mss=mss,
            delayed_ack=delayed_ack,
        )

    # -- Environment ----------------------------------------------------
    @property
    def now(self) -> float:
        return self.sim.now

    def after(self, delay: float, callback: Callable[[], None]) -> Timer:
        return self.sim.after(delay, callback)

    def transmit(self, segment: pk.Segment) -> None:
        if self.drop is not None and self.drop(segment):
            self.dropped += 1
            return
        forward = segment.src == self.sport
        size = pk.HEADER + len(segment.payload)
        if forward:
            self.sim.record(segment, ">")
            arrival = self.path.transmit_forward(self.sim.now, size, self.sim.rng)
        else:
            self.sim.record(segment, "<")
            arrival = self.path.transmit_reverse(self.sim.now, size, self.sim.rng)
        if arrival is None:
            self.dropped += 1
            return
        self.sim.after(arrival - self.sim.now, lambda: self._deliver(segment, forward))

    # -- application ----------------------------------------------------
    def start(self) -> None:
        self.sender.connect()
        self.feed()

    def feed(self) -> None:
        """Keep a bounded amount of data queued, like an application would."""
        while self.sender.queued < LOOKAHEAD:
            if self.limit_bytes is not None and self.written >= self.limit_bytes:
                if self.written and not self.sender.closing:
                    self.sender.close()
                return
            count = CHUNK
            if self.limit_bytes is not None:
                count = min(count, self.limit_bytes - self.written)
            start = self.written % len(BLOCK)
            chunk = BLOCK[start : start + count]
            if len(chunk) < count:
                chunk += BLOCK[: count - len(chunk)]
            self.sender.write(chunk)
            self.written += count

    # -- delivery and the ledger ----------------------------------------
    def _deliver(self, segment: pk.Segment, forward: bool) -> None:
        if forward:
            backlog = self.path.bottleneck.backlog(self.sim.now)
            self.queue_samples.append(backlog / self.path.bottleneck.rate)
            before = len(self.receiver.delivered)
            self.receiver.receive(segment)
            after = len(self.receiver.delivered)
            if after > before:
                got = bytes(self.receiver.delivered[before:after])
                want = pattern(self.absolute + before, after - before)
                if got != want:
                    raise IntegrityError(
                        f"{self.name}: bytes {self.absolute + before}.."
                        f"{self.absolute + after} do not match the source"
                    )
            if self.receiver.unread >= self.receiver.rcvbuf // 2:
                self._drain()
        else:
            self.sender.receive(segment)
            self.feed()

    def _drain(self) -> None:
        data = self.receiver.read()
        self.absolute += len(data)
        self.receiver.compact()

    # -- results --------------------------------------------------------
    @property
    def complete(self) -> bool:
        return (
            self.limit_bytes is not None
            and self.written == self.limit_bytes
            and self.sender.all_acked
        )

    def result(self, elapsed: Optional[float] = None) -> FlowResult:
        stats = self.sender.stats
        seconds = elapsed
        if seconds is None:
            if stats.first_ack is None:
                seconds = 0.0
            else:
                seconds = max(stats.last_ack - stats.first_ack, 1e-9)
        delivered = stats.bytes_acked
        return FlowResult(
            name=self.name,
            control=self.sender.cc.name,
            bytes_acked=delivered,
            seconds=seconds,
            throughput=delivered / seconds if seconds else 0.0,
            retransmits=stats.retransmits,
            timeouts=stats.timeouts,
            fast_retransmits=stats.fast_retransmits,
            segments=stats.segments,
            srtt=self.sender.srtt or 0.0,
            rto=self.sender.rto,
            avg_queue_delay=(
                sum(self.queue_samples) / len(self.queue_samples)
                if self.queue_samples else 0.0
            ),
            max_queue_delay=max(self.queue_samples) if self.queue_samples else 0.0,
            samples=list(self.sender.samples),
            rto_history=list(self.sender.rto_history),
        )


@dataclass
class Run:
    """Everything one experiment produced."""

    flows: List[FlowResult]
    wall: float
    dropped: int
    seed: int
    trace: Optional[List[Tuple[float, str, int, int, int, int]]] = None

    @property
    def delivered(self) -> int:
        return sum(flow.bytes_acked for flow in self.flows)

    @property
    def goodput(self) -> float:
        """Total bytes per second across all flows."""
        return sum(flow.throughput for flow in self.flows)

    def jain(self) -> float:
        """Jain's fairness index, 1.0 when every flow gets the same share."""
        rates = [flow.throughput for flow in self.flows]
        if not rates:
            return 0.0
        total = sum(rates)
        squares = sum(rate * rate for rate in rates)
        if squares == 0:
            return 0.0
        return total * total / (len(rates) * squares)

    def by_name(self, name: str) -> FlowResult:
        for flow in self.flows:
            if flow.name == name:
                return flow
        raise KeyError(name)


def run_flows(
    controls: List[Tuple[str, CongestionControl]],
    seconds: float,
    path: Path,
    seed: int = 0,
    limit_bytes: Optional[int] = None,
    mss: int = 1000,
    rcvbuf: int = 65_535,
    iss: int = 1,
    record: bool = False,
    drop: Optional[Callable[[pk.Segment], bool]] = None,
    until_complete: bool = False,
) -> Run:
    """Start every flow at t=0 and let them fight over the shared path.

    With ``until_complete`` the clock keeps going past ``seconds`` until every
    flow with a byte limit has delivered all of it, up to four times the
    requested time.  Used by the wraparound experiment, where the point is that
    the transfer finishes, not that it finishes within an arbitrary deadline.
    """
    sim = Sim(seed)
    path.reset()
    if record:
        sim.trace = []
    flows = []
    for index, (name, control) in enumerate(controls):
        flow = Flow(
            sim, path, control, name=name, sport=4000 + index, dport=80 + index,
            mss=mss, rcvbuf=rcvbuf, iss=iss, limit_bytes=limit_bytes, drop=drop,
        )
        sim.add(flow)
        flows.append(flow)
    for flow in flows:
        flow.start()
    sim.run_for(seconds)
    if until_complete and limit_bytes is not None:
        deadline = seconds * 4
        while sim.time < deadline and not all(flow.complete for flow in flows):
            sim.run_for(1.0)
    results = [flow.result() for flow in flows]
    return Run(
        flows=results,
        wall=sim.time,
        dropped=sum(link.dropped for link in path.forward + path.reverse),
        seed=seed,
        trace=sim.trace,
    )
