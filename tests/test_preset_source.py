# pyright: basic
"""Tests for the PresetSource seam: fake sheets on disk, no Google needed.

Run: .venv\\Scripts\\python -m unittest tests.test_preset_source
"""
import asyncio
import os
import tempfile
import unittest
from unittest import mock

from modules.env_config import MissingEnvError
from modules.preset_source import Preset, PresetNotFound, PresetSource

PRESETS_CSV = (
    "Fullname,My Cup Series,Weeknight Battle\n"
    "S16,series:8579 season:16,day:=friday\n"
    "S20,series:8579 season:20,\n"
)
OPTIONS_CSV = "Fullname,Points\nDriver A,10\nDriver B,7\n"


def fake_csv_url(directory: str):
    def csv_url(sheet: str) -> str:
        data = PRESETS_CSV if sheet == "presets" else OPTIONS_CSV
        path = os.path.join(directory, f"{sheet}.csv")
        if not os.path.exists(path):
            with open(path, "w") as fh:
                fh.write(data)
        return path

    return csv_url


class PresetSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.source = PresetSource(fake_csv_url(self.tmp.name))

    def test_names_follow_sheet_order(self):
        self.assertEqual(self.source.names(), ["S16", "S20"])

    def test_preset_filters_excludes_name_and_blank_cells(self):
        preset = self.source.preset("s16")  # lookup is case-insensitive
        self.assertEqual(
            preset.filters(),
            [
                ("My Cup Series", "series:8579 season:16"),
                ("Weeknight Battle", "day:=friday"),
            ],
        )

    def test_resolve_tab_matches_case_insensitively(self):
        tab, filter_string = self.source.preset("S16").resolve_tab("weeknight battle")
        self.assertEqual(tab, "Weeknight Battle")
        self.assertEqual(filter_string, "day:=friday")

    def test_resolve_tab_missing_raises(self):
        with self.assertRaises(PresetNotFound):
            self.source.preset("S16").resolve_tab("Nope")

    def test_missing_preset_raises_and_tabs_returns_empty(self):
        with self.assertRaises(PresetNotFound):
            self.source.preset("S99")
        self.assertEqual(self.source.tabs("S99"), [])

    def test_refresh_failure_keeps_old_frame(self):
        self.assertTrue(asyncio.run(self.source.refresh()))

        def broken_url(sheet: str) -> str:
            raise ConnectionError("sheet went away")

        self.source._csv_url = broken_url
        self.assertFalse(asyncio.run(self.source.refresh()))
        self.assertEqual(self.source.names(), ["S16", "S20"])

    def test_read_tab_returns_option_frame(self):
        frame = asyncio.run(self.source.read_tab("anything"))
        self.assertEqual(list(frame["Fullname"]), ["Driver A", "Driver B"])


class FromEnvTests(unittest.TestCase):
    def test_missing_sheet_id_raises_named_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(MissingEnvError):
                PresetSource.from_env()

    def test_sheet_id_interpolated_into_urls(self):
        with mock.patch.dict(os.environ, {"GSHEET_ID": "abc123"}):
            source = PresetSource.from_env()
        url = source._csv_url("presets")
        self.assertIn("/d/abc123/", url)
        self.assertNotIn("None", url)


if __name__ == "__main__":
    unittest.main()
