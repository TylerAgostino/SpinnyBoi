# pyright: basic
"""Scheduler: persists scheduled events and executes them when due.

One instance owns one SQLite database (path resolved at construction,
never at import — importing this module has no side effects). Executors
register handlers by function name; `run_due()` dispatches every due,
uncompleted event to its handler and marks it completed on success. A
handler that raises leaves its event pending so it retries on the next
tick, but only up to `Scheduler.MAX_ATTEMPTS` consecutive failures — the
event is then completed (give-up logged) instead of retrying forever. An
unregistered function is logged and completed so one bad row can't poison
the loop.

Cancel is distinct from completed: a canceled event is never executed
but stays distinguishable in the table (`cancelled = 1`, `completed = 0`).
"""
from __future__ import annotations

import datetime
import logging
import os
import sqlite3
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional

from modules.env_config import env_str

logger = logging.getLogger(__name__)

Handler = Callable[["ScheduledEvent"], Awaitable[None]]


@dataclass
class ScheduledEvent:
    """One event row: when to run, what to run, and the payload."""

    id: Optional[int]
    timestamp: float
    function_name: str
    message_id: int
    channel_id: int
    completed: bool = False
    data: Optional[str] = None  # JSON string, opaque to the scheduler
    cancelled: bool = False
    attempts: int = 0  # handler failures so far


class Scheduler:
    """Event store + executor over one SQLite file. Sync storage, async
    execution through registered handlers."""

    MAX_ATTEMPTS = 3  # consecutive handler failures before giving up

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = _db_path_from_env()
        self.db_path = db_path
        self._handlers: Dict[str, Handler] = {}
        self._ensure_schema()

    @classmethod
    def from_env(cls) -> "Scheduler":
        return cls()

    def register_handler(self, function_name: str, handler: Handler) -> None:
        """Bind an async handler to an event's function_name."""
        self._handlers[function_name] = handler

    # --- storage ---------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        return sqlite3.connect(self.db_path)

    def _ensure_schema(self) -> None:
        conn = None
        try:
            conn = self._connect()
            cursor = conn.cursor()
            cursor.execute(
                """
            CREATE TABLE IF NOT EXISTS scheduled_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                function_name TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                completed BOOLEAN NOT NULL DEFAULT 0,
                data TEXT,
                cancelled BOOLEAN NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0
            )
            """
            )
            cursor.execute(
                """
            CREATE INDEX IF NOT EXISTS idx_scheduled_events_timestamp
            ON scheduled_events (timestamp)
            """
            )
            # Pre-existing databases lack the newer columns.
            cursor.execute("PRAGMA table_info(scheduled_events)")
            columns = {row[1] for row in cursor.fetchall()}
            if "cancelled" not in columns:
                cursor.execute(
                    "ALTER TABLE scheduled_events "
                    "ADD COLUMN cancelled BOOLEAN NOT NULL DEFAULT 0"
                )
            if "attempts" not in columns:
                cursor.execute(
                    "ALTER TABLE scheduled_events "
                    "ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"
                )
            conn.commit()
            logger.info(f"Scheduler database ready: {self.db_path}")
        except Exception as e:
            logger.error(f"Error initializing database: {str(e)}")
            raise
        finally:
            if conn is not None:
                conn.close()

    def schedule(
        self,
        timestamp: float,
        function_name: str,
        message_id: int,
        channel_id: int,
        data: Optional[str] = None,
    ) -> int:
        """Schedule an event; returns its id."""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
            INSERT INTO scheduled_events (timestamp, function_name, message_id, channel_id, completed, data)
            VALUES (?, ?, ?, ?, 0, ?)
            """,
                (timestamp, function_name, message_id, channel_id, data),
            )
            event_id = cursor.lastrowid
            conn.commit()
        finally:
            conn.close()
        logger.info(
            f"Event scheduled: {function_name} on message {message_id} at {datetime.datetime.fromtimestamp(timestamp).isoformat()}"
        )
        return int(event_id)

    def _rows(self, where: str, params: tuple = ()) -> List[ScheduledEvent]:
        try:
            conn = self._connect()
        except sqlite3.Error as ex:
            # Same policy as the query failure below: loud in logs, never
            # crash the scheduler loop.
            logger.error(f"Scheduler database error: {ex}", exc_info=True)
            return []
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    f"""
                SELECT id, timestamp, function_name, message_id, channel_id, completed, data, cancelled, attempts
                FROM scheduled_events
                WHERE {where}
                ORDER BY timestamp ASC
                """,
                    params,
                )
            except sqlite3.Error as ex:
                # No database/table yet means nothing has been stored. A
                # lock or corruption must stay visible in the logs instead
                # of masquerading as "no pending events" - but it must not
                # crash the scheduler loop either.
                if "no such table" not in str(ex).lower():
                    logger.error(
                        f"Scheduler database error: {ex}", exc_info=True
                    )
                return []
            return [_to_event(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def pending(self, now: Optional[float] = None) -> List[ScheduledEvent]:
        """Due, not completed, not canceled, oldest first."""
        if now is None:
            now = datetime.datetime.now().timestamp()
        return self._rows(
            "timestamp <= ? AND completed = 0 AND cancelled = 0", (now,)
        )

    def all_active(self) -> List[ScheduledEvent]:
        """Everything not completed and not canceled, oldest first."""
        return self._rows("completed = 0 AND cancelled = 0")

    def mark_completed(self, event_id: int) -> bool:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE scheduled_events SET completed = 1 WHERE id = ?", (event_id,)
            )
            changed = cursor.rowcount > 0
            conn.commit()
        finally:
            conn.close()
        if not changed:
            logger.warning(f"Event {event_id} not found when marking completed")
        return changed

    def cancel(self, event_id: int) -> bool:
        """Cancel a pending event. Completed events cannot be canceled."""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE scheduled_events SET cancelled = 1 "
                "WHERE id = ? AND completed = 0 AND cancelled = 0",
                (event_id,),
            )
            changed = cursor.rowcount > 0
            conn.commit()
        finally:
            conn.close()
        if not changed:
            logger.warning(f"Event {event_id} not cancellable")
        return changed

    def _record_attempt(self, event_id: int, attempts: int) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE scheduled_events SET attempts = ? WHERE id = ?",
                (attempts, event_id),
            )
            conn.commit()
        finally:
            conn.close()

    # --- execution --------------------------------------------------------

    async def run_due(self) -> int:
        """Dispatch every due event to its registered handler.

        Returns the number executed. Success marks the event completed; a
        handler exception counts as one attempt and leaves the event
        pending until `MAX_ATTEMPTS` consecutive failures, after which it
        is completed (never retried again). An unregistered function is
        completed (a poison pill must not loop).
        """
        executed = 0
        for event in self.pending():
            handler = self._handlers.get(event.function_name)
            if handler is None:
                logger.error(
                    f"No handler for scheduled event {event.id} "
                    f"({event.function_name}); completing it"
                )
                self.mark_completed(event.id)  # type: ignore[arg-type]
                continue
            try:
                logger.info(
                    f"Processing scheduled event {event.id}: {event.function_name}"
                )
                await handler(event)
            except Exception as e:
                attempts = event.attempts + 1
                self._record_attempt(event.id, attempts)  # type: ignore[arg-type]
                if attempts >= self.MAX_ATTEMPTS:
                    logger.error(
                        f"Scheduled event {event.id} ({event.function_name}) "
                        f"failed {attempts} consecutive times; giving up: {e}"
                    )
                    self.mark_completed(event.id)  # type: ignore[arg-type]
                else:
                    logger.error(
                        f"Error executing scheduled event {event.id}: "
                        f"{str(e)} (attempt {attempts}/{self.MAX_ATTEMPTS})"
                    )
                continue
            self.mark_completed(event.id)  # type: ignore[arg-type]
            executed += 1
        return executed


def _to_event(row: sqlite3.Row) -> ScheduledEvent:
    return ScheduledEvent(
        id=row["id"],
        timestamp=row["timestamp"],
        function_name=row["function_name"],
        message_id=row["message_id"],
        channel_id=row["channel_id"],
        completed=bool(row["completed"]),
        data=row["data"],
        cancelled=bool(row["cancelled"]),
        attempts=row["attempts"],
    )


def _db_path_from_env() -> str:
    """Resolve the SQLite path from DATABASE_URL (sqlite:///path or path)."""
    url = env_str(os.environ, "DATABASE_URL", "sqlite:///data/spinny.db")
    if url.startswith("sqlite:///"):
        return url.replace("sqlite:///", "")
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "")
    return url
