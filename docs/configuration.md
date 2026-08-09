# Configuration

Configuration is read from environment variables when the process starts.
`.env` is not loaded automatically; run `source .env` or export variables by
another mechanism before starting Python.

Boolean variables accept `1`, `true`, `yes`, or `on` case-insensitively. Other
values are false. A misspelling therefore does not fail closed: for example,
`DRY_RUN=treu` disables dry-run mode. Confirm the startup line reports
`dry_run=True` before relying on it.

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
| `MAX_MODEL_TOKENS` | `1800` | solver-clamped `256..4096` | Response-token cap per normal model call; finalization is additionally capped at `768` |
| `MAX_ATTEMPTS` | `3` | minimum `1` | Normal attempts before a tile stops retrying |
| `RECOVERY_ATTEMPTS` | `1` | minimum `0` | Deferred attempts allowed after no-candidate exhaustion |
| `RECIPE_BACKOFF_SECONDS` | `30` | minimum `5` | Family cooldown after solver exhaustion |
| `BOARD_POLL_SECONDS` | `2` | minimum `0.5` | Base interval between board refreshes |
| `SUBMIT_INTERVAL` | `3.15` | minimum `3.05` | Minimum spacing between submission attempts |
| `SUBMISSION_SOFT_DEADLINE_SECONDS` | `15` | minimum `1` | Age at which queued candidates gain deadline priority |
| `MIN_CONFIDENCE` | `0.82` | clamped `0..1` | Candidate confidence threshold |
| `CANDIDATE_BUFFER` | `16` | minimum `1` | Pause new work when verified submissions back up |
| `CPU_HEAVY_WORKERS` | `1` | minimum `1` | Maximum concurrent CPU-heavy deterministic handlers |
| `FAST_PATHS` | true | boolean | Attempt deterministic handlers after eligible recipe replay |
| `FAST_PATH_ONLY` | false | boolean | Force handler dispatch after replay, skip model fallback, and make handler misses terminal |

Invalid numeric strings raise during startup. `AgentConfig.from_env()` applies
the `256` minimum while reading `MAX_MODEL_TOKENS`; `TileSolver` applies the
final `256..4096` clamp when it is constructed. Normal model requests use that
normalized value; a restricted finalization request uses the smaller of that
value and `768`. Other lower bounds do not prevent operationally poor high
values. Increase concurrency only after measuring model throttling, CPU
pressure, queue delay, and claimed-tile rate.

## Effective behavior and interactions

### Workload selection

- `TASK_FILTER` trims whitespace and matches exact, currently open tile IDs.
  Unknown, closed, or already-solved IDs are ignored.
- A positive `MAX_TILES` freezes the first richest-first IDs observed on the
  initial board sync. The bounded run does not replace a selected ID with a new
  one that appears later.
- `RUN_ONCE=1` exits only after every selected record is terminal and the active
  worker and candidate queues are empty. It does **not** imply `DRY_RUN=1`.
- A normal exhausted attempt may enter deferred recovery for at least 60
  seconds. For a short diagnostic, set `MAX_ATTEMPTS=1 RECOVERY_ATTEMPTS=0`.

### Model budgets

`MAX_TURNS` is the ceiling, but lower-value tasks receive smaller normal
budgets:

| Tile value | Normal tool-loop turns |
|---:|---:|
| 100–300 | `min(MAX_TURNS, 8)` |
| 400 | `min(MAX_TURNS, 10)` |
| 500 and above | `MAX_TURNS` |

The solver may add one restricted finalization turn only when a prior
`record_answer` call was attempted but did not record a candidate. That turn
can use only `record_answer`, existing artifacts, and at most 768 response
tokens. It does not perform more investigation.

### Fast-path controls

- Recipe replay is evaluated before either fast-path flag. An eligible,
  compatible cached recipe can return a candidate when `FAST_PATHS=0` or
  `FAST_PATH_ONLY=1`.
- `FAST_PATHS=0` skips deterministic handler dispatch after replay. If no
  replay succeeds and `FAST_PATH_ONLY=0`, the task proceeds to the model.
- `FAST_PATH_ONLY=1` enables handler dispatch even if `FAST_PATHS=0`. If
  neither replay nor a handler solves the task, the model is skipped.
- After replay, both `not_applicable` and `inconclusive` handler outcomes are
  terminal in fast-path-only mode; they do not use normal retries or deferred
  recovery.
- The flags do not guarantee model-only or handler-only behavior when an
  eligible replay is already cached. To isolate handlers, start a fresh CLI
  process with `FAST_PATH_ONLY=1` or run the handler's offline tests directly.
- `CPU_HEAVY_WORKERS` applies to the five titles listed in
  [Deterministic solvers](deterministic-solvers.md#heavy-compute), within the
  overall `MODEL_WORKERS` cap. Effective CPU-heavy concurrency cannot exceed
  either setting.

### External effects

| Profile | Event API reads | Model calls | `/api/submit` |
|---|---:|---:|---:|
| Offline unit tests | No | No | No |
| `FAST_PATH_ONLY=1 DRY_RUN=1` | Yes | No | No |
| `DRY_RUN=1` | Yes | When needed | No |
| Defaults or `RUN_ONCE=1` without dry-run | Yes | When needed | Yes |

Dry-run mode can still consume model quota, download untrusted files, execute
task-local helper code, and make task-required requests to the event host. Use
a disposable least-privilege environment for every live profile.

## Recommended profiles

### Bounded live model inspection

```bash
export RUN_ONCE=1
export MAX_TILES=3
export DRY_RUN=1
export VERBOSE=1
export MAX_ATTEMPTS=1
export RECOVERY_ATTEMPTS=0
```

This is bounded but not offline. It may call the model proxy and run
model-proposed Python.

### Fresh no-model handler calibration

```bash
export RUN_ONCE=1
export MAX_TILES=10
export DRY_RUN=1
export FAST_PATH_ONLY=1
```

This fresh CLI profile guarantees that the solver does not construct a model
fallback. Its replay cache starts empty, and the no-model run cannot create new
replay recipes, so selected tasks exercise deterministic dispatch. A caller
that instead reuses a populated solver/cache can still replay an eligible,
compatible recipe before handler dispatch. Both cases still read the live
board, tasks, files, and task-required event-host web pages.

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

`agent.zip` intentionally excludes `.env`, and the application does not load
dotenv files automatically. Set connection variables and any optional tuning
knobs through the hosted service's environment-injection mechanism, if one is
available. If the service injects only the required connection variables, the
archive-compliant hosted configuration uses the documented defaults. Do not add
`.env` to the archive unless the deployment contract and `RUNTIME_FILES` parity
check are deliberately changed and revalidated; never package secrets.
