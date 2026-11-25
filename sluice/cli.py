"""The command line: eight experiments, one terminal.

Every subcommand prints a table or a chart rather than a paragraph, and every
one of them can emit JSON instead.  Exit codes are part of the contract:

===  ==================================================================
0    the experiment ran and the numbers are in the output
1    the run broke an invariant -- bytes were lost, repeated or reordered
2    the command line did not make sense
===  ==================================================================
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List, Optional, Sequence

from . import __version__
from . import demos
from . import experiments as ex
from . import packet as pk
from . import render
from . import sim
from .cc import CONTROLS, make
from .experiments import Scenario

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_USAGE = 2


def _scenario(args: argparse.Namespace, **overrides) -> Scenario:
    values = dict(
        mbps=args.mbps,
        rtt_ms=args.rtt,
        queue=args.queue,
        seconds=args.seconds,
        loss=getattr(args, "loss", 0.0),
    )
    values.update(overrides)
    return Scenario(**values)


def _emit(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))


def _chart(flow, width: int = 66) -> str:
    points = [(sample.time, sample.cwnd) for sample in flow.samples]
    return render.chart(
        points, width_of=width, height=11, scale=1 / 1000,
        unit="congestion window, KB", x_unit="s",
    )


# -- subcommands ---------------------------------------------------------
def cmd_run(args: argparse.Namespace) -> int:
    scenario = _scenario(args)
    run = ex.transfer(make(args.cc), scenario, rcvbuf=args.window)
    flow = run.flows[0]
    if args.json:
        _emit(
            {
                "control": flow.control,
                "mbps": flow.mbps,
                "bytes": flow.bytes_acked,
                "retransmits": flow.retransmits,
                "timeouts": flow.timeouts,
                "fast_retransmits": flow.fast_retransmits,
                "srtt_ms": flow.srtt * 1000,
                "rto_ms": flow.rto * 1000,
                "queue_ms": flow.avg_queue_delay * 1000,
                "scenario": {
                    "mbps": scenario.mbps, "rtt_ms": scenario.rtt_ms,
                    "queue": scenario.queue, "loss": scenario.loss,
                    "seconds": scenario.seconds,
                },
                "segments": flow.segments,
            },
            True,
        )
        return EXIT_OK
    print(f"{flow.control} over {scenario.label()}, {scenario.seconds:g} s")
    print(
        render.pairs(
            [
                ["delivered", f"{render.size(flow.bytes_acked)}  ({flow.mbps:.2f} Mbit/s)"],
                ["segments", f"{flow.segments:,} sent, {flow.retransmits:,} retransmitted"],
                ["loss events", f"{flow.fast_retransmits:,} fast retransmit, {flow.timeouts:,} timeout"],
                ["srtt / rto", f"{flow.srtt * 1000:.1f} ms / {flow.rto * 1000:.0f} ms"],
                ["queueing", f"{flow.avg_queue_delay * 1000:.1f} ms average"],
            ]
        )
    )
    print()
    print(_chart(flow))
    return EXIT_OK


def cmd_loss(args: argparse.Namespace) -> int:
    scenario = _scenario(args)
    controls = list(CONTROLS) if args.all else [args.cc]
    blocks = []
    payload = {}
    for name in controls:
        control = make(name)
        rows = ex.loss_sweep(control, scenario, losses=tuple(args.at))
        table_rows = []
        for row in rows:
            if row.loss == 0:
                predicted = "—"
                ratio = "—"
            elif row.mathis_mbps > scenario.mbps * 0.95:
                predicted = f"{row.mathis_mbps:.2f}"
                ratio = "link"
            else:
                predicted = f"{row.mathis_mbps:.2f}"
                ratio = f"{row.ratio:.2f}"
            table_rows.append(
                [render.percent(row.loss), f"{row.mbps:.2f}", predicted, ratio,
                 f"{row.retransmits:,}", f"{row.timeouts:,}"]
            )
        blocks.append(
            f"{control.name}\n"
            + render.table(
                ["loss", "measured", "Mathis", "ratio", "retx", "timeouts"],
                table_rows,
                aligns=["right"] * 6,
            )
        )
        payload[control.name] = [
            {"loss": r.loss, "mbps": r.mbps, "mathis_mbps": r.mathis_mbps,
             "retransmits": r.retransmits, "timeouts": r.timeouts}
            for r in rows
        ]
    if args.json:
        _emit(payload, True)
        return EXIT_OK
    print(f"{scenario.label()}, {scenario.seconds:g} s per row")
    print()
    print("\n\n".join(blocks))
    print()
    print("Mathis: MSS/RTT x 1.22/sqrt(p), using the propagation delay.")
    print("'link' marks rows where the prediction exceeds the link, so only the")
    print("link rate could have been measured.")
    return EXIT_OK


def cmd_fair(args: argparse.Namespace) -> int:
    scenario = _scenario(args, queue=args.queue)
    report = ex.fairness(
        make(args.cc), scenario, flows=args.flows,
        seeds=tuple(args.seeds), rcvbuf=args.window,
    )
    if args.json:
        _emit(
            {
                "flows": args.flows,
                "jain": report.jain(),
                "mean_jain": report.mean_jain(),
                "shares": report.spread(),
            },
            True,
        )
        return EXIT_OK
    print(f"{args.flows} x {args.cc} over {scenario.label()}, "
          f"{scenario.seconds:g} s, seeds {list(args.seeds)}")
    print()
    rows = []
    for seed, run in zip(args.seeds, report.runs):
        rates = [flow.mbps for flow in run.flows]
        rows.append(
            [str(seed), "  ".join(f"{value:5.2f}" for value in rates),
             f"{run.jain():.3f}"]
        )
    print(render.table(["seed", "per-flow Mbit/s", "Jain"], rows,
                       aligns=["right", "right", "right"]))
    print()
    print(f"mean Jain index {report.mean_jain():.3f}  (1.000 is a perfect split)")
    return EXIT_OK


def cmd_mix(args: argparse.Namespace) -> int:
    names = [name.strip().lower() for name in args.flows.split(",") if name.strip()]
    if not names:
        print("no flows named", file=sys.stderr)
        return EXIT_USAGE
    scenario = _scenario(args)
    runs = [
        ex.mix([make(name) for name in names], scenario, rcvbuf=args.window, seed=seed)
        for seed in args.seeds
    ]
    totals: dict = {}
    for run in runs:
        for flow in run.flows:
            entry = totals.setdefault(flow.control, {"mbps": 0.0, "retx": 0, "queue": 0.0})
            entry["mbps"] += flow.mbps / len(runs)
            entry["retx"] += flow.retransmits
            entry["queue"] += flow.avg_queue_delay * 1000 / len(runs)
    combined = sum(entry["mbps"] for entry in totals.values())
    if args.json:
        _emit(
            {
                "controls": {name: {"mbps": value["mbps"],
                                    "share": value["mbps"] / combined if combined else 0}
                             for name, value in totals.items()},
                "jain": [run.jain() for run in runs],
            },
            True,
        )
        return EXIT_OK
    print(f"{' vs '.join(names)} over {scenario.label()}, {scenario.seconds:g} s, "
          f"seeds {list(args.seeds)}")
    print()
    print(
        render.table(
            ["control", "Mbit/s", "share", "retx", "queue"],
            [
                [name, f"{value['mbps']:.2f}",
                 render.percent(value["mbps"] / combined if combined else 0),
                 f"{value['retx']:,}", f"{value['queue']:.0f} ms"]
                for name, value in totals.items()
            ],
            aligns=["left", "right", "right", "right", "right"],
        )
    )
    print()
    print("Jain per seed: " + ", ".join(f"{run.jain():.3f}" for run in runs))
    return EXIT_OK


def cmd_bloat(args: argparse.Namespace) -> int:
    scenario = _scenario(args)
    rows = ex.bufferbloat([make(name) for name in CONTROLS], scenario)
    if args.json:
        _emit([r.__dict__ for r in rows], True)
        return EXIT_OK
    print(f"{scenario.label()}, no random loss; the queue is the only signal")
    print()
    print(
        render.table(
            ["control", "Mbit/s", "avg queue", "peak queue", "retx"],
            [[r.control, f"{r.mbps:.2f}", f"{r.queue_ms:.0f} ms",
              f"{r.peak_ms:.0f} ms", str(r.retransmits)] for r in rows],
            aligns=["left", "right", "right", "right", "right"],
        )
    )
    best = min(rows, key=lambda r: r.queue_ms)
    worst = max(rows, key=lambda r: r.queue_ms)
    print()
    print(
        f"{best.control} keeps the queue {worst.queue_ms / max(best.queue_ms, 1e-9):.0f}x"
        f" shorter than {worst.control} for the same throughput."
    )
    return EXIT_OK


def cmd_wrap(args: argparse.Namespace) -> int:
    scenario = _scenario(args)
    report = ex.wraparound(make(args.cc), scenario)
    flow = report.flow
    if args.json:
        _emit(
            {"iss": report.iss, "bytes": report.bytes, "wrapped": report.wrapped,
             "delivered": flow.bytes_acked, "retransmits": flow.retransmits},
            True,
        )
        return EXIT_OK if flow.bytes_acked == report.bytes else EXIT_BROKEN
    print(f"initial sequence number {report.iss:,} (2**32 - 3,000)")
    print(f"transfer {report.bytes:,} bytes, crossing 2**32 after 3,000")
    print()
    print(
        render.pairs(
            [
                ["delivered", f"{flow.bytes_acked:,} bytes"],
                ["in order", "yes" if flow.bytes_acked == report.bytes else "NO"],
                ["wrapped", "yes" if report.wrapped else "no"],
                ["retransmits", str(flow.retransmits)],
                ["throughput", f"{flow.mbps:.2f} Mbit/s"],
            ]
        )
    )
    return EXIT_OK if flow.bytes_acked == report.bytes else EXIT_BROKEN


def cmd_tail(args: argparse.Namespace) -> int:
    scenario = _scenario(args)
    report = ex.tail_loss(make(args.cc), scenario)
    if args.json:
        _emit(
            {"timeouts": report.timeouts, "rto": report.rto,
             "delivered": report.flow.bytes_acked},
            True,
        )
        return EXIT_OK
    print(f"{scenario.label()}; the last segment is dropped every time it is sent")
    print()
    rows = []
    for index, stamp in enumerate(report.timeouts):
        value = 0.4 * (2 ** index)
        rows.append([str(index + 1), f"{stamp:.2f} s", f"{value:.1f} s"])
    print(render.table(["timeout", "at", "RTO"], rows,
                       aligns=["right", "right", "right"]))
    print()
    print(f"delivered before the stall: {report.flow.bytes_acked:,} bytes")
    print("The window cannot help here: no data follows the hole, so no duplicate")
    print("ACKs ever arrive.  Only the timer can recover it, and the timer doubles.")
    return EXIT_OK


def cmd_trace(args: argparse.Namespace) -> int:
    scenario = _scenario(args)
    run = sim.run_flows(
        [("flow", make(args.cc))], seconds=args.seconds, path=scenario.path(),
        rcvbuf=args.window, record=True, mss=scenario.mss,
    )
    rows = (run.trace or [])[: args.count]
    if args.json:
        _emit(
            [{"time": t, "dir": d, "seq": s, "ack": a, "flags": f, "length": n}
             for t, d, s, a, f, n in rows],
            True,
        )
        return EXIT_OK
    print(f"{scenario.label()}, first {len(rows)} segments")
    print()
    print(
        render.table(
            ["time", "dir", "flags", "seq", "ack", "bytes"],
            [[f"{t:.4f}", d, pk.flags_to_text(f), f"{s:,}", f"{a:,}", str(n)]
             for t, d, s, a, f, n in rows],
            aligns=["right", "left", "left", "right", "right", "right"],
        )
    )
    return EXIT_OK


def cmd_demo(args: argparse.Namespace) -> int:
    if not args.name:
        print("demos:")
        for name in demos.DEMO_NAMES:
            print(f"  {name:<10} {demos.DESCRIPTIONS[name]}")
        return EXIT_OK
    if args.name not in demos.DEMO_NAMES:
        print(f"no such demo: {args.name}", file=sys.stderr)
        return EXIT_USAGE
    demo = demos.build(args.name)
    print(demo.title)
    print("=" * len(demo.title))
    print()
    print(demo.body)
    return EXIT_OK


# -- plumbing ------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sluice",
        description="A packet-level TCP laboratory: congestion control you can watch.",
    )
    parser.add_argument("--version", action="version", version=f"sluice {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="command")

    def add_common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--cc", default="reno", choices=sorted(CONTROLS))
        sub.add_argument("--mbps", type=float, default=2.0, help="bottleneck rate")
        sub.add_argument("--rtt", type=float, default=80.0, help="round trip, ms")
        sub.add_argument("--queue", type=int, default=200, help="buffer, segments")
        sub.add_argument("--seconds", type=float, default=10.0)
        sub.add_argument("--window", type=int, default=65_535,
                         help="receive buffer, bytes")
        sub.add_argument("--json", action="store_true")

    run = subparsers.add_parser("run", help="one bulk transfer")
    add_common(run)
    run.add_argument("--loss", type=float, default=0.0)
    run.set_defaults(func=cmd_run)

    loss = subparsers.add_parser("loss", help="throughput against loss rate")
    add_common(loss)
    loss.add_argument("--at", type=float, nargs="+",
                      default=[0.0, 0.002, 0.01, 0.03])
    loss.add_argument("--all", action="store_true", help="every control")
    loss.set_defaults(func=cmd_loss)

    fair = subparsers.add_parser("fair", help="N flows, one bottleneck")
    add_common(fair)
    fair.add_argument("--flows", type=int, default=4)
    fair.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    fair.add_argument("--loss", type=float, default=0.005)
    fair.set_defaults(func=cmd_fair, window=16_384)

    mix = subparsers.add_parser("mix", help="different controls, same link")
    add_common(mix)
    mix.add_argument("--flows", default="reno,vegas")
    mix.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    mix.add_argument("--loss", type=float, default=0.005)
    mix.set_defaults(func=cmd_mix, window=32_768)

    bloat = subparsers.add_parser("bloat", help="queueing delay with no loss")
    add_common(bloat)
    bloat.set_defaults(func=cmd_bloat, mbps=1.0, queue=300)

    wrap = subparsers.add_parser("wrap", help="cross 2**32 mid-transfer")
    add_common(wrap)
    wrap.set_defaults(func=cmd_wrap, mbps=1.0, rtt=40.0, queue=100, seconds=6.0)

    tail = subparsers.add_parser("tail", help="one loss no ACK can repair")
    add_common(tail)
    tail.set_defaults(func=cmd_tail, queue=100, seconds=7.0)

    trace = subparsers.add_parser("trace", help="the first segments of a run")
    add_common(trace)
    trace.add_argument("--count", type=int, default=40)
    trace.add_argument("--loss", type=float, default=0.01)
    trace.set_defaults(func=cmd_trace, seconds=1.0)

    demo = subparsers.add_parser("demo", help="self-checking demonstrations")
    demo.add_argument("name", nargs="?")
    demo.set_defaults(func=cmd_demo)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_OK
    try:
        return args.func(args)
    except sim.IntegrityError as problem:
        print(f"integrity failure: {problem}", file=sys.stderr)
        return EXIT_BROKEN
    except (ValueError, KeyError) as problem:
        print(f"error: {problem}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
