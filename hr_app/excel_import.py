from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from .config import EMPLOYEE_STATUSES
from .identity_rules import IDENTITY_TYPES, identity_type_for_nationality

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_NS = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_REL_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

FIELD_LABELS = {
    "emp_code": "رقم الموظف",
    "name": "الاسم",
    "phone": "رقم الجوال",
    "id_number": "رقم الهوية / الإقامة / الجواز",
    "id_type": "نوع الوثيقة",
    "id_issue_date": "تاريخ بداية الهوية",
    "id_expiry_date": "تاريخ انتهاء الهوية",
    "work_permit_start_date": "تاريخ بداية كرت العمل",
    "work_permit_expiry_date": "تاريخ انتهاء كرت العمل",
    "medical_insurance_start_date": "تاريخ بداية التأمين الطبي",
    "medical_insurance_expiry_date": "تاريخ انتهاء التأمين الطبي",
    "nationality": "الجنسية",
    "birth_date": "تاريخ الميلاد",
    "join_date": "تاريخ الالتحاق",
    "department": "القسم",
    "job_title": "المسمى الوظيفي",
    "salary": "الراتب",
    "work_location": "مكان العمل",
    "email": "البريد الإلكتروني",
    "bank_name": "اسم البنك",
    "bank_account_number": "رقم الحساب البنكي",
    "iban": "رقم الآيبان",
    "status": "الحالة",
    "performance_rating": "التقييم من 5",
    "notes": "ملاحظات",
}

REQUIRED_FIELDS = {"name"}
CORE_FIELDS = {"name"}

_HEADER_ALIASES = {
    "emp_code": [
        "رقم الموظف", "كود الموظف", "رمز الموظف", "معرف الموظف", "الرقم الوظيفي",
        "employee code", "employee id", "emp code", "emp id", "staff id",
    ],
    "name": [
        "الاسم", "اسم الموظف", "الاسم الكامل", "اسم المشترك", "اسم العامل",
        "employee name", "name", "full name", "worker name",
    ],
    "phone": [
        "رقم الجوال", "الجوال", "رقم الهاتف", "الهاتف", "موبايل", "mobile", "phone",
        "phone number", "mobile number",
    ],
    "id_number": [
        "رقم الهوية", "الهوية", "رقم الهوية الوطنية", "رقم الاقامة", "رقم الإقامة",
        "رقم البطاقة", "رقم الوثيقة", "رقم اثبات الهوية", "رقم الجواز", "رقم جواز السفر",
        "national id", "id number", "identity number", "iqama number", "nationality id",
        "passport number", "passport no",
    ],
    "id_type": [
        "نوع الوثيقة", "نوع الهوية", "نوع الاثبات", "نوع الإثبات", "هوية او اقامة",
        "هوية أو إقامة", "جواز سفر", "جواز", "identity type", "document type", "id type", "passport",
    ],
    "id_issue_date": [
        "تاريخ بداية الهوية", "تاريخ اصدار الهوية", "تاريخ إصدار الهوية",
        "تاريخ اصدار الاقامة", "تاريخ إصدار الإقامة", "identity issue date",
        "id issue date", "iqama issue date",
    ],
    "id_expiry_date": [
        "تاريخ انتهاء الهوية", "انتهاء الهوية", "صلاحية الهوية", "تاريخ انتهاء الاقامة",
        "تاريخ انتهاء الإقامة", "تاريخ الانتهاء", "انتهاء الاقامة",
        "identity expiry", "id expiry", "iqama expiry", "expiry date",
    ],
    "work_permit_expiry_date": [
        "تاريخ انتهاء كرت العمل", "انتهاء كرت العمل", "صلاحية كرت العمل",
        "تاريخ انتهاء رخصة العمل", "انتهاء رخصة العمل",
        "work permit expiry", "work card expiry", "labor card expiry",
    ],
    "work_permit_start_date": [
        "تاريخ بداية كرت العمل", "بداية كرت العمل", "تاريخ اصدار كرت العمل",
        "تاريخ إصدار رخصة العمل", "work permit start", "work card start",
        "labor card issue date",
    ],
    "medical_insurance_expiry_date": [
        "تاريخ انتهاء التأمين الطبي", "انتهاء التأمين الطبي",
        "صلاحية التأمين الطبي", "تاريخ انتهاء التأمين الصحي",
        "medical insurance expiry", "health insurance expiry",
    ],
    "medical_insurance_start_date": [
        "تاريخ بداية التأمين الطبي", "بداية التأمين الطبي",
        "تاريخ بداية التأمين الصحي", "medical insurance start",
        "health insurance start", "insurance effective date",
    ],
    "nationality": ["الجنسية", "nationality"],
    "birth_date": [
        "تاريخ الميلاد", "تاريخ الميلاد ميلادي", "ميلاد", "birth date", "date of birth", "dob",
    ],
    "join_date": [
        "تاريخ الالتحاق", "تاريخ التعيين", "تاريخ التعين", "تاريخ المباشرة", "تاريخ الانضمام",
        "join date", "joining date", "hire date", "start date",
    ],
    "department": [
        "القسم", "الادارة", "الإدارة", "اسم الشركة", "الشركة",
        "department", "division", "section", "company",
    ],
    "job_title": [
        "المسمى الوظيفي", "المسمي الوظيفي", "المسمي الوظيفة", "الوظيفة", "المسمى",
        "المهنة", "job title", "position", "title", "role", "profession", "occupation",
    ],
    "salary": ["الراتب", "الراتب الاساسي", "الراتب الأساسي", "salary", "basic salary", "wage"],
    "work_location": [
        "مكان العمل", "موقع العمل", "جهة العمل داخل الشركة", "جهة العمل", "الفرع",
        "work location", "location", "branch", "office",
    ],
    "email": [
        "البريد الإلكتروني", "البريد الالكتروني", "البريد", "الايميل", "الإيميل",
        "email", "email address", "e-mail",
    ],
    "bank_name": ["اسم البنك", "البنك", "bank name", "bank"],
    "bank_account_number": [
        "رقم الحساب البنكي", "رقم الحساب", "حساب البنك", "bank account number",
        "account number", "bank account",
    ],
    "iban": ["رقم الايبان", "رقم الآيبان", "الايبان", "الآيبان", "iban", "iban number"],
    "status": ["الحالة", "حالة الموظف", "status", "employee status"],
    "performance_rating": ["التقييم", "التقييم من 5", "تقييم الموظف", "النجوم", "rating", "performance rating", "stars"],
    "notes": ["ملاحظات", "ملاحظة", "notes", "note", "comments", "comment"],
}

_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_header(value: Any) -> str:
    text = str(value or "").strip().lower().translate(_ARABIC_DIGITS)
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    text = (
        text.replace("\u0640", "")
        .replace("أ", "ا").replace("إ", "ا").replace("آ", "ا")
        .replace("ة", "ه").replace("ى", "ي").replace("ؤ", "و").replace("ئ", "ي")
    )
    return re.sub(r"[^\w\u0600-\u06ff]+", "", text, flags=re.UNICODE)


ALIAS_TO_FIELD: dict[str, str] = {}
for _field, _aliases in _HEADER_ALIASES.items():
    for _alias in [FIELD_LABELS[_field], _field, *_aliases]:
        ALIAS_TO_FIELD[normalize_header(_alias)] = _field


@dataclass
class SheetData:
    name: str
    rows: list[list[Any]]


@dataclass
class ParsedSheet:
    sheet_name: str
    header_row: int
    header_rows: int
    mapping: dict[str, int]
    records: list[dict[str, Any]]
    missing_required: list[str]
    ignored_headers: list[str]


def _column_index(cell_ref: str) -> int:
    letters = "".join(ch for ch in cell_ref if ch.isalpha()).upper()
    number = 0
    for ch in letters:
        number = number * 26 + (ord(ch) - 64)
    return max(0, number - 1)


def _read_shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    values: list[str] = []
    for item in root.findall(f"{_NS}si"):
        parts = [node.text or "" for node in item.iter(f"{_NS}t")]
        values.append("".join(parts))
    return values


def _date_style_indexes(archive: zipfile.ZipFile) -> set[int]:
    try:
        root = ET.fromstring(archive.read("xl/styles.xml"))
    except KeyError:
        return set()
    date_num_fmt_ids = set(range(14, 23)) | {27, 30, 36, 45, 46, 47, 50, 57}
    num_fmts = root.find(f"{_NS}numFmts")
    if num_fmts is not None:
        for item in num_fmts.findall(f"{_NS}numFmt"):
            try:
                fmt_id = int(item.attrib.get("numFmtId", "0"))
            except ValueError:
                continue
            code = item.attrib.get("formatCode", "").lower()
            code = re.sub(r'"[^"]*"|\\.', "", code)
            if any(token in code for token in ("yy", "dd", "mm", "hh", "ss")):
                date_num_fmt_ids.add(fmt_id)
    result: set[int] = set()
    cell_xfs = root.find(f"{_NS}cellXfs")
    if cell_xfs is not None:
        for index, xf in enumerate(cell_xfs.findall(f"{_NS}xf")):
            try:
                if int(xf.attrib.get("numFmtId", "0")) in date_num_fmt_ids:
                    result.add(index)
            except ValueError:
                pass
    return result


def _excel_date(value: float, date_1904: bool) -> str:
    origin = datetime(1904, 1, 1) if date_1904 else datetime(1899, 12, 30)
    converted = origin + timedelta(days=value)
    if converted.time() == datetime.min.time():
        return converted.date().isoformat()
    return converted.isoformat(sep=" ", timespec="seconds")


def _cell_value(cell: ET.Element, shared_strings: list[str], date_styles: set[int], date_1904: bool) -> Any:
    cell_type = cell.attrib.get("t", "")
    style_index = int(cell.attrib.get("s", "0") or 0)
    if cell_type == "inlineStr":
        return "".join(node.text or "" for node in cell.iter(f"{_NS}t"))
    value_node = cell.find(f"{_NS}v")
    if value_node is None or value_node.text is None:
        return ""
    raw = value_node.text
    if cell_type == "s":
        try:
            return shared_strings[int(raw)]
        except (ValueError, IndexError):
            return raw
    if cell_type in {"str", "e"}:
        return raw
    if cell_type == "b":
        return raw == "1"
    try:
        number = float(raw)
    except ValueError:
        return raw
    if style_index in date_styles:
        return _excel_date(number, date_1904)
    if number.is_integer():
        return int(number)
    return number


def read_xlsx(path: Path | str) -> list[SheetData]:
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        shared_strings = _read_shared_strings(archive)
        date_styles = _date_style_indexes(archive)
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        workbook_pr = workbook.find(f"{_NS}workbookPr")
        date_1904 = bool(workbook_pr is not None and workbook_pr.attrib.get("date1904") in {"1", "true", "True"})
        rel_root = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        relationships = {
            rel.attrib["Id"]: rel.attrib["Target"]
            for rel in rel_root.findall(f"{_REL_NS}Relationship")
        }
        sheets: list[SheetData] = []
        sheets_root = workbook.find(f"{_NS}sheets")
        if sheets_root is None:
            return sheets
        for sheet in sheets_root.findall(f"{_NS}sheet"):
            rel_id = sheet.attrib.get(f"{_OFFICE_REL_NS}id")
            target = relationships.get(rel_id or "")
            if not target:
                continue
            target = target.lstrip("/")
            worksheet_path = target if target.startswith("xl/") else f"xl/{target}"
            root = ET.fromstring(archive.read(worksheet_path))
            rows: list[list[Any]] = []
            for row_node in root.findall(f".//{_NS}sheetData/{_NS}row"):
                values: list[Any] = []
                for cell in row_node.findall(f"{_NS}c"):
                    index = _column_index(cell.attrib.get("r", "A1"))
                    while len(values) <= index:
                        values.append("")
                    values[index] = _cell_value(cell, shared_strings, date_styles, date_1904)
                rows.append(values)
            sheets.append(SheetData(sheet.attrib.get("name", "Sheet"), rows))
        return sheets


def read_csv_file(path: Path | str) -> list[SheetData]:
    path = Path(path)
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-16", "cp1256", "windows-1252"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("تعذر قراءة ترميز ملف CSV.")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [list(row) for row in csv.reader(text.splitlines(), dialect)]
    return [SheetData(path.stem, rows)]


def read_xls(path: Path | str) -> list[SheetData]:
    """قراءة ملفات Excel 97-2003 القديمة مع الاحتفاظ بالتواريخ والأرقام."""
    try:
        import xlrd
    except ImportError as exc:  # pragma: no cover - protected by the packaged build
        raise ValueError(
            "تعذر تشغيل قارئ ملفات XLS القديمة. أعد تثبيت النسخة الكاملة من البرنامج."
        ) from exc

    try:
        workbook = xlrd.open_workbook(str(Path(path)), on_demand=True)
    except Exception as exc:
        raise ValueError(f"تعذر فتح ملف XLS القديم: {exc}") from exc

    sheets: list[SheetData] = []
    try:
        for worksheet in workbook.sheets():
            rows: list[list[Any]] = []
            for row_index in range(worksheet.nrows):
                values: list[Any] = []
                for cell in worksheet.row(row_index):
                    value: Any = cell.value
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        try:
                            converted = xlrd.xldate_as_datetime(value, workbook.datemode)
                            value = (
                                converted.date().isoformat()
                                if converted.time() == datetime.min.time()
                                else converted.isoformat(sep=" ", timespec="seconds")
                            )
                        except (ValueError, OverflowError):
                            pass
                    elif cell.ctype == xlrd.XL_CELL_NUMBER and float(value).is_integer():
                        value = int(value)
                    elif cell.ctype in {xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR}:
                        value = ""
                    values.append(value)
                while values and _is_empty(values[-1]):
                    values.pop()
                rows.append(values)
            sheets.append(SheetData(worksheet.name or "Sheet", rows))
    finally:
        workbook.release_resources()
    return sheets


def read_employee_file(path: Path | str) -> list[SheetData]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".xlsx":
        return read_xlsx(path)
    if suffix == ".csv":
        return read_csv_file(path)
    if suffix == ".xls":
        return read_xls(path)
    raise ValueError("اختر ملف Excel بصيغة XLSX أو XLS، أو ملف CSV.")


def _is_empty(value: Any) -> bool:
    return value is None or str(value).strip() == ""


def _western_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return str(value).strip().translate(_ARABIC_DIGITS)


def _normalize_digits(value: Any) -> str:
    text = _western_text(value)
    if text.endswith(".0") and text[:-2].replace("-", "").isdigit():
        text = text[:-2]
    return "".join(ch for ch in text if ch.isdigit())


def _normalize_phone(value: Any) -> str:
    digits = _normalize_digits(value)
    if len(digits) == 9 and digits.startswith("5"):
        digits = "0" + digits
    if len(digits) == 12 and digits.startswith("966"):
        digits = "0" + digits[3:]
    return digits


def _normalize_salary(value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    text = _western_text(value).replace(",", "").replace("٬", "").replace("ريال", "").strip()
    text = text.replace("٫", ".")
    return text


def _normalize_date(value: Any) -> str:
    if _is_empty(value):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if 1 <= number <= 2958465:
            try:
                return (datetime(1899, 12, 30) + timedelta(days=number)).date().isoformat()
            except (OverflowError, ValueError):
                pass
    text = _western_text(value)
    if " " in text and re.match(r"^\d{4}-\d{2}-\d{2}", text):
        return text[:10]
    formats = (
        "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d-%m-%Y", "%m/%d/%Y",
        "%d.%m.%Y", "%Y.%m.%d",
    )
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt).date()
            if parsed.year < 1700:
                break
            return parsed.isoformat()
        except ValueError:
            continue
    hijri_match = re.fullmatch(r"\s*(\d{1,2})[./-](\d{1,2})[./-](1[34]\d{2})\s*", text)
    if hijri_match:
        day_value, month_value, year_value = map(int, hijri_match.groups())
        try:
            from hijridate import Hijri

            return Hijri(year_value, month_value, day_value).to_gregorian().isoformat()
        except (ImportError, ValueError, OverflowError):
            return ""
    return text


def _normalize_status(value: Any) -> str:
    text = _western_text(value)
    if not text:
        return "نشط"
    normalized = normalize_header(text)
    aliases = {
        normalize_header("نشط"): "نشط",
        normalize_header("active"): "نشط",
        normalize_header("في إجازة"): "في إجازة",
        normalize_header("اجازة"): "في إجازة",
        normalize_header("leave"): "في إجازة",
        normalize_header("on leave"): "في إجازة",
        normalize_header("منتهي الخدمة"): "منتهي الخدمة",
        normalize_header("غير نشط"): "منتهي الخدمة",
        normalize_header("terminated"): "منتهي الخدمة",
        normalize_header("inactive"): "منتهي الخدمة",
    }
    return aliases.get(normalized, text if text in EMPLOYEE_STATUSES else text)


def _header_candidates(value: Any) -> list[tuple[int, str]]:
    """إرجاع الحقول المحتملة مرتبة بدرجة ثقة للمطابقة الذكية."""
    normalized = normalize_header(value)
    if not normalized:
        return []
    candidates: dict[str, int] = {}

    exact = ALIAS_TO_FIELD.get(normalized)
    if exact:
        candidates[exact] = 120

    def offer(field: str, score: int, *, required: tuple[str, ...] = (), blocked: tuple[str, ...] = ()) -> None:
        if required and not all(token in normalized for token in required):
            return
        if any(token in normalized for token in blocked):
            return
        candidates[field] = max(score, candidates.get(field, 0))

    if any(token in normalized for token in ("جوال", "هاتف", "موبايل", "mobile", "phone")):
        offer("phone", 98 if not normalized.endswith("2") else 82)
    if any(token in normalized for token in ("ايميل", "بريدالكتروني", "email")):
        offer("email", 96)
    if "جنسيه" in normalized or "nationality" in normalized:
        offer("nationality", 100)
    if any(token in normalized for token in ("مهنه", "مسمي", "وظيفه", "jobtitle", "profession", "occupation", "position")):
        offer("job_title", 98, blocked=("رقموظيفي",))
    if any(token in normalized for token in ("راتباساسي", "الراتبالاساسي", "basicsalary")):
        offer("salary", 106)
    elif any(token in normalized for token in ("راتب", "salary", "wage")):
        offer("salary", 94, blocked=("اجماليراتب", "صافيراتب", "بدلسكن"))
    if any(token in normalized for token in ("مكانعمل", "موقععمل", "جههالعمل", "فرع", "worklocation", "branch")):
        offer("work_location", 95)
    if any(token in normalized for token in ("اسمالشركه", "قسم", "اداره", "department", "division", "section")):
        offer("department", 96)
    if any(token in normalized for token in ("ملاحظ", "notes", "comment")):
        offer("notes", 90)
    if any(token in normalized for token in ("حالهالموظف", "status")) or normalized == normalize_header("الحالة"):
        offer("status", 94)
    if any(token in normalized for token in ("تقييم", "rating", "stars")):
        offer("performance_rating", 92)
    if (
        any(token in normalized for token in ("رقمالموظف", "الرقمالوظيفي", "كودالموظف", "empcode", "employeeid", "staffid"))
        and not any(token in normalized for token in ("هويه", "اقامه", "بطاقه"))
    ):
        offer("emp_code", 104)
    if any(token in normalized for token in ("رقمالهويه", "رقمالاقامه", "رقمالبطاقه", "رقمالوثيقه", "iqamanumber", "nationalid", "identitynumber")):
        offer("id_number", 108, blocked=("انتهاء", "تاريخ"))
    if any(token in normalized for token in ("نوعالوثيقه", "نوعالهويه", "نوعالاثبات", "identitytype", "documenttype")):
        offer("id_type", 101)
    if (
        any(token in normalized for token in ("تاريخانتهاء", "انتهاءالهويه", "انتهاءالاقامه", "idexpiry", "iqamaexpiry", "expirydate"))
        and not any(token in normalized for token in (
            "جواز", "passport", "كرتالعمل", "رخصهالعمل", "workpermit",
            "workcard", "laborcard", "تامينطبي", "تامينصحي",
            "medicalinsurance", "healthinsurance",
        ))
    ):
        offer("id_expiry_date", 104 if "هجري" not in normalized else 78)
    if any(token in normalized for token in (
        "انتهاءكرتالعمل", "تاريخانتهاءكرتالعمل", "انتهاءرخصهالعمل",
        "workpermitexpiry", "workcardexpiry", "laborcardexpiry",
    )):
        offer("work_permit_expiry_date", 110 if "هجري" not in normalized else 78)
    if any(token in normalized for token in (
        "انتهاءالتامينالطبي", "تاريخانتهاءالتامينالطبي",
        "انتهاءالتامينالصحي", "medicalinsuranceexpiry",
        "healthinsuranceexpiry",
    )):
        offer("medical_insurance_expiry_date", 110 if "هجري" not in normalized else 78)
    if any(token in normalized for token in ("تاريخميلاد", "birthdate", "dateofbirth", "dob")):
        offer("birth_date", 106 if "هجري" not in normalized else 76)
    if any(token in normalized for token in ("تاريخالتحاق", "تاريختعيين", "تاريختعين", "تاريخمباشره", "تاريخانضمام", "joindate", "hiredate", "startdate")):
        offer("join_date", 104)

    name_tokens = ("اسمالموظف", "اسمالمشترك", "اسمالعامل", "الاسمالرباعي", "الاسمالكامل", "employeename", "fullname")
    if any(token in normalized for token in name_tokens):
        offer("name", 110)
    elif normalized in {"الاسم", "اسم", "name"}:
        offer("name", 105)
    elif normalized.startswith("الاسم") and not any(
        token in normalized for token in ("شركه", "بنك", "فرع", "قسم", "صاحبالعمل")
    ):
        offer("name", 100)

    return sorted(((score, field) for field, score in candidates.items()), reverse=True)


def _header_texts(rows: list[list[Any]], row_index: int, depth: int) -> list[str]:
    max_columns = max((len(row) for row in rows[row_index : row_index + depth]), default=0)
    texts: list[str] = []
    for column in range(max_columns):
        parts: list[str] = []
        for row in rows[row_index : row_index + depth]:
            if column < len(row) and not _is_empty(row[column]):
                value = _western_text(row[column])
                if not parts or normalize_header(value) != normalize_header(parts[-1]):
                    parts.append(value)
        texts.append(" ".join(parts))
    return texts


def _map_header_texts(texts: list[str]) -> tuple[dict[str, int], list[str], int]:
    options: list[tuple[int, str, int]] = []
    for column, text in enumerate(texts):
        for score, field in _header_candidates(text):
            options.append((score, field, column))
    options.sort(reverse=True)

    mapping: dict[str, int] = {}
    selected_columns: set[int] = set()
    score_total = 0
    for score, field, column in options:
        if field in mapping or column in selected_columns:
            continue
        mapping[field] = column
        selected_columns.add(column)
        score_total += score
    ignored = [text for column, text in enumerate(texts) if text and column not in selected_columns]
    return mapping, ignored, score_total


def _find_header(rows: list[list[Any]]) -> tuple[int, int, dict[str, int], list[str]]:
    best: tuple[int, int, dict[str, int], list[str], int] | None = None
    for row_index in range(min(60, len(rows))):
        for depth in range(1, min(3, len(rows) - row_index) + 1):
            texts = _header_texts(rows, row_index, depth)
            mapping, ignored, confidence = _map_header_texts(texts)
            has_identity = "id_number" in mapping or "emp_code" in mapping
            if "name" not in mapping or not has_identity or len(mapping) < 3:
                continue
            score = (
                confidence
                + len(mapping) * 35
                + len(REQUIRED_FIELDS.intersection(mapping)) * 25
                + len(CORE_FIELDS.intersection(mapping)) * 60
                - (depth - 1) * 8
            )
            if best is None or score > best[4]:
                best = row_index, depth, mapping, ignored, score
    if best is None:
        raise ValueError(
            "لم يتم العثور على صف عناوين يحتوي الاسم ورقم الموظف أو الهوية أو الإقامة أو الجواز."
        )
    return best[0], best[1], best[2], best[3]


_NON_EMPLOYEE_NAMES = {
    normalize_header(value)
    for value in (
        "الإجمالي", "الاجمالي", "الإجمالي الكلي", "الاجمالي الكلي", "المجموع",
        "المستقيلين", "المستبعدين", "المحاسب", "المدير العام", "شؤون الموظفين",
        "كشف إجمالي", "رواتب نقدية", "رواتب بنكية",
    )
}


def _plausible_employee_name(value: Any) -> bool:
    text = _western_text(value)
    normalized = normalize_header(text)
    if len(normalized) < 3 or normalized in _NON_EMPLOYEE_NAMES:
        return False
    if any(marker in normalized for marker in ("الاجمالي", "المجموع", "المستقيلين", "المستبعدين")):
        return False
    return any(ch.isalpha() for ch in text)


def _infer_department_from_sheet(sheet_name: str) -> str:
    normalized = normalize_header(sheet_name)
    generic_markers = (
        "sheet", "موظف", "سعودي", "مقيم", "نشط", "مستبعد", "بيانات", "بنام",
        "رواتب", "مسير", "اجمالي", "سلف", "حاسبه",
    )
    if not normalized or any(marker in normalized for marker in generic_markers):
        return ""
    return _western_text(sheet_name)


def _append_import_note(existing: str, warnings: list[str]) -> str:
    if not warnings:
        return existing
    note = "استيراد ذكي - بيانات غير متوفرة في المصدر: " + "، ".join(warnings)
    return f"{existing}\n{note}".strip() if existing else note


def parse_employee_sheet(sheet: SheetData) -> ParsedSheet:
    header_index, header_depth, mapping, ignored = _find_header(sheet.rows)
    missing = sorted(REQUIRED_FIELDS - set(mapping), key=lambda f: FIELD_LABELS[f])
    records: list[dict[str, Any]] = []
    data_start = header_index + header_depth
    for zero_index, row in enumerate(sheet.rows[data_start:], start=data_start):
        if not any(not _is_empty(value) for value in row):
            continue
        mapped_fields = {
            field
            for field, col_index in mapping.items()
            if col_index < len(row) and not _is_empty(row[col_index])
        }
        record: dict[str, Any] = {
            "_source_row": zero_index + 1,
            "_mapped_fields": (),
        }
        for field, col_index in mapping.items():
            record[field] = row[col_index] if col_index < len(row) else ""
        record["emp_code"] = _western_text(record.get("emp_code", "")).upper()
        record["name"] = _western_text(record.get("name", ""))
        record["phone"] = _normalize_phone(record.get("phone", ""))
        record["id_number"] = re.sub(
            r"[^A-Z0-9]", "", _western_text(record.get("id_number", "")).upper()
        )

        inferred_fields: set[str] = set()
        row_digit_values = [
            (_normalize_digits(value), column)
            for column, value in enumerate(row)
            if not _is_empty(value)
        ]
        if not record["id_number"]:
            inferred_id, inferred_id_column = next(
                (
                    (digits, column)
                    for digits, column in row_digit_values
                    if len(digits) == 10 and digits[:1] in {"1", "2"}
                ),
                ("", -1),
            )
            if inferred_id:
                record["id_number"] = inferred_id
                inferred_fields.add("id_number")
                for field, mapped_column in mapping.items():
                    if mapped_column == inferred_id_column and field not in {"id_number", "emp_code"}:
                        record[field] = ""
                        mapped_fields.discard(field)
        if not record["phone"]:
            inferred_phone, inferred_phone_column = next(
                (
                    (digits, column)
                    for digits, column in row_digit_values
                    if len(digits) == 10 and digits.startswith("05")
                ),
                ("", -1),
            )
            if inferred_phone:
                record["phone"] = inferred_phone
                inferred_fields.add("phone")
                for field, mapped_column in mapping.items():
                    if mapped_column == inferred_phone_column and field != "phone":
                        record[field] = ""
                        mapped_fields.discard(field)

        raw_identity_type = _western_text(record.get("id_type", "")).strip()
        normalized_identity_type = normalize_header(raw_identity_type)
        passport_declared = (
            "جواز" in normalized_identity_type
            or "passport" in normalized_identity_type
            or (record["id_number"] and not record["id_number"].isdigit())
        )
        valid_passport = (
            passport_declared
            and 5 <= len(record["id_number"]) <= 20
            and len(_normalize_digits(record["id_number"])) >= 4
        )
        if (
            not _plausible_employee_name(record["name"])
            or (
                record["id_number"]
                and not valid_passport
                and not (record["id_number"].isdigit() and len(record["id_number"]) == 10)
            )
            or (not record["id_number"] and not record["emp_code"])
        ):
            continue

        record["nationality"] = _western_text(record.get("nationality", ""))
        if not record["nationality"]:
            if record["id_number"].startswith("1"):
                record["nationality"] = "سعودي"
                inferred_fields.add("nationality")
            elif record["id_number"].startswith("2"):
                record["nationality"] = "غير سعودي"
                inferred_fields.add("nationality")
        supplied_type = raw_identity_type
        if supplied_type in {"اقامة", "إقامه", "اقامه"}:
            supplied_type = "إقامة"
        elif supplied_type in {"هوية", "هويه", "هوية وطنية", "هويه وطنيه"}:
            supplied_type = "هوية وطنية"
        elif passport_declared:
            supplied_type = "جواز سفر"
        record["id_type"] = (
            supplied_type
            or (identity_type_for_nationality(record["nationality"]) if record["nationality"] else "")
        )
        record["birth_date"] = _normalize_date(record.get("birth_date", ""))
        record["id_issue_date"] = _normalize_date(record.get("id_issue_date", ""))
        record["id_expiry_date"] = _normalize_date(record.get("id_expiry_date", ""))
        record["work_permit_start_date"] = _normalize_date(
            record.get("work_permit_start_date", "")
        )
        record["work_permit_expiry_date"] = _normalize_date(
            record.get("work_permit_expiry_date", "")
        )
        record["medical_insurance_start_date"] = _normalize_date(
            record.get("medical_insurance_start_date", "")
        )
        record["medical_insurance_expiry_date"] = _normalize_date(
            record.get("medical_insurance_expiry_date", "")
        )
        record["join_date"] = _normalize_date(record.get("join_date", ""))
        record["department"] = _western_text(record.get("department", ""))
        if not record["department"]:
            inferred_department = _infer_department_from_sheet(sheet.name)
            if inferred_department:
                record["department"] = inferred_department
                inferred_fields.add("department")
        record["job_title"] = _western_text(record.get("job_title", ""))
        record["salary"] = _normalize_salary(record.get("salary", ""))
        record["work_location"] = _western_text(record.get("work_location", ""))
        record["email"] = _western_text(record.get("email", ""))
        record["bank_name"] = _western_text(record.get("bank_name", ""))
        record["bank_account_number"] = _western_text(record.get("bank_account_number", ""))
        record["iban"] = _western_text(record.get("iban", ""))
        record["status"] = _normalize_status(record.get("status", ""))
        rating_raw = _western_text(record.get("performance_rating", "")).strip()
        record["performance_rating"] = rating_raw or "0"
        warnings: list[str] = []
        for field in ("phone", "join_date", "department", "job_title", "salary"):
            if not str(record.get(field, "")).strip():
                warnings.append(FIELD_LABELS[field])
        record["notes"] = _append_import_note(
            _western_text(record.get("notes", "")),
            warnings,
        )
        record["_mapped_fields"] = tuple(sorted(mapped_fields | inferred_fields))
        record["_inferred_fields"] = tuple(sorted(inferred_fields))
        record["_import_warnings"] = tuple(warnings)
        records.append(record)
    return ParsedSheet(
        sheet_name=sheet.name,
        header_row=header_index + 1,
        header_rows=header_depth,
        mapping=mapping,
        records=records,
        missing_required=missing,
        ignored_headers=ignored,
    )


def template_rows() -> dict[str, list[dict[str, Any]]]:
    return {
        "الموظفون": [
            {
                "رقم الموظف": "",
                "الاسم": "مثال: أحمد محمد",
                "رقم الجوال": "0500000000",
                "الجنسية": "سعودي",
                "نوع الوثيقة": "هوية وطنية",
                "رقم الهوية / الإقامة / الجواز": "1234567890",
                "تاريخ بداية الوثيقة": "2024-01-01",
                "تاريخ انتهاء الوثيقة": "2029-12-31",
                "تاريخ بداية كرت العمل": "2024-10-31",
                "تاريخ انتهاء كرت العمل": "2029-10-31",
                "تاريخ بداية التأمين الطبي": "2024-08-31",
                "تاريخ انتهاء التأمين الطبي": "2029-08-31",
                "تاريخ الميلاد": "1990-01-01",
                "تاريخ الالتحاق": date.today().isoformat(),
                "القسم": "تقنية المعلومات",
                "المسمى الوظيفي": "مطور",
                "الراتب": 10000,
                "مكان العمل": "الرياض",
                "البريد الإلكتروني": "employee@example.com",
                "اسم البنك": "البنك التجريبي",
                "رقم الحساب البنكي": "608010167519",
                "رقم الآيبان": "SA0380000000608010167519",
                "الحالة": "نشط",
                "التقييم من 5": 0,
                "ملاحظات": "احذف صف المثال قبل الاستيراد",
            }
        ],
        "تعليمات": [
            {"البند": "الملف المدعوم", "التفاصيل": "XLSX أو CSV"},
            {"البند": "الحد الأدنى للحفظ", "التفاصيل": "الاسم فقط في الإدخال اليدوي، والاسم مع رقم موظف أو وثيقة عند الاستيراد. يمكن استكمال البقية لاحقًا."},
            {"البند": "نوع الوثيقة", "التفاصيل": "يدعم هوية وطنية وإقامة وجواز سفر. يمكن ترك النوع ليحدده النظام من الجنسية أو شكل الرقم عند الإمكان."},
            {"البند": "صيغة التاريخ", "التفاصيل": "YYYY-MM-DD مثل 2026-07-11"},
            {"البند": "الحالة", "التفاصيل": "نشط، في إجازة، منتهي الخدمة"},
            {"البند": "رقم الموظف", "التفاصيل": "اختياري؛ يُنشئه النظام تلقائيًا عند تركه فارغًا"},
        ],
    }
