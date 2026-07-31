from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from round2_acceptance import (
    CATEGORIES,
    RUNTIME_FILES,
    analyze_run,
    evaluate_archive,
    evaluate_run,
)


def passing_log() -> str:
    lines = ["12:00:00 [12:00:00] phase changed to 'game'"]
    tasks = []
    for index, category in enumerate(CATEGORIES):
        second = index + 1
        task = f"G-{index}"
        points = (index + 1) * 100
        tasks.append(task)
        lines.append(
            f"12:00:{second:02d} [{second}] {task}: solving "
            f"{category} {points}pt attempt=1"
        )
    for index, task in enumerate(tasks):
        queued = index + 2
        submitted = queued + 3
        lines.append(
            f"12:00:{queued:02d} [{queued}] {task}: "
            "verified candidate queued confidence=0.99"
        )
        lines.append(
            f"12:00:{submitted:02d} [{submitted}] {task}: "
            "submission -> 'correct'"
        )
    return "\n".join(lines)


class Round2AcceptanceTests(unittest.TestCase):
    def test_passing_run_meets_all_log_gates(self) -> None:
        metrics = analyze_run(passing_log(), "game", first_batch_size=4)
        results = evaluate_run(
            metrics,
            first_candidate_limit=3,
            category_start_limit=10,
            queue_p95_limit=15,
            already_claimed_limit=0.10,
            first_batch_size=4,
        )
        self.assertTrue(all(result.passed for result in results), results)

    def test_round1_failure_shape_is_rejected(self) -> None:
        log = "\n".join(
            [
                "12:00:00 [0] phase changed to 'round1'",
                "12:00:01 [1] Q-A2: solving Ancient Scrolls 200pt attempt=1",
                "12:00:01 [1] Q-A2-2: solving Ancient Scrolls 200pt attempt=1",
                "12:00:01 [1] Q-A2-3: solving Ancient Scrolls 200pt attempt=1",
                "12:00:01 [1] Q-A2-4: solving Ancient Scrolls 200pt attempt=1",
                "12:00:05 [5] Q-A2: verified candidate queued confidence=1.00",
                "12:00:25 [25] Q-A2: submission -> 'already_claimed'",
            ]
        )
        metrics = analyze_run(log, "round1", first_batch_size=4)
        results = evaluate_run(
            metrics,
            first_candidate_limit=3,
            category_start_limit=10,
            queue_p95_limit=15,
            already_claimed_limit=0.10,
            first_batch_size=4,
        )
        failed = {result.name for result in results if not result.passed}
        self.assertEqual(
            failed,
            {
                "first_candidate_latency",
                "category_start_latency",
                "first_batch_recipe_diversity",
                "queue_wait_p95",
                "already_claimed_rate",
            },
        )

    def test_archive_must_exactly_match_runtime_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in RUNTIME_FILES:
                Path(root, name).write_bytes(f"source:{name}".encode())
            archive = root / "agent.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                for name in RUNTIME_FILES:
                    bundle.write(root / name, name)
            self.assertTrue(evaluate_archive(archive, root).passed)

            Path(root, "solver.py").write_text("changed", encoding="utf-8")
            result = evaluate_archive(archive, root)
            self.assertFalse(result.passed)
            self.assertIn("solver.py", result.detail)


if __name__ == "__main__":
    unittest.main()
