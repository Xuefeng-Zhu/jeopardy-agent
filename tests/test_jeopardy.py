from __future__ import annotations

import gzip
import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from threading import Event
from unittest.mock import patch

from agent_types import CancellationToken, SolveCancelled


class FakeResponse:
    status_code = 200
    ok = True
    text = ""
    encoding = "utf-8"

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.headers = {"content-length": str(sum(map(len, chunks)))}
        self.closed = False

    def iter_content(self, chunk_size: int):
        del chunk_size
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class JeopardyTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        sys.modules.pop("jeopardy", None)
        with patch.dict(
            os.environ,
            {
                "JEOPARDY_BASE_URL": "https://example.test",
                "TEAM_API_KEY": "test-key",
            },
            clear=False,
        ):
            self.jeopardy = importlib.import_module("jeopardy")

    def tearDown(self) -> None:
        sys.modules.pop("jeopardy", None)

    def test_stale_nonempty_download_is_replaced_atomically_then_cached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "payload.bin")
            target.write_bytes(b"stale")
            response = FakeResponse([b"fresh-", b"payload"])
            calls: list[str] = []

            def get(url: str, **_kwargs):
                calls.append(url)
                return response

            self.jeopardy._s.get = get
            detail = {"files": ["payload.bin"]}
            self.jeopardy.fetch_files("T-1", detail, directory)
            self.jeopardy.fetch_files("T-1", detail, directory)

            self.assertEqual(target.read_bytes(), b"fresh-payload")
            self.assertEqual(len(calls), 1)
            target.write_bytes(b"")
            self.jeopardy.fetch_files("T-1", detail, directory)
            self.assertEqual(target.read_bytes(), b"fresh-payload")
            self.assertEqual(len(calls), 2)
            self.assertTrue(response.closed)
            self.assertEqual(list(Path(directory).glob(".payload.bin.*")), [])

    def test_cancelled_download_keeps_existing_file_and_removes_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory, "payload.bin")
            target.write_bytes(b"known-old")
            token = CancellationToken()
            response = FakeResponse([b"partial", b"rest"])

            def chunks(chunk_size: int):
                del chunk_size
                yield b"partial"
                token.cancel("claimed elsewhere")
                yield b"rest"

            response.iter_content = chunks
            self.jeopardy._s.get = lambda *_args, **_kwargs: response

            with self.assertRaises(SolveCancelled):
                self.jeopardy.fetch_files(
                    "T-2",
                    {"files": ["payload.bin"]},
                    directory,
                    cancellation=token,
                )

            self.assertEqual(target.read_bytes(), b"known-old")
            self.assertEqual(list(Path(directory).glob(".payload.bin.*")), [])
            self.assertTrue(response.closed)

    def test_encoded_content_length_does_not_reject_decoded_body(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            decoded = b"decoded payload" * 200
            encoded = gzip.compress(decoded)
            response = FakeResponse([decoded])
            response.headers = {
                "content-length": str(len(encoded)),
                "content-encoding": "gzip",
            }
            request_headers: list[dict[str, str]] = []

            def get(_url: str, **kwargs):
                request_headers.append(dict(kwargs.get("headers") or {}))
                return response

            self.jeopardy._s.get = get
            self.jeopardy.fetch_files(
                "T-gzip", {"files": ["payload.bin"]}, directory
            )

            self.assertEqual(Path(directory, "payload.bin").read_bytes(), decoded)
            self.assertEqual(request_headers, [{"Accept-Encoding": "identity"}])
            self.assertTrue(response.closed)

    def test_task_cancellation_closes_stalled_response(self) -> None:
        started = Event()
        released = Event()
        token = CancellationToken()

        class Response(FakeResponse):
            def __init__(self) -> None:
                super().__init__([])
                self.headers = {"content-type": "application/json"}

            def json(self) -> dict:
                started.set()
                released.wait(10)
                return {"prompt": "late"}

            def close(self) -> None:
                super().close()
                released.set()

        response = Response()
        self.jeopardy._s.get = lambda *_args, **_kwargs: response

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.jeopardy.task, "T-3", token)
            self.assertTrue(started.wait(timeout=1))
            token.cancel("claimed elsewhere")
            with self.assertRaises(SolveCancelled):
                future.result(timeout=1)
        self.assertTrue(response.closed)
