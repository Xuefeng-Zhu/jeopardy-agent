# Contributing

This is a private competition-agent repository. Before sharing code or opening
the repository, confirm that the event organizer permits redistribution of the
starter-kit portions.

## Development setup

Use Python 3.12 to match the hosted runtime:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install anthropic httpx requests beautifulsoup4 lxml numpy pandas
```

No event credential is required for the offline test suite.

## Before changing code

1. Fetch the latest remote state.
2. Keep `.env` and task materials out of Git.
3. Identify the invariant being changed and the failure shape it prevents.
4. Prefer a focused change with a regression test.

## Required verification

Run:

```bash
python -m unittest discover -s tests -v
git diff --check
```

When the hosted runtime files change, rebuild `agent.zip` and run archive
parity through `round2_acceptance.py` with a representative saved log.

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
- an independent verification result for scored high-value candidates when
  applicable.

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
- same-host network restrictions;
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
