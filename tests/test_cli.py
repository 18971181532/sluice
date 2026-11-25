"""Every subcommand, in-process, with its exit code checked.

The CLI is the only part of the project a user meets first, so it gets tested
the way a user meets it: run it, look at what came out, look at the exit code.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from sluice import cli


def run(argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        code = cli.main(argv)
    return code, out.getvalue()


class RunTests(unittest.TestCase):
    def test_a_short_transfer(self):
        code, text = run(["run", "--seconds", "1"])
        self.assertEqual(code, 0)
        self.assertIn("delivered", text)
        self.assertIn("Mbit/s", text)
        self.assertIn("┤", text, "the chart is part of the output")

    def test_json_is_parseable(self):
        code, text = run(["run", "--seconds", "1", "--json"])
        payload = json.loads(text)
        self.assertEqual(code, 0)
        self.assertEqual(payload["control"], "reno")
        self.assertGreater(payload["mbps"], 0)
        self.assertEqual(payload["scenario"]["seconds"], 1)

    def test_the_loss_flag_changes_the_result(self):
        clean = json.loads(run(["run", "--seconds", "2", "--json"])[1])
        lossy = json.loads(run(["run", "--seconds", "2", "--loss", "0.03", "--json"])[1])
        self.assertGreater(clean["mbps"], lossy["mbps"])


class LossTests(unittest.TestCase):
    def test_a_sweep_prints_the_prediction_column(self):
        code, text = run(["loss", "--at", "0", "0.01", "--seconds", "2"])
        self.assertEqual(code, 0)
        self.assertIn("Mathis", text)
        self.assertIn("1.00%", text)

    def test_all_three_controls(self):
        code, text = run(["loss", "--all", "--at", "0.01", "--seconds", "1"])
        self.assertEqual(code, 0)
        for name in ("reno", "cubic", "vegas"):
            self.assertIn(name, text)

    def test_json_has_one_entry_per_control(self):
        code, text = run(["loss", "--all", "--at", "0", "0.01", "--seconds", "1", "--json"])
        payload = json.loads(text)
        self.assertEqual(set(payload), {"reno", "cubic", "vegas"})
        self.assertEqual(len(payload["reno"]), 2)


class FairnessTests(unittest.TestCase):
    def test_two_flows_one_seed(self):
        code, text = run(["fair", "--flows", "2", "--seeds", "0", "--seconds", "2"])
        self.assertEqual(code, 0)
        self.assertIn("Jain", text)
        self.assertIn("mean Jain index", text)

    def test_json(self):
        code, text = run(
            ["fair", "--flows", "2", "--seeds", "0", "1", "--seconds", "1", "--json"]
        )
        payload = json.loads(text)
        self.assertEqual(payload["flows"], 2)
        self.assertEqual(len(payload["jain"]), 2)
        self.assertEqual(len(payload["shares"]), 2)


class MixTests(unittest.TestCase):
    def test_two_named_controls(self):
        code, text = run(["mix", "--flows", "reno,vegas", "--seconds", "2"])
        self.assertEqual(code, 0)
        self.assertIn("reno", text)
        self.assertIn("vegas", text)
        self.assertIn("Jain", text)

    def test_an_empty_list_is_a_usage_error(self):
        code, _ = run(["mix", "--flows", " , ", "--seconds", "1"])
        self.assertEqual(code, cli.EXIT_USAGE)

    def test_json(self):
        code, text = run(["mix", "--flows", "reno,vegas", "--seconds", "1", "--json"])
        payload = json.loads(text)
        self.assertEqual(set(payload["controls"]), {"reno", "vegas"})
        self.assertEqual(len(payload["jain"]), 3)


class BloatTests(unittest.TestCase):
    def test_the_table_names_all_three(self):
        code, text = run(["bloat", "--seconds", "2"])
        self.assertEqual(code, 0)
        self.assertIn("avg queue", text)
        self.assertIn("vegas", text)

    def test_json_is_a_list_of_rows(self):
        code, text = run(["bloat", "--seconds", "1", "--json"])
        payload = json.loads(text)
        self.assertEqual(len(payload), 3)
        self.assertIn("queue_ms", payload[0])


class WrapTests(unittest.TestCase):
    def test_it_reports_the_boundary(self):
        code, text = run(["wrap", "--seconds", "2"])
        self.assertEqual(code, 0)
        self.assertIn("2**32", text)
        self.assertIn("in order", text)

    def test_json(self):
        code, text = run(["wrap", "--seconds", "1", "--json"])
        payload = json.loads(text)
        self.assertTrue(payload["wrapped"])
        self.assertEqual(payload["delivered"], payload["bytes"])


class TailTests(unittest.TestCase):
    def test_the_rto_column_grows(self):
        code, text = run(["tail", "--seconds", "3"])
        self.assertEqual(code, 0)
        self.assertIn("RTO", text)
        self.assertIn("delivered before the stall", text)

    def test_json(self):
        code, text = run(["tail", "--seconds", "3", "--json"])
        payload = json.loads(text)
        self.assertGreaterEqual(len(payload["timeouts"]), 2)
        self.assertGreater(payload["rto"], 1.0)


class TraceTests(unittest.TestCase):
    def test_it_prints_the_requested_number_of_segments(self):
        code, text = run(["trace", "--count", "5", "--seconds", "1"])
        self.assertEqual(code, 0)
        body = [line for line in text.splitlines() if "flow" not in line]
        self.assertIn("flags", text)
        self.assertIn("seq", text)

    def test_json_rows_carry_the_header_fields(self):
        code, text = run(["trace", "--count", "3", "--seconds", "1", "--json"])
        payload = json.loads(text)
        self.assertEqual(len(payload), 3)
        for row in payload:
            self.assertIn(row["dir"], (">", "<"))
            self.assertGreaterEqual(row["length"], 0)


class DemoTests(unittest.TestCase):
    def test_listing(self):
        code, text = run(["demo"])
        self.assertEqual(code, 0)
        self.assertIn("sawtooth", text)
        self.assertIn("tail", text)

    def test_running_one(self):
        code, text = run(["demo", "bloat"])
        self.assertEqual(code, 0)
        self.assertIn("Same throughput", text)

    def test_an_unknown_demo_is_a_usage_error(self):
        code, _ = run(["demo", "nope"])
        self.assertEqual(code, cli.EXIT_USAGE)


class ParserTests(unittest.TestCase):
    def test_no_command_prints_help(self):
        code, text = run([])
        self.assertEqual(code, 0)
        self.assertIn("usage", text.lower())

    def test_an_unknown_control_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit) as caught:
            run(["run", "--cc", "bbr"])
        self.assertEqual(caught.exception.code, 2)

    def test_a_negative_seconds_is_refused_by_the_experiment(self):
        code, _ = run(["run", "--seconds", "0"])
        self.assertEqual(code, cli.EXIT_USAGE)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
