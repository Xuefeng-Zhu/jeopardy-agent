"""Shared state and value objects for the Jeopardy agent.

These objects deliberately contain no networking or model code. Keeping the
contracts small makes the scheduler and verifier testable without event
credentials.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from threading import Event, Lock
from typing import Any


class TileStatus(str, Enum):
    NEW = "new"
    SOLVING = "solving"
    CANDIDATE = "candidate"
    RETRY = "retry"
    SOLVED = "solved"
    CLAIMED = "claimed"
    FAILED = "failed"


class SolveCancelled(RuntimeError):
    """Raised inside a worker when its tile is no longer claimable."""


class CancellationToken:
    """A small thread-safe cancellation signal shared with blocking workers."""

    def __init__(self) -> None:
        self._event = Event()
        self._lock = Lock()
        self._reason = "cancelled"

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._reason = reason
            self._event.set()

    def check(self) -> None:
        if self.cancelled:
            raise SolveCancelled(self.reason)

    def wait(self, timeout: float) -> bool:
        return self._event.wait(timeout)


@dataclass(frozen=True, slots=True)
class ReplayRecipe:
    """A successful Python extraction that can be tried on sibling variants."""

    code: str
    extraction_regex: str = ""
    regex_group: int = 1
    generator_fingerprint: str = ""
    fingerprint_version: int = 0
    answer_format: str = ""
    scheduler_key: str = ""

    def compatible_with(
        self,
        generator_fingerprint: str,
        fingerprint_version: int,
        answer_format: str,
    ) -> bool:
        return (
            bool(self.generator_fingerprint)
            and self.generator_fingerprint == generator_fingerprint
            and self.fingerprint_version == fingerprint_version
            and self.answer_format == answer_format
        )


class ReplayRecipeCache:
    """Thread-safe recipes keyed by a structural generator fingerprint."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._recipes: dict[str, ReplayRecipe] = {}
        self._scheduler_groups: dict[str, set[str]] = {}

    def lookup(self, generator_fingerprint: str) -> ReplayRecipe | None:
        with self._lock:
            return self._recipes.get(generator_fingerprint)

    def store(self, recipe: ReplayRecipe) -> None:
        if not recipe.generator_fingerprint:
            raise ValueError("replay recipe has no generator fingerprint")
        if not recipe.scheduler_key:
            raise ValueError("replay recipe has no scheduler key")
        with self._lock:
            previous = self._recipes.get(recipe.generator_fingerprint)
            if previous is not None and previous.scheduler_key != recipe.scheduler_key:
                previous_group = self._scheduler_groups.get(previous.scheduler_key)
                if previous_group is not None:
                    previous_group.discard(recipe.generator_fingerprint)
            self._recipes[recipe.generator_fingerprint] = recipe
            self._scheduler_groups.setdefault(recipe.scheduler_key, set()).add(
                recipe.generator_fingerprint
            )

    def discard(self, generator_fingerprint: str) -> None:
        if not generator_fingerprint:
            return
        with self._lock:
            recipe = self._recipes.pop(generator_fingerprint, None)
            if recipe is None:
                return
            group = self._scheduler_groups.get(recipe.scheduler_key)
            if group is not None:
                group.discard(generator_fingerprint)
                if not group:
                    self._scheduler_groups.pop(recipe.scheduler_key, None)

    def discard_scheduler_key(self, scheduler_key: str) -> None:
        with self._lock:
            fingerprints = self._scheduler_groups.pop(scheduler_key, set())
            for fingerprint in fingerprints:
                self._recipes.pop(fingerprint, None)

    def has_scheduler_key(self, scheduler_key: str) -> bool:
        with self._lock:
            return bool(self._scheduler_groups.get(scheduler_key))

    def fingerprints(self, scheduler_key: str) -> frozenset[str]:
        with self._lock:
            return frozenset(self._scheduler_groups.get(scheduler_key, set()))


@dataclass(frozen=True, slots=True)
class Tile:
    id: str
    category: str
    points: int
    title: str = ""
    board: str = ""
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_board(cls, value: dict[str, Any]) -> Tile:
        return cls(
            id=str(value["id"]),
            category=str(value.get("category") or "Unknown"),
            points=int(value.get("points") or 0),
            title=str(value.get("title") or ""),
            board=str(value.get("board") or ""),
            raw=dict(value),
        )

    @property
    def recipe_key(self) -> str:
        # A board cell is one generator family. Prompts and data vary per tile,
        # while category+tier remains stable across its stacked variants.
        return f"{self.category}:{self.points}"


@dataclass(frozen=True, slots=True)
class Candidate:
    task_id: str
    answer: str
    answer_format: str
    category: str
    points: int
    recipe_key: str
    recipe_generation: int
    confidence: float
    evidence: str
    source: str
    strategy: str = "claude"
    verification_source: str = ""
    generator_fingerprint: str = ""
    verified: bool = False
    verification_note: str = ""
    replay_recipe: ReplayRecipe | None = None
    replayed: bool = False
    created_at: float = field(default_factory=time.monotonic)
    not_before: float = 0.0

    @property
    def priority(self) -> float:
        # Submission is the scarce resource. Favor value and confidence, with a
        # small freshness bonus applied by the queue when candidates tie.
        return self.points * max(0.0, min(self.confidence, 1.0))

    def with_verification(self, verified: bool, note: str) -> Candidate:
        return replace(self, verified=verified, verification_note=note)

    def delayed(self, seconds: float) -> Candidate:
        return replace(self, not_before=time.monotonic() + max(0.0, seconds))


@dataclass(slots=True)
class TileRecord:
    tile: Tile
    status: TileStatus = TileStatus.NEW
    attempts: int = 0
    recoveries: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""
    candidate: Candidate | None = None


@dataclass(frozen=True, slots=True)
class Verification:
    accepted: bool
    note: str
