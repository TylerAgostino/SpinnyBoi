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
    /scoring/league_series.php?league_id=N   -> HTML: the league's series
        (row id 'ser_<id>', Active Yes/No cell, link to series_seasons.php)
    /scoring/series_seasons.php?series_id=N  -> HTML: embedded JS
        `curr_season_id=<id>;seasons=[{id,sname,scrt,scrt_str,...}, ...]`
        — every historical season of the series plus the active-season pointer.

Only a league id is ever configured: season discovery walks league_series ->
series_seasons for the league's series and assembles a catalog of every
season ever run, flagging the active one(s). A season rollover on SRH needs
no config change; standings/results without an explicit season read the
active season, and "Season 14"/"S14"/season-id selects a historical one.

This module fetches those payloads, normalises them into compact records, and
exposes the filter entry points (standings + recent results, driver-filtered
and time-filtered) plus season discovery. Everything degrades to
``{"status": ..., ...}`` payloads —
network errors, malformed payloads, and "no results uploaded yet" never raise,
so a flaky lookup can't take down a chat reply.

SimRacerHub aggressively rate-limits non-browser traffic, so every request
goes through one polite path: repeat lookups reuse a short-lived payload
cache, real fetches wait for their slot instead of being dropped, and
rate-limit-shaped failures (429/5xx, transient I/O, non-JSON bodies) are
retried with backoff, honouring ``Retry-After``.


Configuration is environment-driven:

    SIMRACERHUB_ENABLED              "0"/"false"/"off" disables the feature
    SIMRACERHUB_TIMEOUT_SECONDS      per-request HTTP timeout (default 8)
    SIMRACERHUB_MIN_INTERVAL_SECONDS min seconds between API calls (default 3)
    SIMRACERHUB_MAX_ROWS             max rows per result set (default 15)
    SIMRACERHUB_CACHE_TTL_SECONDS    reuse fetched payloads this long (default 300; 0 disables)
    SIMRACERHUB_MAX_RETRIES          retries per fetch on rate-limit-shaped failures (default 2)
    SIMRACERHUB_RETRY_BASE_SECONDS   first backoff step; doubles per retry (default 2)
    SIMRACERHUB_LEAGUE_ID            SRH league id; sole id needed (league default).
                                     Active/historical seasons are inferred from it.
"""
import html as html_lib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from strands import tool as strands_tool

from .env_config import env_enabled, env_float, env_int, env_int_or_none
from .league_config import LeagueConfig

log = logging.getLogger(__name__)

SRH_BASE_URL = "https://www.simracerhub.com/scoring"
USER_AGENT = "SpinnyBoi-Discord-bot/1.0 (league stats)"

# SRH throttles automated traffic with these statuses (and sometimes an
# HTML body under 200); they're worth a backed-off retry.
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_WAIT_SECONDS = 60.0


class SRHRetryableFetchError(Exception):
    """Fetch failed in a way a retry may fix (e.g. rate-limit HTML body)."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class SimRacerHubConfig:
    enabled: bool = True
    timeout_seconds: float = 8.0
    min_interval_seconds: float = 3.0
    max_rows: int = 15
    league_id: Optional[int] = None
    max_retries: int = 2
    retry_base_seconds: float = 2.0
    cache_ttl_seconds: float = 300.0

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
            league_id=env_int_or_none(env, "SIMRACERHUB_LEAGUE_ID")
            or env_int_or_none(env, "SRH_LEAGUE_ID")
            or LeagueConfig.from_env(env).srh_league_id,
            max_retries=env_int(env, "SIMRACERHUB_MAX_RETRIES", 2, minimum=0, maximum=5),
            retry_base_seconds=env_float(
                env, "SIMRACERHUB_RETRY_BASE_SECONDS", 2.0, 0.1, 30.0
            ),
            cache_ttl_seconds=env_float(
                env, "SIMRACERHUB_CACHE_TTL_SECONDS", 300.0, 0.0, 3600.0
            ),
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
# Season discovery: URLs + HTML parsing (pure — season ids are never configured)
# ---------------------------------------------------------------------------


def build_series_index_url(league_id: int) -> str:
    params = {"league_id": league_id}
    return f"{SRH_BASE_URL}/league_series.php?{urllib.parse.urlencode(params)}"


def build_seasons_index_url(series_id: int) -> str:
    params = {"series_id": series_id}
    return f"{SRH_BASE_URL}/series_seasons.php?{urllib.parse.urlencode(params)}"


_SERIES_ROW_RE = re.compile(r"<tr[^>]*id=['\"]ser_(\d+)['\"][^>]*>(.*?)</tr>", re.I | re.S)
_SERIES_LINK_RE = re.compile(r"series_seasons\.php\?series_id=(\d+)[^>]*>\s*([^<]*)", re.I)
_SERIES_ACTIVE_RE = re.compile(r">\s*<b>\s*yes\s*</b>\s*<", re.I)
_CURRENT_SEASON_RE = re.compile(r"curr_season_id\s*=\s*(\d+)")
_SEASON_ENTRY_RE = re.compile(r'\{\s*id:\s*(\d+)\s*,\s*sname:\s*"((?:[^"\\]|\\.)*)"', re.S)
_SEASON_CREATED_RE = re.compile(r"\bscrt:\s*(\d+)")
_SEASON_NUMBER_RE = re.compile(r"^\s*(?:season|s)\s*(\d+)\s*$", re.I)
MAX_SEASON_OPTIONS = 40


def parse_series_index(html: str) -> List[Dict[str, Any]]:
    """The series rows of ``league_series.php``: id, name, Active flag."""
    out: List[Dict[str, Any]] = []
    for sid, row in _SERIES_ROW_RE.findall(html or ""):
        link = _SERIES_LINK_RE.search(row)
        if link is None or int(link.group(1)) != int(sid):
            continue
        out.append(
            {
                "series_id": int(sid),
                "series_name": html_lib.unescape(link.group(2)).strip() or f"Series {sid}",
                "active": bool(_SERIES_ACTIVE_RE.search(row)),
            }
        )
    return out


def parse_seasons_page(html: str) -> Dict[str, Any]:
    """``{current_season_id, seasons}`` from ``series_seasons.php`` HTML.

    The data is embedded as JS literals (unquoted keys): ``curr_season_id=``
    marks the active season; the ``seasons=[{id,sname,scrt,...}, ...]`` array
    holds every season the series has ever run.
    """
    text = html or ""
    current = _CURRENT_SEASON_RE.search(text)
    seasons: List[Dict[str, Any]] = []
    for match in _SEASON_ENTRY_RE.finditer(text):
        try:
            name = json.loads(f'"{match.group(2)}"')
        except json.JSONDecodeError:
            name = match.group(2)
        created = _SEASON_CREATED_RE.search(text, match.end(), match.end() + 240)
        seasons.append(
            {
                "season_id": int(match.group(1)),
                "season_name": html_lib.unescape(name),
                "created": int(created.group(1)) if created else None,
            }
        )
    return {
        "current_season_id": int(current.group(1)) if current else None,
        "seasons": seasons,
    }


def season_number(entry: Dict[str, Any]) -> Optional[int]:
    """The trailing number of a season name ("Season 16" / "S16" -> 16)."""
    match = _SEASON_NUMBER_RE.match(str(entry.get("season_name") or ""))
    return int(match.group(1)) if match else None


def assemble_season_catalog(
    league_id: int,
    series_pages: List[Tuple[Dict[str, Any], Dict[str, Any]]],
) -> Dict[str, Any]:
    """Merge per-series season lists into one league-wide season catalog.

    A season is "active" when it is its series' ``current_season_id``.
    ``active_season_id`` is set ONLY when every active-series current season
    agrees on one id; leagues with several active series running different
    seasons (main + AM) get ``None`` — an unqualified season request then
    answers "ambiguous_season" with the per-series current seasons as
    options instead of silently guessing one.
    """
    entries: List[Dict[str, Any]] = []
    series_rows: List[Dict[str, Any]] = []
    for meta, parsed in series_pages:
        current = parsed.get("current_season_id")
        series_active = bool(meta.get("active", True))
        series_rows.append(
            {
                "series_id": meta["series_id"],
                "series_name": meta.get("series_name"),
                "active": series_active,
                "current_season_id": current,
            }
        )
        for season in parsed.get("seasons") or []:
            entries.append(
                {
                    **season,
                    "series_id": meta["series_id"],
                    "series_name": meta.get("series_name"),
                    "active": season.get("season_id") == current,
                    "series_active": series_active,
                }
            )
    entries.sort(key=lambda s: s.get("created") or 0, reverse=True)
    currents = [s for s in entries if s["active"] and s["series_active"]]
    current_ids = {s["season_id"] for s in currents}
    return {
        "league_id": league_id,
        "series": series_rows,
        "active_season_id": currents[0]["season_id"] if len(current_ids) == 1 else None,
        "seasons": entries,
    }


_ACTIVE_SEASON_ALIASES = frozenset(
    {"", "active", "current", "latest", "newest", "now", "this", "this season"}
)


def season_option(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Compact season descriptor for tool payloads (ids select historical ones)."""
    return {
        "season_id": entry.get("season_id"),
        "season_name": entry.get("season_name"),
        "series_name": entry.get("series_name"),
        "active": bool(entry.get("active")),
    }


def match_season(
    ref: Optional[Union[str, int]], catalog: Dict[str, Any]
) -> Tuple[Optional[Dict[str, Any]], Optional[Tuple[str, List[Dict[str, Any]]]]]:
    """Resolve a season reference against a catalog from `assemble_season_catalog`.

    ``None``/alias ("active"/"current"/"latest") picks the active season; a
    number matches a season id first, else a season number ("16"); "s16"
    matches the season number; other text matches season names exact-first,
    then substring. Returns ``(entry, None)`` or ``(None, (status, options))``
    with statuses "unknown_season"/"ambiguous_season" and selectable options.
    """
    seasons = catalog.get("seasons") or []
    if ref is None or (
        isinstance(ref, str) and ref.strip().casefold() in _ACTIVE_SEASON_ALIASES
    ):
        active_id = catalog.get("active_season_id")
        entry = next((s for s in seasons if s.get("season_id") == active_id), None)
        if entry is not None:
            return entry, None
        if (
            len(
                {s.get("season_id") for s in seasons if s.get("active") and s.get("series_active")}
            )
            > 1
        ):
            return None, (
                "ambiguous_season",
                [
                    season_option(s)
                    for s in seasons
                    if s.get("active") and s.get("series_active")
                ][:MAX_SEASON_OPTIONS],
            )
        options = [season_option(s) for s in seasons[:MAX_SEASON_OPTIONS]]
        return None, ("unknown_season", options)
    needle = str(ref).strip().casefold()
    names = [str(s.get("season_name") or "").casefold() for s in seasons]
    tiers: List[List[Dict[str, Any]]]
    if needle.isdigit():
        target = int(needle)
        tiers = [
            [s for s in seasons if s.get("season_id") == target],
            [s for s in seasons if season_number(s) == target],
        ]
    elif re.fullmatch(r"s(\d+)", needle):
        tiers = [[s for s in seasons if season_number(s) == int(needle[1:])]]
    else:
        tiers = [
            [s for s, n in zip(seasons, names) if n == needle],
            [s for s, n in zip(seasons, names) if n == f"season {needle}"],
            [
                s
                for s, n in zip(seasons, names)
                if needle in n
            ],
        ]
    tier = next((t for t in tiers if t), [])
    if len(tier) == 1:
        return tier[0], None
    if tier:
        return None, (
            "ambiguous_season",
            [season_option(s) for s in tier[:MAX_SEASON_OPTIONS]],
        )
    return None, (
        "unknown_season",
        [season_option(s) for s in seasons[:MAX_SEASON_OPTIONS]],
    )



def _is_retryable(exc: Exception) -> bool:
    """Rate-limit-shaped failures: throttled HTTP, transient I/O, junk bodies."""
    if isinstance(exc, SRHRetryableFetchError):
        return True
    if isinstance(exc, urllib.error.HTTPError):  # subclass of URLError: check first
        return exc.code in RETRYABLE_HTTP_STATUSES
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def _retry_delay(exc: Exception, base: float, attempt: int) -> float:
    """Honour ``Retry-After`` when the site sends it; else exponential backoff."""
    if isinstance(exc, urllib.error.HTTPError) and exc.headers:
        try:
            after = float(exc.headers.get("Retry-After"))
            return max(0.0, min(after, MAX_RETRY_WAIT_SECONDS))
        except (TypeError, ValueError):
            pass  # HTTP-date form or garbage; fall through to backoff
    return min(base * (2**attempt), MAX_RETRY_WAIT_SECONDS)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class SimRacerHubClient:
    """Fetch + normalise SimRacerHub scoring data. Never raises out of
    :meth:`seasons`, :meth:`standings`, :meth:`results`, or
    :meth:`resolve_driver`. Only a league id is configured; season discovery
    infers the active season and every historical one from it. Talks to the
    site through one polite path: cached payloads, paced fetches, backed-off
    retries (SRH aggressively rate-limits automated traffic)."""

    def __init__(
        self,
        config: Optional[SimRacerHubConfig] = None,
        fetch_json: Optional[Callable[[str, float], Any]] = None,
        fetch_text: Optional[Callable[[str, float], str]] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self.config = config or SimRacerHubConfig.from_env()
        self._fetch_json = fetch_json or _default_fetch_json
        self._fetch_text = fetch_text or _default_fetch_text
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._last_call: Optional[float] = None
        self._cache: Dict[str, Any] = {}  # url -> (fetch-start clock time, payload)

    # -- polite fetch path: cache, pacing, retry -----------------------------

    def _pace(self) -> None:
        """Wait out ``min_interval_seconds`` since the last real fetch."""
        now = self._clock()
        if self._last_call is not None:
            wait = self._last_call + self.config.min_interval_seconds - now
            if wait > 0:
                log.info("simracerhub: pacing, sleeping %.1fs", wait)
                self._sleep(wait)
                now = self._clock()
        self._last_call = now

    def _get(self, url: str, fetcher: Optional[Callable[[str, float], Any]] = None) -> Any:
        """Cached + retried fetch (JSON unless a fetcher says otherwise, e.g.
        the HTML season-discovery pages). standings/results share one season
        URL and chat follow-ups repeat it, so the TTL keeps a question to
        ≤1 request."""
        ttl = self.config.cache_ttl_seconds
        now = self._clock()
        hit = self._cache.get(url)
        if ttl > 0 and hit is not None and now - hit[0] < ttl:
            return hit[1]
        self._pace()
        payload = self._fetch_with_retry(url, fetcher or self._fetch_json)
        if ttl > 0:
            self._cache[url] = (now, payload)
        return payload

    def _fetch_with_retry(self, url: str, fetch: Callable[[str, float], Any]) -> Any:
        attempts = max(1, self.config.max_retries + 1)
        for attempt in range(attempts):
            try:
                return fetch(url, self.config.timeout_seconds)
            except Exception as exc:  # classified below; degrades like before
                if attempt + 1 >= attempts or not _is_retryable(exc):
                    raise
                delay = _retry_delay(exc, self.config.retry_base_seconds, attempt)
                log.warning(
                    "simracerhub fetch failed (attempt %d/%d, %s); retrying in %.1fs",
                    attempt + 1,
                    attempts,
                    exc,
                    delay,
                )
                self._sleep(delay)
        raise AssertionError("unreachable: last attempt always returns or raises")

    # -- season discovery: league id -> active + historical seasons ---------

    def seasons(self, league_id: Optional[int] = None) -> Dict[str, Any]:
        """Catalog every season the league has run, the active one flagged.

        Why no season id is ever configured: league_series.php lists the
        league's series, series_seasons.php lists each series' historical
        seasons plus the current-season pointer. Pages ride the same cached,
        paced, retried fetch path, so repeat lookups stay polite."""
        if not self.config.enabled:
            return {"status": "disabled", "seasons": []}
        league = league_id or self.config.league_id
        if not league:
            return {
                "status": "no_league",
                "seasons": [],
                "error": "no league_id given and no SIMRACERHUB_LEAGUE_ID/SRH_LEAGUE_ID default",
            }
        league = int(league)
        try:
            index_html = self._get(
                build_series_index_url(league), fetcher=self._fetch_text
            )
            pages = []
            for meta in parse_series_index(index_html):
                seasons_html = self._get(
                    build_seasons_index_url(meta["series_id"]),
                    fetcher=self._fetch_text,
                )
                pages.append((meta, parse_seasons_page(seasons_html)))
        except Exception:
            log.exception("simracerhub season discovery failed (league=%s)", league)
            return {"status": "error", "seasons": []}
        catalog = assemble_season_catalog(league, pages)
        if not catalog["seasons"]:
            return {"status": "no_data", "seasons": [], "league_id": league}
        return {"status": "ok", **catalog}

    def _season_for(
        self, season: Optional[Union[str, int]]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Resolve a season reference via discovery: ``(entry, None)`` or
        ``(None, degraded-payload)`` (discovery failure or unmatched season —
        the payload lists the selectable seasons)."""
        catalog = self.seasons()
        if catalog["status"] != "ok":
            degraded = {"rows": [], **catalog}
            degraded["seasons"] = catalog.get("seasons", [])[:MAX_SEASON_OPTIONS]
            return None, degraded
        entry, err = match_season(season, catalog)
        if err is not None:
            status, options = err
            return None, {
                "status": status,
                "rows": [],
                "season_query": season,
                "seasons": options,
                "error": f"season {season!r} is {status}; pick one of the listed seasons",
            }
        return entry, None

    def standings(
        self,
        season: Optional[Union[str, int]] = None,
        driver: Optional[str] = None,
        days: Optional[int] = None,  # accepted for filter symmetry; n/a here
    ) -> Dict[str, Any]:
        """Season standings. ``season`` omitted/alias = the active season;
        "Season 14"/"S14"/season-id = a historical one."""
        if not self.config.enabled:
            return {"status": "disabled", "rows": []}
        entry, degraded = self._season_for(season)
        if degraded is not None:
            return degraded
        try:
            payload = self._get(build_standings_url(entry["season_id"]))
        except Exception:
            log.exception(
                "simracerhub standings fetch failed (season=%s)", entry["season_id"]
            )
            return {"status": "error", "rows": []}
        if not isinstance(payload, dict):
            return {"status": "error", "rows": []}
        if payload.get("error"):
            return {"status": "no_data", "rows": [], "error": str(payload["error"]).strip()}
        rows = normalize_standings(
            payload, driver=clean_driver_filter(driver or ""), max_rows=self.config.max_rows
        )
        if not rows:
            return {"status": "no_data", "rows": [], "season": season_option(entry)}
        return {
            "status": "ok",
            "season": season_option(entry),
            "season_id": entry["season_id"],
            "rows": rows,
        }

    def results(
        self,
        season: Optional[Union[str, int]] = None,
        driver: Optional[str] = None,
        days: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Recent race finishes; ``season`` resolution as in :meth:`standings`."""
        if not self.config.enabled:
            return {"status": "disabled", "rows": []}
        entry, degraded = self._season_for(season)
        if degraded is not None:
            return degraded
        try:
            payload = self._get(build_standings_url(entry["season_id"]))
        except Exception:
            log.exception(
                "simracerhub results fetch failed (season=%s)", entry["season_id"]
            )
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
            return {"status": "no_data", "rows": [], "season": season_option(entry)}
        return {
            "status": "ok",
            "season": season_option(entry),
            "season_id": entry["season_id"],
            "rows": rows,
        }

    def resolve_driver(self, name: str) -> Dict[str, Any]:
        """Resolve a driver name to SimRacerHub ids (helps the model re-query)."""
        if not self.config.enabled:
            return {"status": "disabled", "drivers": []}
        needle = clean_driver_filter(name)
        if not needle:
            return {"status": "empty_query", "drivers": []}
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
        seasons = self.seasons

        @strands_tool
        def get_simracerhub_stats(
            kind: str,
            season: Optional[str] = None,
            driver: Optional[str] = None,
            days: Optional[int] = None,
        ) -> Dict[str, Any]:
            """Look up the league's racing results and driver stats on SimRacerHub.

            ``kind`` is "standings" (season points table: points, wins, top-5s,
            incidents, safety rating, team per driver), "results" (individual
            race finishes: start/finish position, points, incidents, rating,
            team), or "seasons" (every season the league has run, newest
            first). ``season`` picks which season to read: omit it (or
            "active"/"current"/"latest") for the active one; for a historical
            season pass its name/number ("Season 14", "S14") or its season id.
            ``driver`` optionally filters by driver name OR team name
            (substring, e.g. "McFast" or "Test Squad"). ``days`` limits
            "results" to the last N days (default 14). Returns compact rows,
            or a status like "no_data" when nothing was uploaded yet; an
            unknown season name comes back with the seasons to pick from.
            """
            kind_norm = (kind or "").strip().lower()
            if kind_norm in ("seasons", "season", "list"):
                return seasons()
            if kind_norm in ("standings", "points", "table", "champ", "championship"):
                return standings(season=season, driver=driver)
            if kind_norm in ("results", "race", "races", "finishes", "history"):
                return results(season=season, driver=driver, days=days)
            return {
                "status": "bad_kind",
                "rows": [],
                "error": "kind must be 'standings', 'results', or 'seasons'",
            }

        return get_simracerhub_stats


# ---------------------------------------------------------------------------
# Default fetcher and module-level singleton used by ChatHandler wiring
# ---------------------------------------------------------------------------


def _default_fetch_json(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        # A rate-limited SRH answers with an HTML teapage under 200; mark it
        # retryable so the client backs off instead of surfacing "error".
        raise SRHRetryableFetchError(
            f"non-JSON response from {url}: {body[:120]!r}"
        ) from exc


def _default_fetch_text(url: str, timeout: float) -> str:
    """Plain-text (HTML) fetcher for the season-discovery pages; transport
    errors go through the same retry path as the JSON one."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


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
