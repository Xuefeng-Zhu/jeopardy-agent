# Event API reference

This reference describes the protocol consumed by `jeopardy.py` and
`round2_acceptance.py`. The event service owns the protocol and may change it;
the client implementation remains the executable source of truth.

All endpoints are relative to `JEOPARDY_BASE_URL`. Authenticated requests send:

```http
X-Api-Key: <TEAM_API_KEY>
```

## Game endpoints

### `GET /api/board`

Returns phase, board cells, claims, scoreboard information, server time, and an
account-specific `you.solved_ids` list.

Known phases and playable board keys:

| Phase | Board key |
|---|---|
| `practice` | `practice` |
| `round1` | `qual` |
| `game` | `main` |

Only these three phases have an explicit client mapping. If the service reports
another phase, `live_board()` falls back to the first available key in
`main`, `qual`, `practice` order. Treat that as protocol drift rather than proof
that submissions are valid: keep `DRY_RUN=1`, inspect the board payload, and
confirm the event's current lifecycle rules.

Cells contain `open_ids`, `remaining`, and `total`. Every ID in `open_ids` is
an independent, currently claimable tile. `open_tiles()` expands those stacks
and excludes locked or previously solved IDs.

The board may be publicly readable. The client requires a `you` block and
raises `AuthError` when it is absent, preventing a bad key from masquerading as
a healthy session.

### `GET /api/task/{id}`

Returns task metadata similar to:

```json
{
  "id": "PR-A1",
  "category": "Ancient Scrolls",
  "title": "...",
  "points": 100,
  "board": "practice",
  "prompt": "...",
  "files": ["document.txt"],
  "answer_format": "exact",
  "claimed": false
}
```

The client treats HTTP 403 and 404 as `TileUnavailable`, which is a normal
board-state outcome rather than a fatal transport error.

Supported answer formats observed by the client:

- `exact` — whitespace-normalized string equality
- `exact_ci` — case-insensitive equality
- `numeric` — numeric parsing and service-defined tolerance
- `literal` — Python/JSON literal value comparison
- `validator` — service-side property validation

### `GET /api/task/{id}/file/{name}`

Downloads one task file. The client requests identity encoding, checks the HTTP
status, rejects empty bodies, validates content length when it represents the
decoded bytes, and installs the completed file atomically.

Download reuse is process-local. A file is skipped only after this process
completed its download and its size and modification time remain unchanged. A
pre-existing file not known to the current process is replaced through a new
temporary download. If cancellation or transfer validation fails, the old
destination remains in place and the temporary file is removed.

### `POST /api/submit`

Request:

```json
{"task_id": "PR-A1", "answer": "..."}
```

Known result values include:

- `correct`
- `incorrect`
- `already_claimed`
- `locked_out` or `locked`
- `rate_limited`
- `wrong_phase`
- `forbidden`
- `voided`
- `unknown_task`

These are application outcomes returned in successful HTTP responses. A
response without a `result` field is treated as a failed call.

The controller handles outcomes as follows:

| Result | Controller action |
|---|---|
| `correct` | Mark the tile solved, update recipe/replay trust, and discard task runtime state |
| `dry_run` | Mark the tile solved locally without an API submission |
| `already_claimed`, `voided`, `unknown_task` | Mark the tile claimed/terminal and discard task runtime state |
| `rate_limited`, `locked_out`, `locked`, `wrong_phase` | Retain the verified candidate and delay it using `retry_in`, `retry_after`, or `cooldown`, defaulting to five seconds |
| `incorrect` | Increment and block the recipe generation, discard the exact replay fingerprint, disable the implicated fast handler for the next generation, and retry/fail affected siblings |
| `forbidden` | Mark the tile failed; the client interprets this as scored submissions requiring the hosted agent |
| Missing `result` field | Raise a transport-style client error; retain and delay the same candidate for five seconds |
| Present but empty or unrecognized `result` | Schedule a fresh solve attempt, subject to attempt limits |

Transport exceptions retain and delay the candidate rather than converting the
call into an incorrect answer. The single submission lane enforces at least
3.05 seconds between calls because the client assumes a three-second service
limit.

The client comments describe incorrect scored answers as point-costing and
cooldown-triggering. Treat those event rules as service-controlled and verify
the current round policy before removing `DRY_RUN=1`.

### `GET /api/me`

Returns team identity, model usage, rate-limit information, and hosted-agent
status. Live acceptance passes or fails on `agent.state` and `agent.healthy`.
It includes `agent.revision` in diagnostic output for an operator to compare,
but does not validate that revision against local archive bytes.

## Hosted-agent endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/agent/submit` | Upload, deploy, and restart an archive supplied as multipart field `file` |
| `POST /api/agent/start` | Start the deployed agent |
| `POST /api/agent/stop` | Stop the deployed agent |
| `GET /api/agent/status` | Read hosted process status |
| `GET /api/agent/logs?tail=N` | Read recent stdout/stderr logs |

The uploaded archive must contain `main.py` at its root. The runner executes
`python -u main.py`. Every imported local module must also be present.

Upload, start, and stop are external state changes. A successful HTTP response
does not by itself prove sustained health. After an upload, compare the reported
revision with the response/dashboard record, confirm `/api/me` reports
`state="running"` and `healthy=true`, inspect recent logs, and run the live
acceptance checks described in [Operations](operations.md#post-deployment-verification).

## Task categories

The solver has dedicated playbooks for six categories:

| Category | Primary tool pattern |
|---|---|
| Needle in the Haystack | Inspect schemas, then filter or aggregate data with Python |
| The Dark Web | Preserve HTTP sessions, forms, cookies, tokens, and redirects |
| Ship It | Extract, inspect, run, patch, and retest supplied code |
| Ancient Scrolls | Search documents, resolve references, and apply amendments |
| Cryptic | Inspect bytes, decode archives/formats, and verify round trips |
| Heavy Compute | Implement bounded search, pruning, or vectorized computation |

## Error handling

- `AuthError` is fatal because all authenticated work will fail until the key
  is corrected.
- `TileUnavailable` is expected when a task is locked, inactive, or unknown.
- Other non-success HTTP responses raise `JeopardyError` with a bounded server
  detail.
- Board polling tolerates transient non-auth errors with bounded backoff.
- Submission transport failures retain the candidate for retry.
