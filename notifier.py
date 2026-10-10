"""
notifier.py — отправка email-уведомлений о завершении генерации.

Использует aiosmtplib (асинхронный SMTP). Все исключения перехватываются:
падение почты НЕ должно ломать сохранение материала в БД.
"""

from __future__ import annotations

import os
import re
from email.message import EmailMessage

import aiosmtplib

SMTP_ENABLED = os.getenv("SMTP_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "465") or 465)
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "").strip()
SMTP_USE_SSL = os.getenv("SMTP_USE_SSL", "true").strip().lower() in ("1", "true", "yes", "on")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER) or "no-reply@localhost"


def smtp_configured() -> bool:
    """SMTP реально настроен и включён?"""
    return bool(SMTP_ENABLED and SMTP_HOST)


def _markdown_to_plain(md: str) -> str:
    """Грубое превращение Markdown в читаемый текст для письма."""
    text = md
    text = re.sub(r"```mermaid[\s\S]*?```", "[схема — см. веб-приложение]", text)
    text = re.sub(r"```[\s\S]*?```", "[код/схема]", text)
    text = re.sub(r"\$\$([\s\S]*?)\$\$", r"\1", text)
    text = re.sub(r"\$([^$\n]+)\$", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"(\*\*|\*|_)(.+?)\1", r"\2", text, flags=re.DOTALL)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return text.strip()


async def send_completion_email(
    to_email: str,
    lesson_title: str,
    content_markdown: str,
) -> dict:
    """
    Отправляет письмо с готовым материалом преподавателю.

    Возвращает словарь-результат: {"sent": bool, "reason": str}.
    Исключения не пробрасываются наружу — только фиксируются в результате.
    """
    if not smtp_configured():
        return {"sent": False, "reason": "SMTP выключен или не настроен (SMTP_ENABLED/SMTP_HOST)"}

    if not to_email or "@" not in to_email:
        return {"sent": False, "reason": f"Некорректный email получателя: {to_email!r}"}

    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    msg["Subject"] = f"[КТП] Материалы готовы: {lesson_title}"
    msg.set_content(_markdown_to_plain(content_markdown))
    # Приложим исходный Markdown файлом
    msg.add_attachment(
        content_markdown.encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename="material.md",
    )

    try:
        await aiosmtplib.send(
            msg,
            hostname=SMTP_HOST,
            port=SMTP_PORT,
            username=SMTP_USER or None,
            password=SMTP_PASSWORD or None,
            use_tls=SMTP_USE_SSL,
            timeout=20,
        )
        return {"sent": True, "reason": f"Письмо отправлено на {to_email}"}
    except Exception as exc:  # noqa: BLE001 — почта не должна ронять пайплайн
        return {"sent": False, "reason": f"Ошибка SMTP: {type(exc).__name__}: {exc}"}
