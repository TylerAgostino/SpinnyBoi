# pyright: basic
"""Scheduler seam tests: every behavior through a Scheduler instance built
on a temp database — nothing here touches data/spinny.db."""
import asyncio
import logging
import os
import sqlite3
import tempfile
import time
import unittest
from typing import List
from unittest import mock

from modules.scheduler.scheduler import ScheduledEvent, Scheduler


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "sub", "test.db")
        self.scheduler = Scheduler(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_schedule_and_pending_round_trip(self):
        now = time.time()
        event_id = self.scheduler.schedule(
            now - 5, "spin_preset", 111, 222, data='{"preset_name": "p"}'
        )
        events = self.scheduler.pending()
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.id, event_id)
        self.assertEqual(event.function_name, "spin_preset")
        self.assertEqual(event.message_id, 111)
        self.assertEqual(event.channel_id, 222)
        self.assertEqual(event.data, '{"preset_name": "p"}')
        self.assertFalse(event.completed)
        self.assertFalse(event.cancelled)

    def test_future_events_not_pending_but_active(self):
        now = time.time()
        self.scheduler.schedule(now + 3600, "spin_preset", 1, 2)
        self.assertEqual(self.scheduler.pending(now=now), [])
        self.assertEqual(len(self.scheduler.all_active()), 1)

    def test_pending_order_is_oldest_first(self):
        now = time.time()
        self.scheduler.schedule(now - 1, "f", 1, 1)
        self.scheduler.schedule(now - 99, "f", 2, 1)
        ids = [e.message_id for e in self.scheduler.pending(now=now)]
        self.assertEqual(ids, [2, 1])

    def test_mark_completed_excludes_from_pending_and_active(self):
        event_id = self.scheduler.schedule(time.time() - 1, "f", 1, 2)
        self.assertTrue(self.scheduler.mark_completed(event_id))
        self.assertEqual(self.scheduler.pending(), [])
        self.assertEqual(self.scheduler.all_active(), [])
        self.assertFalse(self.scheduler.mark_completed(9999))

    def test_cancel_is_not_completed_but_never_runs(self):
        event_id = self.scheduler.schedule(time.time() - 1, "f", 1, 2)
        self.assertTrue(self.scheduler.cancel(event_id))
        self.assertEqual(self.scheduler.pending(), [])
        self.assertEqual(self.scheduler.all_active(), [])
        # The row survives, distinguishable from an executed event.
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT completed, cancelled FROM scheduled_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        conn.close()
        self.assertEqual(row, (0, 1))

    def test_completed_events_cannot_be_canceled(self):
        event_id = self.scheduler.schedule(time.time() - 1, "f", 1, 2)
        self.scheduler.mark_completed(event_id)
        self.assertFalse(self.scheduler.cancel(event_id))

    # --- execution --------------------------------------------------------

    def _run(self, coro):
        return asyncio.run(coro)

    def test_run_due_dispatches_and_marks_completed(self):
        seen: List[ScheduledEvent] = []

        async def handler(event):
            seen.append(event)

        self.scheduler.register_handler("spin_preset", handler)
        now = time.time()
        self.scheduler.schedule(now - 2, "spin_preset", 7, 8)
        self.scheduler.schedule(now + 300, "spin_preset", 9, 8)  # future: untouched

        executed = self._run(self.scheduler.run_due())
        self.assertEqual(executed, 1)
        self.assertEqual([e.message_id for e in seen], [7])
        self.assertEqual(self.scheduler.pending(now=now), [])
        self.assertEqual(len(self.scheduler.all_active()), 1)  # future one left

    def test_handler_exception_leaves_event_pending(self):
        async def boom(event):
            raise RuntimeError("driver died")

        self.scheduler.register_handler("f", boom)
        self.scheduler.schedule(time.time() - 1, "f", 1, 2)
        executed = self._run(self.scheduler.run_due())
        self.assertEqual(executed, 0)
        self.assertEqual(len(self.scheduler.pending()), 1)  # retried next tick


    def _attempts_of(self, event_id: int) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT attempts FROM scheduled_events WHERE id = ?", (event_id,)
            ).fetchone()
        finally:
            conn.close()
        return int(row[0])

    def test_failures_stop_at_max_attempts_then_give_up(self):
        calls: List[int] = []

        async def boom(event):
            calls.append(event.id)
            raise RuntimeError("preset gone")

        self.scheduler.register_handler("f", boom)
        event_id = self.scheduler.schedule(time.time() - 1, "f", 1, 2)
        for expected_attempts in (1, 2):
            self._run(self.scheduler.run_due())
            self.assertEqual(len(self.scheduler.pending()), 1)
            self.assertEqual(self._attempts_of(event_id), expected_attempts)
        with self.assertLogs(level=logging.ERROR) as logs:
            self._run(self.scheduler.run_due())  # 3rd consecutive failure
        self.assertIn("giving up", "\n".join(logs.output))
        self.assertEqual(self.scheduler.pending(), [])  # never retried again
        self.assertEqual(len(calls), Scheduler.MAX_ATTEMPTS)
        self.assertEqual(self._attempts_of(event_id), Scheduler.MAX_ATTEMPTS)

    def test_old_schema_db_gains_attempts_column(self):
        old_db = os.path.join(self.temp_dir.name, "old.db")
        conn = sqlite3.connect(old_db)
        conn.execute(
            "CREATE TABLE scheduled_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp REAL NOT NULL, function_name TEXT NOT NULL, "
            "message_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
            "completed BOOLEAN NOT NULL DEFAULT 0, data TEXT)"
        )
        conn.execute(
            "INSERT INTO scheduled_events "
            "(timestamp, function_name, message_id, channel_id) "
            "VALUES (0.0, 'spin_preset', 1, 2)"
        )
        conn.commit()
        conn.close()

        migrated = Scheduler(old_db)

        events = migrated.pending()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].attempts, 0)


    def test_unregistered_function_is_completed_not_looped(self):
        self.scheduler.schedule(time.time() - 1, "nobody_home", 1, 2)
        executed = self._run(self.scheduler.run_due())
        self.assertEqual(executed, 0)
        self.assertEqual(self.scheduler.pending(), [])

    def test_canceled_event_is_never_dispatched(self):
        called = []

        async def handler(event):
            called.append(event.id)

        self.scheduler.register_handler("f", handler)
        event_id = self.scheduler.schedule(time.time() - 1, "f", 1, 2)
        self.scheduler.cancel(event_id)
        self._run(self.scheduler.run_due())
        self.assertEqual(called, [])

    def test_two_schedulers_two_databases(self):
        "No module-global state: a second instance cannot see the first's rows."
        other = Scheduler(os.path.join(self.temp_dir.name, "other.db"))
        self.scheduler.schedule(time.time() - 1, "f", 1, 2)
        self.assertEqual(len(self.scheduler.all_active()), 1)
        self.assertEqual(other.all_active(), [])


class DbErrorHandlingTests(unittest.TestCase):
    """Issue #132: a locked/corrupt database must be loud in logs, never
    crash the scheduler loop, and never leak its connection."""

    class FailingConn:
        def __init__(self, error):
            self._error = error
            self.closed = False

        @property
        def row_factory(self):
            return None

        @row_factory.setter
        def row_factory(self, value):
            pass

        def cursor(self):
            error = self._error

            class FailingCursor:
                def execute(self, *args):
                    raise error

                def fetchall(self):
                    return []

            return FailingCursor()

        def commit(self):
            pass

        def close(self):
            self.closed = True

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.scheduler = Scheduler(os.path.join(self.temp_dir.name, "sched.db"))
        self.addCleanup(self.temp_dir.cleanup)

    def test_missing_table_reads_empty_without_logging(self):
        conn = sqlite3.connect(self.scheduler.db_path)
        conn.execute("DROP TABLE scheduled_events")
        conn.commit()
        conn.close()
        with self.assertNoLogs(level=logging.ERROR):
            self.assertEqual(self.scheduler.pending(), [])

    def test_database_lock_reads_empty_but_logs_error(self):
        locked = sqlite3.OperationalError("database is locked")
        fake = self.FailingConn(locked)
        with mock.patch.object(self.scheduler, "_connect", return_value=fake):
            with self.assertLogs(level=logging.ERROR) as captured:
                self.assertEqual(self.scheduler.pending(), [])
        self.assertTrue(fake.closed)
        self.assertIn("database is locked", "\n".join(captured.output))

    def test_write_error_propagates_but_closes_connection(self):
        locked = sqlite3.OperationalError("database is locked")
        fake = self.FailingConn(locked)
        with mock.patch.object(self.scheduler, "_connect", return_value=fake):
            with self.assertRaises(sqlite3.OperationalError):
                self.scheduler.mark_completed(1)
        self.assertTrue(fake.closed)

    def test_connect_failure_reads_empty_but_logs_error(self):
        def refuse_connect():
            raise sqlite3.OperationalError("unable to open database file")

        with mock.patch.object(self.scheduler, "_connect", refuse_connect):
            with self.assertLogs(level=logging.ERROR) as captured:
                self.assertEqual(self.scheduler.pending(), [])
        self.assertIn("unable to open database file", "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
