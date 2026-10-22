# pyright: basic
"""spinfo: preset resolution failures, percentage maths, zero-weight drop,
descending sort, 30-row PNG chunking, returned tuple."""

import asyncio
import io
import unittest
from types import SimpleNamespace
from unittest import mock

import modules.wheelCog as wheelModule
from modules.preset_source import PresetNotFound
from modules.wheelCog import WheelCog, _WheelOption

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x01\x00\x00\xff\xff\x03\x00!\xba\x0fK\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_cog(options):
    cog = SimpleNamespace()
    cog.presets = SimpleNamespace(load=lambda: None, preset=mock.MagicMock())
    cog.presets.preset.return_value.resolve_tab.return_value = ("tab1", "")
    cog.generate_option_set = mock.AsyncMock(return_value=options)
    return cog


def _write_png(df, path, **kwargs):
    with open(path, "wb") as fh:
        fh.write(PNG)


def run_spinfo(cog, preset_name="presetA", tab_name="tab1", export=_write_png):
    with mock.patch.object(wheelModule.dfi, "export", export):
        return asyncio.run(
            WheelCog.spinfo.__wrapped__(cog, None, preset_name, tab_name)
        )


def options_from(weights):
    return [
        _WheelOption(f"opt{i}", weight, None, None)
        for i, weight in enumerate(weights)
    ]


class ResolutionFailureTests(unittest.TestCase):
    def test_unknown_preset_returns_can_not_find_message(self):
        cog = make_cog([])
        cog.presets.preset.side_effect = PresetNotFound("gone")
        message, images, name = run_spinfo(cog)
        self.assertEqual(
            message, "I can't find tab tab1 on preset presetA."
        )
        self.assertIsNone(images)
        self.assertIsNone(name)

    def test_generate_failure_is_generic(self):
        cog = make_cog([])
        cog.generate_option_set = mock.AsyncMock(side_effect=ValueError("bad tab"))
        with self.assertLogs(level="ERROR"):
            message, images, name = run_spinfo(cog)
        self.assertEqual(
            message,
            "The preset presetA ran into an error: something unexpected went"
            " wrong - details are in the bot logs.",
        )
        self.assertNotIn("bad tab", message)
        self.assertIsNone(images)
        self.assertIsNone(name)


class PercentageTests(unittest.TestCase):
    def export_captures_frames(self):
        frames = []

        def fake_export(df, path, **kwargs):
            with open(path, "wb") as fh:
                fh.write(PNG)
            frames.append(df)

        return frames, fake_export

    def run_weights(self, weights):
        frames, export = self.export_captures_frames()
        cog = make_cog(options_from(weights))
        message, images, name = run_spinfo(cog, export=export)
        self.assertEqual(len(frames), 1)
        table = frames[0]
        self.assertEqual(list(table.columns), ["Option", "Percentage"])
        return table

    def test_zero_weight_options_are_dropped(self):
        table = self.run_weights([5, 0, 5])
        self.assertEqual(list(table["Option"]), ["opt0", "opt2"])

    def test_rows_sorted_by_percentage_descending(self):
        table = self.run_weights([1, 7, 3])
        self.assertEqual(
            list(table["Option"]), ["opt1", "opt2", "opt0"]
        )

    def test_two_dp_percentage_formatting(self):
        table = self.run_weights([5, 5, 1])
        self.assertEqual(
            list(table["Percentage"]), ["45.45%", "45.45%", "9.09%"]
        )

    def test_single_option_is_100_percent_and_tuple_shape(self):
        cog = make_cog(options_from([4]))
        message, images, name = run_spinfo(cog)
        self.assertIn("preset presetA, tab tab1", message)
        self.assertEqual(name, "options.png")
        self.assertEqual(len(images), 1)
        self.assertIsInstance(images[0], io.BytesIO)
        self.assertEqual(images[0].read(), PNG)


class ChunkingTests(unittest.TestCase):
    def export_records(self):
        records = []

        def fake_export(df, path, **kwargs):
            with open(path, "wb") as fh:
                fh.write(PNG)
            records.append((len(df), path))

        return records, fake_export

    def test_35_options_export_two_chunks_of_30_and_5(self):
        records, export = self.export_records()
        cog = make_cog(options_from([1] * 35))
        message, images, name = run_spinfo(cog, export=export)
        self.assertEqual([count for count, _ in records], [30, 5])
        self.assertEqual(len(images), 2)

    def test_temp_png_files_removed_after_each_chunk(self):
        import os

        seen_paths = []
        still_there_before_next_export = []

        def fake_export(df, path, **kwargs):
            if seen_paths:
                still_there_before_next_export.append(
                    os.path.exists(seen_paths[-1])
                )
            with open(path, "wb") as fh:
                fh.write(PNG)
            seen_paths.append(path)

        cog = make_cog(options_from([1] * 35))
        run_spinfo(cog, export=fake_export)
        self.assertEqual(still_there_before_next_export, [False])
        self.assertEqual(len(seen_paths), 2)
        for path in seen_paths:
            self.assertFalse(os.path.exists(path))


    def test_exactly_30_options_export_single_chunk(self):
        records, export = self.export_records()
        cog = make_cog(options_from([1] * 30))
        message, images, name = run_spinfo(cog, export=export)
        self.assertEqual([count for count, _ in records], [30])
        self.assertEqual(len(images), 1)

    def test_all_zero_weights_yield_no_images(self):
        cog = make_cog(options_from([0, 0]))
        message, images, name = run_spinfo(cog)
        self.assertEqual(images, [])
        self.assertEqual(name, "options.png")


if __name__ == "__main__":
    unittest.main()
