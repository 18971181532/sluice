"""Sequence numbers are a circle; these tests walk around it."""

from __future__ import annotations

import unittest

from sluice import seq


class AddTests(unittest.TestCase):
    def test_plain_addition(self):
        self.assertEqual(seq.add(100, 50), 150)

    def test_wraps_at_two_to_the_thirty_two(self):
        self.assertEqual(seq.add(seq.MOD - 1, 1), 0)
        self.assertEqual(seq.add(seq.MOD - 10, 25), 15)

    def test_zero_and_huge_steps(self):
        self.assertEqual(seq.add(7, 0), 7)
        self.assertEqual(seq.add(7, seq.MOD * 3 + 5), 12)

    def test_wrap_folds_negatives(self):
        self.assertEqual(seq.wrap(-1), seq.MOD - 1)


class CompareTests(unittest.TestCase):
    def test_ordinary_order(self):
        self.assertEqual(seq.compare(1, 2), -1)
        self.assertEqual(seq.compare(2, 1), 1)
        self.assertEqual(seq.compare(2, 2), 0)

    def test_order_across_the_wrap(self):
        low, high = seq.MOD - 5, 5
        self.assertTrue(seq.before(low, high), "2**32-5 comes before 5")
        self.assertTrue(seq.after(high, low))
        self.assertEqual(seq.distance(high, low), 10)

    def test_distance_is_signed_and_small(self):
        self.assertEqual(seq.distance(110, 100), 10)
        self.assertEqual(seq.distance(100, 110), -10)
        self.assertEqual(seq.distance(seq.MOD - 1, 0), -1)

    def test_half_the_space_is_not_ordered_but_is_deterministic(self):
        """Exactly 2**31 apart is genuinely ambiguous; the tie-break matters less
        than the fact that it is the same every time."""
        a, b = 0, seq.HALF
        self.assertEqual(seq.compare(a, b), 1)
        self.assertEqual(seq.compare(b, a), 1, "both ends look ahead at the boundary")
        self.assertEqual((seq.compare(a, b), seq.compare(b, a)),
                         (seq.compare(a, b), seq.compare(b, a)))

    def test_before_and_after_agree_with_compare(self):
        for a, b in ((0, 1), (1, 0), (seq.MOD - 3, 4), (4, seq.MOD - 3)):
            self.assertEqual(seq.before(a, b), seq.compare(a, b) < 0)
            self.assertEqual(seq.after(a, b), seq.compare(a, b) > 0)

    def test_small_steps_stay_ordered_even_when_they_wrap(self):
        """The property that matters: a window of a few thousand bytes is never
        ambiguous, no matter where it starts."""
        base = seq.MOD - 2000
        for i in range(0, 4000, 37):
            for j in range(0, 4000, 41):
                self.assertEqual(
                    seq.compare(seq.add(base, i), seq.add(base, j)),
                    (i > j) - (i < j),
                    f"i={i} j={j}",
                )


class IntervalTests(unittest.TestCase):
    def test_between_plain(self):
        self.assertTrue(seq.between(5, 0, 10))
        self.assertFalse(seq.between(10, 0, 10), "the high end is exclusive")
        self.assertTrue(seq.between(0, 0, 10), "the low end is inclusive")

    def test_between_across_the_wrap(self):
        low, high = seq.MOD - 10, 10
        self.assertTrue(seq.between(seq.MOD - 1, low, high))
        self.assertTrue(seq.between(0, low, high))
        self.assertTrue(seq.between(9, low, high))
        self.assertFalse(seq.between(10, low, high))
        self.assertFalse(seq.between(seq.MOD - 11, low, high))

    def test_empty_interval_contains_nothing(self):
        self.assertFalse(seq.between(3, 3, 3))

    def test_span(self):
        self.assertEqual(seq.span(10, 20), 10)
        self.assertEqual(seq.span(seq.MOD - 4, 4), 8)
        self.assertEqual(seq.span(4, 4), 0)

    def test_split_a_wrapping_segment(self):
        self.assertEqual(seq.split(seq.MOD - 4, 10), (4, 6))
        self.assertEqual(seq.split(100, 10), (10, 0))
        with self.assertRaises(ValueError):
            seq.split(0, -1)


class OverlapTests(unittest.TestCase):
    def test_disjoint(self):
        self.assertEqual(seq.overlaps(0, 10, 10, 10), 0)
        self.assertEqual(seq.overlaps(10, 10, 0, 10), 0)

    def test_partial(self):
        self.assertEqual(seq.overlaps(0, 10, 5, 10), 5)
        self.assertEqual(seq.overlaps(5, 10, 0, 10), 5)

    def test_containment(self):
        self.assertEqual(seq.overlaps(0, 100, 10, 10), 10)
        self.assertEqual(seq.overlaps(10, 10, 0, 100), 10)

    def test_identical(self):
        self.assertEqual(seq.overlaps(7, 20, 7, 20), 20)

    def test_across_the_wrap(self):
        start = seq.MOD - 5
        self.assertEqual(seq.overlaps(start, 10, 0, 5), 5)
        self.assertEqual(seq.overlaps(0, 5, start, 10), 5)

    def test_empty(self):
        self.assertEqual(seq.overlaps(0, 0, 0, 10), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
