# Configuration

Configuration is read from environment variables when the process starts.
`.env` is not loaded automatically; run `source .env` or export variables by
another mechanism before starting Python.

Boolean variables accept `1`, `true`, `yes`, or `on` case-insensitively. Other
values are false.

## Required connection variables

| Variable | Required | Description |
|---|---:|---|
| `JEOPARDY_BASE_URL` | Yes | HTTPS base URL for the event API, without a trailing slash |
| `TEAM_API_KEY` | Yes | Team credential sent as the `X-Api-Key` header |
| `ANTHROPIC_BASE_URL` | Usually derived | Event model proxy; defaults to `${JEOPARDY_BASE_URL}/anthropic` |
| `ANTHROPIC_API_KEY` | Usually derived | Proxy credential; defaults to `TEAM_API_KEY` |
| `MODEL` | No | Requested model identifier; defaults to `claude-haiku-4-5` and may be overridden by the event proxy |

Use HTTPS. An HTTP base URL would expose the team credential in transit.

## Runtime controls

| Variable | Default | Enforced bound | Effect |
|---|---:|---:|---|
| `VERBOSE` | false | boolean | Log bounded tool inputs/results and solver prompts |
| `TASK_FILTER` | empty | comma-separated IDs | Work only the listed open tiles |
| `MAX_TILES` | `0` | minimum `0` | Cap unique selected tiles; `0` means all |
| `RUN_ONCE` | false | boolean | Exit when the selected bounded workload finishes |
| `DRY_RUN` | false | boolean | Solve and verify but return `dry_run` without calling submit |
| `MODEL_WORKERS` | `4` | minimum `1` | Maximum concurrent tile workers |
| `MAX_TURNS` | `12` | minimum `2` | Upper model-turn budget; lower tiers may receive fewer turns |
| `MAX_MODEL_TOKENS` | `1800` | minimum `256` | Response-token cap per normal model call |
| `MAX_ATTEMPTS` | `3` | minimum `1` | Normal attempts before a tile stops retrying |
| `RECOVERY_ATTEMPTS` | `1` | minimum `0` | Deferred attempts allowed after no-candidate exhaustion |
| `RECIPE_BACKOFF_SECONDS` | `30` | minimum `5` | Family cooldown after solver exhaustion |
| `BOARD_POLL_SECONDS` | `2` | minimum `0.5` | Base interval between board refreshes |
| `SUBMIT_INTERVAL` | `3.15` | minimum `3.05` | Minimum spacing between submission attempts |
| `SUBMISSION_SOFT_DEADLINE_SECONDS` | `15` | minimum `1` | Age at which queued candidates gain deadline priority |
| `MIN_CONFIDENCE` | `0.82` | clamped `0..1` | Candidate confidence threshold |
| `CANDIDATE_BUFFER` | `16` | minimum `1` | Pause new work when verified submissions back up |
| `CPU_HEAVY_WORKERS` | `1` | minimum `1` | Maximum concurrent CPU-heavy deterministic handlers |
| `FAST_PATHS` | true | boolean | Attempt deterministic handlers before the model |
| `FAST_PATH_ONLY` | false | boolean | Skip the model when no deterministic handler solves the task |

Invalid numeric strings raise during startup. Bounds prevent unsafe low values
but do not prevent an operationally poor high value. Increase concurrency only
after measuring model throttling, CPU pressure, queue delay, and claimed-tile
rate.

## Recommended profiles

### Safe local inspection

```bash
export RUN_ONCE=1
export MAX_TILES=3
export DRY_RUN=1
export VERBOSE=1
```

### Deterministic calibration

```bash
export RUN_ONCE=1
export MAX_TILES=10
export DRY_RUN=1
export FAST_PATH_ONLY=1
```

This profile guarantees that the solver does not construct a model fallback
for unmatched tasks.

### Hosted long-running process

Use the defaults unless a measured run supports a change:

```bash
unset RUN_ONCE DRY_RUN TASK_FILTER MAX_TILES VERBOSE FAST_PATH_ONLY
```

## Credential handling

- Keep credentials only in the shell, an ignored local `.env`, or the hosted
  platform's injected environment.
- Never add real values to `.env.example`.
- Never include a credential-bearing `.env` in `agent.zip`.
- Do not print the full key. Some transport errors include only its length and
  a short prefix to make configuration mistakes diagnosable.
- Rotate the team key if it appears in Git history, logs, screenshots, or a
  shared archive; deleting a later copy is not sufficient.

## Hosted constraints

The supplied event documentation describes a Python 3.12 container with two
CPU cores, 2 GB of memory, and network access limited to the event service.
Dependencies from `requirements.txt` are installed at deployment time. Treat
these properties as event-controlled and recheck them if the service changes.
