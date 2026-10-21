# pyright: basic
"""StandingsCog browser capture: driver setup args, _capture_table_png flow, send-side failures."""

import asyncio
import io
import unittest
from unittest import mock

from PIL import Image

import modules.standingsCog as standingsModule
from modules.standingsCog import StandingsCog


def png_bytes():
    buf = io.BytesIO()
    Image.new("RGB", (4, 3), "red").save(buf, format="PNG")
    return buf.getvalue()


def make_cog():
    return StandingsCog(bot=mock.MagicMock(), config=mock.MagicMock())


class SetUpDriverTests(unittest.TestCase):
    def test_headless_viewport_options_passed_to_firefox(self):
        options_cls = mock.MagicMock()
        firefox = mock.MagicMock()
        with (
            mock.patch.object(standingsModule, "Options", options_cls),
            mock.patch.object(standingsModule.webdriver, "Firefox", firefox),
        ):
            cog = make_cog()
            driver = cog._setup_driver()

        self.assertIs(driver, firefox.return_value)
        texts = [
            c.args[0]
            for c in options_cls.return_value.add_argument.call_args_list
        ]
        self.assertIn("--headless", texts)
        self.assertIn("--width=1920", texts)
        self.assertIn("--height=4000", texts)
        self.assertEqual(
            firefox.call_args.kwargs["options"], options_cls.return_value
        )


class CaptureTablePngTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(mock.patch.stopall)
        self.driver = mock.MagicMock()
        self.element = mock.MagicMock()
        self.element.screenshot_as_png = png_bytes()
        self.wait = mock.MagicMock()
        self.sleep = mock.patch.object(standingsModule.time, "sleep").start()
        self.wait_cls = mock.patch.object(
            standingsModule, "WebDriverWait", mock.MagicMock(return_value=self.wait)
        ).start()
        self.presence = mock.patch.object(
            standingsModule.EC, "presence_of_element_located"
        ).start()
        self.presence.return_value = "presence-cond"
        self.clickable = mock.patch.object(
            standingsModule.EC, "element_to_be_clickable"
        ).start()
        self.clickable.return_value = "clickable-cond"
        self.cog = make_cog()
        self.cog._setup_driver = mock.MagicMock(return_value=self.driver)
        self.wait.until.side_effect = lambda fn: self.element

    def capture(self, use_class=False, tab_to_click=""):
        return self.cog._capture_table_png(
            "http://example.test/table",
            "myTable",
            use_class,
            tab_to_click,
            "Driver Standings",
        )

    def test_id_selector_flow_returns_png_and_quits(self):
        result = self.capture()
        # polled twice even without a tab: initial presence + re-locate
        self.assertEqual(
            [c.args[0] for c in self.presence.call_args_list],
            [("id", "myTable")] * 2,
        )
        self.assertEqual(
            [c.args[0] for c in self.wait.until.call_args_list],
            ["presence-cond"] * 2,
        )
        self.wait_cls.assert_called_once_with(self.driver, 15)
        self.driver.get.assert_called_once_with("http://example.test/table")
        scroll = self.driver.execute_script.call_args
        self.assertIn("scrollIntoView", scroll.args[0])
        self.assertIs(scroll.args[1], self.element)
        # the PIL open/re-save actually ran: real image, real size
        with Image.open(result) as img:
            self.assertEqual(img.size, (4, 3))
        self.driver.quit.assert_called_once()

    def test_garbage_screenshot_fails_after_quit(self):
        self.element.screenshot_as_png = b"not a png at all"
        with self.assertRaises(Exception):
            self.capture()
        self.driver.quit.assert_called_once()

    def test_class_selector_used_when_flagged(self):
        self.capture(use_class=True)
        self.assertEqual(
            [c.args[0] for c in self.presence.call_args_list],
            [("class name", "myTable")] * 2,
        )

    def test_tab_clicked_before_reshot(self):
        self.capture(tab_to_click="Sprint")
        xpath = self.clickable.call_args.args[0]
        self.assertEqual(xpath[0], "xpath")
        self.assertIn("normalize-space(text())='Sprint'", xpath[1])
        self.assertEqual(
            [c.args[0] for c in self.wait.until.call_args_list],
            ["presence-cond", "clickable-cond", "presence-cond"],
        )
        scripts = [
            c.args[0]
            for c in self.driver.execute_script.call_args_list
        ]
        self.assertEqual(
            scripts,
            ["arguments[0].click();", "arguments[0].scrollIntoView(true);"],
        )

    def test_quit_runs_even_when_navigation_fails(self):
        self.driver.get.side_effect = RuntimeError("net died")
        with self.assertRaisesRegex(RuntimeError, "net died"):
            self.capture()
        self.driver.quit.assert_called_once()

    def test_setup_failure_never_reaches_quit(self):
        self.cog._setup_driver = mock.MagicMock(
            side_effect=RuntimeError("no browser")
        )
        with self.assertRaisesRegex(RuntimeError, "no browser"):
            self.capture()
        self.driver.quit.assert_not_called()


class SendSideTests(unittest.TestCase):
    def run_send(self, fetch_result, capture=None, additional=""):
        cog = make_cog()
        if capture is not None:
            cog._capture_table_png = capture
        else:
            cog._capture_table_png = mock.MagicMock(return_value=io.BytesIO(b"img"))
        bot = mock.AsyncMock()
        if isinstance(fetch_result, Exception):
            bot.fetch_channel = mock.AsyncMock(side_effect=fetch_result)
        else:
            bot.fetch_channel = mock.AsyncMock(return_value=fetch_result)
        cog.bot = bot
        ok = asyncio.run(
            cog._capture_and_send_table(
                "http://example.test",
                "id",
                123,
                "Race Report",
                additional_description=additional,
            )
        )
        return ok

    def test_missing_channel_returns_false_with_error_log(self):
        with self.assertLogs(level="ERROR") as captured:
            ok = self.run_send(None)
        self.assertFalse(ok)
        self.assertIn("123", "\n".join(captured.output))

    def test_fetch_channel_exception_swallowed_to_false(self):
        with self.assertLogs(level="ERROR") as captured:
            ok = self.run_send(RuntimeError("discord down"))
        self.assertFalse(ok)
        self.assertIn("discord down", "\n".join(captured.output))

    def test_capture_failure_never_fetches_or_sends(self):
        boom = mock.MagicMock(side_effect=RuntimeError("browser exploded"))
        with self.assertLogs(level="ERROR") as captured:
            ok = self.run_send(None, capture=boom)
        self.assertFalse(ok)
        self.assertIn("browser exploded", "\n".join(captured.output))

    def test_success_sends_named_png_with_descriptions(self):
        channel = mock.AsyncMock()
        ok = self.run_send(channel, additional="season 7 finale")
        self.assertTrue(ok)
        text = channel.send.call_args.args[0]
        self.assertEqual(text, "**Race Report**\nseason 7 finale")
        sent_file = channel.send.call_args.kwargs["file"]
        self.assertEqual(sent_file.filename, "Race_Report.png")


if __name__ == "__main__":
    unittest.main()
