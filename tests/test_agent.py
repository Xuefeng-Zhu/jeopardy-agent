from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from agent_types import (
    CancellationToken,
    Candidate,
    SolveCancelled,
    Tile,
    TileRecord,
    TileStatus,
)
from orchestrator import (
    AgentConfig,
    AgentController,
    RecipeGate,
    SubmissionManager,
)
from solver import TileSolver
from tool_runtime import TaskToolRuntime
from verification import CandidateVerifier


def candidate(
    task_id: str = "Q-A5",
    *,
    recipe: str = "Cryptic:500",
    generation: int = 0,
    answer: str = "TOKEN-ABC",
    source: str = "artifact:artifact-0001",
    verification_source: str = "artifact:artifact-0002",
    confidence: float = 0.99,
    answer_format: str = "exact",
) -> Candidate:
    return Candidate(
        task_id=task_id,
        answer=answer,
        answer_format=answer_format,
        category="Cryptic",
        points=500,
        recipe_key=recipe,
        recipe_generation=generation,
        confidence=confidence,
        evidence="computed twice and matched",
        source=source,
        verification_source=verification_source,
    )


class VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.verifier = CandidateVerifier(0.82)

    def test_accepts_artifact_backed_exact_answer(self) -> None:
        result = self.verifier.verify(candidate())
        self.assertTrue(result.accepted, result.note)

    def test_rejects_low_confidence_model_retyping(self) -> None:
        result = self.verifier.verify(candidate(source="model", confidence=0.90))
        self.assertFalse(result.accepted)

    def test_scored_high_value_answer_requires_independent_artifact(self) -> None:
        result = self.verifier.verify(
            candidate(verification_source="")
        )
        self.assertFalse(result.accepted)
        self.assertIn("separate verification artifact", result.note)

    def test_practice_answer_does_not_require_independent_artifact(self) -> None:
        result = self.verifier.verify(
            candidate("PR-A5", verification_source="")
        )
        self.assertTrue(result.accepted, result.note)

    def test_validates_numeric_and_literal_shapes(self) -> None:
        numeric = candidate(answer="$1,234.50", answer_format="numeric")
        literal = candidate(answer='["a", 2]', answer_format="literal")
        self.assertTrue(self.verifier.verify(numeric).accepted)
        self.assertTrue(self.verifier.verify(literal).accepted)
        self.assertFalse(
            self.verifier.verify(
                candidate(answer="many", answer_format="numeric")
            ).accepted
        )


class ToolRuntimeTests(unittest.TestCase):
    def test_universal_tool_response_cap_handles_escape_heavy_text(self) -> None:
        result = TaskToolRuntime._result(ok=True, payload="\\" * 20_000)
        self.assertLessEqual(len(result), 8_000)
        self.assertTrue(json.loads(result)["compacted"])

    def test_artifact_answer_preserves_exact_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            run_result = runtime.execute(
                "run_python",
                {"code": 'print("GHOST-6SAS2HPHXQ5V")'},
            )
            self.assertIn("artifact-0001", run_result)
            record_result = runtime.execute(
                "record_answer",
                {
                    "artifact_id": "artifact-0001",
                    "extraction_regex": r"(GHOST-[A-Z0-9]+)",
                    "regex_group": 1,
                    "confidence": 0.99,
                    "evidence": "decoded and format checked",
                },
            )
            self.assertIn('"recorded": true', record_result)
            self.assertEqual(runtime.recorded_answer.answer, "GHOST-6SAS2HPHXQ5V")

    def test_paths_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            result = runtime.execute("read_file", {"path": "../secret"})
            self.assertIn("path escapes", result)

    def test_python_subprocess_does_not_receive_team_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            result = runtime.execute(
                "run_python",
                {
                    "code": (
                        "import os; print(os.environ.get('TEAM_API_KEY', "
                        "'not-present'))"
                    )
                },
            )
            self.assertIn("not-present", result)

    def test_python_subprocess_can_import_workspace_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "challenge_helper.py").write_text(
                'VALUE = "WORKSPACE-IMPORT-OK"\n',
                encoding="utf-8",
            )
            runtime = TaskToolRuntime(directory, "https://example.test")
            result = runtime.execute(
                "run_python",
                {"code": "import challenge_helper; print(challenge_helper.VALUE)"},
            )
            self.assertIn("WORKSPACE-IMPORT-OK", result)

    def test_cancellation_kills_running_python_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            token = CancellationToken()
            runtime = TaskToolRuntime(
                directory, "https://example.test", cancellation=token
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                started = time.monotonic()
                future = pool.submit(
                    runtime.execute,
                    "run_python",
                    {"code": "import time; time.sleep(10)", "timeout": 20},
                )
                time.sleep(0.15)
                token.cancel("claimed elsewhere")
                with self.assertRaises(SolveCancelled):
                    future.result(timeout=2)
                self.assertLess(time.monotonic() - started, 2)

    def test_inspect_csv_returns_schema_samples_and_aggregates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "events.csv").write_text(
                "kind,duration,owner\n"
                "build,12.5,alpha\n"
                "deploy,7.0,beta\n"
                "build,9.5,alpha\n",
                encoding="utf-8",
            )
            runtime = TaskToolRuntime(directory, "https://example.test")
            result_text = runtime.execute("inspect_file", {"path": "events.csv"})
            result = json.loads(result_text)
            self.assertLessEqual(len(result_text), 8_000)
            self.assertEqual(result["row_count"], 3)
            self.assertEqual(result["column_count"], 3)
            duration = next(
                column for column in result["columns"] if column["name"] == "duration"
            )
            self.assertEqual(duration["type"], "number")
            self.assertEqual(duration["min"], 7.0)
            self.assertEqual(duration["max"], 12.5)

    def test_large_stdout_stays_internal_and_searchable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            result_text = runtime.execute(
                "run_python",
                {"code": 'print("X" * 30000); print("TOKEN-COMPACT-9")'},
            )
            result = json.loads(result_text)
            self.assertLessEqual(len(result_text), 8_000)
            self.assertNotIn("X" * 5_000, result_text)
            artifact_id = result["stdout_artifact"]
            search_text = runtime.execute(
                "search_artifact",
                {"artifact_id": artifact_id, "pattern": r"TOKEN-[A-Z0-9-]+"},
            )
            self.assertLessEqual(len(search_text), 8_000)
            self.assertIn("TOKEN-COMPACT-9", search_text)
            runtime.execute(
                "record_answer",
                {
                    "artifact_id": artifact_id,
                    "extraction_regex": r"(TOKEN-[A-Z0-9-]+)",
                    "confidence": 0.99,
                    "evidence": "located in compactly searched stdout",
                },
            )
            self.assertEqual(runtime.recorded_answer.answer, "TOKEN-COMPACT-9")

    def test_subprocess_output_is_bounded_before_artifact_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            result = json.loads(
                runtime.execute(
                    "run_python",
                    {"code": 'import sys; sys.stdout.write("X" * 400000)'},
                )
            )
            self.assertTrue(result["stdout_artifact_truncated"])
            artifact = runtime.artifacts.get(result["stdout_artifact"])
            self.assertEqual(len(artifact), 250_000)

    def test_verification_artifact_must_be_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            answer_result = json.loads(
                runtime.execute("run_python", {"code": 'print("TOKEN-OK")'})
            )
            answer_artifact = answer_result["stdout_artifact"]
            rejected = runtime.execute(
                "record_answer",
                {
                    "artifact_id": answer_artifact,
                    "verification_artifact_id": answer_artifact,
                    "confidence": 0.99,
                    "evidence": "same artifact",
                },
            )
            self.assertIn("must be independent", rejected)
            verification_result = json.loads(
                runtime.execute("run_python", {"code": 'print("verified")'})
            )
            accepted = runtime.execute(
                "record_answer",
                {
                    "artifact_id": answer_artifact,
                    "verification_artifact_id": verification_result[
                        "stdout_artifact"
                    ],
                    "confidence": 0.99,
                    "evidence": "independent check",
                },
            )
            self.assertIn('"recorded": true', accepted)

    def test_large_file_reads_have_a_compact_total_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "large.log").write_text("A" * 20_000, encoding="utf-8")
            runtime = TaskToolRuntime(directory, "https://example.test")
            for offset in (0, 4_000, 8_000):
                result = runtime.execute(
                    "read_file",
                    {"path": "large.log", "offset": offset, "limit": 4_000},
                )
                self.assertLessEqual(len(result), 8_000)
            blocked = runtime.execute(
                "read_file",
                {"path": "large.log", "offset": 12_000, "limit": 4_000},
            )
            self.assertIn("compact read budget exhausted", blocked)

    def test_html_summary_returns_forms_links_and_short_text(self) -> None:
        body = (
            "<html><head><title>Login</title></head><body>"
            '<form method="post" action="/session">'
            '<input name="csrf" value="abc"><input name="email"></form>'
            '<a href="/help">Help</a>' + ("long text " * 2_000) + "</body></html>"
        )
        summary = TaskToolRuntime._summarize_http_body(body, "text/html")
        serialized = json.dumps(summary)
        self.assertEqual(summary["title"], "Login")
        self.assertEqual(summary["forms"][0]["action"], "/session")
        self.assertEqual(summary["links"][0]["href"], "/help")
        self.assertLess(len(serialized), 8_000)


class FakeMessages:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = responses
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeSolverGame:
    BASE = "https://example.test"
    MODEL = "test-model"

    def __init__(self, directory: str) -> None:
        first = SimpleNamespace(
            type="tool_use",
            id="tool-1",
            name="run_python",
            input={"code": 'print("TOKEN-ABC")'},
        )
        second = SimpleNamespace(
            type="tool_use",
            id="tool-2",
            name="record_answer",
            input={
                "artifact_id": "artifact-0001",
                "extraction_regex": r"(TOKEN-[A-Z]+)",
                "regex_group": 1,
                "confidence": 0.99,
                "evidence": "script emitted and pattern checked",
            },
        )
        self.messages = FakeMessages(
            [
                SimpleNamespace(content=[first]),
                SimpleNamespace(content=[second]),
            ]
        )
        self.client = SimpleNamespace(messages=self.messages)
        self.directory = Path(directory)
        self.logs: list[str] = []

    def task(self, _task_id: str) -> dict:
        return {
            "prompt": "Compute the token using Python.",
            "category": "Cryptic",
            "points": 500,
            "answer_format": "exact",
            "files": [],
        }

    def workdir(self, _task_id: str) -> Path:
        return self.directory

    @staticmethod
    def fetch_files(_task_id: str, _detail: dict) -> list[str]:
        return []

    def anthropic_client(self):
        return self.client

    def log(self, *values) -> None:
        self.logs.append(" ".join(map(str, values)))


class SolverTests(unittest.TestCase):
    def test_tool_loop_returns_artifact_backed_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game = FakeSolverGame(directory)
            solver = TileSolver(game, max_turns=4)
            result = solver.solve(
                Tile(id="PR-C5", category="Cryptic", points=500),
                recipe_generation=0,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result.answer, "TOKEN-ABC")
            self.assertEqual(result.source, "artifact:artifact-0001")
            self.assertIsNotNone(result.replay_recipe)
            self.assertEqual(len(game.messages.calls), 2)
            self.assertTrue(
                any("solver summary outcome=recorded" in line for line in game.logs)
            )

            replayed = solver.solve(
                Tile(id="PR-C5-b", category="Cryptic", points=500),
                recipe_generation=0,
                replay_recipe=result.replay_recipe,
            )
            self.assertEqual(replayed.answer, "TOKEN-ABC")
            self.assertTrue(replayed.replayed)
            self.assertEqual(len(game.messages.calls), 2)

    def test_turn_budget_is_tier_aware(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            solver = TileSolver(FakeSolverGame(directory), max_turns=12)
            self.assertEqual(solver._turn_budget(300), 8)
            self.assertEqual(solver._turn_budget(400), 10)
            self.assertEqual(solver._turn_budget(500), 12)

    def test_streamed_model_call_closes_when_cancelled(self) -> None:
        class Stream:
            def __init__(self) -> None:
                self.started = Event()
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                self.closed = True

            def __iter__(self):
                self.started.set()
                while True:
                    time.sleep(0.01)
                    yield object()

            @staticmethod
            def get_final_message():
                raise AssertionError("cancelled stream must not complete")

        stream = Stream()
        messages = SimpleNamespace(stream=lambda **_kwargs: stream)
        client = SimpleNamespace(messages=messages)
        token = CancellationToken()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(TileSolver._model_call, client, {}, token)
            self.assertTrue(stream.started.wait(timeout=1))
            token.cancel("claimed elsewhere")
            with self.assertRaises(SolveCancelled):
                future.result(timeout=1)
        self.assertTrue(stream.closed)

    def test_old_tool_turns_are_compacted_but_recent_turn_is_preserved(self) -> None:
        old_result = "artifact-0007 " + ("R" * 10_000)
        recent_result = "recent evidence"
        messages = [
            {"role": "user", "content": "task"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "old-tool",
                        "name": "run_python",
                        "input": {"code": "print(1)\n" + ("x" * 5_000)},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "old-tool",
                        "content": old_result,
                    }
                ],
            },
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "recent-tool",
                        "name": "search_artifact",
                        "input": {"artifact_id": "artifact-0007", "pattern": "token"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "recent-tool",
                        "content": recent_result,
                    }
                ],
            },
        ]
        TileSolver._compact_history(messages)
        self.assertLess(len(messages[1]["content"][0]["input"]["code"]), 100)
        compacted_result = messages[2]["content"][0]["content"]
        self.assertLess(len(compacted_result), 2_000)
        self.assertIn("artifact-0007", compacted_result)
        self.assertEqual(messages[4]["content"][0]["content"], recent_result)


class RecipeGateTests(unittest.TestCase):
    def test_scored_phase_holds_sibling_until_canary_passes(self) -> None:
        gate = RecipeGate()
        gate.set_phase("round1")
        first = candidate("Q-C5-a")
        sibling = candidate("Q-C5-b")
        now = time.monotonic()
        self.assertTrue(gate.can_submit(first, now))
        self.assertFalse(gate.can_submit(sibling, now))
        gate.on_correct(first)
        self.assertTrue(gate.can_submit(sibling, now))

    def test_wrong_canary_invalidates_generation(self) -> None:
        gate = RecipeGate()
        gate.set_phase("game")
        first = candidate("C5-a")
        gate.on_incorrect(first, 30)
        self.assertFalse(gate.is_current(first))
        self.assertEqual(gate.generation(first.recipe_key), 1)


class SubmissionManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_lane_releases_sibling_after_correct_canary(self) -> None:
        class Game:
            def __init__(self) -> None:
                self.submitted: list[str] = []

            def submit(self, task_id: str, _answer: str) -> dict:
                self.submitted.append(task_id)
                return {"result": "correct"}

            @staticmethod
            def log(*_values) -> None:
                return None

        game = Game()
        gate = RecipeGate()
        gate.set_phase("round1")
        results: list[str] = []
        finished = asyncio.Event()

        async def on_result(value: Candidate, _result: dict) -> None:
            results.append(value.task_id)
            if len(results) == 2:
                finished.set()

        manager = SubmissionManager(
            game,
            AgentConfig(submit_interval=0.01),
            gate,
            lambda _task_id: True,
            on_result,
        )
        runner = asyncio.create_task(manager.run())
        await manager.enqueue(candidate("Q-C5-a"))
        await manager.enqueue(candidate("Q-C5-b"))
        await asyncio.wait_for(finished.wait(), timeout=2)
        await manager.stop()
        await asyncio.wait_for(runner, timeout=2)
        self.assertEqual(game.submitted, ["Q-C5-a", "Q-C5-b"])


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _controller_for_unit_tests() -> AgentController:
        class Game:
            BASE = "https://example.test"
            MODEL = "test-model"

            def __init__(self) -> None:
                self.logs: list[str] = []

            def log(self, *values) -> None:
                self.logs.append(" ".join(map(str, values)))

        return AgentController(Game(), AgentConfig(model_workers=4))

    async def test_scheduler_reserves_workers_for_fast_and_high_tiers(self) -> None:
        controller = self._controller_for_unit_tests()
        records = [
            TileRecord(Tile(f"T-{points}", f"Category-{points}", points))
            for points in (100, 200, 300, 400, 500)
        ]
        controller.records = {record.tile.id: record for record in records}
        selected = controller._select_records(records, room=4)
        self.assertEqual(len(selected), 4)
        self.assertEqual(
            sum(record.tile.points >= 400 for record in selected),
            2,
        )
        self.assertEqual(
            sum(record.tile.points < 400 for record in selected),
            2,
        )

    async def test_no_candidate_exhaustion_gets_one_deferred_recovery(self) -> None:
        controller = self._controller_for_unit_tests()
        record = TileRecord(
            Tile("PR-C5", "Cryptic", 500),
            status=TileStatus.SOLVING,
            attempts=controller.config.max_attempts,
        )
        controller._retry_or_fail(
            record, "solver produced no candidate", recoverable=True
        )
        self.assertEqual(record.status, TileStatus.RETRY)
        self.assertEqual(record.recoveries, 1)
        self.assertGreater(record.next_attempt_at, time.monotonic())

        record.attempts += 1
        controller._retry_or_fail(
            record, "solver produced no candidate", recoverable=True
        )
        self.assertEqual(record.status, TileStatus.FAILED)

    async def test_terminal_fast_path_miss_is_not_retried(self) -> None:
        controller = self._controller_for_unit_tests()
        record = TileRecord(
            Tile("PR-H5", "Heavy Compute", 500),
            status=TileStatus.SOLVING,
            attempts=1,
        )
        controller._retry_or_fail(
            record,
            "solver produced no candidate",
            recoverable=True,
            terminal=True,
        )
        self.assertEqual(record.status, TileStatus.FAILED)
        self.assertEqual(record.recoveries, 0)

    async def test_run_once_dry_run_completes_multiple_tiles(self) -> None:
        class Game:
            BASE = "https://example.test"
            MODEL = "test-model"

            def __init__(self) -> None:
                self.logs: list[str] = []

            @staticmethod
            def board() -> dict:
                return {"phase": "practice"}

            @staticmethod
            def open_tiles(_board: dict) -> list[dict]:
                return [
                    {
                        "id": "PR-C5-a",
                        "category": "Cryptic",
                        "points": 500,
                    },
                    {
                        "id": "PR-H4-a",
                        "category": "Heavy Compute",
                        "points": 400,
                    },
                ]

            def log(self, *values) -> None:
                self.logs.append(" ".join(map(str, values)))

        class Solver:
            @staticmethod
            def solve(
                tile: Tile,
                generation: int,
                _replay=None,
                _cancellation=None,
                *,
                attempt: int = 1,
            ) -> Candidate:
                return Candidate(
                    task_id=tile.id,
                    answer="42",
                    answer_format="numeric",
                    category=tile.category,
                    points=tile.points,
                    recipe_key=tile.recipe_key,
                    recipe_generation=generation,
                    confidence=0.99,
                    evidence="computed independently twice",
                    source="artifact:artifact-0001",
                )

        game = Game()
        controller = AgentController(
            game,
            AgentConfig(
                model_workers=2,
                run_once=True,
                dry_run=True,
                board_poll_seconds=0.01,
                submit_interval=0.01,
            ),
        )
        controller.solver = Solver()
        await asyncio.wait_for(controller.run(), timeout=2)
        self.assertEqual(
            {record.status.value for record in controller.records.values()},
            {"solved"},
        )

    async def test_board_poll_cancels_worker_when_tile_is_claimed(self) -> None:
        class Game:
            BASE = "https://example.test"
            MODEL = "test-model"

            def __init__(self) -> None:
                self.polls = 0
                self.logs: list[str] = []

            def board(self) -> dict:
                self.polls += 1
                return {"phase": "practice", "tile_open": self.polls == 1}

            @staticmethod
            def open_tiles(board: dict) -> list[dict]:
                if not board["tile_open"]:
                    return []
                return [
                    {
                        "id": "PR-H5-a",
                        "category": "Heavy Compute",
                        "points": 500,
                    }
                ]

            def log(self, *values) -> None:
                self.logs.append(" ".join(map(str, values)))

        class BlockingSolver:
            def __init__(self) -> None:
                self.started = Event()
                self.stopped = Event()

            def solve(
                self,
                _tile: Tile,
                _generation: int,
                _replay,
                cancellation: CancellationToken,
                *,
                attempt: int = 1,
            ) -> None:
                self.started.set()
                try:
                    while True:
                        cancellation.check()
                        cancellation.wait(0.01)
                finally:
                    self.stopped.set()

        game = Game()
        solver = BlockingSolver()
        controller = AgentController(
            game,
            AgentConfig(
                model_workers=1,
                run_once=True,
                board_poll_seconds=0.01,
                submit_interval=0.01,
            ),
        )
        controller.solver = solver
        started = time.monotonic()
        await asyncio.wait_for(controller.run(), timeout=2)
        self.assertTrue(solver.started.is_set())
        self.assertTrue(solver.stopped.is_set())
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(controller.records["PR-H5-a"].status.value, "claimed")
        self.assertTrue(
            any("cancelling active work" in message for message in game.logs)
        )


if __name__ == "__main__":
    unittest.main()
