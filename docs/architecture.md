# Architecture

This document describes the runtime implemented by the current source. The
entrypoint is `main.py`; all long-running behavior is owned by
`AgentController` in `orchestrator.py`.

## Design goals

1. Cover the whole playable board instead of treating each displayed cell as
   one task.
2. Minimize time spent on tiles that another team has already claimed.
3. Prefer deterministic, reproducible computation over model reasoning when a
   generator is recognized exactly.
4. Keep exact answers in artifacts and code paths instead of round-tripping
   them through model text.
5. Fail closed before a costly submission.
6. Bound external I/O, context size, subprocess work, retries, and concurrency.

## Runtime flow

### 1. Board synchronization

`jeopardy.board()` fetches the current board. Authentication is considered
invalid when the response lacks the account-specific `you` block, even if the
public board payload otherwise looks valid.

`jeopardy.open_tiles()`:

- selects the board associated with the current phase;
- expands every cell's `open_ids` into individual tile records;
- removes locked and already-solved tiles; and
- orders the initial list by points while preserving server order for ties.

The controller polls throughout execution. When an open tile disappears, its
`CancellationToken` closes active HTTP/model streams and stops cooperative
tool work at the next cancellation check.

### 2. Scheduling

`AgentController` owns one `TileRecord` per observed tile. Eligible records are
new or retryable, still open, within their attempt budget, and not delayed by a
retry or family backoff.

The scheduler greedily selects a wave using:

- tile points and expected solve time;
- estimated recipe success;
- claim pressure from the cell's remaining depth;
- coverage of categories and recipe families not already active;
- deterministic or replay readiness;
- a cap on CPU-heavy deterministic handlers; and
- available model-worker and candidate-buffer capacity.

This is a race scheduler, not a FIFO work queue. Selection is recalculated as
workers finish, candidates queue, claims change, or backoffs expire.

### 3. Task preparation

`TileSolver` fetches task metadata and materials once into a stable
task-specific temporary directory. Retries reuse the directory, tool runtime,
downloaded files, and fingerprint until the file signature changes.

The task fingerprint covers structural prompt, title/category, answer format,
file names and types, schemas, archive layout, and selected magic bytes. It is
used to scope deterministic handlers and replay recipes to the generator shape
they were validated against.

### 4. Solve strategies

The solver attempts strategies in this order:

1. **Recipe replay.** A compatible Python recipe may be replayed for the exact
   generator fingerprint. It must produce the same extracted output twice.
   Scored phases require prior practice validation of that fingerprint.
2. **Deterministic fast path.** `FastPathDispatcher` runs the registered
   handler only when its full invariants match. A malformed or ambiguous input
   is inconclusive and falls through rather than guessing.
3. **Claude tool loop.** The event model receives a category/title playbook and
   only the tools relevant to the task. Turn and output budgets scale within
   configured bounds. Text-only replies are not candidates.

The model tools are:

| Tool | Purpose |
|---|---|
| `list_files` | List task files and sizes |
| `inspect_file` | Return bounded schema, shape, sample, or binary metadata |
| `read_file` | Read a bounded byte window and retain it as an artifact |
| `search_files` | Search workspace text with bounded matches |
| `search_artifact` | Search stored stdout, HTTP bodies, or file windows |
| `write_file` | Create a task-local helper or patch |
| `extract_archive` | Extract supported archives with traversal and size checks |
| `run_python` | Execute bounded Python and store full output as artifacts |
| `http_request` | Make same-host requests with optional named cookie sessions |
| `record_answer` | Resolve and record the only valid model-produced candidate |

If a normal turn attempts but fails to record an answer, one restricted
finalization turn may call `record_answer` using already-produced evidence.

### 5. Candidate verification

`CandidateVerifier` checks candidates before enqueueing them. It rejects:

- empty or oversized answers;
- non-finite or below-threshold confidence;
- missing evidence;
- malformed numeric or literal formats;
- invalid exact-match shapes; and
- unreplayed scored 400/500-point answers without a separate verification
  artifact.

Verification is structural. The hidden answer is known only to the event
server, so this layer reduces predictable mistakes but cannot prove semantic
correctness.

### 6. Recipe gating

`RecipeGate` tracks successes, failures, generations, cooldowns, and phase
validation by recipe family. Sibling candidates are held behind a canary until
the family is trusted for the current phase. An incorrect canary increments
the generation and invalidates queued siblings from the old generation.

Recipe caches are in memory. They do not survive a process restart.

### 7. Submission

All candidates pass through one `SubmissionManager`. It:

- deduplicates task IDs;
- ranks aging candidates against expected value;
- rechecks tile openness and recipe generation before and after waiting;
- enforces a minimum interval between submissions;
- retains candidates across rate limits, lockouts, and phase transitions; and
- reports correct, incorrect, claimed, invalidated, and neutral outcomes back
  to the controller.

The model has no submission tool. `jeopardy.submit()` is reachable only from
this controller-owned lane.

## State model

Each tile moves through `TileStatus` values:

```text
NEW -> SOLVING -> CANDIDATE -> SOLVED
            |          |
            |          +------> CLAIMED / RETRY
            +-----------------> RETRY / FAILED / CLAIMED
```

The exact transition depends on solver exhaustion, verification, board
updates, server outcomes, configured attempt budgets, and recovery eligibility.

## Concurrency model

- One asyncio controller loop polls the board and schedules work.
- Tile solving runs in worker tasks; blocking network/model/tool calls are
  moved to threads where needed.
- A thread-safe cancellation token bridges asyncio, blocking HTTP calls,
  subprocesses, and model streams.
- One submission coroutine serializes all answers.
- Shared logs use a lock so one event remains one line.

## Data and persistence

| Data | Lifetime |
|---|---|
| Board state and tile records | Process lifetime |
| Replay cache and recipe trust | Process lifetime |
| Task files and generated helpers | Stable temp directory for the container lifetime |
| Tool artifacts | Task runtime lifetime, memory-resident |
| Hosted logs | Event service retention policy |

No application database is used.

## Extension points

- Add a deterministic handler by implementing the fast-path handler contract,
  registering it with `FastPathDispatcher`, and adding positive, near-match,
  malformed-input, and cancellation tests.
- Add a tool by defining its schema, implementing it in `TaskToolRuntime`, and
  deciding which categories may receive it.
- Change scheduling through `_race_priority` and `_select_records`; preserve
  coverage, claim-pressure, candidate-buffer, and CPU-heavy constraints.
- Change candidate policy in `CandidateVerifier`; add tests for every new
  rejection and acceptance boundary.
