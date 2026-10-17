# Glossary

Domain terms for spinnyBoi (the iRacing league Discord bot) and the deep modules
that own them. Architecture vocabulary (module, interface, depth, seam, adapter,
leverage, locality) follows the `codebase-design` skill.

## League domain

- **League** — the SimRacing league (iRacing `league_id` 8579) the bot serves.
- **Season** — a league season (e.g. S16). **LeagueConfig**
  (`modules/league_config.py`) is the single source of every season-derived
  fact: default/seasons list, sheet tabs (`S15`..), stats series, staff role,
  season prompt. Built once at wiring time via `LeagueConfig.from_env()`;
  `docker-compose.yaml` env vars override the baked defaults. A season flip is
  one edit here.
- **Incident** — a rules-infraction poll run per race by `incidentCog`
  (in-memory poll state only; the summary feature is unwired).

## Registration & attendance

- **Registration** — a driver signing up for a season, recorded in Google
  Sheets. `registrationCog` owns the Discord UI (modal validation, embeds).
- **Roster** — the season's signed-up drivers. **RegistrationRoster**
  (`modules/registration_roster.py`) is the roster DAO: read/register/update/
  drop, `roster_diff`, `is_registered`, and the pure
  `penalty_prefix(member_display_name)` (`"Penalty: "` for existing roster
  names).
- **Attendance** — who ran which race, recorded per season from iRacing
  results by `attendanceCog` (`/mark_attendance`) into sheets via
  **SheetsClient**.

## Wheel & presets

- **Preset** — a named spin configuration: one row of the `presets` sheet,
  mapping option-tab names to filter strings. Owned by **PresetSource**
  (`modules/preset_source.py`), which answers: which presets exist
  (autocomplete), which tabs a preset has, what filter a `(preset, tab)` pair
  uses (`resolve_tab`, case-insensitive, real tab name returned), and the raw
  option frame of any tab (`read_tab`). `PresetSource.default()` is the shared
  instance; failures keep the previous frame serving.
- **Tab / filter string** — an option sheet and its per-preset query
  (`series:8579`, `day:=friday`, ...). The filter mini-DSL is parsed by
  `WheelCog.generate_option_set`.
- **Wheel / spin** — **WheelSpinner** (`modules/WheelSpinner.py`): a weighted
  spinner. Construction *decides* the winner through an **rng seam** (pass any
  `random.Random`-like object for deterministic spins); the outcome is the
  explicit `SpinResult` (`result.winner`, `result.response`,
  `result.next_spin`), not hidden state read after `return_gif`.
- **Scheduled spin** — a preset spin queued in SQLite by **Scheduler**
  (`modules/scheduler/scheduler.py`); persistence is inline in the
  Scheduler class over synchronous `sqlite3` connections, DB path from
  `DATABASE_URL` (default `sqlite:///data/spinny.db`). Handlers register
  by name (`scheduler.register_handler`); `ScheduledEvent.function_name`
  routes dispatch — currently `spin_preset` (registered by `wheelCog`).

## Chat (strands)

- **Turn** — `ChatHandler.respond_in_chat(message, bot_user, model=None)`:
  one chat-message → reply exchange; `model` injects a model double in
  tests, and a tool is "disabled" by its adapter getter returning `None`
  (env flags like `WEB_LOOKUP_ENABLED`), not by control-flow exceptions.
- **Chat tool** — no shared construction frame module exists;
  `ChatHandler._enabled_tools()` collects tools straight from the adapter
  singletons — `league_memory` (`lookup_tool`), `web_lookup`
  (`lookup_tool`), `simracerhub` (`stats_tool`). An unconfigured or broken
  adapter is logged and skipped: a dead tool never kills the reply.
  The SRH client self-polices (paced fetches, backoff retries honouring
  `Retry-After`, short payload-cache TTL) because simracerhub.com
  aggressively rate-limits automated traffic.
- **League memory** — in-memory vector store over the channels listed in
  `LEAGUE_VECTOR_CHANNEL_ID` (comma-separated), indexed at startup and
  queried by the chat agent (`league_memory.py`). How far back indexing
  reaches is set by **history cutoff** (`LEAGUE_VECTOR_HISTORY_SINCE`: ISO
  date, relative age like `30d`, or oldest message id; default all time).
  `LEAGUE_VECTOR_HISTORY_SINCE_BY_CHANNEL` ("111=365d,222=2026-01-05", the
  form docker compose passes through) or a `LEAGUE_VECTOR_HISTORY_SINCE_<channel_id>`
  key overrides it per channel, so different channels can be indexed to
  different depths in time.
- **Web lookup** — live search/fetch adapter (`web_lookup.py`) behind a
  `fetch_json` seam (tests inject doubles); tuned by `WEB_LOOKUP_*` env.

## Infra seams

- **SheetsClient** (`modules/sheets_client.py`) — the deep Google Sheets
  module: `available()`, `read_grid` (raises `SheetsError` naming the
  tab; the spreadsheet id is not in the message), `read_rows` (rows as
  dicts — but note it *swallows* `SheetsError` and returns `[]`),
  `strip_id_quote()` strips the leading apostrophe Google echoes back,
  `append_record` / `update_record` / `update_column_cells` raise
  `SheetsError` describing the failed operation (and key/sheet name for
  `update_record`). Owns the google-api-client plumbing; credentials from
  `GOOGLE_CREDENTIALS_FILE` / `GOOGLE_CREDENTIALS_JSON`.
- **iRacing API handler** — `iRacingAPIHandler` (`modules/api.py`), a
  `requests.Session` subclass owning OAuth/legacy login and bounded API
  calls. One client per process: `shared_iracing_handler()` builds lazily
  via `iracing_handler_from_env()` under a lock (success memoized; failed
  builds retry). `attendanceCog`/`registrationCog` delegate their
  `_get_iracing_api` to it and accept `iracing_api=` constructor
  injection that bypasses the global (tests use this seam).
  `standingsCog`/`wheelCog` do not use it.
- **DB paths** — scheduler SQLite via `DATABASE_URL`
  (`sqlite:///data/spinny.db` default); the only env-selected DB today.
