"""Bounded Anthropic tool-use loop for one Jeopardy tile."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from threading import Lock
from typing import Any

from agent_types import (
    CancellationToken,
    Candidate,
    ReplayRecipe,
    ReplayRecipeCache,
    Tile,
)
from fast_paths import FastPathDispatcher, FastPathStatus, TaskFingerprint
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

TITLE_PLAYBOOKS = {
    "deep pages": (
        "Treat this as a bounded graph traversal. Reuse one named HTTP session, "
        "queue each same-host link once, cap the crawl, and accept only one "
        "explicitly labeled final token. Do not infer a token from page names."
    ),
    "breadcrumbs": (
        "Replay the complete same-session HTTP chain programmatically. Preserve "
        "cookies, follow each same-host breadcrumb once, and extract the exact "
        "answer from the final response artifact."
    ),
    "needle in 2^44": (
        "Parse every numeric constraint before computing. Reduce the 2^44 search "
        "with meet-in-the-middle, baby-step/giant-step, or modular decomposition; "
        "never enumerate the full domain. Validate the candidate against every "
        "original constraint with a separate calculation."
    ),
    "traveling salesman": (
        "Parse and validate the complete graph or coordinate set. Use deterministic "
        "multi-start construction followed by 2-opt/3-opt improvement, verify that "
        "the route is a permutation with the required endpoints, and recompute its "
        "objective independently before recording the exact requested format."
    ),
    "quintuple fault": (
        "Call extract_archive first when an archive is present, then list the "
        "extracted tree. Run the strongest available tests once, cluster the five "
        "root failures, make minimal targeted fixes, and rerun both focused and "
        "full tests. Record only a verified requested artifact or token."
    ),
}


BASE_SYSTEM_PROMPT = """You are an autonomous competition task solver.

Rules:
- Work from the actual task files and tool results. Never guess.
- Use tools until you can explain concrete verification evidence.
- Keep context compact. The downloaded filenames are already in the task prompt;
  inspect the relevant inputs directly. Use list_files after extracting archives
  or generating helpers. For large data, return schemas, samples, counts,
  aggregates, and targeted search matches only.
- Never print or request a complete log, document, table, or HTTP body. Use
  search_files/search_artifact and short read windows to drill into evidence.
- Make Python print concise computed summaries and the final candidate, not data.
- For web tasks, preserve cookies with one named HTTP session.
- Before finishing, independently check the answer or its defining properties.
- On scored 400/500 tasks, create a separate verification artifact from an
  independent check and pass it as verification_artifact_id to record_answer.
- Finish ONLY by calling record_answer. Do not put the answer in normal prose.
- Prefer record_answer with artifact_id and extraction_regex. This preserves the
  exact bytes emitted by code or found in a source and prevents transcription
  mistakes. Use the direct answer field only when no tool can emit the value.
- The submit action is intentionally unavailable. The controller decides whether
  a candidate is safe enough to submit.
"""

FINALIZATION_SYSTEM_PROMPT = """

This is the single finalization turn. Do not perform more investigation.
Use the evidence and artifacts already present. Call record_answer only when
they support a concrete answer; otherwise stop without inventing one.
"""

FINALIZATION_TOOLS = [
    schema for schema in TOOL_SCHEMAS if schema.get("name") == "record_answer"
]


class TileSolver:
    def __init__(
        self,
        game: Any,
        *,
        max_turns: int = 12,
        max_tokens: int = 1800,
        verbose: bool = False,
        fast_paths: bool = True,
        fast_path_only: bool = False,
        replay_cache: ReplayRecipeCache | None = None,
        replay_allowed: Callable[[str], bool] | None = None,
    ) -> None:
        self.game = game
        self.max_turns = max(2, max_turns)
        self.max_tokens = max(256, min(max_tokens, 4096))
        self.verbose = verbose
        self.fast_paths_enabled = fast_paths or fast_path_only
        self.fast_path_only = fast_path_only
        self.fast_path_dispatcher = FastPathDispatcher()
        self.replay_cache = replay_cache or ReplayRecipeCache()
        self.replay_allowed = replay_allowed or (lambda _fingerprint: True)
        self._disabled_fast_paths: dict[tuple[str, int], set[str]] = {}
        self._disabled_fast_paths_lock = Lock()
        self._failure_reports: dict[str, str] = {}
        self._failure_reports_lock = Lock()

    def disable_fast_path(
        self, recipe_key: str, recipe_generation: int, handler: str
    ) -> None:
        """Force one recipe generation through Claude after a bad canary."""

        with self._disabled_fast_paths_lock:
            self._disabled_fast_paths.setdefault(
                (recipe_key, recipe_generation), set()
            ).add(handler)

    def is_fast_path_disabled(
        self, recipe_key: str, recipe_generation: int, handler: str
    ) -> bool:
        with self._disabled_fast_paths_lock:
            return handler in self._disabled_fast_paths.get(
                (recipe_key, recipe_generation), set()
            )

    def _disabled_handlers(
        self, recipe_key: str, recipe_generation: int
    ) -> frozenset[str]:
        with self._disabled_fast_paths_lock:
            return frozenset(
                self._disabled_fast_paths.get((recipe_key, recipe_generation), set())
            )

    def pop_failure_report(self, task_id: str) -> str:
        with self._failure_reports_lock:
            return self._failure_reports.pop(task_id, "")

    def _store_failure_report(self, task_id: str, report: str) -> None:
        with self._failure_reports_lock:
            self._failure_reports[task_id] = report[:1_200]

    def solve(
        self,
        tile: Tile,
        recipe_generation: int,
        replay_recipe: ReplayRecipe | None = None,
        cancellation: CancellationToken | None = None,
        attempt: int = 1,
        family_failure_hint: str = "",
    ) -> Candidate | None:
        self.pop_failure_report(tile.id)
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
        fingerprint = TaskFingerprint.build(
            tile, detail, names, runtime.root, cancellation
        )
        self.game.log(f"{tile.id}: fingerprint {fingerprint.compact_summary()}")
        generator_fingerprint = fingerprint.generator_fingerprint
        compatible_recipe = None
        if replay_recipe is not None and replay_recipe.compatible_with(
            generator_fingerprint,
            fingerprint.fingerprint_version,
            answer_format,
        ):
            compatible_recipe = replay_recipe
        elif replay_recipe is not None:
            self.game.log(
                f"{tile.id}: skipped incompatible or legacy replay recipe"
            )
        if compatible_recipe is None and self.replay_allowed(generator_fingerprint):
            cached = self.replay_cache.lookup(generator_fingerprint)
            if cached is not None and cached.compatible_with(
                generator_fingerprint,
                fingerprint.fingerprint_version,
                answer_format,
            ):
                compatible_recipe = cached

        if compatible_recipe is not None:
            replayed = self._try_replay(runtime, compatible_recipe)
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
                    strategy="replay",
                    verification_source=replayed.verification_source,
                    generator_fingerprint=generator_fingerprint,
                    replay_recipe=compatible_recipe,
                    replayed=True,
                )
            self.replay_cache.discard(generator_fingerprint)
            self.game.log(
                f"{tile.id}: cached recipe did not produce a candidate; "
                "discarding this fingerprint recipe and continuing"
            )

        if self.fast_paths_enabled:
            outcome = self.fast_path_dispatcher.attempt(
                tile,
                detail,
                names,
                runtime,
                disabled_handlers=self._disabled_handlers(
                    tile.recipe_key, recipe_generation
                ),
                fingerprint=fingerprint,
            )
            self.game.log(
                f"{tile.id}: fast path handler={outcome.handler or 'none'} "
                f"outcome={outcome.status.value} "
                f"elapsed={outcome.elapsed_seconds:.3f}s"
            )
            if outcome.status == FastPathStatus.SOLVED:
                return Candidate(
                    task_id=tile.id,
                    answer=outcome.answer,
                    answer_format=answer_format,
                    category=category,
                    points=int(detail.get("points") or tile.points),
                    recipe_key=tile.recipe_key,
                    recipe_generation=recipe_generation,
                    confidence=outcome.confidence,
                    evidence=outcome.evidence,
                    source=outcome.source,
                    strategy=f"fast:{outcome.handler}",
                    verification_source=outcome.verification_source,
                    generator_fingerprint=generator_fingerprint,
                )
            self.game.log(
                f"{tile.id}: deterministic fallback reason={outcome.reason}"
            )

        if self.fast_path_only:
            self.game.log(
                f"{tile.id}: FAST_PATH_ONLY enabled; skipping Claude fallback"
            )
            return None

        task_prompt = str(detail.get("prompt") or "")
        normalized_title = " ".join(str(detail.get("title") or tile.title).lower().split())
        playbook = TITLE_PLAYBOOKS.get(
            normalized_title,
            CATEGORY_PLAYBOOKS.get(
                category,
                "Inspect the materials with tools, compute the result, and verify it.",
            ),
        )
        prompt = (
            f"Task id: {tile.id}\n"
            f"Category: {category}\n"
            f"Points: {detail.get('points', tile.points)}\n"
            f"Answer format: {answer_format}\n"
            f"Attempt: {attempt}\n"
            f"Downloaded files: {names or 'none'}\n\n"
            f"Category playbook: {playbook}\n\n"
            f"Task:\n{task_prompt[:12_000]}"
            + (
                "\n[Task instructions truncated at 12,000 characters.]"
                if len(task_prompt) > 12_000
                else ""
            )
        )
        if attempt > 1:
            prompt += (
                "\n\nA prior attempt exhausted its tool budget. Reuse any helper "
                "files already in the workspace, choose a different approach, "
                "and reserve the final turn for record_answer."
            )
        if family_failure_hint:
            prompt += (
                "\n\nA sibling in this generator family exhausted with this compact "
                f"report:\n{family_failure_hint[:1_200]}\n"
                "Do not repeat the same failing tool sequence. Choose a materially "
                "different approach and validate it before record_answer."
            )
        if self.verbose:
            self.game.log(f"{tile.id} solver prompt:\n{prompt}\n---")

        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        client = self.game.anthropic_client()

        turn_budget = self._turn_budget(tile.points)
        tool_counts: Counter[str] = Counter()
        tool_errors = 0
        input_tokens = 0
        output_tokens = 0
        stop_reasons: list[str] = []
        record_errors: list[str] = []
        last_turn_tools: list[str] = []
        turns_used = 0
        finalizer_used = False
        final_turn = turn_budget + 1
        for turn in range(1, final_turn + 1):
            cancellation.check()
            finalizing = turn == final_turn
            if finalizing:
                if tool_counts["record_answer"] == 0:
                    break
                finalizer_used = True
                messages = self._finalization_messages(
                    tile,
                    category,
                    answer_format,
                    runtime,
                    record_errors,
                )
            else:
                self._compact_history(messages)
            request = {
                "model": self.game.MODEL,
                "max_tokens": min(self.max_tokens, 768)
                if finalizing
                else self.max_tokens,
                "temperature": 0,
                "system": (
                    BASE_SYSTEM_PROMPT + FINALIZATION_SYSTEM_PROMPT
                    if finalizing
                    else BASE_SYSTEM_PROMPT
                ),
                "tools": FINALIZATION_TOOLS if finalizing else TOOL_SCHEMAS,
                "messages": list(messages),
            }
            response = self._model_call(client, request, cancellation)
            turns_used = turn
            usage = getattr(response, "usage", None)
            input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
            stop_reason = str(getattr(response, "stop_reason", "") or "")
            if stop_reason:
                stop_reasons.append(stop_reason)
            cancellation.check()
            assistant_content = [self._block_dict(block) for block in response.content]
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results: list[dict[str, Any]] = []
            turn_tools: list[str] = []
            for block in response.content:
                cancellation.check()
                if getattr(block, "type", None) != "tool_use":
                    continue
                tool_name = str(block.name)
                turn_tools.append(tool_name)
                tool_input = dict(block.input or {})
                result = runtime.execute(tool_name, tool_input)
                tool_counts[tool_name] += 1
                parsed_result: dict[str, Any] = {}
                try:
                    parsed_result = json.loads(result)
                    if not parsed_result.get("ok", True):
                        tool_errors += 1
                except (json.JSONDecodeError, AttributeError):
                    tool_errors += 1
                if tool_name == "record_answer" and not parsed_result.get("ok"):
                    record_errors.append(str(parsed_result.get("error") or result)[:500])
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
            if not finalizing:
                last_turn_tools = turn_tools

            if runtime.recorded_answer is not None:
                recorded = runtime.recorded_answer
                recorded_recipe = (
                    replace(
                        recorded.replay_recipe,
                        generator_fingerprint=generator_fingerprint,
                        fingerprint_version=fingerprint.fingerprint_version,
                        answer_format=answer_format,
                        scheduler_key=tile.recipe_key,
                    )
                    if recorded.replay_recipe is not None
                    else None
                )
                self._log_summary(
                    tile.id,
                    turn,
                    turn_budget,
                    tool_counts,
                    tool_errors,
                    input_tokens,
                    output_tokens,
                    stop_reasons,
                    "recorded",
                    finalizing=finalizing,
                )
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
                    strategy="claude",
                    verification_source=recorded.verification_source,
                    generator_fingerprint=generator_fingerprint,
                    replay_recipe=recorded_recipe,
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

        self._log_summary(
            tile.id,
            turns_used,
            turn_budget,
            tool_counts,
            tool_errors,
            input_tokens,
            output_tokens,
            stop_reasons,
            "exhausted",
            finalizing=finalizer_used,
        )
        tools = ",".join(
            f"{name}:{count}" for name, count in sorted(tool_counts.items())
        ) or "none"
        last_tools = ",".join(last_turn_tools) or "none"
        errors = " | ".join(record_errors[-2:]) or "none"
        self._store_failure_report(
            tile.id,
            f"tools={tools}; last_turn_tools={last_tools}; "
            f"record_answer_errors={errors}",
        )
        return None

    def _turn_budget(self, points: int) -> int:
        if points >= 500:
            return self.max_turns
        if points >= 400:
            return min(self.max_turns, 10)
        return min(self.max_turns, 8)

    @staticmethod
    def _finalization_messages(
        tile: Tile,
        category: str,
        answer_format: str,
        runtime: TaskToolRuntime,
        record_errors: list[str],
    ) -> list[dict[str, Any]]:
        payload = {
            "task_id": tile.id,
            "category": category,
            "answer_format": answer_format,
            "recent_artifacts": runtime.artifacts.recent(),
            "record_answer_errors": record_errors[-3:],
        }
        return [
            {
                "role": "user",
                "content": (
                    "Finalize from this bounded evidence snapshot. Call "
                    "record_answer only if the evidence supports one exact answer.\n"
                    + json.dumps(payload, ensure_ascii=False)
                ),
            }
        ]

    def _log_summary(
        self,
        task_id: str,
        turns: int,
        budget: int,
        tool_counts: Counter[str],
        tool_errors: int,
        input_tokens: int,
        output_tokens: int,
        stop_reasons: list[str],
        outcome: str,
        *,
        finalizing: bool = False,
    ) -> None:
        tools = ",".join(
            f"{name}:{count}" for name, count in sorted(tool_counts.items())
        ) or "none"
        stops = ",".join(dict.fromkeys(stop_reasons)) or "unknown"
        self.game.log(
            f"{task_id}: solver summary outcome={outcome} turns={turns}/{budget} "
            f"tools={tools} tool_errors={tool_errors} "
            f"tokens_in={input_tokens} tokens_out={output_tokens} stops={stops} "
            f"finalizer={int(finalizing)}"
        )

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
    def _compact_history(messages: list[dict[str, Any]]) -> None:
        """Bound old turns while retaining valid tool_use/tool_result pairing."""

        if len(messages) <= 4:
            return
        for message in messages[1:-2]:
            content = message.get("content")
            if isinstance(content, str):
                if len(content) > 1_200:
                    message["content"] = content[:1_200] + "\n[older text compacted]"
                continue
            if not isinstance(content, list):
                continue
            compacted: list[dict[str, Any]] = []
            for block in content:
                block = dict(block)
                block_type = block.get("type")
                if block_type == "tool_use":
                    tool_input = dict(block.get("input") or {})
                    for key in ("code", "content"):
                        if key in tool_input and len(str(tool_input[key])) > 600:
                            tool_input[key] = (
                                f"<older {key} compacted; "
                                f"{len(str(tool_input[key]))} chars>"
                            )
                    block["input"] = tool_input
                elif block_type == "tool_result":
                    result = str(block.get("content") or "")
                    artifact_ids = sorted(set(re.findall(r"artifact-\d+", result)))
                    block["content"] = json.dumps(
                        {
                            "compacted_prior_result": True,
                            "artifact_ids": artifact_ids,
                            "sample": result[:1_200],
                        }
                    )
                elif block_type == "text":
                    text = str(block.get("text") or "")
                    block["text"] = text[:800]
                compacted.append(block)
            message["content"] = compacted

    @staticmethod
    def _try_replay(runtime: TaskToolRuntime, recipe: ReplayRecipe):
        first = json.loads(
            runtime.execute("run_python", {"code": recipe.code, "timeout": 30})
        )
        second = json.loads(
            runtime.execute("run_python", {"code": recipe.code, "timeout": 30})
        )
        for result in (first, second):
            if (
                not result.get("ok")
                or result.get("returncode") != 0
                or result.get("stdout_artifact_truncated")
            ):
                return None
        first_artifact = first.get("stdout_artifact")
        second_artifact = second.get("stdout_artifact")
        if not first_artifact or not second_artifact:
            return None
        if runtime.artifacts.get(first_artifact) != runtime.artifacts.get(
            second_artifact
        ):
            return None
        record_arguments: dict[str, Any] = {
            "artifact_id": first_artifact,
            "verification_artifact_id": second_artifact,
            "confidence": 0.99,
            "evidence": "cached recipe replay produced identical output twice",
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
