"""Unit tests for the browser-side logic of the public page.

Two sources, one technique. `tools/public_template.html` holds the page shell;
`grafana/weekly-dashboard.json` holds the panel code the page reuses, extracted
from the dashboard by tools/render_public_html.py. Neither is an importable
module, so the pieces with real logic are fenced between marker comments and
executed under node.
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
DASHBOARD = Path(__file__).with_name("grafana").joinpath("weekly-dashboard.json")
NODE = shutil.which("node")


def fenced(text, name):
    """Return the JS fenced between `// --- <name>:begin` and `:end`."""
    begin = text.index(f"// --- {name}:begin")
    end = text.index(f"// --- {name}:end")
    return text[begin:end]


def extract_block(name):
    return fenced(TEMPLATE.read_text(encoding="utf-8"), name)


def extract_panel_block(panel_id, name):
    """Same, but from a panel's getOption inside the dashboard JSON."""
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    panel = next(p for p in dashboard["panels"] if p.get("id") == panel_id)
    return fenced(panel["options"]["getOption"], name)


def run_js(script):
    return subprocess.run([NODE, "-e", script], text=True,
                          capture_output=True, check=True).stdout


def panel_option(panel_id, times, values, closures=None, extra="{}"):
    """Run a whole getOption body, not just a fenced helper.

    The fenced blocks cover the arithmetic; this covers the wiring around it --
    which series option the result lands in, and whether some other key
    (connectNulls, say) quietly undoes it.
    """
    body = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    js = next(p for p in body["panels"]
              if p.get("id") == panel_id)["options"]["getOption"]
    ctx = {
        "panel": {"data": {"series": [{"fields": [
            {"type": "time", "values": [None] * len(times)},
            {"type": "number", "values": values},
        ]}]}},
    }
    script = (
        "const ctx = " + json.dumps(ctx) + ";\n"
        "ctx.panel.data.series[0].fields[0].values = "
        + json.dumps(times) + ".map((s) => Date.parse(s));\n"
        + ("ctx.windowClosures = " + json.dumps(closures) + ";\n"
           if closures is not None else "")
        + "ctx.grafana = { replaceVariables: (s) => String(s) };\n"
        "const opt = new Function('context', " + json.dumps(js) + ")(ctx);\n"
        "process.stdout.write(JSON.stringify((" + extra + ").f "
        "? (" + extra + ").f(opt) : opt, (k, v) => "
        "typeof v === 'function' ? '[fn]' : v));"
    )
    return json.loads(run_js(script))


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


@unittest.skipIf(NODE is None, "node not installed")
class ClosureBandTests(unittest.TestCase):
    """Panel 3 maps a closure's timestamps onto ordinal category indices.

    The axis has one slot per reading, so a band cannot be expressed in time
    at all -- it has to be resolved to the first and last reading the closure
    covers. Getting that wrong is silent: an empty range becomes a zero-width
    markArea drawn at the origin rather than a visible error.
    """

    # 09:00, 09:05, ... 09:25 on 2026-08-24, the fourth Monday.
    TIMES = [f"2026-08-24T09:{m:02d}:00+08:00" for m in (0, 5, 10, 15, 20, 25)]

    def bands(self, closures, times=None):
        script = (
            extract_panel_block(3, "closure-bands")
            + "\nconst t = " + json.dumps(times if times is not None else self.TIMES)
            + ".map((s) => Date.parse(s));"
            + "\nprocess.stdout.write(JSON.stringify("
              "closureBands(t, " + json.dumps(closures) + ")));"
        )
        return json.loads(run_js(script))

    def closure(self, frm, to, reason="Monthly maintenance"):
        return {"venue": "健身中心", "from": frm, "to": to, "reason": reason}

    def test_a_closure_before_the_window_draws_nothing(self):
        self.assertEqual(self.bands([self.closure(
            "2026-08-24T07:00:00+08:00", "2026-08-24T08:00:00+08:00")]), [])

    def test_a_closure_after_the_window_draws_nothing(self):
        self.assertEqual(self.bands([self.closure(
            "2026-08-24T10:00:00+08:00", "2026-08-24T11:00:00+08:00")]), [])

    def test_a_closure_covering_everything_spans_the_whole_axis(self):
        self.assertEqual(self.bands([self.closure(
            "2026-08-24T00:00:00+08:00", "2026-08-25T00:00:00+08:00")]),
            [{"from": 0, "to": 5, "reason": "Monthly maintenance"}])

    def test_a_closure_covering_the_middle_clamps_to_real_readings(self):
        # 09:07 - 09:18 contains the 09:10 and 09:15 readings only.
        self.assertEqual(self.bands([self.closure(
            "2026-08-24T09:07:00+08:00", "2026-08-24T09:18:00+08:00")]),
            [{"from": 2, "to": 3, "reason": "Monthly maintenance"}])

    def test_the_range_is_half_open_at_both_ends(self):
        # Starts exactly on the 09:05 reading, ends exactly on 09:20: the
        # 09:20 reading is outside, the 09:05 one is inside.
        self.assertEqual(self.bands([self.closure(
            "2026-08-24T09:05:00+08:00", "2026-08-24T09:20:00+08:00")]),
            [{"from": 1, "to": 3, "reason": "Monthly maintenance"}])

    def test_a_closure_matching_one_reading_is_still_a_band(self):
        self.assertEqual(self.bands([self.closure(
            "2026-08-24T09:08:00+08:00", "2026-08-24T09:12:00+08:00")]),
            [{"from": 2, "to": 2, "reason": "Monthly maintenance"}])

    def test_an_empty_window_draws_nothing(self):
        # The venue has no readings at all in the window -- a closed venue
        # whose collector was also down. An unguarded implementation returns
        # a band from -1 to -1 here.
        self.assertEqual(self.bands([self.closure(
            "2026-08-24T00:00:00+08:00", "2026-08-25T00:00:00+08:00")],
            times=[]), [])

    def test_two_closures_produce_two_bands(self):
        self.assertEqual(
            self.bands([
                self.closure("2026-08-24T08:59:00+08:00",
                             "2026-08-24T09:06:00+08:00", "Renovation"),
                self.closure("2026-08-24T09:19:00+08:00",
                             "2026-08-24T09:30:00+08:00"),
            ]),
            [{"from": 0, "to": 1, "reason": "Renovation"},
             {"from": 4, "to": 5, "reason": "Monthly maintenance"}])

    def test_overlapping_closures_shade_once(self):
        """2026-08-24 is inside the renovation *and* is a fourth Monday.

        Both rows come back from window_closure_rows, covering the same
        readings. One band each would shade the morning twice -- ECharts
        composites the fills, so it renders visibly darker -- and stack two
        labels at the same position.
        """
        self.assertEqual(
            self.bands([
                self.closure("2026-08-17T00:00:00+08:00",
                             "2026-09-21T00:00:00+08:00", "Renovation"),
                self.closure("2026-08-24T00:00:00+08:00",
                             "2026-08-24T17:00:00+08:00"),
            ]),
            [{"from": 0, "to": 5, "reason": "Renovation"}])

    def test_closures_meeting_end_to_end_keep_their_own_labels(self):
        """Back-to-back closures are two bands, not one.

        Deduplication is for a closure *nested* in another, where one shade
        would be drawn twice. Two distinct reasons are two facts, and merging
        them would silently discard the second one's label.
        """
        self.assertEqual(
            self.bands([
                self.closure("2026-08-24T09:00:00+08:00",
                             "2026-08-24T09:15:00+08:00", "First"),
                self.closure("2026-08-24T09:15:00+08:00",
                             "2026-08-24T09:30:00+08:00", "Second"),
            ]),
            [{"from": 0, "to": 2, "reason": "First"},
             {"from": 3, "to": 5, "reason": "Second"}])

    def test_no_closures_at_all_is_the_grafana_path(self):
        # Grafana's context has no windowClosures, so the panel passes
        # undefined and must get an empty list rather than throwing.
        script = (extract_panel_block(3, "closure-bands")
                  + "\nprocess.stdout.write(JSON.stringify("
                    "closureBands([1, 2, 3], undefined)));")
        self.assertEqual(json.loads(run_js(script)), [])


@unittest.skipIf(NODE is None, "node not installed")
class ClosedReadingsAreBlankedTests(unittest.TestCase):
    """During a closure the counter measures staff, not public occupancy.

    The readings stay on the axis -- dropping them would collapse a 35-day
    closure into one step -- but the line is not drawn through them. Plotting
    0 was the alternative and is worse: 0 is a value that genuinely occurs,
    both at opening time and all day on 2026-06-19.
    """

    TIMES = [f"2026-08-24T09:{m:02d}:00+08:00" for m in (0, 5, 10, 15, 20, 25)]
    VALUES = [11, 12, 13, 14, 15, 16]

    def values(self, closures=None):
        return panel_option(3, self.TIMES, self.VALUES, closures,
                            extra="{ f: (o) => o.series[0].data }")

    def test_readings_inside_a_closure_are_not_drawn(self):
        self.assertEqual(
            self.values([{"venue": "健身中心",
                          "from": "2026-08-24T09:07:00+08:00",
                          "to": "2026-08-24T09:18:00+08:00",
                          "reason": "Monthly maintenance"}]),
            [11, 12, None, None, 15, 16])

    def test_readings_outside_any_closure_are_untouched(self):
        self.assertEqual(self.values([]), self.VALUES)

    def test_the_grafana_path_draws_every_reading(self):
        # No windowClosures in the context at all, which is what Grafana gives.
        self.assertEqual(self.values(None), self.VALUES)

    def test_the_line_is_not_reconnected_across_the_gap(self):
        """connectNulls would silently undo the whole thing."""
        series = panel_option(3, self.TIMES, self.VALUES, [],
                              extra="{ f: (o) => o.series[0] }")
        self.assertNotIn("connectNulls", series)

    def test_a_blanked_slot_reads_closed_rather_than_null(self):
        out = panel_option(
            3, self.TIMES, self.VALUES, [],
            extra="{ f: (o) => [o.tooltip.formatter("
                  "[{axisValue: '2026-08-24 09:10', value: null}]),"
                  " o.tooltip.formatter("
                  "[{axisValue: '2026-08-24 09:10', value: 13}]),"
                  " o.tooltip.formatter([])] }")
        self.assertIn("closed", out[0])
        self.assertNotIn("null", out[0])
        self.assertIn("13 people", out[1])
        self.assertEqual(out[2], "")


if __name__ == "__main__":
    unittest.main()
