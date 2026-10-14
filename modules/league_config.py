# pyright: basic
"""LeagueConfig: one seam for "what season are we in, which ids matter".

Built once at wiring (spinnyBoi.py) from the environment and handed to every
cog that needs a season fact. Defaults are the current production values, so
an unconfigured deploy behaves exactly as before; season rollovers become a
one-place edit (or one env var) instead of a multi-file scavenger hunt.

Env knobs (empty string or unset = default):
    LEAGUE_SEASON               current season number (default 16)
    LEAGUE_STAFF_ROLE_ID        staff role id for command gates (decorator-level)
    LEAGUE_DRIVER_ROLE_ID       role granted on registration (default 1551759071533858838)
    LEAGUE_REGISTRATION_SHEET   registration sheet tab (default "SpinnyBoiRegistrations")
    LEAGUE_POINTS_SHEET         points/attendance sheet tab (default "Points Entry S15-17")
    LEAGUE_POINTS_ID_COLUMN     iRacing customer-id column header (default "iRacing ID")
    IRACING_LEAGUE_ID           iRacing league id (default 8579)
    SRH_SERIES_ID               SimRacerHub main series id (default 8812)
    SRH_AM_SERIES_ID            SimRacerHub AM series id (default 13428)
    SRH_SEASON_ID               SimRacerHub season id for league stats (default 28156)
    LEAGUE_DEBUG_GUILD_IDS      comma-separated slash-command guild ids
    LEAGUE_AUTO_DELETE_CHANNEL_ID  channel whose messages are deleted (default 1362287075142930442)
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Mapping, Optional, Tuple

DEFAULT_SEASON = 16
DEFAULT_STAFF_ROLE_ID = 1146907150229192747
DEFAULT_DRIVER_ROLE_ID = 1551759071533858838
DEFAULT_REGISTRATION_SHEET = "SpinnyBoiRegistrations"
DEFAULT_POINTS_SHEET = "Points Entry S15-17"
DEFAULT_POINTS_ID_COLUMN = "iRacing ID"
DEFAULT_IRACING_LEAGUE_ID = 8579
DEFAULT_SRH_SERIES_ID = 8812
DEFAULT_SRH_AM_SERIES_ID = 13428
DEFAULT_SRH_SEASON_ID = 28156
DEFAULT_DEBUG_GUILD_IDS = (420037391454044171, 981935710514839572)
DEFAULT_AUTO_DELETE_CHANNEL_ID = 1362287075142930442


def _env_str(name: str, default: str, env: Mapping[str, str]) -> str:
    """Return the env value, or the default when unset/empty."""
    value = env.get(name)
    if value is None or value.strip() == "":
        return default
    return value


def _env_int(name: str, default: int, env: Mapping[str, str]) -> int:
    """Return the env value as int, falling back to the default on garbage."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logging.warning(f"{name}={raw!r} is not an integer; using default {default}")
        return default


def _env_int_tuple(name: str, default: Tuple[int, ...], env: Mapping[str, str]) -> Tuple[int, ...]:
    """Return comma-separated ints, falling back to the default on garbage."""
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return tuple(int(part) for part in raw.split(",") if part.strip())
    except ValueError:
        logging.warning(f"{name}={raw!r} is not a comma-separated id list; using default")
        return default


# Discord.py evaluates command decorators at class-definition time, so the
# staff role gate cannot come from an instance. Read it here, once, so every
# cog gate shares one source.
STAFF_ROLE_ID = _env_int("LEAGUE_STAFF_ROLE_ID", DEFAULT_STAFF_ROLE_ID, os.environ)


@dataclass(frozen=True)
class LeagueConfig:
    """Season facts handed from wiring to every cog. Frozen: nobody mutates."""

    season: int = DEFAULT_SEASON
    driver_role_id: int = DEFAULT_DRIVER_ROLE_ID
    registration_sheet_name: str = DEFAULT_REGISTRATION_SHEET
    points_sheet_name: str = DEFAULT_POINTS_SHEET
    points_iracing_id_column: str = DEFAULT_POINTS_ID_COLUMN
    iracing_league_id: int = DEFAULT_IRACING_LEAGUE_ID
    srh_series_id: int = DEFAULT_SRH_SERIES_ID
    srh_am_series_id: int = DEFAULT_SRH_AM_SERIES_ID
    srh_season_id: int = DEFAULT_SRH_SEASON_ID
    debug_guild_ids: Tuple[int, ...] = field(default=DEFAULT_DEBUG_GUILD_IDS)
    auto_delete_channel_id: int = DEFAULT_AUTO_DELETE_CHANNEL_ID

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "LeagueConfig":
        env = os.environ if env is None else env
        return cls(
            season=_env_int("LEAGUE_SEASON", DEFAULT_SEASON, env),
            driver_role_id=_env_int(
                "LEAGUE_DRIVER_ROLE_ID", DEFAULT_DRIVER_ROLE_ID, env
            ),
            registration_sheet_name=_env_str(
                "LEAGUE_REGISTRATION_SHEET", DEFAULT_REGISTRATION_SHEET, env
            ),
            points_sheet_name=_env_str(
                "LEAGUE_POINTS_SHEET", DEFAULT_POINTS_SHEET, env
            ),
            points_iracing_id_column=_env_str(
                "LEAGUE_POINTS_ID_COLUMN", DEFAULT_POINTS_ID_COLUMN, env
            ),
            iracing_league_id=_env_int(
                "IRACING_LEAGUE_ID", DEFAULT_IRACING_LEAGUE_ID, env
            ),
            srh_series_id=_env_int("SRH_SERIES_ID", DEFAULT_SRH_SERIES_ID, env),
            srh_am_series_id=_env_int(
                "SRH_AM_SERIES_ID", DEFAULT_SRH_AM_SERIES_ID, env
            ),
            srh_season_id=_env_int("SRH_SEASON_ID", DEFAULT_SRH_SEASON_ID, env),
            debug_guild_ids=_env_int_tuple(
                "LEAGUE_DEBUG_GUILD_IDS", DEFAULT_DEBUG_GUILD_IDS, env
            ),
            auto_delete_channel_id=_env_int(
                "LEAGUE_AUTO_DELETE_CHANNEL_ID", DEFAULT_AUTO_DELETE_CHANNEL_ID, env
            ),
        )
