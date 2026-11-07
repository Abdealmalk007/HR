from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

from .excel_import import SheetData, normalize_header, read_employee_file

ALIASES = {
    "emp_code": ("رقم الموظف", "كود الموظف", "الرقم الوظيفي", "employee code", "emp code", "emp id"),
    "attendance_date": ("التاريخ", "تاريخ الحضور", "date", "attendance date", "day"),
    "status": ("الحالة", "حالة الحضور", "status", "attendance status"),
    "check_in_time": ("وقت الحضور", "دخول", "حضور", "check in", "check-in", "checkin"),
    "check_out_time": ("وقت الانصراف", "انصراف", "خروج", "check out", "check-out", "checkout"),
    "note": ("ملاحظة", "ملاحظات", "note", "notes", "comment"),
}
ALIAS_MAP = {normalize_header(alias): field for field, aliases in ALIASES.items() for alias in (field, *aliases)}
REQUIRED = {"emp_code", "attendance_date", "status"}


def _normalize_date(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    if " " in text:
        text = text.split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"تاريخ غير صالح: {text}")


def parse_attendance_sheet(sheet: SheetData) -> tuple[list[dict[str, Any]], list[str]]:
    header_index = -1
    mapping: dict[str, int] = {}
    for idx, row in enumerate(sheet.rows[:25]):
        candidate: dict[str, int] = {}
        for col, value in enumerate(row):
            field = ALIAS_MAP.get(normalize_header(value))
            if field and field not in candidate:
                candidate[field] = col
        if len(REQUIRED & set(candidate)) >= 2 and "emp_code" in candidate:
            header_index, mapping = idx, candidate
            break
    if header_index < 0 or not REQUIRED.issubset(mapping):
        missing = REQUIRED - set(mapping)
        raise ValueError("تعذر التعرف على أعمدة الحضور المطلوبة: " + ", ".join(sorted(missing)))

    records: list[dict[str, Any]] = []
    errors: list[str] = []
    for source_row, row in enumerate(sheet.rows[header_index + 1 :], start=header_index + 2):
        if not any(str(v or "").strip() for v in row):
            continue
        try:
            get = lambda field: row[mapping[field]] if mapping.get(field, -1) < len(row) else ""
            emp_code = str(get("emp_code") or "").strip().upper()
            status = str(get("status") or "").strip()
            record = {
                "emp_code": emp_code,
                "attendance_date": _normalize_date(get("attendance_date")),
                "status": status,
                "check_in_time": str(get("check_in_time") or "").strip()
                if "check_in_time" in mapping else "",
                "check_out_time": str(get("check_out_time") or "").strip()
                if "check_out_time" in mapping else "",
                "note": str(get("note") or "").strip() if "note" in mapping else "",
                "_source_row": source_row,
            }
            if not emp_code or not status:
                raise ValueError("رقم الموظف والحالة مطلوبان")
            records.append(record)
        except Exception as exc:
            errors.append(f"الصف {source_row}: {exc}")
    return records, errors


def read_attendance_file(path: str | Path) -> tuple[list[dict[str, Any]], list[str], str]:
    sheets = read_employee_file(path)
    if not sheets:
        raise ValueError("الملف لا يحتوي أوراقًا قابلة للقراءة.")
    best = None
    last_error = None
    for sheet in sheets:
        try:
            records, errors = parse_attendance_sheet(sheet)
            if best is None or len(records) > len(best[0]):
                best = (records, errors, sheet.name)
        except Exception as exc:
            last_error = exc
    if best is None:
        raise ValueError(str(last_error or "تعذر قراءة ملف الحضور."))
    return best


def attendance_template_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "Attendance": [
            {
                "رقم الموظف": "EMP0001", "التاريخ": date.today().isoformat(),
                "الحالة": "حاضر", "وقت الحضور": "08:00",
                "وقت الانصراف": "17:00", "ملاحظة": "",
            },
            {
                "رقم الموظف": "EMP0002", "التاريخ": date.today().isoformat(),
                "الحالة": "غائب", "وقت الحضور": "",
                "وقت الانصراف": "", "ملاحظة": "مثال",
            },
        ]
    }
