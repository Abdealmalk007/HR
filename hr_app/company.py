from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from .config import COMPANY_NAME


COMPANY_PROFILE_FIELDS = (
    "company_name",
    "legal_name",
    "entity_type",
    "registration_number",
    "unified_number",
    "tax_number",
    "activity",
    "address",
    "city",
    "postal_code",
    "country",
    "phone",
    "email",
    "website",
)

COMPANY_PROFILE_LABELS = {
    "company_name": "اسم الشركة المخصص",
    "legal_name": "الاسم القانوني",
    "entity_type": "نوع الكيان",
    "registration_number": "رقم السجل التجاري",
    "unified_number": "الرقم الموحد",
    "tax_number": "الرقم الضريبي",
    "activity": "النشاط",
    "address": "العنوان الوطني",
    "city": "المدينة",
    "postal_code": "الرمز البريدي",
    "country": "الدولة",
    "phone": "الهاتف",
    "email": "البريد الإلكتروني",
    "website": "الموقع الإلكتروني",
}

COMPANY_SETTING_PREFIX = "company."
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_PHONE_RE = re.compile(r"^[0-9+()\- .]*$")
_MAX_LENGTHS = {
    "company_name": 160,
    "legal_name": 200,
    "entity_type": 100,
    "registration_number": 80,
    "unified_number": 80,
    "tax_number": 80,
    "activity": 200,
    "address": 300,
    "city": 100,
    "postal_code": 30,
    "country": 100,
    "phone": 40,
    "email": 160,
    "website": 240,
}


def normalize_company_profile(
    data: dict[str, Any] | None,
    *,
    include_defaults: bool = True,
) -> dict[str, str]:
    source = data or {}
    profile: dict[str, str] = {}
    for key in COMPANY_PROFILE_FIELDS:
        value = str(source.get(key) or "").strip()
        if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
            raise ValueError(f"{COMPANY_PROFILE_LABELS[key]} يحتوي أحرف تحكم غير مسموحة.")
        if len(value) > _MAX_LENGTHS[key]:
            raise ValueError(
                f"{COMPANY_PROFILE_LABELS[key]} يتجاوز الحد المسموح ({_MAX_LENGTHS[key]} حرفًا)."
            )
        profile[key] = value

    email = profile["email"]
    if email and not _EMAIL_RE.fullmatch(email):
        raise ValueError("البريد الإلكتروني للشركة غير صالح.")

    phone = profile["phone"]
    if phone and not _PHONE_RE.fullmatch(phone):
        raise ValueError("هاتف الشركة يحتوي أحرفًا غير صالحة.")

    website = profile["website"]
    if website:
        parsed = urlsplit(website)
        if (
            parsed.scheme.casefold() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("موقع الشركة يجب أن يكون رابط HTTP أو HTTPS صالحًا دون بيانات دخول.")

    if include_defaults and not profile["company_name"]:
        profile["company_name"] = COMPANY_NAME
    return profile


def company_profile_rows(profile: dict[str, Any] | None) -> list[dict[str, str]]:
    normalized = normalize_company_profile(profile)
    return [
        {"field": COMPANY_PROFILE_LABELS[key], "value": normalized.get(key, "")}
        for key in COMPANY_PROFILE_FIELDS
    ]


def company_details(profile: dict[str, Any] | None) -> str:
    normalized = normalize_company_profile(profile)
    parts: list[str] = []
    for key in (
        "legal_name",
        "entity_type",
        "registration_number",
        "unified_number",
        "tax_number",
        "activity",
    ):
        if normalized.get(key):
            parts.append(f"{COMPANY_PROFILE_LABELS[key]}: {normalized[key]}")
    location = "، ".join(
        value for value in (
            normalized.get("address", ""), normalized.get("city", ""),
            normalized.get("postal_code", ""), normalized.get("country", ""),
        ) if value
    )
    if location:
        parts.append(location)
    parts.extend(
        value for value in (
            normalized.get("phone", ""), normalized.get("email", ""), normalized.get("website", ""),
        ) if value
    )
    return " | ".join(parts)
