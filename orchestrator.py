"""Concurrent board controller and globally throttled submission lane."""

from __future__ import annotations

import asyncio
import inspect
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from agent_types import (
    CancellationToken,
    Candidate,
    ReplayRecipeCache,
    SolveCancelled,
    Tile,
    TileRecord,
    TileStatus,
)
from fast_paths import (
    fast_path_expected_seconds,
    is_cpu_heavy_fast_path_title,
    is_registered_fast_path_title,
)
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
    recipe_backoff_seconds: float = 30.0
    board_poll_seconds: float = 2.0
    submit_interval: float = 3.15
    submission_soft_deadline: float = 15.0
    minimum_confidence: float = 0.82
    candidate_buffer: int = 16
    run_once: bool = False
    dry_run: bool = False
    fast_paths: bool = True
    fast_path_only: bool = False
    cpu_heavy_workers: int = 1

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
            recipe_backoff_seconds=max(
                5.0, float(os.environ.get("RECIPE_BACKOFF_SECONDS", "30"))
            ),
            board_poll_seconds=max(
                0.5, float(os.environ.get("BOARD_POLL_SECONDS", "2"))
            ),
            submit_interval=max(3.05, float(os.environ.get("SUBMIT_INTERVAL", "3.15"))),
            submission_soft_deadline=max(
                1.0,
                float(os.environ.get("SUBMISSION_SOFT_DEADLINE_SECONDS", "15")),
            ),
            minimum_confidence=min(
                1.0, max(0.0, float(os.environ.get("MIN_CONFIDENCE", "0.82")))
            ),
            candidate_buffer=max(1, int(os.environ.get("CANDIDATE_BUFFER", "16"))),
            run_once=_env_bool("RUN_ONCE"),
            dry_run=_env_bool("DRY_RUN"),
            fast_paths=_env_bool("FAST_PATHS", default=True),
            fast_path_only=_env_bool("FAST_PATH_ONLY"),
            cpu_heavy_workers=max(
                1, int(os.environ.get("CPU_HEAVY_WORKERS", "1"))
            ),
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
ChangeHandler = Callable[[], None]


class SubmissionManager:
    def __init__(
        self,
        game: Any,
        config: AgentConfig,
        gate: RecipeGate,
        is_open: OpenCheck,
        on_result: ResultHandler,
        on_change: ChangeHandler | None = None,
    ) -> None:
        self.game = game
        self.config = config
        self.gate = gate
        self.is_open = is_open
        self.on_result = on_result
        self.on_change = on_change or (lambda: None)
        self._pending: list[Candidate] = []
        self._condition = asyncio.Condition()
        self._stopping = False
        self._stop_event = asyncio.Event()
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
        self.on_change()

    async def stop(self, *, discard_pending: bool = True) -> None:
        async with self._condition:
            self._stopping = True
            self._stop_event.set()
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
            if not self.gate.is_current(candidate):
                self.gate.on_neutral(candidate)
                await self.on_result(candidate, {"result": "invalidated"})
                await self._notify()
                continue

            wait = self.config.submit_interval - (time.monotonic() - self._last_submit)
            if wait > 0:
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                except TimeoutError:
                    pass
                if self._stopping:
                    self.gate.on_neutral(candidate)
                    return

            # A board update can claim or close the tile while it waits for the
            # single submission lane.
            if not self.is_open(candidate.task_id):
                self.gate.on_neutral(candidate)
                await self.on_result(candidate, {"result": "already_claimed"})
                await self._notify()
                continue
            if not self.gate.is_current(candidate):
                self.gate.on_neutral(candidate)
                await self.on_result(candidate, {"result": "invalidated"})
                await self._notify()
                continue

            try:
                if self.config.dry_run:
                    result = {"result": "dry_run"}
                else:
                    result = await asyncio.to_thread(
                        self.game.submit, candidate.task_id, candidate.answer
                    )
                self._last_submit = time.monotonic()
            except Exception as exc:
                if type(exc).__name__ == "AuthError":
                    raise
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
                    key=lambda item: (
                        item.deadline_priority(
                            now, self.config.submission_soft_deadline
                        ),
                        item.priority,
                        -item.created_at,
                    ),
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
                    self.on_change()
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
        self.on_change()

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
        self.replay_cache = ReplayRecipeCache()
        self.replay_validations: dict[str, int] = {}
        self.solver = TileSolver(
            game,
            max_turns=config.max_turns,
            max_tokens=config.max_tokens,
            verbose=config.verbose,
            fast_paths=config.fast_paths,
            fast_path_only=config.fast_path_only,
            replay_cache=self.replay_cache,
            replay_allowed=self._replay_allowed,
        )
        self.records: dict[str, TileRecord] = {}
        self._recipe_backoff_until: dict[str, float] = {}
        self._recipe_exhaustions: dict[str, int] = {}
        self._family_failure_hints: dict[str, str] = {}
        self.open_ids: set[str] = set()
        self.phase = ""
        self._active: dict[str, asyncio.Task[None]] = {}
        self._cancel_tokens: dict[str, CancellationToken] = {}
        self._selected_ids: set[str] | None = None
        self._fatal_error: BaseException | None = None
        self._wake_event = asyncio.Event()
        self.submitter = SubmissionManager(
            game,
            config,
            self.gate,
            self.is_open,
            self._on_submission_result,
            self._wake_scheduler,
        )

    def is_open(self, task_id: str) -> bool:
        return task_id in self.open_ids

    async def run(self) -> None:
        submit_task = asyncio.create_task(self.submitter.run())
        submit_task.add_done_callback(lambda _task: self._wake_scheduler())
        first_sync = False
        board_errors = 0
        next_board_at = 0.0
        try:
            while True:
                self._wake_event.clear()
                if self._fatal_error is not None:
                    raise self._fatal_error
                if submit_task.done():
                    submit_error = submit_task.exception()
                    if submit_error is not None:
                        raise submit_error
                    raise RuntimeError("submission manager stopped unexpectedly")
                now = time.monotonic()
                if now >= next_board_at:
                    try:
                        board = await asyncio.to_thread(self.game.board)
                        board_errors = 0
                        self._sync_board(board)
                        first_sync = True
                    except Exception as exc:
                        if type(exc).__name__ == "AuthError":
                            raise
                        board_errors += 1
                        self.game.log(
                            f"board poll failed ({board_errors}): {exc!r}; continuing"
                        )
                    interval = min(
                        10.0,
                        self.config.board_poll_seconds * (1 + board_errors),
                    )
                    next_board_at = time.monotonic() + interval

                self._schedule()

                if self.config.run_once and first_sync and self._run_once_done():
                    break
                wake_at = min(next_board_at, self._next_scheduler_wake())
                timeout = max(0.01, wake_at - time.monotonic())
                try:
                    await asyncio.wait_for(self._wake_event.wait(), timeout=timeout)
                except TimeoutError:
                    pass
        finally:
            for token in self._cancel_tokens.values():
                token.cancel("agent controller is shutting down")
            if self._active:
                await asyncio.gather(*self._active.values(), return_exceptions=True)
            await self.submitter.stop()
            await asyncio.gather(submit_task, return_exceptions=True)
            close_solver = getattr(self.solver, "close", None)
            if callable(close_solver):
                close_solver()

    def _wake_scheduler(self) -> None:
        self._wake_event.set()

    def _next_scheduler_wake(self) -> float:
        now = time.monotonic()
        wake_times = [
            record.next_attempt_at
            for record in self.records.values()
            if record.status == TileStatus.RETRY and record.next_attempt_at > now
        ]
        wake_times.extend(
            value for value in self._recipe_backoff_until.values() if value > now
        )
        return min(wake_times) if wake_times else now + 60.0

    def _sync_board(self, board: dict[str, Any]) -> None:
        phase = str(board.get("phase") or "")
        if phase != self.phase:
            self.phase = phase
            self.gate.set_phase(phase)
            self._recipe_backoff_until.clear()
            self._recipe_exhaustions.clear()
            self._family_failure_hints.clear()
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
                self._discard_task_state(task_id)

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
        """Greedily cover the board lanes most likely to disappear first.

        The board is a race, so fixed family/tier batches are a poor proxy for
        value: they can spend a whole wave on comfortable cells while a nearly
        exhausted cell disappears. Selection instead uses marginal coverage.
        Each added worker is scored against the categories and claim lanes
        already covered by active work and by earlier picks in this wave.
        """

        if room <= 0:
            return []
        busy_recipes = {
            record.tile.recipe_key
            for record in self.records.values()
            if record.status in {TileStatus.SOLVING, TileStatus.CANDIDATE}
        }
        covered_categories = {
            record.tile.category
            for record in self.records.values()
            if record.status in {TileStatus.SOLVING, TileStatus.CANDIDATE}
        }
        covered_recipes = set(busy_recipes)
        selected: list[TileRecord] = []
        selected_recipes: set[str] = set()
        active_cpu_heavy = sum(
            1
            for record in self.records.values()
            if record.status in {TileStatus.SOLVING, TileStatus.CANDIDATE}
            and is_cpu_heavy_fast_path_title(record.tile.title)
        )
        now = time.monotonic()

        def can_select(record: TileRecord) -> bool:
            selected_cpu_heavy = sum(
                is_cpu_heavy_fast_path_title(item.tile.title)
                for item in selected
            )
            if (
                is_cpu_heavy_fast_path_title(record.tile.title)
                and active_cpu_heavy + selected_cpu_heavy
                >= min(
                    self.config.model_workers,
                    self.config.cpu_heavy_workers,
                )
            ):
                return False
            recipe_key = record.tile.recipe_key
            if self._recipe_backoff_until.get(recipe_key, 0.0) > now:
                return False
            has_replay = self.replay_cache.has_scheduler_key(recipe_key)
            has_success = self.gate.state(recipe_key).successes > 0
            return not (
                not has_replay
                and not has_success
                and (recipe_key in busy_recipes or recipe_key in selected_recipes)
            )

        remaining = list(eligible)
        while remaining and len(selected) < room:
            selectable = [record for record in remaining if can_select(record)]
            if not selectable:
                break
            uncovered_categories = [
                record
                for record in selectable
                if record.tile.category not in covered_categories
            ]
            uncovered_recipes = [
                record
                for record in selectable
                if record.tile.recipe_key not in covered_recipes
            ]
            record = max(
                uncovered_categories or uncovered_recipes or selectable,
                key=lambda item: (
                    self._race_priority(
                        item, covered_categories, covered_recipes
                    ),
                    item.tile.points,
                    -item.attempts,
                    item.tile.id,
                ),
            )
            selected.append(record)
            selected_recipes.add(record.tile.recipe_key)
            covered_categories.add(record.tile.category)
            covered_recipes.add(record.tile.recipe_key)
            remaining.remove(record)
        return selected

    def _race_priority(
        self,
        record: TileRecord,
        covered_categories: set[str],
        covered_recipes: set[str],
    ) -> float:
        """Expected points per second, adjusted for contention and coverage."""

        score = self._solve_priority(record)
        tile = record.tile
        if is_registered_fast_path_title(tile.title):
            score *= 2.0
            # Unique high tiers disappear first and carried most of the points
            # missed in Round 1. Preserve expected-yield ordering within a
            # tier, while ensuring a validated 500 is not displaced by a deep
            # stack of cheap lower-value siblings.
            if tile.points >= 500:
                score *= 2.5
            elif tile.points >= 400:
                score *= 1.5
        if self.replay_cache.has_scheduler_key(tile.recipe_key):
            score *= 1.5
        score *= self._claim_pressure(tile)
        if tile.category not in covered_categories:
            score *= 2.0
        if tile.recipe_key not in covered_recipes:
            score *= 1.25
        return score

    @staticmethod
    def _claim_pressure(tile: Tile) -> float:
        """Estimate urgency from how much of a board cell has disappeared."""

        try:
            total = int(tile.raw.get("total") or 0)
            remaining = int(tile.raw.get("remaining") or 0)
        except (TypeError, ValueError):
            return 1.0
        if total <= 0 or remaining <= 0 or remaining > total:
            return 1.0
        claimed_fraction = (total - remaining) / total
        scarcity = 1.0 / remaining
        return 1.0 + (1.25 * claimed_fraction) + (0.25 * scarcity)

    def _solve_priority(self, record: TileRecord) -> float:
        expected_seconds = fast_path_expected_seconds(record.tile.title)
        if expected_seconds is None:
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
        self._wake_scheduler()

    def _replay_allowed(self, generator_fingerprint: str) -> bool:
        return (
            self.phase == "practice"
            or self.replay_validations.get(generator_fingerprint, 0) >= 1
        )

    async def _solve(self, record: TileRecord, cancellation: CancellationToken) -> None:
        record.status = TileStatus.SOLVING
        record.attempts += 1
        generation = self.gate.generation(record.tile.recipe_key)
        self.game.log(
            f"{record.tile.id}: solving {record.tile.category} "
            f"{record.tile.points}pt attempt={record.attempts}"
        )
        try:
            family_failure_hint = self._family_failure_hints.get(
                record.tile.recipe_key, ""
            )
            solve_kwargs: dict[str, Any] = {"attempt": record.attempts}
            parameters = inspect.signature(self.solver.solve).parameters.values()
            accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
            parameter_names = {parameter.name for parameter in parameters}
            if family_failure_hint and (
                accepts_kwargs or "family_failure_hint" in parameter_names
            ):
                solve_kwargs["family_failure_hint"] = family_failure_hint
            if record.last_error and (
                accepts_kwargs or "prior_failure_hint" in parameter_names
            ):
                solve_kwargs["prior_failure_hint"] = record.last_error
            candidate = await asyncio.to_thread(
                self.solver.solve,
                record.tile,
                generation,
                None,
                cancellation,
                **solve_kwargs,
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
                self._wake_scheduler()
                return
            self.game.log(f"{record.tile.id}: solver error {exc!r}")
            self._retry_or_fail(record, str(exc))
            return

        if candidate is None:
            report_getter = getattr(self.solver, "pop_failure_report", None)
            report = report_getter(record.tile.id) if callable(report_getter) else ""
            if report:
                self._family_failure_hints[record.tile.recipe_key] = report
                self.game.log(
                    f"{record.tile.id}: stored compact failure strategy for siblings"
                )
            delay = self._defer_recipe_after_exhaustion(record)
            self._retry_or_fail(
                record,
                "solver produced no candidate",
                delay=delay,
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

        try:
            verification = self.verifier.verify(candidate)
        except Exception as exc:  # noqa: BLE001 - a bad verifier must not strand a tile
            self.game.log(f"{record.tile.id}: verifier error {exc!r}")
            self._retry_or_fail(record, f"verifier error: {exc}")
            return
        candidate = candidate.with_verification(
            verification.accepted, verification.note
        )
        if not verification.accepted:
            self.game.log(f"{record.tile.id}: candidate rejected: {verification.note}")
            if candidate.strategy.startswith("fast:"):
                self.solver.disable_fast_path(
                    candidate.recipe_key,
                    generation,
                    candidate.strategy.removeprefix("fast:"),
                )
            self._retry_or_fail(record, verification.note)
            return

        record.candidate = candidate
        record.status = TileStatus.CANDIDATE
        await self.submitter.enqueue(candidate)
        self.game.log(
            f"{record.tile.id}: verified candidate queued "
            f"confidence={candidate.confidence:.2f} source={candidate.source} "
            f"pending={self.submitter.pending_count}"
        )

    def _defer_recipe_after_exhaustion(self, record: TileRecord) -> float:
        """Avoid spending adjacent workers on the same recently exhausted family."""

        recipe_key = record.tile.recipe_key
        failures = self._recipe_exhaustions.get(recipe_key, 0) + 1
        self._recipe_exhaustions[recipe_key] = failures
        delay = min(
            120.0,
            self.config.recipe_backoff_seconds * (2 ** max(0, failures - 1)),
        )
        self._recipe_backoff_until[recipe_key] = max(
            self._recipe_backoff_until.get(recipe_key, 0.0),
            time.monotonic() + delay,
        )
        self.game.log(
            f"{record.tile.id}: recipe family deferred for {delay:.1f}s "
            f"after exhaustion count={failures}"
        )
        return delay

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
            self._discard_task_state(record.tile.id)
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
                    f"{record.recoveries}/{self.config.recovery_attempts} "
                    f"retry_in={max(60.0, delay or 0.0):.1f}s: {reason}"
                )
                return
            record.status = TileStatus.FAILED
            self._discard_task_state(record.tile.id)
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
        self.game.log(
            f"{record.tile.id}: retry scheduled attempt={record.attempts + 1} "
            f"retry_in={retry_delay:.1f}s reason={reason}"
        )

    async def _on_submission_result(
        self, candidate: Candidate, result: dict[str, Any]
    ) -> None:
        record = self.records.get(candidate.task_id)
        if record is None:
            return
        outcome = str(result.get("result") or "")
        self.game.log(f"{candidate.task_id}: submission -> {outcome or result!r}")

        if outcome == "correct":
            self._recipe_backoff_until.pop(candidate.recipe_key, None)
            self._recipe_exhaustions.pop(candidate.recipe_key, None)
            self._family_failure_hints.pop(candidate.recipe_key, None)
            if candidate.replayed:
                fingerprint = candidate.generator_fingerprint
                if fingerprint:
                    self.replay_validations[fingerprint] = (
                        self.replay_validations.get(fingerprint, 0) + 1
                    )
            elif candidate.replay_recipe is not None:
                previous = self.replay_cache.lookup(
                    candidate.replay_recipe.generator_fingerprint
                )
                if previous != candidate.replay_recipe:
                    self.replay_validations[
                        candidate.replay_recipe.generator_fingerprint
                    ] = 0
                self.replay_cache.store(candidate.replay_recipe)
            record.status = TileStatus.SOLVED
            record.candidate = None
            self.open_ids.discard(candidate.task_id)
            self._discard_task_state(candidate.task_id)
            return
        if outcome in {"already_claimed", "voided", "unknown_task"}:
            record.status = TileStatus.CLAIMED
            record.candidate = None
            self.open_ids.discard(candidate.task_id)
            self._discard_task_state(candidate.task_id)
            return
        if outcome == "dry_run":
            record.status = TileStatus.SOLVED
            record.candidate = None
            self.open_ids.discard(candidate.task_id)
            self._discard_task_state(candidate.task_id)
            return
        if outcome == "incorrect":
            self._family_failure_hints[candidate.recipe_key] = (
                f"submission strategy={candidate.strategy} was incorrect; "
                "do not reuse that exact approach or candidate derivation"
            )
            self.replay_cache.discard(candidate.generator_fingerprint)
            self.replay_validations.pop(candidate.generator_fingerprint, None)
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
            self._discard_task_state(candidate.task_id)
            return

        self._retry_or_fail(record, f"submission result {outcome or result!r}")

    def _discard_task_state(self, task_id: str) -> None:
        discard = getattr(self.solver, "discard_task_state", None)
        if callable(discard):
            discard(task_id)

    def _run_once_done(self) -> bool:
        if self._active or self.submitter.pending_count:
            return False
        return all(
            record.status in {TileStatus.SOLVED, TileStatus.CLAIMED, TileStatus.FAILED}
            for record in self.records.values()
        )
