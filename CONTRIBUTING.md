# Contributing

This is a private competition-agent repository. Before sharing code or opening
the repository, confirm that the event organizer permits redistribution of the
starter-kit portions.

## Development setup

Use Python 3.12 to match the hosted runtime:

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python \
  anthropic httpx requests beautifulsoup4 lxml numpy pandas
source .venv/bin/activate
python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
```

If Python 3.12 is already installed, `python3.12 -m venv .venv` followed by
`python -m pip install ...` is equivalent. `requirements.txt` contains only
additional hosted dependencies; it is not the full local development set. No
event credential is required for the offline test suite, but the dependencies
are still required.

## Before changing code

1. Fast-forward a clean local `main` and create a focused branch:

   ```bash
   git fetch origin --prune
   git switch main
   git merge --ff-only origin/main
   git switch -c codex/<topic>
   ```

2. Keep `.env` and task materials out of Git.
3. Identify the invariant being changed and the failure shape it prevents.
4. Prefer a focused change with a regression test.

If `main` has local commits or edits, do not force the fast-forward or discard
them; resolve ownership and branch state first.

## Required verification

Run:

```bash
python -m unittest discover -s tests -v
python -m compileall -q \
  main.py jeopardy.py agent_types.py verification.py \
  tool_runtime.py solver.py orchestrator.py fast_paths.py \
  round2_acceptance.py tests
git diff --check
```

When hosted runtime files change, rebuild `agent.zip` and run the archive-only
parity command in [Operations](docs/operations.md#archive-parity-only). When
scheduler, log, or acceptance behavior changes, also evaluate a representative
timestamped run log.

Useful focused commands include:

```bash
# Acceptance parser and parity behavior
python -m unittest discover -s tests \
  -p 'test_round2_acceptance.py' -v

# One named controller behavior before the full suite
python -m unittest discover -s tests \
  -p 'test_agent.py' -k 'run_once' -v
```

| Changed area | Minimum evidence before review |
|---|---|
| Scheduler or controller | Focused controller regression, then full suite |
| Deterministic handler | Positive, near-match, malformed/ambiguous, cancellation, and repeatability coverage |
| Tool runtime | Containment/resource/security regression, then full suite |
| Runtime module or import | Rebuild `agent.zip` and run byte-level archive parity |
| Environment variable | Update `.env.example`, the configuration table, and interaction notes |
| Acceptance parser or threshold | Acceptance tests plus a representative timestamped log |
| Event behavior | Offline regression evidence and separately labeled live evidence |
| Documentation only | Resolve local Markdown links and run `git diff --check` |

## Test organization

| File | Coverage |
|---|---|
| `tests/test_agent.py` | Controller, scheduler, cancellation, solver, tools, verification, submission, and recipe gates |
| `tests/test_fast_paths.py` | Handler fixtures, exact-match integration, malformed inputs, safety, and cancellation |
| `tests/test_jeopardy.py` | Transport cancellation, atomic downloads, caching, and encoded responses |
| `tests/test_round2_acceptance.py` | Run-metric and archive acceptance behavior |

## Deterministic handler checklist

A new or changed fast path should test:

- an exact supported generator shape;
- a structurally similar near-match that must fall through;
- malformed, missing, duplicated, or ambiguous inputs;
- answer-format behavior;
- cancellation during expensive loops or subprocess work;
- deterministic repeatability; and
- a separate verification artifact derived from an independent check for
  scored high-value candidates when applicable. The runtime enforces distinct
  artifact IDs, so the test must establish the stronger semantic independence.

Do not broaden a handler merely to make one fixture pass. A false positive can
submit an incorrect answer; an inconclusive result safely falls back to the
model.

## Tool-runtime checklist

Changes to model tools must preserve:

- task-root path containment;
- output and artifact caps;
- cancellation checks;
- safe archive handling;
- credential scrubbing for subprocesses;
- initial-host URL restrictions and the known redirect-boundary limitation;
- bounded regex behavior; and
- structured error responses instead of unhandled exceptions.

Remember that `run_python` is not a full OS sandbox. Tests should run in a
disposable environment without sensitive files.

## Documentation

Update documentation in the same change when you modify:

- environment variables or their bounds;
- archive membership;
- event endpoints or result semantics;
- solver strategy order;
- candidate verification policy;
- acceptance thresholds; or
- security controls and limitations.

Documentation must distinguish offline verification from live hosted proof.

## Commits and pull requests

- Keep commits scoped and use imperative summaries.
- Do not commit real event credentials, generated task data, virtual
  environments, or local caches.
- Explain the failure mode, implemented invariant, and verification evidence.
- Create ready-for-review pull requests by default unless a known blocker makes
  review inappropriate.
