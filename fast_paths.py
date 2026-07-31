"""Conservative deterministic solvers for stable Jeopardy generator families.

The dispatcher recognizes a family from title, category, and prompt markers,
then validates file extensions, schemas, archive metadata, and magic bytes
before any answer can be produced. A recognizable but malformed task is
inconclusive: callers must fall back to the model rather than guess.
"""

from __future__ import annotations

import ast
import base64
import binascii
import bisect
import calendar
import csv
import hashlib
import io
import itertools
import json
import math
import os
import re
import signal
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import zlib
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol
from urllib.parse import urljoin, urlparse
from zipfile import BadZipFile, ZipFile

from bs4 import BeautifulSoup, Comment

from agent_types import CancellationToken, SolveCancelled, Tile
from tool_runtime import TaskToolRuntime, python_resource_prelude

MAX_FINGERPRINT_FILES = 160
MAX_ARCHIVE_MEMBERS = 256
MAX_ARCHIVE_EXPANDED = 16_000_000
MAX_FAST_FILE_BYTES = 12_000_000
MAX_DIRECT_OUTPUT = 64_000
TOKEN_CHARS = r"[A-Z0-9]"
GENERATOR_FINGERPRINT_VERSION = 1


class FastPathStatus(str, Enum):
    SOLVED = "solved"
    NOT_APPLICABLE = "not_applicable"
    INCONCLUSIVE = "inconclusive"


class FastPathError(RuntimeError):
    """An invariant failed after a task family was recognized."""


class FastPathDeadline(FastPathError):
    """A handler exceeded its own bounded deadline."""


@dataclass(frozen=True, slots=True)
class ArchiveMember:
    name: str
    compressed_bytes: int
    expanded_bytes: int
    crc: int


@dataclass(frozen=True, slots=True)
class FileFingerprint:
    path: str
    suffix: str
    size: int
    magic_hex: str
    kind: str
    columns: tuple[str, ...] = ()
    json_keys: tuple[str, ...] = ()
    archive_members: tuple[ArchiveMember, ...] = ()
    archive_total_expanded: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class TaskFingerprint:
    title: str
    normalized_title: str
    category: str
    normalized_category: str
    prompt: str = field(repr=False)
    normalized_prompt: str = field(repr=False)
    prompt_markers: frozenset[str]
    answer_format: str
    files: tuple[FileFingerprint, ...]
    inspection_errors: tuple[str, ...] = ()

    @classmethod
    def build(
        cls,
        tile: Tile,
        detail: dict[str, Any],
        names: list[str],
        root: Path,
        cancellation: CancellationToken,
    ) -> TaskFingerprint:
        title = str(detail.get("title") or tile.title or "")
        category = str(detail.get("category") or tile.category or "")
        prompt = str(detail.get("prompt") or "")
        normalized_prompt = _normalize(prompt)
        markers = frozenset(re.findall(r"[a-z0-9][a-z0-9_.-]*", normalized_prompt))
        files: list[FileFingerprint] = []
        errors: list[str] = []
        if len(names) > MAX_FINGERPRINT_FILES:
            errors.append("file_count_exceeds_limit")
        for name in names[:MAX_FINGERPRINT_FILES]:
            cancellation.check()
            try:
                path = _safe_task_path(root, name)
                files.append(_fingerprint_file(path, name, cancellation))
            except (
                OSError,
                FastPathError,
                TypeError,
                ValueError,
                BadZipFile,
                csv.Error,
                json.JSONDecodeError,
            ) as exc:
                errors.append(f"{name}:{type(exc).__name__}")
                try:
                    path = _safe_task_path(root, name)
                    size = path.stat().st_size if path.is_file() else 0
                    if path.is_file():
                        with path.open("rb") as stream:
                            magic = stream.read(16).hex()
                    else:
                        magic = ""
                except (OSError, ValueError):
                    size = 0
                    magic = ""
                files.append(
                    FileFingerprint(
                        path=name,
                        suffix=Path(name).suffix.lower(),
                        size=size,
                        magic_hex=magic,
                        kind="error",
                        error=type(exc).__name__,
                    )
                )
        return cls(
            title=title,
            normalized_title=_normalize(title),
            category=category,
            normalized_category=_normalize(category),
            prompt=prompt,
            normalized_prompt=normalized_prompt,
            prompt_markers=markers,
            answer_format=str(detail.get("answer_format") or "exact"),
            files=tuple(files),
            inspection_errors=tuple(errors),
        )

    def file(self, name: str) -> FileFingerprint | None:
        return next((item for item in self.files if item.path == name), None)

    def files_with_suffix(self, suffix: str) -> tuple[FileFingerprint, ...]:
        return tuple(item for item in self.files if item.suffix == suffix.lower())

    @property
    def fingerprint_version(self) -> int:
        return GENERATOR_FINGERPRINT_VERSION

    @property
    def generator_fingerprint(self) -> str:
        """Versioned structural identity for safe sibling recipe replay."""

        files = []
        for item in sorted(self.files, key=lambda value: value.path):
            files.append(
                {
                    "path": _path_template(item.path),
                    "suffix": item.suffix,
                    "kind": item.kind,
                    "magic": _magic_type(item),
                    "columns": list(item.columns),
                    "json_keys": list(item.json_keys),
                    "archive_members": [
                        {
                            "path": _path_template(member.name),
                            "suffix": PurePosixPath(member.name).suffix.lower(),
                        }
                        for member in sorted(
                            item.archive_members, key=lambda value: value.name
                        )
                    ],
                    "error": item.error,
                }
            )
        payload = {
            "version": self.fingerprint_version,
            "title": self.normalized_title,
            "category": self.normalized_category,
            "answer_format": self.answer_format,
            "prompt_template": _prompt_template(self.prompt),
            "files": files,
            "inspection_error_types": sorted(
                error.rsplit(":", 1)[-1] for error in self.inspection_errors
            ),
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return f"gf{self.fingerprint_version}:{hashlib.sha256(encoded).hexdigest()}"

    def compact_summary(self) -> str:
        extensions = Counter(item.suffix or "<none>" for item in self.files)
        kinds = Counter(item.kind for item in self.files)
        schemas = sum(bool(item.columns or item.json_keys) for item in self.files)
        archives = sum(bool(item.archive_members) for item in self.files)
        magic_types: set[str] = set()
        for item in self.files:
            if item.magic_hex.startswith("504b"):
                magic_types.add("zip")
            elif item.magic_hex.startswith("89504e470d0a1a0a"):
                magic_types.add("png")
            elif item.magic_hex.startswith("414a4231"):
                magic_types.add("ajb1")
            elif item.kind == "binary":
                magic_types.add("binary")
        return (
            f"generator={self.generator_fingerprint[:16]} "
            f"title={self.normalized_title or '<none>'} "
            f"category={self.normalized_category or '<none>'} "
            f"format={self.answer_format} files={len(self.files)} "
            f"extensions={dict(sorted(extensions.items()))} "
            f"kinds={dict(sorted(kinds.items()))} schemas={schemas} "
            f"archives={archives} magic_types={sorted(magic_types)} "
            f"errors={len(self.inspection_errors)}"
        )


@dataclass(frozen=True, slots=True)
class FastPathOutcome:
    status: FastPathStatus
    handler: str = ""
    answer: str = ""
    source: str = ""
    verification_source: str = ""
    evidence: str = ""
    confidence: float = 0.0
    elapsed_seconds: float = 0.0
    reason: str = ""
    fingerprint: TaskFingerprint | None = field(default=None, repr=False)

    @classmethod
    def not_applicable(
        cls, fingerprint: TaskFingerprint, reason: str = "no registered match"
    ) -> FastPathOutcome:
        return cls(
            status=FastPathStatus.NOT_APPLICABLE,
            reason=reason,
            fingerprint=fingerprint,
        )


@dataclass(slots=True)
class FastPathContext:
    tile: Tile
    detail: dict[str, Any]
    fingerprint: TaskFingerprint
    root: Path
    runtime: TaskToolRuntime
    cancellation: CancellationToken
    deadline: float

    def check(self) -> None:
        self.cancellation.check()
        if time.monotonic() > self.deadline:
            raise FastPathDeadline("handler deadline exceeded")

    def path(self, name: str) -> Path:
        self.check()
        return _safe_task_path(self.root, name)

    def text(self, name: str, *, max_bytes: int = MAX_FAST_FILE_BYTES) -> str:
        path = self.path(name)
        if path.stat().st_size > max_bytes:
            raise FastPathError(f"{name} exceeds fast-path size limit")
        raw = path.read_bytes()
        self.check()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FastPathError(f"{name} is not UTF-8") from exc

    def artifact(self, answer: str) -> str:
        artifact_id = self.runtime.artifacts.put(answer)
        return f"artifact:{artifact_id}"


class FastPathHandler(Protocol):
    name: ClassVar[str]
    title: ClassVar[str]
    timeout_seconds: ClassVar[float]

    def recognizes(self, fingerprint: TaskFingerprint) -> bool: ...

    def solve(self, context: FastPathContext) -> tuple[str, str, float]: ...


class BaseHandler:
    name = "base"
    title = ""
    category = ""
    answer_formats: tuple[str, ...] = ()
    prompt_phrases: tuple[str, ...] = ()
    timeout_seconds = 20.0

    def recognizes(self, fingerprint: TaskFingerprint) -> bool:
        return (
            fingerprint.normalized_title == _normalize(self.title)
            and (
                not self.category
                or fingerprint.normalized_category == _normalize(self.category)
            )
            and (
                not self.answer_formats
                or fingerprint.answer_format in self.answer_formats
            )
            and all(
                _normalize(phrase) in fingerprint.normalized_prompt
                for phrase in self.prompt_phrases
            )
        )


class DirtyLedgerHandler(BaseHandler):
    name = "dirty_ledger"
    title = "Dirty Ledger"
    category = "Needle in the Haystack"
    answer_formats = ("numeric",)
    prompt_phrases = ("amount column is messy", "status is exactly")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"transactions.csv"})
        file = _require_file(context.fingerprint, "transactions.csv")
        _require_columns(file, {"txn_id", "status", "amount"})
        status_match = re.search(
            r"status is exactly\s*[\"']([A-Za-z0-9_-]+)[\"']",
            context.fingerprint.prompt,
            re.IGNORECASE,
        )
        if not status_match:
            raise FastPathError("target ledger status is missing")
        target_status = status_match.group(1)
        path = context.path("transactions.csv")
        total_decimal, valid_rows = _dirty_ledger_decimal(
            path, target_status, context
        )
        total_cents, valid_rows_again = _dirty_ledger_cents(
            path, target_status, context
        )
        if valid_rows != valid_rows_again or total_decimal != Decimal(total_cents) / 100:
            raise FastPathError("independent money totals disagree")
        answer = _format_decimal(total_decimal.quantize(Decimal("0.01")), places=2)
        evidence = (
            f"parsed exact {target_status!r} rows twice with Decimal and integer cents; "
            f"valid_rows={valid_rows} totals matched"
        )
        return answer, evidence, 0.995


class EventHorizonHandler(BaseHandler):
    name = "event_horizon"
    title = "Event Horizon"
    category = "Needle in the Haystack"
    answer_formats = ("exact_ci",)
    prompt_phrases = ("events.jsonl", "unique winner", "inclusive")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"events.jsonl"})
        file = _require_file(context.fingerprint, "events.jsonl")
        _require_json_keys(file, {"user", "type", "ts"})
        match = re.search(
            r'events of type ["\']([^"\']+)["\'].*?'
            r"from (\d{4}-\d{2}-\d{2}) through (\d{4}-\d{2}-\d{2}) inclusive",
            context.fingerprint.prompt,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            raise FastPathError("could not parse event type and inclusive date range")
        event_type, start_text, end_text = match.groups()
        start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
        if start > end:
            raise FastPathError("event date range is reversed")
        path = context.path("events.jsonl")
        counts: Counter[str] = Counter()
        mirror: dict[str, int] = {}
        rows = 0
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if line_number % 500 == 0:
                    context.check()
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict) or not {"user", "type", "ts"} <= value.keys():
                    raise FastPathError(f"malformed event at line {line_number}")
                rows += 1
                timestamp = _utc_datetime(str(value["ts"]))
                if value["type"] == event_type and start <= timestamp.date() <= end:
                    user = str(value["user"])
                    counts[user] += 1
                    mirror[user] = mirror.get(user, 0) + 1
        if dict(counts) != mirror:
            raise FastPathError("independent event counts disagree")
        winner, winning_count = _unique_counter_winner(counts)
        return (
            winner,
            f"validated {rows} JSONL rows; two counts agree; unique maximum={winning_count}",
            0.995,
        )


class JoinTheDotsHandler(BaseHandler):
    name = "join_the_dots"
    title = "Join the Dots"
    category = "Needle in the Haystack"
    answer_formats = ("exact_ci",)
    prompt_phrases = ("keep only the first occurrence", "exclude orders", "highest total")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"orders.jsonl", "users.csv"})
        _require_columns(
            _require_file(context.fingerprint, "users.csv"),
            {"id", "email", "signup"},
        )
        _require_json_keys(
            _require_file(context.fingerprint, "orders.jsonl"),
            {"order_id", "user_id", "amount_cents", "status"},
        )
        users: dict[str, str] = {}
        with context.path("users.csv").open("r", encoding="utf-8", newline="") as stream:
            for row in csv.DictReader(stream):
                context.check()
                user_id, email = str(row.get("id", "")).strip(), str(
                    row.get("email", "")
                ).strip()
                if not user_id or not email or user_id in users:
                    raise FastPathError("users.csv has a missing or duplicate user")
                users[user_id] = email

        seen: set[str] = set()
        totals: dict[str, int] = defaultdict(int)
        kept: list[tuple[str, int]] = []
        with context.path("orders.jsonl").open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if line_number % 500 == 0:
                    context.check()
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict) or not {
                    "order_id",
                    "user_id",
                    "amount_cents",
                    "status",
                } <= value.keys():
                    raise FastPathError(f"malformed order at line {line_number}")
                order_id = str(value["order_id"])
                if order_id in seen:
                    continue
                seen.add(order_id)
                if value["status"] == "refunded":
                    continue
                user_id = str(value["user_id"])
                amount = value["amount_cents"]
                if isinstance(amount, bool) or not isinstance(amount, int):
                    raise FastPathError("amount_cents is not an integer")
                if user_id not in users:
                    raise FastPathError("order refers to an unknown user")
                totals[user_id] += amount
                kept.append((user_id, amount))
        mirror: dict[str, int] = defaultdict(int)
        for user_id, amount in kept:
            mirror[user_id] += amount
        if dict(totals) != dict(mirror):
            raise FastPathError("independent joined totals disagree")
        winner, winning_total = _unique_mapping_winner(totals)
        return (
            users[winner],
            f"deduplicated {len(seen)} order ids; totals recomputed; unique cents maximum={winning_total}",
            0.995,
        )


class SessionHunterHandler(BaseHandler):
    name = "session_hunter"
    title = "Session Hunter"
    category = "Needle in the Haystack"
    answer_formats = ("exact_ci",)
    prompt_phrases = ("session is a maximal run", "session-day count", "lines are not sorted")
    timeout_seconds = 25.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"server.log"})
        match = re.search(
            r"during ([A-Za-z]+) (\d{4}).*?(\d+)(?:st|nd|rd|th)-highest",
            context.fingerprint.prompt,
            re.IGNORECASE | re.DOTALL,
        )
        gap_match = re.search(
            r"at most (\d+) minutes apart", context.fingerprint.prompt, re.IGNORECASE
        )
        if not match or not gap_match:
            raise FastPathError("could not parse target month, rank, and session gap")
        month_name, year_text, rank_text = match.groups()
        try:
            month = list(calendar.month_name).index(month_name.title())
        except ValueError as exc:
            raise FastPathError("invalid target month") from exc
        year, rank = int(year_text), int(rank_text)
        max_gap = timedelta(minutes=int(gap_match.group(1)))
        events: dict[str, list[datetime]] = defaultdict(list)
        rows = 0
        with context.path("server.log").open(
            "r", encoding="utf-8", errors="strict"
        ) as stream:
            for line_number, line in enumerate(stream, 1):
                if line_number % 1_000 == 0:
                    context.check()
                user_match = re.search(r"\buser=([A-Za-z0-9_.-]+)", line)
                timestamp = _log_timestamp(line, year)
                if not user_match or timestamp is None:
                    raise FastPathError(f"malformed log line {line_number}")
                events[user_match.group(1)].append(timestamp)
                rows += 1
        day_counts: dict[str, int] = {}
        for index, (user, timestamps) in enumerate(events.items()):
            if index % 50 == 0:
                context.check()
            session_days: set[date] = set()
            previous: datetime | None = None
            for timestamp in sorted(timestamps):
                if (
                    (previous is None or timestamp - previous > max_gap)
                    and timestamp.year == year
                    and timestamp.month == month
                ):
                    session_days.add(timestamp.date())
                previous = timestamp
            day_counts[user] = len(session_days)
        ranked = sorted(day_counts.items(), key=lambda item: (-item[1], item[0]))
        if rank < 1 or rank > len(ranked):
            raise FastPathError("requested session rank is unavailable")
        if (rank > 1 and ranked[rank - 2][1] == ranked[rank - 1][1]) or (
            rank < len(ranked) and ranked[rank][1] == ranked[rank - 1][1]
        ):
            raise FastPathError("requested session rank is tied")
        user, count = ranked[rank - 1]
        return (
            user,
            f"parsed {rows} unsorted log rows; rebuilt sessions for {len(events)} users; rank={rank} count={count}",
            0.99,
        )


class ThirtySystemsHandler(BaseHandler):
    name = "thirty_systems"
    title = "Thirty Systems"
    category = "Needle in the Haystack"
    answer_formats = ("numeric",)
    prompt_phrases = (
        "alphabetical order of filename",
        "first occurrence",
        "usd_per_unit",
        "round",
        "2 decimals",
    )
    timeout_seconds = 20.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"regional_sales.zip"})
        archive_file = _require_file(
            context.fingerprint, "regional_sales.zip"
        )
        if archive_file.kind != "zip":
            raise FastPathError("regional_sales.zip is not a ZIP archive")
        with ZipFile(context.path("regional_sales.zip")) as archive:
            infos = _validate_archive(archive, context)
            names = {item.filename for item in infos if not item.is_dir()}
            expected_sales = sorted(
                name
                for name in names
                if re.fullmatch(r"sales_[A-Z]{2}_\d{3}\.csv", name)
            )
            if "fx.csv" not in names or len(expected_sales) != 16:
                raise FastPathError("regional archive manifest is unexpected")
            rates = _regional_fx_rates(archive.read("fx.csv"))
            total, kept = _regional_sales_total(
                archive, expected_sales, rates, context
            )
        mirror = sum(
            (amount * rates[region] for _txn, region, amount in kept),
            Decimal(0),
        )
        if mirror != total or len({txn for txn, _region, _amount in kept}) != len(
            kept
        ):
            raise FastPathError("regional revenue verification disagreed")
        rounded = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return (
            _format_decimal(rounded, places=2),
            (
                f"validated 16 exports; retained {len(kept)} globally unique "
                "transactions; exact Decimal USD total recomputed before rounding"
            ),
            0.999,
        )


class LayerCakeHandler(BaseHandler):
    name = "layer_cake"
    title = "Layer Cake"
    category = "Cryptic"
    answer_formats = ("exact",)
    prompt_phrases = ("base64", "zlib", "blob:")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        matches = re.findall(
            r"(?i)\bblob:\s*([A-Za-z0-9+/]+={0,2})",
            context.fingerprint.prompt,
        )
        if len(matches) != 1:
            raise FastPathError("blob marker is missing or ambiguous")
        data = matches[0].encode("ascii")
        for layers in range(21):
            context.check()
            token = _unique_token(data, rf"ONION-{TOKEN_CHARS}{{12}}")
            if token:
                return token, f"strictly decoded {layers} base64/zlib layers", 0.998
            if layers == 20:
                break
            decoded: bytes | None = None
            stripped = data.strip()
            if re.fullmatch(rb"[A-Za-z0-9+/]*={0,2}", stripped) and len(stripped) % 4 == 0:
                try:
                    decoded = base64.b64decode(stripped, validate=True)
                except binascii.Error:
                    decoded = None
            if decoded is None:
                try:
                    decoded = zlib.decompress(data)
                except zlib.error as exc:
                    raise FastPathError("next encoding layer is neither strict base64 nor zlib") from exc
            if decoded == data or len(decoded) > 2_000_000:
                raise FastPathError("invalid or oversized encoding layer")
            data = decoded
        raise FastPathError("encoding layer limit exceeded")


class LockedBoxHandler(BaseHandler):
    name = "locked_box"
    title = "Locked Box"
    category = "Cryptic"
    answer_formats = ("exact",)
    prompt_phrases = ("bundle.zip", "note.txt", "token")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"bundle.zip"})
        fingerprint = _require_file(context.fingerprint, "bundle.zip")
        if not fingerprint.magic_hex.startswith("504b"):
            raise FastPathError("bundle.zip has the wrong magic bytes")
        path = context.path("bundle.zip")
        if path.stat().st_size > MAX_FAST_FILE_BYTES:
            raise FastPathError("bundle.zip exceeds the compressed size limit")
        with ZipFile(io.BytesIO(path.read_bytes())) as archive:
            members = _validate_archive(archive, context)
            names = {item.filename for item in members}
            if "note.txt" not in names or "inner.blob" not in names:
                raise FastPathError("bundle is missing note.txt or inner.blob")
            dat_files = [item for item in members if item.filename.endswith(".dat")]
            if not dat_files:
                raise FastPathError("bundle has no .dat key candidates")
            largest_size = max(item.file_size for item in dat_files)
            largest = [item for item in dat_files if item.file_size == largest_size]
            if len(largest) != 1:
                raise FastPathError("largest .dat file is not unique")
            note = archive.read("note.txt").decode("utf-8")
            if not all(
                phrase in _normalize(note)
                for phrase in ("largest .dat file", "reversed", "xor-encrypted")
            ):
                raise FastPathError("note.txt instructions do not match the known family")
            key = Path(largest[0].filename).stem[::-1].encode("ascii")
            encrypted = archive.read("inner.blob")
        context.check()
        decrypted = _xor_repeating(encrypted, key, context)
        if not decrypted.startswith(b"PK"):
            raise FastPathError("decrypted inner.blob is not a ZIP archive")
        with ZipFile(io.BytesIO(decrypted)) as inner:
            inner_members = _validate_archive(inner, context)
            payloads: list[bytes] = []
            for item in inner_members:
                context.check()
                payloads.append(inner.read(item))
            payload = b"\n".join(payloads)
        token = _unique_token(payload, rf"VAULT-{TOKEN_CHARS}{{12}}")
        if not token:
            raise FastPathError("inner archive does not contain exactly one VAULT token")
        return (
            token,
            f"validated two in-memory ZIPs and CRCs; unique largest key file={largest[0].filename}",
            0.998,
        )


class HiddenPixelsHandler(BaseHandler):
    name = "hidden_pixels"
    title = "Hidden Pixels"
    category = "Cryptic"
    answer_formats = ("exact",)
    prompt_phrases = ("image.png", "metadata", "not in its pixels")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"image.png"})
        fingerprint = _require_file(context.fingerprint, "image.png")
        if not fingerprint.magic_hex.startswith("89504e470d0a1a0a"):
            raise FastPathError("image.png has the wrong PNG signature")
        raw = context.path("image.png").read_bytes()
        if len(raw) > MAX_FAST_FILE_BYTES:
            raise FastPathError("PNG exceeds fast-path size limit")
        metadata, chunk_count = _png_metadata(raw, context)
        token = _unique_token(metadata, rf"PIXEL-{TOKEN_CHARS}{{12}}")
        if not token:
            raise FastPathError("PNG metadata does not contain exactly one PIXEL token")
        return token, f"validated CRCs for {chunk_count} PNG chunks; token appears only in metadata", 0.999


class BinaryLedgerHandler(BaseHandler):
    name = "binary_ledger"
    title = "Binary Ledger"
    category = "Cryptic"
    answer_formats = ("exact",)
    prompt_phrases = ("data.bin", "spec.md", "maximum is unique")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"data.bin", "spec.md"})
        fingerprint = _require_file(context.fingerprint, "data.bin")
        if not fingerprint.magic_hex.startswith("414a4231"):
            raise FastPathError("data.bin does not start with AJB1")
        spec = _normalize(context.text("spec.md", max_bytes=20_000))
        for marker in (
            'magic, the ascii characters "ajb1"',
            "u16 record_count",
            "u16 id",
            "u32 value",
            "u8 name_len",
            "name_len bytes",
        ):
            if marker not in spec:
                raise FastPathError("spec.md does not declare the expected binary schema")
        raw = context.path("data.bin").read_bytes()
        if len(raw) < 6 or raw[:4] != b"AJB1":
            raise FastPathError("binary header is truncated")
        count = struct.unpack_from("<H", raw, 4)[0]
        offset = 6
        records: list[tuple[int, int, str]] = []
        for index in range(count):
            if index % 250 == 0:
                context.check()
            if offset + 7 > len(raw):
                raise FastPathError("record header exceeds binary bounds")
            record_id, value, name_len = struct.unpack_from("<HIB", raw, offset)
            offset += 7
            if offset + name_len > len(raw):
                raise FastPathError("record name exceeds binary bounds")
            try:
                name = raw[offset : offset + name_len].decode("ascii")
            except UnicodeDecodeError as exc:
                raise FastPathError("record name is not ASCII") from exc
            offset += name_len
            records.append((record_id, value, name))
        if offset != len(raw) or len(records) != count or not records:
            raise FastPathError("record count or final binary offset does not match")
        maximum = max(record[1] for record in records)
        winners = [record for record in records if record[1] == maximum]
        if len(winners) != 1 or not re.fullmatch(rf"ITEM-{TOKEN_CHARS}{{8}}", winners[0][2]):
            raise FastPathError("maximum record is tied or has an invalid name")
        return winners[0][2], f"parsed {count} bounded records; unique maximum value={maximum}", 0.999


class CribSheetHandler(BaseHandler):
    name = "crib_sheet"
    title = "Crib Sheet"
    category = "Cryptic"
    answer_formats = ("exact",)
    prompt_phrases = ("first line is exactly agentjeopardy-v1", "xor-encrypted", "repeating key")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"cipher.bin"})
        cipher = context.path("cipher.bin").read_bytes()
        crib = b"AGENTJEOPARDY-V1\n"
        if len(cipher) <= len(crib):
            raise FastPathError("ciphertext is too short")
        solutions: dict[str, tuple[int, bytes]] = {}
        for key_length in range(1, len(crib) + 1):
            context.check()
            key_slots: list[int | None] = [None] * key_length
            valid = True
            for index, plain in enumerate(crib):
                candidate = cipher[index] ^ plain
                slot = index % key_length
                if key_slots[slot] is not None and key_slots[slot] != candidate:
                    valid = False
                    break
                key_slots[slot] = candidate
            if not valid or any(value is None for value in key_slots):
                continue
            key = bytes(int(value) for value in key_slots)
            plain = _xor_repeating(cipher, key, context)
            if not plain.startswith(crib) or not _readable_ascii(plain, context):
                continue
            lines = plain.decode("ascii").splitlines()
            tokens = [
                line
                for line in lines
                if re.fullmatch(rf"GHOST-{TOKEN_CHARS}{{12}}", line)
            ]
            if len(tokens) == 1:
                solutions[tokens[0]] = (key_length, plain)
        if len(solutions) != 1:
            raise FastPathError("crib recovery did not yield one unique token")
        token, (key_length, _) = next(iter(solutions.items()))
        return token, f"recovered repeating XOR key length={key_length}; plaintext is readable ASCII with exact crib", 0.998


class BuriedClauseHandler(BaseHandler):
    name = "buried_clause"
    title = "Buried Clause"
    category = "Ancient Scrolls"
    answer_formats = ("numeric",)
    prompt_phrases = ("maximum reimbursement for a single claim", "section")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"policy_manual.txt"})
        match = re.search(r"under Section (\d+\.\d+)", context.fingerprint.prompt)
        if not match:
            raise FastPathError("target section is missing")
        section = match.group(1)
        block = _numbered_section(
            context.text("policy_manual.txt"), "Section", section
        )
        values = re.findall(
            r"maximum reimbursement for a single claim under this section is "
            r"\$([0-9][0-9,]*(?:\.\d+)?)",
            block,
            re.IGNORECASE,
        )
        value = _unique(values, "maximum reimbursement")
        return _normalize_numeric(value), f"isolated explicit Section {section} boundaries and one reimbursement sentence", 0.997


class TwoHopsHandler(BaseHandler):
    name = "two_hops"
    title = "Two Hops"
    category = "Ancient Scrolls"
    answer_formats = ("numeric",)
    prompt_phrases = ("monthly service fee", "cross-referencing", "appendix")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"contract.txt"})
        match = re.search(r"Clause (\d+\.\d+) defines", context.fingerprint.prompt)
        if not match:
            raise FastPathError("fee clause is missing")
        text = context.text("contract.txt")
        target = match.group(1)
        block = _numbered_section(text, "Clause", target)
        reference = re.search(
            r"base rate listed in Appendix ([A-Z]+) for tier ([A-Za-z]+) "
            r"customers, multiplied by the adjustment multiplier set forth in Clause "
            r"(\d+\.\d+)",
            block,
            re.IGNORECASE,
        )
        if not reference:
            raise FastPathError("Clause cross-references are not unique")
        appendix, tier, multiplier_clause = reference.groups()
        multiplier_block = _numbered_section(text, "Clause", multiplier_clause)
        multipliers = re.findall(
            r"adjustment multiplier .*? is ([0-9]+(?:\.[0-9]+)?)",
            multiplier_block,
            re.IGNORECASE | re.DOTALL,
        )
        multiplier = Decimal(_unique(multipliers, "adjustment multiplier"))
        appendix_block = _appendix_section(text, appendix)
        rates = re.findall(
            rf"(?im)^\s*Tier {re.escape(tier)}:\s*\$([0-9][0-9,]*(?:\.\d+)?) per month\s*$",
            appendix_block,
        )
        rate = Decimal(_unique(rates, "tier base rate").replace(",", ""))
        answer = _format_decimal(rate * multiplier)
        return answer, f"resolved Clause {target} -> Appendix {appendix}/{tier} and Clause {multiplier_clause}; recomputed product", 0.998


class MinuteDetailsHandler(BaseHandler):
    name = "minute_details"
    title = "Minute Details"
    category = "Ancient Scrolls"
    answer_formats = ("exact",)
    prompt_phrases = ("action item was assigned", "ticket code", "meeting heading")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        match = re.search(
            r"assigned to ([A-Za-z]+) during ([A-Za-z]+) (\d{4})",
            context.fingerprint.prompt,
            re.IGNORECASE,
        )
        if not match:
            raise FastPathError("assignee and month are missing")
        assignee, month_name, year_text = match.groups()
        try:
            month = list(calendar.month_name).index(month_name.title())
        except ValueError as exc:
            raise FastPathError("invalid target month") from exc
        year = int(year_text)
        expected_names = {f"notes_{year}-{number:02d}.md" for number in range(1, 13)}
        _require_files(context.fingerprint, expected_names)
        tokens: list[str] = []
        meeting_count = 0
        for name in sorted(expected_names):
            text = context.text(name)
            blocks = _meeting_blocks(text)
            for meeting_date, block in blocks:
                context.check()
                if meeting_date.year != year or meeting_date.month != month:
                    continue
                meeting_count += 1
                action_match = re.search(
                    r"(?ms)^### Action Items\s*$\n(.*?)(?=^### |^## |\Z)",
                    block,
                )
                if not action_match:
                    continue
                for line in action_match.group(1).splitlines():
                    item = re.fullmatch(
                        rf"-\s*{re.escape(assignee)}:\s*.*?\(ticket ([A-Z]{{2}}-\d{{4}})\)\s*",
                        line,
                        re.IGNORECASE,
                    )
                    if item:
                        tokens.append(item.group(1).upper())
        token = _unique(tokens, "assigned ticket")
        return token, f"bounded searches to {meeting_count} dated meetings in {month_name} {year}; one action-item ticket", 0.998


class LivingDocumentHandler(BaseHandler):
    name = "living_document"
    title = "Living Document"
    category = "Ancient Scrolls"
    answer_formats = ("numeric",)
    prompt_phrases = ("amendments section", "effective date", "revoked amendment")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"regulation.txt"})
        match = re.search(
            r"for Clause (\d+\.\d+) on (\d{4}-\d{2}-\d{2})",
            context.fingerprint.prompt,
        )
        if not match:
            raise FastPathError("target clause and date are missing")
        clause, target_text = match.groups()
        target = date.fromisoformat(target_text)
        text = context.text("regulation.txt")
        parts = re.split(r"(?m)^AMENDMENTS\s*$", text)
        if len(parts) != 2:
            raise FastPathError("AMENDMENTS boundary is missing or ambiguous")
        original_block = _numbered_section(parts[0], "Clause", clause)
        originals = re.findall(
            r"Transfers under this clause are subject to a limit of "
            r"\$([0-9][0-9,]*(?:\.\d+)?)",
            original_block,
        )
        original = Decimal(_unique(originals, "original clause limit").replace(",", ""))
        entries = _parse_amendments(parts[1])
        revoked = _resolve_revocations(entries)
        applicable: list[tuple[date, Decimal, int]] = []
        for number, entry in entries.items():
            context.check()
            if number in revoked or entry["clause"] != clause:
                continue
            effective = entry["effective"]
            if effective <= target:
                applicable.append((effective, entry["limit"], number))
        if not applicable:
            selected = original
            selected_note = "original clause"
        else:
            latest = max(value[0] for value in applicable)
            winners = [value for value in applicable if value[0] == latest]
            if len(winners) != 1:
                raise FastPathError("latest effective amendment is tied")
            _, selected, number = winners[0]
            selected_note = f"Amendment {number}"
        return (
            _format_decimal(selected),
            f"parsed {len(entries)} amendments; removed {len(revoked)} revoked entries; selected {selected_note} by effective date",
            0.998,
        )


class TrustTheErrataHandler(BaseHandler):
    name = "trust_the_errata"
    title = "Trust the Errata"
    category = "Ancient Scrolls"
    answer_formats = ("numeric",)
    prompt_phrases = ("latest-dated entry is final", "authoritative report", "(a - b) * c")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        expected = {
            "errata.txt",
            "report_alpha.txt",
            "report_bravo.txt",
            "report_charlie.txt",
            "report_delta.txt",
        }
        _require_files(context.fingerprint, expected)
        match = re.search(
            r"A is .*?Section (\d+), B is .*?Section (\d+), and C is .*?Section (\d+)",
            context.fingerprint.prompt,
            re.IGNORECASE | re.DOTALL,
        )
        if not match:
            raise FastPathError("arithmetic section mapping is missing")
        section_ids = tuple(match.groups())
        errata = context.text("errata.txt", max_bytes=100_000)
        entries: dict[str, list[tuple[date, str]]] = defaultdict(list)
        pattern = re.compile(
            r"^Entry dated (\d{4}-\d{2}-\d{2}) - Section (\d+): "
            r"authoritative report is report_(alpha|bravo|charlie|delta)\s*$",
            re.MULTILINE,
        )
        for date_text, section, report in pattern.findall(errata):
            entries[section].append((date.fromisoformat(date_text), report))
        if len(pattern.findall(errata)) != len(
            re.findall(r"(?m)^Entry dated ", errata)
        ):
            raise FastPathError("an errata entry has an unexpected shape")
        figures: list[int] = []
        chosen: list[str] = []
        for section in section_ids:
            context.check()
            candidates = entries.get(section, [])
            if not candidates:
                raise FastPathError(f"errata has no entry for Section {section}")
            latest_date = max(item[0] for item in candidates)
            reports = {report for when, report in candidates if when == latest_date}
            report = _unique(sorted(reports), f"latest report for Section {section}")
            block = _integer_section(context.text(f"report_{report}.txt"), section)
            values = re.findall(
                r"Recorded figure for this section:\s*([0-9][0-9,]*)", block
            )
            figures.append(int(_unique(values, "recorded figure").replace(",", "")))
            chosen.append(f"{section}:{report}@{latest_date.isoformat()}")
        answer = (figures[0] - figures[1]) * figures[2]
        return str(answer), f"resolved latest errata entries ({', '.join(chosen)}) and recomputed (A-B)*C", 0.999


class SpaghettiHandler(BaseHandler):
    name = "spaghetti"
    title = "Spaghetti"
    category = "Ship It"
    answer_formats = ("exact",)
    prompt_phrases = ("deliberately obfuscated python program", "prints when run with python3", "starts with out-")
    timeout_seconds = 15.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"program.py"})
        output = _repeat_program(context, [sys.executable, "-I", "program.py"])
        if not re.fullmatch(r"OUT-[A-Za-z0-9_-]+", output):
            raise FastPathError("program output is not one format-valid OUT line")
        return output, "sanitized subprocess exited zero twice with one identical OUT line", 0.997


class SpecVsCodeHandler(BaseHandler):
    name = "spec_vs_code"
    title = "Spec vs Code"
    category = "Ship It"
    answer_formats = ("literal",)
    prompt_phrases = (
        "lib.py contains exactly one bug",
        "per the spec",
        "exact python repr",
    )

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"lib.py", "spec.md"})
        spec = context.text("spec.md")
        normalized_spec = _normalize(spec)
        prompt = context.fingerprint.prompt
        bucket_match = re.search(
            r"bucket_counts\(\s*(\[[^\]]*\])\s*,\s*(\[[^\]]*\])\s*\)",
            prompt,
        )
        window_match = re.search(
            r"window_sums\(\s*(\[[^\]]*\])\s*,\s*(\d+)\s*\)",
            prompt,
        )
        if bucket_match:
            if not all(
                marker in normalized_spec
                for marker in (
                    "strictly increasing",
                    "half-open range",
                    "values outside every bucket are ignored",
                )
            ):
                raise FastPathError("bucket specification is not the known family")
            try:
                values = ast.literal_eval(bucket_match.group(1))
                edges = ast.literal_eval(bucket_match.group(2))
            except (SyntaxError, ValueError) as exc:
                raise FastPathError("bucket_counts invocation is malformed") from exc
            if (
                not isinstance(values, list)
                or not isinstance(edges, list)
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in values
                )
                or any(
                    isinstance(edge, bool) or not isinstance(edge, int)
                    for edge in edges
                )
                or len(edges) < 2
                or any(left >= right for left, right in itertools.pairwise(edges))
            ):
                raise FastPathError("bucket invocation violates the specification")
            counts = [0] * (len(edges) - 1)
            for value in values:
                index = bisect.bisect_right(edges, value) - 1
                if 0 <= index < len(counts) and value < edges[index + 1]:
                    counts[index] += 1
            mirror = [
                sum(1 for value in values if low <= value < high)
                for low, high in itertools.pairwise(edges)
            ]
            if counts != mirror:
                raise FastPathError("independent bucket computations disagree")
            return (
                repr(counts),
                (
                    f"parsed the invocation from the prompt; applied {len(counts)} "
                    "half-open ranges with binary search and direct recount"
                ),
                0.999,
            )
        if window_match:
            if not all(
                marker in normalized_spec
                for marker in (
                    "contiguous window of length k",
                    "in order",
                    "n - k + 1 elements",
                )
            ):
                raise FastPathError("window specification is not the known family")
            try:
                values = ast.literal_eval(window_match.group(1))
                width = int(window_match.group(2))
            except (SyntaxError, ValueError) as exc:
                raise FastPathError("window_sums invocation is malformed") from exc
            if (
                not isinstance(values, list)
                or any(
                    isinstance(value, bool) or not isinstance(value, int)
                    for value in values
                )
                or not 1 <= width <= len(values)
            ):
                raise FastPathError("window invocation violates the specification")
            sums = [
                sum(values[index : index + width])
                for index in range(len(values) - width + 1)
            ]
            prefix = [0]
            for value in values:
                prefix.append(prefix[-1] + value)
            mirror = [
                prefix[index + width] - prefix[index]
                for index in range(len(values) - width + 1)
            ]
            if sums != mirror or len(sums) != len(values) - width + 1:
                raise FastPathError("independent window computations disagree")
            return (
                repr(sums),
                (
                    f"parsed the invocation from the prompt; computed all "
                    f"{len(sums)} windows by slicing and prefix-sum recount"
                ),
                0.999,
            )
        raise FastPathError("prompt does not contain a supported spec invocation")


class TinyVMHandler(BaseHandler):
    name = "tiny_vm"
    title = "Tiny VM"
    category = "Ship It"
    answer_formats = ("exact",)
    prompt_phrases = ("vm.py", "program.bin", "tiny bytecode interpreter", "starts with vm-")
    timeout_seconds = 15.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"program.bin", "vm.py"})
        if not _require_file(context.fingerprint, "program.bin").size:
            raise FastPathError("program.bin is empty")
        output = _repeat_program(
            context, [sys.executable, "-I", "vm.py", "program.bin"]
        )
        if not re.fullmatch(r"VM-[A-Za-z0-9_-]+", output):
            raise FastPathError("VM output is not one format-valid line")
        return output, "sanitized VM subprocess exited zero twice with one identical VM line", 0.997


class FixAndRunHandler(BaseHandler):
    name = "fix_and_run"
    title = "Fix and Run"
    category = "Ship It"
    answer_formats = ("exact",)
    prompt_phrases = (
        "project.tar.gz",
        "exactly one function",
        "violates its documented spec",
        "python proj/selftest.py",
        "magic-",
    )
    timeout_seconds = 8.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        expected = {
            "README.md",
            "proj/__init__.py",
            "proj/core.py",
            "proj/selftest.py",
        }
        answer, cases = _solve_repair_archive(context, expected, {"core"})
        return (
            answer,
            (
                f"parsed {cases} literal self-test cases from a bounded tarball; "
                "recomputed every expected digest from the documented contracts"
            ),
            0.999,
        )


class QuintupleFaultHandler(BaseHandler):
    name = "quintuple_fault"
    title = "Quintuple Fault"
    category = "Ship It"
    answer_formats = ("exact",)
    prompt_phrases = (
        "project.tar.gz",
        "exactly five of the seven functions",
        "alpha.py",
        "beta.py",
        "gamma.py",
        "delta.py",
        "python proj/selftest.py",
        "magic-",
    )
    timeout_seconds = 8.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        expected = {
            "README.md",
            "proj/__init__.py",
            "proj/alpha.py",
            "proj/beta.py",
            "proj/gamma.py",
            "proj/delta.py",
            "proj/selftest.py",
        }
        answer, cases = _solve_repair_archive(
            context, expected, {"alpha", "beta", "gamma", "delta"}
        )
        return (
            answer,
            (
                f"parsed {cases} literal self-test cases from a bounded tarball; "
                "recomputed every expected digest from seven documented contracts"
            ),
            0.999,
        )


class CongruentialCascadeHandler(BaseHandler):
    name = "congruential_cascade"
    title = "Congruential Cascade"
    category = "Heavy Compute"
    answer_formats = ("numeric",)
    prompt_phrases = ("pseudo-random sequence", "apply the recurrence", "divisible by")
    timeout_seconds = 20.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        prompt = context.fingerprint.prompt
        recurrence = re.search(
            r"x0\s*=\s*(\d+).*?x_\(n\+1\)\s*=\s*\((\d+)\s*\*\s*x_n\s*\+\s*(\d+)\)\s*mod\s*(\d+)",
            prompt,
            re.DOTALL,
        )
        terms = re.search(
            r"apply the recurrence ([0-9,]+) times", prompt, re.IGNORECASE
        )
        divisor = re.search(r"divisible by (\d+)", prompt, re.IGNORECASE)
        if not recurrence or not terms or not divisor:
            raise FastPathError("could not parse every recurrence parameter")
        x0, multiplier, increment, modulus = map(int, recurrence.groups())
        count = int(terms.group(1).replace(",", ""))
        divisor_value = int(divisor.group(1))
        if count < 1 or modulus < 2 or divisor_value < 1:
            raise FastPathError("recurrence parameters are outside safe bounds")
        result = _lcg_sum(
            x0, multiplier, increment, modulus, count, divisor_value, context
        )
        expected_final = _lcg_final_state(
            x0, multiplier, increment, modulus, count
        )
        if result[2] != expected_final:
            raise FastPathError("linear and logarithmic final-state checks disagree")
        return (
            str(result[0]),
            f"generated {count} terms once; logarithmic final-state check matched",
            0.999,
        )


class CongruentialWorkfactorHandler(BaseHandler):
    name = "congruential_workfactor"
    title = "Congruential Cascade"
    category = "Heavy Compute"
    answer_formats = ("validator",)
    prompt_phrases = (
        "sha-256 digest",
        "zero bits",
        "impossible",
        "workfactor",
        "expected work",
        "far beyond this event",
        "wrong verdict",
    )
    timeout_seconds = 1.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        prompt = context.fingerprint.prompt
        target = re.search(
            r"sha-256 digest begins with (\d+) zero bits", prompt, re.IGNORECASE
        )
        prefix = re.search(
            r"ascii string that begins with '([^']+)'", prompt, re.IGNORECASE
        )
        rubric = re.findall(
            r"^\s*([A-Z]+)\s+(.+)$", prompt, re.MULTILINE
        )
        reasons = {code: text for code, text in rubric}
        if (
            not target
            or not prefix
            or set(reasons) != {"PARITY", "MAGNITUDE", "ONETIMEPAD", "WORKFACTOR"}
            or "solution exists" not in reasons["WORKFACTOR"].lower()
            or "expected work" not in reasons["WORKFACTOR"].lower()
        ):
            raise FastPathError("impossibility rubric is not the observed validator")
        zero_bits = int(target.group(1))
        fixed_prefix = prefix.group(1)
        if (
            zero_bits < 60
            or zero_bits > 256
            or not fixed_prefix
            or any(ord(char) < 33 or ord(char) > 126 for char in fixed_prefix)
        ):
            raise FastPathError("proof-of-work parameters are outside safe verdict bounds")
        expected_trials = 1 << zero_bits
        if expected_trials < 1 << 60:
            raise FastPathError("proof-of-work is not demonstrably event-infeasible")
        return (
            "IMPOSSIBLE WORKFACTOR",
            (
                f"validator explicitly maps feasible-but-event-intractable search "
                f"to WORKFACTOR; a {zero_bits}-bit SHA-256 prefix requires "
                f"2^{zero_bits} expected trials"
            ),
            0.999,
        )


class SquarefreeCensusHandler(BaseHandler):
    name = "squarefree_census"
    title = "Squarefree Census"
    category = "Heavy Compute"
    answer_formats = ("numeric",)
    prompt_phrases = ("squarefree", "sum of its decimal digits", "consecutive integers")
    timeout_seconds = 25.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        prompt = context.fingerprint.prompt
        count_match = re.search(r"([0-9,]+) consecutive integers", prompt)
        range_match = re.search(r"(\d+)\s*<=\s*n\s*<=\s*(\d+)", prompt)
        divisor_match = re.search(
            r"sum of its decimal digits is divisible by (\d+)", prompt
        )
        if not count_match or not range_match or not divisor_match:
            raise FastPathError("could not parse every squarefree range parameter")
        expected = int(count_match.group(1).replace(",", ""))
        low, high = map(int, range_match.groups())
        divisor = int(divisor_match.group(1))
        if high - low + 1 != expected or low < 1 or divisor < 1:
            raise FastPathError("squarefree range invariants failed")
        result = _squarefree_count(low, high, divisor, context)
        return (
            str(result),
            f"segmented squarefree sieve over {expected} integers with trial-division spot checks",
            0.999,
        )


class ProofOfWorkHandler(BaseHandler):
    name = "proof_of_work"
    title = "Proof of Work"
    category = "Heavy Compute"
    answer_formats = ("validator",)
    prompt_phrases = ("sha-256 hex digest", "printable non-whitespace ascii", "prefix")
    timeout_seconds = 30.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        prompt = context.fingerprint.prompt
        prefix_match = re.search(r"starts with the prefix '([^']+)'", prompt)
        zeros_match = re.search(r"starts with '([0]+)' \((\d+) zeros\)", prompt)
        length_match = re.search(r"at most (\d+) characters long", prompt)
        range_match = re.search(r"codepoints (\d+)-(\d+)", prompt)
        if not prefix_match or not zeros_match or not length_match or not range_match:
            raise FastPathError("could not parse every proof-of-work parameter")
        prefix = prefix_match.group(1)
        zero_text, zero_count = zeros_match.groups()
        max_length = int(length_match.group(1))
        lower, upper = map(int, range_match.groups())
        if len(zero_text) != int(zero_count) or lower != 33 or upper != 126:
            raise FastPathError("proof-of-work constraints are inconsistent")
        if any(ord(char) < lower or ord(char) > upper for char in prefix):
            raise FastPathError("proof-of-work prefix violates ASCII constraints")
        target = zero_text
        for nonce in range(100_000_000):
            if nonce % 4_096 == 0:
                context.check()
            candidate = prefix + _base36(nonce)
            if len(candidate) > max_length:
                raise FastPathError("proof-of-work search exceeded maximum length")
            digest = hashlib.sha256(candidate.encode("ascii")).hexdigest()
            if digest.startswith(target):
                verify = hashlib.sha256(candidate.encode("ascii")).hexdigest()
                if not verify.startswith(target):
                    raise FastPathError("proof-of-work verification disagreed")
                return candidate, f"searched deterministic nonces; independently verified SHA-256 prefix {target}", 0.999
        raise FastPathError("proof-of-work nonce search bound exhausted")


class NeedleInTwoTo44Handler(BaseHandler):
    name = "needle_in_2_44"
    title = "Needle in 2^44"
    category = "Heavy Compute"
    answer_formats = ("validator",)
    prompt_phrases = (
        "subset of exactly",
        "0-based indices",
        "strictly ascending order",
        "guaranteed to exist",
    )
    timeout_seconds = 55.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"numbers.txt"})
        lines = [
            line.strip()
            for line in context.text("numbers.txt", max_bytes=100_000).splitlines()
            if line.strip()
        ]
        if len(lines) != 50:
            raise FastPathError("numbers.txt does not contain 48 values")
        target_match = re.fullmatch(r"TARGET:\s*(\d+)", lines[0])
        size_match = re.fullmatch(r"K:\s*(\d+)", lines[1])
        if not target_match or not size_match:
            raise FastPathError("subset target header is malformed")
        target = int(target_match.group(1))
        subset_size = int(size_match.group(1))
        values = [int(value) for value in lines[2:]]
        if (
            target <= 0
            or not 1 <= subset_size <= len(values)
            or any(value <= 0 for value in values)
            or len(set(values)) != len(values)
        ):
            raise FastPathError("subset instance invariants failed")
        prompt_numbers = re.search(
            r"subset of EXACTLY (\d+).*?sum is exactly (\d+)",
            context.fingerprint.prompt,
            re.IGNORECASE | re.DOTALL,
        )
        if prompt_numbers and (
            int(prompt_numbers.group(1)) != subset_size
            or int(prompt_numbers.group(2)) != target
        ):
            raise FastPathError("prompt and subset file disagree")
        indices = _fixed_cardinality_subset_sum(
            values, target, subset_size, context
        )
        if (
            len(indices) != subset_size
            or indices != sorted(set(indices))
            or sum(values[index] for index in indices) != target
        ):
            raise FastPathError("subset solution verification failed")
        return (
            ",".join(str(index) for index in indices),
            (
                f"cardinality-aware meet-in-the-middle found {subset_size} "
                "strictly increasing indices; exact sum independently recomputed"
            ),
            0.999,
        )


class TravelingSalesmanHandler(BaseHandler):
    name = "traveling_salesman"
    title = "Traveling Salesman"
    category = "Heavy Compute"
    answer_formats = ("validator",)
    prompt_phrases = (
        "visits every point exactly once",
        "total euclidean length",
        "permutation",
        "do not repeat",
    )
    timeout_seconds = 50.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"points.txt"})
        threshold_match = re.search(
            r"must be at most\s+(\d+)", context.fingerprint.prompt, re.IGNORECASE
        )
        if not threshold_match:
            raise FastPathError("tour threshold is missing")
        threshold = float(threshold_match.group(1))
        points: dict[int, tuple[float, float]] = {}
        for line_number, line in enumerate(context.text("points.txt").splitlines(), 1):
            parts = line.split()
            if len(parts) != 3:
                raise FastPathError(f"malformed point line {line_number}")
            index = int(parts[0])
            if index in points:
                raise FastPathError("duplicate point index")
            points[index] = (float(parts[1]), float(parts[2]))
        if sorted(points) != list(range(90)):
            raise FastPathError("points.txt must contain indices 0..89")
        order = sorted(points)
        distances = [
            [
                math.hypot(
                    points[left][0] - points[right][0],
                    points[left][1] - points[right][1],
                )
                for right in order
            ]
            for left in order
        ]
        tour, length = _tsp_search(distances, threshold, context)
        if sorted(tour) != order or len(tour) != len(order):
            raise FastPathError("tour is not a permutation")
        verified = _tour_length(tour, distances)
        if not math.isclose(length, verified, rel_tol=1e-12, abs_tol=1e-6):
            raise FastPathError("tour length verification disagreed")
        if verified > threshold + 1e-7:
            raise FastPathError("best deterministic tour exceeds threshold")
        return (
            ",".join(str(index) for index in tour),
            (
                f"multi-start nearest-neighbor and iterated 2-opt produced a "
                f"90-point closed tour of length {verified:.6f} <= {threshold:.6f}"
            ),
            0.998,
        )


class GatekeeperHandler(BaseHandler):
    name = "gatekeeper"
    title = "The Gatekeeper"
    category = "The Dark Web"
    answer_formats = ("exact",)
    prompt_phrases = (
        "get past the gatekeeper",
        "hold a session",
        "answer its challenge",
        "starts with gate-",
    )
    timeout_seconds = 18.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        starts = _web_trail_starts(
            context.fingerprint.prompt, context.runtime.base_url
        )
        if len(starts) != 1:
            raise FastPathError("gatekeeper start URL is ambiguous")
        session = "fast-gatekeeper"
        instructions = _fast_http_page(context, starts[0], session=session)
        text = BeautifulSoup(
            instructions["body"], "html.parser"
        ).get_text(" ", strip=True)
        challenge_url = _one_same_host_url(
            text, context.runtime.allowed_host, suffix="/challenge"
        )
        answer_url = _one_same_host_url(
            text, context.runtime.allowed_host, suffix="/answer"
        )
        rule = re.search(
            r"field [\"']([^\"']+)[\"'] equal to "
            r"\(N \* N \+ (\d+)\) mod (\d+)",
            text,
            re.IGNORECASE,
        )
        if not rule:
            raise FastPathError("gatekeeper arithmetic rule is missing")
        field, increment_text, modulus_text = rule.groups()
        challenge = _fast_http_page(context, challenge_url, session=session)
        numbers = re.findall(
            r"challenge number:\s*(\d+)",
            BeautifulSoup(challenge["body"], "html.parser").get_text(
                " ", strip=True
            ),
            re.IGNORECASE,
        )
        if len(numbers) != 1:
            raise FastPathError("gatekeeper challenge number is ambiguous")
        number = int(numbers[0])
        increment = int(increment_text)
        modulus = int(modulus_text)
        if number < 0 or increment < 0 or not 2 <= modulus <= 2_147_483_647:
            raise FastPathError("gatekeeper arithmetic parameters are invalid")
        response = (number * number + increment) % modulus
        final = _fast_http_page(
            context,
            answer_url,
            session=session,
            method="POST",
            form={field: str(response)},
        )
        candidates = _explicit_web_tokens(
            final["body"], context.fingerprint.prompt
        )
        if len(candidates) != 1:
            raise FastPathError(
                f"gatekeeper response contained {len(candidates)} tokens"
            )
        return (
            next(iter(candidates)),
            (
                "preserved one session across challenge GET and answer POST; "
                "parsed and applied the server-stated modular formula"
            ),
            0.999,
        )


class InitiationHandler(BaseHandler):
    name = "initiation"
    title = "Initiation"
    category = "The Dark Web"
    answer_formats = ("exact",)
    prompt_phrases = (
        "complete the initiation",
        "multi-step",
        "stateful ritual",
        "follow all of them exactly",
        "starts with init-",
    )
    timeout_seconds = 40.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        starts = _web_trail_starts(
            context.fingerprint.prompt, context.runtime.base_url
        )
        if len(starts) != 1:
            raise FastPathError("initiation start URL is ambiguous")
        session = "fast-initiation"
        codename = "codexagent"
        first = _fast_http_page(context, starts[0], session=session)
        first_text = BeautifulSoup(first["body"], "html.parser").get_text(
            " ", strip=True
        )
        registration = re.search(
            r"post a form to (https?://\S+) with field [\"']([^\"']+)[\"']",
            first_text,
            re.IGNORECASE,
        )
        if not registration:
            raise FastPathError("initiation registration instruction is missing")
        register_url = registration.group(1).rstrip(".,;")
        register_field = registration.group(2)
        _require_same_host(register_url, context.runtime.allowed_host)
        second = _fast_http_page(
            context,
            register_url,
            session=session,
            method="POST",
            form={register_field: codename},
        )
        second_text = BeautifulSoup(second["body"], "html.parser").get_text(
            " ", strip=True
        )
        books = re.findall(
            r"https?://[^\s,]+/guestbook/([A-Za-z0-9_-]+)",
            second_text,
        )
        order_match = re.search(
            r"assemble the code in this order\s*:\s*([A-Za-z0-9_, ]+?)\.",
            second_text,
            re.IGNORECASE,
        )
        verify_match = re.search(
            r"post it to (https?://\S+) as field [\"']([^\"']+)[\"']",
            second_text,
            re.IGNORECASE,
        )
        if (
            len(books) != 3
            or len(set(books)) != 3
            or not order_match
            or not verify_match
        ):
            raise FastPathError("initiation guest-book instruction is malformed")
        book_urls = {
            name: _one_same_host_url(
                second_text,
                context.runtime.allowed_host,
                suffix=f"/guestbook/{name}",
            )
            for name in books
        }
        order = [
            value.strip()
            for value in order_match.group(1).split(",")
            if value.strip()
        ]
        if len(order) != 3 or set(order) != set(book_urls):
            raise FastPathError("initiation fragment order is inconsistent")
        fragments: dict[str, str] = {}
        for name, url in book_urls.items():
            page = _fast_http_page(context, url, session=session)
            soup = BeautifulSoup(page["body"], "html.parser")
            matches: list[str] = []
            for item in soup.find_all("li"):
                line = item.get_text(" ", strip=True)
                if re.search(
                    rf"\bsigned,\s*{re.escape(codename)}\b",
                    line,
                    re.IGNORECASE,
                ):
                    matches.extend(re.findall(r"\b\d{4}\b", line))
            if len(matches) != 1:
                raise FastPathError(
                    f"guest book {name!r} has {len(matches)} matching fragments"
                )
            fragments[name] = matches[0]
        code = "".join(fragments[name] for name in order)
        if not re.fullmatch(r"\d{12}", code):
            raise FastPathError("assembled verification code is malformed")
        verify_url = verify_match.group(1).rstrip(".,;")
        verify_field = verify_match.group(2)
        _require_same_host(verify_url, context.runtime.allowed_host)
        third = _fast_http_page(
            context,
            verify_url,
            session=session,
            method="POST",
            form={verify_field: code},
        )
        third_text = BeautifulSoup(third["body"], "html.parser").get_text(
            " ", strip=True
        )
        rooms_match = re.search(
            r"exact order\s*:\s*([A-Za-z0-9_, ]+?)\.",
            third_text,
            re.IGNORECASE,
        )
        template_match = re.search(
            r"(https?://\S+/room/)<name>",
            third_text,
            re.IGNORECASE,
        )
        header_match = re.search(
            r"header\s+([A-Za-z0-9-]+):\s*<your codename>",
            third_text,
            re.IGNORECASE,
        )
        if not rooms_match or not template_match or not header_match:
            raise FastPathError("initiation room-walk instruction is malformed")
        rooms = [
            value.strip()
            for value in rooms_match.group(1).split(",")
            if value.strip()
        ]
        if (
            len(rooms) != 4
            or len(set(rooms)) != 4
            or any(not re.fullmatch(r"[a-z]+", room) for room in rooms)
        ):
            raise FastPathError("initiation room order is unsafe")
        room_base = template_match.group(1)
        _require_same_host(room_base, context.runtime.allowed_host)
        final = third
        for room in rooms:
            final = _fast_http_page(
                context,
                urljoin(room_base, room),
                session=session,
                headers={header_match.group(1): codename},
            )
        candidates = _explicit_web_tokens(
            final["body"], context.fingerprint.prompt
        )
        if len(candidates) != 1:
            raise FastPathError(
                f"initiation response contained {len(candidates)} tokens"
            )
        return (
            next(iter(candidates)),
            (
                "followed the server-stated registration, three-fragment order, "
                "verification POST, and four-room header-authenticated walk"
            ),
            0.999,
        )


class MembersOnlyHandler(BaseHandler):
    name = "members_only"
    title = "Members Only"
    category = "The Dark Web"
    answer_formats = ("exact", "exact_ci")
    prompt_phrases = (
        "members-only vault",
        "username:",
        "password:",
        "session cookie",
    )
    timeout_seconds = 24.0

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        starts = _web_trail_starts(
            context.fingerprint.prompt, context.runtime.base_url
        )
        credentials = re.search(
            r"username:\s*(\S+)\s+password:\s*(\S+)",
            context.fingerprint.prompt,
            re.IGNORECASE,
        )
        if len(starts) != 1 or not credentials:
            raise FastPathError("login URL or credentials are ambiguous")
        username, password = (
            value.rstrip(".,;") for value in credentials.groups()
        )
        session = "fast-members-only"
        first = _fast_http_page(context, starts[0], session=session)
        soup = BeautifulSoup(first["body"], "html.parser")
        forms = [
            form
            for form in soup.find_all("form")
            if form.find(
                "input", attrs={"type": re.compile("^password$", re.IGNORECASE)}
            )
        ]
        if len(forms) != 1:
            raise FastPathError("expected exactly one password form")
        form = forms[0]
        payload: dict[str, str] = {}
        username_field = ""
        password_field = ""
        for input_tag in form.find_all("input"):
            name = str(input_tag.get("name") or "")
            if not name:
                continue
            input_type = str(input_tag.get("type") or "text").lower()
            value = str(input_tag.get("value") or "")
            if input_type == "hidden":
                payload[name] = value
            elif input_type == "password":
                if password_field:
                    raise FastPathError("multiple password fields are ambiguous")
                password_field = name
            elif input_type in {"text", "email"} and not username_field:
                username_field = name
        if not username_field or not password_field:
            raise FastPathError("login fields could not be identified")
        payload[username_field] = username
        payload[password_field] = password
        action = urljoin(
            first["url"], str(form.get("action") or first["url"])
        )
        if urlparse(action).hostname != context.runtime.allowed_host:
            raise FastPathError("login form leaves the event host")
        method = str(form.get("method") or "post").upper()
        if method not in {"POST", "GET"}:
            raise FastPathError("unsupported login form method")
        logged_in = _fast_http_page(
            context,
            action,
            session=session,
            method=method,
            form=payload if method == "POST" else None,
            params=payload if method == "GET" else None,
        )
        queue = deque([logged_in])
        seen = {_normalized_web_url(first["url"])}
        candidates: set[str] = set()
        pages = 0
        while queue and pages < 12:
            context.check()
            page = queue.popleft()
            normalized = _normalized_web_url(page["url"])
            if normalized in seen and page is not logged_in:
                continue
            seen.add(normalized)
            pages += 1
            candidates.update(
                _explicit_web_tokens(
                    page["body"], context.fingerprint.prompt
                )
            )
            for link in _same_host_links(
                page["body"], page["url"], context.runtime.allowed_host
            ):
                if _normalized_web_url(link) not in seen:
                    queue.append(
                        _fast_http_page(context, link, session=session)
                    )
        if queue:
            raise FastPathError("members-only crawl exceeded page limit")
        if len(candidates) != 1:
            raise FastPathError(
                f"expected one vault token, found {len(candidates)}"
            )
        return (
            next(iter(candidates)),
            (
                f"submitted one same-host login form with preserved cookies; "
                f"searched {pages} authenticated pages and found one vault token"
            ),
            0.997,
        )


class BoundedWebTrailHandler(BaseHandler):
    """Replay a read-only, same-session link trail and extract one explicit token."""

    category = "The Dark Web"
    answer_formats = ("exact", "exact_ci")
    timeout_seconds = 24.0
    max_pages = 48

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        prompt = context.fingerprint.prompt
        starts = _web_trail_starts(prompt, context.runtime.base_url)
        if not starts:
            raise FastPathError("could not identify an event-host start URL")

        queue = deque(starts)
        seen: set[str] = set()
        candidates: set[str] = set()
        successful_pages = 0
        while queue and len(seen) < self.max_pages:
            context.check()
            target = queue.popleft()
            normalized = _normalized_web_url(target)
            if normalized in seen:
                continue
            seen.add(normalized)
            page = _fast_http_page(
                context, target, session=f"fast-{self.name}"
            )
            successful_pages += 1
            candidates.update(_explicit_web_tokens(page["body"], prompt))

            links = _same_host_links(
                page["body"], page["url"], context.runtime.allowed_host
            )
            links.extend(
                _same_host_header_links(
                    page["headers"], page["url"], context.runtime.allowed_host
                )
            )
            for link in links:
                if _normalized_web_url(link) not in seen:
                    queue.append(link)

        if queue:
            raise FastPathError("web trail exceeded the bounded page limit")
        if successful_pages == 0:
            raise FastPathError("no web-trail page returned successfully")
        if len(candidates) != 1:
            raise FastPathError(
                f"expected one explicit web token, found {len(candidates)}"
            )
        answer = next(iter(candidates))
        return (
            answer,
            (
                f"replayed one cookie-preserving trail across {successful_pages} "
                "pages; exactly one explicitly labeled token remained"
            ),
            0.995,
        )


class DeepPagesHandler(BoundedWebTrailHandler):
    name = "deep_pages"
    title = "Deep Pages"

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, set())
        starts = _web_trail_starts(
            context.fingerprint.prompt, context.runtime.base_url
        )
        if len(starts) != 1:
            raise FastPathError("deep-pages start URL is ambiguous")
        target = starts[0]
        seen: set[str] = set()
        for page_number in range(1, self.max_pages + 1):
            context.check()
            normalized = _normalized_web_url(target)
            if normalized in seen:
                raise FastPathError("deep-pages trail contains a cycle")
            seen.add(normalized)
            page = _fast_http_page(
                context, target, session="fast-deep_pages"
            )
            soup = BeautifulSoup(page["body"], "html.parser")
            next_links = []
            for anchor in soup.find_all("a"):
                label = _normalize(anchor.get_text(" ", strip=True))
                if label != "next":
                    continue
                absolute = urljoin(
                    page["url"], str(anchor.get("href") or "")
                )
                if urlparse(absolute).hostname == context.runtime.allowed_host:
                    next_links.append(absolute)
            if len(next_links) > 1:
                raise FastPathError("deep-pages page has multiple next links")
            if next_links:
                target = next_links[0]
                continue
            candidates = _explicit_web_tokens(
                page["body"], context.fingerprint.prompt
            )
            if len(candidates) != 1:
                raise FastPathError(
                    f"terminal deep page has {len(candidates)} tokens"
                )
            return (
                next(iter(candidates)),
                (
                    f"followed exactly one labeled next link per page across "
                    f"{page_number} pages; terminal page had one token"
                ),
                0.997,
            )
        raise FastPathError("deep-pages trail exceeded the bounded page limit")


class BreadcrumbsHandler(BoundedWebTrailHandler):
    name = "breadcrumbs"
    title = "Breadcrumbs"


HANDLERS: tuple[FastPathHandler, ...] = (
    DeepPagesHandler(),
    GatekeeperHandler(),
    InitiationHandler(),
    MembersOnlyHandler(),
    BreadcrumbsHandler(),
    DirtyLedgerHandler(),
    EventHorizonHandler(),
    JoinTheDotsHandler(),
    SessionHunterHandler(),
    ThirtySystemsHandler(),
    LayerCakeHandler(),
    LockedBoxHandler(),
    HiddenPixelsHandler(),
    BinaryLedgerHandler(),
    CribSheetHandler(),
    BuriedClauseHandler(),
    TwoHopsHandler(),
    MinuteDetailsHandler(),
    LivingDocumentHandler(),
    TrustTheErrataHandler(),
    SpaghettiHandler(),
    SpecVsCodeHandler(),
    TinyVMHandler(),
    FixAndRunHandler(),
    QuintupleFaultHandler(),
    CongruentialCascadeHandler(),
    CongruentialWorkfactorHandler(),
    SquarefreeCensusHandler(),
    ProofOfWorkHandler(),
    NeedleInTwoTo44Handler(),
    TravelingSalesmanHandler(),
)

REGISTERED_TITLES = frozenset(
    " ".join(handler.title.lower().split()) for handler in HANDLERS
)

CPU_HEAVY_FAST_PATH_TITLES = frozenset(
    {
        "congruential cascade",
        "squarefree census",
        "proof of work",
        "needle in 2^44",
        "traveling salesman",
    }
)

FAST_PATH_EXPECTED_SECONDS = {
    "deep pages": 4.0,
    "members only": 5.0,
    "breadcrumbs": 6.0,
    "the gatekeeper": 4.0,
    "initiation": 12.0,
    "trust the errata": 0.1,
    "crib sheet": 0.1,
    "spec vs code": 1.0,
    "fix and run": 2.0,
    "quintuple fault": 2.0,
    "thirty systems": 4.0,
    "congruential cascade": 5.0,
    "squarefree census": 8.0,
    "proof of work": 15.0,
    "needle in 2^44": 40.0,
    # The real 90-city qualifier fixture completes in about 0.04s. Keep a
    # conservative floor for download/fingerprint overhead without making the
    # scheduler treat this as a model-scale search.
    "traveling salesman": 0.1,
}


class FastPathDispatcher:
    def __init__(self, handlers: tuple[FastPathHandler, ...] = HANDLERS) -> None:
        self.handlers = handlers

    def attempt(
        self,
        tile: Tile,
        detail: dict[str, Any],
        names: list[str],
        runtime: TaskToolRuntime,
        *,
        disabled_handlers: frozenset[str] = frozenset(),
        fingerprint: TaskFingerprint | None = None,
    ) -> FastPathOutcome:
        started = time.monotonic()
        if fingerprint is None:
            fingerprint = TaskFingerprint.build(
                tile, detail, names, runtime.root, runtime.cancellation
            )
        for handler in self.handlers:
            runtime.cancellation.check()
            if not handler.recognizes(fingerprint):
                continue
            if handler.name in disabled_handlers:
                return FastPathOutcome(
                    status=FastPathStatus.INCONCLUSIVE,
                    handler=handler.name,
                    elapsed_seconds=time.monotonic() - started,
                    reason="handler disabled after an incorrect canary",
                    fingerprint=fingerprint,
                )
            context = FastPathContext(
                tile=tile,
                detail=detail,
                fingerprint=fingerprint,
                root=runtime.root,
                runtime=runtime,
                cancellation=runtime.cancellation,
                deadline=time.monotonic() + handler.timeout_seconds,
            )
            try:
                answer, evidence, confidence = handler.solve(context)
                context.check()
                if not answer.strip():
                    raise FastPathError("handler produced an empty answer")
                source = context.artifact(answer.strip())
                verification_source = context.artifact(
                    f"handler={handler.name}\nverification={evidence}"
                )
                return FastPathOutcome(
                    status=FastPathStatus.SOLVED,
                    handler=handler.name,
                    answer=answer.strip(),
                    source=source,
                    verification_source=verification_source,
                    evidence=evidence,
                    confidence=confidence,
                    elapsed_seconds=time.monotonic() - started,
                    fingerprint=fingerprint,
                )
            except SolveCancelled:
                raise
            except (
                FastPathError,
                OSError,
                ValueError,
                UnicodeError,
                csv.Error,
                json.JSONDecodeError,
                BadZipFile,
                tarfile.TarError,
                binascii.Error,
                struct.error,
                zlib.error,
            ) as exc:
                return FastPathOutcome(
                    status=FastPathStatus.INCONCLUSIVE,
                    handler=handler.name,
                    elapsed_seconds=time.monotonic() - started,
                    reason=_safe_failure_reason(exc),
                    fingerprint=fingerprint,
                )
        return FastPathOutcome.not_applicable(fingerprint)


def is_registered_fast_path_title(title: str) -> bool:
    return _normalize(title) in REGISTERED_TITLES


def is_cpu_heavy_fast_path_title(title: str) -> bool:
    return _normalize(title) in CPU_HEAVY_FAST_PATH_TITLES


def fast_path_expected_seconds(title: str) -> float | None:
    return FAST_PATH_EXPECTED_SECONDS.get(_normalize(title))


def _web_trail_starts(prompt: str, base_url: str) -> list[str]:
    candidates = re.findall(r"https?://[^\s)\]}>'\"]+", prompt)
    relative_patterns = (
        r"(?:start(?:ing)?(?:\s+page)?|begin|visit|url|endpoint|path)"
        r"\s*(?:is|at|:|=)?\s*[`'\"]?(/[A-Za-z0-9_./?=&%:+~-]+)"
    )
    candidates.extend(re.findall(relative_patterns, prompt, flags=re.IGNORECASE))
    if not candidates:
        candidates.extend(
            re.findall(r"[`'\"](/[A-Za-z0-9_./?=&%:+~-]{3,})[`'\"]", prompt)
        )

    allowed_host = urlparse(base_url).hostname
    starts: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        absolute = urljoin(base_url, candidate.rstrip(".,;"))
        if urlparse(absolute).hostname != allowed_host:
            continue
        normalized = _normalized_web_url(absolute)
        if normalized not in seen:
            seen.add(normalized)
            starts.append(absolute)
    return starts[:8]


def _fast_http_page(
    context: FastPathContext,
    url: str,
    *,
    session: str,
    method: str = "GET",
    form: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    context.check()
    raw = context.runtime.execute(
        "http_request",
        {
            "url": url,
            "session": session,
            "method": method,
            "form": form,
            "params": params,
            "headers": headers,
            "follow_redirects": True,
            "timeout": 5,
        },
    )
    result = json.loads(raw)
    status = int(result.get("status") or 0)
    artifact_id = str(result.get("body_artifact") or "")
    if not result.get("ok") or not 200 <= status < 400 or not artifact_id:
        raise FastPathError("same-host HTTP request failed")
    return {
        "url": str(result.get("url") or url),
        "headers": dict(result.get("headers") or {}),
        "body": context.runtime.artifacts.get(artifact_id),
    }


def _require_same_host(url: str, allowed_host: str | None) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname != allowed_host:
        raise FastPathError("instruction URL leaves the event host")


def _one_same_host_url(
    text: str, allowed_host: str | None, *, suffix: str
) -> str:
    matches = {
        value.rstrip(".,;")
        for value in re.findall(r"https?://[^\s,]+", text)
        if value.rstrip(".,;").endswith(suffix)
    }
    if len(matches) != 1:
        raise FastPathError(f"expected one same-host URL ending {suffix!r}")
    result = next(iter(matches))
    _require_same_host(result, allowed_host)
    return result


def _same_host_links(body: str, current_url: str, allowed_host: str | None) -> list[str]:
    soup = BeautifulSoup(body, "html.parser")
    raw_links = [str(tag.get("href") or "") for tag in soup.find_all("a")]
    raw_links.extend(re.findall(r"https?://[^\s)\]}>'\"]+", body))
    for meta in soup.find_all("meta"):
        if str(meta.get("http-equiv") or "").lower() != "refresh":
            continue
        content = str(meta.get("content") or "")
        match = re.search(
            r"\burl\s*=\s*['\"]?([^'\";\s]+)", content, re.IGNORECASE
        )
        if match:
            raw_links.append(match.group(1))
    raw_links.extend(
        re.findall(
            (
                r"(?:window\.)?location(?:\.href)?\s*=\s*"
                r"['\"]([^'\"]+)['\"]"
            ),
            body,
            flags=re.IGNORECASE,
        )
    )
    raw_links.extend(
        re.findall(
            (
                r"(?:next|next_url|url|path|href|location)\s*"
                r"(?:is|:|=)\s*['\"]?(/[A-Za-z0-9_./?=&%:+~-]+)"
            ),
            body,
            flags=re.IGNORECASE,
        )
    )
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        raw_links.extend(
            re.findall(
                r"https?://[^\s)\]}>'\"]+|/[A-Za-z0-9_./?=&%:+~-]{3,}",
                str(comment),
            )
        )
    try:
        parsed_json = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        parsed_json = None
    raw_links.extend(_json_web_pointers(parsed_json))
    for candidate in re.findall(
        r"(?<![A-Za-z0-9+/])([A-Za-z0-9+/]{8,256}={0,2})(?![A-Za-z0-9+/])",
        body,
    ):
        try:
            decoded = base64.b64decode(candidate, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            continue
        if decoded.startswith(("/", "http://", "https://")):
            raw_links.append(decoded.strip())
    links: list[str] = []
    seen: set[str] = set()
    for raw in raw_links:
        if not raw or raw.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urljoin(current_url, raw)
        if urlparse(absolute).hostname != allowed_host:
            continue
        normalized = _normalized_web_url(absolute)
        if normalized not in seen:
            seen.add(normalized)
            links.append(absolute)
    return links


def _json_web_pointers(value: Any) -> list[str]:
    if isinstance(value, dict):
        output: list[str] = []
        for key, item in value.items():
            normalized = _normalize(str(key)).replace(" ", "_")
            if normalized in {
                "next",
                "next_url",
                "url",
                "path",
                "href",
                "location",
                "redirect",
            } and isinstance(item, str):
                output.append(item)
            elif isinstance(item, (dict, list)):
                output.extend(_json_web_pointers(item))
        return output
    if isinstance(value, list):
        return [
            pointer
            for item in value
            for pointer in _json_web_pointers(item)
        ]
    return []


def _same_host_header_links(
    headers: dict[str, Any],
    current_url: str,
    allowed_host: str | None,
) -> list[str]:
    raw: list[str] = []
    for key, value in headers.items():
        normalized = key.lower()
        text = str(value)
        if normalized in {"location", "x-next", "x-next-url", "refresh"}:
            raw.append(text.rsplit("url=", 1)[-1].strip(" '\""))
        elif normalized == "link":
            raw.extend(re.findall(r"<([^>]+)>", text))
    output: list[str] = []
    for value in raw:
        absolute = urljoin(current_url, value)
        if urlparse(absolute).hostname == allowed_host:
            output.append(absolute)
    return output


def _normalized_web_url(value: str) -> str:
    parsed = urlparse(value)
    return parsed._replace(fragment="").geturl()


def _explicit_web_tokens(body: str, prompt: str) -> set[str]:
    text = BeautifulSoup(body, "html.parser").get_text(" ", strip=True)
    candidates: set[str] = set()
    labeled_patterns = (
        (
            r"\b(?:answer|token|flag|secret)\b\s*(?:is|:|=)\s*"
            r"[`'\"]?([A-Za-z0-9][A-Za-z0-9_.:{}-]{3,159})"
        ),
        r"""["'](?:answer|token|flag|secret)["']\s*:\s*
        ["']([^"'\\\s]{4,160})["']""",
    )
    for pattern in labeled_patterns:
        for match in re.findall(pattern, body + "\n" + text, flags=re.IGNORECASE | re.VERBOSE):
            value = match.rstrip(".,;:)]'\"")
            if _looks_like_web_token(value):
                candidates.add(value)

    prefix_patterns = (
        (
            r"(?:starts?|begins?)\s+with(?:\s+the)?(?:\s+prefix)?\s*[`'\"]"
            r"?([A-Za-z][A-Za-z0-9_-]{1,31})"
        ),
        r"\bprefix\s*(?:is|:|=)\s*[`'\"]?([A-Za-z][A-Za-z0-9_-]{1,31})",
    )
    prefixes = {
        match
        for pattern in prefix_patterns
        for match in re.findall(pattern, prompt, flags=re.IGNORECASE)
    }
    for prefix in prefixes:
        pattern = rf"(?<![A-Za-z0-9_])({re.escape(prefix)}[A-Za-z0-9_.:{{}}-]{{2,128}})"
        for match in re.findall(pattern, body):
            value = match.rstrip(".,;:)]'\"")
            if _looks_like_web_token(value):
                candidates.add(value)
    return candidates


def _looks_like_web_token(value: str) -> bool:
    if not (4 <= len(value) <= 160) or value.lower().startswith(("http", "www.")):
        return False
    return any(char.isdigit() for char in value) or any(
        marker in value for marker in ("-", "_", "{", "}")
    )


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


def _prompt_template(value: str) -> str:
    """Remove instance values while retaining the generator's instructions."""

    normalized = value.lower()
    substitutions = (
        (r"https?://[^\s)\]}>'\"]+", "<url>"),
        (r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", "<email>"),
        (
            (
                r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                r"(?:z|[+-]\d{2}:\d{2})?)?\b"
            ),
            "<date>",
        ),
        (r"(?<![a-z0-9])[a-f0-9]{24,}(?![a-z0-9])", "<hex>"),
        (r"(?<![a-z0-9])[a-z0-9+/]{32,}={0,2}(?![a-z0-9])", "<blob>"),
        (
            r"""(?:"(?:\\.|[^"\\])*"|(?<![a-z0-9])'(?:\\.|[^'\\])*'(?![a-z0-9]))""",
            "<quoted>",
        ),
        (
            (
                r"(?<![a-z0-9_])[-+]?(?:[$€£]\s*)?"
                r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?(?![a-z0-9_])"
            ),
            "<number>",
        ),
    )
    for pattern, replacement in substitutions:
        normalized = re.sub(pattern, replacement, normalized, flags=re.DOTALL)
    return " ".join(normalized.split())


def _path_template(value: str) -> str:
    normalized = value.replace("\\", "/").lower()
    return re.sub(r"\d+", "<n>", normalized)


def _magic_type(item: FileFingerprint) -> str:
    if item.magic_hex.startswith("504b"):
        return "zip"
    if item.magic_hex.startswith("89504e470d0a1a0a"):
        return "png"
    if item.magic_hex.startswith("414a4231"):
        return "ajb1"
    if item.kind == "binary":
        return "binary"
    return item.kind


def _safe_failure_reason(exc: BaseException) -> str:
    """Classify a fallback without echoing prompt or file contents into logs."""

    if isinstance(exc, FastPathDeadline):
        return "FastPathDeadline: handler deadline exceeded"
    if isinstance(exc, FastPathError):
        return "FastPathError: deterministic invariant failed"
    if isinstance(exc, json.JSONDecodeError):
        return "JSONDecodeError: malformed JSON"
    if isinstance(exc, BadZipFile):
        return "BadZipFile: malformed archive"
    if isinstance(exc, UnicodeError):
        return f"{type(exc).__name__}: invalid text encoding"
    return f"{type(exc).__name__}: parse or validation failure"


def _safe_task_path(root: Path, name: str) -> Path:
    if not name or "\x00" in name:
        raise ValueError("invalid task path")
    path = (root / name).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("task path escapes workspace") from exc
    if not path.is_file():
        raise ValueError(f"task file is missing: {name}")
    return path


def _fingerprint_file(
    path: Path, display_name: str, cancellation: CancellationToken
) -> FileFingerprint:
    size = path.stat().st_size
    with path.open("rb") as stream:
        magic = stream.read(16)
    suffix = path.suffix.lower()
    kind = "binary" if b"\x00" in magic else "text"
    columns: tuple[str, ...] = ()
    json_keys: tuple[str, ...] = ()
    members: tuple[ArchiveMember, ...] = ()
    total_expanded = 0
    if suffix in {".csv", ".tsv"}:
        delimiter = "\t" if suffix == ".tsv" else ","
        with path.open("r", encoding="utf-8", newline="") as stream:
            header_line = stream.readline(65_537)
        if len(header_line) > 65_536:
            raise ValueError("delimited header exceeds inspection limit")
        header = next(csv.reader([header_line], delimiter=delimiter), [])
        columns = tuple(value.strip() for value in header)
        kind = "delimited"
    elif suffix == ".jsonl":
        keys: set[str] = set()
        with path.open("r", encoding="utf-8") as stream:
            sampled = 0
            while sampled < 3:
                cancellation.check()
                line = stream.readline(65_537)
                if not line:
                    break
                if len(line) > 65_536:
                    raise ValueError("JSONL sample line exceeds inspection limit")
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("JSONL sample is not an object")
                keys.update(str(key) for key in value)
                sampled += 1
        json_keys = tuple(sorted(keys))
        kind = "jsonl"
    elif suffix == ".json":
        if size <= 1_000_000:
            value = json.loads(path.read_text("utf-8"))
            if isinstance(value, dict):
                json_keys = tuple(sorted(str(key) for key in value))
        kind = "json"
    elif suffix == ".zip" or magic.startswith(b"PK"):
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise ValueError("archive member count exceeds limit")
            _validate_archive_paths(infos)
            total_expanded = sum(item.file_size for item in infos)
            if total_expanded > MAX_ARCHIVE_EXPANDED:
                raise ValueError("archive expanded size exceeds limit")
            members = tuple(
                ArchiveMember(
                    name=item.filename,
                    compressed_bytes=item.compress_size,
                    expanded_bytes=item.file_size,
                    crc=item.CRC,
                )
                for item in infos
            )
        kind = "zip"
    elif magic.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "png"
    return FileFingerprint(
        path=display_name,
        suffix=suffix,
        size=size,
        magic_hex=magic.hex(),
        kind=kind,
        columns=columns,
        json_keys=json_keys,
        archive_members=members,
        archive_total_expanded=total_expanded,
    )


def _require_files(fingerprint: TaskFingerprint, expected: set[str]) -> None:
    actual = {item.path for item in fingerprint.files}
    if actual != expected:
        raise FastPathError(
            f"file manifest mismatch: expected {sorted(expected)}, got {sorted(actual)}"
        )
    if fingerprint.inspection_errors:
        raise FastPathError("file fingerprint contains inspection errors")


def _require_file(fingerprint: TaskFingerprint, name: str) -> FileFingerprint:
    file = fingerprint.file(name)
    if file is None or file.error:
        raise FastPathError(f"required file fingerprint unavailable: {name}")
    return file


def _require_columns(file: FileFingerprint, expected: set[str]) -> None:
    if set(file.columns) != expected:
        raise FastPathError(
            f"schema mismatch for {file.path}: expected {sorted(expected)}, got {list(file.columns)}"
        )


def _require_json_keys(file: FileFingerprint, expected: set[str]) -> None:
    if not expected <= set(file.json_keys):
        raise FastPathError(
            f"JSON schema mismatch for {file.path}: missing {sorted(expected - set(file.json_keys))}"
        )


def _parse_money(value: str) -> Decimal | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    cleaned = re.sub(r"\s*USD\s*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip().removeprefix("$").replace(",", "").strip()
    if not re.fullmatch(r"[+-]?\d+(?:\.\d{1,2})?", cleaned):
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _dirty_ledger_decimal(
    path: Path, target_status: str, context: FastPathContext
) -> tuple[Decimal, int]:
    total = Decimal(0)
    count = 0
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        if set(reader.fieldnames or ()) != {"txn_id", "status", "amount"}:
            raise FastPathError("CSV header changed during parsing")
        for index, row in enumerate(reader):
            if index % 250 == 0:
                context.check()
            if row.get("status") != target_status:
                continue
            amount = _parse_money(str(row.get("amount") or ""))
            if amount is not None:
                total += amount
                count += 1
    return total, count


def _dirty_ledger_cents(
    path: Path, target_status: str, context: FastPathContext
) -> tuple[int, int]:
    total = 0
    count = 0
    with path.open("r", encoding="utf-8", newline="") as stream:
        for index, row in enumerate(csv.reader(stream)):
            if index == 0:
                if row != ["txn_id", "status", "amount"]:
                    raise FastPathError("CSV header changed during independent pass")
                continue
            if index % 250 == 0:
                context.check()
            if len(row) != 3 or row[1] != target_status:
                continue
            amount = _parse_money(row[2])
            if amount is not None:
                cents = amount * 100
                if cents != cents.to_integral_value():
                    raise FastPathError("money value has fractional cents")
                total += int(cents)
                count += 1
    return total, count


def _regional_fx_rates(raw: bytes) -> dict[str, Decimal]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FastPathError("fx.csv is not UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text))
    if set(reader.fieldnames or ()) != {"region", "currency", "usd_per_unit"}:
        raise FastPathError("fx.csv schema is unexpected")
    rates: dict[str, Decimal] = {}
    for row in reader:
        region = str(row.get("region") or "")
        currency = str(row.get("currency") or "")
        try:
            rate = Decimal(str(row.get("usd_per_unit") or "").strip())
        except InvalidOperation as exc:
            raise FastPathError("fx.csv contains an invalid rate") from exc
        if (
            not re.fullmatch(r"[A-Z]{2}", region)
            or not currency
            or rate <= 0
            or region in rates
        ):
            raise FastPathError("fx.csv row invariants failed")
        rates[region] = rate
    if len(rates) != 4:
        raise FastPathError("fx.csv must contain four regions")
    return rates


def _regional_sales_total(
    archive: ZipFile,
    names: list[str],
    rates: dict[str, Decimal],
    context: FastPathContext,
) -> tuple[Decimal, list[tuple[str, str, Decimal]]]:
    revenue_names = {"amount", "amt", "value", "gross"}
    transaction_names = {"txn_id", "order_id", "ref", "record_id"}
    seen: set[str] = set()
    kept: list[tuple[str, str, Decimal]] = []
    total = Decimal(0)
    for file_index, name in enumerate(sorted(names)):
        context.check()
        region_match = re.fullmatch(r"sales_([A-Z]{2})_\d{3}\.csv", name)
        if not region_match or region_match.group(1) not in rates:
            raise FastPathError("sales filename has an unknown region")
        region = region_match.group(1)
        try:
            lines = archive.read(name).decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise FastPathError("sales export is not UTF-8") from exc
        header_index = next(
            (index for index, line in enumerate(lines) if not line.startswith("#")),
            None,
        )
        if header_index is None:
            raise FastPathError("sales export has no header")
        header = next(csv.reader([lines[header_index]]), [])
        if not header or len(set(header)) != len(header):
            raise FastPathError("sales header is empty or duplicated")
        revenue_columns = [value for value in header if value in revenue_names]
        transaction_columns = [
            value for value in header if value in transaction_names
        ]
        if len(revenue_columns) != 1 or len(transaction_columns) != 1:
            raise FastPathError("sales columns are ambiguous")
        revenue_index = header.index(revenue_columns[0])
        transaction_index = header.index(transaction_columns[0])
        for line_index, line in enumerate(lines[header_index + 1 :], 1):
            if (file_index * 1_000 + line_index) % 250 == 0:
                context.check()
            try:
                row = next(csv.reader([line]))
            except csv.Error:
                continue
            if len(row) != len(header) or not row or row[0] == "TOTAL":
                continue
            transaction_id = row[transaction_index]
            if not transaction_id:
                raise FastPathError("sales row has an empty transaction id")
            if transaction_id in seen:
                continue
            cleaned = row[revenue_index].strip().replace(",", "")
            try:
                amount = Decimal(cleaned)
            except InvalidOperation as exc:
                raise FastPathError("sales row has invalid revenue") from exc
            if not amount.is_finite():
                raise FastPathError("sales row has non-finite revenue")
            seen.add(transaction_id)
            kept.append((transaction_id, region, amount))
            total += amount * rates[region]
    if not kept:
        raise FastPathError("regional archive produced no transactions")
    return total, kept


def _utc_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FastPathError(f"invalid ISO timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise FastPathError(f"timestamp is not UTC: {value!r}")
    return parsed.astimezone(UTC)


def _unique_counter_winner(values: Counter[str]) -> tuple[str, int]:
    return _unique_mapping_winner(values)


def _unique_mapping_winner(values: dict[str, int] | Counter[str]) -> tuple[str, int]:
    if not values:
        raise FastPathError("no qualifying values")
    maximum = max(values.values())
    winners = [key for key, value in values.items() if value == maximum]
    if len(winners) != 1:
        raise FastPathError("requested winner is not unique")
    return winners[0], maximum


def _log_timestamp(line: str, default_year: int) -> datetime | None:
    iso = re.search(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\b", line)
    if iso:
        return datetime.fromisoformat(iso.group(1).replace("Z", "+00:00")).astimezone(
            UTC
        )
    apache = re.search(
        r"^(\d{2}/[A-Za-z]{3}/\d{4}:\d{2}:\d{2}:\d{2} \+0000)\b", line
    )
    if apache:
        return datetime.strptime(
            apache.group(1), "%d/%b/%Y:%H:%M:%S %z"
        ).astimezone(UTC)
    syslog = re.search(
        r"^([A-Za-z]{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\b", line
    )
    if syslog:
        return datetime.strptime(
            syslog.group(1), "%b %d %H:%M:%S"
        ).replace(year=default_year, tzinfo=UTC)
    return None


def _unique_token(data: bytes, pattern: str) -> str | None:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    matches = set(re.findall(pattern, text))
    return next(iter(matches)) if len(matches) == 1 else None


_REPAIR_FUNCTIONS = {
    "core": {
        "top_score",
        "largest_token",
        "running_min",
        "unique_sorted",
        "rank_words",
    },
    "alpha": {"net_price", "col_sums"},
    "beta": {"chunked", "avg_ratio"},
    "gamma": {"bundle", "rank_scores"},
    "delta": {"local_day"},
}


def _solve_repair_archive(
    context: FastPathContext,
    expected_members: set[str],
    expected_modules: set[str],
) -> tuple[str, int]:
    _require_files(context.fingerprint, {"project.tar.gz"})
    archive_file = _require_file(context.fingerprint, "project.tar.gz")
    if not archive_file.magic_hex.startswith("1f8b"):
        raise FastPathError("project.tar.gz does not have gzip magic")
    sources = _bounded_tar_sources(context)
    if set(sources) != expected_members:
        raise FastPathError("repair archive manifest is unexpected")
    selftest = sources["proj/selftest.py"]
    compact = re.sub(r"\s+", "", selftest)
    required_formulas = (
        "hashlib.sha256(repr(value).encode()).hexdigest()[:10]",
        '"MAGIC-"+hashlib.sha256((repr(results)+SALT).encode()).hexdigest()[:12].upper()',
    )
    if not all(formula in compact for formula in required_formulas):
        raise FastPathError("self-test digest formula is not the known family")
    assignments = _literal_assignments(selftest)
    salt = assignments.get("SALT")
    cases = assignments.get("CASES")
    if not isinstance(salt, str) or not re.fullmatch(r"[A-Z0-9]{16}", salt):
        raise FastPathError("self-test salt is malformed")
    if not isinstance(cases, list) or not 2 <= len(cases) <= 64:
        raise FastPathError("self-test cases are malformed")
    allowed_functions = {
        (module, function)
        for module in expected_modules
        for function in _REPAIR_FUNCTIONS[module]
    }
    observed_functions: Counter[tuple[str, str]] = Counter()
    results: list[Any] = []
    for index, case in enumerate(cases):
        context.check()
        if (
            not isinstance(case, tuple)
            or len(case) != 4
            or not isinstance(case[0], str)
            or not isinstance(case[1], str)
            or not isinstance(case[2], tuple)
            or not isinstance(case[3], str)
            or not re.fullmatch(r"[a-f0-9]{10}", case[3])
        ):
            raise FastPathError(f"self-test case {index} is malformed")
        module, function, args, expected_digest = case
        if (module, function) not in allowed_functions:
            raise FastPathError("self-test references an unexpected function")
        source_name = f"proj/{module}.py"
        _require_repair_contract(sources[source_name], function)
        value = _repair_expected_value(function, args)
        digest = hashlib.sha256(repr(value).encode()).hexdigest()[:10]
        if digest != expected_digest:
            raise FastPathError(
                f"documented result disagrees with self-test case {index}"
            )
        observed_functions[(module, function)] += 1
        results.append(value)
    observed_set = set(observed_functions)
    if (
        not observed_set
        or {module for module, _function in observed_set} != expected_modules
        or (
            expected_modules != {"core"}
            and observed_set != allowed_functions
        )
        or (
            expected_modules == {"core"}
            and not 2 <= len(observed_set) <= len(_REPAIR_FUNCTIONS["core"])
        )
        or any(count < 2 for count in observed_functions.values())
    ):
        raise FastPathError("self-test does not cover every documented function twice")
    answer = (
        "MAGIC-"
        + hashlib.sha256((repr(results) + salt).encode()).hexdigest()[:12].upper()
    )
    if not re.fullmatch(r"MAGIC-[A-F0-9]{12}", answer):
        raise FastPathError("computed MAGIC line is malformed")
    return answer, len(cases)


def _bounded_tar_sources(context: FastPathContext) -> dict[str, str]:
    sources: dict[str, str] = {}
    expanded = 0
    with tarfile.open(context.path("project.tar.gz"), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise FastPathError("repair archive member count exceeds limit")
        for member in members:
            context.check()
            pure = PurePosixPath(member.name)
            if (
                not member.name
                or pure.is_absolute()
                or ".." in pure.parts
                or any(part in {"", "."} for part in pure.parts)
                or member.issym()
                or member.islnk()
                or member.isdev()
            ):
                raise FastPathError("repair archive contains an unsafe member")
            if member.isdir():
                continue
            if not member.isfile() or member.size < 0:
                raise FastPathError("repair archive contains a non-regular member")
            if member.name in sources:
                raise FastPathError("repair archive contains a duplicate member")
            expanded += member.size
            if expanded > MAX_ARCHIVE_EXPANDED:
                raise FastPathError("repair archive expanded size exceeds limit")
            reader = archive.extractfile(member)
            if reader is None:
                raise FastPathError("repair archive member is unreadable")
            raw = reader.read(member.size + 1)
            if len(raw) != member.size:
                raise FastPathError("repair archive member size changed while reading")
            try:
                sources[member.name] = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FastPathError("repair archive source is not UTF-8") from exc
    return sources


def _literal_assignments(source: str) -> dict[str, Any]:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise FastPathError("self-test is not valid Python") from exc
    values: dict[str, Any] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id in {"SALT", "CASES"}
        ):
            try:
                values[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError, SyntaxError) as exc:
                raise FastPathError(
                    f"{node.targets[0].id} is not a literal"
                ) from exc
    return values


def _require_repair_contract(source: str, function: str) -> None:
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise FastPathError("repair module is not valid Python") from exc
    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    node = definitions.get(function)
    if node is None:
        raise FastPathError(f"documented function {function!r} is missing")
    contract = _normalize(
        " ".join(
            value
            for value in (ast.get_docstring(tree), ast.get_docstring(node))
            if value
        )
    )
    markers = {
        "top_score": ("highest score", "lexicographically smallest"),
        "largest_token": ("decimal digit strings", "largest numeric value"),
        "running_min": ("running minimums", "min(xs[:i+1])"),
        "unique_sorted": ("distinct values", "sorted ascending"),
        "rank_words": ("length ascending", "alphabetically ascending"),
        "net_price": ("integer floor arithmetic", "discount_pct"),
        "col_sums": ("rectangular", "column sums"),
        "chunked": ("consecutive chunks", "final chunk may be shorter"),
        "avg_ratio": ("mean of the exact ratios", "as a float"),
        "bundle": ("new list", "never modify"),
        "rank_scores": ("score descending", "equal scores keep"),
        "local_day": ("local calendar day index", "3600", "86400"),
    }[function]
    if not all(marker in contract for marker in markers):
        raise FastPathError(f"contract for {function!r} is not the known family")


def _repair_expected_value(function: str, args: tuple[Any, ...]) -> Any:
    if function == "top_score" and len(args) == 1:
        pairs = args[0]
        if not isinstance(pairs, list) or not pairs:
            raise FastPathError("top_score arguments are malformed")
        normalized = _name_score_pairs(pairs)
        highest = max(score for _name, score in normalized)
        return min(name for name, score in normalized if score == highest)
    if function == "largest_token" and len(args) == 1:
        tokens = args[0]
        if (
            not isinstance(tokens, list)
            or not tokens
            or any(not isinstance(value, str) or not value.isdecimal() for value in tokens)
        ):
            raise FastPathError("largest_token arguments are malformed")
        return max(tokens, key=lambda value: int(value))
    if function == "running_min" and len(args) == 1:
        values = args[0]
        if not isinstance(values, list) or not values:
            raise FastPathError("running_min arguments are malformed")
        _require_plain_ints(values)
        output: list[int] = []
        current = values[0]
        for value in values:
            current = min(current, value)
            output.append(current)
        return output
    if function == "unique_sorted" and len(args) == 1:
        values = args[0]
        if not isinstance(values, list):
            raise FastPathError("unique_sorted arguments are malformed")
        _require_plain_ints(values)
        return sorted(set(values))
    if function == "rank_words" and len(args) == 1:
        words = args[0]
        if not isinstance(words, list) or any(
            not isinstance(word, str) for word in words
        ):
            raise FastPathError("rank_words arguments are malformed")
        return sorted(words, key=lambda word: (len(word), word))
    if function == "net_price" and len(args) == 2:
        cents, discount = args
        _require_plain_ints((cents, discount))
        if cents < 0 or not 0 <= discount <= 100:
            raise FastPathError("net_price arguments are outside contract")
        return cents - (cents * discount) // 100
    if function == "col_sums" and len(args) == 1:
        grid = args[0]
        if (
            not isinstance(grid, list)
            or not grid
            or any(not isinstance(row, list) or not row for row in grid)
        ):
            raise FastPathError("col_sums grid is malformed")
        width = len(grid[0])
        if any(len(row) != width for row in grid):
            raise FastPathError("col_sums grid is not rectangular")
        for row in grid:
            _require_plain_ints(row)
        return [sum(row[index] for row in grid) for index in range(width)]
    if function == "chunked" and len(args) == 2:
        values, size = args
        if not isinstance(values, list):
            raise FastPathError("chunked values are malformed")
        _require_plain_ints((size,))
        if size <= 0:
            raise FastPathError("chunk size must be positive")
        return [values[index : index + size] for index in range(0, len(values), size)]
    if function == "avg_ratio" and len(args) == 1:
        pairs = args[0]
        if not isinstance(pairs, list) or not pairs:
            raise FastPathError("avg_ratio arguments are malformed")
        total = 0.0
        for pair in pairs:
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise FastPathError("avg_ratio pair is malformed")
            numerator, denominator = pair
            _require_plain_ints((numerator, denominator))
            if denominator <= 0:
                raise FastPathError("avg_ratio denominator is not positive")
            total += numerator / denominator
        return total / len(pairs)
    if function == "bundle" and len(args) in {1, 2}:
        item = args[0]
        extras = [] if len(args) == 1 else args[1]
        if not isinstance(item, str) or not isinstance(extras, list):
            raise FastPathError("bundle arguments are malformed")
        return [*extras, item]
    if function == "rank_scores" and len(args) == 1:
        rows = args[0]
        if not isinstance(rows, list):
            raise FastPathError("rank_scores arguments are malformed")
        normalized = _name_score_pairs(rows)
        return [name for name, _score in sorted(normalized, key=lambda row: -row[1])]
    if function == "local_day" and len(args) == 2:
        timestamp, offset = args
        _require_plain_ints((timestamp, offset))
        if not -24 <= offset <= 24:
            raise FastPathError("timezone offset is outside contract")
        return (timestamp + offset * 3600) // 86400
    raise FastPathError(f"unsupported documented repair function {function!r}")


def _name_score_pairs(values: list[Any]) -> list[tuple[str, int]]:
    result: list[tuple[str, int]] = []
    for pair in values:
        if not isinstance(pair, tuple) or len(pair) != 2:
            raise FastPathError("name-score pair is malformed")
        name, score = pair
        if not isinstance(name, str):
            raise FastPathError("score name is not text")
        _require_plain_ints((score,))
        result.append((name, score))
    return result


def _require_plain_ints(values: Any) -> None:
    if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
        raise FastPathError("expected plain integer arguments")


def _validate_archive_paths(infos: list[Any]) -> None:
    for item in infos:
        name = item.filename
        pure = PurePosixPath(name)
        if (
            not name
            or name.startswith(("/", "\\"))
            or pure.is_absolute()
            or ".." in pure.parts
            or re.match(r"^[A-Za-z]:", name)
        ):
            raise FastPathError("archive contains an unsafe path")


def _validate_archive(
    archive: ZipFile, context: FastPathContext | None = None
) -> list[Any]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise FastPathError("archive member count exceeds limit")
    _validate_archive_paths(infos)
    if sum(item.file_size for item in infos) > MAX_ARCHIVE_EXPANDED:
        raise FastPathError("archive expanded size exceeds limit")
    expanded = 0
    for item in infos:
        if context is not None:
            context.check()
        with archive.open(item) as stream:
            while True:
                if context is not None:
                    context.check()
                chunk = stream.read(65_536)
                if not chunk:
                    break
                expanded += len(chunk)
                if expanded > MAX_ARCHIVE_EXPANDED:
                    raise FastPathError("archive expanded size exceeds limit")
    return infos


def _png_metadata(raw: bytes, context: FastPathContext) -> tuple[bytes, int]:
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        raise FastPathError("invalid PNG signature")
    offset = 8
    chunks = 0
    metadata: list[bytes] = []
    saw_iend = False
    while offset < len(raw):
        context.check()
        if offset + 12 > len(raw):
            raise FastPathError("truncated PNG chunk header")
        length = struct.unpack_from(">I", raw, offset)[0]
        kind = raw[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(raw):
            raise FastPathError("PNG chunk exceeds file bounds")
        data = raw[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", raw, offset + 8 + length)[0]
        if zlib.crc32(kind + data) & 0xFFFFFFFF != expected_crc:
            raise FastPathError("PNG chunk CRC mismatch")
        chunks += 1
        if kind == b"tEXt":
            metadata.append(data.partition(b"\x00")[2])
        elif kind == b"zTXt":
            _, separator, rest = data.partition(b"\x00")
            if not separator or not rest or rest[0] != 0:
                raise FastPathError("invalid zTXt chunk")
            metadata.append(zlib.decompress(rest[1:]))
        elif kind == b"iTXt":
            fields = data.split(b"\x00", 5)
            if len(fields) != 6:
                raise FastPathError("invalid iTXt chunk")
            compressed_flag, method, text = fields[1], fields[2], fields[5]
            if compressed_flag == b"\x01":
                if method != b"\x00":
                    raise FastPathError("unsupported iTXt compression method")
                text = zlib.decompress(text)
            elif compressed_flag != b"\x00":
                raise FastPathError("invalid iTXt compression flag")
            metadata.append(text)
        if kind == b"IEND":
            saw_iend = True
            offset = end
            break
        offset = end
    if not saw_iend or offset != len(raw):
        raise FastPathError("PNG does not end exactly at IEND")
    return b"\n".join(metadata), chunks


def _readable_ascii(value: bytes, context: FastPathContext) -> bool:
    if not value:
        return False
    for index, byte in enumerate(value):
        if index % 8_192 == 0:
            context.check()
        if byte not in {9, 10, 13} and not 32 <= byte <= 126:
            return False
    return True


def _xor_repeating(
    value: bytes, key: bytes, context: FastPathContext
) -> bytes:
    if not key:
        raise FastPathError("repeating XOR key is empty")
    output = bytearray(len(value))
    for index, byte in enumerate(value):
        if index % 8_192 == 0:
            context.check()
        output[index] = byte ^ key[index % len(key)]
    return bytes(output)


def _numbered_section(text: str, label: str, target: str) -> str:
    pattern = re.compile(
        rf"(?m)^{re.escape(label)}\s+(\d+\.\d+)\b[^\n]*$"
    )
    matches = list(pattern.finditer(text))
    selected = [index for index, match in enumerate(matches) if match.group(1) == target]
    if len(selected) != 1:
        raise FastPathError(f"{label} {target} boundary is missing or ambiguous")
    index = selected[0]
    start = matches[index].start()
    end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    return text[start:end]


def _integer_section(text: str, target: str) -> str:
    pattern = re.compile(r"(?m)^Section\s+(\d+):[^\n]*$")
    matches = list(pattern.finditer(text))
    selected = [index for index, match in enumerate(matches) if match.group(1) == target]
    if len(selected) != 1:
        raise FastPathError(f"Section {target} boundary is missing or ambiguous")
    index = selected[0]
    end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    return text[matches[index].start() : end]


def _appendix_section(text: str, appendix: str) -> str:
    pattern = re.compile(r"(?m)^Appendix\s+([A-Z]+)\b[^\n]*$")
    matches = list(pattern.finditer(text))
    selected = [index for index, match in enumerate(matches) if match.group(1) == appendix]
    if len(selected) != 1:
        raise FastPathError(f"Appendix {appendix} boundary is missing or ambiguous")
    index = selected[0]
    end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
    return text[matches[index].start() : end]


def _meeting_blocks(text: str) -> list[tuple[date, str]]:
    pattern = re.compile(r"(?m)^##\s+.+?-\s+(\d{4}-\d{2}-\d{2})\s*$")
    matches = list(pattern.finditer(text))
    blocks: list[tuple[date, str]] = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks.append((date.fromisoformat(match.group(1)), text[match.start() : end]))
    return blocks


def _parse_amendments(text: str) -> dict[int, dict[str, Any]]:
    line_pattern = re.compile(
        r"^Amendment (\d+) \(published (\d{4}-\d{2}-\d{2}); "
        r"effective (\d{4}-\d{2}-\d{2})\): (.+)$",
        re.MULTILINE,
    )
    entries: dict[int, dict[str, Any]] = {}
    matches = line_pattern.findall(text)
    if len(matches) != len(re.findall(r"(?m)^Amendment \d+ ", text)):
        raise FastPathError("an amendment line has an unexpected shape")
    for number_text, published_text, effective_text, action in matches:
        number = int(number_text)
        if number in entries:
            raise FastPathError("duplicate amendment number")
        entry: dict[str, Any] = {
            "published": date.fromisoformat(published_text),
            "effective": date.fromisoformat(effective_text),
            "revokes": None,
            "clause": None,
            "limit": None,
        }
        revoke = re.fullmatch(r"Amendment (\d+) is hereby revoked\.", action)
        change = re.fullmatch(
            r'Clause (\d+\.\d+) is amended to read: "Transfers under this clause '
            r'are subject to a limit of \$([0-9][0-9,]*(?:\.\d+)?)\."',
            action,
        )
        if revoke:
            entry["revokes"] = int(revoke.group(1))
        elif change:
            entry["clause"] = change.group(1)
            entry["limit"] = Decimal(change.group(2).replace(",", ""))
        else:
            raise FastPathError(f"unrecognized Amendment {number} action")
        entries[number] = entry
    if not entries:
        raise FastPathError("no amendments parsed")
    return entries


def _resolve_revocations(entries: dict[int, dict[str, Any]]) -> set[int]:
    revokers: dict[int, list[int]] = defaultdict(list)
    for number, entry in entries.items():
        target = entry["revokes"]
        if target is not None:
            if target not in entries:
                raise FastPathError(f"Amendment {number} revokes an unknown amendment")
            revokers[target].append(number)
    memo: dict[int, bool] = {}

    def is_revoked(number: int, stack: set[int]) -> bool:
        if number in memo:
            return memo[number]
        if number in stack:
            raise FastPathError("cyclic amendment revocation is ambiguous")
        stack.add(number)
        result = any(not is_revoked(revoker, stack) for revoker in revokers[number])
        stack.remove(number)
        memo[number] = result
        return result

    return {number for number in entries if is_revoked(number, set())}


def _repeat_program(context: FastPathContext, command: list[str]) -> str:
    first = _run_program(context, command)
    second = _run_program(context, command)
    if first != second:
        raise FastPathError("program output changed between executions")
    return first


def _run_program(context: FastPathContext, command: list[str]) -> str:
    safe_env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONIOENCODING": "utf-8",
        "TASK_DIR": str(context.root),
    }
    if (
        len(command) >= 3
        and Path(command[0]).resolve() == Path(sys.executable).resolve()
        and command[1] == "-I"
    ):
        script = command[2]
        arguments = [script, *command[3:]]
        code = python_resource_prelude(
            max(1, int(context.deadline - time.monotonic()))
        ) + (
            "import runpy, sys\n"
            f"sys.path.insert(0, {str(context.root)!r})\n"
            f"sys.argv = {arguments!r}\n"
            f"runpy.run_path({script!r}, run_name='__main__')\n"
        )
        command = [sys.executable, "-I", "-c", code]
    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        process = subprocess.Popen(
            command,
            cwd=context.root,
            env=safe_env,
            stdout=stdout_file,
            stderr=stderr_file,
            start_new_session=True,
        )
        while True:
            if context.cancellation.cancelled:
                _kill_process(process)
                process.wait()
                context.cancellation.check()
            if time.monotonic() > context.deadline:
                _kill_process(process)
                process.wait()
                raise FastPathDeadline("direct execution deadline exceeded")
            try:
                process.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
        _kill_process(process)
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout_bytes = stdout_file.read(MAX_DIRECT_OUTPUT + 1)
        stderr_bytes = stderr_file.read(MAX_DIRECT_OUTPUT + 1)
    context.check()
    if process.returncode != 0:
        raise FastPathError(f"subprocess exited with status {process.returncode}")
    if len(stdout_bytes) > MAX_DIRECT_OUTPUT or len(stderr_bytes) > MAX_DIRECT_OUTPUT:
        raise FastPathError("subprocess output exceeds limit")
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if stderr.strip():
        raise FastPathError("subprocess wrote to stderr")
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0].strip() or lines[0] != lines[0].strip():
        raise FastPathError("subprocess did not emit exactly one clean line")
    return lines[0]


def _kill_process(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _lcg_sum(
    x0: int,
    multiplier: int,
    increment: int,
    modulus: int,
    count: int,
    divisor: int,
    context: FastPathContext,
) -> tuple[int, int, int]:
    value = x0
    total = 0
    accepted = 0
    for index in range(count):
        if index % 8_192 == 0:
            context.check()
        value = (multiplier * value + increment) % modulus
        if value % divisor == 0:
            total += value
            accepted += 1
    return total, accepted, value


def _lcg_final_state(
    x0: int, multiplier: int, increment: int, modulus: int, count: int
) -> int:
    """Apply an affine recurrence in O(log count) for an independent check."""

    result_multiplier, result_increment = 1, 0
    base_multiplier, base_increment = multiplier % modulus, increment % modulus
    remaining = count
    while remaining:
        if remaining & 1:
            result_multiplier, result_increment = (
                base_multiplier * result_multiplier % modulus,
                (base_multiplier * result_increment + base_increment) % modulus,
            )
        base_multiplier, base_increment = (
            base_multiplier * base_multiplier % modulus,
            (base_multiplier * base_increment + base_increment) % modulus,
        )
        remaining >>= 1
    return (result_multiplier * x0 + result_increment) % modulus


def _squarefree_count(
    low: int, high: int, digit_divisor: int, context: FastPathContext
) -> int:
    limit = math.isqrt(high)
    sieve = bytearray(b"\x01") * (limit + 1)
    sieve[:2] = b"\x00\x00"
    for value in range(2, math.isqrt(limit) + 1):
        if sieve[value]:
            start = value * value
            sieve[start : limit + 1 : value] = b"\x00" * (
                ((limit - start) // value) + 1
            )
    flags = bytearray(b"\x01") * (high - low + 1)
    for index, prime in enumerate(
        value for value in range(2, limit + 1) if sieve[value]
    ):
        if index % 250 == 0:
            context.check()
        square = prime * prime
        first = ((low + square - 1) // square) * square
        if first <= high:
            count = ((high - first) // square) + 1
            flags[first - low : high - low + 1 : square] = b"\x00" * count
    sample_count = min(32, len(flags))
    for sample in range(sample_count):
        index = sample * (len(flags) - 1) // max(1, sample_count - 1)
        if bool(flags[index]) != _is_squarefree_trial(low + index):
            raise FastPathError("squarefree sieve disagrees with trial division")
    total = 0
    for index, flag in enumerate(flags):
        if index % 8_192 == 0:
            context.check()
        if flag and sum(int(char) for char in str(low + index)) % digit_divisor == 0:
            total += 1
    return total


def _is_squarefree_trial(value: int) -> bool:
    factor = 2
    while factor * factor <= value:
        if value % (factor * factor) == 0:
            return False
        factor += 1
    return True


def _fixed_cardinality_subset_sum(
    values: list[int],
    target: int,
    subset_size: int,
    context: FastPathContext,
) -> list[int]:
    midpoint = len(values) // 2
    left = values[:midpoint]
    right = values[midpoint:]
    minimum_left = max(0, subset_size - len(right))
    maximum_left = min(subset_size, len(left))
    expected_left = subset_size * len(left) / len(values)
    left_counts = sorted(
        range(minimum_left, maximum_left + 1),
        key=lambda value: (abs(value - expected_left), value),
    )

    def combinations_with_sum(source: list[int], count: int) -> Any:
        for ordinal, indices in enumerate(
            itertools.combinations(range(len(source)), count)
        ):
            if ordinal % 8_192 == 0:
                context.check()
            yield sum(source[index] for index in indices), indices

    for left_count in left_counts:
        context.check()
        right_count = subset_size - left_count
        left_combinations = math.comb(len(left), left_count)
        right_combinations = math.comb(len(right), right_count)
        if left_combinations <= right_combinations:
            lookup: dict[int, tuple[int, ...]] = {}
            for subtotal, indices in combinations_with_sum(left, left_count):
                lookup.setdefault(subtotal, indices)
            for subtotal, indices in combinations_with_sum(right, right_count):
                match = lookup.get(target - subtotal)
                if match is not None:
                    return list(match) + [midpoint + index for index in indices]
        else:
            lookup = {}
            for subtotal, indices in combinations_with_sum(right, right_count):
                lookup.setdefault(subtotal, indices)
            for subtotal, indices in combinations_with_sum(left, left_count):
                match = lookup.get(target - subtotal)
                if match is not None:
                    return list(indices) + [
                        midpoint + index for index in match
                    ]
        del lookup
    raise FastPathError("no exact-cardinality subset found")


def _tour_length(tour: list[int], distances: list[list[float]]) -> float:
    return sum(
        distances[tour[index]][tour[(index + 1) % len(tour)]]
        for index in range(len(tour))
    )


def _nearest_neighbor_tour(
    start: int, distances: list[list[float]], context: FastPathContext
) -> list[int]:
    remaining = set(range(len(distances)))
    remaining.remove(start)
    tour = [start]
    while remaining:
        context.check()
        current = tour[-1]
        next_point = min(
            remaining,
            key=lambda candidate: (distances[current][candidate], candidate),
        )
        remaining.remove(next_point)
        tour.append(next_point)
    return tour


def _two_opt(
    initial: list[int],
    distances: list[list[float]],
    threshold: float,
    context: FastPathContext,
) -> tuple[list[int], float]:
    tour = list(initial)
    length = _tour_length(tour, distances)
    size = len(tour)
    improved = True
    passes = 0
    while improved and time.monotonic() < context.deadline - 0.25:
        improved = False
        passes += 1
        for left in range(size - 1):
            if left % 8 == 0:
                context.check()
            a = tour[left]
            b = tour[(left + 1) % size]
            last_right = size - 1 if left == 0 else size
            for right in range(left + 2, last_right):
                c = tour[right]
                d = tour[(right + 1) % size]
                delta = (
                    distances[a][c]
                    + distances[b][d]
                    - distances[a][b]
                    - distances[c][d]
                )
                if delta < -1e-9:
                    tour[left + 1 : right + 1] = reversed(
                        tour[left + 1 : right + 1]
                    )
                    length += delta
                    improved = True
                    break
            if improved or length <= threshold:
                break
        if passes > size * size:
            raise FastPathError("2-opt failed to converge")
    return tour, _tour_length(tour, distances)


def _double_bridge(tour: list[int], iteration: int) -> list[int]:
    size = len(tour)
    quarter = size // 4
    first = 1 + (iteration * 3) % max(1, quarter - 2)
    second = quarter + (iteration * 5) % max(1, quarter - 2)
    third = 2 * quarter + (iteration * 7) % max(1, quarter - 2)
    return (
        tour[:first]
        + tour[second:third]
        + tour[first:second]
        + tour[third:]
    )


def _tsp_search(
    distances: list[list[float]],
    threshold: float,
    context: FastPathContext,
) -> tuple[list[int], float]:
    best: list[int] = []
    best_length = math.inf
    starts = sorted(
        range(len(distances)),
        key=lambda index: (-sum(distances[index]), index),
    )
    for start in starts:
        if time.monotonic() >= context.deadline - 0.5:
            break
        candidate = _nearest_neighbor_tour(start, distances, context)
        candidate, length = _two_opt(
            candidate, distances, threshold, context
        )
        if length < best_length:
            best, best_length = candidate, length
        if best_length <= threshold:
            return best, best_length
    iteration = 0
    while best and time.monotonic() < context.deadline - 0.5:
        candidate = _double_bridge(best, iteration)
        candidate, length = _two_opt(
            candidate, distances, threshold, context
        )
        if length < best_length:
            best, best_length = candidate, length
        if best_length <= threshold:
            break
        iteration += 1
    if not best:
        raise FastPathError("tour search produced no candidate")
    return best, best_length


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    output = ""
    while value:
        value, remainder = divmod(value, 36)
        output = alphabet[remainder] + output
    return output


def _unique(values: list[Any], label: str) -> Any:
    if len(values) != 1:
        raise FastPathError(f"{label} is missing or ambiguous")
    return values[0]


def _normalize_numeric(value: str) -> str:
    return _format_decimal(Decimal(value.replace(",", "")))


def _format_decimal(value: Decimal, *, places: int | None = None) -> str:
    if places is not None:
        return f"{value:.{places}f}"
    output = format(value, "f")
    if "." in output:
        output = output.rstrip("0").rstrip(".")
    return output or "0"
