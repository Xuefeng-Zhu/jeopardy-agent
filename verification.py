"""Cheap structural checks that run before any answer can be submitted."""

from __future__ import annotations

import ast
import json
import math
from decimal import Decimal, InvalidOperation

from agent_types import Candidate, Verification


class CandidateVerifier:
    """Fail closed on malformed or weakly-supported candidates.

    This cannot know the hidden answer. Its job is to prevent predictable
    losses: empty output, prose accidentally submitted as a token, malformed
    numerics/literals, and low-confidence guesses.
    """

    def __init__(self, minimum_confidence: float = 0.82) -> None:
        self.minimum_confidence = minimum_confidence

    def verify(self, candidate: Candidate) -> Verification:
        answer = candidate.answer.strip()
        if not answer:
            return Verification(False, "empty answer")
        if len(answer) > 20_000:
            return Verification(False, "answer exceeds 20,000 characters")
        if candidate.confidence < self.minimum_confidence:
            return Verification(
                False,
                f"confidence {candidate.confidence:.2f} below "
                f"{self.minimum_confidence:.2f}",
            )
        if not candidate.evidence.strip():
            return Verification(False, "candidate has no verification evidence")

        fmt = candidate.answer_format or "exact"
        if fmt == "numeric":
            number = answer.replace(",", "").replace("$", "").strip()
            try:
                parsed = Decimal(number)
            except InvalidOperation:
                return Verification(False, f"not a numeric answer: {answer!r}")
            if not math.isfinite(float(parsed)):
                return Verification(False, "numeric answer is not finite")
        elif fmt == "literal":
            try:
                try:
                    json.loads(answer)
                except json.JSONDecodeError:
                    ast.literal_eval(answer)
            except (ValueError, SyntaxError):
                return Verification(False, "literal answer cannot be parsed")
        elif fmt == "validator":
            if candidate.confidence < max(self.minimum_confidence, 0.92):
                return Verification(
                    False, "validator answers require at least 0.92 confidence"
                )
        elif fmt not in {"exact", "exact_ci"}:
            return Verification(False, f"unknown answer format {fmt!r}")

        if candidate.source == "model":
            required = 0.95 if fmt in {"exact", "exact_ci", "validator"} else 0.90
            if candidate.confidence < required:
                return Verification(
                    False,
                    f"model-retyped {fmt} answer requires {required:.2f} confidence",
                )

        return Verification(True, f"{fmt} structure and evidence checks passed")
