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
- **Attendance** — per-season sign-ups (`attendanceCog`, sheets via
  **SheetsClient**).

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
- **Scheduled spin** — a preset spin queued in SQLite by **Scheduler**;
  persistence goes through the **EventStore** seam
  (`modules/scheduler/store.py`; `SQLiteEventStore` in production). Handlers
  register by name (`scheduler.register_handler`); jobs carry
  `(handler_name, data)` — currently `wheel`, `role_prompt`, `message`.

## Chat (LangChain/strands)

- **Turn** — `ChatHandler.respond_in_chat`: one chat-message → reply exchange
  with injected `ChatModel` deps and an explicit `disabled_tools` protocol
  (no bare-`KeyError` control flow).
- **Tool frame** — `modules/tool_frame.py`: the shared construction wrapper
  for the three chat tools. **ToolAdapter** names the seam; the adapters are
  `simracershub` (SimRacingHistory HTTP), `web_lookup` (ChromaDB embeddings),
  and `league_memory` (see below). One `ToolFrame.make_embedder()` fixes the
  per-tool `OPENAI_API_KEY` bug. The SRH client self-polices (paced fetches,
  backoff retries honouring `Retry-After`, short payload-cache TTL) because
  simracerhub.com aggressively rate-limits automated traffic.
- **League memory** — per-channel key/value notes the chat agent keeps
  (`league_memory.py` over `modules/memory_store.py`).
- **Web lookup** — embeddings-backed lookup (`web_lookup.py`, also over
  **memory_store**).

## Infra seams

- **SheetsClient** (`modules/sheets_client.py`) — the deep Google Sheets
  module: `read_rows` (rows as dicts, `strip_prefix()` strips the leading
  apostrophe Google echoes back), `append`, `update_cell`, `find_row`,
  `find_row_by_column`, `column_values`. Owns the gspread/ADC plumbing;
  `iracing_api_handler.IRacingAPIHandler` is the shared cached stats-API
  adapter used by `standingsCog`, `wheelCog`, and `simracershub`.
- **DB paths** — SQLite files (scheduler; legacy sqlite users of web_lookup).
  `DB_PATH`, `WEB_LOOKUP_DB_PATH`, `LEAGUE_MEMORY_DB_PATH` env vars.
