"""Concurrent board controller and globally throttled submission lane."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent_types import (
    CancellationToken,
    Candidate,
    ReplayRecipe,
    SolveCancelled,
    Tile,
    TileRecord,
    TileStatus,
)
from fast_paths import is_registered_fast_path_title
from solver import TileSolver
from verification import CandidateVerifier


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class AgentConfig:
    verbose: bool = False
    task_filter: tuple[str, ...] = ()
    max_tiles: int = 0
    model_workers: int = 4
    max_turns: int = 12
    max_tokens: int = 1800
    max_attempts: int = 3
    recovery_attempts: int = 1
    board_poll_seconds: float = 2.0
    submit_interval: float = 3.15
    minimum_confidence: float = 0.82
    candidate_buffer: int = 16
    run_once: bool = False
    dry_run: bool = False
    fast_paths: bool = True
    fast_path_only: bool = False

    @classmethod
    def from_env(cls) -> AgentConfig:
        task_filter = tuple(
            item.strip()
            for item in os.environ.get("TASK_FILTER", "").split(",")
            if item.strip()
        )
        return cls(
            verbose=_env_bool("VERBOSE"),
            task_filter=task_filter,
            max_tiles=max(0, int(os.environ.get("MAX_TILES", "0"))),
            model_workers=max(1, int(os.environ.get("MODEL_WORKERS", "4"))),
            max_turns=max(2, int(os.environ.get("MAX_TURNS", "12"))),
            max_tokens=max(256, int(os.environ.get("MAX_MODEL_TOKENS", "1800"))),
            max_attempts=max(1, int(os.environ.get("MAX_ATTEMPTS", "3"))),
            recovery_attempts=max(
                0, int(os.environ.get("RECOVERY_ATTEMPTS", "1"))
            ),
            board_poll_seconds=max(
                0.5, float(os.environ.get("BOARD_POLL_SECONDS", "2"))
            ),
            submit_interval=max(3.05, float(os.environ.get("SUBMIT_INTERVAL", "3.15"))),
            minimum_confidence=min(
                1.0, max(0.0, float(os.environ.get("MIN_CONFIDENCE", "0.82")))
            ),
            candidate_buffer=max(1, int(os.environ.get("CANDIDATE_BUFFER", "16"))),
            run_once=_env_bool("RUN_ONCE"),
            dry_run=_env_bool("DRY_RUN"),
            fast_paths=_env_bool("FAST_PATHS", default=True),
            fast_path_only=_env_bool("FAST_PATH_ONLY"),
        )


@dataclass(slots=True)
class RecipeState:
    successes: int = 0
    failures: int = 0
    generation: int = 0
    blocked_until: float = 0.0
    passed_phase: str = ""
    canary_phase: str = ""
    canary_task: str | None = None

    @property
    def trusted(self) -> bool:
        return self.successes >= 2 and self.failures == 0


class RecipeGate:
    """Protect sibling tiles from a solver family that has not earned trust."""

    def __init__(self) -> None:
        self.phase = ""
        self.states: dict[str, RecipeState] = {}

    def set_phase(self, phase: str) -> None:
        if phase == self.phase:
            return
        self.phase = phase
        for state in self.states.values():
            state.canary_phase = ""
            state.canary_task = None

    def state(self, recipe_key: str) -> RecipeState:
        return self.states.setdefault(recipe_key, RecipeState())

    def generation(self, recipe_key: str) -> int:
        return self.state(recipe_key).generation

    def is_current(self, candidate: Candidate) -> bool:
        return candidate.recipe_generation == self.generation(candidate.recipe_key)

    def can_submit(self, candidate: Candidate, now: float) -> bool:
        state = self.state(candidate.recipe_key)
        if candidate.recipe_generation != state.generation:
            return False
        if now < state.blocked_until:
            return False
        if self.phase == "practice" or state.trusted:
            return True
        if state.passed_phase == self.phase:
            return True
        if state.canary_phase != self.phase:
            state.canary_phase = self.phase
            state.canary_task = None
        if state.canary_task is None:
            state.canary_task = candidate.task_id
            return True
        return state.canary_task == candidate.task_id

    def next_unblock(self, candidate: Candidate) -> float:
        return self.state(candidate.recipe_key).blocked_until

    def on_correct(self, candidate: Candidate) -> None:
        state = self.state(candidate.recipe_key)
        state.successes += 1
        if state.canary_task == candidate.task_id:
            state.passed_phase = self.phase
            state.canary_task = None

    def on_neutral(self, candidate: Candidate) -> None:
        state = self.state(candidate.recipe_key)
        if state.canary_task == candidate.task_id:
            state.canary_task = None

    def on_incorrect(self, candidate: Candidate, retry_after: float) -> None:
        state = self.state(candidate.recipe_key)
        state.failures += 1
        state.generation += 1
        state.blocked_until = time.monotonic() + max(10.0, retry_after)
        state.passed_phase = ""
        state.canary_task = None
        state.canary_phase = self.phase

    def estimated_success(self, recipe_key: str) -> float:
        state = self.state(recipe_key)
        if state.trusted:
            return 0.98
        return (state.successes + 1.0) / (state.successes + state.failures + 2.0)


ResultHandler = Callable[[Candidate, dict[str, Any]], Awaitable[None]]
OpenCheck = Callable[[str], bool]


class SubmissionManager:
    def __init__(
        self,
        game: Any,
        config: AgentConfig,
        gate: RecipeGate,
        is_open: OpenCheck,
        on_result: ResultHandler,
    ) -> None:
        self.game = game
        self.config = config
        self.gate = gate
        self.is_open = is_open
        self.on_result = on_result
        self._pending: list[Candidate] = []
        self._condition = asyncio.Condition()
        self._stopping = False
        self._last_submit = 0.0

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def enqueue(self, candidate: Candidate) -> None:
        async with self._condition:
            if any(item.task_id == candidate.task_id for item in self._pending):
                return
            self._pending.append(candidate)
            self._condition.notify_all()

    async def stop(self, *, discard_pending: bool = True) -> None:
        async with self._condition:
            self._stopping = True
            if discard_pending:
                self._pending.clear()
            self._condition.notify_all()

    async def run(self) -> None:
        while True:
            candidate = await self._next_candidate()
            if candidate is None:
                return

            if not self.is_open(candidate.task_id):
                self.gate.on_neutral(candidate)
                await self.on_result(candidate, {"result": "already_claimed"})
                await self._notify()
                continue

            wait = self.config.submit_interval - (time.monotonic() - self._last_submit)
            if wait > 0:
                await asyncio.sleep(wait)

            try:
                if self.config.dry_run:
                    result = {"result": "dry_run"}
                else:
                    result = await asyncio.to_thread(
                        self.game.submit, candidate.task_id, candidate.answer
                    )
                self._last_submit = time.monotonic()
            except Exception as exc:  # noqa: BLE001 - survive transient API faults
                self.game.log(
                    f"{candidate.task_id}: submit error {exc!r}; retrying in 5s"
                )
                self.gate.on_neutral(candidate)
                await self.enqueue(candidate.delayed(5.0))
                continue

            outcome = str(result.get("result") or "")
            if outcome in {"rate_limited", "locked_out", "locked", "wrong_phase"}:
                delay = self._retry_seconds(result, default=5.0)
                self.game.log(
                    f"{candidate.task_id}: {outcome}; candidate retained for "
                    f"{delay:.1f}s"
                )
                await self.enqueue(candidate.delayed(delay))
                continue

            if outcome == "correct":
                self.gate.on_correct(candidate)
            elif outcome == "incorrect":
                self.gate.on_incorrect(
                    candidate, self._retry_seconds(result, default=30.0)
                )
            else:
                self.gate.on_neutral(candidate)

            await self.on_result(candidate, result)
            await self._notify()

    async def _next_candidate(self) -> Candidate | None:
        while True:
            async with self._condition:
                if self._stopping and not self._pending:
                    return None

                # A failed canary increments the recipe generation. Old sibling
                # answers are unsafe even if their own local verification passed.
                self._pending = [
                    item for item in self._pending if self.gate.is_current(item)
                ]

                now = time.monotonic()
                ranked = sorted(
                    self._pending,
                    key=lambda item: (item.priority, -item.created_at),
                    reverse=True,
                )
                selected: Candidate | None = None
                for item in ranked:
                    if item.not_before > now:
                        continue
                    if self.gate.can_submit(item, now):
                        selected = item
                        break

                if selected is not None:
                    self._pending.remove(selected)
                    return selected

                if self._stopping and not self._pending:
                    return None

                wake_times = [
                    value
                    for item in self._pending
                    for value in (item.not_before, self.gate.next_unblock(item))
                    if value > now
                ]
                timeout = (
                    min(1.0, max(0.05, min(wake_times) - now)) if wake_times else 1.0
                )
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=timeout)
                except TimeoutError:
                    pass

    async def _notify(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    @staticmethod
    def _retry_seconds(result: dict[str, Any], default: float) -> float:
        for key in ("retry_in", "retry_after", "cooldown"):
            try:
                if key in result:
                    return max(0.5, float(result[key]))
            except (TypeError, ValueError):
                pass
        return default


class AgentController:
    def __init__(self, game: Any, config: AgentConfig) -> None:
        self.game = game
        self.config = config
        self.gate = RecipeGate()
        self.verifier = CandidateVerifier(config.minimum_confidence)
        self.solver = TileSolver(
            game,
            max_turns=config.max_turns,
            max_tokens=config.max_tokens,
            verbose=config.verbose,
            fast_paths=config.fast_paths,
            fast_path_only=config.fast_path_only,
        )
        self.records: dict[str, TileRecord] = {}
        self.replay_recipes: dict[str, ReplayRecipe] = {}
        self.open_ids: set[str] = set()
        self.phase = ""
        self._active: dict[str, asyncio.Task[None]] = {}
        self._cancel_tokens: dict[str, CancellationToken] = {}
        self._selected_ids: set[str] | None = None
        self._fatal_error: BaseException | None = None
        self.submitter = SubmissionManager(
            game,
            config,
            self.gate,
            self.is_open,
            self._on_submission_result,
        )

    def is_open(self, task_id: str) -> bool:
        return task_id in self.open_ids

    async def run(self) -> None:
        submit_task = asyncio.create_task(self.submitter.run())
        first_sync = False
        board_errors = 0
        try:
            while True:
                if self._fatal_error is not None:
                    raise self._fatal_error
                try:
                    board = await asyncio.to_thread(self.game.board)
                    board_errors = 0
                    self._sync_board(board)
                    first_sync = True
                    self._schedule()
                except Exception as exc:
                    if type(exc).__name__ == "AuthError":
                        raise
                    board_errors += 1
                    self.game.log(
                        f"board poll failed ({board_errors}): {exc!r}; continuing"
                    )

                if self.config.run_once and first_sync and self._run_once_done():
                    break
                await asyncio.sleep(
                    min(10.0, self.config.board_poll_seconds * (1 + board_errors))
                )
        finally:
            for token in self._cancel_tokens.values():
                token.cancel("agent controller is shutting down")
            if self._active:
                await asyncio.gather(*self._active.values(), return_exceptions=True)
            await self.submitter.stop()
            await submit_task

    def _sync_board(self, board: dict[str, Any]) -> None:
        phase = str(board.get("phase") or "")
        if phase != self.phase:
            self.phase = phase
            self.gate.set_phase(phase)
            self.game.log(f"phase changed to {phase!r}")

        raw_tiles = list(self.game.open_tiles(board))
        if self.config.task_filter:
            wanted = set(self.config.task_filter)
            raw_tiles = [value for value in raw_tiles if value.get("id") in wanted]

        if self.config.max_tiles and self._selected_ids is None:
            self._selected_ids = {
                str(value["id"]) for value in raw_tiles[: self.config.max_tiles]
            }
        if self._selected_ids is not None:
            raw_tiles = [
                value for value in raw_tiles if str(value["id"]) in self._selected_ids
            ]

        self.open_ids = {str(value["id"]) for value in raw_tiles}
        for value in raw_tiles:
            tile = Tile.from_board(value)
            self.records.setdefault(tile.id, TileRecord(tile=tile))

        for task_id, record in self.records.items():
            if task_id in self.open_ids:
                continue
            if record.status not in {
                TileStatus.SOLVED,
                TileStatus.CLAIMED,
                TileStatus.FAILED,
            }:
                token = self._cancel_tokens.get(task_id)
                if token is not None:
                    token.cancel("tile disappeared from the open board")
                    self.game.log(
                        f"{task_id}: claimed elsewhere; cancelling active work"
                    )
                record.status = TileStatus.CLAIMED
                record.candidate = None

    def _schedule(self) -> None:
        capacity = self.config.model_workers - len(self._active)
        if (
            capacity <= 0
            or self.submitter.pending_count >= self.config.candidate_buffer
        ):
            return

        now = time.monotonic()
        eligible = [
            record
            for record in self.records.values()
            if record.tile.id in self.open_ids
            and record.status in {TileStatus.NEW, TileStatus.RETRY}
            and record.next_attempt_at <= now
            and record.attempts
            < self.config.max_attempts + record.recoveries
        ]
        room = min(
            capacity, self.config.candidate_buffer - self.submitter.pending_count
        )
        selected = self._select_records(eligible, room)

        for record in selected:
            token = CancellationToken()
            self._cancel_tokens[record.tile.id] = token
            task = asyncio.create_task(self._solve(record, token))
            self._active[record.tile.id] = task
            task.add_done_callback(
                lambda _task, task_id=record.tile.id: self._worker_done(task_id)
            )

    def _select_records(
        self, eligible: list[TileRecord], room: int
    ) -> list[TileRecord]:
        """Reserve worker capacity for fast tiers and high-value exploration."""

        if room <= 0:
            return []
        ranked = sorted(
            eligible,
            key=lambda record: (
                self._solve_priority(record),
                record.tile.points,
                int(is_registered_fast_path_title(record.tile.title)),
            ),
            reverse=True,
        )
        busy_recipes = {
            record.tile.recipe_key
            for record in self.records.values()
            if record.status in {TileStatus.SOLVING, TileStatus.CANDIDATE}
        }
        selected: list[TileRecord] = []
        selected_recipes: set[str] = set()
        active_high = sum(
            1
            for record in self.records.values()
            if record.status in {TileStatus.SOLVING, TileStatus.CANDIDATE}
            and record.tile.points >= 400
        )
        high_limit = max(1, self.config.model_workers // 2)
        high_slots = max(0, min(room, high_limit - active_high))

        def can_select(record: TileRecord) -> bool:
            recipe_key = record.tile.recipe_key
            has_replay = recipe_key in self.replay_recipes
            has_success = self.gate.state(recipe_key).successes > 0
            return not (
                not has_replay
                and not has_success
                and (recipe_key in busy_recipes or recipe_key in selected_recipes)
            )

        def take(records: list[TileRecord], limit: int) -> None:
            for record in records:
                if len(selected) >= room or limit <= 0:
                    return
                if record in selected or not can_select(record):
                    continue
                selected.append(record)
                selected_recipes.add(record.tile.recipe_key)
                limit -= 1

        high = [record for record in ranked if record.tile.points >= 400]
        fast = [record for record in ranked if record.tile.points < 400]
        take(high, high_slots)
        take(fast, room - len(selected))
        take(ranked, room - len(selected))
        return selected

    def _solve_priority(self, record: TileRecord) -> float:
        expected_seconds = {
            100: 20.0,
            200: 30.0,
            300: 50.0,
            400: 85.0,
            500: 140.0,
        }.get(record.tile.points, 90.0)
        retry_penalty = 1.0 + (0.25 * record.attempts)
        return (
            record.tile.points
            * self.gate.estimated_success(record.tile.recipe_key)
            / (expected_seconds * retry_penalty)
        )

    def _worker_done(self, task_id: str) -> None:
        self._active.pop(task_id, None)
        self._cancel_tokens.pop(task_id, None)

    async def _solve(self, record: TileRecord, cancellation: CancellationToken) -> None:
        record.status = TileStatus.SOLVING
        record.attempts += 1
        generation = self.gate.generation(record.tile.recipe_key)
        replay_recipe = self.replay_recipes.get(record.tile.recipe_key)
        self.game.log(
            f"{record.tile.id}: solving {record.tile.category} "
            f"{record.tile.points}pt attempt={record.attempts}"
        )
        try:
            candidate = await asyncio.to_thread(
                self.solver.solve,
                record.tile,
                generation,
                replay_recipe,
                cancellation,
                attempt=record.attempts,
            )
            cancellation.check()
        except SolveCancelled as exc:
            self.game.log(f"{record.tile.id}: solve cancelled: {exc}")
            if record.tile.id not in self.open_ids:
                record.status = TileStatus.CLAIMED
                record.candidate = None
            else:
                self._retry_or_fail(record, str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - isolate one malformed tile
            if type(exc).__name__ == "AuthError":
                self._fatal_error = exc
                return
            self.game.log(f"{record.tile.id}: solver error {exc!r}")
            self._retry_or_fail(record, str(exc))
            return

        if candidate is None:
            self._retry_or_fail(
                record,
                "solver produced no candidate",
                recoverable=not self.config.fast_path_only,
                terminal=self.config.fast_path_only,
            )
            return

        # A canary can fail while this worker is still reasoning. Do not allow
        # its now-stale sibling candidate into the submission queue.
        current_generation = self.gate.generation(record.tile.recipe_key)
        if generation != current_generation:
            blocked_until = self.gate.state(record.tile.recipe_key).blocked_until
            self._retry_or_fail(
                record,
                "recipe generation changed while the tile was solving",
                delay=max(0.0, blocked_until - time.monotonic()),
            )
            return

        verification = self.verifier.verify(candidate)
        candidate = candidate.with_verification(
            verification.accepted, verification.note
        )
        if not verification.accepted:
            self.game.log(f"{record.tile.id}: candidate rejected: {verification.note}")
            self._retry_or_fail(record, verification.note)
            return

        record.candidate = candidate
        record.status = TileStatus.CANDIDATE
        self.game.log(
            f"{record.tile.id}: verified candidate queued "
            f"confidence={candidate.confidence:.2f} source={candidate.source}"
        )
        await self.submitter.enqueue(candidate)

    def _retry_or_fail(
        self,
        record: TileRecord,
        reason: str,
        delay: float | None = None,
        *,
        recoverable: bool = False,
        terminal: bool = False,
    ) -> None:
        record.last_error = reason
        record.candidate = None
        if terminal:
            record.status = TileStatus.FAILED
            self.game.log(f"{record.tile.id}: terminal solve outcome: {reason}")
            return
        attempt_limit = self.config.max_attempts + record.recoveries
        if record.attempts >= attempt_limit:
            if recoverable and record.recoveries < self.config.recovery_attempts:
                record.recoveries += 1
                record.status = TileStatus.RETRY
                record.next_attempt_at = time.monotonic() + max(
                    60.0, delay or 0.0
                )
                self.game.log(
                    f"{record.tile.id}: deferred for recovery attempt "
                    f"{record.recoveries}/{self.config.recovery_attempts}: {reason}"
                )
                return
            record.status = TileStatus.FAILED
            self.game.log(
                f"{record.tile.id}: failed after {record.attempts} attempts: {reason}"
            )
            return
        record.status = TileStatus.RETRY
        retry_delay = (
            delay
            if delay is not None
            else min(60.0, 5.0 * (2 ** max(0, record.attempts - 1)))
        )
        record.next_attempt_at = time.monotonic() + retry_delay

    async def _on_submission_result(
        self, candidate: Candidate, result: dict[str, Any]
    ) -> None:
        record = self.records.get(candidate.task_id)
        if record is None:
            return
        outcome = str(result.get("result") or "")
        self.game.log(f"{candidate.task_id}: submission -> {outcome or result!r}")

        if outcome == "correct":
            if candidate.replay_recipe is not None:
                self.replay_recipes[candidate.recipe_key] = candidate.replay_recipe
            elif not candidate.replayed:
                # A cached script that fell back to a non-replay solution is no
                # longer useful for this generator.
                self.replay_recipes.pop(candidate.recipe_key, None)
            record.status = TileStatus.SOLVED
            record.candidate = None
            self.open_ids.discard(candidate.task_id)
            return
        if outcome in {"already_claimed", "voided", "unknown_task"}:
            record.status = TileStatus.CLAIMED
            record.candidate = None
            self.open_ids.discard(candidate.task_id)
            return
        if outcome == "dry_run":
            record.status = TileStatus.SOLVED
            record.candidate = None
            self.open_ids.discard(candidate.task_id)
            return
        if outcome == "incorrect":
            self.replay_recipes.pop(candidate.recipe_key, None)
            if candidate.strategy.startswith("fast:"):
                handler = candidate.strategy.removeprefix("fast:")
                next_generation = self.gate.generation(candidate.recipe_key)
                self.solver.disable_fast_path(
                    candidate.recipe_key, next_generation, handler
                )
                self.game.log(
                    f"{candidate.task_id}: disabled fast handler={handler} "
                    f"for recipe={candidate.recipe_key} "
                    f"generation={next_generation}"
                )
            blocked_until = self.gate.state(candidate.recipe_key).blocked_until
            for sibling in self.records.values():
                if (
                    sibling.tile.recipe_key == candidate.recipe_key
                    and sibling.status == TileStatus.CANDIDATE
                ):
                    self._retry_or_fail(
                        sibling,
                        f"recipe invalidated by {candidate.task_id}",
                        delay=max(0.0, blocked_until - time.monotonic()),
                    )
            return
        if outcome == "forbidden":
            record.status = TileStatus.FAILED
            record.last_error = "scored submissions must come from the hosted agent"
            return

        self._retry_or_fail(record, f"submission result {outcome or result!r}")

    def _run_once_done(self) -> bool:
        if self._active or self.submitter.pending_count:
            return False
        return all(
            record.status in {TileStatus.SOLVED, TileStatus.CLAIMED, TileStatus.FAILED}
            for record in self.records.values()
        )
