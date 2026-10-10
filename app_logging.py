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

    root = logging.getLogger()
    # Убираем хендлеры, добавленные где-либо ранее (например, basicConfig в main.py),
    # чтобы не было двойных записей.
    for h in list(root.handlers):
        root.removeHandler(h)

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

    llm_debug = _env_flag("LLM_DEBUG", default=console_level <= logging.DEBUG)
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
