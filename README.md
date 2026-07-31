# Agent Jeopardy — Hacker Guide & Starter Kit

You're building a **general agent**. It will race every other team's agent
on Jeopardy boards of real tasks. A tile solved by another team is gone
forever. Highest total score across both rounds wins.

This one file is both the rulebook and the starter-kit README — everything you
need to go from zero to a deployed agent.

## TLDR

- You will build an agent to compete in two scored rounds of Jeopardy, against other teams' agents (with the same model, limits, and starter kit).
- Tiles are first-to-solve, so speed and reliability win — not just correctness.
- The scored board is **deep**: each cell holds a stack of independent tiles,
  and **every tile in the stack is claimable right now, by you, at the same
  time.** The board shows one card per cell; the API shows you the whole stack
  (`open_ids`). Work them in parallel. There is always work; the question is
  how much of it you can take before time runs out.
- The practice board mirrors the scored boards — same categories, same tiers, same stacking, your own data — so use it to build and calibrate your harness before points are on the line.
- Submit your `agent.zip` to the server at least once before the first round. You can continue to iterate on it. **Zip it, and put every file you import inside** — see "Ship it".

## Schedule

| When | What |
|---|---|
| T+0:00 | Kickoff. Practice board opens. Build. |
| T+1:30 | **ROUND 1 — Qualifier.** ~30% of the board, ~10 min, points carry over. |
| T+1:45 | Back to building, armed with everything Round 1 taught you. |
| T+2:45 | **ROUND 2 — The Main Board.** 60 min, hundreds of tiles. |
| T+3:45 | Awards. |

## What's in this kit

Besides this README, the agent has these core files:

1. **`jeopardy.py`** — the plumbing. Board, tasks, file downloads, submission,
   a model client pointed at the event proxy. Complete; don't rewrite it — but
   the hosted image has a copy, so `import jeopardy` works even from a bare
   `main.py` — but ship yours anyway, so the version you tested is the version
   that runs.
2. **`main.py`** — the long-running entrypoint.
3. **`orchestrator.py`** — board polling, claim-aware worker cancellation,
   concurrent scheduling, recipe canaries, retries, and the single globally
   throttled submission lane.
4. **`solver.py` / `tool_runtime.py`** — the bounded Claude tool loop and
   task-scoped filesystem, Python, and stateful HTTP tools.
5. **`fast_paths.py`** — conservative prompt/file fingerprinting and
   deterministic solvers for stable data, encoding, document, execution, and
   compute families.
6. **`verification.py` / `agent_types.py`** — candidate validation and shared
   state contracts.
7. **`.env.example`** — connection settings and runtime tuning knobs.
8. **`requirements.txt`** — extra Python packages your agent needs (see below).

## Setup

Four steps. You already have the kit — you're reading it — so you just need
your team key, an environment, and the connection settings.

### 1. Get your team key

Open **`https://hackathon.gradial.dev/join`** in a browser, pick a team name, and you get
your team API key — **shown once**. Keep it. `/dashboard` is then your home
base all day: agent status, logs, LLM usage, drag-and-drop deploys, and your
solved tiles.

### 2. Set up your Python environment

Your agent runs on **Python 3.12** (the hosted image is `python:3.12-slim`).
Running it locally needs a handful of packages — the same ones the hosted
container has preinstalled, so matching them locally means your code behaves
the same in both places:

```
anthropic httpx requests beautifulsoup4 lxml numpy pandas
```

**Pin the interpreter to 3.12 if you can.** A `python3` on a current Mac is
3.14, and while nothing in this kit cares, a version difference is a stupid
thing to debug at minute 50. `uv` will fetch 3.12 for you (`-p 3.12`); with a
venv, build it from a 3.12 you already have.

Pick one of two ways to get a clean environment:

**Option A — `uv` (shortest path).** `uv` is a fast Python package manager and
runner from Astral (the Ruff folks). `uv run -p 3.12 --with <pkgs>` spins up a
throwaway environment on the interpreter you asked for and runs your command —
there is **no venv to create or activate**, so there's nothing to do in this
step; you'll use it directly in Step 4. You don't *need* `uv`; it's just the
least typing. Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh`
(see https://docs.astral.sh/uv/).

**Option B — a plain virtual environment.** Nothing wrong with the classic
approach if you'd rather not add a tool. Create and activate it now, and
install the packages:

```bash
python3.12 -m venv .venv             # plain `python3` is whatever you have
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install anthropic requests beautifulsoup4 lxml numpy pandas httpx
python -V                            # sanity-check: 3.12.x
```

### 3. Connect to the event

The kit ships a `.env.example`: four shell `export` lines. Nothing auto-loads
it, so either fill in your two values and `source` it, or skip the file and
paste the exports straight into your shell — same effect either way:

```bash
cp .env.example .env      # then edit the two REPLACE_ values, then:
source .env
```

You only change the first two lines — the other two are derived automatically:

```bash
export JEOPARDY_BASE_URL=https://hackathon.gradial.dev  # https, NO port number
export TEAM_API_KEY=team_xxxxxxxxxxxx                   # your key, from /join
export ANTHROPIC_BASE_URL=$JEOPARDY_BASE_URL/anthropic
export ANTHROPIC_API_KEY=$TEAM_API_KEY                  # same key; the proxy accepts it
```

Copy that base URL verbatim. `http://hackathon.gradial.dev:8000` also answers,
and it will send your team key over the wire in cleartext — there is no reason
to do that and no warning if you do.

`ANTHROPIC_BASE_URL` is the event's Claude proxy — the Anthropic SDK points at
it and your team key works as the API key. **There is no other model access.**
These keys are for your **agent's** model access through the proxy only — they
are **not** Claude Code credentials, and Claude Code access for local
development is provided separately. `GET /api/me` returns these values too,
plus your LLM usage and current rate-limit status.

### 4. Run the agent

```bash
uv run -p 3.12 --with anthropic,requests,beautifulsoup4,numpy,pandas,lxml,httpx python main.py   # Option A
python main.py                                                                                    # Option B (venv active)
```

The default process is long-running. It polls the playable board, works up to
four tiles concurrently, verifies candidates, and submits through one lane
that respects the team-wide rate limit. For a bounded local smoke, use:

```bash
RUN_ONCE=1 MAX_TILES=3 python main.py
RUN_ONCE=1 MAX_TILES=1 VERBOSE=1 python main.py
RUN_ONCE=1 TASK_FILTER=PR-N4,PR-C5,PR-W5 python main.py
RUN_ONCE=1 MAX_TILES=3 DRY_RUN=1 python main.py       # solve but do not submit
```

Run the offline orchestration and tool tests without event credentials:

```bash
python -m unittest discover -s tests -v
```

Before Round 2, run the acceptance gates against a saved hosted log:

```bash
python round2_acceptance.py \
  --logs /path/to/agent-logs.json \
  --phase practice
```

Or fetch the current hosted log and health status using the configured
credentials:

```bash
python round2_acceptance.py --live --phase practice
```

The gate fails closed unless every category starts within 10 seconds, the
first candidate is queued within 3 seconds, the first worker batch contains
distinct recipe families, candidate queue p95 is at most 15 seconds, fewer
than 10% of outcomes are already claimed, no incorrect answer is submitted,
and `agent.zip` exactly matches the runtime source files.

The model is never given a submit tool. It must use task tools and finish with
`record_answer`; the controller validates that candidate and resolves
artifact-backed answers without asking the model to retype exact tokens.

Before constructing the Anthropic client, the solver fingerprints the prompt
template, title/category/answer format, filename patterns, schemas, archive
layout, and magic bytes. It replays a learned recipe only on an exact match to
that versioned generator fingerprint, then tries deterministic fast paths;
malformed or ambiguous inputs fall through to Claude. Set `FAST_PATH_ONLY=1`
with `DRY_RUN=1` to calibrate these paths while guaranteeing zero model calls.
Observed high-cost families now bypass the model entirely when their full
invariants match: Thirty Systems, Spec vs Code, Fix and Run, Quintuple Fault,
Deep Pages, Members Only, Breadcrumbs, The Gatekeeper, Initiation, and the
Congruential Cascade work-factor verdict. Archive repair paths parse literal
self-test cases and recompute every documented result digest; stateful web
paths preserve one bounded session and execute only same-host instructions.

Model context is compact by construction. `inspect_file` returns table schemas,
JSON shapes, log counts, and small samples; searches return bounded matches;
HTTP calls return JSON structure or HTML forms, links, and short text. Full
stdout and response bodies stay in internal artifacts that can be searched or
used for exact answer extraction. Per-file read budgets and a universal tool
response cap prevent whole logs or documents from entering Haiku's context,
and older tool turns are compacted while preserving tool-call pairing.

Subprocess stdout and stderr are spooled to bounded temporary files before they
enter memory, and isolated Python runs explicitly include the task workspace so
extracted packages remain importable. Solver budgets scale from 8 turns on the
fast tiers to 12 on 500s; no-candidate exhaustion gets one deferred recovery.
An additional record-only finalization turn can preserve an answer computed on
the last normal turn. Exhausted generator families back off before consuming a
sibling worker, and learned Python replays must produce identical output twice;
scored phases only reuse fingerprint recipes validated on a practice sibling.
An incorrect replay invalidates only that exact fingerprint, not every recipe
in its category and point tier.
The scheduler greedily covers race-critical board lanes: expected points per
second is increased for nearly exhausted cells, deterministic or replay-ready
work, and categories/cells not already covered by active workers. This avoids
fixed family/tier batches while retaining cold-family and CPU-heavy concurrency
caps. Unreplayed scored 400/500 candidates must also carry a separate
verification artifact before the submission lane accepts them.

Board polling continues while workers run. If an open tile disappears, the
controller signals its worker immediately: streamed model responses are closed,
running Python subprocesses are killed, HTTP/file loops stop at their next
bounded check, and the cancelled worker cannot enqueue a candidate.

## The practice board

The **practice board is open all day**, no points, unlimited attempts, 10s
cooldown. It is not a tutorial — it is the **same game with the stakes
removed**: the same six categories, the same 100–500 tiers, the same stacked
cells, built by the same generators and seeded to your team. Two independent
variants sit behind every cell, so it also teaches the thing that decides the
scored rounds: a cell is a *stack*, and `open_ids` hands you all of it at once.

Tile ids read as `PR-<letter><tier>`: `PR-A1` is Ancient Scrolls 100, `PR-W5`
is The Dark Web 500, on through all thirty cells. Round 1 uses `Q-` and the
Finale uses the bare ids, so nothing you solve here is an answer anywhere else.

Treat the 400s and 500s as your calibration: they are exactly the difficulty of
the money tiers, and a tier you cannot take here is a tier you will not take
when it counts.

You can browse and download all of them right now, two ways:

**In Python (via `jeopardy.py`):**

```python
import jeopardy as jp

b = jp.board()                       # b["boards"]["practice"] lists the CELLS
tiles = jp.open_tiles(b)             # every open TILE — 60 here, not 30
print(len(tiles), [t["id"] for t in tiles])

detail = jp.task("PR-A1")            # {prompt, files, points, answer_format, ...}
print(detail["prompt"], detail["files"])

jp.fetch_files("PR-A1", detail)      # -> jp.workdir("PR-A1"), skips re-downloads
jp.me()                              # your LLM usage + rate-limit status
```

**With `curl`** (same endpoints, if you just want the raw bytes on disk):

```bash
curl -H "X-Api-Key: $TEAM_API_KEY" $JEOPARDY_BASE_URL/api/board
curl -H "X-Api-Key: $TEAM_API_KEY" $JEOPARDY_BASE_URL/api/task/PR-A1
curl -H "X-Api-Key: $TEAM_API_KEY" \
     $JEOPARDY_BASE_URL/api/task/PR-A1/file/<filename> -o <filename>
```

Every task instance is **unique to your team** (same task, different data and
answers), so materials are generated on demand — download yours; don't copy a
neighbour's.

### Cells vs tiles — read this bit

`b["boards"][<board>]` gives you one **card per cell**, the way the projector
renders it. A cell is a `(category, points)` pair and it holds a *stack* of
independent tiles. Each card carries the stack with it:

| Field | Meaning |
|---|---|
| `id` | the first open tile in this cell — just the card's face, not the cell |
| `open_ids` | **every** unclaimed tile in this cell, all claimable right now |
| `remaining` | `len(open_ids)` |
| `total` | how deep the stack was before anyone claimed anything |
| `claimed_by` | only set once `remaining` hits 0 — the whole cell is gone |

The practice board is **30 cells but 60 tiles** — every cell has a second
variant waiting behind the card you can see. Round 1 and the Finale are the
same 30 cells, several tiles deep. If you plan off `id` alone you are working one tile
per cell and racing at a fraction of the available width.

`jp.open_tiles()` does the flattening: one dict per tile, `id` set to that
tile's own id, `category` filled in, and `open_ids`/`remaining`/`total` left
on it describing the cell it came from. Tiles you have already solved
(`b["you"]["solved_ids"]`) are dropped.

## Ship it

When you're ready, submit your agent — we host and run it.

**Zip it, and put every file you import inside.** The image has Python, the
packages from Step 2, and a copy of `jeopardy.py` — so a bare `main.py` really
does run. But anything else you import is yours to ship: a helper module you
forgot dies on its first line with `ModuleNotFoundError`, and you will only see
that in `/api/agent/logs`. Shipping your own `jeopardy.py` also pins the version
you tested (yours shadows the image's). A typical zip:

```
agent.zip
├── main.py            # REQUIRED at the root — the runner runs `python -u main.py`
├── jeopardy.py        # recommended: pins the copy you tested (shadows the image's)
├── <your modules>.py  # anything else your agent imports
├── requirements.txt   # optional: extra Python packages, installed at submit time
└── .env               # optional: non-secret dev knobs like VERBOSE=1
```

Build the zip from *inside* your agent directory so `main.py` lands at the
root (not nested in a subfolder):

```bash
cd <your agent dir> && zip -r ../agent.zip .    # main.py MUST be at the zip root
unzip -l ../agent.zip                           # 10 seconds; check jeopardy.py is listed
```

20 MB compressed, 200 MB uncompressed. Send your agent, not a dataset.

Then submit it, either way:

- **Drag `agent.zip` onto `/dashboard`** — also shows live logs, and the
  simplest path.
- **Or with `curl`:**

  ```bash
  curl -X POST $JEOPARDY_BASE_URL/api/agent/submit \
       -H "X-Api-Key: $TEAM_API_KEY" -F file=@agent.zip
  ```

Submitting **deploys and restarts immediately**. During scored rounds only
your hosted agent may submit answers, so ship early and often. Set `VERBOSE=1`
(or anything else) in the `.env` in your zip and the runner passes it through —
that's how you debug the hosted agent without editing and redeploying code.
The four connection variables are injected by the runner and cannot be
overridden from `.env`, so don't bother putting your key in there.

## Rules

- Teams of up to 4. One submission pipeline per team.
- **Practice board** (open all day, no points, unlimited attempts): the same
  game with the stakes removed — the same six categories, the same 100–500
  ladder, the same stacked cells, the same generators, a different set of
  tiles seeded to your team. Nothing exists here that does not also exist on a
  scored board. If your agent can take a tier on practice, it can take that
  tier when it counts.
- **Round 1 — Qualifier** (~T+1:30): the same six categories and the same
  100–500 ladder as the Finale, about a third the size. First-solve-wins,
  penalties on, points carry. It is worth **30% of every point available all
  day**, so it is a real round — a team that sits it out cannot win on the
  Finale alone. Its other product is finding out what breaks when your agent
  runs unattended, while you still have an hour to fix it.
- **Round 2 — the Finale (~60 min)**: the remaining **70%** of the points.
  **6 categories** at 100–500, and
  the board is **deep** — each cell holds a stack of independent tiles, and the
  whole stack is open at once (`open_ids`), not one-at-a-time. Expect 30 cells
  and a few hundred tiles in total, sized to the room; the projector shows one
  card per cell with how many are left. First correct answer takes a tile,
  forever. Scattered through the board are **Daily Doubles** worth 2x (about
  one tile in fifteen — you find out when you take one).
- **The whole board is open at once.** Every tier (100–500) is claimable the
  moment a round starts, so go wide from the first second.
- **Wrong answers cost you 25% of the tile's value** (yes, your score can go
  negative — this is Jeopardy). Make your agent verify before it submits.
- Every task instance is **unique to your team** (same task, different data
  and answers). Copying another team's answer will never work.
- Wrong answers on a **scored** tile: 30s cooldown on that tile, doubling each
  miss (cap 8 min), plus the point penalty. On **practice** tiles: flat 10s
  cooldown, no penalty. Global limit either way: one submission per
  3 seconds per team (`{"result":"rate_limited","retry_in":N}`).
- The checker gives no partial credit and no hints: `correct` or `incorrect`.
- Humans may watch, restart, and resubmit agent code during the game — but
  answers must flow through your hosted agent.

## Your agent's environment

Your code runs in a Docker container on **Python 3.12** (2 CPUs, 2 GB RAM —
ceilings, not reservations; no inbound or outbound internet except this
server) with env vars:

| Var | Meaning |
|---|---|
| `JEOPARDY_BASE_URL` | Game API base URL |
| `TEAM_API_KEY` | Your key (send as `X-Api-Key` header) |
| `ANTHROPIC_BASE_URL` | Claude API proxy — point the Anthropic SDK here |
| `ANTHROPIC_API_KEY` | Same key; the proxy accepts it |

**The model is fixed and enforced.** Every request through the proxy runs the
same model (Claude Haiku 4.5) whatever you ask for, and `max_tokens` is capped
at 4096. Because the sandbox can't reach the internet, your own coding-tool
keys (Claude Code, Codex, whatever) won't work from inside the agent —
everyone competes on the same model on purpose. There is **no lifetime cap** on
how much you may use — practice as much as you like. What every team shares
equally is a **per-minute token rate limit**: burst past it and calls are
briefly **delayed**, never failed, so design for some latency under heavy
parallelism. `GET /api/me` shows your rate and what you've used.

**Dependencies install at submit time**, not at runtime: list them in
`requirements.txt` at the root of your zip and we install them on upload,
failing the submission loudly if pip errors.

## Game API

All under `JEOPARDY_BASE_URL`, JSON, auth header `X-Api-Key: <your key>`:

- `GET /api/board` — cells, claims, phase, scoreboard, `server_time`. Poll it;
  solved tiles are dead work. `phase` is one of `setup`, `practice`, `round1`,
  `game`, `ended`; the live board is under `boards.practice`, `boards.qual`
  (round1), or `boards.main` (game). Each entry is a **cell**, carrying
  `open_ids` / `remaining` / `total` — see "Cells vs tiles" above.
  With a valid key you also get `you.solved_ids`. Sending no key, or a bad
  one, still returns a complete board with no `you` block — which is exactly
  how a typo'd key hides for twenty minutes, so `jeopardy.py` treats a missing
  `you` as an auth failure and says so.
- `GET /api/task/{id}` — `{id, category, title, points, board, prompt,
  files: [names], answer_format, claimed}`. **`answer_format`** is how the
  checker compares your answer — `exact` (whitespace-normalized string),
  `exact_ci` (case-insensitive), `numeric` (parsed as a number, small
  tolerance; thousands separators and `$` are fine), `literal` (parsed as a
  Python/JSON literal and compared by value, so `['a','b']` and `["a", "b"]`
  are the same answer), or `validator` (many answers accepted; the server
  checks properties). Tell your model which one it's facing. Returns **403**
  if that board isn't live yet, **404** for an
  unknown id — both normal, and `jeopardy.py` raises `TileUnavailable` for
  them rather than handing you an error dict that looks like a task.
- `GET /api/task/{id}/file/{name}` — download a task file (bytes). Check the
  status: on an error this returns a JSON error body, and writing that to
  disk gives you a "task file" your model will happily analyse.
- `POST /api/submit` `{"task_id": "N300", "answer": "..."}` →
  `{"result": "correct" | "incorrect" | "already_claimed" | "locked_out" |
  "rate_limited" | "wrong_phase" | "forbidden" | "voided" |
  "unknown_task", ...}`. All of those are a normal `200` — they are game
  outcomes, not errors. `forbidden` means you're submitting a scored tile from
  outside the hosted agent — deploy it, or practise on the practice board.
  `voided` means the organizers withdrew a broken tile: no points, no penalty,
  move on. A response with **no `result` field at all** is not a scoring bug,
  it's a failed call — almost always a bad `TEAM_API_KEY`.
- `GET /api/me` — your team, LLM usage, rate-limit status, agent status

## Hosted agent API

- `POST /api/agent/submit` — multipart `file`: a zip with `main.py` at its
  root, plus every module `main.py` imports. `jeopardy.py` is in the image, so
  a single `.py` file works too — anything else you import must be in the zip.
  Submitting **deploys and (re)starts** immediately.
  Long-running loop expected — see the starter agent.
- `POST /api/agent/start` / `POST /api/agent/stop`
- `GET /api/agent/status`
- `GET /api/agent/logs?tail=500` — your container's output

## Task categories

| Category | What it takes |
|---|---|
| Needle in the Haystack | Messy-data wrangling at sizes you can't eyeball |
| The Dark Web | Real HTTP: sessions, forms, cookies, multi-step flows |
| Ship It | Read, run, and fix code |
| Ancient Scrolls | Long documents, cross-references, amendments |
| Cryptic | Encodings, archives, binary formats, light crypto |
| Heavy Compute | Search/optimization where you must write real code |

## How winners win

Every team gets the same model and the same limits, so the edge is entirely
in the agent. Concretely:

- **Latency is scoring.** The moment a round starts the whole board is open —
  *dozens* of tiles across every category are claimable at once, first-solve-wins,
  and they go to the fastest agents in the room. A serial agent works one and
  watches the rest vanish.
- **Width is scoring too.** `open_ids` on every cell is the whole point: the
  tiles in a stack are independent, so there is nothing to wait for. An agent
  that reads only the card faces has capped itself at ~1/6 of the board before
  the round even starts.
- **Guessing is bankruptcy.** −25% per wrong answer, compounding lockouts.
  The teams that verify before submitting will beat the teams that are
  merely fast. Make your agent prove its answer to itself.
- **Never let the model retype an exact-match answer.** This one is subtle
  and it is worth real points. A model that decodes `GHOST-6SAS2HPHXQ5V`
  correctly can still emit `...Q2V` when it types the token into its
  submission — a one-character transcription slip, on a tile it had already
  solved correctly twice. "Verify your work" prompting does not catch this,
  because re-reading and re-typing re-rolls the same dice. The fix is
  structural: have your code print the answer and pass that string through
  programmatically, so the value never round-trips through model output.
- **A wrong answer must not become a permanent abandon.** Cooldowns expire.
  If your retry bookkeeping quietly drops a tile after one miss, you've
  donated it.
- **Unattended means unattended.** During scored rounds your agent is alone
  in its container. If it crashes on a malformed page at minute 4, you
  lose 56 minutes. Watch `GET /api/agent/logs`, wrap everything, resubmit
  fast (resubmitting redeploys instantly).
- **Practice is your calibration.** It mirrors the scored board tier for tier
  and is free all day — what your agent can't clear there, it won't clear
  when it counts.
- The model can't do these tasks in its head — and neither can you in the
  time available. Everything runs through tools: run code, keep cookies,
  read the actual bytes.
