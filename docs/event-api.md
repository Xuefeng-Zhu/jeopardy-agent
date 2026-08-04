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

### `GET /api/me`

Returns team identity, model usage, rate-limit information, and hosted-agent
status. Live acceptance uses its `agent.state`, `agent.healthy`, and revision
fields.

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
