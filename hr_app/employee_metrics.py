from __future__ import annotations

import calendar
from datetime import date, datetime


def parse_iso_date(value: str | date | datetime | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def service_duration_parts(join_date: str | date | datetime | None, as_of: date | None = None) -> tuple[int, int, int]:
    """Return complete years, months and days of service.

    The calculation is calendar-aware and does not approximate months as 30 days.
    Invalid or future joining dates return zeroes.
    """
    start = parse_iso_date(join_date)
    end = as_of or date.today()
    if start is None or start > end:
        return 0, 0, 0

    years = end.year - start.year
    months = end.month - start.month
    days = end.day - start.day

    if days < 0:
        previous_month = end.month - 1 or 12
        previous_year = end.year if end.month > 1 else end.year - 1
        days += calendar.monthrange(previous_year, previous_month)[1]
        months -= 1

    if months < 0:
        years -= 1
        months += 12

    return max(0, years), max(0, months), max(0, days)


def format_service_duration(join_date: str | date | datetime | None, as_of: date | None = None) -> str:
    years, months, days = service_duration_parts(join_date, as_of)
    return f"{years} سنة، {months} شهر، {days} يوم"



def identity_expiry_state(
    expiry_date: str | date | datetime | None,
    as_of: date | None = None,
    warning_days: int = 90,
) -> tuple[str, int | None, str]:
    """يعيد رمز الحالة، الأيام المتبقية، ووصفًا عربيًا موحدًا لتاريخ انتهاء الهوية."""
    expiry = parse_iso_date(expiry_date)
    today = as_of or date.today()
    if expiry is None:
        return "missing", None, "غير مسجلة"
    days_left = (expiry - today).days
    if days_left < 0:
        return "expired", days_left, f"منتهية منذ {abs(days_left)} يوم"
    if days_left == 0:
        return "today", 0, "تنتهي اليوم"
    if days_left <= 7:
        return "urgent", days_left, f"تنتهي خلال {days_left} يوم"
    if days_left <= 30:
        return "soon", days_left, f"تنتهي خلال {days_left} يومًا"
    if days_left <= max(30, int(warning_days)):
        return "warning", days_left, f"تنتهي خلال {days_left} يومًا"
    return "valid", days_left, f"سارية — متبقي {days_left} يومًا"


def format_identity_expiry_status(
    expiry_date: str | date | datetime | None,
    as_of: date | None = None,
) -> str:
    return identity_expiry_state(expiry_date, as_of)[2]

def normalize_rating(value: object) -> int:
    if value in (None, ""):
        return 0
    try:
        rating = int(float(str(value).strip()))
    except (TypeError, ValueError) as exc:
        raise ValueError("تقييم الموظف يجب أن يكون من صفر إلى خمس نجوم.") from exc
    if not 0 <= rating <= 5:
        raise ValueError("تقييم الموظف يجب أن يكون من صفر إلى خمس نجوم.")
    return rating


def rating_to_stars(value: object, *, include_score: bool = True) -> str:
    try:
        rating = normalize_rating(value)
    except ValueError:
        rating = 0
    stars = "★" * rating + "☆" * (5 - rating)
    if rating == 0:
        return f"{stars} — غير مقيّم" if include_score else stars
    return f"{stars} — {rating}/5" if include_score else stars


RATING_OPTIONS = tuple(["☆☆☆☆☆ — غير مقيّم"] + [rating_to_stars(i) for i in range(1, 6)])


def rating_from_label(label: str) -> int:
    text = str(label or "").strip()
    if "غير مقيّم" in text or not text:
        return 0
    for value in range(5, 0, -1):
        if f"{value}/5" in text:
            return value
    return normalize_rating(text)
