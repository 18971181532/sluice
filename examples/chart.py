#!/usr/bin/env python3
"""Three controls, one lossy link, three window curves.

The chart is plain text on purpose: it survives a terminal, a log file and a
README without a rendering step.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sluice import experiments as ex  # noqa: E402
from sluice.cc import CONTROLS, make  # noqa: E402
from sluice.render import chart, table  # noqa: E402


def main():
    scenario = ex.Scenario(loss=0.01, seconds=12.0)
    print(f"{scenario.label()}, {scenario.seconds:g} s")
    print()
    rows = []
    for name in CONTROLS:
        run = ex.transfer(make(name), scenario)
        flow = run.flows[0]
        rows.append(
            [flow.control, f"{flow.mbps:.2f}", f"{flow.retransmits}",
             f"{flow.timeouts}", f"{flow.avg_queue_delay * 1000:.0f} ms"]
        )
        print(chart(
            [(sample.time, sample.cwnd) for sample in flow.samples],
            width_of=58, height=8, scale=1 / 1000,
            title=f"{name}", unit="cwnd, KB",
        ))
        print()
    print(table(["control", "Mbit/s", "retx", "timeouts", "queue"], rows,
                aligns=["left", "right", "right", "right", "right"]))


if __name__ == "__main__":
    main()
