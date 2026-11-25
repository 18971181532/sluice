"""The demonstrations check themselves; this checks that they still can."""

from __future__ import annotations

import unittest

from sluice import demos


class DemoTests(unittest.TestCase):
    def test_every_demo_builds(self):
        for name in demos.DEMO_NAMES:
            demo = demos.build(name)
            self.assertEqual(demo.name, name)
            self.assertTrue(demo.title)
            self.assertTrue(demo.body)

    def test_build_all_is_in_order(self):
        built = demos.build_all()
        self.assertEqual([demo.name for demo in built], list(demos.DEMO_NAMES))
        self.assertEqual(len(built), len(demos.DESCRIPTIONS))

    def test_lines_stay_readable(self):
        for demo in demos.build_all():
            for line in demo.body.splitlines():
                self.assertLessEqual(len(line), 100, f"{demo.name}: {line!r}")

    def test_an_unknown_name_is_refused(self):
        with self.assertRaises(ValueError):
            demos.build("nope")

    def test_titles_are_distinct(self):
        titles = [demos.build(name).title for name in demos.DEMO_NAMES]
        self.assertEqual(len(titles), len(set(titles)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
