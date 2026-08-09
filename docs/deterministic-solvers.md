# Deterministic solver catalog

The runtime contains deterministic handlers for task generators whose complete
shape is known. These handlers avoid a model call, produce reproducible
evidence, and fail inconclusively when an expected invariant is missing.

This is an implementation inventory, not a promise that a title alone is
supported. The dispatcher currently contains 31 handlers for 30 distinct task
titles. `Congruential Cascade` has separate numeric and validator handlers.

## Dispatch contract

Before dispatch, `TaskFingerprint` records a versioned structural identity from:

- normalized title, category, and answer format;
- a normalized prompt template and required prompt markers;
- file paths, suffixes, detected types, magic bytes, columns, and JSON keys;
- archive member paths and suffixes; and
- inspection error types.

Instance values are intentionally excluded where possible so a verified recipe
can be reused across sibling variants, while structural differences produce a
different fingerprint. Fingerprinting inspects at most 160 task files.

A handler then requires an exact normalized title and, where declared, the
expected category, answer format, prompt phrases, file manifest, schema, archive
layout, and value constraints. Recognition is only the first gate: every
handler performs additional checks while solving.

The dispatcher returns one of three outcomes:

| Outcome | Meaning | Next step |
|---|---|---|
| `solved` | All handler invariants passed and a non-empty answer was produced | Store the answer and handler-verification text as separate artifacts, then run `CandidateVerifier` |
| `not_applicable` | No registered handler matched the fingerprint | Continue to the Claude tool loop unless `FAST_PATH_ONLY=1` |
| `inconclusive` | A matching handler hit a malformed, ambiguous, unsafe, timed-out, or disabled shape | Continue to the Claude tool loop unless `FAST_PATH_ONLY=1` |

Cancellation is not converted into an inconclusive result. It propagates to the
worker so a claimed or closed tile stops promptly.

An unchanged deterministic miss is cached across retries until the original
downloaded-file signature changes. If those file sizes or modification times
change, the fingerprint and fast-path result are recomputed. An incorrect
submitted fast-path candidate disables that handler for the next recipe
generation and invalidates queued siblings derived from the older generation.

## Handler inventory

### Needle in the Haystack

| Title | Required material | Format | Deterministic method |
|---|---|---|---|
| `Dirty Ledger` | `transactions.csv` with `txn_id`, `status`, and `amount` | `numeric` | Filter the exact requested status, parse money independently as `Decimal` and integer cents, and require both totals to agree |
| `Event Horizon` | `events.jsonl` with `user`, `type`, and `ts` | `exact_ci` | Parse the inclusive UTC date range, count the requested event type through two equivalent aggregations, and require a unique winner |
| `Join the Dots` | `users.csv` and `orders.jsonl` with the expected join keys | `exact_ci` | Keep the first occurrence of each order, exclude refunds, join users, recompute totals, and require a unique maximum |
| `Session Hunter` | `server.log` | `exact_ci` | Sort unsorted user events, rebuild sessions from the prompt-defined gap, count session days, and reject a tied requested rank |
| `Thirty Systems` | `regional_sales.zip` containing `fx.csv` and 16 named exports | `numeric` | Validate the archive, preserve the global first transaction occurrence, convert with exact decimal FX rates, recompute, then round once |

### Cryptic

| Title | Required material | Format | Deterministic method |
|---|---|---|---|
| `Layer Cake` | One prompt-embedded blob | `exact` | Apply strict base64 or zlib decoding for at most 20 layers and accept one correctly shaped token |
| `Locked Box` | `bundle.zip` | `exact` | Validate nested ZIPs and CRCs, derive the prompt-specified XOR key, decrypt in memory, and require one vault token |
| `Hidden Pixels` | `image.png` | `exact` | Validate the PNG signature and chunk CRCs, then extract one token from metadata rather than pixel data |
| `Binary Ledger` | `data.bin` and `spec.md` | `exact` | Validate the binary format against its specification, decode every record, and require the specified maximum to be unique |
| `Crib Sheet` | `cipher.bin` | `exact` | Recover the repeating XOR key from the required known plaintext, verify the decrypted structure, and extract one token |

### Ancient Scrolls

| Title | Required material | Format | Deterministic method |
|---|---|---|---|
| `Buried Clause` | `policy_manual.txt` | `numeric` | Isolate the requested numbered policy section and parse its single-claim reimbursement limit |
| `Two Hops` | `contract.txt` | `numeric` | Follow the requested clause to a tier rate in one appendix and an adjustment multiplier in another clause, then recompute their product |
| `Minute Details` | Exactly 12 files named `notes_<year>-01.md` through `notes_<year>-12.md` | `exact` | Search only the requested month's dated meetings and require one ticket assigned to the named person |
| `Living Document` | `regulation.txt` | `numeric` | Parse dated amendments, resolve revocations, and apply only the effective non-revoked value |
| `Trust the Errata` | `errata.txt` and `report_{alpha,bravo,charlie,delta}.txt` | `numeric` | Resolve each requested section to its unique latest-dated authoritative report and recompute `(A-B)*C` |

### Ship It

| Title | Required material | Format | Deterministic method |
|---|---|---|---|
| `Spaghetti` | `program.py` | `exact` | Execute the bounded program twice and require identical, correctly shaped output |
| `Spec vs Code` | `spec.md` and `lib.py` | `literal` | Validate the documented contract, parse the prompt's supported invocation, and recompute its exact Python representation through two equivalent methods |
| `Tiny VM` | `vm.py` and `program.bin` | `exact` | Validate the interpreter/program contract, run the bytecode twice, and require identical output |
| `Fix and Run` | The prompt-referenced `project.tar.gz` | `exact` | Validate the fixed project manifest and documented contract, parse literal self-test cases, and recompute every expected digest without executing the supplied project |
| `Quintuple Fault` | The prompt-referenced `project.tar.gz` | `exact` | Validate the seven-contract manifest, parse literal self-test cases, and recompute every expected digest without executing the supplied project |

### Heavy Compute

| Title | Required material | Format | Deterministic method |
|---|---|---|---|
| `Congruential Cascade` | Prompt parameters | `numeric` | Generate the bounded linear-congruential sequence, aggregate qualifying terms, and cross-check the final state logarithmically |
| `Congruential Cascade` | Prompt rubric | `validator` | Validate the observed impossibility rubric and return the work-factor verdict only when the SHA-256 search is demonstrably event-infeasible |
| `Squarefree Census` | Prompt range and divisor | `numeric` | Use a segmented squarefree sieve and verify spot values by trial division |
| `Proof of Work` | Prompt prefix, target, and length bounds | `validator` | Search deterministic base-36 nonces within a fixed bound and independently recompute the winning SHA-256 digest |
| `Needle in 2^44` | `numbers.txt` | `validator` | Solve the fixed-cardinality subset sum with meet-in-the-middle and independently verify cardinality, order, and exact sum |
| `Traveling Salesman` | `points.txt` containing indices `0..89` | `validator` | Run deterministic multi-start construction and iterated 2-opt, then verify the route permutation and recompute its closed-tour length |

The scheduler treats `Congruential Cascade`, `Squarefree Census`, `Proof of
Work`, `Needle in 2^44`, and `Traveling Salesman` as CPU-heavy titles. At most
`CPU_HEAVY_WORKERS` of those titles run concurrently.

### The Dark Web

| Title | Required material | Format | Deterministic method |
|---|---|---|---|
| `Deep Pages` | One event-host start URL | `exact` or `exact_ci` | Follow exactly one labeled `next` link per page in one session, reject cycles or multiple next links, and stop after 48 pages |
| `The Gatekeeper` | One event-host start URL | `exact` | Preserve one session across the challenge GET and answer POST, parse the stated modular formula, and accept one final token |
| `Initiation` | One event-host start URL | `exact` | Follow the registration, ordered guest-book fragments, verification POST, and header-authenticated room walk in one session |
| `Members Only` | Prompt credentials and one login URL | `exact` or `exact_ci` | Parse one password form, retain hidden fields and cookies, crawl at most 12 authenticated pages, and require one vault token |
| `Breadcrumbs` | Event-host start URL(s) | `exact` or `exact_ci` | Traverse a bounded same-session graph of body and response-header links and require one explicit token |

Deterministic web handlers validate their initial and explicitly discovered
targets against the configured event hostname. The underlying HTTP client can
follow redirects, and the final redirect target is not currently revalidated.
These handlers perform live network calls and may mutate task-specific server
state when a task's prescribed flow uses forms or other non-GET methods.
`DRY_RUN=1` prevents only the final `/api/submit` call.

## Calibration and failure behavior

Use a fresh no-model CLI run to measure deterministic-handler coverage without
model calls or answer submission:

```bash
RUN_ONCE=1 MAX_TILES=10 FAST_PATH_ONLY=1 DRY_RUN=1 python main.py
```

This is still a live event run: it fetches the board, task metadata, files, and
event-host web pages. Run it only in the hosted container or another disposable
least-privilege environment.

Eligible, compatible recipe replay runs before deterministic dispatch
regardless of `FAST_PATHS` or `FAST_PATH_ONLY`. A fresh CLI process starts with
an empty in-memory cache, and this no-model profile cannot create new replay
recipes. A caller that reuses a populated solver/cache can still replay when
the phase gate permits it. To measure a specific handler without replay, start
a fresh process with one `TASK_FILTER` target or invoke its targeted offline
tests directly.

With `FAST_PATH_ONLY=1`, a handler `not_applicable` or `inconclusive` result
after replay is terminal for that tile; it does not consume normal retries or
deferred recovery. With the default `FAST_PATH_ONLY=0`, the same result falls
through to the bounded Claude tool loop.

When adding or broadening a handler, follow the regression requirements in
[Contributing](../CONTRIBUTING.md#deterministic-handler-checklist). A positive
fixture alone is insufficient: near-matches, ambiguity, malformed inputs,
cancellation, and deterministic repeatability are part of the contract.
