import os
import sqlite3
import logging
import datetime
from dataclasses import dataclass
from typing import List, Optional, Callable, Any

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
logger = logging.getLogger(__name__)

# Get the database URL from environment variables
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/spinny.db")
# Handle both sqlite:/// and sqlite:// formats
if DATABASE_URL.startswith("sqlite:///"):
    DB_PATH = DATABASE_URL.replace("sqlite:///", "")
elif DATABASE_URL.startswith("sqlite://"):
    DB_PATH = DATABASE_URL.replace("sqlite://", "")
else:
    DB_PATH = DATABASE_URL
logger.info(f"Using database path: {DB_PATH}")


@dataclass
class ScheduledEvent:
    """Class representing a scheduled event in the database."""

    id: Optional[int]
    timestamp: float  # Unix timestamp when the event should execute
    function_name: str  # Name of the function to execute
    message_id: int  # Discord message ID to operate on
    channel_id: int  # Discord channel ID where the message is
    completed: bool  # Whether the event has been executed
    data: Optional[str]  # Additional data needed for the function (JSON string)


def init_db() -> None:
    """Initialize the database with the scheduled_events table."""
    try:
        # Extract the directory path
        db_dir = os.path.dirname(DB_PATH)

        # Create the directory if it doesn't exist
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"Created database directory: {db_dir}")

        # Connect to the database
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Create the scheduled_events table if it doesn't exist
        cursor.execute(
            """
        CREATE TABLE IF NOT EXISTS scheduled_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            function_name TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            channel_id INTEGER NOT NULL,
            completed BOOLEAN NOT NULL DEFAULT 0,
            data TEXT
        )
        """
        )

        # Create an index on the timestamp for efficient querying
        cursor.execute(
            """
        CREATE INDEX IF NOT EXISTS idx_scheduled_events_timestamp
        ON scheduled_events (timestamp)
        """
        )

        # Create an index on the completed status for efficient querying
        cursor.execute(
            """
        CREATE INDEX IF NOT EXISTS idx_scheduled_events_completed
        ON scheduled_events (completed)
        """
        )

        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {str(e)}")
        raise


def schedule_event(
    timestamp: float,
    function_name: str,
    message_id: int,
    channel_id: int,
    data: Optional[str] = None,
) -> int:
    """
    Schedule a new event to be executed at the specified timestamp.

    Args:
        timestamp: Unix timestamp when the event should execute
        function_name: Name of the function to execute
        message_id: Discord message ID to operate on
        channel_id: Discord channel ID where the message is
        data: Additional data needed for the function (JSON string)

    Returns:
        The ID of the newly created event
    """
    try:
        # Ensure database directory exists
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"Created database directory: {db_dir}")

        conn = sqlite3.connect(DB_PATH)
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
        conn.close()

        logger.info(
            f"Event scheduled: {function_name} on message {message_id} at {datetime.datetime.fromtimestamp(timestamp).isoformat()}"
        )
        return event_id
    except Exception as e:
        logger.error(f"Error scheduling event: {str(e)}")
        raise


def get_pending_events() -> List[ScheduledEvent]:
    """
    Get all events that are scheduled to execute in the past and haven't been completed yet.

    Returns:
        A list of ScheduledEvent objects
    """
    try:
        # Ensure database exists before querying
        if not os.path.exists(DB_PATH):
            logger.warning(f"Database file does not exist: {DB_PATH}")
            return []

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        current_timestamp = datetime.datetime.now().timestamp()
        cursor.execute(
            """
        SELECT id, timestamp, function_name, message_id, channel_id, completed, data
        FROM scheduled_events
        WHERE timestamp <= ? AND completed = 0
        ORDER BY timestamp ASC
        """,
            (current_timestamp,),
        )

        rows = cursor.fetchall()
        conn.close()

        events = [
            ScheduledEvent(
                id=row["id"],
                timestamp=row["timestamp"],
                function_name=row["function_name"],
                message_id=row["message_id"],
                channel_id=row["channel_id"],
                completed=bool(row["completed"]),
                data=row["data"],
            )
            for row in rows
        ]

        return events
    except Exception as e:
        logger.error(f"Error getting pending events: {str(e)}")
        return []


def mark_event_completed(event_id: int) -> bool:
    """
    Mark an event as completed.

    Args:
        event_id: The ID of the event to mark as completed

    Returns:
        True if the event was marked as completed, False otherwise
    """
    try:
        # Ensure database exists before updating
        if not os.path.exists(DB_PATH):
            logger.warning(f"Database file does not exist: {DB_PATH}")
            return False

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        cursor.execute(
            """
        UPDATE scheduled_events
        SET completed = 1
        WHERE id = ?
        """,
            (event_id,),
        )

        conn.commit()
        conn.close()

        logger.info(f"Event {event_id} marked as completed")
        return True
    except Exception as e:
        logger.error(f"Error marking event as completed: {str(e)}")
        return False


def get_all_scheduled_events() -> List[ScheduledEvent]:
    """
    Get all scheduled events that haven't been completed yet.

    Returns:
        A list of ScheduledEvent objects
    """
    try:
        # Ensure database exists before querying
        if not os.path.exists(DB_PATH):
            logger.warning(f"Database file does not exist: {DB_PATH}")
            return []

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute(
            """
        SELECT id, timestamp, function_name, message_id, channel_id, completed, data
        FROM scheduled_events
        WHERE completed = 0
        ORDER BY timestamp ASC
        """
        )

        rows = cursor.fetchall()
        conn.close()

        events = [
            ScheduledEvent(
                id=row["id"],
                timestamp=row["timestamp"],
                function_name=row["function_name"],
                message_id=row["message_id"],
                channel_id=row["channel_id"],
                completed=bool(row["completed"]),
                data=row["data"],
            )
            for row in rows
        ]

        return events
    except Exception as e:
        logger.error(f"Error getting scheduled events: {str(e)}")
        return []


def cancel_event(event_id: int) -> bool:
    """
    Cancel a scheduled event by marking it as completed.

    Args:
        event_id: The ID of the event to cancel

    Returns:
        True if the event was canceled successfully, False otherwise
    """
    try:
        # Ensure database exists before updating
        if not os.path.exists(DB_PATH):
            logger.warning(f"Database file does not exist: {DB_PATH}")
            return False

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        # Check if the event exists and is not completed
        cursor.execute(
            """
        SELECT id FROM scheduled_events
        WHERE id = ? AND completed = 0
        """,
            (event_id,),
        )

        if not cursor.fetchone():
            logger.warning(f"Event {event_id} not found or already completed")
            conn.close()
            return False

        # Mark the event as completed (effectively canceling it)
        cursor.execute(
            """
        UPDATE scheduled_events
        SET completed = 1
        WHERE id = ?
        """,
            (event_id,),
        )

        conn.commit()
        conn.close()

        logger.info(f"Event {event_id} canceled successfully")
        return True
    except Exception as e:
        logger.error(f"Error canceling event: {str(e)}")
        return False


# Initialize the scheduler database
init_db()
