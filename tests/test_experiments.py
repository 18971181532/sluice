"""The experiments, at small sizes.

These are smoke tests for the claims, not benchmarks: they run the same code
paths as the README numbers with shorter durations, and check the direction of
every effect rather than its precise value.
"""

from __future__ import annotations

import unittest

from sluice import experiments as ex
from sluice.cc import Cubic, Reno, Vegas


class ScenarioTests(unittest.TestCase):
    def test_bandwidth_delay_product(self):
        self.assertEqual(ex.Scenario(mbps=2.0, rtt_ms=80.0).bdp, 20_000)
        self.assertEqual(ex.Scenario(mbps=1.0, rtt_ms=100.0).bdp, 12_500)

    def test_path_has_the_requested_bottleneck(self):
        path = ex.Scenario(mbps=1.0, rtt_ms=50.0, queue=30).path()
        self.assertAlmostEqual(path.bottleneck.rate, 125_000)
        self.assertEqual(path.bottleneck.queue_bytes, 30_000)
        self.assertAlmostEqual(path.propagation, 0.05)

    def test_label_mentions_loss_only_when_there_is_some(self):
        self.assertNotIn("loss", ex.Scenario().label())
        self.assertIn("1%", ex.Scenario(loss=0.01).label())


class LossSweepTests(unittest.TestCase):
    def test_more_loss_means_less_throughput(self):
        rows = ex.loss_sweep(Reno(), ex.Scenario(seconds=4.0))
        clean = rows[0]
        worst = rows[-1]
        self.assertEqual(clean.loss, 0.0)
        self.assertGreater(clean.mbps, worst.mbps * 2)

    def test_mathis_is_only_quoted_when_it_means_something(self):
        rows = ex.loss_sweep(Reno(), ex.Scenario(seconds=4.0))
        self.assertEqual(rows[0].mathis_mbps, 0.0)
        self.assertEqual(rows[0].ratio, 0.0)
        for row in rows[1:]:
            self.assertGreater(row.mathis_mbps, 0)
            self.assertGreater(row.ratio, 0)

    def test_rows_follow_the_requested_losses(self):
        rows = ex.loss_sweep(Reno(), ex.Scenario(seconds=3.0), losses=(0.0, 0.02))
        self.assertEqual([row.loss for row in rows], [0.0, 0.02])

    def test_losses_cost_retransmissions(self):
        rows = ex.loss_sweep(Reno(), ex.Scenario(seconds=5.0), losses=(0.01,))
        self.assertGreater(rows[0].retransmits, 0)


class FairnessTests(unittest.TestCase):
    def test_four_flows_split_the_link(self):
        report = ex.fairness(Reno(), ex.Scenario(seconds=4.0, loss=0.005),
                             flows=3, seeds=(0, 1))
        self.assertEqual(len(report.runs), 2)
        self.assertGreater(report.mean_jain(), 0.9)
        self.assertEqual(len(report.spread()), 2)
        self.assertEqual(len(report.spread()[0]), 3)

    def test_jain_values_are_bounded(self):
        report = ex.fairness(Reno(), ex.Scenario(seconds=3.0, loss=0.005),
                             flows=2, seeds=(0,))
        for value in report.jain():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)


class MixTests(unittest.TestCase):
    def test_two_controls_share_the_path(self):
        run = ex.mix([Reno(), Vegas()], ex.Scenario(seconds=4.0, loss=0.005))
        self.assertEqual({flow.control for flow in run.flows}, {"reno", "vegas"})
        self.assertGreater(run.goodput, 0)
        self.assertLessEqual(run.jain(), 1.0)


class BufferbloatTests(unittest.TestCase):
    def test_delay_based_beats_loss_based_on_queueing(self):
        rows = ex.bufferbloat([Reno(), Cubic(), Vegas()], ex.Scenario(mbps=1.0, seconds=6.0))
        by_name = {row.control: row for row in rows}
        self.assertLess(by_name["vegas"].queue_ms, by_name["reno"].queue_ms / 3)
        self.assertLess(by_name["vegas"].queue_ms, by_name["cubic"].queue_ms / 3)
        for row in rows:
            self.assertAlmostEqual(row.mbps, by_name["reno"].mbps, delta=0.1)
            self.assertGreaterEqual(row.peak_ms, row.queue_ms)


class WraparoundTests(unittest.TestCase):
    def test_the_stream_crosses_the_boundary(self):
        report = ex.wraparound(Reno(), ex.Scenario(mbps=1.0, rtt_ms=40.0, seconds=4.0),
                               bytes_to_send=40_000)
        self.assertTrue(report.wrapped)
        self.assertEqual(report.flow.bytes_acked, 40_000)


class TailLossTests(unittest.TestCase):
    def test_the_timer_fires_and_backs_off(self):
        report = ex.tail_loss(Reno(), ex.Scenario(seconds=5.0), bytes_to_send=30_000)
        self.assertGreaterEqual(len(report.timeouts), 3)
        history = report.flow.rto_history
        self.assertGreaterEqual(len(history), 3)
        for earlier, later in zip(history, history[1:]):
            self.assertAlmostEqual(later, earlier * 2)
        self.assertEqual(report.rto, history[-1])
        self.assertLess(report.flow.bytes_acked, 30_000, "the tail never gets through")

    def test_the_gap_is_exactly_one_segment(self):
        report = ex.tail_loss(Reno(), ex.Scenario(seconds=4.0), bytes_to_send=20_000)
        self.assertEqual(report.flow.bytes_acked, 19_000)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
