"""Проверки замера: полный ответ, строгая очередь, формулы и ошибки ввода."""

from __future__ import annotations

import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from unittest.mock import patch

import measure_speed


def _urlopen_without_proxy(opener):
    def urlopen(request, timeout=None, *, context=None):
        del context
        return opener.open(request, timeout=timeout)

    return urlopen


class LocalServer:
    def __init__(
        self,
        body: bytes,
        *,
        fail_on: int | None = None,
        delay_s: float = 0.05,
        rate_limit_first: int = 0,
        always_rate_limit: bool = False,
        retry_after: str = "0",
    ) -> None:
        self.body = body
        self.fail_on = fail_on
        self.delay_s = delay_s
        self.rate_limit_first = rate_limit_first
        self.always_rate_limit = always_rate_limit
        self.retry_after = retry_after
        self.intervals: list[tuple[float, float, int]] = []
        self._lock = threading.Lock()
        self._calls = 0

        handler = self._handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/payload"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                with owner._lock:
                    owner._calls += 1
                    number = owner._calls
                started = time.perf_counter()
                try:
                    if owner.always_rate_limit or number <= owner.rate_limit_first:
                        body = b"Too many requests"
                        self.send_response(429, "Too many requests")
                        self.send_header("Retry-After", owner.retry_after)
                        self.send_header("Content-Length", str(len(body)))
                        self.send_header("Content-Type", "text/plain")
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    if owner.fail_on is not None and number == owner.fail_on:
                        self.send_error(500, "fail")
                        return
                    if owner.delay_s > 0:
                        time.sleep(owner.delay_s)
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(owner.body)))
                    self.send_header("Content-Type", "application/octet-stream")
                    self.end_headers()
                    self.wfile.write(owner.body)
                    self.wfile.flush()
                finally:
                    with owner._lock:
                        owner.intervals.append((started, time.perf_counter(), number))

            def log_message(self, fmt: str, *args: object) -> None:
                del fmt, args

        return Handler

    def __enter__(self) -> LocalServer:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()


class MeasureSpeedTest(unittest.TestCase):
    def test_units_and_speed_formula(self) -> None:
        self.assertEqual(measure_speed.format_byte_count(1), "1 байт")
        self.assertEqual(measure_speed.format_byte_count(2), "2 байта")
        self.assertEqual(measure_speed.format_byte_count(5), "5 байтов")
        self.assertEqual(measure_speed.format_byte_count(11), "11 байтов")
        self.assertEqual(measure_speed.format_byte_count(21), "21 байт")
        self.assertEqual(measure_speed.format_size(measure_speed.MIB), "1.00 МиБ")
        self.assertAlmostEqual(measure_speed.megabits_per_second(1_000_000, 1.0), 8.0)
        self.assertAlmostEqual(measure_speed.mebibytes_per_second(measure_speed.MIB, 2.0), 0.5)
        self.assertEqual(measure_speed.megabits_per_second(100, 0.0), 0.0)
        self.assertEqual(measure_speed.retry_delay_s("1", 1), 5.0)
        self.assertEqual(measure_speed.retry_delay_s("600", 1), 600.0)
        self.assertEqual(measure_speed.retry_delay_s("9999", 1), measure_speed.MAX_RETRY_DELAY_S)
        self.assertEqual(measure_speed.retry_delay_s(None, 1), measure_speed.MIN_THROTTLE_DELAY_S)
        self.assertEqual(measure_speed.retry_delay_s("нет", 2), 10.0)

    def test_default_is_ten_sequential_full_downloads(self) -> None:
        args = measure_speed.parse_args(["https://example.com/image.jpg"])
        self.assertEqual(args.requests, 10)

        # Больше одного CHUNK_SIZE: итог в байтах доказывает, что тело дочитано, а не первый кусок.
        body = b"x" * (measure_speed.CHUNK_SIZE + 4096)
        with LocalServer(body) as server:
            stdout = StringIO()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with (
                patch("sys.stdout", stdout),
                patch("urllib.request.urlopen", _urlopen_without_proxy(opener)),
            ):
                measure_speed.main([server.url, "-n", "3", "-t", "5"])

        self.assertEqual(len(server.intervals), 3)
        ordered = sorted(server.intervals, key=lambda item: item[2])
        for previous, current in zip(ordered, ordered[1:]):
            self.assertLessEqual(previous[1], current[0])

        text = stdout.getvalue()
        self.assertIn(measure_speed.format_byte_count(len(body) * 3), text)
        self.assertIn("Среднее время запроса", text)
        self.assertIn("Скорость скачивания", text)
        self.assertIn("Мбит/с", text)

    def test_retries_rate_limit_without_counting_the_pause(self) -> None:
        body = b"image-bytes"
        slept: list[float] = []
        with LocalServer(body, rate_limit_first=1, retry_after="1", delay_s=0) as server:
            stdout = StringIO()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with (
                patch("sys.stdout", stdout),
                patch("urllib.request.urlopen", _urlopen_without_proxy(opener)),
                patch("measure_speed.time.sleep", slept.append),
            ):
                measure_speed.main([server.url, "-n", "2", "-t", "5"])

        self.assertEqual(len(server.intervals), 3)
        self.assertEqual(slept, [5.0, 5.0])
        text = stdout.getvalue()
        self.assertIn("HTTP 429", text)
        self.assertIn("повтор через 5 с", text)
        self.assertIn("пауза 5 с перед запросом 2/2", text)
        self.assertIn(measure_speed.format_byte_count(len(body) * 2), text)
        self.assertIn("Скорость скачивания", text)

    def test_waits_full_long_retry_after_once(self) -> None:
        slept: list[float] = []
        with LocalServer(b"image-bytes", always_rate_limit=True, retry_after="600", delay_s=0) as server:
            stdout = StringIO()
            stderr = StringIO()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with (
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
                patch("urllib.request.urlopen", _urlopen_without_proxy(opener)),
                patch("measure_speed.time.sleep", slept.append),
            ):
                with self.assertRaises(SystemExit) as raised:
                    measure_speed.main([server.url, "-n", "2", "-t", "5"])

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(slept, [600.0])
        self.assertEqual(len(server.intervals), 2)
        self.assertIn("600 с (10 мин)", stdout.getvalue())
        self.assertIn("снова просит подождать", stderr.getvalue())

    def test_gives_up_when_rate_limit_persists(self) -> None:
        with LocalServer(b"image-bytes", always_rate_limit=True, retry_after="0", delay_s=0) as server:
            stdout = StringIO()
            stderr = StringIO()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with (
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
                patch("urllib.request.urlopen", _urlopen_without_proxy(opener)),
                patch("measure_speed.time.sleep", lambda delay: None),
            ):
                with self.assertRaises(SystemExit) as raised:
                    measure_speed.main([server.url, "-n", "4", "-t", "5"])

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(len(server.intervals), measure_speed.MAX_ATTEMPTS_PER_REQUEST)
        self.assertIn("HTTP 429", stderr.getvalue())
        self.assertNotIn("Скорость скачивания", stdout.getvalue())

    def test_stops_when_a_request_fails(self) -> None:
        with LocalServer(b"payload", fail_on=2, delay_s=0) as server:
            stdout = StringIO()
            stderr = StringIO()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with (
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
                patch("urllib.request.urlopen", _urlopen_without_proxy(opener)),
            ):
                with self.assertRaises(SystemExit) as raised:
                    measure_speed.main([server.url, "-n", "5", "-t", "5"])

        self.assertEqual(raised.exception.code, 1)
        self.assertEqual(len(server.intervals), 2)
        self.assertIn("HTTP 500", stderr.getvalue())

    def test_rejects_bad_input(self) -> None:
        for argv in (
            ["ftp://example.com/file"],
            ["example.com/file"],
            ["-n", "0", "https://example.com/a"],
            ["-t", "0", "https://example.com/a"],
        ):
            with self.subTest(argv=argv):
                with patch("sys.stderr", StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        measure_speed.parse_args(argv)
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
