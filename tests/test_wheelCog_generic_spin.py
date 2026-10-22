# pyright: basic
"""generic_spin_preset: preset/tab resolution failures, multi-tab wheels,
next_spin chain following with depth cap, gif+response assembly, slots."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import modules.wheelCog as wheelModule
from modules.preset_source import PresetNotFound
from modules.wheelCog import WheelCog, _WheelOption


class FakeWheel:
    instances = []

    def __init__(self, opt_set):
        self.opt_set = opt_set
        self.response = f"resp:{opt_set[0].option}"
        FakeWheel.instances.append(self)

    @property
    def next_spin(self):
        queue = getattr(self.opt_set[0], "chain", None)
        if queue:
            return queue[0]
        return None

    def return_gif(self, driver):
        return f"gif:{self.opt_set[0].option}"


def chained_option(name, chain):
    opt = _WheelOption(name, 1, None, None)
    opt.chain = chain
    return opt


def preset_with(filters):
    return SimpleNamespace(filters=lambda: iter(filters))


def make_cog(preset, tab_options=None, get_message=lambda: "SPIN!"):
    cog = SimpleNamespace()
    cog.presets = SimpleNamespace(load=lambda: None, preset=mock.MagicMock())
    cog.presets.preset.return_value = preset
    options = {} if tab_options is None else tab_options

    async def gen(tab, filter_string=""):
        if isinstance(options, Exception):
            raise options
        return options[tab]

    cog.generate_option_set = mock.Mock(side_effect=gen)
    cog.get_message = get_message
    return cog


def run(cog, preset_name="p", role=None):
    return asyncio.run(
        WheelCog.generic_spin_preset(cog, None, preset_name, role=role)
    )


def run_patched(cog, preset_name="p", role=None):
    FakeWheel.instances = []
    with mock.patch.object(wheelModule.WheelSpinner, "WheelSpinner", FakeWheel):
        return run(cog, preset_name, role)


class FailureTests(unittest.TestCase):
    def test_unknown_preset_message(self):
        cog = make_cog(mock.MagicMock())
        cog.presets.preset.side_effect = PresetNotFound("nope")
        self.assertEqual(
            run(cog, "missing"),
            ("I can't find a preset named missing.", None, None),
        )

    def test_tab_generate_failure_is_generic(self):
        cog = make_cog(
            preset_with([("drivers", "")]), tab_options=RuntimeError("boom tab")
        )
        with self.assertLogs(level="ERROR"):
            result = run(cog)
        self.assertEqual(
            result,
            (
                "The preset p ran into an error: something unexpected went wrong"
                " - details are in the bot logs.",
                None,
                None,
            ),
        )
        self.assertNotIn("boom tab", result[0])


class WheelAssemblyTests(unittest.TestCase):
    def test_two_tabs_build_two_wheels_two_gifs(self):
        cog = make_cog(
            preset_with([("drivers", ""), ("teams", "X")]),
            tab_options={
                "drivers": [_WheelOption("max", 1, None, None)],
                "teams": [_WheelOption("alpha", 1, None, None)],
            },
        )
        message, gifs, name, role = run_patched(cog)
        self.assertEqual(list(gifs), ["gif:max", "gif:alpha"])
        self.assertEqual(name, "wheel.gif")
        self.assertEqual(message, "SPIN! resp:max resp:alpha")
        self.assertEqual(len(FakeWheel.instances), 2)
        cog.generate_option_set.assert_any_call("drivers", "")
        cog.generate_option_set.assert_any_call("teams", "x")

    def test_non_string_filter_is_stringified_and_lowered(self):
        cog = make_cog(
            preset_with([("drivers", 25)]),
            tab_options={"drivers": [_WheelOption("max", 1, None, None)]},
        )
        run_patched(cog)
        cog.generate_option_set.assert_any_call("drivers", "25")

    def test_next_spin_chain_adds_followed_wheels(self):
        opt = chained_option("max", [("teams", "X")])
        cog = make_cog(
            preset_with([("drivers", "")]),
            tab_options={
                "drivers": [opt],
                "teams": [_WheelOption("alpha", 1, None, None)],
            },
        )
        message, gifs, name, role = run_patched(cog)
        self.assertEqual(gifs, ["gif:max", "gif:alpha"])
        self.assertIn("resp:alpha", message)
        # chained filters pass through verbatim (no lower() on that path)
        cog.generate_option_set.assert_any_call("teams", "X")

    def test_chain_stops_at_depth_cap(self):
        tabs = [f"t{n}" for n in range(12)]
        tab_options = {
            t: [chained_option(t, [(f"t{n + 1}", "")])]
            for n, t in enumerate(tabs)
        }
        cog = make_cog(preset_with([("t0", "")]), tab_options=tab_options)
        message, gifs, name, role = run_patched(cog)
        # first wheel + exactly 10 followed wheels; t11 never built
        self.assertEqual(len(gifs), 11)
        self.assertEqual(gifs[0], "gif:t0")
        self.assertEqual(gifs[-1], "gif:t10")

    def test_role_passed_through(self):
        cog = make_cog(
            preset_with([("drivers", "")]),
            tab_options={"drivers": [_WheelOption("max", 1, None, None)]},
        )
        role = object()
        _, _, _, got_role = run_patched(cog, role=role)
        self.assertIs(got_role, role)


if __name__ == "__main__":
    unittest.main()
