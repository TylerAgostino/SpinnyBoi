# spinnyBoi

Discord bot for a SimRacerHub iRacing league, built on [py-cord](https://docs.pycord.dev).

**Add to a server:**
https://discord.com/api/oauth2/authorize?client_id=1051558649379692584&permissions=35840&scope=bot

## What it does

| Surface | Notes |
|---|---|
| `/spin preset …` | Spin a wheel preset (autocomplete over preset names) |
| `/spin custom a,b,c` / `/spin order a,b,c` | One-off weighted spin / random ordering from comma-separated options |
| `/schedule spin …` / `/schedule list` / `/schedule cancel …` | Schedule preset spins |
| `/spin auditor` | Restart the session-auditor container via Portainer webhook (admin-only) |
| `/register`, `/check_registrations` | Driver registration against the league Google Sheet (car-number assignment, roster checks) |
| `/mark_attendance` | Attendance from iRacing race results into the attendance Google Sheet |
| `/standings` | SimRacerHub standings rendered to an image (headless Firefox) |
| `/spincident` | Incident poll with regional-indicator letter reactions |
| `/spinfo <preset> <tab>` | Show a preset tab's options and weights |
| `@mention` chat | Conversational replies from a local LLM (Ollama, OpenAI-compatible endpoint) with web-lookup and SimRacerHub tools; recent image attachments are fed to the model as input |

Wheel presets live in the Google Sheet named by `GSHEET_ID`: a `presets` tab maps preset names to option tabs, each holding wheel entries and weights.

## Running locally

Requires **Python 3.12**. Rendering (`/standings`, wheel GIFs) drives **headless Firefox** via Selenium — install Firefox, and either `geckodriver` on `PATH` or let Selenium Manager fetch it.

```sh
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -r requirements-dev.txt              # runtime + test tooling
set BOT_TOKEN=...  GSHEET_ID=...                 # see Configuration
python spinnyBoi.py
```

The bot exits immediately, naming the variable, if a required env var is missing.

## Configuration

### Required

| Variable | Purpose |
|---|---|
| `BOT_TOKEN` | Discord bot token |
| `GSHEET_ID` | Google Sheet id holding the `presets` tab and wheel option tabs |

### Google Sheets

| Variable | Default | Purpose |
|---|---|---|
| `GOOGLE_CREDENTIALS_FILE` | `credentials.json` | Service-account key file (git-ignored) |
| `GOOGLE_CREDENTIALS_JSON` | — | Service-account JSON inline (alternative to the file) |
| `REGISTRATION_SHEET_ID`, `ATTENDANCE_SHEET_ID` | — (required) | Roster/attendance sheet ids; the registration/attendance commands raise a configuration error without them |
| `LEAGUE_POINTS_SHEET`, `LEAGUE_POINTS_ID_COLUMN`, `LEAGUE_REGISTRATION_SHEET` | league defaults | Sheet tab names used by standings/registration |

### iRacing (attendance results lookup)

| Variable | Notes |
|---|---|
| `IRACING_EMAIL`, `IRACING_PASSWORD` | Account credentials |
| `IRACING_CLIENT_ID`, `IRACING_CLIENT_SECRET` | OAuth 2.1 password-limited flow (preferred in Docker/headless) |
| `IRACING_LEAGUE_ID` | League id for roster/member checks |

### LLM chat (Ollama / OpenAI-compatible server)

| Variable | Default | Purpose |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://192.168.1.152:11080/v1` | OpenAI-compatible endpoint |
| `OLLAMA_MODEL` | `unsloth/Qwen3.8-Flash-Next-GGUF:IQ3_XXS` | Model id |
| `CHAT_VISION_ENABLED` | on | Allow image understanding for attached screenshots |
| `CHAT_MAX_TOKENS`, `CHAT_IMAGE_MAX_DIM`, `CHAT_IMAGE_TOTAL_BYTES`, `CHAT_TURN_TIMEOUT_SECONDS` | bounded defaults | Token/pixel/turn cost guards |

### Feature toggles & tuning

`SIMRACERHUB_ENABLED`, `SIMRACERHUB_LEAGUE_ID` (falls back to `SRH_LEAGUE_ID`), `SIMRACERHUB_TIMEOUT_SECONDS`, `SIMRACERHUB_MIN_INTERVAL_SECONDS`, `SIMRACERHUB_MAX_RETRIES`, `SIMRACERHUB_RETRY_BASE_SECONDS`, `SIMRACERHUB_CACHE_TTL_SECONDS`, `SIMRACERHUB_MAX_ROWS`; `WEB_LOOKUP_ENABLED`, `WEB_LOOKUP_TIMEOUT_SECONDS`, `WEB_LOOKUP_MIN_INTERVAL_SECONDS`, `WEB_LOOKUP_MAX_RESULTS`, `WEB_LOOKUP_MAX_QUERY_CHARS`, `WEB_LOOKUP_MAX_SNIPPET_CHARS` — unset means enabled/default per `modules/env_config.py` semantics.

### Misc

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `sqlite:///data/spinny.db` | Scheduler state (SQLite); `data/` is git-ignored |
| `LEAGUE_DEBUG_GUILD_IDS` | league default | Guilds where slash commands register instantly |
| `LEAGUE_STAFF_ROLE_ID`, `LEAGUE_DRIVER_ROLE_ID`, `LEAGUE_AUTO_DELETE_CHANNEL_ID`, `LEAGUE_SEASON`, `SRH_LEAGUE_ID` | league defaults | Roles/channels/ids; `SRH_LEAGUE_ID` is the only SimRacerHub id — seasons and series are discovered from it |

## Tests

```sh
python -m pytest                    # offline default suite (live tests deselected)
SIMRACERHUB_LIVE=1 python -m pytest -m live    # opt-in: real SimRacerHub endpoints
```

Wheel/standings tests launch headless Firefox, so a browser + geckodriver must be available locally (the runtime Docker image ships both).

## Deployment

- **Image**: `Dockerfile` (python:3.12-slim + Firefox + geckodriver + ms core fonts).
- **Host**: `docker_compose.yaml` defines the service and injects env/secrets; the container persists `/app/data` for the scheduler DB.
- **CI/CD** (Drone, see `.drone.yml`): pushes to any branch and pull requests run `test` (offline suite in `python:3.12-slim`, with `firefox-esr` + geckodriver provisioned for the browser-dependent wheel tests). Release steps are push/main-only with their own changeset filters — `build` (publishes `:latest`, on Python/text/Dockerfile changes) and `deploy` (Portainer webhook, on Python/text/compose/Dockerfile changes) — and when eligible they run in the order `test → build → deploy`; a run with no release step eligible still just runs `test`.
