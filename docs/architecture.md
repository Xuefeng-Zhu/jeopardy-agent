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

| Strategy | Eligibility | Success evidence | Miss or failure behavior |
|---|---|---|---|
| Recipe replay | Exact versioned generator fingerprint and answer format; practice or an in-memory prior replay validation for scored phases | Python exits zero and the exact extraction repeats identically; answer and rerun evidence use separate artifacts | Discard the fingerprint recipe and continue |
| Deterministic fast path | Enabled handler matches title, category, format, prompt markers, files, and handler-specific invariants | Answer artifact plus a separate handler-verification artifact | `not_applicable` or `inconclusive` falls through to Claude; it is terminal under `FAST_PATH_ONLY=1` |
| Claude tool loop | Neither replay nor an enabled fast path solved, and `FAST_PATH_ONLY=0` | `record_answer` supplies evidence and either an artifact extraction or a direct model value | Retry, deferred recovery, or terminal failure according to the configured attempt budget |

The fast-path flags apply after recipe replay: `FAST_PATHS` controls step 2,
while `FAST_PATH_ONLY` enables step 2 and prevents step 3. Neither flag skips
or forces step 1.

See [Deterministic solvers](deterministic-solvers.md) for the current handler
inventory and recognition contract.

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
| `http_request` | Make requests whose initial URL is on the event host, with optional named cookie sessions |
| `record_answer` | Resolve and record the only valid model-produced candidate |

Tool exposure is narrowed before each model call:

| Tool set | Categories |
|---|---|
| `list_files`, `inspect_file`, `read_file`, `search_files`, `search_artifact`, `run_python`, `record_answer` | All categories |
| `http_request` | The Dark Web only |
| `write_file` | Ship It and Heavy Compute |
| `extract_archive` | Ship It, Cryptic, or any task whose original files have a supported archive suffix |

Normal model-turn budgets are `min(MAX_TURNS, 8)` for 100–300-point tiles,
`min(MAX_TURNS, 10)` for 400-point tiles, and `MAX_TURNS` for 500-point tiles.
At most one additional finalization turn is available, and only after a failed
`record_answer` attempt. The finalizer sees a bounded artifact snapshot and can
call only `record_answer`.

`record_answer` accepts either a direct model value or an artifact extraction,
never both. Artifact extraction preserves the matched value without model
retyping, then strips leading and trailing whitespace before storing the
candidate. Only answers extracted from a `run_python` stdout artifact can carry
a replay recipe.

If a normal turn attempts but fails to record an answer, one restricted
finalization turn may call `record_answer` using already-produced evidence.

### 5. Candidate verification

`CandidateVerifier` checks candidates before enqueueing them. It rejects:

- empty or oversized answers;
- non-finite or below-threshold confidence;
- missing evidence;
- malformed numeric or literal formats;
- unknown answer formats;
- validator answers below `max(MIN_CONFIDENCE, 0.92)`;
- directly model-retyped exact, case-insensitive exact, or validator answers
  below `0.95`, and directly model-retyped numeric/literal answers below
  `0.90`; and
- every non-practice answer worth at least 400 points without a separate verification
  artifact, including deterministic and replayed candidates.

The verifier identifies practice candidates by the `PR-` task-ID prefix. It
mechanically enforces a non-empty high-value verification source different from
the answer source. Normal runtime paths supply artifacts, but
`CandidateVerifier` itself does not validate artifact-ID shape or existence and
cannot prove independent derivation. The solver prompt and deterministic
handlers are responsible for making that separate source contain a genuinely
independent check.

Verification is structural. The hidden answer is known only to the event
server, so this layer reduces predictable mistakes but cannot prove semantic
correctness.

### 6. Recipe gating

`RecipeGate` tracks successes, failures, generations, cooldowns, and phase
validation by recipe family. Sibling candidates are held behind a canary until
the family is trusted for the current phase. An incorrect canary increments
the generation and invalidates queued siblings from the old generation.

Recipe caches are in memory. They do not survive a process restart.

Recipe replay and recipe gating serve different scopes:

| Mechanism | Key | Purpose |
|---|---|---|
| Replay cache | Versioned structural generator fingerprint | Reuse one successful Python extraction only on structurally compatible sibling variants |
| Recipe gate | `category:points` family plus generation | Hold scored sibling submissions behind a canary and invalidate a family generation after an incorrect result |

A newly stored Python recipe is not immediately eligible in a scored phase. A
practice replay of that exact generator fingerprint must first submit
successfully during the same process lifetime. The gate considers a
`category:points` family trusted after at least two correct submissions and no
incorrect submissions. An untrusted family admits one canary in a scored phase;
a correct canary opens that family for the rest of that phase.

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
