"""The experiments the README quotes.

Each function here is a scenario, not a benchmark: it fixes the topology, the
run time and the receive window, and returns numbers that can be compared
because everything except the variable under test was held still.

The scenarios were chosen, not tuned to look good.  A 2 Mbit/s link with an
80 ms round trip has a bandwidth-delay product of 20 KB, comfortably inside the
64 KB that a 16-bit window can advertise, so congestion control -- not the
window -- decides the outcome.  The bufferbloat scenario deliberately breaks
that rule with a 1 Mbit/s link, because a small pipe with a big buffer is
exactly the situation where a full window hurts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

from . import seq
from .cc import CongestionControl, mathis
from .link import Path, dumbbell
from .sim import FlowResult, Run, run_flows


@dataclass(frozen=True)
class Scenario:
    mbps: float = 2.0
    rtt_ms: float = 80.0
    queue: int = 200
    seconds: float = 10.0
    loss: float = 0.0
    mss: int = 1000

    def __post_init__(self) -> None:
        if self.mbps <= 0:
            raise ValueError("mbps must be positive")
        if self.rtt_ms < 0:
            raise ValueError("rtt must not be negative")
        if self.queue <= 0:
            raise ValueError("queue must hold at least one segment")
        if self.seconds <= 0:
            raise ValueError("seconds must be positive")
        if not 0.0 <= self.loss < 1.0:
            raise ValueError("loss must be a probability in [0, 1)")

    def path(self) -> Path:
        return dumbbell(self.mbps, self.rtt_ms, self.queue, mss=self.mss,
                        loss=self.loss)

    @property
    def bdp(self) -> int:
        """Bandwidth-delay product in bytes, using the propagation delay."""
        return int(self.mbps * 1e6 / 8 * self.rtt_ms / 1000)

    def label(self) -> str:
        text = f"{self.mbps:g} Mbit/s, {self.rtt_ms:g} ms RTT, {self.queue} segment buffer"
        if self.loss:
            text += f", {self.loss * 100:g}% loss"
        return text


SINGLE = Scenario()
LOSSY = Scenario()
FAIR = Scenario(queue=300, loss=0.005)
MIXED = Scenario(loss=0.005)
BLOAT = Scenario(mbps=1.0, queue=300)
WRAP = Scenario(mbps=1.0, rtt_ms=40.0, queue=100, seconds=6.0)
TAIL = Scenario(mbps=2.0, rtt_ms=80.0, queue=100, seconds=7.0)


def transfer(
    control: CongestionControl,
    scenario: Scenario = SINGLE,
    rcvbuf: int = 65_535,
    seconds: Optional[float] = None,
    seed: int = 0,
    limit_bytes: Optional[int] = None,
) -> Run:
    """One bulk transfer.  The sender always has something to send."""
    return run_flows(
        [("flow", control)],
        seconds=seconds if seconds is not None else scenario.seconds,
        path=scenario.path(),
        seed=seed,
        rcvbuf=rcvbuf,
        mss=scenario.mss,
        limit_bytes=limit_bytes,
    )


@dataclass
class LossRow:
    loss: float
    mbps: float
    retransmits: int
    timeouts: int
    mathis_mbps: float

    @property
    def ratio(self) -> float:
        return self.mbps / self.mathis_mbps if self.mathis_mbps else 0.0


def loss_sweep(
    control: CongestionControl,
    scenario: Scenario = LOSSY,
    losses: Sequence[float] = (0.0, 0.002, 0.01, 0.03),
    rcvbuf: int = 65_535,
) -> List[LossRow]:
    """Throughput as the link gets worse, next to what theory predicts.

    Mathis is evaluated with the *propagation* RTT, not the measured one, so the
    prediction is independent of this simulator's queueing.
    """
    rows = []
    for loss in losses:
        case = Scenario(
            mbps=scenario.mbps, rtt_ms=scenario.rtt_ms, queue=scenario.queue,
            seconds=scenario.seconds, loss=loss, mss=scenario.mss,
        )
        # A control carries state: window, ssthresh, base RTT.  Reusing one
        # object across rows would let each row start where the last one
        # stopped, which quietly turns the sweep into one long connection.
        fresh = type(control)(control.mss)
        run = transfer(fresh, case, rcvbuf=rcvbuf)
        flow = run.flows[0]
        predicted = 0.0
        if loss > 0:
            predicted = mathis(scenario.mss, scenario.rtt_ms / 1000, loss) * 8 / 1e6
        rows.append(
            LossRow(
                loss=loss,
                mbps=flow.mbps,
                retransmits=flow.retransmits,
                timeouts=flow.timeouts,
                mathis_mbps=predicted,
            )
        )
    return rows


@dataclass
class FairnessReport:
    runs: List[Run]
    flows: int

    def jain(self) -> List[float]:
        return [run.jain() for run in self.runs]

    def mean_jain(self) -> float:
        values = self.jain()
        return sum(values) / len(values) if values else 0.0

    def spread(self) -> List[List[float]]:
        return [[flow.mbps for flow in run.flows] for run in self.runs]


def fairness(
    control: CongestionControl,
    scenario: Scenario = FAIR,
    flows: int = 4,
    seeds: Iterable[int] = (0, 1, 2),
    rcvbuf: int = 16_384,
) -> FairnessReport:
    """N identical flows, same path, different loss draws.

    The receive window is deliberately small here (16 KB, roughly three times a
    flow's fair share).  With a 64 KB window the same experiment swings between
    0.92 and 1.00 from seed to seed, because one timeout costs a flow a large
    fraction of its window; that variance is a property of Reno, not of the
    measurement, and it is worth seeing rather than hiding.
    """
    runs = []
    for seed in seeds:
        controls = [(f"flow {index + 1}", type(control)(control.mss))
                    for index in range(flows)]
        runs.append(
            run_flows(
                controls,
                seconds=scenario.seconds,
                path=scenario.path(),
                seed=seed,
                rcvbuf=rcvbuf,
                mss=scenario.mss,
            )
        )
    return FairnessReport(runs=runs, flows=flows)


def mix(
    controls: Sequence[CongestionControl],
    scenario: Scenario = MIXED,
    rcvbuf: int = 32_768,
    seed: int = 0,
) -> Run:
    """Different controls on the same bottleneck, at the same time."""
    named = [(control.name, control) for control in controls]
    return run_flows(named, seconds=scenario.seconds, path=scenario.path(),
                     seed=seed, rcvbuf=rcvbuf, mss=scenario.mss)


@dataclass
class BloatRow:
    control: str
    mbps: float
    queue_ms: float
    peak_ms: float
    retransmits: int


def bufferbloat(
    controls: Sequence[CongestionControl],
    scenario: Scenario = BLOAT,
) -> List[BloatRow]:
    """Same throughput, different patience.

    No random loss: the only signal is the queue itself.  A loss-based sender
    fills the buffer until the receive window stops it; a delay-based sender
    stops when the RTT starts to rise.
    """
    rows = []
    for control in controls:
        run = transfer(control, scenario, rcvbuf=65_535)
        flow = run.flows[0]
        rows.append(
            BloatRow(
                control=control.name,
                mbps=flow.mbps,
                queue_ms=flow.avg_queue_delay * 1000,
                peak_ms=flow.max_queue_delay * 1000,
                retransmits=flow.retransmits,
            )
        )
    return rows


@dataclass
class WrapReport:
    run: Run
    flow: FlowResult
    iss: int
    bytes: int

    @property
    def wrapped(self) -> bool:
        return self.iss + self.bytes >= seq.MOD


def wraparound(
    control: CongestionControl,
    scenario: Scenario = WRAP,
    bytes_to_send: int = 200_000,
) -> WrapReport:
    """Start the stream 3 KB before 2**32 and cross it mid-transfer."""
    iss = seq.MOD - 3000
    run = run_flows(
        [("flow", control)],
        seconds=scenario.seconds,
        path=scenario.path(),
        rcvbuf=65_535,
        mss=scenario.mss,
        iss=iss,
        limit_bytes=bytes_to_send,
        until_complete=True,
    )
    return WrapReport(run=run, flow=run.flows[0], iss=iss, bytes=bytes_to_send)


@dataclass
class TailReport:
    flow: FlowResult
    rto: float
    timeouts: List[float]


def tail_loss(
    control: CongestionControl,
    scenario: Scenario = TAIL,
    bytes_to_send: int = 50_000,
) -> TailReport:
    """Lose the last segment of a transfer and watch the timer back off.

    There is no data behind the hole, so no duplicate ACKs can arrive.  This is
    the one case where a lost segment can only be repaired by the retransmission
    timer, and the only case where exponential backoff is visible.
    """
    last = seq.add(1, 1 + bytes_to_send - scenario.mss)

    def drop(segment) -> bool:
        return bool(segment.payload) and segment.seq == last

    run = run_flows(
        [("flow", control)],
        seconds=scenario.seconds,
        path=scenario.path(),
        rcvbuf=65_535,
        mss=scenario.mss,
        limit_bytes=bytes_to_send,
        drop=drop,
    )
    flow = run.flows[0]
    stamps = [round(sample.time, 3) for sample in flow.samples if sample.event == "timeout"]
    return TailReport(
        flow=flow,
        rto=flow.rto_history[-1] if flow.rto_history else 0.0,
        timeouts=stamps,
    )
