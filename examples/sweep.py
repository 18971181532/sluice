#!/usr/bin/env python3
"""Print the loss sweep as a markdown table, ready to paste into a README."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sluice import experiments as ex  # noqa: E402
from sluice.cc import CONTROLS, make  # noqa: E402


def main():
    scenario = ex.Scenario()  # ten seconds per cell, as in the README table
    losses = (0.0, 0.002, 0.01, 0.03)
    header = "| loss | " + " | ".join(CONTROLS) + " | Mathis |"
    rule = "| ---: | " + " | ".join("---:" for _ in CONTROLS) + " | ---: |"
    print(f"{scenario.label()}, {scenario.seconds:g} s per cell, Mbit/s")
    print()
    print(header)
    print(rule)
    measured = {}
    for loss in losses:
        cells = []
        for name in CONTROLS:
            rows = ex.loss_sweep(make(name), scenario, losses=(loss,))
            measured[(name, loss)] = rows[0].mbps
            cells.append(f"{rows[0].mbps:.2f}")
        if loss:
            predicted = ex.mathis(1000, scenario.rtt_ms / 1000, loss) * 8 / 1e6
            # When the prediction exceeds the link rate, only the link was
            # measured; say so rather than quoting a number nothing can reach.
            suffix = " (link)" if predicted > scenario.mbps else ""
            cells.append(f"{predicted:.2f}{suffix}")
        else:
            cells.append("—")
        print(f"| {loss * 100:.1f}% | " + " | ".join(cells) + " |")
    print()
    for name in CONTROLS:
        values = [measured[(name, loss)] for loss in losses]
        assert values == sorted(values, reverse=True), f"{name} should slow down with loss"
    print("checked: every control gets slower as loss rises.")


if __name__ == "__main__":
    main()
