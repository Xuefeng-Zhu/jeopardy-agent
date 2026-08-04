# Operations

This runbook covers local verification, packaging, deployment, monitoring, and
recovery. Commands assume the repository root as the working directory.

## Preflight

1. Confirm Python 3.12.
2. Load `JEOPARDY_BASE_URL` and `TEAM_API_KEY` without printing them.
3. Start with `DRY_RUN=1` for any new configuration or code path.
4. Run the offline test suite.
5. Rebuild and validate `agent.zip` before deployment.

```bash
python -V
python -m unittest discover -s tests -v
git diff --check
```

## Local execution

### Bounded smoke

```bash
RUN_ONCE=1 MAX_TILES=3 DRY_RUN=1 python main.py
```

Expected behavior:

- authentication succeeds on the first board poll;
- the phase is logged;
- at most three unique tiles are selected;
- candidates are verified and reported as `dry_run`; and
- the process exits after the bounded workload reaches terminal states.

### Focus one or more tasks

```bash
RUN_ONCE=1 TASK_FILTER=PR-W5,PR-C5 DRY_RUN=1 VERBOSE=1 python main.py
```

The filter accepts exact tile IDs and silently excludes IDs that are not open
on the current playable board.

### Long-running mode

```bash
python main.py
```

The controller continues polling through phase changes. Stop it with
`Ctrl-C`; the shutdown path cancels active workers, drains them, stops the
submission manager, and closes solver resources.

## Logging

Logs are timestamped on stdout. Important messages include:

- startup configuration;
- phase transitions;
- fingerprint summaries;
- solver strategy and fast-path outcome;
- candidate verification and queueing;
- submission outcomes and retry delays;
- tile-disappearance cancellation;
- solver exhaustion and family backoff; and
- board-poll or submission errors.

Use `VERBOSE=1` only for bounded diagnosis. It logs compact tool inputs and
results and therefore increases volume; do not assume task contents are
non-sensitive.

## Package the hosted runtime

The accepted runtime file set is defined by `RUNTIME_FILES` in
`round2_acceptance.py`. Build exactly that set:

```bash
python -m zipfile -c agent.zip \
  main.py jeopardy.py agent_types.py verification.py tool_runtime.py \
  solver.py orchestrator.py fast_paths.py requirements.txt
```

Inspect the archive:

```bash
unzip -l agent.zip
```

`main.py` must be at the archive root. Do not include `.env`, `.git`, tests,
task downloads, caches, or development virtual environments.

## Acceptance gates

### Saved logs

```bash
python round2_acceptance.py \
  --logs /path/to/logs.txt \
  --phase practice
```

The log input may be plain text or JSON containing a string `logs` field.

### Hosted process

```bash
python round2_acceptance.py --live --phase practice
```

Live mode also checks that the hosted agent reports `state="running"` and
`healthy=true`.

Default gates:

| Gate | Default |
|---|---:|
| First verified candidate | at most 3 seconds |
| Every category begins | within 10 seconds |
| First worker batch | four distinct category/tier recipes |
| Candidate queue wait p95 | at most 15 seconds |
| Already-claimed outcomes | below 10% |
| Incorrect submissions | exactly 0 |
| Archive parity | every runtime member matches source bytes |

Thresholds can be overridden through command-line flags. The command exits
`0` only when every gate passes, `1` when evaluated gates fail, and `2` when
logs, credentials, network calls, or archive setup cannot be evaluated.

## Deployment

Submitting an archive deploys and restarts the hosted process immediately:

```bash
curl -X POST "$JEOPARDY_BASE_URL/api/agent/submit" \
  -H "X-Api-Key: $TEAM_API_KEY" \
  -F file=@agent.zip
```

The event dashboard may provide drag-and-drop deployment and log viewing. A
deployment is an external state change: confirm the archive and intended phase
before submitting it.

Useful hosted endpoints are documented in [Event API reference](event-api.md).

## Recovery playbook

### Authentication failure

Symptoms:

- startup exits with `[auth]`;
- `/api/board` has no `you` block; or
- an API response reports an invalid key.

Actions:

1. Recopy the team key through the event's supported recovery path.
2. Confirm the base URL is HTTPS and has no incorrect port or trailing space.
3. Reload `.env` in the same shell.
4. Run a one-tile dry run before redeploying.

### `ModuleNotFoundError` after deployment

Rebuild the archive from the explicit runtime list. Add third-party packages
to `requirements.txt`; add imported local modules to both the archive command
and `RUNTIME_FILES` so parity checks enforce the new contract.

### High already-claimed rate

- Reduce solve latency before increasing workers.
- Inspect board polling and cancellation logs.
- Measure candidate queue wait and reduce submission backlog.
- Verify that CPU-heavy handlers are not monopolizing all worker capacity.

### Incorrect submission

An incorrect result invalidates the exact recipe generation and should be
treated as a release blocker:

1. Capture the task fingerprint, strategy, evidence, and verification source.
2. Disable or narrow the implicated deterministic handler.
3. Add the exact malformed or near-match shape as a regression test.
4. Re-run the full suite and a dry-run acceptance sample.

### Board or network instability

Board polling backs off up to ten seconds after repeated non-auth failures.
Submission transport failures retain and delay the candidate. Do not restart
immediately unless the process is unhealthy; a restart discards in-memory
recipe trust and replay state.
