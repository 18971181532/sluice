"""Text layout, including the parts that are easy to get wrong."""

from __future__ import annotations

import unittest

from sluice import render


class WidthTests(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(render.width("abc"), 3)
        self.assertEqual(render.width(""), 0)

    def test_cjk_is_two_columns(self):
        self.assertEqual(render.width("杭州"), 4)
        self.assertEqual(render.width("a杭"), 3)

    def test_combining_marks_are_free(self):
        self.assertEqual(render.width("e\u0301"), 1)

    def test_pad_uses_display_width(self):
        self.assertEqual(render.pad("杭", 4), "杭  ")
        self.assertEqual(render.pad("杭", 4, "right"), "  杭")


class TableTests(unittest.TestCase):
    def test_columns_line_up(self):
        text = render.table(["key", "value"], [["a", "1"], ["longer", "22"]])
        lines = text.splitlines()
        self.assertEqual(lines[0], "key     value")
        self.assertEqual(lines[1], "──────  ─────")
        self.assertEqual(lines[2], "a       1")

    def test_alignment(self):
        text = render.table(["n"], [["1"], ["22"]], aligns=["right"])
        self.assertEqual(text.splitlines()[2], " 1")
        self.assertEqual(text.splitlines()[3], "22")

    def test_cjk_cells_do_not_break_the_grid(self):
        text = render.table(["k", "v"], [["杭州", "1"], ["ab", "2"]])
        rows = text.splitlines()[2:]
        self.assertEqual(render.width(rows[0]), render.width(rows[1]))

    def test_pairs_has_no_rule(self):
        text = render.pairs([["a", "1"], ["bb", "2"]])
        self.assertEqual(text.splitlines(), ["a   1", "bb  2"])

    def test_plural(self):
        self.assertEqual(render.plural(1, "byte"), "byte")
        self.assertEqual(render.plural(0, "byte"), "bytes")


class FormatTests(unittest.TestCase):
    def test_size(self):
        self.assertEqual(render.size(999), "999 B")
        self.assertEqual(render.size(1500), "1.5 KB")
        self.assertEqual(render.size(2_500_000), "2.5 MB")

    def test_number(self):
        self.assertEqual(render.number(7), "7")
        self.assertEqual(render.number(1234), "1,234")
        self.assertEqual(render.number(1.25), "1.2")

    def test_rates_and_times(self):
        self.assertEqual(render.rate(1.5), "1.50 Mbit/s")
        self.assertEqual(render.millis(0.25), "250.0 ms")
        self.assertEqual(render.millis(2.5), "2.50 s")
        self.assertEqual(render.percent(0.0125), "1.25%")

    def test_bar_fills_and_clamps(self):
        self.assertEqual(render.bar(5, 10, 10), "█████·····")
        self.assertEqual(render.bar(20, 10, 4), "████")
        self.assertEqual(render.bar(0, 10, 4), "····")
        self.assertEqual(render.bar(5, 0), "")


class SparklineTests(unittest.TestCase):
    def test_length_is_preserved(self):
        self.assertEqual(len(render.sparkline([1, 2, 3, 4, 5], 5)), 5)

    def test_it_is_downsampled_to_the_requested_width(self):
        self.assertEqual(len(render.sparkline(list(range(100)), 20)), 20)

    def test_a_flat_series_does_not_divide_by_zero(self):
        self.assertEqual(render.sparkline([3, 3, 3], 3), "▁▁▁")

    def test_empty(self):
        self.assertEqual(render.sparkline([], 10), "")


class ChartTests(unittest.TestCase):
    def test_it_draws_one_line_per_row_plus_an_axis(self):
        text = render.chart([(0, 1), (1, 5), (2, 3)], width_of=20, height=6)
        self.assertEqual(len(text.splitlines()), 6 + 2)

    def test_the_title_and_unit_are_shown(self):
        text = render.chart([(0, 1)], title="cwnd", unit="KB")
        self.assertIn("cwnd", text)
        self.assertIn("KB", text)

    def test_empty_input_says_so(self):
        self.assertEqual(render.chart([]), "(no samples)")

    def test_a_single_point_is_a_flat_line(self):
        text = render.chart([(0, 10)], width_of=10, height=4)
        self.assertIn("─", text)

    def test_time_runs_left_to_right(self):
        text = render.chart([(0, 1), (10, 10)], width_of=10, height=5)
        lines = [line for line in text.splitlines() if "┤" in line]
        top = lines[0]
        bottom = lines[-1]
        # A climb leaves the bottom row through a ╯ and enters the top row
        # through a ╭: the stroke goes up, then right.
        self.assertTrue(top.rstrip().endswith("╭"))
        self.assertIn("╯", bottom)

    def test_a_descent_uses_the_other_pair_of_corners(self):
        text = render.chart([(0, 10), (10, 1)], width_of=10, height=5)
        lines = [line for line in text.splitlines() if "┤" in line]
        self.assertTrue(lines[0].rstrip().endswith("╮"))
        self.assertIn("╰", lines[-1])

    def test_a_gap_between_samples_holds_the_last_value(self):
        # Samples arrive at events, not on a fixed grid.  A bucket with no
        # sample must keep the previous value: falling back to zero would draw
        # a spike per sample instead of one line.
        text = render.chart([(0, 0), (0.5, 10), (1.0, 10)], width_of=16, height=4)
        lines = [line for line in text.splitlines() if "┤" in line]
        body = [line.split("┤", 1)[1] for line in lines]
        self.assertEqual(sum(row.count("╭") for row in body), 1)
        self.assertEqual(sum(row.count("╮") for row in body), 0)
        self.assertEqual(sum(row.count("│") for row in body), 2)

    def test_rows_carry_no_trailing_blanks(self):
        text = render.chart([(0, 3), (1, 9)], width_of=20, height=4)
        for line in text.splitlines():
            self.assertEqual(line, line.rstrip())


class WrapTests(unittest.TestCase):
    def test_it_respects_the_limit(self):
        text = render.wrap("word " * 30, limit=30)
        for line in text.splitlines():
            self.assertLessEqual(render.width(line), 30)

    def test_indent_is_kept(self):
        text = render.wrap("one two three", limit=20, indent="  ")
        self.assertTrue(text.startswith("  "))

    def test_a_single_long_word_is_not_broken(self):
        self.assertEqual(render.wrap("supercalifragilistic", limit=5),
                         "supercalifragilistic")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
