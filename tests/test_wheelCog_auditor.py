import asyncio
import unittest
from unittest import mock

from modules import wheelCog


def fake_response(status_code):
    return mock.Mock(status_code=status_code)


class SpinAuditorTests(unittest.TestCase):
    def run_auditor(self, fake_post):
        ctx = mock.Mock()
        ctx.defer = mock.AsyncMock()
        bot_response = mock.AsyncMock()
        ctx.respond = mock.AsyncMock(return_value=bot_response)
        with (
            mock.patch("modules.wheelCog.requests.post", side_effect=fake_post),
            mock.patch(
                "modules.ChatHandler.working_on_it", mock.AsyncMock(return_value="")
            ),
        ):
            asyncio.run(wheelCog.WheelCog.spin_auditor(None, ctx))
        return bot_response.edit.call_args.kwargs["content"]

    def test_success_wording_on_200(self):
        message = self.run_auditor(lambda *a, **k: fake_response(200))
        self.assertIn("sent a request to restart the auditor", message)

    def test_timeout_bounded_on_post(self):
        captured = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs)
            return fake_response(200)

        self.run_auditor(fake_post)
        connect, read = captured["timeout"]
        self.assertGreater(connect, 0)
        self.assertGreater(read, 0)

    def test_failure_wording_on_non_2xx(self):
        message = self.run_auditor(lambda *a, **k: fake_response(500))
        self.assertIn("500", message)
        self.assertNotIn("It should post shortly", message)

    def test_failure_wording_on_connection_error(self):
        def boom(*args, **kwargs):
            raise wheelCog.requests.ConnectionError("stalled")

        message = self.run_auditor(boom)
        self.assertIn("couldn't reach", message)


if __name__ == "__main__":
    unittest.main()
