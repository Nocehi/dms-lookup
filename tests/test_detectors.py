#!/usr/bin/env python3
"""Local data detectors: whole-selection matches, local-only computation."""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from dank_lookup_core import lookup  # noqa: E402
from dank_lookup_detectors import detect, detector_settings, format_number  # noqa: E402
from test_core import FakeClient, MissingClient  # noqa: E402


TZ = dt.timezone(dt.timedelta(hours=8))
NOW = dt.datetime(2026, 11, 6, 23, 54, tzinfo=TZ)
MODULE = ROOT / "lib/dank_lookup_detectors.py"


def run(query, config=None):
    return detect(query, config or {}, now=NOW)


def one(test, query, kind, config=None):
    found = run(query, config)
    test.assertEqual(len(found), 1, f"{query!r}: {found}")
    test.assertEqual(found[0]["kind"], kind)
    return found[0]


class ImportBoundaryTests(unittest.TestCase):
    FORBIDDEN = {
        "socket", "ssl", "http", "urllib.request", "urllib.error", "ftplib", "smtplib",
        "subprocess", "multiprocessing", "asyncio", "requests", "shutil", "tempfile",
        "pathlib", "ctypes", "webbrowser",
    }

    def test_module_imports_no_network_process_or_file_api(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        hits = {name for name in imported for bad in self.FORBIDDEN
                if name == bad or name.startswith(bad + ".")}
        self.assertEqual(hits, set())
        self.assertNotIn("open(", MODULE.read_text(encoding="utf-8"))

    def test_urllib_parse_is_the_only_urllib_use(self):
        tree = ast.parse(MODULE.read_text(encoding="utf-8"))
        modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual({name for name in modules if name and name.startswith("urllib")},
                         {"urllib.parse"})


class DateTests(unittest.TestCase):
    def test_iso_date_with_calendars(self):
        found = one(self, "2026-11-06", "date")
        self.assertEqual(found["lines"][0], "2026-11-06（星期五）")
        self.assertEqual(found["lines"][1], "今天")
        self.assertIn("民國 115 年 · 令和 8 年 · 丙午年", found["lines"])
        self.assertIn("ISO 2026-W45 · 年內第 310 日", found["lines"])
        self.assertEqual(found["copy"][0]["value"], "2026-11-06")

    def test_cjk_era_and_english_forms_agree(self):
        for query in ("民國115年11月6日", "2026年11月6日", "令和8年11月6日", "Nov 6, 2026",
                      "6 November 2026", "Friday, November 6, 2026", "11月6日", "２０２６－１１－０６"):
            with self.subTest(query=query):
                self.assertEqual(one(self, query, "date")["copy"][0]["value"], "2026-11-06")

    def test_era_year_only(self):
        found = one(self, "令和元年", "date")
        self.assertEqual(found["lines"][0], "西元 2019 年")
        self.assertEqual(one(self, "民國元年", "date")["lines"][0], "西元 1912 年")

    def test_era_boundaries(self):
        self.assertIn("平成 31 年", one(self, "2019-04-30", "date")["lines"][2])
        self.assertIn("令和 元 年", one(self, "2019-05-01", "date")["lines"][2])

    def test_ganzhi_notes_spring_boundary(self):
        self.assertIn("丙午年（立春／春節前屬前一年）", one(self, "2026-02-01", "date")["lines"][2])

    def test_ambiguous_numeric_date_lists_both_readings(self):
        with mock.patch.dict(os.environ, {"LC_ALL": "zh_TW.UTF-8"}):
            found = one(self, "03/04/2026", "date")
        self.assertIn("日月順序不明", found["lines"][0])
        self.assertEqual(found["copy"][0]["value"], "2026-04-03")
        found = one(self, "03/04/2026", "date", {"detectors": {"date_order": "mdy"}})
        self.assertEqual(found["copy"][0]["value"], "2026-03-04")
        self.assertEqual(one(self, "25/12/2026", "date")["copy"][0]["value"], "2026-12-25")

    def test_relative_days(self):
        self.assertEqual(one(self, "2026-11-09", "date")["lines"][1], "3 天後")
        self.assertEqual(one(self, "2026-11-05", "date")["lines"][1], "昨天")

    def test_datetime_with_zone_converts_to_local(self):
        found = one(self, "2026-11-06T15:00:00Z", "date")
        self.assertTrue(found["lines"][0].startswith("本機：2026-11-06 23:00:00"))

    def test_time_with_zone(self):
        found = one(self, "15:00 UTC", "date")
        self.assertEqual(found["copy"][0]["value"], "23:00")
        self.assertEqual(one(self, "3 PM PST", "date")["copy"][0]["value"], "07:00")
        self.assertEqual(one(self, "09:30 UTC+9", "date")["copy"][0]["value"], "08:30")

    def test_calendars_can_be_limited(self):
        found = one(self, "2026-11-06", "date", {"detectors": {"calendars": ["roc"]}})
        self.assertEqual(found["lines"][2:], ["民國 115 年"])

    def test_non_dates(self):
        for query in ("May", "March 2026", "2026年", "2026-13-01", "2026-02-30",
                      "15 UTC", "12:00", "3 PM CST", "1-2"):
            with self.subTest(query=query):
                self.assertEqual([d["kind"] for d in run(query)], [])


class TimestampTests(unittest.TestCase):
    def test_seconds_and_milliseconds(self):
        found = one(self, "1793980800", "timestamp")
        self.assertIn("UTC：2026-11-06 16:00:00", found["lines"])
        self.assertEqual(found["copy"][0]["value"], "2026-11-07T00:00:00+08:00")
        one(self, "1793980800123", "timestamp")

    def test_out_of_range_numbers_are_ignored(self):
        for query in ("0912345678", "999999999", "12345678901", "5000000000"):
            with self.subTest(query=query):
                self.assertEqual(run(query), [])


class UrlEmailTests(unittest.TestCase):
    def test_idn_tracking_and_path(self):
        found = one(self, "https://例子.tw/a%20b?utm_source=x&id=3#top", "url")
        self.assertIn("Punycode：xn--fsqu00a.tw", found["lines"])
        self.assertIn("路徑：/a b", found["lines"])
        self.assertIn("追蹤參數：utm_source", found["lines"])
        self.assertEqual([c["value"] for c in found["copy"]],
                         ["https://例子.tw/a%20b?utm_source=x&id=3#top", "https://例子.tw/a%20b?id=3#top"])

    def test_punycode_host_is_decoded(self):
        self.assertIn("Unicode：例.com", one(self, "https://xn--fsq.com", "url")["lines"])

    def test_userinfo_reveals_real_host(self):
        found = one(self, "http://google.com@evil.com/", "url")
        self.assertEqual(found["lines"][0], "主機：evil.com")
        self.assertTrue(any("實際連往的主機是 evil.com" in line for line in found["lines"]))

    def test_bare_www(self):
        found = one(self, "www.example.com/?fbclid=1", "url")
        self.assertEqual(found["copy"][1]["value"], "www.example.com/")

    def test_email(self):
        found = one(self, "mailto:nocehi@example.com", "email")
        self.assertEqual(found["copy"][0]["value"], "nocehi@example.com")

    def test_sentences_with_urls_do_not_trigger(self):
        self.assertEqual(run("see https://example.com now"), [])
        self.assertEqual(run("a@b"), [])


class ColorNumberTests(unittest.TestCase):
    def test_colour_forms(self):
        for query in ("#1e66f5", "rgb(30,102,245)", "RGB(30, 102, 245)"):
            with self.subTest(query=query):
                self.assertEqual(one(self, query, "color")["swatch"], "#1e66f5")
        self.assertEqual(one(self, "#fff", "color")["copy"][0]["value"], "#ffffff")
        self.assertEqual(one(self, "hsl(0, 100%, 50%)", "color")["swatch"], "#ff0000")

    def test_issue_numbers_are_not_colours(self):
        self.assertEqual(run("#123"), [])
        self.assertEqual(run("rgb(300,0,0)"), [])

    def test_number_bases(self):
        self.assertEqual(one(self, "0x1F", "number_base")["copy"][0]["value"], "31")
        self.assertIn("權限：rwxr-xr-x", one(self, "0o755", "number_base")["lines"])
        self.assertIn("二進位：0b1010", one(self, "0b1010", "number_base")["lines"])
        self.assertEqual(run("0x"), [])
        self.assertEqual(run("0x" + "f" * 40), [])


class UnitTests(unittest.TestCase):
    def test_metric_source_leads_with_imperial(self):
        self.assertEqual(one(self, "5 km", "unit")["copy"][0]["value"], "3.107 mi")
        self.assertEqual(one(self, "1 kg", "unit")["lines"][1], "2.205 lb")

    def test_imperial_source_leads_with_metric(self):
        self.assertEqual(one(self, "60 mph", "unit")["copy"][0]["value"], "96.56 km/h")
        self.assertEqual(one(self, "5'8\"", "unit")["copy"][0]["value"], "172.7 cm")
        self.assertEqual(one(self, "5 呎 8 吋", "unit")["copy"][0]["value"], "172.7 cm")

    def test_temperature(self):
        self.assertEqual(one(self, "72°F", "unit")["copy"][0]["value"], "22.22 °C")
        self.assertEqual(one(self, "100℃", "unit")["copy"][0]["value"], "212 °F")
        self.assertEqual(one(self, "-40 °C", "unit")["copy"][0]["value"], "-40 °F")
        self.assertEqual(run("-500 °C"), [])

    def test_regional_catty_lists_every_standard(self):
        found = one(self, "2 斤", "unit")
        self.assertEqual([c["label"] for c in found["copy"]], ["台斤", "市斤", "港斤"])
        self.assertEqual(found["copy"][1]["value"], "1,000 g")

    def test_ping_leads_with_square_metres(self):
        self.assertEqual(one(self, "10 坪", "unit")["copy"][0]["value"], "33.06 m²")
        self.assertEqual(one(self, "100 m²", "unit")["lines"][0], "100 m²")

    def test_data_sizes(self):
        found = one(self, "500 GB", "unit")
        self.assertIn("465.7 GiB（二進位）", found["lines"])
        self.assertEqual(found["copy"][0]["value"], "500000000000")
        self.assertNotIn("2 GiB（二進位）", one(self, "2 GiB", "unit")["lines"])

    def test_non_units(self):
        for query in ("5K", "10", "Linux", "5 apples", "一二三", "km"):
            with self.subTest(query=query):
                self.assertEqual(run(query), [])

    def test_format_number(self):
        self.assertEqual(format_number(5000.0), "5,000")
        self.assertEqual(format_number(0.000123456), "0.0001235")
        self.assertEqual(format_number(3.10686), "3.107")


class ConfigTests(unittest.TestCase):
    def test_disable_all_and_per_kind(self):
        self.assertEqual(run("2026-11-06", {"detectors": {"enabled": False}}), [])
        self.assertEqual(run("5 km", {"detectors": {"kinds": {"unit": False}}}), [])
        self.assertEqual(len(run("5 km", {"detectors": {"kinds": {"date": False}}})), 1)

    def test_bad_config_types_fall_back(self):
        settings = detector_settings({"detectors": {"kinds": [], "calendars": "x",
                                                    "date_order": 3, "max_query_chars": "a"}})
        self.assertTrue(all(settings["kinds"].values()))
        self.assertEqual(settings["date_order"], "auto")
        self.assertEqual(settings["max_query_chars"], 200)
        self.assertEqual(len(settings["calendars"]), 4)

    def test_long_and_multiline_queries_are_skipped(self):
        self.assertEqual(run("https://example.com/" + "a" * 300), [])
        self.assertEqual(run("2026-11-06\n2026-11-07"), [])


class CoreIntegrationTests(unittest.TestCase):
    def test_lookup_carries_detections_even_without_sdcv(self):
        result, _ = lookup("2026-11-06", client=MissingClient())
        self.assertEqual(result["error"]["code"], "sdcv_missing")
        self.assertEqual(result["detections"][0]["kind"], "date")
        json.dumps(result, ensure_ascii=False)

    def test_lookup_detections_alongside_dictionary_results(self):
        result, _ = lookup("5 km", client=FakeClient())
        self.assertEqual(result["detections"][0]["kind"], "unit")

    def test_word_has_no_detections(self):
        result, _ = lookup("word", client=MissingClient())
        self.assertEqual(result["detections"], [])

    def test_detector_failure_is_a_warning_not_an_error(self):
        with mock.patch("dank_lookup_core.detect_data", side_effect=RuntimeError("boom")):
            result, _ = lookup("word", client=FakeClient())
        self.assertEqual(result["detections"], [])
        self.assertIn("detector_error", result["warnings"])

    def test_config_disable_reaches_core(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "config.json"
            path.write_text(json.dumps({"modes": {}, "detectors": {"enabled": False}}), encoding="utf-8")
            result, _ = lookup("2026-11-06", config_path=path, client=MissingClient())
        self.assertEqual(result["detections"], [])


if __name__ == "__main__":
    unittest.main()
