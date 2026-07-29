"""Bounded Anthropic tool-use loop for one Jeopardy tile."""

from __future__ import annotations

import json
from typing import Any

from agent_types import CancellationToken, Candidate, ReplayRecipe, Tile
from tool_runtime import TOOL_SCHEMAS, TaskToolRuntime

CATEGORY_PLAYBOOKS = {
    "Needle in the Haystack": (
        "Inspect schemas and sizes first. Use Python to filter/aggregate the real "
        "data. Recompute the result through a second query or assert invariants."
    ),
    "The Dark Web": (
        "Use one named HTTP session so cookies persist. Inspect forms, hidden "
        "fields, CSRF tokens, redirects, and response status. Extract final "
        "tokens from the response artifact."
    ),
    "Ship It": (
        "List the project, run the relevant code/tests with Python, inspect the "
        "failure, patch only the task workspace, and rerun the strongest "
        "available test before proposing an answer."
    ),
    "Ancient Scrolls": (
        "Search before reading large documents. Resolve cross-references and "
        "later amendments. Verify the conclusion against exact source passages."
    ),
    "Cryptic": (
        "Inspect file signatures and bytes. Build a deterministic decode/archive/"
        "crypto pipeline in Python and verify with a round trip or structural "
        "checks."
    ),
    "Heavy Compute": (
        "Write an actual solver with pruning or vectorization. Independently "
        "check every constraint and recompute the objective before answering."
    ),
}


BASE_SYSTEM_PROMPT = """You are an autonomous competition task solver.

Rules:
- Work from the actual task files and tool results. Never guess.
- Use tools until you can explain concrete verification evidence.
- Keep computations reproducible and bounded. Inspect before loading huge files.
- For web tasks, preserve cookies with one named HTTP session.
- Before finishing, independently check the answer or its defining properties.
- Finish ONLY by calling record_answer. Do not put the answer in normal prose.
- Prefer record_answer with artifact_id and extraction_regex. This preserves the
  exact bytes emitted by code or found in a source and prevents transcription
  mistakes. Use the direct answer field only when no tool can emit the value.
- The submit action is intentionally unavailable. The controller decides whether
  a candidate is safe enough to submit.
"""


class TileSolver:
    def __init__(
        self,
        game: Any,
        *,
        max_turns: int = 8,
        max_tokens: int = 1800,
        verbose: bool = False,
    ) -> None:
        self.game = game
        self.max_turns = max(2, max_turns)
        self.max_tokens = max(256, min(max_tokens, 4096))
        self.verbose = verbose

    def solve(
        self,
        tile: Tile,
        recipe_generation: int,
        replay_recipe: ReplayRecipe | None = None,
        cancellation: CancellationToken | None = None,
    ) -> Candidate | None:
        cancellation = cancellation or CancellationToken()
        cancellation.check()
        detail = self.game.task(tile.id)
        cancellation.check()
        workdir = self.game.workdir(tile.id)
        names = self.game.fetch_files(tile.id, detail)
        cancellation.check()
        runtime = TaskToolRuntime(workdir, self.game.BASE, cancellation)

        answer_format = str(detail.get("answer_format") or "exact")
        category = str(detail.get("category") or tile.category)
        if replay_recipe is not None:
            replayed = self._try_replay(runtime, replay_recipe)
            if replayed is not None:
                self.game.log(f"{tile.id}: reused a previously correct Python recipe")
                return Candidate(
                    task_id=tile.id,
                    answer=replayed.answer,
                    answer_format=answer_format,
                    category=category,
                    points=int(detail.get("points") or tile.points),
                    recipe_key=tile.recipe_key,
                    recipe_generation=recipe_generation,
                    confidence=0.99,
                    evidence=(
                        "replayed a previously correct Python recipe with a "
                        "zero exit status and repeated its exact extraction"
                    ),
                    source=replayed.source,
                    replay_recipe=replay_recipe,
                    replayed=True,
                )
            self.game.log(
                f"{tile.id}: cached recipe did not produce a candidate; "
                "falling back to the model"
            )

        playbook = CATEGORY_PLAYBOOKS.get(
            category,
            "Inspect the materials with tools, compute the result, and verify it.",
        )
        prompt = (
            f"Task id: {tile.id}\n"
            f"Category: {category}\n"
            f"Points: {detail.get('points', tile.points)}\n"
            f"Answer format: {answer_format}\n"
            f"Downloaded files: {names or 'none'}\n\n"
            f"Category playbook: {playbook}\n\n"
            f"Task:\n{str(detail.get('prompt') or '')[:40_000]}"
        )
        if self.verbose:
            self.game.log(f"{tile.id} solver prompt:\n{prompt}\n---")

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        client = self.game.anthropic_client()

        for turn in range(1, self.max_turns + 1):
            cancellation.check()
            request = {
                "model": self.game.MODEL,
                "max_tokens": self.max_tokens,
                "temperature": 0,
                "system": BASE_SYSTEM_PROMPT,
                "tools": TOOL_SCHEMAS,
                "messages": messages,
            }
            response = self._model_call(client, request, cancellation)
            cancellation.check()
            assistant_content = [self._block_dict(block) for block in response.content]
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results: list[dict[str, Any]] = []
            for block in response.content:
                cancellation.check()
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_name = str(block.name)
                tool_input = dict(block.input or {})
                result = runtime.execute(tool_name, tool_input)
                if self.verbose:
                    self.game.log(
                        f"{tile.id} turn={turn} tool={tool_name} "
                        f"input={self._safe_preview(tool_input)} "
                        f"result={result[:1000]}"
                    )
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

            if runtime.recorded_answer is not None:
                recorded = runtime.recorded_answer
                return Candidate(
                    task_id=tile.id,
                    answer=recorded.answer,
                    answer_format=answer_format,
                    category=category,
                    points=int(detail.get("points") or tile.points),
                    recipe_key=tile.recipe_key,
                    recipe_generation=recipe_generation,
                    confidence=recorded.confidence,
                    evidence=recorded.evidence,
                    source=recorded.source,
                    replay_recipe=recorded.replay_recipe,
                )

            if tool_results:
                messages.append({"role": "user", "content": tool_results})
                continue

            # A text-only response is not a candidate. Give the model one clear
            # recovery instruction within the normal turn budget.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "No answer was recorded. Continue using tools to verify the "
                        "result, then call record_answer. Do not reply with prose."
                    ),
                }
            )

        self.game.log(
            f"{tile.id}: solver exhausted {self.max_turns} turns without "
            "a recorded answer"
        )
        return None

    @staticmethod
    def _model_call(
        client: Any,
        request: dict[str, Any],
        cancellation: CancellationToken,
    ) -> Any:
        """Stream when available so cancellation closes an in-flight response."""

        if hasattr(client.messages, "stream"):
            with client.messages.stream(**request) as stream:
                for _event in stream:
                    cancellation.check()
                cancellation.check()
                return stream.get_final_message()
        # Test doubles and older SDKs may expose only create(). Cancellation
        # still applies immediately before and after the blocking request.
        return client.messages.create(**request)

    @staticmethod
    def _try_replay(runtime: TaskToolRuntime, recipe: ReplayRecipe):
        run_result = json.loads(
            runtime.execute("run_python", {"code": recipe.code, "timeout": 30})
        )
        if not run_result.get("ok") or run_result.get("returncode") != 0:
            return None
        artifact_id = run_result.get("stdout_artifact")
        if not artifact_id:
            return None
        record_arguments: dict[str, Any] = {
            "artifact_id": artifact_id,
            "confidence": 0.99,
            "evidence": "cached recipe replay",
        }
        if recipe.extraction_regex:
            record_arguments["extraction_regex"] = recipe.extraction_regex
            record_arguments["regex_group"] = recipe.regex_group
        record_result = json.loads(runtime.execute("record_answer", record_arguments))
        if not record_result.get("ok"):
            return None
        return runtime.recorded_answer

    @staticmethod
    def _block_dict(block: Any) -> dict[str, Any]:
        if hasattr(block, "model_dump"):
            return block.model_dump(exclude_none=True)
        if getattr(block, "type", None) == "text":
            return {"type": "text", "text": block.text}
        if getattr(block, "type", None) == "tool_use":
            return {
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            }
        raise TypeError(f"unsupported response block {block!r}")

    @staticmethod
    def _safe_preview(value: dict[str, Any]) -> str:
        preview = dict(value)
        if "code" in preview:
            preview["code"] = str(preview["code"])[:300]
        if "content" in preview:
            preview["content"] = f"<{len(str(preview['content']))} chars>"
        return repr(preview)[:600]
