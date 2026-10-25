"""
migrate_prompts.py — перенос промптов из хардкода (llm_service.py / pptx_service.py) во внешние файлы.

Что делает:
  1. Читает PROMPTS_DIR (по умолчанию ./prompts).
  2. Для каждого промпта реестра prompt_loader создаёт файл .md/.txt с front-matter
     метаданными; тело файла — эталонный текст промпта (fallback-дефолт модуля
     prompt_loader, соответствующий текущему хардкоду в сервисах).
  3. Пишет prompts/registry.json — машиночитаемый реестр всех промптов.
  4. Проверяет, что загруженные через prompt_loader тексты совпадают с оригиналами
     из llm_service / pptx_service (контроль корректности переноса).
  5. Опционально (--record-db) сохраняет первую версию каждого промпта в таблицу
     БД prompt_versions (история изменений).

Запуск:
    python migrate_prompts.py                 # создать недостающие файлы + registry.json
    python migrate_prompts.py --force         # перезаписать существующие файлы эталоном
    python migrate_prompts.py --dry-run       # только показать, что будет сделано
    python migrate_prompts.py --record-db     # плюс записать v1 в БД (prompt_versions)

После миграции сервисы (llm_service.py, pptx_service.py) работают через prompt_loader:
при отсутствии файлов они автоматически используют те же самые встроенные дефолты,
поэтому миграция не обязательна для запуска приложения — она включает редактируемость
промптов без изменения кода.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import prompt_loader as pl  # noqa: E402


def _compare_with_originals() -> list[str]:
    """Сверка эталонных дефолтов с текстами, которые были в коде до миграции."""
    problems: list[str] = []
    try:
        import llm_service
        import pptx_service
    except Exception as exc:  # noqa: BLE001
        return [f"не удалось импортировать сервисы для сверки: {exc}"]

    pairs = [
        ("material.system", llm_service.SYSTEM_PROMPT),
        ("pptx.system", pptx_service.PPTX_SYSTEM_PROMPT),
        ("mock.material.template", llm_service.MOCK_TEMPLATE),
    ]
    for key, original in pairs:
        default = pl.REGISTRY[key].default
        if default.strip() != str(original).strip():
            problems.append(f"«{key}»: fallback-дефолт prompt_loader отличается от текста в коде")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description="Миграция промптов из кода в файлы prompts/")
    ap.add_argument("--force", action="store_true", help="перезаписать существующие файлы эталонными текстами")
    ap.add_argument("--dry-run", action="store_true", help="только показать план действий")
    ap.add_argument("--record-db", action="store_true", help="записать начальные версии в таблицу prompt_versions (БД)")
    args = ap.parse_args()

    print(f"Каталог промптов: {pl.PROMPTS_DIR}")
    print(f"Авто-перечитывание (mtime): {'включено' if pl.AUTOLOAD else 'выключено'}")

    problems = _compare_with_originals()
    if problems:
        print("\n⚠ Расхождения с исходным кодом:")
        for p in problems:
            print("  -", p)
    else:
        print("✔ Эталонные дефолты prompt_loader совпадают с текстами в llm_service/pptx_service")

    if args.dry_run:
        print("\n[dry-run] Было бы выполнено:")
        for key, meta in pl.REGISTRY.items():
            path = os.path.join(pl.PROMPTS_DIR, meta.file)
            state = "существует" if os.path.isfile(path) else "БУДЕТ СОЗДАН"
            print(f"  {key:24s} -> {path}  [{state}]")
        print(f"  registry.json          -> {os.path.join(pl.PROMPTS_DIR, 'registry.json')}")
        return 0

    result = pl.ensure_files_exist(force=args.force)
    reg_path = pl.write_registry_json()

    print("\nРезультат создания файлов:")
    for key, status in result.items():
        icon = {"created": "✔ создан", "exists": "• уже существует",
                "overwritten": "✔ перезаписан", "skipped_no_file": "— нет файла (только дефолт)"}.get(status, status)
        print(f"  {key:24s} {icon}")
    print(f"  registry.json          ✔ {reg_path}")

    # Проверка round-trip: loader должен вернуть ровно тот же текст, что и дефолт
    print("\nПроверка загрузки через prompt_loader:")
    ok = True
    for key in pl.REGISTRY:
        loaded = pl.get_prompt(key, strip=True)
        expected = pl.REGISTRY[key].default.strip()
        same = loaded == expected
        ok = ok and same
        info = pl.prompt_info(key)
        src = "файл" if (info.exists and not info.from_default) else "fallback-дефолт"
        print(f"  {key:24s} {'OK' if same else 'РАСХОЖДЕНИЕ'} ({len(loaded)} симв., источник: {src})")

    if args.record_db:
        try:
            from database import PromptVersionRecord, init_db
            from sqlalchemy.orm import Session

            from database import engine

            init_db()
            with Session(engine) as s:
                for key in pl.REGISTRY:
                    info = pl.prompt_info(key)
                    exists = (
                        s.query(PromptVersionRecord)
                        .filter(PromptVersionRecord.prompt_key == key)
                        .count()
                    )
                    if exists and not args.force:
                        continue
                    s.add(
                        PromptVersionRecord(
                            prompt_key=key,
                            version_num=(exists + 1),
                            content=info.content,
                            change_note="Начальная версия (миграция из кода migrate_prompts.py)",
                            author="migration-script",
                        )
                    )
                s.commit()
            print("\n✔ Начальные версии промптов записаны в БД (prompt_versions)")
        except Exception as exc:  # noqa: BLE001
            print(f"\n⚠ Не удалось записать версии в БД: {exc}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
