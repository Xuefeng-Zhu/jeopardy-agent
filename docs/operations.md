# Operations

This runbook covers local verification, packaging, deployment, monitoring, and
recovery. Commands assume the repository root as the working directory.

## Preflight

1. Confirm Python 3.12.
2. Run the offline test suite and diff check.
3. Rebuild and validate `agent.zip` before deployment.
4. Before a live run, load `JEOPARDY_BASE_URL` and `TEAM_API_KEY` without
   printing them.
5. Start with `DRY_RUN=1` for any new live configuration or code path.

```bash
python -V
python -m unittest discover -s tests -v
git diff --check
```

The version check, test suite, diff check, archive inspection, and archive
parity are all local. The test suite is the behavioral runtime check that needs
no event or model traffic. Every `main.py` mode authenticates to the live event.
`DRY_RUN=1` blocks answer submission only; see the
[configuration effects matrix](configuration.md#external-effects) before
choosing a runtime profile.

## Local execution

### Bounded live model smoke

```bash
RUN_ONCE=1 MAX_TILES=3 DRY_RUN=1 \
  MAX_ATTEMPTS=1 RECOVERY_ATTEMPTS=0 python main.py
```

Expected behavior:

- authentication succeeds on the first board poll;
- the phase is logged;
- at most three unique tiles are selected;
- candidates are verified and reported as `dry_run`; and
- the process exits after the bounded workload reaches terminal states.

This run can consume model budget and execute model-proposed Python. Use a
disposable environment. To exclude model calls while retaining live event
reads, add `FAST_PATH_ONLY=1`; unmatched or inconclusive deterministic tasks
then become terminal immediately.

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

### Interpreting a tile lifecycle

A typical successful log sequence is:

```text
[12:00:00] phase changed to 'practice'
[12:00:01] PR-A1: solving Ancient Scrolls 100pt attempt=1
[12:00:01] PR-A1: fingerprint generator=gf1:...
[12:00:01] PR-A1: fast path handler=... outcome=solved elapsed=...
[12:00:01] PR-A1: verified candidate queued confidence=... source=...
[12:00:01] PR-A1: submission -> 'dry_run'
```

The exact strategy lines differ for recipe replay and Claude. A missing
`verified candidate queued` line means the task never reached the submission
lane; inspect the immediately preceding fallback, rejection, retry, or terminal
message.

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

### Archive parity only

`round2_acceptance.py` always requires a log source, even when only the archive
changed. Use its parity function directly for a credential-free archive check:

```bash
python -c 'from pathlib import Path; from round2_acceptance import evaluate_archive; r = evaluate_archive(Path("agent.zip"), Path(".").resolve()); print(("PASS" if r.passed else "FAIL"), r.name + ":", r.detail); raise SystemExit(0 if r.passed else 1)'
```

This compares the archive member set and each member's extracted bytes. The
whole-ZIP hash may change when compression metadata changes even when parity
passes.

### Saved logs

```bash
python round2_acceptance.py \
  --logs /path/to/logs.txt \
  --phase practice
```

The log input may be plain text or JSON containing a string `logs` field.

The parser has a stricter line contract than native agent stdout:

- each metric-bearing line must begin with an unbracketed `HH:MM:SS ` outer
  timestamp;
- the line must then contain the runtime message, such as
  `[HH:MM:SS] phase changed to 'practice'`;
- analysis uses the **last** transition into the requested phase and stops at
  the next phase transition; and
- timestamps may cross midnight once within that phase segment.

Native `jeopardy.log()` output starts directly with `[HH:MM:SS]` and therefore
is not accepted without an outer timestamp. Normalize a captured native log
before evaluation:

```bash
set -o pipefail
DRY_RUN=1 VERBOSE=0 python main.py 2>&1 \
  | tee /path/to/native-agent.log

sed -E 's/^\[([0-9]{2}:[0-9]{2}:[0-9]{2})\]/\1 [\1]/' \
  /path/to/native-agent.log > /path/to/acceptance-agent.log
```

Stop the capture after the intended phase has a representative sample. This
live dry run may consume model quota; the command demonstrates capture, not a
free or automatically sufficient acceptance workload.

Inspect the transformed file before trusting it. Hosted log exports may already
contain an outer timestamp; do not add a second one.

A representative default-gate log must contain, within the selected phase:

1. a timestamped phase transition;
2. first-attempt solve lines for all six categories;
3. four distinct `category:points` entries among the first four `attempt=1`
   solve lines; this parser definition is not a concurrency snapshot;
4. at least one queued candidate that reaches a submission outcome; and
5. at least one resolved outcome for the claimed-rate gate.

A three-tile smoke cannot pass the all-category or four-recipe gates. A log can
be syntactically valid and still fail because the sample is incomplete. The
code enforces no larger minimum outcome count, so treat a passing claimed rate
from a very small sample as weak operational evidence.

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
| First `attempt=1` solve records | four distinct category/tier recipes |
| Candidate queue wait p95 | at most 15 seconds |
| Already-claimed outcomes | below 10% |
| Incorrect submissions | exactly 0 |
| Archive parity | every runtime member matches source bytes |

Thresholds can be overridden through command-line flags. The command exits
`0` only when every gate passes, `1` when evaluated gates fail, and `2` when
logs, credentials, network calls, or archive setup cannot be evaluated.

| Flag | Default | Controls |
|---|---:|---|
| `--phase` | required | Selects `practice`, `round1`, or `game` phase segment |
| `--archive` | `agent.zip` | Archive checked against local source |
| `--source-dir` | `.` | Directory containing the expected runtime members |
| `--first-candidate-seconds` | `3.0` | Maximum delay from phase transition to first queued candidate |
| `--category-start-seconds` | `10.0` | Maximum delay for every category's first attempt |
| `--queue-p95-seconds` | `15.0` | Maximum nearest-rank p95 queue-to-outcome delay |
| `--already-claimed-rate` | `0.10` | Strict upper bound for claimed outcomes among resolved outcomes |
| `--first-batch-size` | `4` | Required size and uniqueness of initial `category:points` recipes |

Exit `2` is an input/setup error, not a failed performance gate. Common causes
include a missing phase marker, native logs without an outer timestamp, a JSON
file without a string `logs` field, missing live credentials, or an HTTP error.
Missing, corrupt, or source-mismatched archives are evaluated gates and produce
exit `1`, not setup exit `2`.

Live mode fetches only the most recent 5,000 hosted log lines and health fields
from `/api/me`. It also checks **local** `agent.zip` against **local** source.
It does not prove that the hosted revision contains that local archive. Compare
the reported deployed revision with the upload response or dashboard record as
a separate release check.

The queue p95 uses the nearest-rank rule: sort the observed waits and select
rank `ceil(0.95 * count)`, with ranks starting at one.

## Deployment

Submitting an archive deploys and restarts the hosted process immediately:

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$JEOPARDY_BASE_URL/api/agent/submit" \
  -H "X-Api-Key: $TEAM_API_KEY" \
  -F file=@agent.zip
```

The event dashboard may provide drag-and-drop deployment and log viewing. A
deployment is an external state change: confirm the archive and intended phase
before submitting it.

### Post-deployment verification

Do not treat a successful upload response as proof that the process stayed
healthy. The client does not parse or guarantee an upload-response schema;
record any returned revision or deployment identifier without logging the API
key, then inspect status, identity/health, and recent logs:

```bash
curl --fail-with-body --silent --show-error \
  "$JEOPARDY_BASE_URL/api/agent/status" \
  -H "X-Api-Key: $TEAM_API_KEY"

curl --fail-with-body --silent --show-error \
  "$JEOPARDY_BASE_URL/api/me" \
  -H "X-Api-Key: $TEAM_API_KEY"

curl --fail-with-body --silent --show-error \
  "$JEOPARDY_BASE_URL/api/agent/logs?tail=200" \
  -H "X-Api-Key: $TEAM_API_KEY"
```

Confirm that the `/api/me` JSON reports the intended `agent.revision`,
`agent.state="running"`, and `agent.healthy=true`; inspect logs for a startup
line, the expected phase, and no fatal authentication or import error. If the
upload response does not identify a revision, use the event dashboard's
deployment record for the comparison. Then run:

```bash
python round2_acceptance.py --live --phase practice
```

Choose the actual active phase. Remember that live acceptance proves recent
log metrics and reported health, not that the hosted bytes equal local
`agent.zip`.

### Stop and start

These calls change the hosted process and should be used only after confirming
the intended incident or maintenance action:

```bash
curl --fail-with-body --silent --show-error \
  -X POST "$JEOPARDY_BASE_URL/api/agent/stop" \
  -H "X-Api-Key: $TEAM_API_KEY"

curl --fail-with-body --silent --show-error \
  -X POST "$JEOPARDY_BASE_URL/api/agent/start" \
  -H "X-Api-Key: $TEAM_API_KEY"
```

### Roll back

The client has no built-in deployment-history or rollback command. Before each
upload, retain the last-known-good archive, its matching source commit, and its
reported hosted revision in private operator storage.

To roll back:

1. Validate the saved archive against its matching source checkout.
2. Confirm that replacing and restarting the current hosted process is the
   intended action.
3. Re-upload the saved archive with the same deployment command.
4. Verify the new reported revision, state, health, and startup logs.
5. Re-run live acceptance for the active phase.

Useful hosted endpoints are documented in [Event API reference](event-api.md).

## Recovery playbook

| Symptom | Likely cause | Safe first action |
|---|---|---|
| `python3.12: command not found` | Python 3.12 is not installed | Use the persistent `uv venv --python 3.12` setup from the README |
| `uv` cache permission error | Default cache is not writable | Set `UV_CACHE_DIR` to the documented task-specific temporary path |
| Local `ModuleNotFoundError` | Hosted-image packages were not installed locally | Install the seven local packages into the active `.venv` |
| Hosted `ModuleNotFoundError` | A local module is absent from the ZIP or a third-party dependency is absent from `requirements.txt` | Rebuild from `RUNTIME_FILES` and rerun archive parity |
| `RUN_ONCE` appears idle | Retry, family backoff, or deferred recovery is waiting | For diagnosis use `MAX_ATTEMPTS=1 RECOVERY_ATTEMPTS=0` and inspect retry logs |
| Acceptance exits `2` | Log, credential, HTTP, JSON, or archive setup is invalid | Fix the input contract before interpreting performance gates |
| Submission result `forbidden` | The client interprets scored submissions as hosted-agent-only | Keep local runs in `DRY_RUN`; deploy only after confirming the archive and phase |

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
