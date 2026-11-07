from __future__ import annotations

import re
import unicodedata
from typing import Any

IDENTITY_TYPES = ("هوية وطنية", "إقامة", "جواز سفر")


def _normalized_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = "".join(
        ch for ch in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(ch)
    )
    text = (
        text.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ة", "ه").replace("ى", "ي")
    )
    return re.sub(r"[^\w\u0600-\u06ff]+", "", text, flags=re.UNICODE)


_SAUDI_VALUES = {
    "سعودي",
    "سعوديه",
    "السعودي",
    "السعوديه",
    "المملكهالعربيهالسعوديه",
    "saudi",
    "saudiarabia",
    "saudiarabian",
    "ksa",
}


def is_saudi_nationality(value: Any) -> bool:
    """يعيد True للجنسية السعودية بصيغها العربية والإنجليزية الشائعة."""
    return _normalized_text(value) in _SAUDI_VALUES


def identity_type_for_nationality(value: Any) -> str:
    """السعودي: هوية وطنية، وأي جنسية أخرى: إقامة."""
    return "هوية وطنية" if is_saudi_nationality(value) else "إقامة"


def validate_identity_type(nationality: Any, identity_type: Any = "") -> str:
    """Validate a selected identity type while permitting passport records."""
    nationality_text = str(nationality or "").strip()
    supplied = str(identity_type or "").strip()
    if supplied and supplied not in IDENTITY_TYPES:
        raise ValueError("نوع الوثيقة يجب أن يكون هوية وطنية أو إقامة أو جواز سفر.")
    if supplied == "جواز سفر":
        return supplied
    if not nationality_text:
        if supplied:
            return supplied
        raise ValueError("الجنسية أو نوع الوثيقة مطلوب لتحديد الهوية.")
    expected = identity_type_for_nationality(nationality_text)
    if supplied and supplied != expected:
        if expected == "هوية وطنية":
            raise ValueError("الموظف السعودي يجب أن تكون وثيقته هوية وطنية.")
        raise ValueError("الموظف غير السعودي يجب أن تكون وثيقته إقامة.")
    return expected


def identity_number_label(identity_type: Any) -> str:
    identity_type = str(identity_type).strip()
    if identity_type == "جواز سفر":
        return "رقم جواز السفر"
    return "رقم الإقامة" if identity_type == "إقامة" else "رقم الهوية الوطنية"


def identity_expiry_label(identity_type: Any = "") -> str:
    """اسم موحّد للحقل يشمل الهوية الوطنية والإقامة."""
    identity_type = str(identity_type).strip()
    if identity_type == "جواز سفر":
        return "تاريخ انتهاء جواز السفر"
    return "تاريخ انتهاء الإقامة" if identity_type == "إقامة" else "تاريخ انتهاء الهوية الوطنية"
