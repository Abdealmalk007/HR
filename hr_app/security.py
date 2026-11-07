from __future__ import annotations

import hashlib
import hmac
import re
import secrets

PBKDF2_ITERATIONS = 310_000
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128
COMMON_PASSWORDS = {
    "1234567890",
    "password123",
    "admin12345",
    "qwerty12345",
    "protech123",
}


def validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(
            f"كلمة المرور يجب أن تتكون من {MIN_PASSWORD_LENGTH} أحرف على الأقل."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"كلمة المرور يجب ألا تتجاوز {MAX_PASSWORD_LENGTH} حرفًا."
        )
    if password.casefold() in COMMON_PASSWORDS:
        raise ValueError("كلمة المرور شائعة وسهلة التخمين؛ اختر كلمة أقوى.")
    if not any(ch.isalpha() for ch in password):
        raise ValueError("كلمة المرور يجب أن تحتوي حرفًا واحدًا على الأقل.")
    if not any(ch.isdigit() for ch in password):
        raise ValueError("كلمة المرور يجب أن تحتوي رقمًا واحدًا على الأقل.")


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    validate_password(password)
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return hmac.compare_digest(actual, expected)


def normalize_id_number(value: str) -> str:
    return "".join(ch for ch in value.strip() if ch.isdigit())


def normalize_identity_document_number(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def id_fingerprint(value: str) -> str:
    return hashlib.sha256(normalize_identity_document_number(value).encode("utf-8")).hexdigest()


def mask_id_number(value: str) -> str:
    cleaned = normalize_identity_document_number(value)
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    return "*" * (len(cleaned) - 4) + cleaned[-4:]


def normalize_bank_account(value: str) -> str:
    return re.sub(r"[\s-]+", "", str(value or "").strip()).upper()


def normalize_iban(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def validate_saudi_iban(value: str) -> str:
    iban = normalize_iban(value)
    if not iban:
        return ""
    if not re.fullmatch(r"SA\d{22}", iban):
        raise ValueError("رقم الآيبان السعودي يجب أن يبدأ بـ SA ويتكون من 24 خانة.")
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(ord(char) - 55) if char.isalpha() else char for char in rearranged)
    remainder = 0
    for char in numeric:
        remainder = (remainder * 10 + int(char)) % 97
    if remainder != 1:
        raise ValueError("رقم الآيبان غير صحيح وفق معيار IBAN.")
    return iban


def mask_bank_account(value: str) -> str:
    cleaned = normalize_bank_account(value)
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    return "*" * (len(cleaned) - 4) + cleaned[-4:]


def mask_iban(value: str) -> str:
    cleaned = normalize_iban(value)
    if len(cleaned) <= 6:
        return "*" * len(cleaned)
    return cleaned[:2] + "*" * (len(cleaned) - 6) + cleaned[-4:]
