# pyright: basic
"""AUDITOR_WEBHOOK_URL resolution: env override wins; default preserved."""

import importlib
import os
import unittest

import modules.wheelCog as wheelCog


def reload_with_env(value):
    old = os.environ.get("AUDITOR_WEBHOOK_URL")
    if value is None:
        os.environ.pop("AUDITOR_WEBHOOK_URL", None)
    else:
        os.environ["AUDITOR_WEBHOOK_URL"] = value
    try:
        importlib.reload(wheelCog)
        return wheelCog.AUDITOR_WEBHOOK_URL
    finally:
        if old is None:
            os.environ.pop("AUDITOR_WEBHOOK_URL", None)
        else:
            os.environ["AUDITOR_WEBHOOK_URL"] = old
        importlib.reload(wheelCog)


class AuditorWebhookUrlTests(unittest.TestCase):
    DEFAULT_URL = (
        "http://192.168.1.125:9996/api/stacks/webhooks/"
        "b1fb8123-6c54-439d-839f-11c2ad01a011?pullimage=true"
    )

    def test_default_endpoint_when_env_unset(self):
        self.assertEqual(reload_with_env(None), self.DEFAULT_URL)

    def test_env_override_wins(self):
        url = reload_with_env("http://example.invalid/hook")
        self.assertEqual(url, "http://example.invalid/hook")

    def test_empty_env_falls_back_to_default(self):
        self.assertEqual(reload_with_env(""), self.DEFAULT_URL)


if __name__ == "__main__":
    unittest.main()
