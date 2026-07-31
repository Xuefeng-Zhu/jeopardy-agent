"""Pre-deployment acceptance gates for the Agent Jeopardy Round 2 build."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


CATEGORIES = (
    "Ancient Scrolls",
    "Cryptic",
    "Heavy Compute",
    "Needle in the Haystack",
    "Ship It",
    "The Dark Web",
)
RUNTIME_FILES = (
    "main.py",
    "jeopardy.py",
    "agent_types.py",
    "verification.py",
    "tool_runtime.py",
    "solver.py",
    "orchestrator.py",
    "fast_paths.py",
    "requirements.txt",
)
TIMESTAMP = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s")
PHASE = re.compile(r"phase changed to '([^']+)'")
SOLVE = re.compile(
    r"(?P<task>\S+): solving (?P<category>.+) "
    r"(?P<points>\d+)pt attempt=(?P<attempt>\d+)"
)
QUEUED = re.compile(r"(?P<task>\S+): verified candidate queued\b")
SUBMISSION = re.compile(r"(?P<task>\S+): submission -> '([^']+)'")


@dataclass(frozen=True, slots=True)
class GateResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class RunMetrics:
    first_candidate_seconds: float | None
    category_start_seconds: dict[str, float]
    first_batch_recipes: tuple[str, ...]
    queue_wait_seconds: tuple[float, ...]
    outcomes: tuple[str, ...]


def _seconds(line: str) -> int | None:
    match = TIMESTAMP.match(line)
    if match is None:
        return None
    hours, minutes, seconds = map(int, match.groups())
    return hours * 3_600 + minutes * 60 + seconds


def _elapsed(timestamp: int, start: int) -> float:
    if timestamp < start:
        timestamp += 24 * 3_600
    return float(timestamp - start)


def phase_lines(log_text: str, phase: str) -> list[str]:
    lines = log_text.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if (match := PHASE.search(line)) is not None and match.group(1) == phase
    ]
    if not starts:
        raise ValueError(f"log has no phase transition to {phase!r}")
    start = starts[-1]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if PHASE.search(lines[index]) is not None:
            end = index
            break
    return lines[start:end]


def analyze_run(log_text: str, phase: str, first_batch_size: int) -> RunMetrics:
    lines = phase_lines(log_text, phase)
    start = _seconds(lines[0])
    if start is None:
        raise ValueError("phase transition line has no timestamp")

    queued_at: dict[str, int] = {}
    category_starts: dict[str, float] = {}
    first_batch: list[str] = []
    queue_waits: list[float] = []
    outcomes: list[str] = []

    for line in lines:
        timestamp = _seconds(line)
        if timestamp is None:
            continue
        solve = SOLVE.search(line)
        if solve is not None and solve.group("attempt") == "1":
            category = solve.group("category")
            points = int(solve.group("points"))
            category_starts.setdefault(category, _elapsed(timestamp, start))
            if len(first_batch) < first_batch_size:
                first_batch.append(f"{category}:{points}")
        queued = QUEUED.search(line)
        if queued is not None:
            queued_at.setdefault(queued.group("task"), timestamp)
        submission = SUBMISSION.search(line)
        if submission is not None:
            task_id, outcome = submission.groups()
            outcomes.append(outcome)
            if task_id in queued_at:
                queue_waits.append(
                    _elapsed(timestamp, queued_at[task_id])
                )

    first_candidate = (
        min(_elapsed(value, start) for value in queued_at.values())
        if queued_at
        else None
    )
    return RunMetrics(
        first_candidate_seconds=first_candidate,
        category_start_seconds=category_starts,
        first_batch_recipes=tuple(first_batch),
        queue_wait_seconds=tuple(queue_waits),
        outcomes=tuple(outcomes),
    )


def _percentile(values: tuple[float, ...], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def evaluate_run(
    metrics: RunMetrics,
    *,
    first_candidate_limit: float,
    category_start_limit: float,
    queue_p95_limit: float,
    already_claimed_limit: float,
    first_batch_size: int,
) -> list[GateResult]:
    results: list[GateResult] = []
    first = metrics.first_candidate_seconds
    results.append(
        GateResult(
            "first_candidate_latency",
            first is not None and first <= first_candidate_limit,
            (
                f"{first:.1f}s <= {first_candidate_limit:.1f}s"
                if first is not None
                else "no candidate was queued"
            ),
        )
    )

    missing = [
        category
        for category in CATEGORIES
        if category not in metrics.category_start_seconds
    ]
    late = {
        category: seconds
        for category, seconds in metrics.category_start_seconds.items()
        if category in CATEGORIES and seconds > category_start_limit
    }
    results.append(
        GateResult(
            "category_start_latency",
            not missing and not late,
            (
                f"all categories started within {category_start_limit:.1f}s"
                if not missing and not late
                else f"missing={missing or 'none'} late={late or 'none'}"
            ),
        )
    )

    recipes = metrics.first_batch_recipes
    complete_batch = len(recipes) == first_batch_size
    unique_batch = len(set(recipes)) == len(recipes)
    results.append(
        GateResult(
            "first_batch_recipe_diversity",
            complete_batch and unique_batch,
            f"recipes={list(recipes)}",
        )
    )

    queue_p95 = _percentile(metrics.queue_wait_seconds, 0.95)
    results.append(
        GateResult(
            "queue_wait_p95",
            queue_p95 is not None and queue_p95 <= queue_p95_limit,
            (
                f"{queue_p95:.1f}s <= {queue_p95_limit:.1f}s"
                if queue_p95 is not None
                else "no queued candidate reached an outcome"
            ),
        )
    )

    resolved = len(metrics.outcomes)
    claimed = sum(
        outcome == "already_claimed" for outcome in metrics.outcomes
    )
    claimed_rate = claimed / resolved if resolved else 1.0
    results.append(
        GateResult(
            "already_claimed_rate",
            resolved > 0 and claimed_rate < already_claimed_limit,
            (
                f"{claimed}/{resolved} = {claimed_rate:.1%}; "
                f"limit < {already_claimed_limit:.1%}"
            ),
        )
    )

    incorrect = sum(outcome == "incorrect" for outcome in metrics.outcomes)
    results.append(
        GateResult(
            "incorrect_submissions",
            incorrect == 0,
            f"incorrect={incorrect}",
        )
    )
    return results


def evaluate_archive(archive: Path, source_dir: Path) -> GateResult:
    if not archive.is_file():
        return GateResult("archive_source_parity", False, f"missing {archive}")
    try:
        with zipfile.ZipFile(archive) as bundle:
            bad = bundle.testzip()
            names = tuple(
                name for name in bundle.namelist() if not name.endswith("/")
            )
            if bad is not None:
                return GateResult(
                    "archive_source_parity", False, f"corrupt member {bad}"
                )
            if set(names) != set(RUNTIME_FILES):
                missing = sorted(set(RUNTIME_FILES) - set(names))
                extra = sorted(set(names) - set(RUNTIME_FILES))
                return GateResult(
                    "archive_source_parity",
                    False,
                    f"missing={missing or 'none'} extra={extra or 'none'}",
                )
            mismatches = [
                name
                for name in RUNTIME_FILES
                if not (source_dir / name).is_file()
                or bundle.read(name) != (source_dir / name).read_bytes()
            ]
    except (OSError, zipfile.BadZipFile) as exc:
        return GateResult("archive_source_parity", False, str(exc))
    return GateResult(
        "archive_source_parity",
        not mismatches,
        (
            "all runtime files match source"
            if not mismatches
            else f"mismatched={mismatches}"
        ),
    )


def load_log_file(path: Path) -> str:
    text = path.read_text("utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(value, dict) and isinstance(value.get("logs"), str):
        return value["logs"]
    raise ValueError("JSON log file must contain a string `logs` field")


def fetch_live() -> tuple[str, GateResult]:
    base = os.environ.get("JEOPARDY_BASE_URL", "").rstrip("/")
    key = os.environ.get("TEAM_API_KEY", "")
    if not base or not key:
        raise ValueError("live mode requires JEOPARDY_BASE_URL and TEAM_API_KEY")
    headers = {"X-Api-Key": key}
    logs_response = requests.get(
        f"{base}/api/agent/logs?tail=5000", headers=headers, timeout=30
    )
    logs_response.raise_for_status()
    logs = logs_response.json().get("logs")
    if not isinstance(logs, str):
        raise ValueError("live log response has no string `logs` field")
    me_response = requests.get(
        f"{base}/api/me", headers=headers, timeout=30
    )
    me_response.raise_for_status()
    agent = me_response.json().get("agent") or {}
    healthy = bool(agent.get("healthy")) and agent.get("state") == "running"
    detail = (
        f"state={agent.get('state')!r} healthy={agent.get('healthy')!r} "
        f"revision={agent.get('revision')!r}"
    )
    return logs, GateResult("hosted_agent_health", healthy, detail)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--logs", type=Path, help="plain text or {logs: ...} JSON")
    source.add_argument("--live", action="store_true", help="fetch hosted logs")
    parser.add_argument("--phase", choices=("practice", "round1", "game"), required=True)
    parser.add_argument("--archive", type=Path, default=Path("agent.zip"))
    parser.add_argument("--source-dir", type=Path, default=Path("."))
    parser.add_argument("--first-candidate-seconds", type=float, default=3.0)
    parser.add_argument("--category-start-seconds", type=float, default=10.0)
    parser.add_argument("--queue-p95-seconds", type=float, default=15.0)
    parser.add_argument("--already-claimed-rate", type=float, default=0.10)
    parser.add_argument("--first-batch-size", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        health: GateResult | None = None
        if args.live:
            log_text, health = fetch_live()
        else:
            log_text = load_log_file(args.logs)
        metrics = analyze_run(
            log_text, args.phase, max(1, args.first_batch_size)
        )
        results = evaluate_run(
            metrics,
            first_candidate_limit=max(0.0, args.first_candidate_seconds),
            category_start_limit=max(0.0, args.category_start_seconds),
            queue_p95_limit=max(0.0, args.queue_p95_seconds),
            already_claimed_limit=min(
                1.0, max(0.0, args.already_claimed_rate)
            ),
            first_batch_size=max(1, args.first_batch_size),
        )
        results.append(
            evaluate_archive(args.archive, args.source_dir.resolve())
        )
        if health is not None:
            results.append(health)
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"ERROR acceptance setup: {exc}", file=sys.stderr)
        return 2

    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status:4} {result.name}: {result.detail}")
    passed = sum(result.passed for result in results)
    print(f"\nRound 2 acceptance: {passed}/{len(results)} gates passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
