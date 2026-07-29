from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import tempfile
import time
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import fast_paths
from agent_types import (
    CancellationToken,
    Candidate,
    SolveCancelled,
    Tile,
    TileRecord,
    TileStatus,
)
from fast_paths import (
    FastPathDispatcher,
    FastPathStatus,
    is_registered_fast_path_title,
)
from orchestrator import AgentConfig, AgentController
from solver import TileSolver
from tool_runtime import TaskToolRuntime


def zip_bytes(files: dict[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with ZipFile(output, "w") as archive:
        for name, value in files.items():
            archive.writestr(
                name, value.encode("utf-8") if isinstance(value, str) else value
            )
    return output.getvalue()


def png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


class FastPathFixtureTests(unittest.TestCase):
    def dispatch(
        self,
        *,
        title: str,
        category: str,
        answer_format: str,
        prompt: str,
        files: dict[str, bytes | str],
        disabled: frozenset[str] = frozenset(),
        cancellation: CancellationToken | None = None,
        dispatcher: FastPathDispatcher | None = None,
    ):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, value in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, bytes):
                path.write_bytes(value)
            else:
                path.write_text(value, encoding="utf-8")
        token = cancellation or CancellationToken()
        runtime = TaskToolRuntime(root, "https://example.test", cancellation=token)
        detail = {
            "title": title,
            "category": category,
            "answer_format": answer_format,
            "prompt": prompt,
            "files": list(files),
            "points": 100,
        }
        tile = Tile(
            id="PR-TEST",
            category=category,
            points=100,
            title=title,
        )
        outcome = (dispatcher or FastPathDispatcher()).attempt(
            tile,
            detail,
            list(files),
            runtime,
            disabled_handlers=disabled,
        )
        return outcome, runtime

    def assert_solved(self, outcome, runtime, handler: str, answer: str) -> None:
        self.assertEqual(outcome.status, FastPathStatus.SOLVED, outcome.reason)
        self.assertEqual(outcome.handler, handler)
        self.assertEqual(outcome.answer, answer)
        self.assertTrue(outcome.source.startswith("artifact:artifact-"))
        self.assertTrue(outcome.verification_source.startswith("artifact:artifact-"))
        self.assertNotEqual(outcome.source, outcome.verification_source)
        artifact_id = outcome.source.removeprefix("artifact:")
        self.assertEqual(runtime.artifacts.get(artifact_id), answer)

    def test_data_family_handlers(self) -> None:
        cases = [
            {
                "title": "Dirty Ledger",
                "category": "Needle in the Haystack",
                "answer_format": "numeric",
                "prompt": (
                    "Download transactions.csv. The amount column is messy. "
                    "Compute rows whose status is exactly 'pending'."
                ),
                "files": {
                    "transactions.csv": (
                        "txn_id,status,amount\n"
                        'a,pending,"$1,234.50"\n'
                        'b,pending,"  2,000.25 USD"\n'
                        "c,pending,--\n"
                        "d,paid,99.00\n"
                    )
                },
                "handler": "dirty_ledger",
                "answer": "3234.75",
            },
            {
                "title": "Event Horizon",
                "category": "Needle in the Haystack",
                "answer_format": "exact_ci",
                "prompt": (
                    'Download events.jsonl. Which user has the most events of type '
                    '"purchase" from 2026-10-01 through 2026-10-14 inclusive? '
                    "There is a unique winner."
                ),
                "files": {
                    "events.jsonl": "\n".join(
                        json.dumps(value)
                        for value in (
                            {
                                "user": "u1",
                                "type": "purchase",
                                "ts": "2026-10-01T00:00:00Z",
                            },
                            {
                                "user": "u1",
                                "type": "purchase",
                                "ts": "2026-10-14T23:59:59Z",
                            },
                            {
                                "user": "u2",
                                "type": "purchase",
                                "ts": "2026-10-02T00:00:00Z",
                            },
                        )
                    ),
                },
                "handler": "event_horizon",
                "answer": "u1",
            },
            {
                "title": "Join the Dots",
                "category": "Needle in the Haystack",
                "answer_format": "exact_ci",
                "prompt": (
                    "Keep only the FIRST occurrence of each order id. "
                    "After deduplication, exclude orders whose status is refunded. "
                    "Return the email with the highest total."
                ),
                "files": {
                    "users.csv": (
                        "id,email,signup\n"
                        "1,a@example.com,2026-01-01\n"
                        "2,b@example.com,2026-01-01\n"
                    ),
                    "orders.jsonl": "\n".join(
                        json.dumps(value)
                        for value in (
                            {
                                "order_id": "o1",
                                "user_id": 1,
                                "amount_cents": 500,
                                "status": "paid",
                            },
                            {
                                "order_id": "o1",
                                "user_id": 2,
                                "amount_cents": 9999,
                                "status": "paid",
                            },
                            {
                                "order_id": "o2",
                                "user_id": 2,
                                "amount_cents": 300,
                                "status": "shipped",
                            },
                        )
                    ),
                },
                "handler": "join_the_dots",
                "answer": "a@example.com",
            },
            {
                "title": "Session Hunter",
                "category": "Needle in the Haystack",
                "answer_format": "exact_ci",
                "prompt": (
                    "Lines are NOT sorted. A session is a maximal run whose "
                    "consecutive events are at most 30 minutes apart. A user's "
                    "session-day count is distinct dates. During March 2026, "
                    "which user has the 3rd-highest count?"
                ),
                "files": {
                    "server.log": (
                        "2026-03-01T00:00:00Z host app: user=u1 GET / 200\n"
                        "02/Mar/2026:00:00:00 +0000 host app: user=u1 GET / 200\n"
                        "Mar 03 00:00:00 host app: user=u1 GET / 200\n"
                        "2026-03-04T00:00:00Z host app: user=u1 GET / 200\n"
                        "2026-03-01T00:00:00Z host app: user=u2 GET / 200\n"
                        "2026-03-02T00:00:00Z host app: user=u2 GET / 200\n"
                        "2026-03-03T00:00:00Z host app: user=u2 GET / 200\n"
                        "2026-03-01T00:00:00Z host app: user=u3 GET / 200\n"
                        "2026-03-02T00:00:00Z host app: user=u3 GET / 200\n"
                        "2026-03-01T00:00:00Z host app: user=u4 GET / 200"
                    )
                },
                "handler": "session_hunter",
                "answer": "u3",
            },
        ]
        for case in cases:
            with self.subTest(case["title"]):
                outcome, runtime = self.dispatch(**{
                    key: value
                    for key, value in case.items()
                    if key not in {"handler", "answer"}
                })
                self.assert_solved(
                    outcome, runtime, str(case["handler"]), str(case["answer"])
                )

    def test_encoding_and_archive_family_handlers(self) -> None:
        layer_blob = base64.b64encode(
            zlib.compress(base64.b64encode(b"ONION-ABCDEFGHIJKL"))
        ).decode("ascii")
        inner = zip_bytes({"token.txt": "VAULT-ABCDEFGHIJKL\n"})
        largest_name = "falcon"
        key = largest_name[::-1].encode("ascii")
        encrypted = bytes(value ^ key[index % len(key)] for index, value in enumerate(inner))
        outer = zip_bytes(
            {
                "note.txt": (
                    "The password is the name of the largest .dat file, reversed. "
                    "inner.blob is XOR-encrypted."
                ),
                "tiny.dat": b"x",
                f"{largest_name}.dat": b"xxxx",
                "inner.blob": encrypted,
            }
        )
        png = (
            b"\x89PNG\r\n\x1a\n"
            + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + png_chunk(b"tEXt", b"comment\x00PIXEL-ABCDEFGHIJKL")
            + png_chunk(b"IEND", b"")
        )
        records = [
            struct.pack("<HIB", 1, 10, 13) + b"ITEM-AAAAAAAA",
            struct.pack("<HIB", 2, 99, 13) + b"ITEM-BBBBBBBB",
        ]
        ledger = b"AJB1" + struct.pack("<H", len(records)) + b"".join(records)
        crib_plain = (
            b"AGENTJEOPARDY-V1\n"
            b"ordinary text\n"
            b"GHOST-ABCDEFGHIJKL\n"
        )
        xor_key = b"key"
        cipher = bytes(
            value ^ xor_key[index % len(xor_key)]
            for index, value in enumerate(crib_plain)
        )
        cases = [
            {
                "title": "Layer Cake",
                "category": "Cryptic",
                "answer_format": "exact",
                "prompt": (
                    "Layers include base64 and zlib and may repeat.\n"
                    f"blob: {layer_blob}"
                ),
                "files": {},
                "handler": "layer_cake",
                "answer": "ONION-ABCDEFGHIJKL",
            },
            {
                "title": "Locked Box",
                "category": "Cryptic",
                "answer_format": "exact",
                "prompt": "Download bundle.zip, start with note.txt, and find the token.",
                "files": {"bundle.zip": outer},
                "handler": "locked_box",
                "answer": "VAULT-ABCDEFGHIJKL",
            },
            {
                "title": "Hidden Pixels",
                "category": "Cryptic",
                "answer_format": "exact",
                "prompt": (
                    "Download image.png. The token is hidden in metadata, "
                    "not in its pixels."
                ),
                "files": {"image.png": png},
                "handler": "hidden_pixels",
                "answer": "PIXEL-ABCDEFGHIJKL",
            },
            {
                "title": "Binary Ledger",
                "category": "Cryptic",
                "answer_format": "exact",
                "prompt": (
                    "Download data.bin and spec.md. Parse them; the maximum is unique."
                ),
                "files": {
                    "data.bin": ledger,
                    "spec.md": (
                        'magic, the ASCII characters "AJB1"\n'
                        "u16 record_count\nu16 id\nu32 value\n"
                        "u8 name_len\nname_len bytes\n"
                    ),
                },
                "handler": "binary_ledger",
                "answer": "ITEM-BBBBBBBB",
            },
            {
                "title": "Crib Sheet",
                "category": "Cryptic",
                "answer_format": "exact",
                "prompt": (
                    "The first line is exactly AGENTJEOPARDY-V1 and the file was "
                    "XOR-encrypted with a repeating key."
                ),
                "files": {"cipher.bin": cipher},
                "handler": "crib_sheet",
                "answer": "GHOST-ABCDEFGHIJKL",
            },
        ]
        for case in cases:
            with self.subTest(case["title"]):
                outcome, runtime = self.dispatch(**{
                    key: value
                    for key, value in case.items()
                    if key not in {"handler", "answer"}
                })
                self.assert_solved(
                    outcome, runtime, str(case["handler"]), str(case["answer"])
                )

    def test_document_family_handlers(self) -> None:
        notes: dict[str, str] = {}
        for month in range(1, 13):
            action = (
                "### Action Items\n- Tariq: ship it (ticket AB-1234)\n"
                if month == 6
                else "### Action Items\n- Lee: routine work (ticket ZZ-9999)\n"
            )
            notes[f"notes_2026-{month:02d}.md"] = (
                f"# Notes\n## Sync - 2026-{month:02d}-10\n{action}"
            )
        errata = (
            "Entry dated 2026-01-01 - Section 1: authoritative report is report_alpha\n"
            "Entry dated 2026-02-01 - Section 1: authoritative report is report_bravo\n"
            "Entry dated 2026-03-01 - Section 2: authoritative report is report_charlie\n"
            "Entry dated 2026-04-01 - Section 8: authoritative report is report_charlie\n"
        )
        reports = {
            "report_alpha.txt": "Section 1: A\nRecorded figure for this section: 1\n",
            "report_bravo.txt": "Section 1: A\nRecorded figure for this section: 10\n",
            "report_charlie.txt": (
                "Section 2: B\nRecorded figure for this section: 20\n"
                "Section 8: C\nRecorded figure for this section: 3\n"
            ),
            "report_delta.txt": "Section 9: D\nRecorded figure for this section: 999\n",
        }
        cases = [
            {
                "title": "Buried Clause",
                "category": "Ancient Scrolls",
                "answer_format": "numeric",
                "prompt": (
                    "What is the maximum reimbursement for a single claim under "
                    "Section 8.1?"
                ),
                "files": {
                    "policy_manual.txt": (
                        "Section 7.9 - Other\n"
                        "The maximum reimbursement for a single claim under this "
                        "section is $5.\n"
                        "Section 8.1 - Target\n"
                        "The maximum reimbursement for a single claim under this "
                        "section is $12,345.50.\n"
                        "Section 8.2 - Other\n"
                    )
                },
                "handler": "buried_clause",
                "answer": "12345.5",
            },
            {
                "title": "Two Hops",
                "category": "Ancient Scrolls",
                "answer_format": "numeric",
                "prompt": (
                    "Clause 14.2 defines the monthly service fee. It requires "
                    "cross-referencing an appendix and another clause."
                ),
                "files": {
                    "contract.txt": (
                        "Clause 3.1 - Multiplier\n"
                        "The adjustment multiplier applicable to fee computations "
                        "under this Agreement is 2.5.\n"
                        "Clause 14.2 - Fee\n"
                        "The monthly service fee shall equal the base rate listed "
                        "in Appendix G for tier Silver customers, multiplied by "
                        "the adjustment multiplier set forth in Clause 3.1.\n"
                        "Appendix G - Rates\n"
                        "Tier Silver: $1,200.00 per month\n"
                        "Appendix H - Other\n"
                    )
                },
                "handler": "two_hops",
                "answer": "3000",
            },
            {
                "title": "Minute Details",
                "category": "Ancient Scrolls",
                "answer_format": "exact",
                "prompt": (
                    "Each meeting heading has a date. Exactly one action item was "
                    "assigned to Tariq during June 2026; return its ticket code."
                ),
                "files": notes,
                "handler": "minute_details",
                "answer": "AB-1234",
            },
            {
                "title": "Living Document",
                "category": "Ancient Scrolls",
                "answer_format": "numeric",
                "prompt": (
                    "The AMENDMENTS section lists each effective date. A revoked "
                    "amendment is void. What limit was in force for Clause 5.1 "
                    "on 2024-08-18?"
                ),
                "files": {
                    "regulation.txt": (
                        "Clause 5.1 - Base\n"
                        "Transfers under this clause are subject to a limit of $100.\n"
                        "AMENDMENTS\n"
                        "Amendment 1 (published 2024-01-01; effective 2024-02-01): "
                        'Clause 5.1 is amended to read: "Transfers under this clause '
                        'are subject to a limit of $200."\n'
                        "Amendment 2 (published 2024-02-01; effective 2024-07-01): "
                        'Clause 5.1 is amended to read: "Transfers under this clause '
                        'are subject to a limit of $300."\n'
                        "Amendment 3 (published 2024-03-01; effective 2024-04-01): "
                        "Amendment 2 is hereby revoked.\n"
                    )
                },
                "handler": "living_document",
                "answer": "200",
            },
            {
                "title": "Trust the Errata",
                "category": "Ancient Scrolls",
                "answer_format": "numeric",
                "prompt": (
                    "The latest-dated entry is final and names the authoritative "
                    "report. Compute (A - B) * C, where A is Section 2, "
                    "B is Section 1, and C is Section 8."
                ),
                "files": {"errata.txt": errata, **reports},
                "handler": "trust_the_errata",
                "answer": "30",
            },
        ]
        for case in cases:
            with self.subTest(case["title"]):
                outcome, runtime = self.dispatch(**{
                    key: value
                    for key, value in case.items()
                    if key not in {"handler", "answer"}
                })
                self.assert_solved(
                    outcome, runtime, str(case["handler"]), str(case["answer"])
                )

    def test_direct_execution_handlers(self) -> None:
        cases = [
            {
                "title": "Spaghetti",
                "category": "Ship It",
                "answer_format": "exact",
                "prompt": (
                    "Download program.py. It is a deliberately obfuscated Python "
                    "program. Determine what it prints when run with python3. "
                    "It starts with OUT-."
                ),
                "files": {"program.py": 'print("OUT-LOCALTEST")\n'},
                "handler": "spaghetti",
                "answer": "OUT-LOCALTEST",
            },
            {
                "title": "Tiny VM",
                "category": "Ship It",
                "answer_format": "exact",
                "prompt": (
                    "Download vm.py and program.bin. vm.py is a tiny bytecode "
                    "interpreter. The output starts with VM-."
                ),
                "files": {
                    "vm.py": (
                        "from pathlib import Path\n"
                        "import sys\n"
                        "print('VM-' + Path(sys.argv[1]).read_bytes().decode())\n"
                    ),
                    "program.bin": b"LOCALTEST",
                },
                "handler": "tiny_vm",
                "answer": "VM-LOCALTEST",
            },
        ]
        for case in cases:
            with self.subTest(case["title"]):
                outcome, runtime = self.dispatch(**{
                    key: value
                    for key, value in case.items()
                    if key not in {"handler", "answer"}
                })
                self.assert_solved(
                    outcome, runtime, str(case["handler"]), str(case["answer"])
                )

    def test_prompt_compute_handlers(self) -> None:
        x = 1
        expected_sum = 0
        for _ in range(20):
            x = (3 * x + 1) % 17
            if x % 2 == 0:
                expected_sum += x
        cases = [
            {
                "title": "Congruential Cascade",
                "category": "Heavy Compute",
                "answer_format": "numeric",
                "prompt": (
                    "A pseudo-random sequence has x0 = 1; "
                    "x_(n+1) = (3 * x_n + 1) mod 17. Generate x_1..x_20, "
                    "apply the recurrence 20 times, and sum terms divisible by 2."
                ),
                "files": {},
                "handler": "congruential_cascade",
                "answer": str(expected_sum),
            },
            {
                "title": "Squarefree Census",
                "category": "Heavy Compute",
                "answer_format": "numeric",
                "prompt": (
                    "Consider the 10 consecutive integers with 1 <= n <= 10. "
                    "A number is special if the sum of its decimal digits is "
                    "divisible by 1 and n is squarefree."
                ),
                "files": {},
                "handler": "squarefree_census",
                "answer": "7",
            },
            {
                "title": "Proof of Work",
                "category": "Heavy Compute",
                "answer_format": "validator",
                "prompt": (
                    "Find a string that starts with the prefix 'TEST-' such that "
                    "the SHA-256 hex digest starts with '0' (1 zeros). It must be "
                    "at most 128 characters long and use printable non-whitespace "
                    "ASCII codepoints 33-126."
                ),
                "files": {},
                "handler": "proof_of_work",
                "answer": None,
            },
        ]
        for case in cases:
            with self.subTest(case["title"]):
                outcome, runtime = self.dispatch(**{
                    key: value
                    for key, value in case.items()
                    if key not in {"handler", "answer"}
                })
                self.assertEqual(outcome.status, FastPathStatus.SOLVED, outcome.reason)
                self.assertEqual(outcome.handler, case["handler"])
                if case["answer"] is None:
                    self.assertTrue(outcome.answer.startswith("TEST-"))
                    self.assertTrue(
                        hashlib.sha256(outcome.answer.encode("ascii"))
                        .hexdigest()
                        .startswith("0")
                    )
                else:
                    self.assert_solved(
                        outcome, runtime, str(case["handler"]), str(case["answer"])
                    )


class FastPathSafetyTests(FastPathFixtureTests):
    def test_near_matches_and_malformed_inputs_fall_back(self) -> None:
        extension_only, _ = self.dispatch(
            title="Unknown",
            category="Needle in the Haystack",
            answer_format="numeric",
            prompt="Aggregate this file.",
            files={"transactions.csv": "txn_id,status,amount\na,pending,1\n"},
        )
        self.assertEqual(extension_only.status, FastPathStatus.NOT_APPLICABLE)

        title_only, _ = self.dispatch(
            title="Dirty Ledger",
            category="Needle in the Haystack",
            answer_format="numeric",
            prompt="Do something unrelated.",
            files={"transactions.csv": "txn_id,status,amount\na,pending,1\n"},
        )
        self.assertEqual(title_only.status, FastPathStatus.NOT_APPLICABLE)

        bad_schema, _ = self.dispatch(
            title="Dirty Ledger",
            category="Needle in the Haystack",
            answer_format="numeric",
            prompt="The amount column is messy; status is exactly pending.",
            files={"transactions.csv": "id,state,value\na,pending,1\n"},
        )
        self.assertEqual(bad_schema.status, FastPathStatus.INCONCLUSIVE)
        self.assertFalse(bad_schema.answer)

        wrong_magic, _ = self.dispatch(
            title="Binary Ledger",
            category="Cryptic",
            answer_format="exact",
            prompt="Use data.bin and spec.md; the maximum is unique.",
            files={
                "data.bin": b"NOPE",
                "spec.md": (
                    'magic, the ASCII characters "AJB1"\nu16 record_count\n'
                    "u16 id\nu32 value\nu8 name_len\nname_len bytes\n"
                ),
            },
        )
        self.assertEqual(wrong_magic.status, FastPathStatus.INCONCLUSIVE)
        self.assertFalse(wrong_magic.answer)

    def test_ambiguous_values_and_malformed_jsonl_are_inconclusive(self) -> None:
        records = [
            struct.pack("<HIB", 1, 99, 13) + b"ITEM-AAAAAAAA",
            struct.pack("<HIB", 2, 99, 13) + b"ITEM-BBBBBBBB",
        ]
        duplicate_max, _ = self.dispatch(
            title="Binary Ledger",
            category="Cryptic",
            answer_format="exact",
            prompt="Use data.bin and spec.md; the maximum is unique.",
            files={
                "data.bin": b"AJB1" + struct.pack("<H", 2) + b"".join(records),
                "spec.md": (
                    'magic, the ASCII characters "AJB1"\nu16 record_count\n'
                    "u16 id\nu32 value\nu8 name_len\nname_len bytes\n"
                ),
            },
        )
        self.assertEqual(duplicate_max.status, FastPathStatus.INCONCLUSIVE)

        lines = [
            json.dumps(
                {"user": "u1", "type": "purchase", "ts": "2026-10-01T00:00:00Z"}
            )
            for _ in range(3)
        ]
        lines.append("{malformed")
        malformed, _ = self.dispatch(
            title="Event Horizon",
            category="Needle in the Haystack",
            answer_format="exact_ci",
            prompt=(
                'Use events.jsonl. Find events of type "purchase" from 2026-10-01 '
                "through 2026-10-14 inclusive. There is a unique winner."
            ),
            files={"events.jsonl": "\n".join(lines)},
        )
        self.assertEqual(malformed.status, FastPathStatus.INCONCLUSIVE)
        self.assertFalse(malformed.answer)

        ambiguous_blob = base64.b64encode(
            b"ONION-ABCDEFGHIJKL\nONION-MNOPQRSTUVWX\n"
        ).decode("ascii")
        ambiguous_token, _ = self.dispatch(
            title="Layer Cake",
            category="Cryptic",
            answer_format="exact",
            prompt=(
                "Layers include base64 and zlib and may repeat.\n"
                f"blob: {ambiguous_blob}"
            ),
            files={},
        )
        self.assertEqual(ambiguous_token.status, FastPathStatus.INCONCLUSIVE)
        self.assertFalse(ambiguous_token.answer)

    def test_zip_traversal_expansion_and_crc_fail_closed(self) -> None:
        unsafe = zip_bytes(
            {
                "../escape.dat": b"xxxx",
                "note.txt": "largest .dat file reversed XOR-encrypted",
                "inner.blob": b"x",
            }
        )
        traversal, _ = self.dispatch(
            title="Locked Box",
            category="Cryptic",
            answer_format="exact",
            prompt="Download bundle.zip, read note.txt, find the token.",
            files={"bundle.zip": unsafe},
        )
        self.assertEqual(traversal.status, FastPathStatus.INCONCLUSIVE)

        oversized = zip_bytes(
            {
                "large.dat": b"x" * 20,
                "note.txt": "largest .dat file reversed XOR-encrypted",
                "inner.blob": b"x",
            }
        )
        with patch.object(fast_paths, "MAX_ARCHIVE_EXPANDED", 10):
            expansion, _ = self.dispatch(
                title="Locked Box",
                category="Cryptic",
                answer_format="exact",
                prompt="Download bundle.zip, read note.txt, find the token.",
                files={"bundle.zip": oversized},
            )
        self.assertEqual(expansion.status, FastPathStatus.INCONCLUSIVE)

        inner = zip_bytes({"token.txt": "VAULT-ABCDEFGHIJKL"})
        key = b"cba"
        encrypted = bytes(value ^ key[index % len(key)] for index, value in enumerate(inner))
        valid = zip_bytes(
            {
                "note.txt": "largest .dat file reversed XOR-encrypted",
                "abc.dat": b"AAAA",
                "inner.blob": encrypted,
            }
        )
        corrupt = bytearray(valid)
        location = valid.find(b"AAAA")
        self.assertGreaterEqual(location, 0)
        corrupt[location] ^= 0xFF
        crc, _ = self.dispatch(
            title="Locked Box",
            category="Cryptic",
            answer_format="exact",
            prompt="Download bundle.zip, read note.txt, find the token.",
            files={"bundle.zip": bytes(corrupt)},
        )
        self.assertEqual(crc.status, FastPathStatus.INCONCLUSIVE)

    def test_invalid_amendment_date_is_inconclusive(self) -> None:
        outcome, _ = self.dispatch(
            title="Living Document",
            category="Ancient Scrolls",
            answer_format="numeric",
            prompt=(
                "The AMENDMENTS section uses each effective date. A revoked "
                "amendment is void. What limit applied for Clause 5.1 on 2024-08-18?"
            ),
            files={
                "regulation.txt": (
                    "Clause 5.1 - Base\n"
                    "Transfers under this clause are subject to a limit of $100.\n"
                    "AMENDMENTS\n"
                    "Amendment 1 (published 2024-99-01; effective 2024-02-01): "
                    'Clause 5.1 is amended to read: "Transfers under this clause '
                    'are subject to a limit of $200."\n'
                )
            },
        )
        self.assertEqual(outcome.status, FastPathStatus.INCONCLUSIVE)

    def test_handler_deadline_becomes_inconclusive(self) -> None:
        class DeadlineHandler:
            name = "deadline"
            title = "Deadline"
            timeout_seconds = -1.0

            @staticmethod
            def recognizes(_fingerprint) -> bool:
                return True

            @staticmethod
            def solve(context):
                context.check()
                raise AssertionError("unreachable")

        outcome, _ = self.dispatch(
            title="Deadline",
            category="Test",
            answer_format="exact",
            prompt="deadline",
            files={},
            dispatcher=FastPathDispatcher(handlers=(DeadlineHandler(),)),
        )
        self.assertEqual(outcome.status, FastPathStatus.INCONCLUSIVE)
        self.assertIn("deadline", outcome.reason)

    def test_cpu_and_subprocess_handlers_observe_claim_cancellation(self) -> None:
        cpu_token = CancellationToken()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self.dispatch,
                title="Proof of Work",
                category="Heavy Compute",
                answer_format="validator",
                prompt=(
                    "Find a string that starts with the prefix 'CANCEL-' whose "
                    "SHA-256 hex digest starts with '00000000' (8 zeros). It is "
                    "at most 128 characters long and uses printable non-whitespace "
                    "ASCII codepoints 33-126."
                ),
                files={},
                cancellation=cpu_token,
            )
            time.sleep(0.05)
            cpu_token.cancel("claimed elsewhere")
            with self.assertRaises(SolveCancelled):
                future.result(timeout=2)

        process_token = CancellationToken()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self.dispatch,
                title="Spaghetti",
                category="Ship It",
                answer_format="exact",
                prompt=(
                    "Download program.py. It is a deliberately obfuscated Python "
                    "program. Determine what it prints when run with python3. "
                    "It starts with OUT-."
                ),
                files={
                    "program.py": (
                        "import time\n"
                        "time.sleep(10)\n"
                        "print('OUT-TOOLATE')\n"
                    )
                },
                cancellation=process_token,
            )
            time.sleep(0.15)
            process_token.cancel("claimed elsewhere")
            with self.assertRaises(SolveCancelled):
                future.result(timeout=2)


class FastPathIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def test_success_never_constructs_anthropic_client(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "transactions.csv").write_text(
                "txn_id,status,amount\na,pending,1.25\n", encoding="utf-8"
            )

            class Game:
                BASE = "https://example.test"
                MODEL = "unused"

                @staticmethod
                def task(_task_id: str) -> dict:
                    return {
                        "title": "Dirty Ledger",
                        "category": "Needle in the Haystack",
                        "points": 100,
                        "answer_format": "numeric",
                        "prompt": (
                            "The amount column is messy. Select status is exactly "
                            "'pending'."
                        ),
                    }

                @staticmethod
                def workdir(_task_id: str) -> str:
                    return directory

                @staticmethod
                def fetch_files(_task_id: str, _detail: dict) -> list[str]:
                    return ["transactions.csv"]

                @staticmethod
                def anthropic_client():
                    raise AssertionError("fast path must not construct Claude client")

                @staticmethod
                def log(*_values) -> None:
                    return None

            candidate = TileSolver(Game()).solve(
                Tile(
                    "PR-N1",
                    "Needle in the Haystack",
                    100,
                    "Dirty Ledger",
                ),
                0,
            )
            self.assertEqual(candidate.answer, "1.25")
            self.assertEqual(candidate.strategy, "fast:dirty_ledger")

    async def test_incorrect_canary_disables_next_generation_handler(self) -> None:
        class Game:
            BASE = "https://example.test"
            MODEL = "unused"

            @staticmethod
            def log(*_values) -> None:
                return None

        controller = AgentController(Game(), AgentConfig())
        tile = Tile(
            "Q-N1-a", "Needle in the Haystack", 100, "Dirty Ledger"
        )
        controller.records[tile.id] = TileRecord(
            tile=tile, status=TileStatus.CANDIDATE, attempts=1
        )
        candidate = Candidate(
            task_id=tile.id,
            answer="1",
            answer_format="numeric",
            category=tile.category,
            points=tile.points,
            recipe_key=tile.recipe_key,
            recipe_generation=0,
            confidence=0.99,
            evidence="two totals matched",
            source="artifact:artifact-0001",
            verification_source="artifact:artifact-0002",
            strategy="fast:dirty_ledger",
        )
        controller.gate.on_incorrect(candidate, 0)
        await controller._on_submission_result(candidate, {"result": "incorrect"})
        self.assertTrue(
            controller.solver.is_fast_path_disabled(
                tile.recipe_key, 1, "dirty_ledger"
            )
        )

    def test_disabled_handler_reaches_claude_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "transactions.csv").write_text(
                "txn_id,status,amount\na,pending,1.25\n", encoding="utf-8"
            )

            class ClaudeReached(RuntimeError):
                pass

            class Game:
                BASE = "https://example.test"
                MODEL = "unused"

                @staticmethod
                def task(_task_id: str) -> dict:
                    return {
                        "title": "Dirty Ledger",
                        "category": "Needle in the Haystack",
                        "points": 100,
                        "answer_format": "numeric",
                        "prompt": (
                            "The amount column is messy. Select status is exactly "
                            "'pending'."
                        ),
                    }

                @staticmethod
                def workdir(_task_id: str) -> str:
                    return directory

                @staticmethod
                def fetch_files(_task_id: str, _detail: dict) -> list[str]:
                    return ["transactions.csv"]

                @staticmethod
                def anthropic_client():
                    raise ClaudeReached

                @staticmethod
                def log(*_values) -> None:
                    return None

            tile = Tile(
                "Q-N1-b", "Needle in the Haystack", 100, "Dirty Ledger"
            )
            solver = TileSolver(Game())
            solver.disable_fast_path(tile.recipe_key, 1, "dirty_ledger")
            with self.assertRaises(ClaudeReached):
                solver.solve(tile, 1)

    def test_registered_title_breaks_only_scheduler_ties(self) -> None:
        class Game:
            BASE = "https://example.test"
            MODEL = "unused"

            @staticmethod
            def log(*_values) -> None:
                return None

        controller = AgentController(Game(), AgentConfig(model_workers=1))
        ordinary = TileRecord(Tile("T-1", "Other", 100, "Ordinary"))
        deterministic = TileRecord(
            Tile("T-2", "Needle in the Haystack", 100, "Dirty Ledger")
        )
        selected = controller._select_records([ordinary, deterministic], 1)
        self.assertEqual(selected[0].tile.id, "T-2")
        self.assertTrue(is_registered_fast_path_title("Dirty Ledger"))
        self.assertFalse(is_registered_fast_path_title("Ordinary"))
