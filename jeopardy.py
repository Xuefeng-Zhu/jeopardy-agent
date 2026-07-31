"""Agent Jeopardy client — the boring plumbing, done for you.

Auth, board, task materials, submission, and the model proxy. Nothing here is
the interesting part of the hackathon; it exists so you spend your time on the
agent, not on multipart uploads.

Everything you need is configured from four env vars (see .env.example).

SHIP THIS FILE. The hosted image may provide a copy, but including the version
you tested avoids drift. Submit a zip that contains it and every agent module:

    cd starter_agent && zip -r ../agent.zip .
"""
from __future__ import annotations

import os
import pathlib
import tempfile
import time
from threading import Lock
from typing import Any

import requests

BASE = os.environ.get("JEOPARDY_BASE_URL", "").rstrip("/")
KEY = os.environ.get("TEAM_API_KEY", "")
if not BASE or not KEY:
    raise SystemExit(
        "Set JEOPARDY_BASE_URL and TEAM_API_KEY first (see .env.example).\n"
        "Get them from /join on the event site.")

_s = requests.Session()
_s.headers["X-Api-Key"] = KEY
_log_lock = Lock()
_download_lock = Lock()
_completed_downloads: dict[pathlib.Path, tuple[int, int]] = {}


# ---------------------------------------------------------------- errors
#
# Every call below checks the HTTP status. It did not always, and the failure
# mode was expensive: /api/board needs no auth, so one typo'd character in
# TEAM_API_KEY left the board looking perfect while /api/task quietly returned
# {"detail": "Invalid API key"} and every submit came back with no "result" at
# all — which reads exactly like a scoring bug. That cost a tester twenty
# minutes. Now it raises AuthError on the first call, once, in one line.

class JeopardyError(RuntimeError):
    """The game server refused a call. The message says which and why."""


class AuthError(JeopardyError):
    """Your key was rejected. Nothing will work until you fix it, so this is
    fatal on purpose — don't catch it per-tile."""


class TileUnavailable(JeopardyError):
    """The server won't hand over this tile *right now*: its row hasn't
    unlocked, its board isn't live in this phase, or the id doesn't exist.

    A normal game state, not a bug. Catch it and move to another tile.
    """


_BAD_KEY = ("TEAM_API_KEY was rejected — re-copy it from /join (no quotes, "
            "no trailing whitespace) and `source .env` again")


def _detail(r) -> str:
    """The server's own explanation, whatever shape it arrived in."""
    try:
        body = r.json()
        if isinstance(body, dict):
            body = body.get("detail") or body
        return str(body)[:300]
    except Exception:                                          # noqa: BLE001
        return (r.text or "").strip()[:300]


def _raise_for(r, what: str, unavailable: tuple[int, ...] = ()) -> None:
    """Raise unless this response is usable.

    `unavailable` lists the statuses that mean "not this tile, not now" rather
    than "you have a bug" — 403 (locked row / board not live in this phase)
    and 404 (no such tile) on the task endpoints. Those become
    TileUnavailable so a strategy loop can skip past them.
    """
    if r.status_code == 401:
        raise AuthError(f"{what}: 401 Unauthorized — {_BAD_KEY} "
                        f"(sent a {len(KEY)}-char key starting {KEY[:5]!r})")
    if r.status_code in unavailable:
        raise TileUnavailable(f"{what}: {r.status_code} — {_detail(r)}")
    if not r.ok:
        raise JeopardyError(f"{what}: HTTP {r.status_code} — {_detail(r)}")


def _json(r, what: str, unavailable: tuple[int, ...] = ()) -> dict:
    _raise_for(r, what, unavailable)
    try:
        return r.json()
    except ValueError:
        raise JeopardyError(
            f"{what}: expected JSON, got "
            f"{r.headers.get('content-type', 'nothing')} — "
            f"{(r.text or '')[:200]!r}") from None


# ---------------------------------------------------------------- game state

def board() -> dict:
    """Whole board: phase, categories, tiles, claims, scoreboard.

    Each entry in `boards[<board>][i]["tiles"]` is a CELL, not a tile: one
    card per (category, points) pair, the way the projector shows it. A cell
    holds a stack of independent variants and publishes all of them:

        open_ids   every unclaimed variant in this cell — all claimable NOW,
                   by you, in parallel
        remaining  len(open_ids)
        total      how deep the stack was to begin with
        id         just the first of open_ids (what the card renders)

    `open_tiles()` below flattens that for you. Don't plan off `id` alone;
    the practice board is 30 cells but 60 tiles.

    Also returns `you.solved_ids` — every tile you've already answered
    correctly. Practice tiles never lock, so this is how you avoid paying to
    solve the same tile twice after a redeploy.
    """
    b = _json(_s.get(f"{BASE}/api/board", timeout=30), "GET /api/board")
    if "you" not in b:
        # This endpoint takes no auth, so a bad key still returns a flawless
        # board. The `you` block is added only for a key the server
        # recognises, so its absence is the earliest possible proof.
        raise AuthError(f"GET /api/board returned no `you` block — {_BAD_KEY}")
    return b


_PHASE_BOARD = {"practice": "practice", "round1": "qual", "game": "main"}


def live_board(b: dict | None = None) -> str:
    """Which board is actually PLAYABLE right now: "practice"|"qual"|"main".

    Chosen from `phase`, not from which boards happen to be present. A board
    stays in the payload once its round has started, so after Round 1 you
    still see `qual` and after Round 2 you still see `main` — and organizers
    put the phase back to `practice` between rounds. Picking the richest board
    that EXISTS therefore hands you tiles whose every submission comes back
    `{"result": "wrong_phase"}` during exactly the hour you meant to practise.
    """
    b = b or board()
    boards = b.get("boards", {})
    want = _PHASE_BOARD.get(b.get("phase") or "")
    if want in boards:
        return want
    return next((k for k in ("main", "qual", "practice") if k in boards),
                "practice")


def open_tiles(b: dict | None = None) -> list[dict]:
    """EVERY open tile on the board that is playable right now (`live_board`),
    richest first.

    One dict per VARIANT, not per cell: a copy of the cell's card with `id`
    replaced by that variant's own id, plus `category` (the board payload puts
    the category name on the parent, not the card). `open_ids`, `remaining`
    and `total` are left untouched, so they still describe the whole cell.

    This is your parallelism budget, not a work queue. Measured on the
    practice board: 30 cells, 60 tiles — the head-only version of this
    function hid the second variant behind every card, including the
    stateful-web tile that is the hardest thing in the kit. On a sized main
    board it is 30 cells and a few hundred tiles. Every one of them is yours to
    work concurrently, and a team that reads only the cards is racing at a
    fraction of the width.

    Locked rows and anything in `you.solved_ids` are dropped. Ordering is
    richest first, and within a cell the server's own `open_ids` order is kept
    (the leading variants of a cell are the ones it pre-generates, so they
    answer fastest). It is a starting heuristic and a bad one — see the README.
    """
    b = b or board()
    solved = set((b.get("you") or {}).get("solved_ids") or [])
    out: list[dict] = []
    for cat in b.get("boards", {}).get(live_board(b), []):
        for cell in cat["tiles"]:
            if cell.get("locked"):
                continue
            ids = cell.get("open_ids")
            if ids is None:           # defensive: an older/flatter payload
                ids = [] if cell.get("claimed_by") else [cell["id"]]
            for tid in ids:
                if tid in solved:
                    continue
                out.append({**cell, "id": tid, "category": cat.get("name")})
    # Stable: equal-points tiles keep the order the server listed them in.
    return sorted(out, key=lambda t: -t.get("points", 0))


def _check_cancel(cancellation: Any | None) -> None:
    if cancellation is not None:
        cancellation.check()


def _register_close(cancellation: Any | None, response: Any):
    if cancellation is not None and hasattr(cancellation, "add_callback"):
        cancellation.add_callback(response.close)
        return response.close
    return None


def task(task_id: str, cancellation: Any | None = None) -> dict:
    """Prompt, file list, points, and `answer_format` (how the checker
    compares your answer — worth telling your model).

    Raises TileUnavailable if the row is still locked or the board isn't live
    yet; that is a game state, so catch it and pick another tile.
    """
    _check_cancel(cancellation)
    response = _s.get(
        f"{BASE}/api/task/{task_id}",
        timeout=(5, 10),
        stream=True,
    )
    close_callback = _register_close(cancellation, response)
    try:
        result = _json(
            response, f"GET /api/task/{task_id}", unavailable=(403, 404)
        )
        _check_cancel(cancellation)
        return result
    finally:
        if close_callback is not None:
            cancellation.remove_callback(close_callback)
        response.close()


def workdir(task_id: str) -> pathlib.Path:
    """A stable scratch directory for one task. Reuse it across attempts.

    Stable on purpose. A fresh `tempfile.mkdtemp()` per attempt leaves another
    copy of the materials behind on every retry — the heaviest tiles are ~4.7 MB
    — and your container has no disk quota, so a retry loop quietly fills the
    host. Reusing one directory per task bounds your scratch space by the size
    of the board's materials instead of by how many times you retry.
    """
    d = pathlib.Path(tempfile.gettempdir()) / f"jeopardy_{task_id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def fetch_files(task_id: str, detail: dict,
                dest: str | pathlib.Path | None = None,
                cancellation: Any | None = None) -> list[str]:
    """Download a task's files. Returns the filenames.

    Defaults to `workdir(task_id)` and SKIPS anything already downloaded: your
    materials are generated deterministically from your team id, so they never
    change and a second call costs nothing. Re-pulling them every turn is pure
    waste — for you and for the server everyone else is sharing.
    """
    out = pathlib.Path(dest) if dest is not None else workdir(task_id)
    out.mkdir(parents=True, exist_ok=True)
    names = []
    for name in detail.get("files", []):
        _check_cancel(cancellation)
        path = out / name
        with _download_lock:
            expected_stat = _completed_downloads.get(path)
        try:
            current_stat = path.stat()
        except OSError:
            current_stat = None
        complete = (
            expected_stat is not None
            and current_stat is not None
            and expected_stat
            == (current_stat.st_size, current_stat.st_mtime_ns)
        )
        if not complete:
            r = _s.get(
                f"{BASE}/api/task/{task_id}/file/{name}",
                timeout=(5, 10),
                stream=True,
                headers={"Accept-Encoding": "identity"},
            )
            close_callback = _register_close(cancellation, r)
            temporary: pathlib.Path | None = None
            try:
                # Checked, because an error body is a JSON blob rather than a
                # challenge file.
                _raise_for(
                    r,
                    f"GET /api/task/{task_id}/file/{name}",
                    unavailable=(403, 404),
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    dir=path.parent, prefix=f".{path.name}.", delete=False
                ) as writer:
                    temporary = pathlib.Path(writer.name)
                    downloaded = 0
                    for chunk in r.iter_content(chunk_size=65_536):
                        _check_cancel(cancellation)
                        if not chunk:
                            continue
                        writer.write(chunk)
                        downloaded += len(chunk)
                expected = r.headers.get("content-length")
                content_encoding = (
                    r.headers.get("content-encoding") or "identity"
                ).strip().lower()
                # requests.iter_content() transparently decodes gzip/br bodies,
                # while Content-Length still describes the encoded wire bytes.
                # Enforce the header only when it describes the bytes written.
                if (
                    expected is not None
                    and content_encoding in {"", "identity"}
                    and downloaded != int(expected)
                ):
                    raise JeopardyError(
                        f"GET /api/task/{task_id}/file/{name}: expected "
                        f"{expected} bytes, downloaded {downloaded}"
                    )
                if downloaded == 0:
                    raise JeopardyError(
                        f"GET /api/task/{task_id}/file/{name}: empty download"
                    )
                _check_cancel(cancellation)
                os.replace(temporary, path)
                temporary = None
                completed_stat = path.stat()
                with _download_lock:
                    _completed_downloads[path] = (
                        completed_stat.st_size,
                        completed_stat.st_mtime_ns,
                    )
            finally:
                if close_callback is not None:
                    cancellation.remove_callback(close_callback)
                r.close()
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        names.append(name)
    return names


def submit(task_id: str, answer: str) -> dict:
    """Submit an answer.

    Returns {"result": "correct" | "incorrect" | "already_claimed" |
    "locked_out" | "rate_limited" | "locked" | "wrong_phase" | "forbidden" |
    "voided" | "unknown_task", ...}. Every one of those is a normal 200 from
    the server, so they come back as data; only a broken request or a bad key
    raises. Wrong answers on scored tiles cost points and trigger a doubling
    cooldown, so submit deliberately.
    """
    res = _json(_s.post(f"{BASE}/api/submit",
                        json={"task_id": task_id, "answer": str(answer)},
                        timeout=30),
                f"POST /api/submit {task_id}")
    if "result" not in res:
        raise JeopardyError(f"POST /api/submit {task_id}: no `result` field in "
                            f"{res!r} — that is not a scoring bug, the call "
                            f"itself failed")
    return res


def me() -> dict:
    """Your team, LLM usage, and remaining budget."""
    return _json(_s.get(f"{BASE}/api/me", timeout=30), "GET /api/me")


# ---------------------------------------------------------------- the model

def anthropic_client():
    """An Anthropic SDK client pointed at the event proxy.

    The proxy forces one model for everyone regardless of what you request,
    and your agent's sandbox has no other network access — so this is the
    only model you get. The competition is what you build around it.
    """
    from anthropic import Anthropic
    return Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", KEY),
                     base_url=os.environ.get("ANTHROPIC_BASE_URL",
                                             f"{BASE}/anthropic"))


MODEL = os.environ.get("MODEL", "claude-haiku-4-5")


def log(*a):
    with _log_lock:
        print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)
