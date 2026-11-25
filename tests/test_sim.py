"""The event loop, the ledger, and the promise of reproducibility."""

from __future__ import annotations

import hashlib
import unittest

from sluice import packet as pk
from sluice import sim
from sluice.cc import Reno
from sluice.link import dumbbell


def digest(trace) -> str:
    shadow = hashlib.sha256()
    for row in trace:
        shadow.update(repr(row).encode())
    return shadow.hexdigest()[:16]


class PatternTests(unittest.TestCase):
    def test_pattern_is_periodic_and_position_dependent(self):
        self.assertEqual(sim.pattern(0, 10), sim.BLOCK[:10])
        self.assertEqual(sim.pattern(len(sim.BLOCK), 10), sim.BLOCK[:10])
        self.assertEqual(sim.pattern(5, 10), sim.BLOCK[5:15])

    def test_pattern_spans_the_block_boundary(self):
        self.assertEqual(
            sim.pattern(len(sim.BLOCK) - 3, 6),
            sim.BLOCK[-3:] + sim.BLOCK[:3],
        )

    def test_zero_length(self):
        self.assertEqual(sim.pattern(7, 0), b"")


class DeterminismTests(unittest.TestCase):
    def run_once(self, seed):
        run = sim.run_flows(
            [("a", Reno())], seconds=2.0,
            path=dumbbell(1.0, 40.0, 40, loss=0.01), seed=seed, record=True,
        )
        return run

    def test_the_same_seed_replays_exactly(self):
        first = self.run_once(3)
        second = self.run_once(3)
        self.assertEqual(digest(first.trace), digest(second.trace))
        self.assertEqual(first.flows[0].bytes_acked, second.flows[0].bytes_acked)

    def test_a_different_seed_changes_the_trace(self):
        self.assertNotEqual(
            digest(self.run_once(3).trace), digest(self.run_once(4).trace)
        )

    def test_the_trace_is_a_packet_log(self):
        run = self.run_once(0)
        self.assertGreater(len(run.trace), 20)
        times = [row[0] for row in run.trace]
        self.assertEqual(times, sorted(times), "time never runs backwards")
        self.assertTrue(all(row[1] in (">", "<") for row in run.trace))


class IntegrityTests(unittest.TestCase):
    def test_an_extra_byte_is_caught_at_the_segment(self):
        class Corrupting(sim.Flow):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.damaged = False

            def _deliver(self, segment, forward):
                super()._deliver(segment, forward)
                if forward and not self.damaged and len(self.receiver.delivered) > 500:
                    # Append a byte the sender never sent.  The next range the
                    # ledger checks starts here, so the extra byte is caught.
                    self.receiver.delivered.append(0x00)
                    self.damaged = True

        event_loop = sim.Sim(0)
        flow = Corrupting(event_loop, dumbbell(1.0, 40.0, 60), Reno(), name="bad")
        event_loop.add(flow)
        flow.start()
        with self.assertRaises(sim.IntegrityError):
            event_loop.run_for(2.0)

    def test_a_dropped_byte_is_caught(self):
        class Skipping(sim.Flow):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.damaged = False

            def _deliver(self, segment, forward):
                super()._deliver(segment, forward)
                if forward and not self.damaged and len(self.receiver.delivered) > 800:
                    del self.receiver.delivered[400]
                    self.damaged = True

        event_loop = sim.Sim(0)
        flow = Skipping(event_loop, dumbbell(1.0, 40.0, 60), Reno(), name="bad")
        event_loop.add(flow)
        flow.start()
        with self.assertRaises(sim.IntegrityError):
            event_loop.run_for(2.0)

    def test_a_clean_run_delivers_exactly_the_bytes_sent(self):
        run = sim.run_flows(
            [("a", Reno())], seconds=3.0,
            path=dumbbell(2.0, 40.0, 100, loss=0.02),
        )
        flow = run.flows[0]
        self.assertGreater(flow.bytes_acked, 0)
        self.assertEqual(flow.bytes_acked % 1000, 0, "everything acked is whole segments")


class RunTests(unittest.TestCase):
    def test_two_flows_produce_two_results(self):
        run = sim.run_flows(
            [("a", Reno()), ("b", Reno())], seconds=2.0,
            path=dumbbell(2.0, 40.0, 100), rcvbuf=16384,
        )
        self.assertEqual(len(run.flows), 2)
        self.assertEqual({flow.name for flow in run.flows}, {"a", "b"})
        self.assertGreater(run.delivered, 0)

    def test_jain_is_one_for_equal_shares(self):
        run = sim.Run(
            flows=[
                sim.FlowResult("a", "reno", 100, 1.0, 100.0, 0, 0, 0, 0, 0, 0, 0, 0),
                sim.FlowResult("b", "reno", 100, 1.0, 100.0, 0, 0, 0, 0, 0, 0, 0, 0),
            ],
            wall=1.0, dropped=0, seed=0,
        )
        self.assertAlmostEqual(run.jain(), 1.0)

    def test_jain_is_one_over_n_when_one_flow_starves(self):
        run = sim.Run(
            flows=[
                sim.FlowResult("a", "reno", 100, 1.0, 100.0, 0, 0, 0, 0, 0, 0, 0, 0),
                sim.FlowResult("b", "reno", 0, 1.0, 0.0, 0, 0, 0, 0, 0, 0, 0, 0),
            ],
            wall=1.0, dropped=0, seed=0,
        )
        self.assertAlmostEqual(run.jain(), 0.5)

    def test_by_name(self):
        run = sim.run_flows([("only", Reno())], seconds=1.0, path=dumbbell(1, 40, 50))
        self.assertEqual(run.by_name("only").control, "reno")
        with self.assertRaises(KeyError):
            run.by_name("missing")

    def test_a_bounded_transfer_completes(self):
        run = sim.run_flows(
            [("a", Reno())], seconds=10.0, path=dumbbell(2.0, 40.0, 100),
            limit_bytes=50_000,
        )
        self.assertEqual(run.flows[0].bytes_acked, 50_000)

    def test_drops_are_counted_once_per_link(self):
        run = sim.run_flows(
            [("a", Reno())], seconds=5.0,
            path=dumbbell(2.0, 40.0, 5, loss=0.05),
        )
        self.assertGreater(run.dropped, 0)
        self.assertGreater(run.flows[0].retransmits, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
