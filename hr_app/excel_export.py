from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import re
from xml.sax.saxutils import escape, quoteattr
import zipfile

from .config import APP_NAME, APP_VERSION, RELEASE_CHANNEL
from .company import company_profile_rows, normalize_company_profile
from .security import mask_bank_account, mask_iban, mask_id_number


HEADER_LABELS = {
    "id": "المعرّف",
    "emp_code": "رقم الموظف",
    "employee_id": "معرّف الموظف",
    "employee_name": "اسم الموظف",
    "name": "الاسم",
    "phone": "رقم الجوال",
    "id_number": "رقم الهوية / الإقامة / الجواز",
    "id_type": "نوع الوثيقة",
    "id_issue_date": "تاريخ بداية الوثيقة",
    "id_expiry_date": "تاريخ انتهاء الوثيقة",
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
    "base_salary": "الراتب الأساسي",
    "work_location": "مكان العمل",
    "email": "البريد الإلكتروني",
    "bank_name": "اسم البنك",
    "bank_account_number": "رقم الحساب البنكي",
    "iban": "رقم الآيبان",
    "status": "الحالة",
    "performance_rating": "التقييم من 5",
    "notes": "ملاحظات",
    "attendance_date": "تاريخ الحضور",
    "date": "التاريخ",
    "check_in": "وقت الحضور",
    "check_out": "وقت الانصراف",
    "check_in_time": "وقت الحضور",
    "check_out_time": "وقت الانصراف",
    "working_hours": "ساعات العمل",
    "worked_hours": "ساعات العمل",
    "worked_minutes": "دقائق العمل",
    "late_minutes": "دقائق التأخير",
    "leave_type": "نوع الإجازة",
    "start_date": "تاريخ البداية",
    "end_date": "تاريخ النهاية",
    "days": "عدد الأيام",
    "total_days": "عدد الأيام",
    "reason": "السبب",
    "document_type": "نوع المستند",
    "original_name": "اسم الملف",
    "expiry_date": "تاريخ الانتهاء",
    "label": "الحقل",
    "value_text": "القيمة",
    "period": "الفترة",
    "allowances": "البدلات",
    "deductions": "الخصومات",
    "net_salary": "صافي الراتب",
    "created_at": "تاريخ الإنشاء",
    "updated_at": "تاريخ التحديث",
    "approved_at": "تاريخ الاعتماد",
    "field": "البيان",
    "value": "القيمة",
}

SHEET_LABELS = {
    "Employees": "الموظفون",
    "Attendance": "الحضور",
    "Leaves": "الإجازات",
    "Users": "المستخدمون",
    "Payroll": "مسير الرواتب",
    "AllowanceDetails": "تفاصيل البدلات",
    "Documents": "المستندات",
    "Contracts": "العقود",
    "Notifications": "الإشعارات",
    "Company": "بيانات الشركة",
    "EmployeeSummary": "الملخص التنفيذي",
    "EmployeeDetails": "بيانات الموظف",
    "AttendanceSummary": "ملخص الحضور",
    "CustomFields": "البيانات المخصصة",
}

_INVALID_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")


def _column_name(number: int) -> str:
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _display_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bool):
        return "نعم" if value else "لا"
    return str(value)


def _cell_xml(row: int, col: int, value, style_id: int) -> str:
    ref = f"{_column_name(col)}{row}"
    if value is None:
        return f'<c r="{ref}" s="{style_id}" t="inlineStr"><is><t></t></is></c>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style_id}"><v>{value}</v></c>'
    text = escape(_display_value(value))
    preserve = ' xml:space="preserve"' if text.startswith(" ") or text.endswith(" ") else ""
    return f'<c r="{ref}" s="{style_id}" t="inlineStr"><is><t{preserve}>{text}</t></is></c>'


def _column_widths(headers: list[str], records: list[dict], keys: list[str]) -> list[float]:
    widths: list[float] = []
    for header, key in zip(headers, keys):
        longest = len(header)
        for record in records[:1000]:
            text = _display_value(record.get(key, ""))
            longest = max(longest, max((len(line) for line in text.splitlines()), default=0))
        if key in {"name", "notes", "details", "job_title", "department"}:
            width = min(42, max(18, longest * 1.15 + 2))
        elif "date" in key or key.endswith("_at"):
            width = min(22, max(14, longest + 2))
        else:
            width = min(28, max(11, longest * 1.05 + 2))
        widths.append(round(width, 1))
    return widths


def _sheet_xml(
    headers: list[str],
    keys: list[str],
    records: list[dict],
    *,
    title: str = "",
    subtitle: str = "",
) -> str:
    widths = _column_widths(headers, records, keys)
    last_column = _column_name(max(1, len(headers)))
    header_row = 3 if title else 1
    last_row = max(header_row, header_row + len(records))
    row_parts: list[str] = []
    if title:
        row_parts.append(
            f'<row r="1" ht="36" customHeight="1">{_cell_xml(1, 1, title, 8)}</row>'
        )
        row_parts.append(
            f'<row r="2" ht="24" customHeight="1">{_cell_xml(2, 1, subtitle, 9)}</row>'
        )
    table_rows: list[list] = [headers]
    table_rows.extend([[record.get(key, "") for key in keys] for record in records])
    for offset, values in enumerate(table_rows):
        row_index = header_row + offset
        cells: list[str] = []
        for column_index, value in enumerate(values, start=1):
            if row_index == header_row:
                style_id = 1
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                key = keys[column_index - 1]
                is_money = any(
                    token in key.lower()
                    for token in ("salary", "amount", "allowance", "deduction", "total", "balance", "price", "cost")
                )
                if is_money:
                    style_id = 6 if row_index % 2 else 7
                else:
                    style_id = 3 if row_index % 2 else 5
            else:
                style_id = 2 if row_index % 2 else 4
            cells.append(_cell_xml(row_index, column_index, value, style_id))
        height = ' ht="28" customHeight="1"' if row_index == header_row else ' ht="21" customHeight="1"'
        row_parts.append(f'<row r="{row_index}"{height}>{"".join(cells)}</row>')
    columns = "".join(
        f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
        for index, width in enumerate(widths, start=1)
    )
    auto_filter = (
        f'<autoFilter ref="A{header_row}:{last_column}{last_row}"/>' if records else ""
    )
    merged = (
        f'<mergeCells count="2"><mergeCell ref="A1:{last_column}1"/>'
        f'<mergeCell ref="A2:{last_column}2"/></mergeCells>'
        if title and len(headers) > 1 else ""
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_column}{last_row}"/>'
        '<sheetViews><sheetView rightToLeft="1" showGridLines="0" workbookViewId="0">'
        f'<pane ySplit="{header_row}" topLeftCell="A{header_row + 1}" activePane="bottomLeft" state="frozen"/>'
        f'<selection pane="bottomLeft" activeCell="A{header_row + 1}" sqref="A{header_row + 1}"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="21"/>'
        f'<cols>{columns}</cols>'
        '<sheetData>' + "".join(row_parts) + '</sheetData>'
        f'{merged}'
        f'{auto_filter}'
        '<printOptions horizontalCentered="1"/>'
        '<pageMargins left="0.25" right="0.25" top="0.5" bottom="0.5" header="0.2" footer="0.2"/>'
        '<pageSetup orientation="landscape" paperSize="9" fitToWidth="1" fitToHeight="0"/>'
        '</worksheet>'
    )


def _safe_sheet_name(name: str, index: int, used: set[str]) -> str:
    translated = SHEET_LABELS.get(str(name), str(name))
    base = _INVALID_SHEET_CHARS.sub(" ", translated).strip().strip("'")[:31] or f"ورقة {index}"
    candidate = base
    suffix = 2
    while candidate.casefold() in used:
        trailer = f" ({suffix})"
        candidate = f"{base[:31 - len(trailer)]}{trailer}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


def _styles_xml() -> str:
    return """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>
  <fonts count="5">
    <font><sz val="11"/><name val="Segoe UI"/><family val="2"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Segoe UI"/><family val="2"/></font>
    <font><color rgb="FF17243C"/><sz val="11"/><name val="Segoe UI"/><family val="2"/></font>
    <font><b/><color rgb="FFFFFFFF"/><sz val="18"/><name val="Segoe UI"/><family val="2"/></font>
    <font><color rgb="FFFFFFFF"/><sz val="10"/><name val="Segoe UI"/><family val="2"/></font>
  </fonts>
  <fills count="5">
    <fill><patternFill patternType="none"/></fill>
    <fill><patternFill patternType="gray125"/></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF102C50"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FFF3F7FA"/><bgColor indexed="64"/></patternFill></fill>
    <fill><patternFill patternType="solid"><fgColor rgb="FF07966F"/><bgColor indexed="64"/></patternFill></fill>
  </fills>
  <borders count="2">
    <border><left/><right/><top/><bottom/><diagonal/></border>
    <border>
      <left style="thin"><color rgb="FFD5DEE8"/></left>
      <right style="thin"><color rgb="FFD5DEE8"/></right>
      <top style="thin"><color rgb="FFD5DEE8"/></top>
      <bottom style="thin"><color rgb="FFD5DEE8"/></bottom>
      <diagonal/>
    </border>
  </borders>
  <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
  <cellXfs count="10">
    <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
    <xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="right" vertical="center" wrapText="1" readingOrder="2"/></xf>
    <xf numFmtId="49" fontId="2" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center" wrapText="1" readingOrder="2"/></xf>
    <xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="49" fontId="2" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="right" vertical="center" wrapText="1" readingOrder="2"/></xf>
    <xf numFmtId="0" fontId="2" fillId="3" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="164" fontId="2" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="164" fontId="2" fillId="3" borderId="1" xfId="0" applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center" vertical="center"/></xf>
    <xf numFmtId="0" fontId="3" fillId="2" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="right" vertical="center" readingOrder="2"/></xf>
    <xf numFmtId="0" fontId="4" fillId="4" borderId="1" xfId="0" applyAlignment="1"><alignment horizontal="right" vertical="center" readingOrder="2"/></xf>
  </cellXfs>
  <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""


def export_xlsx(
    path: Path | str,
    sheets: dict[str, list[dict]],
    company_profile: dict | None = None,
    sheet_titles: dict[str, tuple[str, str]] | None = None,
) -> Path:
    path = Path(path)
    company_name = normalize_company_profile(company_profile)["company_name"]
    sheets = dict(sheets)
    if company_profile is not None and "Company" not in sheets:
        sheets = {"Company": company_profile_rows(company_profile), **sheets}
    workbook_sheets: list[str] = []
    rels: list[str] = []
    content_overrides: list[str] = []
    sheet_payloads: list[tuple[str, str]] = []
    used_names: set[str] = set()

    for index, (sheet_name, records) in enumerate(sheets.items(), start=1):
        safe_name = _safe_sheet_name(sheet_name, index, used_names)
        keys = list(records[0].keys()) if records else ["لا توجد بيانات"]
        headers = [HEADER_LABELS.get(key, str(key).replace("_", " ")) for key in keys]
        workbook_sheets.append(f'<sheet name={quoteattr(safe_name)} sheetId="{index}" r:id="rId{index}"/>')
        rels.append(
            f'<Relationship Id="rId{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{index}.xml"/>'
        )
        content_overrides.append(
            f'<Override PartName="/xl/worksheets/sheet{index}.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )
        title, subtitle = (sheet_titles or {}).get(sheet_name, ("", ""))
        sheet_payloads.append(
            (
                f"xl/worksheets/sheet{index}.xml",
                _sheet_xml(
                    headers, keys, records, title=title, subtitle=subtitle
                ),
            )
        )

    style_relationship_id = len(sheet_payloads) + 1
    rels.append(
        f'<Relationship Id="rId{style_relationship_id}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<bookViews><workbookView/></bookViews><sheets>'
        + "".join(workbook_sheets)
        + '</sheets><calcPr calcId="191029" fullCalcOnLoad="1"/></workbook>'
    )
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(rels)
        + "</Relationships>"
    )
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
        'Target="docProps/app.xml"/></Relationships>'
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        + "".join(content_overrides)
        + "</Types>"
    )
    created = datetime.now().astimezone().isoformat(timespec="seconds")
    core_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f'<dc:title>{escape(APP_NAME)} v{escape(APP_VERSION)} — {escape(RELEASE_CHANNEL)}</dc:title>'
        f'<dc:creator>{escape(company_name)}</dc:creator>'
        f'<cp:lastModifiedBy>{escape(company_name)}</cp:lastModifiedBy>'
        f'<dc:description>تقرير موارد بشرية — {escape(RELEASE_CHANNEL)}</dc:description>'
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{escape(created)}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{escape(created)}</dcterms:modified>'
        '</cp:coreProperties>'
    )
    app_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f'<Application>ProTech HR {escape(APP_VERSION)}</Application>'
        f'<Company>{escape(company_name)}</Company>'
        '<AppVersion>19.0000</AppVersion>'
        '</Properties>'
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", _styles_xml())
        archive.writestr("docProps/core.xml", core_xml)
        archive.writestr("docProps/app.xml", app_xml)
        for filename, payload in sheet_payloads:
            archive.writestr(filename, payload)
    return path


def export_employee_xlsx(path: Path | str, report: dict) -> Path:
    """ينشئ ملف موظف متعدد الأوراق بهوية عربية وتنسيق تنفيذي واضح."""
    employee = dict(report.get("employee") or {})
    attendance = list(report.get("attendance") or [])
    leaves = list(report.get("leaves") or [])
    documents = list(report.get("documents") or [])
    custom_fields = list(report.get("custom_fields") or [])
    company = normalize_company_profile(report.get("company"))
    generated_at = datetime.now()

    status_counts: dict[str, int] = {}
    for row in attendance:
        status = str(row.get("status") or "غير محدد")
        status_counts[status] = status_counts.get(status, 0) + 1
    total_late = sum(int(row.get("late_minutes") or 0) for row in attendance)
    total_worked = sum(int(row.get("worked_minutes") or 0) for row in attendance)

    summary = [
        {"field": "اسم الموظف", "value": employee.get("name") or "—"},
        {"field": "رقم الموظف", "value": employee.get("emp_code") or "—"},
        {"field": "القسم", "value": employee.get("department") or "—"},
        {"field": "المسمى الوظيفي", "value": employee.get("job_title") or "—"},
        {"field": "الحالة", "value": employee.get("status") or "—"},
        {"field": "عدد سجلات الحضور", "value": len(attendance)},
        {"field": "إجمالي التأخير بالدقائق", "value": total_late},
        {"field": "إجمالي ساعات العمل", "value": round(total_worked / 60, 2)},
        {"field": "عدد الإجازات", "value": len(leaves)},
        {"field": "عدد المستندات", "value": len(documents)},
        {"field": "ملخص حالات الحضور", "value": "، ".join(f"{k}: {v}" for k, v in status_counts.items()) or "لا توجد سجلات"},
        {"field": "تاريخ إنشاء التقرير", "value": generated_at.strftime("%Y-%m-%d %H:%M")},
    ]
    detail_fields = (
        ("رقم الموظف", "emp_code", None), ("الاسم", "name", None),
        ("الجوال", "phone", None), ("البريد الإلكتروني", "email", None),
        ("الجنسية", "nationality", None), ("نوع الوثيقة", "id_type", None),
        ("رقم الهوية / الإقامة / الجواز", "id_number", mask_id_number),
        ("بداية الوثيقة", "id_issue_date", None), ("انتهاء الوثيقة", "id_expiry_date", None),
        ("بداية كرت العمل", "work_permit_start_date", None),
        ("انتهاء كرت العمل", "work_permit_expiry_date", None),
        ("بداية التأمين الطبي", "medical_insurance_start_date", None),
        ("انتهاء التأمين الطبي", "medical_insurance_expiry_date", None),
        ("تاريخ الميلاد", "birth_date", None), ("تاريخ الالتحاق", "join_date", None),
        ("القسم", "department", None), ("المسمى الوظيفي", "job_title", None),
        ("مكان العمل", "work_location", None), ("الراتب", "salary", None),
        ("اسم البنك", "bank_name", None),
        ("رقم الحساب البنكي", "bank_account_number", mask_bank_account),
        ("رقم الآيبان", "iban", mask_iban), ("الحالة", "status", None),
        ("التقييم من 5", "performance_rating", None), ("الملاحظات", "notes", None),
    )
    details = []
    for label, key, masker in detail_fields:
        value = employee.get(key)
        if masker:
            value = masker(str(value or ""))
        details.append({"field": label, "value": value if value not in (None, "") else "—"})

    attendance_rows = [
        {
            "attendance_date": row.get("attendance_date"),
            "status": row.get("status"),
            "check_in_time": row.get("check_in_time") or "—",
            "check_out_time": row.get("check_out_time") or "—",
            "late_minutes": int(row.get("late_minutes") or 0),
            "worked_hours": round(int(row.get("worked_minutes") or 0) / 60, 2),
            "notes": row.get("note") or "",
        }
        for row in attendance
    ]
    leave_rows = [
        {
            "leave_type": row.get("leave_type"), "start_date": row.get("start_date"),
            "end_date": row.get("end_date"), "total_days": int(row.get("total_days") or 0),
            "status": row.get("status"), "reason": row.get("reason") or "",
            "notes": row.get("notes") or "",
        }
        for row in leaves
    ]
    document_rows = [
        {
            "document_type": row.get("document_type"), "original_name": row.get("original_name"),
            "start_date": row.get("start_date") or "—", "expiry_date": row.get("expiry_date") or "—",
            "notes": row.get("notes") or "", "created_at": row.get("created_at") or "",
        }
        for row in documents
    ]
    custom_rows = [
        {"label": row.get("label") or "—", "value_text": row.get("value_text") or "—"}
        for row in custom_fields
    ]
    sheets = {
        "EmployeeSummary": summary,
        "EmployeeDetails": details,
        "AttendanceSummary": [
            {"field": "عدد السجلات", "value": len(attendance)},
            {"field": "إجمالي التأخير بالدقائق", "value": total_late},
            {"field": "إجمالي ساعات العمل", "value": round(total_worked / 60, 2)},
            *({"field": key, "value": value} for key, value in status_counts.items()),
        ],
        "Attendance": attendance_rows,
        "Leaves": leave_rows,
        "Documents": document_rows,
        "CustomFields": custom_rows,
        "Company": company_profile_rows(company),
    }
    subtitle = f"{company['company_name']} | أُنشئ في {generated_at:%Y-%m-%d %H:%M} | الإصدار {APP_VERSION}"
    titles = {
        key: (f"تقرير الموظف — {employee.get('name') or employee.get('emp_code') or 'غير محدد'}", subtitle)
        for key in sheets
    }
    return export_xlsx(
        path, sheets, company_profile=company, sheet_titles=titles
    )
