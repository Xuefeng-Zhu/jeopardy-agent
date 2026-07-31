from __future__ import annotations

import asyncio
import gzip
import io
import json
import tarfile
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from agent_types import (
    CancellationToken,
    Candidate,
    ReplayRecipe,
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
    replayed: bool = False,
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
        replayed=replayed,
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

    def test_scored_replay_still_requires_independent_artifact(self) -> None:
        result = self.verifier.verify(
            candidate(verification_source="", replayed=True)
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

    def test_rejects_nonfinite_decimal_without_raising(self) -> None:
        for value in ("NaN", "sNaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                result = self.verifier.verify(
                    candidate(answer=value, answer_format="numeric")
                )
                self.assertFalse(result.accepted)
                self.assertIn("not finite", result.note)


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

    def test_artifact_answer_defaults_to_full_regex_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            artifact_id = runtime.artifacts.put("Final token: VAULT-ABC123")
            result = runtime.execute(
                "record_answer",
                {
                    "artifact_id": artifact_id,
                    "extraction_regex": r"VAULT-[A-Z0-9]+",
                    "confidence": 0.99,
                    "evidence": "matched the explicit final token",
                },
            )
            self.assertIn('"recorded": true', result)
            self.assertEqual(runtime.recorded_answer.answer, "VAULT-ABC123")

    def test_paths_cannot_escape_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            result = runtime.execute("read_file", {"path": "../secret"})
            self.assertIn("path escapes", result)

    def test_read_file_seeks_to_bounded_byte_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "large.txt")
            path.write_bytes((b"x" * 2_000_000) + b"TAIL")
            runtime = TaskToolRuntime(directory, "https://example.test")
            result = json.loads(
                runtime.execute(
                    "read_file",
                    {"path": "large.txt", "offset": 2_000_000, "limit": 4},
                )
            )
            self.assertEqual(result["sample"], "TAIL")
            self.assertEqual(result["returned_bytes"], 4)
            self.assertFalse(result["truncated"])

    def test_regex_tools_reject_nested_quantifiers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "text.txt").write_text("a" * 100_000 + "!", encoding="utf-8")
            runtime = TaskToolRuntime(directory, "https://example.test")
            started = time.monotonic()
            result = json.loads(
                runtime.execute(
                    "search_files",
                    {"pattern": r"(a+)+$", "glob": "*.txt"},
                )
            )
            self.assertFalse(result["ok"])
            self.assertIn("nested quantified", result["error"])
            self.assertLess(time.monotonic() - started, 0.5)

    def test_extract_archive_supports_gzip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory, "events.jsonl.gz")
            with gzip.open(archive, "wb") as stream:
                stream.write(b'{"event":"ok"}\n')
            runtime = TaskToolRuntime(directory, "https://example.test")
            result = json.loads(
                runtime.execute(
                    "extract_archive",
                    {"path": archive.name, "output_dir": "unpacked"},
                )
            )
            self.assertTrue(result["ok"])
            self.assertEqual(result["file_count"], 1)
            self.assertEqual(
                Path(directory, "unpacked", "events.jsonl").read_text(),
                '{"event":"ok"}\n',
            )

    def test_extract_archive_rejects_tar_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory, "unsafe.tar")
            with tarfile.open(archive, "w") as stream:
                payload = b"nope"
                member = tarfile.TarInfo("../escape.txt")
                member.size = len(payload)
                stream.addfile(member, io.BytesIO(payload))
            runtime = TaskToolRuntime(directory, "https://example.test")
            result = json.loads(
                runtime.execute("extract_archive", {"path": archive.name})
            )
            self.assertFalse(result["ok"])
            self.assertIn("unsafe", result["error"])
            self.assertFalse(Path(directory, "escape.txt").exists())

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
            self.assertTrue(result.generator_fingerprint.startswith("gf1:"))
            self.assertEqual(len(game.messages.calls), 2)
            self.assertTrue(
                any("solver summary outcome=recorded" in line for line in game.logs)
            )

            solver.replay_cache.store(result.replay_recipe)
            replayed = solver.solve(
                Tile(id="PR-C5-b", category="Cryptic", points=500),
                recipe_generation=0,
            )
            self.assertEqual(replayed.answer, "TOKEN-ABC")
            self.assertTrue(replayed.replayed)
            self.assertNotEqual(replayed.source, replayed.verification_source)
            self.assertEqual(len(game.messages.calls), 2)

    def test_replay_rejects_nondeterministic_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = TaskToolRuntime(directory, "https://example.test")
            recipe = ReplayRecipe(
                code=(
                    "from pathlib import Path\n"
                    "path = Path('counter.txt')\n"
                    "value = int(path.read_text()) + 1 if path.exists() else 1\n"
                    "path.write_text(str(value))\n"
                    "print(f'TOKEN-{value}')\n"
                ),
                extraction_regex=r"(TOKEN-\d+)",
            )
            self.assertIsNone(TileSolver._try_replay(runtime, recipe))
            self.assertIsNone(runtime.recorded_answer)

    def test_legacy_recipe_is_not_replayed_without_fingerprint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game = FakeSolverGame(directory)
            solver = TileSolver(game, max_turns=4)
            result = solver.solve(
                Tile(id="PR-C5", category="Cryptic", points=500),
                recipe_generation=0,
                replay_recipe=ReplayRecipe(
                    code='raise RuntimeError("must not run")'
                ),
            )

            self.assertIsNotNone(result)
            self.assertEqual(result.strategy, "claude")
            self.assertEqual(len(game.messages.calls), 2)
            self.assertTrue(
                any("skipped incompatible or legacy" in line for line in game.logs)
            )

    def test_finalization_turn_can_record_existing_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game = FakeSolverGame(directory)
            first = SimpleNamespace(
                type="tool_use",
                id="tool-1",
                name="run_python",
                input={"code": 'print("TOKEN-FINAL")'},
            )
            second = SimpleNamespace(
                type="tool_use",
                id="tool-2",
                name="record_answer",
                input={
                    "artifact_id": "artifact-0001",
                    "extraction_regex": r"(MISSING-[A-Z]+)",
                    "confidence": 0.99,
                    "evidence": "first extraction pattern was too narrow",
                },
            )
            third = SimpleNamespace(
                type="tool_use",
                id="tool-3",
                name="record_answer",
                input={
                    "artifact_id": "artifact-0001",
                    "extraction_regex": r"(TOKEN-[A-Z]+)",
                    "confidence": 0.99,
                    "evidence": "finalizer used the previously computed artifact",
                },
            )
            game.messages = FakeMessages(
                [
                    SimpleNamespace(content=[first]),
                    SimpleNamespace(content=[second]),
                    SimpleNamespace(content=[third]),
                ]
            )
            game.client = SimpleNamespace(messages=game.messages)
            solver = TileSolver(game, max_turns=2)
            result = solver.solve(
                Tile(id="PR-C3", category="Cryptic", points=300),
                recipe_generation=0,
            )
            self.assertIsNotNone(result)
            self.assertEqual(result.answer, "TOKEN-FINAL")
            self.assertEqual(len(game.messages.calls), 3)
            self.assertEqual(
                [tool["name"] for tool in game.messages.calls[-1]["tools"]],
                ["record_answer"],
            )
            self.assertEqual(len(game.messages.calls[-1]["messages"]), 1)
            self.assertIn(
                "recent_artifacts",
                game.messages.calls[-1]["messages"][0]["content"],
            )
            self.assertTrue(
                any("finalizer=1" in message for message in game.logs)
            )

    def test_finalization_is_skipped_without_a_record_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game = FakeSolverGame(directory)
            game.messages = FakeMessages(
                [
                    SimpleNamespace(
                        content=[
                            SimpleNamespace(
                                type="text", text="still investigating"
                            )
                        ]
                    ),
                    SimpleNamespace(
                        content=[
                            SimpleNamespace(type="text", text="no candidate")
                        ]
                    ),
                ]
            )
            game.client = SimpleNamespace(messages=game.messages)
            solver = TileSolver(game, max_turns=2)
            result = solver.solve(
                Tile(id="PR-C3", category="Cryptic", points=300),
                recipe_generation=0,
            )
            self.assertIsNone(result)
            self.assertEqual(len(game.messages.calls), 2)
            self.assertIn("tools=none", solver.pop_failure_report("PR-C3"))
            self.assertTrue(
                any("finalizer=0" in message for message in game.logs)
            )

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

    def test_cancellation_closes_a_stream_stalled_before_next_event(self) -> None:
        class Stream:
            def __init__(self) -> None:
                self.started = Event()
                self.released = Event()
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                self.closed = True

            def close(self) -> None:
                self.closed = True
                self.released.set()

            def __iter__(self):
                self.started.set()
                self.released.wait(10)
                return
                yield  # pragma: no cover

            @staticmethod
            def get_final_message():
                raise AssertionError("cancelled stream must not complete")

        stream = Stream()
        client = SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **_kwargs: stream)
        )
        token = CancellationToken()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(TileSolver._model_call, client, {}, token)
            self.assertTrue(stream.started.wait(timeout=1))
            token.cancel("claimed elsewhere")
            with self.assertRaises(SolveCancelled):
                future.result(timeout=1)
        self.assertTrue(stream.closed)

    def test_retries_reuse_task_files_runtime_and_model_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            game = FakeSolverGame(directory)
            game.task_calls = 0
            game.fetch_calls = 0
            game.client_calls = 0

            original_task = game.task

            def counted_task(task_id: str) -> dict:
                game.task_calls += 1
                return original_task(task_id)

            def counted_fetch(_task_id: str, _detail: dict) -> list[str]:
                game.fetch_calls += 1
                return []

            def counted_client():
                game.client_calls += 1
                return game.client

            game.task = counted_task
            game.fetch_files = counted_fetch
            game.anthropic_client = counted_client
            game.messages = FakeMessages(
                [
                    SimpleNamespace(content=[SimpleNamespace(type="text", text="x")])
                    for _ in range(4)
                ]
            )
            game.client = SimpleNamespace(messages=game.messages)
            solver = TileSolver(game, max_turns=2, fast_paths=False)
            tile = Tile(id="PR-C3-retry", category="Cryptic", points=300)

            self.assertIsNone(solver.solve(tile, 0, attempt=1))
            retained = solver._prepared[tile.id].runtime.artifacts.put(
                "retained evidence"
            )
            self.assertIsNone(solver.solve(tile, 0, attempt=2))

            self.assertEqual(game.task_calls, 1)
            self.assertEqual(game.fetch_calls, 1)
            self.assertEqual(game.client_calls, 1)
            self.assertEqual(
                solver._prepared[tile.id].runtime.artifacts.get(retained),
                "retained evidence",
            )

    def test_tool_schemas_are_scoped_to_the_task_category(self) -> None:
        cryptic = {
            schema["name"]
            for schema in TileSolver._tools_for("Cryptic", ["payload.zip"])
        }
        web = {
            schema["name"]
            for schema in TileSolver._tools_for("The Dark Web", [])
        }
        self.assertIn("extract_archive", cryptic)
        self.assertNotIn("http_request", cryptic)
        self.assertIn("http_request", web)
        self.assertNotIn("write_file", web)

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
    @staticmethod
    def _ordering_manager(
        *, soft_deadline: float = 15.0
    ) -> SubmissionManager:
        async def on_result(_value: Candidate, _result: dict) -> None:
            return None

        return SubmissionManager(
            SimpleNamespace(log=lambda *_values: None),
            AgentConfig(submission_soft_deadline=soft_deadline),
            RecipeGate(),
            lambda _task_id: True,
            on_result,
        )

    async def test_fresh_candidates_favor_expected_value(self) -> None:
        manager = self._ordering_manager()
        now = time.monotonic()
        low = replace(
            candidate("Q-A1", recipe="Ancient Scrolls:100"),
            points=100,
            created_at=now,
        )
        high = replace(
            candidate("Q-C5", recipe="Cryptic:500"),
            points=500,
            created_at=now,
        )
        await manager.enqueue(low)
        await manager.enqueue(high)

        selected = await asyncio.wait_for(manager._next_candidate(), timeout=1)

        self.assertEqual(selected.task_id, "Q-C5")
        await manager.stop()

    async def test_near_deadline_candidate_preempts_fresh_high_value(self) -> None:
        manager = self._ordering_manager(soft_deadline=15.0)
        now = time.monotonic()
        old_low = replace(
            candidate("Q-A1", recipe="Ancient Scrolls:100"),
            points=100,
            created_at=now - 14.0,
        )
        fresh_high = replace(
            candidate("Q-C5", recipe="Cryptic:500"),
            points=500,
            created_at=now,
        )
        await manager.enqueue(fresh_high)
        await manager.enqueue(old_low)

        selected = await asyncio.wait_for(manager._next_candidate(), timeout=1)

        self.assertEqual(selected.task_id, "Q-A1")
        await manager.stop()

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

    async def test_rechecks_open_state_after_rate_limit_wait(self) -> None:
        submitted: list[str] = []
        results: list[str] = []
        open_ids = {"Q-C5-a"}

        class Game:
            @staticmethod
            def submit(task_id: str, _answer: str) -> dict:
                submitted.append(task_id)
                return {"result": "correct"}

            @staticmethod
            def log(*_values) -> None:
                return None

        async def on_result(value: Candidate, result: dict) -> None:
            results.append(str(result["result"]))

        manager = SubmissionManager(
            Game(),
            AgentConfig(submit_interval=0.1),
            RecipeGate(),
            lambda task_id: task_id in open_ids,
            on_result,
        )
        manager._last_submit = time.monotonic()
        runner = asyncio.create_task(manager.run())
        await manager.enqueue(candidate("Q-C5-a"))
        await asyncio.sleep(0.02)
        open_ids.clear()
        await asyncio.sleep(0.12)
        await manager.stop()
        await asyncio.wait_for(runner, timeout=1)
        self.assertEqual(submitted, [])
        self.assertEqual(results, ["already_claimed"])

    async def test_auth_failure_terminates_submission_manager(self) -> None:
        class AuthError(RuntimeError):
            pass

        class Game:
            @staticmethod
            def submit(_task_id: str, _answer: str) -> dict:
                raise AuthError("bad key")

            @staticmethod
            def log(*_values) -> None:
                return None

        async def on_result(_value: Candidate, _result: dict) -> None:
            raise AssertionError("fatal auth error must not become a result")

        manager = SubmissionManager(
            Game(),
            AgentConfig(submit_interval=0),
            RecipeGate(),
            lambda _task_id: True,
            on_result,
        )
        await manager.enqueue(candidate("Q-C5-a"))
        with self.assertRaises(AuthError):
            await asyncio.wait_for(manager.run(), timeout=1)

    async def test_rechecks_recipe_generation_after_rate_limit_wait(self) -> None:
        submitted: list[str] = []
        results: list[str] = []
        gate = RecipeGate()
        gate.set_phase("round1")
        value = candidate("Q-C5-a")

        class Game:
            @staticmethod
            def submit(task_id: str, _answer: str) -> dict:
                submitted.append(task_id)
                return {"result": "correct"}

            @staticmethod
            def log(*_values) -> None:
                return None

        async def on_result(_candidate: Candidate, result: dict) -> None:
            results.append(str(result["result"]))

        manager = SubmissionManager(
            Game(),
            AgentConfig(submit_interval=0.1),
            gate,
            lambda _task_id: True,
            on_result,
        )
        manager._last_submit = time.monotonic()
        runner = asyncio.create_task(manager.run())
        await manager.enqueue(value)
        await asyncio.sleep(0.02)
        gate.on_incorrect(value, 0)
        await asyncio.sleep(0.12)
        await manager.stop()
        await asyncio.wait_for(runner, timeout=1)
        self.assertEqual(submitted, [])
        self.assertEqual(results, ["invalidated"])


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

    async def test_scheduler_covers_distinct_categories_in_each_wave(self) -> None:
        controller = self._controller_for_unit_tests()
        records = [
            TileRecord(Tile(f"T-A-{points}", "Category-A", points))
            for points in (100, 200, 300, 400, 500)
        ]
        records.extend(
            TileRecord(Tile(f"T-{category}", f"Category-{category}", 200))
            for category in ("B", "C", "D")
        )
        controller.records = {record.tile.id: record for record in records}
        selected = controller._select_records(records, room=4)
        self.assertEqual(len(selected), 4)
        self.assertEqual(
            len({record.tile.category for record in selected}),
            4,
        )

    async def test_scheduler_opens_with_distinct_validated_500s(self) -> None:
        controller = self._controller_for_unit_tests()
        records = [
            TileRecord(
                Tile(
                    "A5",
                    "Ancient Scrolls",
                    500,
                    "Trust the Errata",
                    raw={"remaining": 1, "total": 1},
                )
            ),
            TileRecord(
                Tile(
                    "C5",
                    "Cryptic",
                    500,
                    "Crib Sheet",
                    raw={"remaining": 1, "total": 1},
                )
            ),
            TileRecord(
                Tile(
                    "H5",
                    "Heavy Compute",
                    500,
                    "Traveling Salesman",
                    raw={"remaining": 1, "total": 1},
                )
            ),
            TileRecord(
                Tile(
                    "N5",
                    "Needle in the Haystack",
                    500,
                    "Thirty Systems",
                    raw={"remaining": 1, "total": 1},
                )
            ),
            TileRecord(
                Tile(
                    "S5",
                    "Ship It",
                    500,
                    "Quintuple Fault",
                    raw={"remaining": 1, "total": 1},
                )
            ),
            TileRecord(
                Tile(
                    "W5",
                    "The Dark Web",
                    500,
                    "Initiation",
                    raw={"remaining": 1, "total": 1},
                )
            ),
            TileRecord(
                Tile(
                    "S2",
                    "Ship It",
                    200,
                    "Spec vs Code",
                    raw={"remaining": 4, "total": 4},
                )
            ),
            TileRecord(
                Tile(
                    "W4",
                    "The Dark Web",
                    400,
                    "The Gatekeeper",
                    raw={"remaining": 2, "total": 2},
                )
            ),
        ]
        controller.records = {record.tile.id: record for record in records}

        selected = controller._select_records(records, room=4)

        self.assertEqual(
            {record.tile.id for record in selected},
            {"A5", "C5", "H5", "S5"},
        )
        self.assertEqual(
            len({record.tile.category for record in selected}),
            4,
        )

    async def test_scheduler_prioritizes_a_contested_cell(self) -> None:
        controller = self._controller_for_unit_tests()
        calm = TileRecord(
            Tile(
                "T-calm",
                "Calm",
                300,
                raw={"remaining": 8, "total": 8},
            )
        )
        contested = TileRecord(
            Tile(
                "T-contested",
                "Contested",
                300,
                raw={"remaining": 1, "total": 8},
            )
        )
        controller.records = {
            record.tile.id: record for record in (calm, contested)
        }

        selected = controller._select_records([calm, contested], room=1)

        self.assertEqual(selected[0].tile.id, contested.tile.id)

    async def test_scheduler_covers_unrelated_lane_before_trusted_sibling(
        self,
    ) -> None:
        controller = self._controller_for_unit_tests()
        first = TileRecord(Tile("T-fast-a", "Fast", 200))
        sibling = TileRecord(Tile("T-fast-b", "Fast", 200))
        unrelated = TileRecord(Tile("T-slow", "Slow", 500))
        controller.gate.state(first.tile.recipe_key).successes = 1
        records = [first, sibling, unrelated]
        controller.records = {record.tile.id: record for record in records}

        selected = controller._select_records(records, room=2)

        self.assertEqual(
            {record.tile.recipe_key for record in selected},
            {first.tile.recipe_key, unrelated.tile.recipe_key},
        )

    async def test_scheduler_ranks_tiers_by_expected_yield(self) -> None:
        controller = self._controller_for_unit_tests()
        records = [
            TileRecord(Tile(f"T-{points}", f"Category-{points}", points))
            for points in (100, 200, 300, 400, 500)
        ]
        controller.records = {record.tile.id: record for record in records}

        selected = controller._select_records(records, room=4)

        self.assertEqual(
            {record.tile.points for record in selected},
            {100, 200, 300, 400},
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

    async def test_retry_schedule_is_logged(self) -> None:
        controller = self._controller_for_unit_tests()
        record = TileRecord(
            Tile("PR-S3", "Ship It", 300),
            status=TileStatus.SOLVING,
            attempts=1,
        )
        controller._retry_or_fail(record, "solver produced no candidate", delay=7)
        self.assertEqual(record.status, TileStatus.RETRY)
        self.assertTrue(
            any(
                "retry scheduled attempt=2 retry_in=7.0s" in message
                for message in controller.game.logs
            )
        )

    async def test_verifier_exception_does_not_leave_tile_solving(self) -> None:
        controller = self._controller_for_unit_tests()
        record = TileRecord(Tile("PR-C3", "Cryptic", 300))
        controller.open_ids.add(record.tile.id)

        class Solver:
            @staticmethod
            def solve(*_args, **_kwargs) -> Candidate:
                return candidate("PR-C3")

        class Verifier:
            @staticmethod
            def verify(_candidate: Candidate):
                raise ArithmeticError("malformed decimal")

        controller.solver = Solver()
        controller.verifier = Verifier()
        await controller._solve(record, CancellationToken())
        self.assertEqual(record.status, TileStatus.RETRY)
        self.assertIn("verifier error", record.last_error)

    async def test_exhausted_recipe_family_is_temporarily_deprioritized(self) -> None:
        controller = self._controller_for_unit_tests()
        exhausted = TileRecord(Tile("PR-S3-a", "Ship It", 300))
        sibling = TileRecord(Tile("PR-S3-b", "Ship It", 300))
        unrelated = TileRecord(Tile("PR-N3-a", "Needle in the Haystack", 300))
        controller.records = {
            record.tile.id: record
            for record in (exhausted, sibling, unrelated)
        }
        delay = controller._defer_recipe_after_exhaustion(exhausted)
        selected = controller._select_records([sibling, unrelated], room=1)
        self.assertEqual(delay, controller.config.recipe_backoff_seconds)
        self.assertEqual(selected[0].tile.id, unrelated.tile.id)

    async def test_exhaustion_strategy_is_transferred_to_sibling(self) -> None:
        controller = self._controller_for_unit_tests()

        class Solver:
            def __init__(self) -> None:
                self.hints: list[str] = []
                self.prior_hints: list[str] = []

            def solve(
                self,
                _tile: Tile,
                _generation: int,
                _replay=None,
                _cancellation=None,
                *,
                attempt: int = 1,
                family_failure_hint: str = "",
                prior_failure_hint: str = "",
            ) -> None:
                self.hints.append(family_failure_hint)
                self.prior_hints.append(prior_failure_hint)

            @staticmethod
            def pop_failure_report(task_id: str) -> str:
                if task_id.endswith("-a"):
                    return "tools=http_request:8; last_turn_tools=http_request"
                return ""

        solver = Solver()
        controller.solver = solver
        first = TileRecord(Tile("PR-W3-a", "The Dark Web", 300))
        second = TileRecord(
            Tile("PR-W3-b", "The Dark Web", 300),
            last_error="previous candidate failed verification",
        )
        await controller._solve(first, CancellationToken())
        await controller._solve(second, CancellationToken())
        self.assertEqual(solver.hints[0], "")
        self.assertIn("http_request:8", solver.hints[1])
        self.assertIn("failed verification", solver.prior_hints[1])

    async def test_scored_replay_requires_practice_variant_validation(self) -> None:
        controller = self._controller_for_unit_tests()
        fingerprint = "gf1:" + ("a" * 64)
        recipe = ReplayRecipe(
            code='print("TOKEN-OK")',
            generator_fingerprint=fingerprint,
            fingerprint_version=1,
            answer_format="exact",
            scheduler_key="Ship It:200",
        )
        controller.replay_cache.store(recipe)

        controller.phase = "round1"
        self.assertFalse(controller._replay_allowed(fingerprint))

        controller.phase = "practice"
        self.assertTrue(controller._replay_allowed(fingerprint))

        controller.phase = "round1"
        controller.replay_validations[fingerprint] = 1
        self.assertTrue(controller._replay_allowed(fingerprint))

    async def test_incorrect_replay_invalidates_only_its_fingerprint(self) -> None:
        controller = self._controller_for_unit_tests()
        scheduler_key = "Cryptic:500"
        first_fingerprint = "gf1:" + ("a" * 64)
        second_fingerprint = "gf1:" + ("b" * 64)
        for fingerprint in (first_fingerprint, second_fingerprint):
            controller.replay_cache.store(
                ReplayRecipe(
                    code='print("TOKEN-OK")',
                    generator_fingerprint=fingerprint,
                    fingerprint_version=1,
                    answer_format="exact",
                    scheduler_key=scheduler_key,
                )
            )
        tile = Tile("Q-C5-a", "Cryptic", 500)
        controller.records[tile.id] = TileRecord(
            tile=tile, status=TileStatus.CANDIDATE, attempts=1
        )
        failed = replace(
            candidate(task_id=tile.id),
            generator_fingerprint=first_fingerprint,
        )

        await controller._on_submission_result(failed, {"result": "incorrect"})

        self.assertIsNone(controller.replay_cache.lookup(first_fingerprint))
        self.assertIsNotNone(controller.replay_cache.lookup(second_fingerprint))

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

    async def test_run_once_refills_worker_without_waiting_for_board_poll(self) -> None:
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
                model_workers=1,
                run_once=True,
                dry_run=True,
                board_poll_seconds=5,
                submit_interval=0.01,
            ),
        )
        controller.solver = Solver()
        await asyncio.wait_for(controller.run(), timeout=1)
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
