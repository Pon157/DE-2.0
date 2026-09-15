"""
Миграция: добавляем новые поля в child_bots и suggestions.

Запуск: python migrate_new_features.py
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from db.base import engine
from sqlalchemy import text


MIGRATIONS = [
    # Подтверждение бана
    "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS ban_confirm_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    # Доступ к /info: 'admins' | 'all'
    "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS info_access VARCHAR(16) NOT NULL DEFAULT 'admins'",
    # Анонимная предложка
    "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS anon_suggestion_enabled BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS anon_suggestion_ask_text TEXT NOT NULL DEFAULT 'Как хотите отправить предложку?'",
    "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS anon_suggestion_yes_text VARCHAR(64) NOT NULL DEFAULT '🕵️ Анонимно'",
    "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS anon_suggestion_no_text VARCHAR(64) NOT NULL DEFAULT '👤 От моего имени'",
    # Тексты уведомлений предложки
    "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS suggestion_approved_text TEXT NOT NULL DEFAULT '🎉 Ваш пост опубликован!'",
    "ALTER TABLE child_bots ADD COLUMN IF NOT EXISTS suggestion_rejected_text TEXT NOT NULL DEFAULT '❌ Ваш пост отклонён.'",
    # Флаг анонимности в таблице предложок
    "ALTER TABLE suggestions ADD COLUMN IF NOT EXISTS is_anonymous BOOLEAN NOT NULL DEFAULT FALSE",
]


async def main():
    async with engine.begin() as conn:
        for sql in MIGRATIONS:
            print(f"  Running: {sql[:80]}...")
            await conn.execute(text(sql))
    print("✅ Миграция выполнена успешно.")


if __name__ == "__main__":
    asyncio.run(main())
