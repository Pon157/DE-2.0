"""Шифрование токенов дочерних ботов.

Fernet (симметричное AES-128-CBC + HMAC) — НЕ детерминированное шифрование:
одно и то же значение каждый раз даёт разный шифротекст (случайный IV). Это
хорошо для защиты данных в покое, но означает, что колонку `token` больше
НЕЛЬЗЯ сравнивать через `WHERE token = :value` — нужно расшифровать все
строки, чтобы найти совпадение, либо (правильный путь) хранить рядом
отдельный ДЕТЕРМИНИРОВАННЫЙ отпечаток (HMAC-SHA256) для поиска/уникальности,
а сам токен только шифровать. Так и сделано: см.
db/models.py::ChildBot.token (шифруется) и .token_fingerprint (для поиска).
"""
import hashlib
import hmac
import re
from cryptography.fernet import Fernet, InvalidToken
from config import TOKEN_ENCRYPTION_KEY

_fernet = Fernet(TOKEN_ENCRYPTION_KEY.encode()
                 if isinstance(TOKEN_ENCRYPTION_KEY, str) else TOKEN_ENCRYPTION_KEY)

_RAW_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


def encrypt_token(token: str) -> str:
    return _fernet.encrypt(token.encode()).decode()


def decrypt_token(value: str) -> str:
    return _fernet.decrypt(value.encode()).decode()


def token_fingerprint(token: str) -> str:
    """Детерминированный отпечаток токена (HMAC-SHA256, ключ — тот же
    TOKEN_ENCRYPTION_KEY) — используется ТОЛЬКО для проверки уникальности и
    поиска "не создан ли уже бот с таким токеном", саму строку токена из
    отпечатка восстановить нельзя."""
    key = TOKEN_ENCRYPTION_KEY.encode() if isinstance(TOKEN_ENCRYPTION_KEY, str) else TOKEN_ENCRYPTION_KEY
    return hmac.new(key, token.encode(), hashlib.sha256).hexdigest()


def looks_like_plaintext_token(value: str) -> bool:
    """True, если значение похоже на СЫРОЙ (незашифрованный) токен вида
    '123456789:AAABBBccc...' — используется скриптом перешифровки старых
    записей (см. scripts/reencrypt_tokens.py), чтобы отличить их от уже
    зашифрованных значений."""
    return bool(value) and bool(_RAW_TOKEN_RE.match(value))


def is_valid_ciphertext(value: str) -> bool:
    try:
        _fernet.decrypt(value.encode())
        return True
    except (InvalidToken, ValueError, Exception):
        return False
