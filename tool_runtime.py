"""Task-scoped tools exposed to the model.

The runtime keeps filesystem access inside one downloaded task directory,
persists HTTP cookies per named session, caps output, and stores exact tool
results as artifacts so answers do not have to be retyped by the model.
"""

from __future__ import annotations

import csv
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
from bs4 import BeautifulSoup

from agent_types import CancellationToken, ReplayRecipe, SolveCancelled

MAX_TOOL_RESPONSE = 8_000
MAX_TOOL_SAMPLE = 4_000
MAX_ARTIFACT_TEXT = 250_000
MAX_READ_PER_FILE = 12_000


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
        "name": "inspect_file",
        "description": (
            "Inspect a file compactly before reading it. Returns CSV columns and "
            "aggregates, JSON shape, text/log counts and samples, or binary magic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read one small targeted window from a task file. Never returns a "
            "whole large document. Inspect or search first. The window is stored "
            "as an artifact for exact answer extraction."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 4000,
                    "default": 3000,
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
                    "maximum": 40,
                    "default": 20,
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_artifact",
        "description": (
            "Regex-search a stored stdout, HTTP body, or prior file window. "
            "Returns only bounded matches with short surrounding context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string"},
                "pattern": {"type": "string"},
                "ignore_case": {"type": "boolean", "default": False},
                "max_matches": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 10,
                },
                "context_chars": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 300,
                    "default": 100,
                },
            },
            "required": ["artifact_id", "pattern"],
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
            "files, compute aggregate summaries, decode data, or verify results. "
            "Only compact stdout/stderr samples are returned; full stdout is kept "
            "as an internal artifact."
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
            "cookies across calls for login and multi-step flows. Returns compact "
            "JSON shape, HTML forms/links/text, or text samples. Full response "
            "bodies remain internal artifacts searchable with search_artifact."
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
        self._read_totals: dict[str, int] = {}
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
                return self._result(ok=True, sample=value[:MAX_TOOL_SAMPLE])
            return self._result(ok=True, **value)
        except SolveCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - tool errors return to the model
            return self._result(ok=False, error=f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _result(**value: Any) -> str:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
        if len(serialized) <= MAX_TOOL_RESPONSE:
            return serialized
        artifact_ids = sorted(set(re.findall(r"artifact-\d+", serialized)))
        compacted = {
            "ok": value.get("ok", True),
            "compacted": True,
            "artifact_ids": artifact_ids,
            "available_fields": sorted(value),
            "sample": serialized[:3_000],
            "instruction": (
                "Oversized result compacted. Use inspect_file, search_files, "
                "or search_artifact for targeted evidence."
            ),
        }
        compacted_text = json.dumps(compacted, ensure_ascii=False)
        if len(compacted_text) > MAX_TOOL_RESPONSE:
            compacted["sample"] = serialized[:1_500]
            compacted_text = json.dumps(compacted, ensure_ascii=False)
        return compacted_text

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
        total_files = 0
        total_bytes = 0
        for path in sorted(self.root.glob(glob)):
            self.cancellation.check()
            if not path.is_file():
                continue
            size = path.stat().st_size
            total_files += 1
            total_bytes += size
            if len(entries) < 120:
                entries.append(
                    {
                        "path": str(path.relative_to(self.root)),
                        "bytes": size,
                    }
                )
        return {
            "file_count": total_files,
            "total_bytes": total_bytes,
            "files": entries,
            "files_truncated": total_files > len(entries),
        }

    def _tool_inspect_file(self, path: str) -> dict[str, Any]:
        file_path = self._path(path)
        if not file_path.is_file():
            raise ValueError(f"not a file: {path!r}")
        suffix = file_path.suffix.lower()
        if suffix in {".csv", ".tsv"}:
            return self._inspect_delimited(path, file_path, suffix)

        raw = file_path.read_bytes()
        self.cancellation.check()
        base: dict[str, Any] = {"path": path, "bytes": len(raw), "suffix": suffix}
        if b"\x00" in raw[:4096]:
            return {
                **base,
                "kind": "binary",
                "magic_hex": raw[:64].hex(" "),
            }

        text = raw.decode("utf-8", errors="replace")
        if suffix in {".json", ".jsonl"}:
            try:
                if suffix == ".jsonl":
                    lines = [line for line in text.splitlines() if line.strip()]
                    sample_values = [json.loads(line) for line in lines[:3]]
                    return {
                        **base,
                        "kind": "jsonl",
                        "line_count": len(lines),
                        "sample_shapes": [
                            self._json_shape(value) for value in sample_values
                        ],
                    }
                parsed = json.loads(text)
                return {
                    **base,
                    "kind": "json",
                    "shape": self._json_shape(parsed),
                    "sample": self._json_sample(parsed),
                }
            except (json.JSONDecodeError, ValueError):
                base["parse_error"] = "invalid JSON; summarized as text"

        lines = text.splitlines()
        upper = raw.upper()
        return {
            **base,
            "kind": "text",
            "line_count": len(lines),
            "level_counts": {
                "error": upper.count(b"ERROR"),
                "warning": upper.count(b"WARN"),
                "info": upper.count(b"INFO"),
            },
            "sample": self._line_sample(lines),
        }

    def _tool_read_file(
        self,
        path: str,
        offset: int = 0,
        limit: int = 3_000,
        mode: str = "text",
    ) -> dict[str, Any]:
        file_path = self._path(path)
        if not file_path.is_file():
            raise ValueError(f"not a file: {path!r}")
        used = self._read_totals.get(path, 0)
        remaining_budget = MAX_READ_PER_FILE - used
        if remaining_budget <= 0:
            raise ValueError(
                f"compact read budget exhausted for {path!r}; use search_files "
                "or run_python for targeted analysis"
            )
        limit = max(1, min(limit, MAX_TOOL_SAMPLE, remaining_budget))
        raw = file_path.read_bytes()
        if mode == "hex":
            limit = min(limit, MAX_TOOL_SAMPLE // 3)
            content = raw[offset : offset + limit].hex(" ")
        else:
            content = raw.decode("utf-8", errors="replace")[offset : offset + limit]
        self._read_totals[path] = used + len(content)
        artifact_id = self.artifacts.put(content)
        return {
            "path": path,
            "artifact_id": artifact_id,
            "sample": content,
            "offset": offset,
            "returned_chars": len(content),
            "remaining_read_budget": MAX_READ_PER_FILE - self._read_totals[path],
            "truncated": offset + limit < len(raw),
            "instruction": "Use search_files for more targeted context.",
        }

    def _tool_search_files(
        self,
        pattern: str,
        glob: str = "**/*",
        ignore_case: bool = False,
        max_matches: int = 20,
    ) -> dict[str, Any]:
        max_matches = max(1, min(max_matches, 40))
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
                    matches.append(f"{relative}:{line_number}:{line[:300]}")
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

    def _tool_search_artifact(
        self,
        artifact_id: str,
        pattern: str,
        ignore_case: bool = False,
        max_matches: int = 10,
        context_chars: int = 100,
    ) -> dict[str, Any]:
        source = self.artifacts.get(artifact_id)
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        max_matches = max(1, min(max_matches, 30))
        context_chars = max(0, min(context_chars, 300))
        matches: list[dict[str, Any]] = []
        for match in regex.finditer(source):
            self.cancellation.check()
            start = max(0, match.start() - context_chars)
            end = min(len(source), match.end() + context_chars)
            matches.append(
                {
                    "span": [match.start(), match.end()],
                    "match": match.group(0)[:300],
                    "context": source[start:end].replace("\x00", "�")[:700],
                }
            )
            if len(matches) >= max_matches:
                break
        return {
            "artifact_id": artifact_id,
            "source_chars": len(source),
            "match_count_returned": len(matches),
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
        stdout_full = stdout[:MAX_ARTIFACT_TEXT]
        stderr_full = stderr[:MAX_ARTIFACT_TEXT]
        artifact_id = self.artifacts.put(stdout_full)
        stderr_artifact = self.artifacts.put(stderr_full) if stderr_full else None
        self._python_producers[artifact_id] = code
        return {
            "returncode": None if timed_out else process.returncode,
            "timed_out": timed_out,
            "stdout_artifact": artifact_id,
            "stdout_summary": self._text_summary(stdout_full),
            "stderr_artifact": stderr_artifact,
            "stderr_summary": self._text_summary(stderr_full),
            "stdout_artifact_truncated": len(stdout) > MAX_ARTIFACT_TEXT,
            "instruction": (
                "Use search_artifact on stdout_artifact or stderr_artifact "
                "instead of printing complete output."
            ),
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
        content_type = response.headers.get("content-type", "")
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
            "body_summary": self._summarize_http_body(body, content_type),
            "body_artifact_truncated": truncated,
            "cookies": client.cookies.get_dict(),
            "instruction": ("Use search_artifact for targeted raw-response evidence."),
        }

    def _inspect_delimited(
        self, display_path: str, file_path: Path, suffix: str
    ) -> dict[str, Any]:
        with file_path.open(
            "r", encoding="utf-8", errors="replace", newline=""
        ) as file:
            prefix = file.read(4096)
            file.seek(0)
            delimiter = "\t" if suffix == ".tsv" else ","
            if suffix != ".tsv":
                try:
                    delimiter = csv.Sniffer().sniff(prefix).delimiter
                except csv.Error:
                    pass
            reader = csv.reader(file, delimiter=delimiter)
            try:
                headers = next(reader)
            except StopIteration:
                return {
                    "path": display_path,
                    "kind": "delimited",
                    "bytes": file_path.stat().st_size,
                    "row_count": 0,
                    "columns": [],
                }

            tracked_headers = headers[:50]
            profiles = [
                {
                    "name": name[:200],
                    "nonempty": 0,
                    "numeric": 0,
                    "min": None,
                    "max": None,
                    "examples": [],
                }
                for name in tracked_headers
            ]
            samples: list[dict[str, str]] = []
            row_count = 0
            for row in reader:
                row_count += 1
                if row_count % 500 == 0:
                    self.cancellation.check()
                if len(samples) < 3:
                    samples.append(
                        {
                            name[:100]: (row[index] if index < len(row) else "")[:120]
                            for index, name in enumerate(tracked_headers[:15])
                        }
                    )
                for index, profile in enumerate(profiles):
                    if index >= len(row):
                        continue
                    value = row[index].strip()
                    if not value:
                        continue
                    profile["nonempty"] += 1
                    examples = profile["examples"]
                    if len(examples) < 3 and value[:80] not in examples:
                        examples.append(value[:80])
                    try:
                        number = float(value.replace(",", ""))
                    except ValueError:
                        continue
                    profile["numeric"] += 1
                    profile["min"] = (
                        number
                        if profile["min"] is None
                        else min(profile["min"], number)
                    )
                    profile["max"] = (
                        number
                        if profile["max"] is None
                        else max(profile["max"], number)
                    )

        schema = []
        for profile in profiles:
            nonempty = profile.pop("nonempty")
            numeric = profile.pop("numeric")
            if nonempty and numeric == nonempty:
                inferred = "number"
            elif numeric:
                inferred = "mixed"
            else:
                inferred = "text"
            schema.append(
                {
                    **profile,
                    "type": inferred,
                    "nonempty_sample_count": nonempty,
                }
            )
        return {
            "path": display_path,
            "kind": "delimited",
            "bytes": file_path.stat().st_size,
            "delimiter": delimiter,
            "row_count": row_count,
            "column_count": len(headers),
            "columns": schema,
            "columns_truncated": len(headers) > len(tracked_headers),
            "row_samples": samples,
        }

    @classmethod
    def _json_shape(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 3:
            return type(value).__name__
        if isinstance(value, dict):
            items = list(value.items())
            return {
                "type": "object",
                "key_count": len(items),
                "keys": {
                    str(key)[:100]: cls._json_shape(child, depth + 1)
                    for key, child in items[:30]
                },
                "keys_truncated": len(items) > 30,
            }
        if isinstance(value, list):
            return {
                "type": "array",
                "length": len(value),
                "item_shapes": [
                    cls._json_shape(child, depth + 1) for child in value[:3]
                ],
            }
        return type(value).__name__

    @classmethod
    def _json_sample(cls, value: Any, depth: int = 0) -> Any:
        if depth >= 2:
            return str(value)[:160]
        if isinstance(value, dict):
            return {
                str(key)[:100]: cls._json_sample(child, depth + 1)
                for key, child in list(value.items())[:10]
            }
        if isinstance(value, list):
            return [cls._json_sample(child, depth + 1) for child in value[:3]]
        if isinstance(value, str):
            return value[:200]
        return value

    @staticmethod
    def _line_sample(lines: list[str]) -> dict[str, Any]:
        return {
            "head": [line[:500] for line in lines[:4]],
            "tail": [line[:500] for line in lines[-4:]] if len(lines) > 4 else [],
        }

    @classmethod
    def _text_summary(cls, text: str) -> dict[str, Any]:
        lines = text.splitlines()
        return {
            "chars": len(text),
            "lines": len(lines),
            **cls._line_sample(lines),
        }

    @classmethod
    def _summarize_http_body(cls, body: str, content_type: str) -> dict[str, Any]:
        stripped = body.lstrip()
        if "json" in content_type.lower() or stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(body)
                return {
                    "kind": "json",
                    "shape": cls._json_shape(parsed),
                    "sample": cls._json_sample(parsed),
                }
            except json.JSONDecodeError:
                pass

        if "html" in content_type.lower() or re.search(
            r"<(?:html|form|body)\b", body[:2000], re.IGNORECASE
        ):
            soup = BeautifulSoup(body, "html.parser")
            forms = []
            for form in soup.find_all("form")[:20]:
                inputs = []
                for field in form.find_all(["input", "select", "textarea"])[:30]:
                    inputs.append(
                        {
                            "tag": field.name,
                            "name": field.get("name"),
                            "type": field.get("type"),
                            "value": str(field.get("value") or "")[:160],
                        }
                    )
                forms.append(
                    {
                        "method": str(form.get("method") or "GET").upper(),
                        "action": form.get("action"),
                        "inputs": inputs,
                    }
                )
            links = [
                {
                    "text": " ".join(link.stripped_strings)[:120],
                    "href": link.get("href"),
                }
                for link in soup.find_all("a", href=True)[:30]
            ]
            text_sample = " ".join(list(soup.stripped_strings)[:80])[:2500]
            return {
                "kind": "html",
                "title": soup.title.get_text(" ", strip=True)[:200]
                if soup.title
                else "",
                "forms": forms,
                "links": links,
                "text_sample": text_sample,
            }

        return {"kind": "text", **cls._text_summary(body)}

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
