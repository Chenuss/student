"""
app_logging.py — единая настройка отладочного логирования приложения.

Управление через переменные окружения (см. .env.example):

  LOG_LEVEL          — уровень консоли: DEBUG / INFO / WARNING / ERROR (по умолчанию INFO)
  LOG_FILE           — путь к файлу логов; пусто = файл не пишется (по умолчанию logs/app.log)
  LOG_FILE_LEVEL     — уровень для файла, если нужно подробнее консоли (по умолчанию = LOG_LEVEL)
  LLM_DEBUG          — true: подробный лог обмена с ЛЛМ (промпты целиком, ответы, usage, время)
  LLM_LOG_CONTENT    — true: писать полный текст промптов/ответов (иначе — обрезки ~600 симв.)
  LLM_LOG_MAX_CHARS  — размер обрезки контента при LLM_LOG_CONTENT=false (по умолчанию 600)

Логи пишутся одновременно в консоль и (при наличии) в файл с ротацией.
Функция идемпотентна — повторные вызовы не добавляют дублирующиеся хендлеры.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

load_dotenv()

_CONFIGURED = False


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "д", "да")


def _parse_level(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip().upper()
    if not raw:
        return default
    level = getattr(logging, raw, None)
    if isinstance(level, int):
        return level
    return default


def setup_logging() -> logging.Logger:
    """Настраивает root-логгер: консоль + файл. Возвращает логгер 'ktp'."""
    global _CONFIGURED
    log = logging.getLogger("ktp")
    if _CONFIGURED:
        return log

    console_level = _parse_level("LOG_LEVEL", logging.INFO)
    file_level = _parse_level("LOG_FILE_LEVEL", console_level)
    log_file = (os.getenv("LOG_FILE") or "").strip()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    llm_debug = _env_flag("LLM_DEBUG", default=console_level <= logging.DEBUG)

    root = logging.getLogger()
    # Убираем хендлеры, добавленные где-либо ранее (например, basicConfig в main.py),
    # чтобы не было двойных записей.
    for h in list(root.handlers):
        root.removeHandler(h)

    # LLM_DEBUG=true должен работать даже при LOG_LEVEL=INFO: DEBUG-записи обмена
    # с моделью обязаны доходить хотя бы до одного хендлера (файла или консоли).
    if llm_debug:
        if log_file:
            # Есть файл — подробные промпты/ответы пишутся в него, консоль не засоряем.
            file_level = min(file_level, logging.DEBUG)
        else:
            # Файла нет — значит подробные DEBUG-логи должны видеться в консоли.
            console_level = min(console_level, logging.DEBUG)

    root.setLevel(min(console_level, file_level))

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file:
        try:
            dir_ = os.path.dirname(os.path.abspath(log_file))
            os.makedirs(dir_, exist_ok=True)
            fh = RotatingFileHandler(
                log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
            )
            fh.setLevel(file_level)
            fh.setFormatter(fmt)
            root.addHandler(fh)
            log.info("Файловый лог: %s (уровень %s)", os.path.abspath(log_file),
                      logging.getLevelName(file_level))
        except OSError as exc:
            log.warning("Не удалось открыть файл лога %r: %s — логи только в консоль", log_file, exc)

    # При DEBUG дополнительно включаем информацию от httpx/openai о сетевых запросах.
    if console_level <= logging.DEBUG or (log_file and file_level <= logging.DEBUG):
        logging.getLogger("httpx").setLevel(logging.INFO)

    log.debug(
        "Логирование инициализировано: console=%s, file=%s, LLM_DEBUG=%s, LLM_LOG_CONTENT=%s",
        logging.getLevelName(console_level),
        log_file or "<выключен>",
        llm_debug,
        _env_flag("LLM_LOG_CONTENT"),
    )
    _CONFIGURED = True
    return log


def llm_debug_enabled() -> bool:
    """LLM_DEBUG=true или общий LOG_LEVEL=DEBUG."""
    return _env_flag("LLM_DEBUG", default=False) or _parse_level("LOG_LEVEL", logging.INFO) <= logging.DEBUG


def truncate(text: str, prefix: str = "") -> str:
    """Возвращает строку для лога: полную (LLM_LOG_CONTENT=true) или обрезанную."""
    text = text or ""
    if _env_flag("LLM_LOG_CONTENT"):
        return f"{prefix}{text}" if prefix else text
    limit = int((os.getenv("LLM_LOG_MAX_CHARS") or "600").strip() or 600)
    if len(text) <= limit:
        return f"{prefix}{text}" if prefix else text
    tail = f"... [+{len(text) - limit} симв., всего {len(text)}]"
    body = text[:limit] + tail
    return f"{prefix}{body}" if prefix else body


# ---------------------------------------------------------------------------
# Журнал сырых HTTP-обменов с API ЛЛМ (LLM_HTTP_LOG=true)
# ---------------------------------------------------------------------------

def http_log_enabled() -> bool:
    """LLM_HTTP_LOG=true — писать точные JSON тела запросов/ответов в отдельный журнал."""
    return _env_flag("LLM_HTTP_LOG", default=False)


def http_logger():
    """Логгер 'ktp.llm.http' + отдельный файл журнала (по умолчанию logs/llm_http.log).

    Файл журнала создаётся независимо от LOG_FILE, чтобы обмен с моделью
    всегда можно было посмотреть целиком, не засоряя общий лог.
    Повторные вызовы не добавляют дублирующихся хендлеров.
    """
    log = logging.getLogger("ktp.llm.http")
    if getattr(log, "_ktp_http_ready", False):
        return log

    path = (os.getenv("LLM_HTTP_LOG_FILE") or "logs/llm_http.log").strip()
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        fh = RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except OSError as exc:
        log.warning("Не удалось открыть файл HTTP-журнала %r: %s", path, exc)

    # Копии записей — в общий лог (консоль/файл), если он настроен.
    if not logging.getLogger().handlers:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(fmt)
        log.addHandler(console)

    log.setLevel(logging.INFO)
    log.propagate = False  # без дублей через root-логгер
    log.info("HTTP-журнал обмена с ЛЛМ включён (LLM_HTTP_LOG=true): %s",
             os.path.abspath(path))
    log._ktp_http_ready = True
    return log


def redact_headers(headers) -> dict:
    """Словарь заголовков с маскированием авторизационных значений."""
    out = {}
    for k, v in dict(headers).items():
        if k.lower() in ("authorization", "x-api-key", "api-key"):
            out[k] = (v[:10] + "...***") if len(v) > 10 else "***"
        else:
            out[k] = v
    return out
