"""The three controls, with no packets in sight.

Each test drives a scripted sequence of ACKs.  If a window curve is wrong here,
it is wrong in the simulator too, and this is a much easier place to see it.
"""

from __future__ import annotations

import math
import unittest

from sluice import cc
from sluice.cc import MSS, Cubic, Reno, Vegas, mathis


def round_trip(control, segments, rtt=0.1, now=0.0, ack_size=MSS):
    """ACK one window's worth of data and return the time afterwards."""
    for index in range(segments):
        control.on_ack(ack_size, now + index * rtt / max(segments, 1), rtt)
    return now + rtt


class RenoSlowStartTests(unittest.TestCase):
    def test_window_doubles_every_round_trip(self):
        reno = Reno()
        self.assertEqual(reno.cwnd, MSS)
        now = 0.0
        for expected in (2, 4, 8, 16):
            now = round_trip(reno, reno.cwnd // MSS, now=now)
            self.assertEqual(reno.cwnd, expected * MSS)

    def test_it_leaves_slow_start_at_the_threshold(self):
        reno = Reno()
        reno._ssthresh = 4 * MSS
        now = 0.0
        now = round_trip(reno, 1, now=now)
        now = round_trip(reno, 2, now=now)
        self.assertEqual(reno.cwnd, 4 * MSS)
        self.assertFalse(reno.in_slow_start)

    def test_congestion_avoidance_adds_one_segment_per_round_trip(self):
        reno = Reno()
        reno._cwnd = 10 * MSS
        reno._ssthresh = 10 * MSS
        round_trip(reno, 10)
        # The window grows while the round is being ACKed, so the total is a
        # hair under one full segment -- 10,958 rather than 11,000 here.
        self.assertAlmostEqual(reno.cwnd, 11 * MSS, delta=MSS // 10)

    def test_slow_start_gains_one_segment_per_ack(self):
        reno = Reno()
        for _ in range(5):
            reno.on_ack(MSS, 0.0, 0.1)
        self.assertEqual(reno.cwnd, 6 * MSS)


class RenoLossTests(unittest.TestCase):
    def test_a_triple_duplicate_ack_halves_the_window(self):
        reno = Reno()
        reno._cwnd = 40 * MSS
        reno.on_loss(1.0, timeout=False)
        self.assertEqual(reno.cwnd, 20 * MSS)
        self.assertEqual(reno.ssthresh, 20 * MSS)
        self.assertEqual(reno.losses, 1)

    def test_a_timeout_drops_to_one_segment(self):
        reno = Reno()
        reno._cwnd = 40 * MSS
        reno.on_loss(1.0, timeout=True)
        self.assertEqual(reno.cwnd, MSS)
        self.assertEqual(reno.ssthresh, 20 * MSS)

    def test_the_floor_is_two_segments(self):
        reno = Reno()
        reno._cwnd = 3 * MSS
        reno.on_loss(1.0)
        self.assertEqual(reno.cwnd, 2 * MSS)

    def test_zero_or_negative_acks_are_ignored(self):
        reno = Reno()
        before = reno.cwnd
        reno.on_ack(0, 0.0, 0.1)
        reno.on_ack(-5, 0.0, 0.1)
        self.assertEqual(reno.cwnd, before)


class CubicTests(unittest.TestCase):
    def test_a_loss_sets_the_window_to_beta_times_the_old_one(self):
        cubic = Cubic()
        cubic._cwnd = 100 * MSS
        cubic._ssthresh = 100 * MSS
        cubic.on_loss(0.0)
        self.assertAlmostEqual(cubic.cwnd, 70 * MSS, delta=1)
        self.assertAlmostEqual(cubic._w_max, 100 * MSS)

    def test_k_is_the_time_to_climb_back(self):
        cubic = Cubic()
        cubic._cwnd = 100 * MSS
        cubic.on_loss(0.0)
        expected_k = ((100 * 0.3) / cubic.C) ** (1 / 3)
        self.assertAlmostEqual(cubic._k, expected_k, places=6)

    def test_the_cubic_passes_through_the_old_ceiling_at_k(self):
        cubic = Cubic()
        cubic._cwnd = 100 * MSS
        cubic.on_loss(0.0)
        self.assertAlmostEqual(cubic._target(cubic._k) / MSS, 100.0, places=6)

    def test_growth_decelerates_near_the_ceiling(self):
        cubic = Cubic()
        cubic._cwnd = 100 * MSS
        cubic.on_loss(0.0)
        early = cubic._target(0.5) - cubic._target(0.0)
        late = cubic._target(cubic._k + 0.5) - cubic._target(cubic._k)
        self.assertGreater(early, late, "cubic is concave past its inflection")

    def test_it_grows_after_a_loss_rather_than_sitting_still(self):
        cubic = Cubic()
        cubic._cwnd = 50 * MSS
        cubic._ssthresh = 50 * MSS
        cubic.on_loss(0.0)
        before = cubic.cwnd
        for step in range(40):
            cubic.on_ack(MSS, 0.1 + step * 0.001, 0.1)
        self.assertGreater(cubic.cwnd, before)

    def test_slow_start_before_the_first_loss(self):
        cubic = Cubic()
        cubic.on_ack(MSS, 0.0, 0.1)
        self.assertEqual(cubic.cwnd, 2 * MSS)


class VegasTests(unittest.TestCase):
    def setUp(self):
        self.vegas = Vegas()
        self.vegas._cwnd = 50 * MSS
        self.vegas._ssthresh = 50 * MSS
        self.vegas._base_rtt = 0.1

    def test_it_adds_a_segment_when_the_path_is_empty(self):
        self.vegas.on_ack(MSS, 1.0, 0.1)
        self.assertEqual(self.vegas.cwnd, 51 * MSS)

    def test_it_holds_inside_the_corridor(self):
        # diff = 50 * (0.104 - 0.1) / 0.104 ~ 1.9, between alpha and beta.
        self.vegas.on_ack(MSS, 1.0, 0.104)
        self.assertEqual(self.vegas.cwnd, 50 * MSS)

    def test_it_backs_off_when_a_queue_is_visible(self):
        self.vegas.on_ack(MSS, 1.0, 0.12)
        self.assertEqual(self.vegas.cwnd, 49 * MSS)

    def test_it_adjusts_at_most_once_per_round_trip(self):
        self.vegas.on_ack(MSS, 1.0, 0.1)
        self.vegas.on_ack(MSS, 1.01, 0.1)
        self.vegas.on_ack(MSS, 1.02, 0.1)
        self.assertEqual(self.vegas.cwnd, 51 * MSS)

    def test_base_rtt_never_grows(self):
        self.vegas.on_ack(MSS, 1.0, 0.3)
        self.assertAlmostEqual(self.vegas._base_rtt, 0.1)

    def test_a_duplicate_ack_loss_costs_a_quarter_of_the_window(self):
        """Gentler than Reno's half, but not free: the decrease still happens."""
        before = self.vegas.cwnd
        self.vegas.on_loss(2.0, timeout=False)
        self.assertEqual(self.vegas.cwnd, int(before * 0.75))

    def test_a_timeout_does(self):
        self.vegas.on_loss(2.0, timeout=True)
        self.assertEqual(self.vegas.cwnd, 2 * MSS)

    def test_it_leaves_slow_start_when_the_queue_appears(self):
        vegas = Vegas()
        vegas._cwnd = 10 * MSS
        vegas._ssthresh = cc.INFINITE
        vegas._base_rtt = 0.1
        vegas.on_ack(MSS, 1.0, 0.2)  # diff ~ 5
        self.assertEqual(vegas.ssthresh, 10 * MSS)


class RegistryTests(unittest.TestCase):
    def test_names(self):
        for name in ("reno", "cubic", "vegas"):
            self.assertEqual(cc.make(name).name, name)
            self.assertEqual(cc.make(name.upper()).name, name)

    def test_unknown_name_lists_the_alternatives(self):
        with self.assertRaises(ValueError) as caught:
            cc.make("bbr")
        self.assertIn("cubic", str(caught.exception))

    def test_bad_parameters(self):
        with self.assertRaises(ValueError):
            Reno(mss=0)
        with self.assertRaises(ValueError):
            Reno(initial_window=0)


class MathisTests(unittest.TestCase):
    def test_it_falls_as_one_over_root_p(self):
        self.assertAlmostEqual(mathis(1000, 0.1, 0.01) / mathis(1000, 0.1, 0.04), 2.0)

    def test_it_scales_with_mss_and_rtt(self):
        self.assertAlmostEqual(mathis(2000, 0.1, 0.01), 2 * mathis(1000, 0.1, 0.01))
        self.assertAlmostEqual(mathis(1000, 0.05, 0.01), 2 * mathis(1000, 0.1, 0.01))

    def test_it_refuses_nonsense(self):
        with self.assertRaises(ValueError):
            mathis(1000, 0.0, 0.01)
        with self.assertRaises(ValueError):
            mathis(1000, 0.1, 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
