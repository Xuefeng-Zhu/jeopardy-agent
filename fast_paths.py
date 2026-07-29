"""Conservative deterministic solvers for stable Jeopardy generator families.

The dispatcher recognizes a family from title, category, and prompt markers,
then validates file extensions, schemas, archive metadata, and magic bytes
before any answer can be produced. A recognizable but malformed task is
inconclusive: callers must fall back to the model rather than guess.
"""

from __future__ import annotations

import base64
import binascii
import calendar
import csv
import hashlib
import io
import json
import math
import os
import re
import signal
import struct
import subprocess
import sys
import time
import zlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol
from zipfile import BadZipFile, ZipFile

from agent_types import CancellationToken, SolveCancelled, Tile
from tool_runtime import TaskToolRuntime

MAX_FINGERPRINT_FILES = 160
MAX_ARCHIVE_MEMBERS = 256
MAX_ARCHIVE_EXPANDED = 16_000_000
MAX_FAST_FILE_BYTES = 12_000_000
MAX_DIRECT_OUTPUT = 64_000
TOKEN_CHARS = r"[A-Z0-9]"


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
                    magic = path.read_bytes()[:16].hex() if path.is_file() else ""
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

    def compact_summary(self) -> str:
        extensions = Counter(item.suffix or "<none>" for item in self.files)
        kinds = Counter(item.kind for item in self.files)
        schemas = sum(bool(item.columns or item.json_keys) for item in self.files)
        archives = sum(bool(item.archive_members) for item in self.files)
        magic = sorted({item.magic_hex[:16] for item in self.files if item.magic_hex})
        return (
            f"title={self.normalized_title or '<none>'} "
            f"category={self.normalized_category or '<none>'} "
            f"format={self.answer_format} files={len(self.files)} "
            f"extensions={dict(sorted(extensions.items()))} "
            f"kinds={dict(sorted(kinds.items()))} schemas={schemas} "
            f"archives={archives} magic={magic[:6]} "
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
    prompt_phrases = ("amount column is messy", "status is exactly", "pending")

    def solve(self, context: FastPathContext) -> tuple[str, str, float]:
        _require_files(context.fingerprint, {"transactions.csv"})
        file = _require_file(context.fingerprint, "transactions.csv")
        _require_columns(file, {"txn_id", "status", "amount"})
        path = context.path("transactions.csv")
        total_decimal, valid_rows = _dirty_ledger_decimal(path, context)
        total_cents, valid_rows_again = _dirty_ledger_cents(path, context)
        if valid_rows != valid_rows_again or total_decimal != Decimal(total_cents) / 100:
            raise FastPathError("independent money totals disagree")
        answer = _format_decimal(total_decimal.quantize(Decimal("0.01")), places=2)
        evidence = (
            f"parsed exact pending rows twice with Decimal and integer cents; "
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
        with ZipFile(path) as archive:
            members = _validate_archive(archive)
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
        decrypted = bytes(value ^ key[index % len(key)] for index, value in enumerate(encrypted))
        if not decrypted.startswith(b"PK"):
            raise FastPathError("decrypted inner.blob is not a ZIP archive")
        with ZipFile(io.BytesIO(decrypted)) as inner:
            inner_members = _validate_archive(inner)
            payload = b"\n".join(inner.read(item) for item in inner_members)
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
            plain = bytes(
                value ^ key[index % key_length] for index, value in enumerate(cipher)
            )
            if not plain.startswith(crib) or not _readable_ascii(plain):
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
        first = _lcg_sum(
            x0, multiplier, increment, modulus, count, divisor_value, context
        )
        second = _lcg_sum(
            x0, multiplier, increment, modulus, count, divisor_value, context
        )
        if first != second:
            raise FastPathError("independent recurrence computations disagree")
        return str(first[0]), f"generated {count} terms twice; sum/count/final-state all matched", 0.999


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
        first = _squarefree_count(low, high, divisor, context)
        second = _squarefree_count(low, high, divisor, context)
        if first != second:
            raise FastPathError("independent squarefree counts disagree")
        return str(first), f"segmented squarefree sieve over {expected} integers ran twice and matched", 0.999


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


HANDLERS: tuple[FastPathHandler, ...] = (
    DirtyLedgerHandler(),
    EventHorizonHandler(),
    JoinTheDotsHandler(),
    SessionHunterHandler(),
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
    TinyVMHandler(),
    CongruentialCascadeHandler(),
    SquarefreeCensusHandler(),
    ProofOfWorkHandler(),
)

REGISTERED_TITLES = frozenset(
    " ".join(handler.title.lower().split()) for handler in HANDLERS
)


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
    ) -> FastPathOutcome:
        started = time.monotonic()
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
                binascii.Error,
                struct.error,
                zlib.error,
            ) as exc:
                return FastPathOutcome(
                    status=FastPathStatus.INCONCLUSIVE,
                    handler=handler.name,
                    elapsed_seconds=time.monotonic() - started,
                    reason=f"{type(exc).__name__}: {exc}",
                    fingerprint=fingerprint,
                )
        return FastPathOutcome.not_applicable(fingerprint)


def is_registered_fast_path_title(title: str) -> bool:
    return _normalize(title) in REGISTERED_TITLES


def _normalize(value: str) -> str:
    return " ".join(value.lower().split())


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
            header = next(csv.reader(stream, delimiter=delimiter), [])
        columns = tuple(value.strip() for value in header)
        kind = "delimited"
    elif suffix == ".jsonl":
        keys: set[str] = set()
        with path.open("r", encoding="utf-8") as stream:
            sampled = 0
            for line in stream:
                cancellation.check()
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise TypeError("JSONL sample is not an object")
                keys.update(str(key) for key in value)
                sampled += 1
                if sampled >= 3:
                    break
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
    path: Path, context: FastPathContext
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
            if row.get("status") != "pending":
                continue
            amount = _parse_money(str(row.get("amount") or ""))
            if amount is not None:
                total += amount
                count += 1
    return total, count


def _dirty_ledger_cents(path: Path, context: FastPathContext) -> tuple[int, int]:
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
            if len(row) != 3 or row[1] != "pending":
                continue
            amount = _parse_money(row[2])
            if amount is not None:
                cents = amount * 100
                if cents != cents.to_integral_value():
                    raise FastPathError("money value has fractional cents")
                total += int(cents)
                count += 1
    return total, count


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


def _validate_archive(archive: ZipFile) -> list[Any]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_MEMBERS:
        raise FastPathError("archive member count exceeds limit")
    _validate_archive_paths(infos)
    if sum(item.file_size for item in infos) > MAX_ARCHIVE_EXPANDED:
        raise FastPathError("archive expanded size exceeds limit")
    bad_member = archive.testzip()
    if bad_member is not None:
        raise FastPathError(f"archive CRC failed for {bad_member}")
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


def _readable_ascii(value: bytes) -> bool:
    return bool(value) and all(byte in {9, 10, 13} or 32 <= byte <= 126 for byte in value)


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
    process = subprocess.Popen(
        command,
        cwd=context.root,
        env=safe_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    while True:
        if context.cancellation.cancelled:
            _kill_process(process)
            process.communicate()
            context.cancellation.check()
        if time.monotonic() > context.deadline:
            _kill_process(process)
            process.communicate()
            raise FastPathDeadline("direct execution deadline exceeded")
        try:
            stdout, stderr = process.communicate(timeout=0.05)
            break
        except subprocess.TimeoutExpired:
            continue
    context.check()
    if process.returncode != 0:
        raise FastPathError(f"subprocess exited with status {process.returncode}")
    if len(stdout) > MAX_DIRECT_OUTPUT or len(stderr) > MAX_DIRECT_OUTPUT:
        raise FastPathError("subprocess output exceeds limit")
    if stderr.strip():
        raise FastPathError("subprocess wrote to stderr")
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0].strip() or lines[0] != lines[0].strip():
        raise FastPathError("subprocess did not emit exactly one clean line")
    return lines[0]


def _kill_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
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
    total = 0
    for index, flag in enumerate(flags):
        if index % 8_192 == 0:
            context.check()
        if flag and sum(int(char) for char in str(low + index)) % digit_divisor == 0:
            total += 1
    return total


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
