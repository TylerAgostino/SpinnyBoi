import unittest
import sys
import os
import time
import tempfile
import json
from unittest.mock import patch, MagicMock

# Add the parent directory to sys.path so we can import the modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from modules.scheduler.scheduler import (
    init_db,
    schedule_event,
    get_pending_events,
    mark_event_completed,
    ScheduledEvent,
)


class TestScheduler(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for the test database
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test.db")

        # Set the database URL for testing
        self.original_db_path = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = f"sqlite:///{self.db_path}"

        # Initialize the database
        init_db()

    def tearDown(self):
        # Reset the database URL
        if self.original_db_path:
            os.environ["DATABASE_URL"] = self.original_db_path
        else:
            del os.environ["DATABASE_URL"]

        # Clean up the temporary directory
        self.temp_dir.cleanup()

    def test_schedule_and_get_event(self):
        # Schedule a test event
        now = time.time()
        event_id = schedule_event(
            timestamp=now - 10,  # 10 seconds in the past
            function_name="test_function",
            message_id=12345,
            channel_id=67890,
            data=json.dumps({"test": "data"}),
        )

        # Verify the event was scheduled
        self.assertIsNotNone(event_id)

        # Get pending events
        events = get_pending_events()

        # Verify we got our event back
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].id, event_id)
        self.assertEqual(events[0].function_name, "test_function")
        self.assertEqual(events[0].message_id, 12345)
        self.assertEqual(events[0].channel_id, 67890)
        self.assertEqual(events[0].data, json.dumps({"test": "data"}))
        self.assertFalse(events[0].completed)

    def test_mark_event_completed(self):
        # Schedule a test event
        now = time.time()
        event_id = schedule_event(
            timestamp=now - 10,
            function_name="test_function",
            message_id=12345,
            channel_id=67890,
        )

        # Mark it as completed
        result = mark_event_completed(event_id)

        # Verify it was marked as completed
        self.assertTrue(result)

        # Get pending events
        events = get_pending_events()

        # Verify our event is no longer pending
        self.assertEqual(len(events), 0)

    def test_future_events_not_returned(self):
        # Schedule a test event in the future
        now = time.time()
        event_id = schedule_event(
            timestamp=now + 3600,  # 1 hour in the future
            function_name="future_function",
            message_id=12345,
            channel_id=67890,
        )

        # Get pending events
        events = get_pending_events()

        # Verify our future event is not returned
        self.assertEqual(len(events), 0)


if __name__ == "__main__":
    unittest.main()
