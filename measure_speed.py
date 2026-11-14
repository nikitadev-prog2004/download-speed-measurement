#!/usr/bin/env python3
"""Замер скорости скачивания: последовательные GET-запросы к одному адресу.

Зависимостей нет, достаточно стандартной библиотеки Python 3.9+.
"""

from __future__ import annotations

import argparse
import http.client
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import NoReturn

DEFAULT_REQUESTS = 10
DEFAULT_TIMEOUT_S = 60.0
CHUNK_SIZE = 64 * 1024
# 429 и 503 — сервер просит повторить позже. Чужие коды (404, 500) не повторяем.
RETRYABLE_STATUS = frozenset({429, 503})
MAX_ATTEMPTS_PER_REQUEST = 4
# Короткий Retry-After вроде 1 с не снимает ограничение, а частые повторы
# приводят к долгому бану. Ниже этой паузы не опускаемся.
MIN_THROTTLE_DELAY_S = 5.0
# Википедия в жёстком лимите присылает Retry-After: 600. Ждём его целиком.
MAX_RETRY_DELAY_S = 600.0
# После ограничения не начинаем следующий файл сразу.
GAP_AFTER_THROTTLE_S = 5.0
USER_AGENT = "measure-speed/1.0 (sequential download speed test)"

KIB = 1024
MIB = 1024 * 1024
GIB = 1024 * MIB
# Десятичный мегабит — так скорость указывают провайдеры: 1 Мбит/с = 10^6 бит/с.
MEGABIT = 1_000_000

TIME_WIDTH = 10
SIZE_WIDTH = 12
SPEED_WIDTH = 14


@dataclass(frozen=True)
class Download:
    """Один успешно дочитанный до конца ответ."""

    elapsed_s: float
    size_bytes: int


def configure_stdio() -> None:
    """Печатаем UTF-8, чтобы единицы «МиБ» и русский текст не ломались в консоли Windows."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8")
        except (OSError, ValueError):
            pass


def fail(message: str) -> NoReturn:
    print(f"Ошибка: {message}", file=sys.stderr)
    raise SystemExit(1)


def format_size(num_bytes: int) -> str:
    if num_bytes < KIB:
        return f"{num_bytes} Б"
    if num_bytes < MIB:
        return f"{num_bytes / KIB:.1f} КиБ"
    if num_bytes < GIB:
        return f"{num_bytes / MIB:.2f} МиБ"
    return f"{num_bytes / GIB:.2f} ГиБ"


def format_byte_count(num_bytes: int) -> str:
    grouped = f"{num_bytes:,}".replace(",", " ")
    n = abs(num_bytes) % 100
    n1 = n % 10
    if 11 <= n <= 14:
        word = "байтов"
    elif n1 == 1:
        word = "байт"
    elif 2 <= n1 <= 4:
        word = "байта"
    else:
        word = "байтов"
    return f"{grouped} {word}"


def megabits_per_second(num_bytes: int, elapsed_s: float) -> float:
    if elapsed_s <= 0:
        return 0.0
    return (num_bytes * 8) / elapsed_s / MEGABIT


def mebibytes_per_second(num_bytes: int, elapsed_s: float) -> float:
    if elapsed_s <= 0:
        return 0.0
    return (num_bytes / MIB) / elapsed_s


def format_speed(num_bytes: int, elapsed_s: float) -> str:
    return f"{megabits_per_second(num_bytes, elapsed_s):.2f} Мбит/с"


def format_pause(delay_s: float) -> str:
    text = f"{delay_s:.1f}".rstrip("0").rstrip(".")
    if delay_s >= 60:
        minutes = f"{delay_s / 60:.1f}".rstrip("0").rstrip(".")
        return f"{text} с ({minutes} мин)"
    return f"{text} с"


def retry_delay_s(retry_after: str | None, attempt: int) -> float:
    """Сколько ждать перед повтором.

    Большой Retry-After соблюдаем как есть. Слишком маленький увеличиваем:
    серия повторов через 1 с приводит к бану на много минут.
    """
    parsed = _parse_retry_after(retry_after)
    floor = MIN_THROTTLE_DELAY_S * attempt
    if parsed is None:
        delay = floor
    elif parsed >= MIN_THROTTLE_DELAY_S:
        delay = parsed
    else:
        delay = max(parsed, floor)
    return min(max(delay, 0.0), MAX_RETRY_DELAY_S)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    return moment.timestamp() - time.time()


def release_http_error(exc: urllib.error.HTTPError) -> None:
    try:
        exc.read(CHUNK_SIZE)
    except OSError:
        pass
    try:
        exc.close()
    except OSError:
        pass


def table_header() -> str:
    return (
        f"{'№':>2}  {'время':>{TIME_WIDTH}}  "
        f"{'объём':>{SIZE_WIDTH}}  {'скорость':>{SPEED_WIDTH}}"
    )


def format_row(index: int, item: Download) -> str:
    elapsed = f"{item.elapsed_s:.3f} с"
    return (
        f"{index:2d}  {elapsed:>{TIME_WIDTH}}  "
        f"{format_size(item.size_bytes):>{SIZE_WIDTH}}  "
        f"{format_speed(item.size_bytes, item.elapsed_s):>{SPEED_WIDTH}}"
    )


def download(url: str, timeout_s: float) -> Download:
    """Скачивает тело ответа целиком и возвращает время и число полученных байт.

    Таймер включает DNS, соединение, TLS, заголовки и чтение тела до EOF.
    Закрытие сокета в замер не входит. В память кладётся только текущий кусок.
    """
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
        method="GET",
    )
    started = time.perf_counter()
    size = 0
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        while True:
            chunk = response.read(CHUNK_SIZE)
            if not chunk:
                break
            size += len(chunk)
        elapsed_s = time.perf_counter() - started
    return Download(elapsed_s=elapsed_s, size_bytes=size)


def measure_one(url: str, timeout_s: float, index: int, count: int) -> tuple[Download, bool]:
    """Один успешный ответ. На 429 и 503 ждёт и повторяет тот же номер запроса.

    В замер попадает только удачная попытка: пауза и отклонённый ответ не считаются.
    Второй результат — был ли повтор из-за ограничения частоты.
    """
    retried = False
    for attempt in range(1, MAX_ATTEMPTS_PER_REQUEST + 1):
        try:
            return download(url, timeout_s), retried
        except urllib.error.HTTPError as exc:
            code = exc.code
            reason = exc.reason
            retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
            can_retry = code in RETRYABLE_STATUS and attempt < MAX_ATTEMPTS_PER_REQUEST
            release_http_error(exc)
            if can_retry:
                delay = retry_delay_s(retry_after, attempt)
                if retried and delay >= 60:
                    fail(
                        f"запрос {index}/{count}: HTTP {code} {reason}. "
                        f"Сервер снова просит подождать {format_pause(delay)}"
                    )
                retried = True
                print(
                    f"запрос {index}/{count}: HTTP {code} {reason}, повтор через {format_pause(delay)}",
                    flush=True,
                )
                time.sleep(delay)
                continue
            fail(f"запрос {index}/{count}: HTTP {code} {reason}")
        except TimeoutError:
            fail(f"запрос {index}/{count}: превышен таймаут {timeout_s:g} с")
        except urllib.error.URLError as exc:
            url_reason = exc.reason
            if isinstance(url_reason, TimeoutError):
                fail(f"запрос {index}/{count}: превышен таймаут {timeout_s:g} с")
            fail(f"запрос {index}/{count}: {url_reason}")
        except (http.client.HTTPException, OSError) as exc:
            fail(f"запрос {index}/{count}: {exc}")
    fail(f"запрос {index}/{count}: не удалось получить ответ")


def measure(url: str, count: int, timeout_s: float) -> list[Download]:
    print(f"Адрес: {url}")
    print(f"Запросов: {count}", flush=True)

    results: list[Download] = []
    slow_down = False
    for index in range(1, count + 1):
        if slow_down:
            print(
                f"пауза {format_pause(GAP_AFTER_THROTTLE_S)} перед запросом {index}/{count}",
                flush=True,
            )
            time.sleep(GAP_AFTER_THROTTLE_S)
        result, retried = measure_one(url, timeout_s, index, count)
        slow_down = slow_down or retried
        results.append(result)
        if index == 1:
            print()
            print(table_header(), flush=True)
        print(format_row(index, result), flush=True)
    return results


def print_summary(results: list[Download]) -> None:
    total_bytes = sum(item.size_bytes for item in results)
    total_elapsed = sum(item.elapsed_s for item in results)
    average_elapsed = total_elapsed / len(results)
    speed_mbit = megabits_per_second(total_bytes, total_elapsed)
    speed_mib = mebibytes_per_second(total_bytes, total_elapsed)

    print()
    summary = (
        ("Среднее время запроса", f"{average_elapsed:.3f} с"),
        ("Общий объём", f"{format_size(total_bytes)} ({format_byte_count(total_bytes)})"),
        ("Общее время", f"{total_elapsed:.3f} с"),
        ("Скорость скачивания", f"{speed_mbit:.2f} Мбит/с ({speed_mib:.2f} МиБ/с)"),
    )
    label_width = max(len(label) for label, _ in summary)
    for label, value in summary:
        print(f"{label:<{label_width}}  {value}")

    if total_bytes == 0:
        print("Тело ответа пустое: объём и скорость равны нулю.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Делает несколько GET-запросов к адресу строго по очереди, "
            "дожидается полного ответа на каждый и печатает скорость скачивания."
        )
    )
    parser.add_argument("url", help="адрес ресурса, например тяжёлой картинки")
    parser.add_argument(
        "-n",
        "--requests",
        type=int,
        default=DEFAULT_REQUESTS,
        help=f"число последовательных запросов (по умолчанию {DEFAULT_REQUESTS})",
    )
    parser.add_argument(
        "-t",
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help=(
            "таймаут сокета на соединение и каждую операцию чтения, секунды "
            f"(по умолчанию {DEFAULT_TIMEOUT_S:g})"
        ),
    )
    args = parser.parse_args(argv)
    if args.requests < 1:
        parser.error("число запросов должно быть не меньше 1")
    if not args.timeout > 0:
        parser.error("таймаут должен быть больше нуля")

    parsed = urllib.parse.urlparse(args.url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parser.error("нужен абсолютный адрес со схемой http:// или https://")
    return args


def main(argv: list[str] | None = None) -> None:
    configure_stdio()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        results = measure(args.url, args.requests, args.timeout)
    except KeyboardInterrupt:
        fail("измерение прервано")
    print_summary(results)


if __name__ == "__main__":
    main()
