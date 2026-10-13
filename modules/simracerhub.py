# pyright: basic
"""SimRacerHub league-stats client for the ChatHandler agent.

SimRacerHub (simracerhub.com) publishes league/season standings and race
results as JSON from its scoring pages (no API key required — these are the
same endpoints the site's own React tables call):

    /scoring/search_suggest.php?q=<name>     -> driver/series id resolution
    /scoring/get_standings.php?season_id=N   -> full standings payload:
        rps        per-driver season aggregates (points, wins, inc, rating...)
        drivers    driver identity records
        schedules  race weeks (race_date epoch-seconds, per-race results)
        teams      team aggregates keyed by team id
        team_drivers        driver_id -> team_id (season-level team)
        race_team_assignments  race_participant_id -> team_id (per-race team)
        tracks     track metadata keyed by config_id
        lss        "last scored schedule" pointer

This module fetches that payload, normalises it into compact records, and
exposes two filter entry points (standings + recent results, driver-filtered
and time-filtered). Everything degrades to ``{"status": ..., ...}`` payloads —
network errors, malformed payloads, and "no results uploaded yet" never raise,
so a flaky lookup can't take down a chat reply.

Configuration is environment-driven:

    SIMRACERHUB_ENABLED              "0"/"false"/"off" disables the feature
    SIMRACERHUB_TIMEOUT_SECONDS      per-request HTTP timeout (default 8)
    SIMRACERHUB_MIN_INTERVAL_SECONDS min seconds between API calls (default 3)
    SIMRACERHUB_MAX_ROWS             max rows per result set (default 15)
    SIMRACERHUB_SEASON_ID            default season id (league default)
    SIMRACERHUB_SERIES_ID            default series id (league default)
"""
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from strands import tool as strands_tool

from .env_config import env_enabled, env_float, env_int, env_int_or_none
from .league_config import LeagueConfig

log = logging.getLogger(__name__)

SRH_BASE_URL = "https://www.simracerhub.com/scoring"
USER_AGENT = "SpinnyBoi-Discord-bot/1.0 (league stats)"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SimRacerHubConfig:
    enabled: bool = True
    timeout_seconds: float = 8.0
    min_interval_seconds: float = 3.0
    max_rows: int = 15
    default_season_id: Optional[int] = None
    default_series_id: Optional[int] = None

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "SimRacerHubConfig":
        env = os.environ if env is None else env
        enabled = env_enabled(env, "SIMRACERHUB_ENABLED")
        return cls(
            enabled=enabled,
            timeout_seconds=env_float(
                env, "SIMRACERHUB_TIMEOUT_SECONDS", 8.0, 1.0, 30.0
            ),
            min_interval_seconds=env_float(
                env, "SIMRACERHUB_MIN_INTERVAL_SECONDS", 3.0, 0.0, 60.0
            ),
            max_rows=env_int(env, "SIMRACERHUB_MAX_ROWS", 15, minimum=1, maximum=50),
            default_season_id=env_int_or_none(env, "SIMRACERHUB_SEASON_ID")
            or LeagueConfig.from_env(env).srh_season_id,
            default_series_id=env_int_or_none(env, "SIMRACERHUB_SERIES_ID"),
        )




# ---------------------------------------------------------------------------
# Query formatting helpers (pure)
# ---------------------------------------------------------------------------


def clean_driver_filter(raw: Optional[str]) -> str:
    """Normalise a driver name filter: collapse whitespace, cap length."""
    name = re.sub(r"\s+", " ", raw or "").strip()
    return name[:60]


def normalize_days(raw: Any, default: int, maximum: int = 3650) -> int:
    """Coerce a time-window filter (days, '7d', '') to a bounded int."""
    if raw is None or isinstance(raw, bool):
        return default
    if isinstance(raw, (int, float)):
        days = int(raw)
    else:
        match = re.search(r"-?\d+", str(raw))
        if not match:
            return default
        days = int(match.group(0))
    return max(0, min(maximum, days))


def driver_matches(name: str, needle: str) -> bool:
    """Case/whitespace-insensitive substring match against a driver name."""
    if not needle:
        return True
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return norm(needle) in norm(name) or norm(name) in norm(needle)


def _to_number(value: Any) -> Any:
    """SRH serialises most numerics as strings; coerce int/float cleanly."""
    if value is None:
        return None
    try:
        as_float = float(value)
    except (TypeError, ValueError):
        return value
    if as_float.is_integer():
        return int(as_float)
    return round(as_float, 4)


def _fmt_lap_time(micros: Any) -> Optional[int]:
    try:
        return int(int(micros) / 1_000_000) if micros not in (None, "", "0", 0) else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Payload parsing / normalisation (pure; unit-tested without network)
# ---------------------------------------------------------------------------


def normalize_standings(
    payload: Dict[str, Any],
    driver: str = "",
    max_rows: int = 15,
) -> List[Dict[str, Any]]:
    """Turn ``rps`` + ``drivers`` into compact standings rows sorted by position."""
    drivers_meta: Dict[str, Any] = payload.get("drivers") or {}
    by_driver, _ = _team_lookups(payload)
    rows: List[Dict[str, Any]] = []
    for rp in (payload.get("rps") or {}).values():
        if not isinstance(rp, dict):
            continue
        driver_id = str(rp.get("drid", ""))
        meta = drivers_meta.get(driver_id) or {}
        name = meta.get("name") or rp.get("name", "")
        team = by_driver.get(driver_id)
        if not (
            driver_matches(str(name), driver)
            or (team and driver_matches(str(team), driver))
        ):
            continue
        starts = _to_number(rp.get("starts")) or 0
        inc = _to_number(rp.get("inc")) or 0
        rows.append(
            {
                "driver_id": _to_number(driver_id),
                "name": name,
                "team": team,
                "is_ai": bool(meta.get("is_ai", False)),
                "points": _to_number(rp.get("tpts")),
                "starts": starts,
                "wins": _to_number(rp.get("wins")),
                "top5": _to_number(rp.get("t5")),
                "top10": _to_number(rp.get("t10")),
                "poles": _to_number(rp.get("poles")),
                "laps": _to_number(rp.get("laps")),
                "incidents": inc,
                "inc_per_race": round(inc / starts, 2) if starts else 0,
                "rating": _to_number(rp.get("rat")),
            }
        )
    rows.sort(key=lambda r: (r.get("points") is None, -(r.get("points") or 0)))
    for i, row in enumerate(rows, 1):
        row["pos"] = i
    return rows[: max(1, max_rows)]


def _sched_driver_entries(sched: Dict[str, Any]):
    """Yield ``(driver_id, entry)`` pairs from a schedule's ``drivers`` map.

    Live SRH nests results one level deeper, keyed by race id
    (``{race_id: {driver_id: entry}}``); the flat
    ``{driver_id: entry}`` shape also occurs. Accept both.
    """
    raw = sched.get("drivers") or {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        if "finish_pos" in value or "driver_id" in value:
            yield str(key), value
        else:
            for inner_id, inner in value.items():
                if isinstance(inner, dict):
                    yield str(inner_id), inner


def _team_lookups(payload: Dict[str, Any]):
    """``(by_driver_id, by_participant)`` maps of id -> team name.

    ``team_drivers`` gives the season-level team; ``race_team_assignments``
    the per-race one (drivers can swap teams mid-season, so results prefer
    the race-level mapping). Missing or dangling team ids map to no entry:
    team is optional per driver.
    """
    teams: Dict[str, Any] = payload.get("teams") or {}

    def name_of(team_id: Any) -> Optional[str]:
        team = teams.get(str(team_id))
        if isinstance(team, dict) and team.get("name"):
            return str(team["name"])
        return None

    by_driver = {}
    for driver_id, team_id in (payload.get("team_drivers") or {}).items():
        team_name = name_of(team_id)
        if team_name:
            by_driver[str(driver_id)] = team_name
    by_participant = {}
    for participant_id, team_id in (
        payload.get("race_team_assignments") or {}
    ).items():
        team_name = name_of(team_id)
        if team_name:
            by_participant[str(participant_id)] = team_name
    return by_driver, by_participant


def normalize_results(
    payload: Dict[str, Any],
    driver: str = "",
    days: Optional[int] = None,
    now_epoch: Optional[int] = None,
    max_rows: int = 15,
    max_weeks: int = 4,
) -> List[Dict[str, Any]]:
    """Turn ``schedules`` per-race results into compact finish records.

    ``days`` keeps races whose race_date is within the last N days (future
    schedules always excluded). At most the ``max_weeks`` most recent scored
    weeks are scanned to keep the payload compact.
    """
    drivers_meta: Dict[str, Any] = payload.get("drivers") or {}
    tracks: Dict[str, Any] = payload.get("tracks") or {}
    by_driver, by_participant = _team_lookups(payload)
    now = int(now_epoch if now_epoch is not None else time.time())
    cutoff = now - days * 86400 if days is not None else None

    scored: List[Dict[str, Any]] = []
    for sched in payload.get("schedules") or []:
        if not isinstance(sched, dict) or not sched.get("drivers"):
            continue
        race_date = _to_number(sched.get("race_date")) or 0
        if race_date and race_date > now:
            continue  # scheduled, not yet run
        if cutoff is not None and race_date and race_date < cutoff:
            continue
        scored.append(sched)
    scored.sort(key=lambda s: _to_number(s.get("race_date")) or 0)
    scored = scored[-max_weeks:]

    results: List[Dict[str, Any]] = []
    for sched in scored:
        track = tracks.get(str(sched.get("config_id"))) or {}
        track_name = track.get("track_name") or track.get("name") or ""
        race_date = _to_number(sched.get("race_date")) or 0
        for driver_id, entry in _sched_driver_entries(sched):
            meta = drivers_meta.get(driver_id) or {}
            name = meta.get("name", "")
            team = (
                by_participant.get(str(entry.get("race_participant_id", "")))
                or by_driver.get(driver_id)
            )
            if not (
                driver_matches(str(name), driver)
                or (team and driver_matches(str(team), driver))
            ):
                continue
            results.append(
                {
                    "driver_id": _to_number(driver_id),
                    "name": name,
                    "team": team,
                    "race_date": race_date,
                    "track": track_name,
                    "finish_pos": _to_number(entry.get("finish_pos")),
                    "start_pos": _to_number(entry.get("qualify_pos")),
                    "race_points": _to_number(entry.get("race_points")),
                    "incidents": _to_number(entry.get("incidents")),
                    "laps": _to_number(entry.get("num_laps")),
                    "laps_led": _to_number(entry.get("laps_led")),
                    "status": entry.get("status"),
                    "rating": _to_number(entry.get("rating")),
                }
            )
    results.sort(key=lambda r: (-(r.get("race_date") or 0), r.get("finish_pos") or 999))
    return results[: max(1, max_rows)]


def build_suggest_url(name: str) -> str:
    params = {"q": clean_driver_filter(name)}
    return f"{SRH_BASE_URL}/search_suggest.php?{urllib.parse.urlencode(params)}"


def build_standings_url(season_id: int, class_id: Optional[int] = None) -> str:
    params: Dict[str, Any] = {"season_id": season_id}
    if class_id:
        params["class_id"] = class_id
    return f"{SRH_BASE_URL}/get_standings.php?{urllib.parse.urlencode(params)}"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SimRacerHubClient:
    """Fetch + normalise SimRacerHub scoring data. Never raises out of
    :meth:`standings`, :meth:`results`, or :meth:`resolve_driver`."""

    def __init__(
        self,
        config: Optional[SimRacerHubConfig] = None,
        fetch_json: Optional[Callable[[str, float], Any]] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.config = config or SimRacerHubConfig.from_env()
        self._fetch_json = fetch_json or _default_fetch_json
        self._clock = clock or time.monotonic
        self._last_call: Optional[float] = None

    # -- rate limiting ------------------------------------------------------

    def _rate_limit_ok(self) -> bool:
        now = self._clock()
        if (
            self._last_call is not None
            and now - self._last_call < self.config.min_interval_seconds
        ):
            return False
        self._last_call = now
        return True

    def _get(self, url: str) -> Any:
        return self._fetch_json(url, self.config.timeout_seconds)

    # -- public API ----------------------------------------------------------

    def standings(
        self,
        season_id: Optional[int] = None,
        driver: Optional[str] = None,
        days: Optional[int] = None,  # accepted for filter symmetry; n/a here
    ) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"status": "disabled", "rows": []}
        season = season_id or self.config.default_season_id
        if not season:
            return {"status": "no_season", "rows": [], "error": "no season_id given and no SIMRACERHUB_SEASON_ID default"}
        if not self._rate_limit_ok():
            return {"status": "rate_limited", "rows": []}
        try:
            payload = self._get(build_standings_url(int(season)))
        except Exception:
            log.exception("simracerhub standings fetch failed (season=%s)", season)
            return {"status": "error", "rows": []}
        if not isinstance(payload, dict):
            return {"status": "error", "rows": []}
        if payload.get("error"):
            return {"status": "no_data", "rows": [], "error": str(payload["error"]).strip()}
        rows = normalize_standings(
            payload, driver=clean_driver_filter(driver or ""), max_rows=self.config.max_rows
        )
        if not rows:
            return {"status": "no_data", "rows": [], "season_id": int(season)}
        return {"status": "ok", "season_id": int(season), "rows": rows}

    def results(
        self,
        season_id: Optional[int] = None,
        driver: Optional[str] = None,
        days: Optional[int] = None,
    ) -> Dict[str, Any]:
        if not self.config.enabled:
            return {"status": "disabled", "rows": []}
        season = season_id or self.config.default_season_id
        if not season:
            return {"status": "no_season", "rows": [], "error": "no season_id given and no SIMRACERHUB_SEASON_ID default"}
        if not self._rate_limit_ok():
            return {"status": "rate_limited", "rows": []}
        try:
            payload = self._get(build_standings_url(int(season)))
        except Exception:
            log.exception("simracerhub results fetch failed (season=%s)", season)
            return {"status": "error", "rows": []}
        if not isinstance(payload, dict):
            return {"status": "error", "rows": []}
        if payload.get("error"):
            return {"status": "no_data", "rows": [], "error": str(payload["error"]).strip()}
        rows = normalize_results(
            payload,
            driver=clean_driver_filter(driver or ""),
            days=normalize_days(days, default=14),
        )
        if not rows:
            return {"status": "no_data", "rows": [], "season_id": int(season)}
        return {"status": "ok", "season_id": int(season), "rows": rows}

    def resolve_driver(self, name: str) -> Dict[str, Any]:
        """Resolve a driver name to SimRacerHub ids (helps the model re-query)."""
        if not self.config.enabled:
            return {"status": "disabled", "drivers": []}
        needle = clean_driver_filter(name)
        if not needle:
            return {"status": "empty_query", "drivers": []}
        if not self._rate_limit_ok():
            return {"status": "rate_limited", "drivers": []}
        try:
            payload = self._get(build_suggest_url(needle))
        except Exception:
            log.exception("simracerhub driver resolve failed (name=%r)", needle)
            return {"status": "error", "drivers": []}
        if not isinstance(payload, dict):
            return {"status": "error", "drivers": []}
        drivers = [
            {"driver_id": d.get("id"), "name": d.get("name")}
            for d in (payload.get("drivers") or [])[:5]
            if isinstance(d, dict)
        ]
        return {"status": "ok", "query": needle, "drivers": drivers}

    # -- strands tool ---------------------------------------------------------

    def stats_tool(self):
        standings = self.standings
        results = self.results

        @strands_tool
        def get_simracerhub_stats(
            kind: str,
            season_id: Optional[int] = None,
            driver: Optional[str] = None,
            days: Optional[int] = None,
        ) -> Dict[str, Any]:
            """Look up the league's racing results and driver stats on SimRacerHub.

            ``kind`` is "standings" (season points table: points, wins, top-5s,
            incidents, safety rating, team per driver) or "results" (recent
            individual race finishes: start/finish position, points, incidents,
            rating, team). ``driver`` optionally filters by driver name OR team
            name (substring, e.g. "Tucker" or "Snack Attack"). ``days`` limits
            "results" to the last N days
            (default 14). ``season_id`` selects a season when there are
            several; omit it to use the league default. Returns compact rows,
            or a status like "no_data" when nothing was uploaded yet.
            """
            kind_norm = (kind or "").strip().lower()
            if kind_norm in ("standings", "points", "table", "champ", "championship"):
                return standings(season_id=season_id, driver=driver)
            if kind_norm in ("results", "race", "races", "finishes", "history"):
                return results(season_id=season_id, driver=driver, days=days)
            return {
                "status": "bad_kind",
                "rows": [],
                "error": "kind must be 'standings' or 'results'",
            }

        return get_simracerhub_stats


# ---------------------------------------------------------------------------
# Default fetcher and module-level singleton used by ChatHandler wiring
# ---------------------------------------------------------------------------


def _default_fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


_simracerhub: Optional[SimRacerHubClient] = None


def get_simracerhub() -> Optional[SimRacerHubClient]:
    """Return the shared SimRacerHubClient, or None if the feature is disabled."""
    global _simracerhub
    if _simracerhub is None:
        config = SimRacerHubConfig.from_env()
        if not config.enabled:
            return None
        _simracerhub = SimRacerHubClient(config)
    return _simracerhub
