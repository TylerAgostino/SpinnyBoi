# pyright: basic
"""spinfo renders in an OS temp dir: no CWD litter on success or mid-loop
failure, BytesIO payloads intact (issue #164)."""

import asyncio
import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import modules.wheelCog as wheelModule
from modules.wheelCog import WheelCog, _WheelOption

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x01\x00\x00\xff\xff\x03\x00!\xba\x0fK\x00\x00\x00\x00IEND\xaeB`\x82"
)


def make_cog(n_options):
    cog = SimpleNamespace()
    cog.presets = SimpleNamespace(load=lambda: None, preset=mock.MagicMock())
    cog.presets.preset.return_value.resolve_tab.return_value = ("tab1", "")
    cog.generate_option_set = mock.AsyncMock(
        return_value=[
            _WheelOption(f"opt{i}", 1, None, None) for i in range(n_options)
        ]
    )
    return cog


def run_spinfo(cog, export):
    with mock.patch.object(wheelModule.dfi, "export", export):
        return asyncio.run(WheelCog.spinfo.__wrapped__(cog, None, "p", "t"))


class TempDirRenderingTests(unittest.TestCase):
    def setUp(self):
        self.cwd_before = set(os.listdir("."))
        self.seen_paths = []

    def tearDown(self):
        self.assertEqual(set(os.listdir(".")), self.cwd_before)

    def writer(self, fail_on=None):
        def fake_export(df, path, **kwargs):
            with open(path, "wb") as fh:
                fh.write(PNG)
            self.seen_paths.append(path)
            if fail_on is not None and len(self.seen_paths) == fail_on:
                raise RuntimeError("export died")

        return fake_export

    def test_success_exports_only_under_temp_dir(self):
        cog = make_cog(35)
        message, images, name = run_spinfo(cog, self.writer())
        temp_root = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
        for path in self.seen_paths:
            self.assertTrue(
                os.path.normcase(os.path.abspath(path)).startswith(temp_root),
                f"{path} is not under {temp_root}",
            )
        self.assertEqual(len(self.seen_paths), 2)

    def test_payload_and_chunking_unchanged(self):
        cog = make_cog(35)
        message, images, name = run_spinfo(cog, self.writer())
        self.assertEqual(len(images), 2)
        for fh in images:
            self.assertIsInstance(fh, io.BytesIO)
            self.assertEqual(fh.read(), PNG)
        self.assertEqual(name, "options.png")

    def test_mid_loop_failure_leaves_no_files_behind(self):
        cog = make_cog(35)
        with self.assertRaisesRegex(RuntimeError, "export died"):
            run_spinfo(cog, self.writer(fail_on=2))
        # the failing chunk's file was written inside the temp dir, which
        # teardown must remove even though the loop never reached its end
        self.assertEqual(len(self.seen_paths), 2)
        last_dir = os.path.dirname(self.seen_paths[-1])
        self.assertFalse(os.path.exists(last_dir))


if __name__ == "__main__":
    unittest.main()
