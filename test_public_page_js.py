"""Unit tests for the closure-span formatter inside tools/public_template.html.

The template is a browser file, not an importable module, so the one piece of
it with real logic is fenced between marker comments and executed under node.
Skips when node is absent -- the same shape as the database tests, which skip
rather than fail when unarmed, so `python3 -m unittest discover` stays safe to
run anywhere.

Why this exists: the first draft of this code subtracted 24 hours from every
half-open end, which renders the monthly maintenance window -- 24 Aug
00:00-17:00 -- as "24 Aug - 23 Aug". The second draft fixed the arithmetic but
took the month name from Intl, which returns "Sept" on current ICU builds and
"Sep" on older ones, so the same page said different things in different
browsers.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).with_name("tools").joinpath("public_template.html")
NODE = shutil.which("node")


def extract_block(name):
    """Return the JS fenced between `// --- <name>:begin` and `:end`."""
    text = TEMPLATE.read_text(encoding="utf-8")
    begin = text.index(f"// --- {name}:begin")
    end = text.index(f"// --- {name}:end")
    return text[begin:end]


@unittest.skipIf(NODE is None, "node not installed")
class ClosureSpanTests(unittest.TestCase):
    def span(self, from_iso, to_iso):
        script = (
            extract_block("closure-span")
            + f"\nprocess.stdout.write(closureSpan({json.dumps(from_iso)},"
              f" {json.dumps(to_iso)}));"
        )
        return subprocess.run([NODE, "-e", script], text=True,
                              capture_output=True, check=True).stdout

    def test_a_multi_day_range_names_the_last_closed_day(self):
        # Half-open to 21 Sep 00:00 means the 20th is the last closed day.
        self.assertEqual(
            self.span("2026-08-17T00:00:00+08:00", "2026-09-21T00:00:00+08:00"),
            "17 Aug – 20 Sep",
        )

    def test_a_single_whole_day_is_named_once(self):
        self.assertEqual(
            self.span("2026-08-17T00:00:00+08:00", "2026-08-18T00:00:00+08:00"),
            "17 Aug",
        )

    def test_a_part_day_range_keeps_its_clock_time(self):
        # The monthly maintenance morning. Subtracting a day here produced
        # "24 Aug – 23 Aug".
        self.assertEqual(
            self.span("2026-08-24T00:00:00+08:00", "2026-08-24T17:00:00+08:00"),
            "24 Aug · until 17:00",
        )

    def test_it_reads_the_range_in_taipei_not_the_host_timezone(self):
        # The same two instants written in UTC. A viewer in London must still
        # be told 24 August, because that is when the gym is shut.
        self.assertEqual(
            self.span("2026-08-23T16:00:00Z", "2026-08-24T09:00:00Z"),
            "24 Aug · until 17:00",
        )

    def test_september_is_abbreviated_the_same_on_every_browser(self):
        # Intl's `month: "short"` says "Sept" on current ICU and "Sep" on
        # older builds. The banner must not depend on that.
        self.assertIn("Sep ", self.span("2026-09-01T00:00:00+08:00",
                                        "2026-09-30T00:00:00+08:00") + " ")


if __name__ == "__main__":
    unittest.main()
