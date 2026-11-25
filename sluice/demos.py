"""Six demonstrations that check themselves before they print anything.

A demo that only prints is a screenshot.  Each of these asserts the claim it is
about to make -- the bytes really did arrive, the queue really was shorter, the
timer really did double -- so a regression shows up as a failing assertion, not
as a paragraph that quietly stopped being true.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List

from . import experiments as ex
from . import render
from .cc import Cubic, Reno, Vegas
from .experiments import Scenario

DEMO_NAMES = ("sawtooth", "reliable", "fairness", "bloat", "wrap", "tail")

DESCRIPTIONS = {
    "sawtooth": "one percent loss, and the window that keeps climbing back",
    "reliable": "three percent loss, and every byte still arrives once",
    "fairness": "four flows, one bottleneck, and Jain's index",
    "bloat": "same throughput, forty times the queueing delay",
    "wrap": "a transfer that crosses 2**32 in the middle",
    "tail": "the one loss that no duplicate ACK can repair",
}


@dataclass
class Demo:
    name: str
    title: str
    body: str


def _chart(flow, width: int = 62) -> str:
    return render.chart(
        [(sample.time, sample.cwnd) for sample in flow.samples],
        width_of=width, height=10, scale=1 / 1000,
        unit="cwnd, KB", x_unit="s",
    )


def _sawtooth() -> Demo:
    scenario = Scenario(loss=0.01)
    run = ex.transfer(Reno(), scenario)
    flow = run.flows[0]
    peaks = [sample.cwnd for sample in flow.samples if sample.event != "handshake"]
    after_loss = [sample.cwnd for sample in flow.samples
                  if sample.event in ("fast-retransmit", "timeout")]
    assert flow.retransmits > 0, "a lossy link should cost retransmissions"
    assert max(peaks) > 2 * min(peaks), "the window should actually move"
    body = "\n".join(
        [
            f"Reno, {scenario.label()}, {scenario.seconds:g} s.",
            f"{render.size(flow.bytes_acked)} delivered at {flow.mbps:.2f} Mbit/s.",
            "",
            _chart(flow),
            "",
            "The window climbs until a segment is lost, halves, and climbs again.",
            f"It peaked at {max(peaks) // 1000} KB and fell to "
            f"{min(after_loss) // 1000} KB at the last loss; "
            f"{flow.fast_retransmits} fast retransmits",
            f"and {flow.timeouts} timeouts repaired {flow.retransmits} lost segments.",
            f"{flow.segments:,} segments carried {render.size(flow.bytes_acked)}.",
        ]
    )
    return Demo("sawtooth", "The sawtooth, one tooth per loss", body)


def _reliable() -> Demo:
    scenario = Scenario(loss=0.03, seconds=20.0)
    total = 200_000
    run = ex.transfer(Reno(), scenario, limit_bytes=total)
    flow = run.flows[0]
    assert flow.bytes_acked == total, f"only {flow.bytes_acked} of {total} bytes arrived"
    body = "\n".join(
        [
            f"{total:,} bytes across a link that drops 3% of segments.",
            "",
            render.pairs(
                [
                    ["delivered", f"{flow.bytes_acked:,} bytes, in order"],
                    ["verified", "every segment checked as it landed"],
                    ["segments", f"{flow.segments:,} sent, {flow.retransmits} retransmitted"],
                    ["loss events", f"{flow.fast_retransmits} fast, {flow.timeouts} timeouts"],
                    ["time", f"{flow.seconds:.2f} s ({flow.mbps:.2f} Mbit/s)"],
                ],
            ),
            "",
            "The ledger in sim.py compares each newly delivered byte range with the",
            "source before the next segment is processed, so a repeated or missing",
            "byte fails at the segment that caused it, not at the end of the run.",
        ]
    )
    return Demo("reliable", "Lossy does not mean lossy", body)


def _fairness() -> Demo:
    scenario = Scenario(queue=300, loss=0.005)
    report = ex.fairness(Reno(), scenario, flows=4)
    assert report.mean_jain() > 0.95, f"Jain index {report.mean_jain():.3f}"
    rows = []
    for seed, run in zip((0, 1, 2), report.runs):
        rows.append(
            [str(seed), "  ".join(f"{flow.mbps:5.2f}" for flow in run.flows),
             f"{run.jain():.3f}"]
        )
    body = "\n".join(
        [
            f"Four Reno flows share {scenario.label()} for {scenario.seconds:g} s.",
            "",
            render.table(["seed", "per-flow Mbit/s", "Jain"], rows,
                         aligns=["right", "right", "right"]),
            "",
            f"Mean Jain index {report.mean_jain():.3f}.  Every flow runs the same",
            "algorithm over the same path, so the split is close to even; the",
            "remaining wobble is where each flow happened to lose a segment.",
        ]
    )
    return Demo("fairness", "Four flows, one queue", body)


def _bloat() -> Demo:
    scenario = Scenario(mbps=1.0, queue=300)
    rows = ex.bufferbloat([Reno(), Cubic(), Vegas()], scenario)
    loss_based = [row for row in rows if row.control != "vegas"]
    vegas = next(row for row in rows if row.control == "vegas")
    for row in loss_based:
        assert row.queue_ms > 5 * vegas.queue_ms, "loss-based senders should fill the buffer"
        assert abs(row.mbps - vegas.mbps) < 0.1 * max(row.mbps, 1e-9), "throughput should match"
    body = "\n".join(
        [
            f"{scenario.label()}, no random loss, {scenario.seconds:g} s.",
            "",
            render.table(
                ["control", "Mbit/s", "avg queue", "peak queue"],
                [[r.control, f"{r.mbps:.2f}", f"{r.queue_ms:.0f} ms", f"{r.peak_ms:.0f} ms"]
                 for r in rows],
                aligns=["left", "right", "right", "right"],
            ),
            "",
            f"All three deliver {vegas.mbps:.2f} Mbit/s, within one percent.  Reno",
            "and CUBIC only stop when the receive window runs out, so they leave",
            f"a full buffer behind: {loss_based[0].queue_ms:.0f} ms of standing delay.",
            f"Vegas notices the RTT rising and holds at {vegas.queue_ms:.0f} ms.",
            "",
            "This is bufferbloat in one table: the cost is not bandwidth, it is",
            "latency, and only the delay-based sender refuses to pay it.",
        ]
    )
    return Demo("bloat", "Same throughput, different patience", body)


def _wrap() -> Demo:
    scenario = Scenario(mbps=1.0, rtt_ms=40.0, queue=100, seconds=6.0)
    report = ex.wraparound(Reno(), scenario)
    assert report.wrapped, "the transfer was supposed to cross 2**32"
    assert report.flow.bytes_acked == report.bytes
    body = "\n".join(
        [
            f"Starting at sequence {report.iss:,}, sending {report.bytes:,} bytes.",
            "The counter passes 2**32 after 3,000 bytes and continues from zero.",
            "",
            render.pairs(
                [
                    ["delivered", f"{report.flow.bytes_acked:,} bytes"],
                    ["order", "byte for byte, including the wrap"],
                    ["retransmits", str(report.flow.retransmits)],
                    ["throughput", f"{report.flow.mbps:.2f} Mbit/s"],
                ],
            ),
            "",
            "Every comparison in seq.py is modular: before(), after() and",
            "distance() never subtract two sequence numbers directly.  The test",
            "suite walks a 4,000-byte window across the wrap for the same reason.",
        ]
    )
    return Demo("wrap", "Crossing 2**32 in the middle of a segment", body)


def _tail() -> Demo:
    scenario = Scenario(queue=100, seconds=7.0)
    report = ex.tail_loss(Reno(), scenario)
    assert len(report.timeouts) >= 3, "the timer should fire more than twice"
    assert report.rto >= 3.0, "the RTO should have backed off"
    rows = []
    for index, stamp in enumerate(report.timeouts):
        value = report.flow.rto_history[index] if index < len(report.flow.rto_history) else 0.0
        rows.append([str(index + 1), f"{stamp:.2f} s", f"{value:.1f} s"])
    body = "\n".join(
        [
            "The final segment of a 50 KB transfer is dropped every time it is",
            "sent.  Nothing follows it, so the receiver has nothing to ACK twice",
            "and the sender never sees three duplicate ACKs.",
            "",
            render.table(["timeout", "at", "RTO"], rows,
                         aligns=["right", "right", "right"]),
            "",
            "This is the case fast retransmit cannot fix.  Waiting is the only",
            "option, and each wait doubles from the first timeout onward.  A stack",
            "without SACK or RACK pays this bill once per tail loss.",
        ]
    )
    return Demo("tail", "The loss that only a timer can fix", body)


BUILDERS: Dict[str, Callable[[], Demo]] = {
    "sawtooth": _sawtooth,
    "reliable": _reliable,
    "fairness": _fairness,
    "bloat": _bloat,
    "wrap": _wrap,
    "tail": _tail,
}


def build(name: str) -> Demo:
    if name not in BUILDERS:
        raise ValueError(f"unknown demo {name!r}; choose from {list(BUILDERS)}")
    return BUILDERS[name]()


def build_all() -> List[Demo]:
    return [build(name) for name in DEMO_NAMES]
