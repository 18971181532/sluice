"""Serialisation, queueing and the shape of the dumbbell."""

from __future__ import annotations

import random
import unittest

from sluice.link import Link, Path, dumbbell


class SerialisationTests(unittest.TestCase):
    def test_last_bit_arrives_after_size_over_rate(self):
        link = Link(rate=8000, delay=0.01)  # 8 kB/s, 10 ms
        arrival = link.transmit(0.0, 1000)
        self.assertAlmostEqual(arrival, 0.125 + 0.01)

    def test_two_packets_queue_behind_each_other(self):
        link = Link(rate=8000)
        first = link.transmit(0.0, 1000)
        second = link.transmit(0.0, 1000)
        self.assertAlmostEqual(first, 0.125)
        self.assertAlmostEqual(second, 0.250, msg="the second waits for the first")

    def test_a_gap_in_time_leaves_the_link_idle(self):
        link = Link(rate=8000)
        link.transmit(0.0, 1000)
        self.assertEqual(link.backlog(0.5), 0.0, "the first packet is long gone")
        self.assertAlmostEqual(link.transmit(0.5, 1000), 0.625)

    def test_zero_length_segment_still_pays_propagation(self):
        link = Link(rate=8000, delay=0.02)
        self.assertAlmostEqual(link.transmit(0.0, 0), 0.02)

    def test_backlog_is_measured_in_bytes_that_would_wait(self):
        link = Link(rate=8000)
        link.transmit(0.0, 1000)
        self.assertAlmostEqual(link.backlog(0.05), 600)
        self.assertAlmostEqual(link.queue_delay(0.05), 0.075)
        self.assertEqual(link.backlog(1.0), 0.0)

    def test_peak_backlog_is_remembered(self):
        link = Link(rate=8000, queue_bytes=1 << 20)
        for _ in range(4):
            link.transmit(0.0, 1000)
        self.assertAlmostEqual(link.peak_backlog, 3000)

    def test_rate_and_delay_are_validated(self):
        for kwargs in ({"rate": 0}, {"rate": -1}, {"rate": 1, "delay": -0.1},
                       {"rate": 1, "queue_bytes": -1}, {"rate": 1, "loss": 1.0}):
            with self.assertRaises(ValueError):
                Link(**kwargs)


class DropTailTests(unittest.TestCase):
    def test_a_full_buffer_drops(self):
        link = Link(rate=8000, queue_bytes=2000)
        self.assertIsNotNone(link.transmit(0.0, 1000))
        self.assertIsNotNone(link.transmit(0.0, 1000))
        self.assertIsNone(link.transmit(0.0, 1000), "2000 bytes already waiting")
        self.assertEqual(link.dropped, 1)

    def test_a_drop_does_not_disturb_the_queue(self):
        link = Link(rate=8000, queue_bytes=2000)
        link.transmit(0.0, 1000)
        link.transmit(0.0, 1000)
        self.assertIsNone(link.transmit(0.0, 1000))
        self.assertAlmostEqual(link.backlog(0.0), 2000, msg="the drop changed nothing")

    def test_zero_buffer_accepts_nothing(self):
        link = Link(rate=8000, queue_bytes=0)
        self.assertIsNone(link.transmit(10.0, 1), "no room for even one byte")

    def test_a_segment_larger_than_the_buffer_is_dropped(self):
        link = Link(rate=8000, queue_bytes=500)
        self.assertIsNone(link.transmit(0.0, 1000))

    def test_a_buffer_exactly_one_segment_long_holds_one_segment(self):
        link = Link(rate=8000, queue_bytes=1000)
        self.assertIsNotNone(link.transmit(0.0, 1000))
        self.assertIsNone(link.transmit(0.0, 1000))


class LossTests(unittest.TestCase):
    def test_loss_zero_never_drops(self):
        link = Link(rate=8000, loss=0.0)
        rng = random.Random(1)
        for _ in range(100):
            self.assertIsNotNone(link.transmit(0.0, 10, rng))
        self.assertEqual(link.dropped, 0)

    def test_loss_is_reproducible_from_a_seed(self):
        def run(seed):
            link = Link(rate=8000, loss=0.5)
            rng = random.Random(seed)
            return [link.transmit(0.0, 10, rng) is None for _ in range(50)]

        self.assertEqual(run(7), run(7))
        self.assertNotEqual(run(7), run(8))
        self.assertTrue(any(run(7)))

    def test_without_an_rng_loss_is_ignored(self):
        link = Link(rate=8000, loss=0.999)
        self.assertIsNotNone(link.transmit(0.0, 10))


class PathTests(unittest.TestCase):
    def test_propagation_sums_both_directions(self):
        path = Path(
            [Link(1000, 0.01), Link(5000, 0.02)],
            [Link(5000, 0.03)],
        )
        self.assertAlmostEqual(path.propagation, 0.06)

    def test_forward_walk_accumulates_serialisation(self):
        path = Path([Link(1000, 0.0), Link(1000, 0.5)], [Link(1000, 0.0)])
        self.assertAlmostEqual(path.transmit_forward(0.0, 1000), 1.0 + 1.0 + 0.5)

    def test_a_drop_on_any_hop_drops_the_segment(self):
        path = Path([Link(1000, 0.0, queue_bytes=0), Link(1000, 0.0, queue_bytes=0)],
                    [Link(1000, 0.0)])
        self.assertIsNone(path.transmit_forward(0.0, 1))

    def test_bottleneck_is_the_slowest_forward_link(self):
        path = Path([Link(9000), Link(1000), Link(4000)], [Link(5000)])
        self.assertEqual(path.bottleneck.rate, 1000)

    def test_a_path_needs_both_directions(self):
        with self.assertRaises(ValueError):
            Path([], [Link(1000)])

    def test_reset_clears_the_queue(self):
        path = Path([Link(1000)], [Link(1000)])
        path.forward[0].transmit(0.0, 500)
        path.reset()
        self.assertEqual(path.forward[0].backlog(0.0), 0.0)


class DumbbellTests(unittest.TestCase):
    def test_rates_and_delays_add_up(self):
        path = dumbbell(mbps=8, rtt_ms=100, queue_packets=50)
        self.assertAlmostEqual(path.bottleneck.rate, 1_000_000)
        self.assertAlmostEqual(path.propagation, 0.100)
        self.assertEqual(path.bottleneck.queue_bytes, 50 * 1000)

    def test_the_access_links_are_faster_than_the_bottleneck(self):
        path = dumbbell(mbps=2, rtt_ms=20, queue_packets=10)
        self.assertTrue(all(link.rate >= path.bottleneck.rate
                            for link in path.forward))

    def test_a_packet_crosses_in_half_the_rtt(self):
        path = dumbbell(mbps=100, rtt_ms=100, queue_packets=10)
        # 1000 bytes on 12.5 MB/s is 80 microseconds, invisible next to 50 ms.
        arrival = path.transmit_forward(0.0, 1000)
        # Propagation is exactly 50 ms; three serialisation delays (bottleneck
        # plus two fast links) add well under a millisecond.
        self.assertGreaterEqual(arrival, 0.05)
        self.assertAlmostEqual(arrival, 0.05, delta=2e-4)
        ack = path.transmit_reverse(arrival, 20)
        self.assertAlmostEqual(ack - arrival, 0.05, delta=2e-4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
