"""Task-scoped tools exposed to the model.

The runtime keeps filesystem access inside one downloaded task directory,
persists HTTP cookies per named session, caps output, and stores exact tool
results as artifacts so answers do not have to be retyped by the model.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from agent_types import CancellationToken, ReplayRecipe, SolveCancelled

MAX_TOOL_TEXT = 20_000
MAX_ARTIFACT_TEXT = 250_000


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_files",
        "description": (
            "List task files recursively with byte sizes. Paths are relative to "
            "the task workspace."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "glob": {
                    "type": "string",
                    "description": "Optional glob such as **/*.json.",
                    "default": "**/*",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read a bounded part of a task file as text or hexadecimal bytes. "
            "The result is also saved as an artifact for exact answer extraction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20000,
                    "default": 12000,
                },
                "mode": {
                    "type": "string",
                    "enum": ["text", "hex"],
                    "default": "text",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_files",
        "description": (
            "Regex-search text files in the task workspace. Returns matching "
            "file names, line numbers, and bounded line text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "glob": {"type": "string", "default": "**/*"},
                "ignore_case": {"type": "boolean", "default": False},
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "default": 50,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write_file",
        "description": (
            "Create or replace a UTF-8 file inside the task workspace. Use this "
            "for patches or helper scripts, never for the final answer alone."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string", "maxLength": 200000},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_python",
        "description": (
            "Run Python 3 in the task workspace with a timeout. Use it to parse "
            "files, test hypotheses, decode data, or verify computations. Stdout "
            "is stored verbatim as an artifact."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "maxLength": 100000},
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 60,
                    "default": 20,
                },
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "http_request",
        "description": (
            "Make an HTTP request to the event host. Named sessions preserve "
            "cookies across calls for login and multi-step flows. Response bodies "
            "are stored as artifacts."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session": {"type": "string", "default": "default"},
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                    "default": "GET",
                },
                "url": {
                    "type": "string",
                    "description": "Absolute event-host URL or a /relative/path.",
                },
                "params": {"type": "object"},
                "form": {"type": "object"},
                "json_body": {},
                "headers": {"type": "object"},
                "follow_redirects": {"type": "boolean", "default": True},
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 60,
                    "default": 20,
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "record_answer",
        "description": (
            "Record a final candidate after verification. Prefer artifact_id plus "
            "a regex so the exact value flows from a tool result without being "
            "retyped. Use answer only when no tool can emit the value."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "extraction_regex": {
                    "type": "string",
                    "description": (
                        "Regex applied to the artifact. The selected group becomes "
                        "the answer. Omit to use the whole stripped artifact."
                    ),
                },
                "regex_group": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 20,
                    "default": 1,
                },
                "answer": {"type": "string", "maxLength": 20000},
                "confidence": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                },
                "evidence": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                },
            },
            "required": ["confidence", "evidence"],
            "additionalProperties": False,
        },
    },
]


@dataclass(frozen=True, slots=True)
class RecordedAnswer:
    answer: str
    confidence: float
    evidence: str
    source: str
    replay_recipe: ReplayRecipe | None = None


class ArtifactStore:
    def __init__(self) -> None:
        self._next = 1
        self._values: dict[str, str] = {}

    def put(self, value: str) -> str:
        artifact_id = f"artifact-{self._next:04d}"
        self._next += 1
        self._values[artifact_id] = value[:MAX_ARTIFACT_TEXT]
        return artifact_id

    def get(self, artifact_id: str) -> str:
        try:
            return self._values[artifact_id]
        except KeyError as exc:
            raise ValueError(f"unknown artifact {artifact_id!r}") from exc


class TaskToolRuntime:
    def __init__(
        self,
        root: str | Path,
        base_url: str,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.base_url = base_url.rstrip("/") + "/"
        parsed = urlparse(self.base_url)
        self.allowed_host = parsed.hostname
        self.artifacts = ArtifactStore()
        self.sessions: dict[str, requests.Session] = {}
        self.recorded_answer: RecordedAnswer | None = None
        self._python_producers: dict[str, str] = {}
        self.cancellation = cancellation or CancellationToken()

    def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.cancellation.check()
        try:
            handler = getattr(self, f"_tool_{name}")
        except AttributeError:
            return self._result(ok=False, error=f"unknown tool {name!r}")
        try:
            value = handler(**arguments)
            self.cancellation.check()
            if isinstance(value, str):
                return value
            return self._result(ok=True, **value)
        except SolveCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - tool errors return to the model
            return self._result(ok=False, error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _result(**value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, default=str)

    def _path(self, relative: str) -> Path:
        if not relative or relative == ".":
            return self.root
        path = (self.root / relative).resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("path escapes the task workspace") from exc
        return path

    def _tool_list_files(self, glob: str = "**/*") -> dict[str, Any]:
        entries: list[dict[str, Any]] = []
        for path in sorted(self.root.glob(glob)):
            self.cancellation.check()
            if not path.is_file():
                continue
            entries.append(
                {
                    "path": str(path.relative_to(self.root)),
                    "bytes": path.stat().st_size,
                }
            )
            if len(entries) >= 300:
                break
        return {"files": entries, "truncated": len(entries) >= 300}

    def _tool_read_file(
        self,
        path: str,
        offset: int = 0,
        limit: int = 12_000,
        mode: str = "text",
    ) -> dict[str, Any]:
        file_path = self._path(path)
        if not file_path.is_file():
            raise ValueError(f"not a file: {path!r}")
        raw = file_path.read_bytes()
        if mode == "hex":
            content = raw[offset : offset + limit].hex(" ")
        else:
            content = raw.decode("utf-8", errors="replace")[offset : offset + limit]
        artifact_id = self.artifacts.put(content)
        return {
            "path": path,
            "artifact_id": artifact_id,
            "content": content,
            "truncated": offset + limit < len(raw),
        }

    def _tool_search_files(
        self,
        pattern: str,
        glob: str = "**/*",
        ignore_case: bool = False,
        max_matches: int = 50,
    ) -> dict[str, Any]:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
        matches: list[str] = []
        for path in sorted(self.root.glob(glob)):
            self.cancellation.check()
            if not path.is_file() or path.stat().st_size > 8_000_000:
                continue
            text = path.read_text("utf-8", errors="replace")
            relative = path.relative_to(self.root)
            for line_number, line in enumerate(text.splitlines(), start=1):
                if line_number % 250 == 0:
                    self.cancellation.check()
                if regex.search(line):
                    matches.append(f"{relative}:{line_number}:{line[:500]}")
                    if len(matches) >= max_matches:
                        break
            if len(matches) >= max_matches:
                break
        content = "\n".join(matches)
        artifact_id = self.artifacts.put(content)
        return {
            "artifact_id": artifact_id,
            "matches": matches,
            "truncated": len(matches) >= max_matches,
        }

    def _tool_write_file(self, path: str, content: str) -> dict[str, Any]:
        file_path = self._path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    def _tool_run_python(self, code: str, timeout: int = 20) -> dict[str, Any]:
        safe_env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "PYTHONIOENCODING": "utf-8",
            "TASK_DIR": str(self.root),
        }
        process = subprocess.Popen(
            [sys.executable, "-I", "-c", code],
            cwd=self.root,
            env=safe_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + max(1, min(timeout, 60))
        timed_out = False
        while True:
            if self.cancellation.cancelled:
                self._kill_process_tree(process)
                process.communicate()
                self.cancellation.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                self._kill_process_tree(process)
                stdout, stderr = process.communicate()
                break
            try:
                stdout, stderr = process.communicate(timeout=min(0.1, remaining))
                break
            except subprocess.TimeoutExpired:
                continue

        self.cancellation.check()
        stdout = stdout[:MAX_ARTIFACT_TEXT]
        stderr = stderr[:MAX_TOOL_TEXT]
        artifact_id = self.artifacts.put(stdout)
        self._python_producers[artifact_id] = code
        return {
            "returncode": None if timed_out else process.returncode,
            "timed_out": timed_out,
            "stdout_artifact": artifact_id,
            "stdout": stdout[:MAX_TOOL_TEXT],
            "stderr": stderr,
            "stdout_truncated": len(stdout) > MAX_TOOL_TEXT,
        }

    @staticmethod
    def _kill_process_tree(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return

    def _safe_url(self, url: str) -> str:
        target = urljoin(self.base_url, url)
        parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("only http and https URLs are allowed")
        if not parsed.hostname or parsed.hostname != self.allowed_host:
            raise ValueError(
                f"HTTP tools are restricted to event host {self.allowed_host!r}"
            )
        return target

    def _tool_http_request(
        self,
        url: str,
        session: str = "default",
        method: str = "GET",
        params: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        json_body: Any = None,
        headers: dict[str, Any] | None = None,
        follow_redirects: bool = True,
        timeout: int = 20,
    ) -> dict[str, Any]:
        target = self._safe_url(url)
        client = self.sessions.setdefault(session, requests.Session())
        response = client.request(
            method=method,
            url=target,
            params=params,
            data=form,
            json=json_body,
            headers={str(k): str(v) for k, v in (headers or {}).items()},
            allow_redirects=follow_redirects,
            timeout=min(5, max(1, timeout)),
            stream=True,
        )
        chunks: list[bytes] = []
        body_bytes = 0
        truncated = False
        try:
            for chunk in response.iter_content(chunk_size=8192):
                self.cancellation.check()
                if not chunk:
                    continue
                remaining = MAX_ARTIFACT_TEXT - body_bytes
                if len(chunk) > remaining:
                    chunks.append(chunk[:remaining])
                    truncated = True
                    break
                chunks.append(chunk)
                body_bytes += len(chunk)
                if body_bytes >= MAX_ARTIFACT_TEXT:
                    truncated = True
                    break
        finally:
            response.close()
        encoding = response.encoding or "utf-8"
        body = b"".join(chunks).decode(encoding, errors="replace")
        artifact_id = self.artifacts.put(body)
        useful_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {"content-type", "location", "set-cookie", "www-authenticate"}
        }
        return {
            "status": response.status_code,
            "url": response.url,
            "headers": useful_headers,
            "body_artifact": artifact_id,
            "body": body[:MAX_TOOL_TEXT],
            "body_truncated": truncated or len(body) > MAX_TOOL_TEXT,
            "cookies": client.cookies.get_dict(),
        }

    def _tool_record_answer(
        self,
        confidence: float,
        evidence: str,
        artifact_id: str | None = None,
        extraction_regex: str | None = None,
        regex_group: int = 1,
        answer: str | None = None,
    ) -> dict[str, Any]:
        if bool(artifact_id) == bool(answer is not None):
            raise ValueError("provide exactly one of artifact_id or answer")

        if artifact_id:
            source_text = self.artifacts.get(artifact_id)
            if extraction_regex:
                match = re.search(extraction_regex, source_text, re.MULTILINE)
                if not match:
                    raise ValueError("extraction_regex did not match the artifact")
                try:
                    resolved = match.group(regex_group)
                except IndexError as exc:
                    raise ValueError(
                        f"regex has no capture group {regex_group}"
                    ) from exc
            else:
                resolved = source_text.strip()
            source = f"artifact:{artifact_id}"
        else:
            resolved = str(answer)
            source = "model"

        if not resolved.strip():
            raise ValueError("resolved answer is empty")
        self.recorded_answer = RecordedAnswer(
            answer=resolved.strip(),
            confidence=float(confidence),
            evidence=evidence.strip(),
            source=source,
            replay_recipe=(
                ReplayRecipe(
                    code=self._python_producers[artifact_id],
                    extraction_regex=extraction_regex or "",
                    regex_group=regex_group,
                )
                if artifact_id in self._python_producers
                else None
            ),
        )
        return {
            "recorded": True,
            "source": source,
            "answer_length": len(self.recorded_answer.answer),
            "answer_preview": self.recorded_answer.answer[:200],
        }
