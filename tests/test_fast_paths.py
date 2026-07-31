from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import struct
import tarfile
import tempfile
import time
import unittest
import zlib
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urljoin
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
    is_cpu_heavy_fast_path_title,
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


def tar_gz_bytes(files: dict[str, bytes | str]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, value in files.items():
            raw = value.encode("utf-8") if isinstance(value, str) else value
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            archive.addfile(info, io.BytesIO(raw))
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

    def test_bounded_web_trail_replays_one_session(self) -> None:
        calls: list[dict] = []

        def fake_execute(runtime, name, arguments):
            self.assertEqual(name, "http_request")
            calls.append(arguments)
            url = str(arguments["url"])
            if url.endswith("/trail/start"):
                body = '<html><a href="/trail/next">next</a></html>'
                resolved = "https://example.test/trail/start"
            elif url.endswith("/trail/next"):
                body = "<html>Final token: CRUMB-ABC123</html>"
                resolved = "https://example.test/trail/next"
            else:
                return json.dumps({"ok": False, "error": "unexpected URL"})
            artifact_id = runtime.artifacts.put(body)
            return json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "url": resolved,
                    "body_artifact": artifact_id,
                }
            )

        with patch.object(
            TaskToolRuntime, "execute", autospec=True, side_effect=fake_execute
        ):
            outcome, runtime = self.dispatch(
                title="Breadcrumbs",
                category="The Dark Web",
                answer_format="exact",
                prompt=(
                    "Begin at /trail/start and follow the breadcrumbs. "
                    "The token begins with CRUMB-."
                ),
                files={},
            )
        self.assert_solved(outcome, runtime, "breadcrumbs", "CRUMB-ABC123")
        self.assertEqual(len(calls), 2)
        self.assertEqual({call["session"] for call in calls}, {"fast-breadcrumbs"})

    def test_deep_pages_matches_live_prompt_shape(self) -> None:
        calls: list[dict] = []

        def fake_execute(runtime, name, arguments):
            self.assertEqual(name, "http_request")
            calls.append(arguments)
            url = str(arguments["url"])
            if url.endswith("/web/task/start/"):
                body = '<html><a href="step-2/">next</a></html>'
            elif url.endswith("/web/task/start/step-2/"):
                body = "<html>The token is DEEP-ABC123</html>"
            else:
                return json.dumps({"ok": False, "error": "unexpected URL"})
            artifact_id = runtime.artifacts.put(body)
            return json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "url": url,
                    "body_artifact": artifact_id,
                }
            )

        with patch.object(
            TaskToolRuntime, "execute", autospec=True, side_effect=fake_execute
        ):
            outcome, runtime = self.dispatch(
                title="Deep Pages",
                category="The Dark Web",
                answer_format="exact",
                prompt=(
                    "Somewhere deep in this site is a token: "
                    "https://example.test/web/task/start/ "
                    "Keep following the 'next' links. Answer with the token "
                    "(starts with DEEP-)."
                ),
                files={},
            )
        self.assert_solved(outcome, runtime, "deep_pages", "DEEP-ABC123")
        self.assertEqual(len(calls), 2)
        self.assertEqual({call["session"] for call in calls}, {"fast-deep_pages"})

    def test_bounded_web_trail_rejects_ambiguous_tokens(self) -> None:
        def fake_execute(runtime, _name, arguments):
            body = (
                "<html><a href='/trail/next'>next</a></html>"
                if str(arguments["url"]).endswith("/start")
                else (
                    "<html>Token: CRUMB-ABC123 "
                    "Token: CRUMB-XYZ789</html>"
                )
            )
            artifact_id = runtime.artifacts.put(body)
            return json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "url": urljoin(
                        "https://example.test", str(arguments["url"])
                    ),
                    "body_artifact": artifact_id,
                }
            )

        with patch.object(
            TaskToolRuntime, "execute", autospec=True, side_effect=fake_execute
        ):
            outcome, _runtime = self.dispatch(
                title="Deep Pages",
                category="The Dark Web",
                answer_format="exact",
                prompt="Start at /start and return the unique token.",
                files={},
            )
        self.assertEqual(outcome.status, FastPathStatus.INCONCLUSIVE)
        self.assertIn("deterministic invariant failed", outcome.reason)

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

    def test_observed_workfactor_verdict_is_deterministic(self) -> None:
        prompt = (
            "Find an ASCII string that begins with 'MINE26D94537-' whose "
            "SHA-256 digest begins with 68 zero BITS.\n"
            "If you conclude that no answer exists, or that finding one is "
            "beyond any feasible amount of compute, do not guess. Answer with "
            "the word IMPOSSIBLE, a space, and the ONE reason code below that "
            "applies:\n"
            "  PARITY     a parity or divisibility argument rules out every candidate\n"
            "  MAGNITUDE  the target is outside the range any combination can reach\n"
            "  ONETIMEPAD a one-time pad makes every plaintext equally likely\n"
            "  WORKFACTOR a solution exists, but the expected work to find it is "
            "far beyond this event\n"
            "Exactly one code is correct. A wrong verdict costs the tile's full "
            "value."
        )
        outcome, runtime = self.dispatch(
            title="Congruential Cascade",
            category="Heavy Compute",
            answer_format="validator",
            prompt=prompt,
            files={},
        )
        self.assert_solved(
            outcome,
            runtime,
            "congruential_workfactor",
            "IMPOSSIBLE WORKFACTOR",
        )

    def test_observed_repair_archives_are_solved_from_specs(self) -> None:
        def digest(value):
            return hashlib.sha256(repr(value).encode()).hexdigest()[:10]

        def selftest(salt, cases):
            return (
                "import hashlib\n"
                f"SALT = {salt!r}\n"
                f"CASES = {cases!r}\n"
                "def _digest(value):\n"
                "    return hashlib.sha256(repr(value).encode()).hexdigest()[:10]\n"
                "def main():\n"
                "    results = []\n"
                "    for mod_name, fn_name, args, want in CASES:\n"
                "        got = None\n"
                "        results.append(got)\n"
                '    print("MAGIC-" + hashlib.sha256(\n'
                "        (repr(results) + SALT).encode()).hexdigest()[:12].upper())\n"
            )

        fix_values = ["alpha", "beta", "10", "9"]
        fix_cases = [
            ("core", "top_score", ([("beta", 8), ("alpha", 8)],), digest("alpha")),
            ("core", "top_score", ([("beta", 9), ("alpha", 8)],), digest("beta")),
            ("core", "largest_token", (["9", "10"],), digest("10")),
            ("core", "largest_token", (["8", "9"],), digest("9")),
        ]
        fix_salt = "ABCDEFGHIJKLMNOP"
        fix_expected = (
            "MAGIC-"
            + hashlib.sha256(
                (repr(fix_values) + fix_salt).encode()
            ).hexdigest()[:12].upper()
        )
        fix_core = '''
"""top_score returns the highest score and the lexicographically smallest
name on ties. largest_token accepts decimal digit strings and returns the
largest numeric value."""
def top_score(pairs):
    """highest score; lexicographically smallest"""
def largest_token(tokens):
    """decimal digit strings; largest numeric value"""
'''
        fix_archive = tar_gz_bytes(
            {
                "README.md": "repair one function",
                "proj/__init__.py": "",
                "proj/core.py": fix_core,
                "proj/selftest.py": selftest(fix_salt, fix_cases),
            }
        )
        fix, fix_runtime = self.dispatch(
            title="Fix and Run",
            category="Ship It",
            answer_format="exact",
            prompt=(
                "Download project.tar.gz and extract it. The package proj/ "
                "contains exactly ONE function that violates its documented "
                "spec. Fix it so python proj/selftest.py passes and prints "
                "MAGIC- followed by 12 uppercase hex characters."
            ),
            files={"project.tar.gz": fix_archive},
        )
        self.assert_solved(fix, fix_runtime, "fix_and_run", fix_expected)

        observed_core_cases_with_values = [
            (
                "core",
                "running_min",
                ([5, 3, 4, -1],),
                [5, 3, 3, -1],
            ),
            (
                "core",
                "running_min",
                ([2, 8, 1],),
                [2, 2, 1],
            ),
            (
                "core",
                "unique_sorted",
                ([3, 1, 3, 2],),
                [1, 2, 3],
            ),
            (
                "core",
                "unique_sorted",
                ([7, 7, 4],),
                [4, 7],
            ),
            (
                "core",
                "rank_words",
                (["bbb", "a", "cc", "aa"],),
                ["a", "aa", "cc", "bbb"],
            ),
            (
                "core",
                "rank_words",
                (["z", "b", "aaa"],),
                ["b", "z", "aaa"],
            ),
        ]
        observed_core_cases = [
            (module, function, args, digest(value))
            for module, function, args, value in observed_core_cases_with_values
        ]
        observed_core_values = [
            item[3] for item in observed_core_cases_with_values
        ]
        observed_core_salt = "QWERTYUIOPASDFGH"
        observed_core_expected = (
            "MAGIC-"
            + hashlib.sha256(
                (repr(observed_core_values) + observed_core_salt).encode()
            ).hexdigest()[:12].upper()
        )
        observed_core = '''
"""running_min returns running minimums with out[i] == min(xs[:i+1]).
unique_sorted returns distinct values sorted ascending.
rank_words sorts by length ascending, ties broken alphabetically ascending."""
def running_min(xs):
    """running minimums; min(xs[:i+1])"""
def unique_sorted(xs):
    """distinct values; sorted ascending"""
def rank_words(words):
    """length ascending; alphabetically ascending"""
'''
        observed_core_archive = tar_gz_bytes(
            {
                "README.md": "repair one function",
                "proj/__init__.py": "",
                "proj/core.py": observed_core,
                "proj/selftest.py": selftest(
                    observed_core_salt, observed_core_cases
                ),
            }
        )
        observed, observed_runtime = self.dispatch(
            title="Fix and Run",
            category="Ship It",
            answer_format="exact",
            prompt=(
                "Download project.tar.gz and extract it. The package proj/ "
                "contains exactly ONE function that violates its documented "
                "spec. Fix it so python proj/selftest.py passes and prints "
                "MAGIC- followed by 12 uppercase hex characters."
            ),
            files={"project.tar.gz": observed_core_archive},
        )
        self.assert_solved(
            observed,
            observed_runtime,
            "fix_and_run",
            observed_core_expected,
        )

        repair_cases_with_values = [
            ("alpha", "net_price", (100, 25), 75),
            ("alpha", "net_price", (99, 10), 90),
            ("alpha", "col_sums", ([[1, 2], [3, 4]],), [4, 6]),
            ("alpha", "col_sums", ([[5], [7]],), [12]),
            ("beta", "chunked", ([1, 2, 3], 2), [[1, 2], [3]]),
            ("beta", "chunked", ([1, 2], 1), [[1], [2]]),
            ("beta", "avg_ratio", ([(1, 2), (3, 2)],), 1.0),
            ("beta", "avg_ratio", ([(2, 1), (4, 1)],), 3.0),
            ("gamma", "bundle", ("x",), ["x"]),
            ("gamma", "bundle", ("x", ["a"]), ["a", "x"]),
            ("gamma", "rank_scores", ([("a", 2), ("b", 2)],), ["a", "b"]),
            ("gamma", "rank_scores", ([("a", 1), ("b", 2)],), ["b", "a"]),
            ("delta", "local_day", (86400, 0), 1),
            ("delta", "local_day", (0, -1), -1),
        ]
        repair_cases = [
            (module, function, args, digest(value))
            for module, function, args, value in repair_cases_with_values
        ]
        repair_values = [item[3] for item in repair_cases_with_values]
        repair_salt = "PONMLKJIHGFEDCBA"
        repair_expected = (
            "MAGIC-"
            + hashlib.sha256(
                (repr(repair_values) + repair_salt).encode()
            ).hexdigest()[:12].upper()
        )
        modules = {
            "proj/alpha.py": '''
"""net_price uses integer floor arithmetic with discount_pct.
col_sums returns column sums for a rectangular grid."""
def net_price(cents, discount_pct):
    """integer floor arithmetic; discount_pct"""
def col_sums(grid):
    """rectangular column sums"""
''',
            "proj/beta.py": '''
"""chunked creates consecutive chunks and the final chunk may be shorter.
avg_ratio returns the mean of the exact ratios as a float."""
def chunked(xs, size):
    """consecutive chunks; final chunk may be shorter"""
def avg_ratio(pairs):
    """mean of the exact ratios as a float"""
''',
            "proj/gamma.py": '''
"""bundle returns a new list and must never modify extras.
rank_scores sorts by score descending and equal scores keep input order."""
def bundle(item, extras=None):
    """new list; never modify"""
def rank_scores(rows):
    """score descending; equal scores keep their order"""
''',
            "proj/delta.py": '''
"""local_day returns the local calendar day index using 3600 and 86400."""
def local_day(ts, tz_offset_hours):
    """local calendar day index; 3600; 86400"""
''',
        }
        fault_archive = tar_gz_bytes(
            {
                "README.md": "repair five functions",
                "proj/__init__.py": "",
                "proj/selftest.py": selftest(repair_salt, repair_cases),
                **modules,
            }
        )
        fault, fault_runtime = self.dispatch(
            title="Quintuple Fault",
            category="Ship It",
            answer_format="exact",
            prompt=(
                "Download project.tar.gz. Exactly FIVE of the seven functions "
                "in proj/ alpha.py, beta.py, gamma.py and delta.py violate their "
                "documented specs. Fix it so python proj/selftest.py passes and "
                "prints MAGIC- followed by 12 uppercase hex characters."
            ),
            files={"project.tar.gz": fault_archive},
        )
        self.assert_solved(
            fault, fault_runtime, "quintuple_fault", repair_expected
        )

    def test_thirty_systems_handler(self) -> None:
        rates = {"AA": "1", "BB": "2", "CC": "0.5", "DD": "1.25"}
        members: dict[str, str] = {
            "fx.csv": (
                "region,currency,usd_per_unit\n"
                + "".join(
                    f"{region},{region}D,{rate}\n"
                    for region, rate in rates.items()
                )
            )
        }
        expected = Decimal(0)
        for file_index in range(16):
            region = tuple(rates)[file_index % 4]
            name = f"sales_{region}_{file_index:03d}.csv"
            amount = Decimal(file_index + 1)
            members[name] = (
                "# generated preamble\n"
                "amount,txn_id,note\n"
                f"{amount},txn-{file_index},ok\n"
                "TOTAL,,footer\n"
            )
            expected += amount * Decimal(rates[region])
        outcome, runtime = self.dispatch(
            title="Thirty Systems",
            category="Needle in the Haystack",
            answer_format="numeric",
            prompt=(
                "Use usd_per_unit. Process files in ALPHABETICAL order of "
                "filename and keep the FIRST occurrence. Return the total "
                "rounded to 2 decimals."
            ),
            files={"regional_sales.zip": zip_bytes(members)},
        )
        self.assert_solved(
            outcome,
            runtime,
            "thirty_systems",
            f"{expected:.2f}",
        )

    def test_dirty_ledger_parses_the_requested_status(self) -> None:
        outcome, runtime = self.dispatch(
            title="Dirty Ledger",
            category="Needle in the Haystack",
            answer_format="numeric",
            prompt=(
                "Download transactions.csv. The amount column is messy. "
                "Skip invalid values and compute rows whose status is exactly "
                "'returned'."
            ),
            files={
                "transactions.csv": (
                    "txn_id,status,amount\n"
                    'a,returned,"$1,234.50"\n'
                    'b,returned,"  2,000.25 USD"\n'
                    "c,returned,missing\n"
                    "d,pending,99.00\n"
                )
            },
        )
        self.assert_solved(
            outcome,
            runtime,
            "dirty_ledger",
            "3234.75",
        )

    def test_spec_subset_and_tsp_handlers(self) -> None:
        spec_outcome, spec_runtime = self.dispatch(
            title="Spec vs Code",
            category="Ship It",
            answer_format="literal",
            prompt=(
                "lib.py contains exactly one bug. Per the spec, what should "
                "bucket_counts([0, 1, 2, 3, 4], [1, 3, 5]) return? "
                "Answer with the exact Python repr."
            ),
            files={
                "lib.py": "def bucket_counts(values, edges): return []\n",
                "spec.md": (
                    "edges is a strictly increasing list. Bucket i covers the "
                    "half-open range edges[i] <= v < edges[i+1]. Values outside "
                    "every bucket are ignored."
                ),
            },
        )
        self.assert_solved(spec_outcome, spec_runtime, "spec_vs_code", "[2, 2]")

        window_outcome, window_runtime = self.dispatch(
            title="Spec vs Code",
            category="Ship It",
            answer_format="literal",
            prompt=(
                "Download lib.py and spec.md. lib.py contains exactly one bug. "
                "Per the spec (NOT per the buggy code), what should "
                "window_sums([45, 12, 37, 39, -8, 21, 31, 38, 46, 44, 15, 20], "
                "3) return? Answer with the exact Python repr."
            ),
            files={
                "lib.py": "def window_sums(xs, k): return []\n",
                "spec.md": (
                    "Return the sum of every contiguous window of length k, in "
                    "order. For len(xs) == n the result has exactly n - k + 1 "
                    "elements."
                ),
            },
        )
        self.assert_solved(
            window_outcome,
            window_runtime,
            "spec_vs_code",
            "[94, 88, 68, 52, 44, 90, 115, 128, 105, 79]",
        )

        values = list(range(1, 49))
        target_indices = [0, 2, 4, 7, 12, 20]
        target = sum(values[index] for index in target_indices)
        subset_outcome, subset_runtime = self.dispatch(
            title="Needle in 2^44",
            category="Heavy Compute",
            answer_format="validator",
            prompt=(
                f"Find a subset of EXACTLY {len(target_indices)} numbers whose "
                f"sum is exactly {target}. It is guaranteed to exist. Answer "
                "with 0-based indices in strictly ascending order."
            ),
            files={
                "numbers.txt": (
                    f"TARGET: {target}\nK: {len(target_indices)}\n"
                    + "\n".join(str(value) for value in values)
                )
            },
        )
        self.assertEqual(subset_outcome.status, FastPathStatus.SOLVED)
        self.assertEqual(subset_outcome.handler, "needle_in_2_44")
        subset = [int(value) for value in subset_outcome.answer.split(",")]
        self.assertEqual(len(subset), len(target_indices))
        self.assertEqual(sum(values[index] for index in subset), target)
        self.assertTrue(subset_outcome.source.startswith("artifact:"))
        self.assertIsNotNone(subset_runtime)

        points = []
        for index in range(90):
            angle = (2 * math.pi * index) / 90
            points.append(
                f"{index} {round(1000 * math.cos(angle))} "
                f"{round(1000 * math.sin(angle))}"
            )
        tsp_outcome, _runtime = self.dispatch(
            title="Traveling Salesman",
            category="Heavy Compute",
            answer_format="validator",
            prompt=(
                "Find a tour that visits every point exactly once. The total "
                "Euclidean length must be at most 7000. Answer with a permutation "
                "and do not repeat the starting index."
            ),
            files={"points.txt": "\n".join(points)},
        )
        self.assertEqual(tsp_outcome.status, FastPathStatus.SOLVED)
        self.assertEqual(tsp_outcome.handler, "traveling_salesman")
        tour = [int(value) for value in tsp_outcome.answer.split(",")]
        self.assertEqual(sorted(tour), list(range(90)))

    def test_members_only_and_structured_breadcrumbs_handlers(self) -> None:
        calls: list[dict] = []

        def fake_execute(runtime, name, arguments):
            self.assertEqual(name, "http_request")
            calls.append(arguments)
            url = str(arguments["url"])
            method = str(arguments.get("method") or "GET")
            if url.endswith("/members/") and method == "GET":
                body = (
                    "<form method='post' action='/login'>"
                    "<input type='hidden' name='csrf' value='abc'>"
                    "<input name='user'><input type='password' name='pass'>"
                    "</form>"
                )
                resolved = url
            elif url.endswith("/login") and method == "POST":
                self.assertEqual(
                    arguments["form"],
                    {
                        "csrf": "abc",
                        "user": "alice",
                        "pass": "secret",
                    },
                )
                body = "<a href='/vault'>Open vault</a>"
                resolved = "https://example.test/home"
            elif url.endswith("/vault"):
                body = "Token: VAULT-ABC123"
                resolved = url
            else:
                return json.dumps({"ok": False, "error": "unexpected URL"})
            artifact_id = runtime.artifacts.put(body)
            return json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "url": resolved,
                    "headers": {},
                    "body_artifact": artifact_id,
                }
            )

        with patch.object(
            TaskToolRuntime, "execute", autospec=True, side_effect=fake_execute
        ):
            outcome, runtime = self.dispatch(
                title="Members Only",
                category="The Dark Web",
                answer_format="exact",
                prompt=(
                    "A token sits in the members-only vault at "
                    "https://example.test/members/ Your credentials — "
                    "username: alice password: secret. Log in via the form, "
                    "keep your session cookie, and open the vault. The token "
                    "starts with VAULT-."
                ),
                files={},
            )
        self.assert_solved(
            outcome, runtime, "members_only", "VAULT-ABC123"
        )
        self.assertEqual({call["session"] for call in calls}, {"fast-members-only"})

        trail_calls: list[str] = []

        def fake_trail(runtime, _name, arguments):
            url = str(arguments["url"])
            trail_calls.append(url)
            if url.endswith("/start"):
                body = json.dumps({"next": "/encoded"})
            elif url.endswith("/encoded"):
                body = base64.b64encode(b"/finish").decode("ascii")
            elif url.endswith("/finish"):
                body = "Token: CRUMB-ABC123"
            else:
                return json.dumps({"ok": False, "error": "unexpected URL"})
            artifact_id = runtime.artifacts.put(body)
            return json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "url": url,
                    "headers": {},
                    "body_artifact": artifact_id,
                }
            )

        with patch.object(
            TaskToolRuntime, "execute", autospec=True, side_effect=fake_trail
        ):
            trail, trail_runtime = self.dispatch(
                title="Breadcrumbs",
                category="The Dark Web",
                answer_format="exact",
                prompt=(
                    "The trail starts at https://example.test/start. Each step "
                    "hides a pointer in a different format. The token starts "
                    "with CRUMB-."
                ),
                files={},
            )
        self.assert_solved(
            trail, trail_runtime, "breadcrumbs", "CRUMB-ABC123"
        )
        self.assertEqual(len(trail_calls), 3)

    def test_gatekeeper_and_initiation_state_machines(self) -> None:
        gate_calls: list[dict] = []

        def fake_gate(runtime, _name, arguments):
            gate_calls.append(arguments)
            url = str(arguments["url"])
            if url.endswith("/gate/"):
                body = (
                    "First, GET https://example.test/gate/challenge to receive "
                    "your challenge number N. Then POST a form to "
                    "https://example.test/gate/answer with a single field "
                    '"response" equal to (N * N + 42) mod 999983.'
                )
            elif url.endswith("/challenge"):
                body = "Your challenge number: 12"
            elif url.endswith("/answer"):
                self.assertEqual(arguments["method"], "POST")
                self.assertEqual(arguments["form"], {"response": "186"})
                body = "token: GATE-ABC123"
            else:
                return json.dumps({"ok": False, "error": "unexpected URL"})
            artifact_id = runtime.artifacts.put(body)
            return json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "url": url,
                    "headers": {},
                    "body_artifact": artifact_id,
                }
            )

        with patch.object(
            TaskToolRuntime, "execute", autospec=True, side_effect=fake_gate
        ):
            gate, gate_runtime = self.dispatch(
                title="The Gatekeeper",
                category="The Dark Web",
                answer_format="exact",
                prompt=(
                    "Get past the gatekeeper at https://example.test/gate/ "
                    "Read carefully; you must hold a session and answer its "
                    "challenge. The token starts with GATE-."
                ),
                files={},
            )
        self.assert_solved(gate, gate_runtime, "gatekeeper", "GATE-ABC123")
        self.assertEqual(
            {call["session"] for call in gate_calls}, {"fast-gatekeeper"}
        )

        initiation_calls: list[dict] = []

        def fake_initiation(runtime, _name, arguments):
            initiation_calls.append(arguments)
            url = str(arguments["url"])
            if url.endswith("/init/"):
                body = (
                    "Step 1: POST a form to https://example.test/init/register "
                    'with field "codename".'
                )
            elif url.endswith("/register"):
                self.assertEqual(
                    arguments["form"], {"codename": "codexagent"}
                )
                body = (
                    "Your 12-digit verification code is in "
                    "https://example.test/init/guestbook/red, "
                    "https://example.test/init/guestbook/green, "
                    "https://example.test/init/guestbook/blue. "
                    "Assemble the code in this order: green, blue, red. "
                    "Then POST it to https://example.test/init/verify as field "
                    '"code".'
                )
            elif "/guestbook/" in url:
                fragment = {
                    "red": "3333",
                    "green": "1111",
                    "blue": "2222",
                }[url.rsplit("/", 1)[-1]]
                body = (
                    "<ul><li>“9999” — signed, someone</li>"
                    f"<li>“{fragment}” — signed, codexagent</li></ul>"
                )
            elif url.endswith("/verify"):
                self.assertEqual(arguments["form"], {"code": "111122223333"})
                body = (
                    "Verified. Final step: walk the rooms in this exact order: "
                    "saffron, cedar, iron, obsidian. Visit "
                    "https://example.test/init/room/&lt;name&gt; for each. "
                    "Every room request MUST carry the header "
                    "X-Pilgrim: &lt;your codename&gt;."
                )
            elif "/room/" in url:
                room = url.rsplit("/", 1)[-1]
                expected = ["saffron", "cedar", "iron", "obsidian"]
                room_calls = [
                    call
                    for call in initiation_calls
                    if "/room/" in str(call["url"])
                ]
                self.assertEqual(room, expected[len(room_calls) - 1])
                self.assertEqual(
                    arguments["headers"], {"X-Pilgrim": "codexagent"}
                )
                body = (
                    "token: INIT-ABC123"
                    if room == "obsidian"
                    else f"The {room} room accepts you."
                )
            else:
                return json.dumps({"ok": False, "error": "unexpected URL"})
            artifact_id = runtime.artifacts.put(body)
            return json.dumps(
                {
                    "ok": True,
                    "status": 200,
                    "url": url,
                    "headers": {},
                    "body_artifact": artifact_id,
                }
            )

        with patch.object(
            TaskToolRuntime, "execute", autospec=True, side_effect=fake_initiation
        ):
            initiation, initiation_runtime = self.dispatch(
                title="Initiation",
                category="The Dark Web",
                answer_format="exact",
                prompt=(
                    "Complete the initiation at https://example.test/init/ "
                    "It is a multi-step, stateful ritual. Every rule is stated "
                    "in the responses; follow ALL of them exactly. The final "
                    "token starts with INIT-."
                ),
                files={},
            )
        self.assert_solved(
            initiation,
            initiation_runtime,
            "initiation",
            "INIT-ABC123",
        )
        self.assertEqual(
            {call["session"] for call in initiation_calls},
            {"fast-initiation"},
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

        feasible_pow, _ = self.dispatch(
            title="Congruential Cascade",
            category="Heavy Compute",
            answer_format="validator",
            prompt=(
                "Find an ASCII string that begins with 'TEST-' whose SHA-256 "
                "digest begins with 32 zero BITS. If it is beyond compute, "
                "answer IMPOSSIBLE with one code:\n"
                "PARITY parity rules out every candidate\n"
                "MAGNITUDE target is outside the range\n"
                "ONETIMEPAD a one-time pad makes plaintext equally likely\n"
                "WORKFACTOR a solution exists, but the expected work is far "
                "beyond this event\n"
                "A wrong verdict is penalized."
            ),
            files={},
        )
        self.assertEqual(feasible_pow.status, FastPathStatus.INCONCLUSIVE)
        self.assertFalse(feasible_pow.answer)

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

    def test_registered_title_outranks_higher_default_solve_priority(self) -> None:
        class Game:
            BASE = "https://example.test"
            MODEL = "unused"

            @staticmethod
            def log(*_values) -> None:
                return None

        controller = AgentController(Game(), AgentConfig(model_workers=1))
        ordinary = TileRecord(Tile("T-1", "Other", 200, "Ordinary"))
        deterministic = TileRecord(
            Tile("T-2", "Needle in the Haystack", 500, "Dirty Ledger")
        )
        selected = controller._select_records([ordinary, deterministic], 1)
        self.assertEqual(selected[0].tile.id, "T-2")
        self.assertTrue(is_registered_fast_path_title("Dirty Ledger"))
        self.assertFalse(is_registered_fast_path_title("Ordinary"))

    def test_scheduler_caps_cpu_heavy_fast_paths(self) -> None:
        class Game:
            BASE = "https://example.test"
            MODEL = "unused"

            @staticmethod
            def log(*_values) -> None:
                return None

        controller = AgentController(
            Game(),
            AgentConfig(model_workers=4, cpu_heavy_workers=1),
        )
        records = [
            TileRecord(
                Tile("H3", "Heavy Compute", 300, "Proof of Work")
            ),
            TileRecord(
                Tile("H4", "Heavy Compute", 400, "Needle in 2^44")
            ),
            TileRecord(
                Tile("N1", "Needle in the Haystack", 100, "Dirty Ledger")
            ),
            TileRecord(Tile("O1", "Other", 200, "Ordinary")),
        ]
        selected = controller._select_records(records, 4)
        heavy = [
            record
            for record in selected
            if is_cpu_heavy_fast_path_title(record.tile.title)
        ]
        self.assertEqual(len(heavy), 1)
        self.assertIn("N1", {record.tile.id for record in selected})

    def test_generator_fingerprint_ignores_instance_values(self) -> None:
        first, _ = FastPathFixtureTests.dispatch(
            self,
            title="Event Horizon",
            category="Needle in the Haystack",
            answer_format="exact_ci",
            prompt=(
                "Count events of type 'purchase' from 2026-01-01 through "
                "2026-01-31 inclusive and return the unique winner among 50 users."
            ),
            files={
                "events-001.jsonl": (
                    '{"user":"a","type":"purchase","ts":"2026-01-02T00:00:00Z"}\n'
                )
            },
        )
        second, _ = FastPathFixtureTests.dispatch(
            self,
            title="Event Horizon",
            category="Needle in the Haystack",
            answer_format="exact_ci",
            prompt=(
                "Count events of type 'refund' from 2026-02-01 through "
                "2026-02-28 inclusive and return the unique winner among 75 users."
            ),
            files={
                "events-002.jsonl": (
                    '{"user":"b","type":"refund","ts":"2026-02-03T00:00:00Z"}\n'
                )
            },
        )

        self.assertEqual(
            first.fingerprint.generator_fingerprint,
            second.fingerprint.generator_fingerprint,
        )

    def test_generator_fingerprint_rejects_structural_near_matches(self) -> None:
        baseline, _ = FastPathFixtureTests.dispatch(
            self,
            title="Hidden Pixels",
            category="Cryptic",
            answer_format="exact",
            prompt="Inspect the PNG chunks and return the unique token.",
            files={"image-01.dat": b"\x89PNG\r\n\x1a\n"},
        )
        wrong_magic, _ = FastPathFixtureTests.dispatch(
            self,
            title="Hidden Pixels",
            category="Cryptic",
            answer_format="exact",
            prompt="Inspect the PNG chunks and return the unique token.",
            files={"image-02.dat": "validator text"},
        )
        wrong_schema, _ = FastPathFixtureTests.dispatch(
            self,
            title="Hidden Pixels",
            category="Cryptic",
            answer_format="exact",
            prompt="Inspect the PNG chunks and return the unique token.",
            files={"image-03.dat": b"\x89PNG\r\n\x1a\n", "extra.csv": "other\n1\n"},
        )
        wrong_format, _ = FastPathFixtureTests.dispatch(
            self,
            title="Hidden Pixels",
            category="Cryptic",
            answer_format="numeric",
            prompt="Inspect the PNG chunks and return the unique token.",
            files={"image-04.dat": b"\x89PNG\r\n\x1a\n"},
        )
        fingerprint = baseline.fingerprint.generator_fingerprint
        self.assertNotEqual(
            fingerprint, wrong_magic.fingerprint.generator_fingerprint
        )
        self.assertNotEqual(
            fingerprint, wrong_schema.fingerprint.generator_fingerprint
        )
        self.assertNotEqual(
            fingerprint, wrong_format.fingerprint.generator_fingerprint
        )
