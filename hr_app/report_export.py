from __future__ import annotations

import hashlib
import os
from collections import Counter
from datetime import datetime
from html import escape
from pathlib import Path
import shutil
import subprocess
import tempfile

from .config import (
    APP_NAME, APP_VERSION, COMPANY_NAME, RELEASE_CHANNEL, RESOURCE_DIR, developer_profile,
)
from .company import company_details, normalize_company_profile
from .security import mask_bank_account, mask_iban, mask_id_number


def _safe_number(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _money_text(value) -> str:
    if value in (None, ""):
        return "—"
    return f"{_safe_number(value):,.2f}"


def _report_html(
    employees: list[dict], stats: dict, company_profile: dict | None = None
) -> str:
    profile = developer_profile()
    company = normalize_company_profile(company_profile)
    company_name = company["company_name"]
    company_information = company_details(company)
    rows: list[str] = []
    for index, emp in enumerate(employees, start=1):
        rows.append(
            "<tr>"
            f"<td class=\"num\">{index}</td>"
            f"<td class=\"code\">{escape(str(emp.get('emp_code', '')))}</td>"
            f"<td class=\"name\">{escape(str(emp.get('name', '')))}</td>"
            f"<td>{escape(str(emp.get('department', '')))}</td>"
            f"<td>{escape(str(emp.get('job_title', '')))}</td>"
            f"<td class=\"money\">{_money_text(emp.get('salary'))}</td>"
            f"<td class=\"status\">{escape(str(emp.get('status', '')))}</td>"
            "</tr>"
        )
    contact_parts = [
        escape(str(profile.get(key, "")))
        for key in ("phone", "email", "website")
        if profile.get(key, "")
    ]
    contact = " | ".join(contact_parts)
    generated_at = datetime.now()
    html = f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8">
<title>{escape(APP_NAME)} v{escape(APP_VERSION)} - تقرير الموارد البشرية</title>
<style>
@page {{
  size: A4 landscape;
  margin: 12mm 11mm 13mm;
}}
* {{ box-sizing: border-box; }}
body {{
  font-family: "Segoe UI", Tahoma, Arial, sans-serif;
  margin: 0;
  color: #17243c;
  background: #fff;
  font-size: 10.5px;
  line-height: 1.45;
}}
.screen-button {{
  border: 0; border-radius: 7px; background: #126ba8; color: white;
  padding: 9px 18px; font: inherit; cursor: pointer; margin-bottom: 12px;
}}
.header {{
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: 18px; border-bottom: 3px solid #1597b8; padding-bottom: 10px;
}}
.title h1 {{ margin: 0; color: #102c50; font-size: 23px; }}
.title .company {{ color: #1597b8; font-weight: 700; font-size: 13px; margin-top: 2px; }}
.title .company-details {{ color: #596a80; font-size: 8.5px; margin-top: 2px; max-width: 680px; }}
.meta {{ text-align: left; color: #53657c; font-size: 10px; white-space: nowrap; }}
.cards {{ display: grid; grid-template-columns: repeat(5, 1fr); gap: 8px; margin: 12px 0 14px; }}
.card {{
  border: 1px solid #cfdae7; border-radius: 8px; padding: 8px 10px;
  background: #f5f9fc; min-height: 57px;
}}
.card span {{ display: block; color: #607287; font-size: 9.5px; }}
.card b {{ display: block; color: #102c50; font-size: 17px; margin-top: 2px; }}
h2 {{ color: #102c50; font-size: 15px; margin: 0 0 7px; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
thead {{ display: table-header-group; }}
tr {{ page-break-inside: avoid; }}
th {{
  background: #102c50; color: white; font-weight: 700; border: 1px solid #102c50;
  padding: 7px 5px; text-align: right;
}}
td {{
  border: 1px solid #d5dee8; padding: 6px 5px; text-align: right;
  vertical-align: middle; overflow-wrap: anywhere;
}}
tbody tr:nth-child(even) {{ background: #f5f8fb; }}
.num {{ width: 4%; text-align: center; }}
.code {{ width: 9%; direction: ltr; text-align: center; }}
.name {{ width: 22%; font-weight: 600; }}
.money {{ width: 10%; direction: ltr; text-align: left; font-variant-numeric: tabular-nums; }}
.status {{ width: 9%; text-align: center; }}
footer {{
  margin-top: 10px; border-top: 1px solid #cfdae7; padding-top: 6px;
  display: flex; justify-content: space-between; gap: 12px; color: #6a788b; font-size: 8.5px;
}}
.stamp {{ direction: ltr; text-align: left; overflow-wrap: anywhere; max-width: 56%; }}
@media print {{
  .screen-button {{ display: none; }}
  body {{ print-color-adjust: exact; -webkit-print-color-adjust: exact; }}
}}
</style>
</head>
<body>
<button class="screen-button" onclick="window.print()">طباعة التقرير</button>
<section class="header">
  <div class="title">
    <h1>{escape(APP_NAME)}</h1>
    <div class="company">{escape(company_name)}</div>
    {f'<div class="company-details">{escape(company_information)}</div>' if company_information else ''}
  </div>
  <div class="meta">
    <strong>تقرير الموارد البشرية</strong><br>
    الإصدار: {escape(APP_VERSION)} — {escape(RELEASE_CHANNEL)}<br>
    تاريخ الإنشاء: {generated_at:%Y-%m-%d}<br>
    الوقت: {generated_at:%H:%M}
  </div>
</section>
<section class="cards">
  <div class="card"><span>إجمالي الموظفين</span><b>{stats.get('total', 0)}</b></div>
  <div class="card"><span>الموظفون النشطون</span><b>{stats.get('active', 0) or 0}</b></div>
  <div class="card"><span>إجمالي الرواتب</span><b>{_safe_number(stats.get('salaries')):,.2f}</b></div>
  <div class="card"><span>متوسط الراتب</span><b>{_safe_number(stats.get('avg_salary')):,.2f}</b></div>
  <div class="card"><span>في إجازة اليوم</span><b>{stats.get('leave_today', 0) or 0}</b></div>
</section>
<h2>قائمة الموظفين</h2>
<table>
  <thead>
    <tr>
      <th class="num">م</th><th class="code">رقم الموظف</th><th class="name">الاسم</th>
      <th>القسم</th><th>المسمى الوظيفي</th><th class="money">الراتب</th><th class="status">الحالة</th>
    </tr>
  </thead>
  <tbody>{''.join(rows) if rows else '<tr><td colspan="7" style="text-align:center;padding:18px">لا توجد بيانات موظفين.</td></tr>'}</tbody>
</table>
<footer>
  <span>تم التطوير بواسطة {escape(str(profile.get('developer') or COMPANY_NAME))}{(' | ' + contact) if contact else ''} — v{escape(APP_VERSION)} — {escape(RELEASE_CHANNEL)}</span>
  __PROTECH_STAMP__
</footer>
</body>
</html>"""
    stamp = hashlib.sha256(html.encode("utf-8")).hexdigest()
    return html.replace("__PROTECH_STAMP__", f'<span class="stamp">SHA-256: {stamp}</span>')


def export_printable_html(
    path: Path | str,
    employees: list[dict],
    stats: dict,
    company_profile: dict | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        _report_html(employees, stats, company_profile=company_profile), encoding="utf-8"
    )
    return path


def _employee_report_html(report: dict) -> str:
    employee = dict(report.get("employee") or {})
    attendance = list(report.get("attendance") or [])
    leaves = list(report.get("leaves") or [])
    documents = list(report.get("documents") or [])
    custom_fields = list(report.get("custom_fields") or [])
    company = normalize_company_profile(report.get("company"))
    company_name = company["company_name"]
    company_information = company_details(company)
    profile = developer_profile()
    generated_at = datetime.now()
    status_counts = Counter(str(row.get("status") or "غير محدد") for row in attendance)
    late_minutes = sum(int(row.get("late_minutes") or 0) for row in attendance)
    worked_minutes = sum(int(row.get("worked_minutes") or 0) for row in attendance)

    def value(key: str, default: str = "—") -> str:
        raw = employee.get(key)
        return escape(str(raw if raw not in (None, "") else default))

    identity = escape(mask_id_number(str(employee.get("id_number") or "")) or "—")
    bank_account = escape(mask_bank_account(str(employee.get("bank_account_number") or "")) or "—")
    iban = escape(mask_iban(str(employee.get("iban") or "")) or "—")
    salary = _money_text(employee.get("salary"))
    attendance_rows = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('attendance_date') or ''))}</td>"
        f"<td>{escape(str(row.get('status') or ''))}</td>"
        f"<td>{escape(str(row.get('check_in_time') or '—'))}</td>"
        f"<td>{escape(str(row.get('check_out_time') or '—'))}</td>"
        f"<td>{int(row.get('late_minutes') or 0)}</td>"
        f"<td>{escape(str(row.get('note') or ''))}</td>"
        "</tr>"
        for row in attendance
    )
    leave_rows = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('leave_type') or ''))}</td>"
        f"<td>{escape(str(row.get('start_date') or ''))}</td>"
        f"<td>{escape(str(row.get('end_date') or ''))}</td>"
        f"<td>{int(row.get('total_days') or 0)}</td>"
        f"<td>{escape(str(row.get('status') or ''))}</td>"
        f"<td>{escape(str(row.get('reason') or ''))}</td>"
        "</tr>"
        for row in leaves
    )
    document_rows = "".join(
        "<tr>"
        f"<td>{escape(str(row.get('document_type') or ''))}</td>"
        f"<td>{escape(str(row.get('original_name') or ''))}</td>"
        f"<td>{escape(str(row.get('start_date') or '—'))}</td>"
        f"<td>{escape(str(row.get('expiry_date') or '—'))}</td>"
        f"<td>{escape(str(row.get('notes') or ''))}</td>"
        "</tr>"
        for row in documents
    )
    attendance_summary = "، ".join(
        f"{escape(label)}: {count}" for label, count in sorted(status_counts.items())
    ) or "لا توجد سجلات"
    custom_fields_html = "".join(
        '<div class="field"><b>'
        + escape(str(field.get("label") or ""))
        + "</b>"
        + escape(str(field.get("value_text") or "—"))
        + "</div>"
        for field in custom_fields
    )
    html = f"""<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<title>تقرير الموظف - {value('name')}</title>
<style>
@page {{ size: A4 portrait; margin: 12mm; }}
* {{ box-sizing: border-box; }}
body {{ font-family:"Segoe UI",Tahoma,Arial,sans-serif; color:#17243c; margin:0; font-size:10px; line-height:1.5; }}
.header {{ border-bottom:3px solid #1597b8; padding-bottom:9px; display:flex; justify-content:space-between; }}
h1 {{ margin:0; color:#102c50; font-size:22px; }} h2 {{ color:#102c50; font-size:14px; margin:14px 0 6px; }}
.brand {{ color:#1597b8; font-weight:700; }} .meta {{ text-align:left; color:#596a80; }}
.company-details {{ color:#596a80; font-size:8.5px; max-width:620px; }}
.grid {{ display:grid; grid-template-columns:repeat(2,1fr); gap:6px 12px; margin-top:12px; }}
.field {{ border:1px solid #d5dee8; background:#f7fafc; padding:6px 8px; }}
.field b {{ color:#607287; display:inline-block; min-width:120px; }}
.cards {{ display:grid; grid-template-columns:repeat(3,1fr); gap:7px; margin-top:8px; }}
.card {{ border:1px solid #cbd8e6; border-radius:7px; padding:7px; background:#f4f9fc; }}
.card b {{ display:block; color:#102c50; font-size:15px; }}
table {{ width:100%; border-collapse:collapse; table-layout:fixed; }}
thead {{ display:table-header-group; }} tr {{ page-break-inside:avoid; }}
th {{ background:#102c50; color:white; padding:6px 4px; text-align:right; border:1px solid #102c50; }}
td {{ border:1px solid #d5dee8; padding:5px 4px; text-align:right; overflow-wrap:anywhere; }}
tbody tr:nth-child(even) {{ background:#f6f9fc; }}
footer {{ margin-top:12px; border-top:1px solid #d5dee8; padding-top:6px; color:#6a788b; font-size:8px; }}
</style></head><body>
<section class="header"><div><h1>تقرير موظف</h1><div class="brand">{escape(company_name)}</div>
{f'<div class="company-details">{escape(company_information)}</div>' if company_information else ''}</div>
<div class="meta">{generated_at:%Y-%m-%d}<br>{generated_at:%H:%M}</div></section>
<section class="grid">
<div class="field"><b>رقم الموظف</b>{value('emp_code')}</div><div class="field"><b>الاسم</b>{value('name')}</div>
<div class="field"><b>القسم</b>{value('department')}</div><div class="field"><b>المسمى الوظيفي</b>{value('job_title')}</div>
<div class="field"><b>الجنسية</b>{value('nationality')}</div><div class="field"><b>نوع الوثيقة</b>{value('id_type')}</div>
<div class="field"><b>رقم الهوية / الإقامة / الجواز</b>{identity}</div><div class="field"><b>بداية الوثيقة</b>{value('id_issue_date')}</div>
<div class="field"><b>انتهاء الوثيقة</b>{value('id_expiry_date')}</div><div class="field"><b>بداية كرت العمل</b>{value('work_permit_start_date')}</div>
<div class="field"><b>انتهاء كرت العمل</b>{value('work_permit_expiry_date')}</div><div class="field"><b>بداية التأمين الطبي</b>{value('medical_insurance_start_date')}</div>
<div class="field"><b>انتهاء التأمين الطبي</b>{value('medical_insurance_expiry_date')}</div><div class="field"><b>اسم البنك</b>{value('bank_name')}</div>
<div class="field"><b>رقم الحساب</b>{bank_account}</div><div class="field"><b>الآيبان</b>{iban}</div>
<div class="field"><b>تاريخ الانضمام</b>{value('join_date')}</div><div class="field"><b>الحالة</b>{value('status')}</div>
<div class="field"><b>موقع العمل</b>{value('work_location')}</div><div class="field"><b>الراتب</b>{salary}</div>
<div class="field"><b>الجوال</b>{value('phone')}</div><div class="field"><b>البريد الإلكتروني</b>{value('email')}</div>
</section>
{f'<h2>البيانات المخصصة</h2><section class="grid">{custom_fields_html}</section>' if custom_fields_html else ''}
<h2>ملخص الحضور</h2><section class="cards">
<div class="card">عدد السجلات<b>{len(attendance)}</b></div>
<div class="card">إجمالي التأخير<b>{late_minutes} دقيقة</b></div>
<div class="card">إجمالي ساعات العمل<b>{worked_minutes / 60:.1f}</b></div>
</section><div class="field" style="margin-top:7px"><b>الحالات</b>{attendance_summary}</div>
<h2>آخر سجلات الحضور</h2><table><thead><tr><th>التاريخ</th><th>الحالة</th><th>الدخول</th><th>الخروج</th><th>التأخير</th><th>الملاحظة</th></tr></thead>
<tbody>{attendance_rows or '<tr><td colspan="6" style="text-align:center">لا توجد سجلات حضور.</td></tr>'}</tbody></table>
<h2>سجل الإجازات</h2><table><thead><tr><th>النوع</th><th>من</th><th>إلى</th><th>الأيام</th><th>الحالة</th><th>السبب</th></tr></thead>
<tbody>{leave_rows or '<tr><td colspan="6" style="text-align:center">لا توجد سجلات إجازات.</td></tr>'}</tbody></table>
<h2>وثائق الموظف</h2><table><thead><tr><th>النوع</th><th>الملف</th><th>البداية</th><th>النهاية</th><th>الملاحظات</th></tr></thead>
<tbody>{document_rows or '<tr><td colspan="5" style="text-align:center">لا توجد وثائق محفوظة.</td></tr>'}</tbody></table>
<h2>ملاحظات الموظف</h2><div class="field">{value('notes', 'لا توجد ملاحظات')}</div>
<footer>تم التطوير بواسطة {escape(str(profile.get('developer') or COMPANY_NAME))} | {escape(APP_NAME)} | v{escape(APP_VERSION)} — {escape(RELEASE_CHANNEL)}</footer>
</body></html>"""
    return html


def _find_chromium_browsers() -> list[Path]:
    roots: list[Path] = []
    for environment_name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        root = os.environ.get(environment_name, "").strip()
        if root:
            roots.append(Path(root))
    # Prefer Edge across every Windows installation root before Chrome.  The
    # previous interleaved order could select Chrome first and fail on systems
    # where its GPU process or profile cache was locked.
    candidates = [root / "Microsoft" / "Edge" / "Application" / "msedge.exe" for root in roots]
    candidates.extend(root / "Google" / "Chrome" / "Application" / "chrome.exe" for root in roots)
    for executable in ("msedge.exe", "chrome.exe"):
        resolved = shutil.which(executable)
        if resolved:
            candidates.append(Path(resolved))
    found: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).casefold()
        if key not in seen and candidate.is_file():
            seen.add(key)
            found.append(candidate)
    return found


def _find_chromium_browser() -> Path | None:
    browsers = _find_chromium_browsers()
    return browsers[0] if browsers else None


def _render_html_to_pdf(path: Path, html: str) -> Path:
    browsers = _find_chromium_browsers()
    if not browsers:
        raise RuntimeError("تعذر العثور على Microsoft Edge أو Google Chrome لإنشاء ملف PDF.")

    errors: list[str] = []
    # Keep the temporary browser profile beside the target report.  Some
    # managed Windows installations restrict Chromium subprocesses in the
    # global Temp folder and make the GPU cache fail even though the report
    # folder itself is writable.
    with tempfile.TemporaryDirectory(prefix=".protech_report_", dir=str(path.parent)) as temporary:
        temporary_path = Path(temporary)
        html_path = temporary_path / "report.html"
        html_path.write_text(html, encoding="utf-8")
        for index, browser in enumerate(browsers, start=1):
            if path.exists():
                path.unlink()
            profile_path = temporary_path / f"browser_profile_{index}"
            command = [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--disable-gpu-compositing",
                "--disable-gpu-shader-disk-cache",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-dev-shm-usage",
                "--no-first-run",
                "--no-default-browser-check",
                "--no-pdf-header-footer",
                f"--user-data-dir={profile_path}",
                f"--disk-cache-dir={profile_path / 'disk_cache'}",
                f"--print-to-pdf={path.resolve()}",
                html_path.resolve().as_uri(),
            ]
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    creationflags=creation_flags,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                errors.append(f"{browser.name}: {exc}")
                continue
            if completed.returncode == 0 and path.is_file() and path.stat().st_size >= 1024:
                return path
            detail = (completed.stderr or completed.stdout or "").strip()
            errors.append(f"{browser.name}: {detail[-500:] or 'فشل غير محدد'}")
    raise RuntimeError("تعذر إنشاء ملف PDF مباشرة. " + " | ".join(errors[-2:]))


def _rtl_text(value) -> str:
    text = str(value if value not in (None, "") else "—")
    try:
        from arabic_reshaper import reshape
        from bidi.algorithm import get_display
        return get_display(reshape(text))
    except Exception:
        return text


def _pdf_styles():
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font_name = "Helvetica"
    windows_root = Path(os.environ.get("WINDIR", r"C:\Windows"))
    candidates = (
        RESOURCE_DIR / "assets" / "fonts" / "DejaVuSans.ttf",
        windows_root / "Fonts" / "tahoma.ttf",
        windows_root / "Fonts" / "arial.ttf",
    )
    for candidate in candidates:
        if candidate.is_file():
            font_name = "ProTechArabic"
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(candidate)))
            break
    base = getSampleStyleSheet()
    body = ParagraphStyle(
        "ProTechBody", parent=base["BodyText"], fontName=font_name,
        fontSize=8.2, leading=10.5, alignment=TA_RIGHT, wordWrap="RTL",
    )
    small = ParagraphStyle(
        "ProTechSmall", parent=body, fontSize=7.2, leading=8.8,
    )
    title = ParagraphStyle(
        "ProTechTitle", parent=body, fontSize=19, leading=23,
        textColor="#102c50", alignment=TA_CENTER, spaceAfter=5,
    )
    heading = ParagraphStyle(
        "ProTechHeading", parent=body, fontSize=12, leading=15,
        textColor="#102c50", spaceBefore=8, spaceAfter=5,
    )
    return font_name, body, small, title, heading


def _pdf_paragraph(value, style):
    from reportlab.platypus import Paragraph
    return Paragraph(escape(_rtl_text(value)), style)


def _pdf_table(rows, widths, body_style, *, header=True):
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Table, TableStyle

    header_style = ParagraphStyle(
        f"{body_style.name}Header", parent=body_style, textColor=colors.white
    )
    converted = []
    for row_index, row in enumerate(rows):
        style = header_style if header and row_index == 0 else body_style
        converted.append([_pdf_paragraph(value, style) for value in row])
    table = Table(converted, colWidths=widths, repeatRows=1 if header else 0, hAlign="RIGHT")
    commands = [
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cad6e4")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    first_data_row = 1 if header else 0
    if header:
        commands.extend(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#102c50")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), body_style.fontName),
            ]
        )
    for index in range(first_data_row, len(rows)):
        if (index - first_data_row) % 2:
            commands.append(("BACKGROUND", (0, index), (-1, index), colors.HexColor("#f4f8fb")))
    table.setStyle(TableStyle(commands))
    return table


def _pdf_page_footer(canvas, doc, font_name: str, company_name: str):
    from reportlab.lib import colors

    canvas.saveState()
    canvas.setStrokeColor(colors.HexColor("#cad6e4"))
    canvas.line(doc.leftMargin, 22, doc.pagesize[0] - doc.rightMargin, 22)
    canvas.setFillColor(colors.HexColor("#66758a"))
    canvas.setFont(font_name, 7.5)
    canvas.drawRightString(
        doc.pagesize[0] - doc.rightMargin,
        11,
        _rtl_text(f"{company_name} — v{APP_VERSION} — {RELEASE_CHANNEL}"),
    )
    canvas.drawString(doc.leftMargin, 11, f"{doc.page}")
    canvas.restoreState()


def _export_general_reportlab(
    path: Path,
    employees: list[dict],
    stats: dict,
    company_profile: dict | None = None,
) -> Path:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Spacer

    font_name, body, small, title, heading = _pdf_styles()
    company = normalize_company_profile(company_profile)
    company_name = company["company_name"]
    company_information = company_details(company)
    doc = SimpleDocTemplate(
        str(path), pagesize=landscape(A4), rightMargin=10 * mm, leftMargin=10 * mm,
        topMargin=9 * mm, bottomMargin=12 * mm, title="ProTech HR Report",
        author=company_name,
    )
    story = [
        _pdf_paragraph("تقرير الموارد البشرية", title),
        _pdf_paragraph(f"{company_name} — {datetime.now():%Y-%m-%d %H:%M}", body),
        Spacer(1, 5),
    ]
    if company_information:
        story.extend([_pdf_paragraph(company_information, small), Spacer(1, 4)])
    summary = [
        ["إجمالي الموظفين", "الموظفون النشطون", "إجمالي الرواتب", "متوسط الراتب", "في إجازة اليوم"],
        [
            stats.get("total", 0), stats.get("active", 0) or 0,
            f"{_safe_number(stats.get('salaries')):,.2f}",
            f"{_safe_number(stats.get('avg_salary')):,.2f}", stats.get("leave_today", 0) or 0,
        ],
    ]
    story.extend([_pdf_table(summary, [53 * mm] * 5, body), Spacer(1, 8), _pdf_paragraph("قائمة الموظفين", heading)])
    rows = [["م", "رقم الموظف", "الاسم", "القسم", "المسمى الوظيفي", "الراتب", "الحالة"]]
    rows.extend(
        [
            index, employee.get("emp_code", ""), employee.get("name", ""),
            employee.get("department", ""), employee.get("job_title", ""),
            _money_text(employee.get("salary")), employee.get("status", ""),
        ]
        for index, employee in enumerate(employees, start=1)
    )
    if len(rows) == 1:
        rows.append(["—", "—", "لا توجد بيانات موظفين", "—", "—", "—", "—"])
    story.append(_pdf_table(rows, [10 * mm, 25 * mm, 56 * mm, 45 * mm, 50 * mm, 30 * mm, 30 * mm], small))
    doc.build(
        story,
        onFirstPage=lambda canvas, current: _pdf_page_footer(canvas, current, font_name, company_name),
        onLaterPages=lambda canvas, current: _pdf_page_footer(canvas, current, font_name, company_name),
    )
    if not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError("تعذر إنشاء ملف PDF.")
    return path


def _export_employee_reportlab(path: Path, report: dict) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Spacer

    employee = dict(report.get("employee") or {})
    attendance = list(report.get("attendance") or [])
    leaves = list(report.get("leaves") or [])
    documents = list(report.get("documents") or [])
    custom_fields = list(report.get("custom_fields") or [])
    company = normalize_company_profile(report.get("company"))
    company_name = company["company_name"]
    company_information = company_details(company)
    font_name, body, small, title, heading = _pdf_styles()
    doc = SimpleDocTemplate(
        str(path), pagesize=A4, rightMargin=10 * mm, leftMargin=10 * mm,
        topMargin=9 * mm, bottomMargin=12 * mm, title=f"Employee {employee.get('emp_code', '')}",
        author=company_name,
    )
    story = [
        _pdf_paragraph("تقرير الموظف", title),
        _pdf_paragraph(f"{company_name} — {datetime.now():%Y-%m-%d %H:%M}", body),
        Spacer(1, 5),
    ]
    if company_information:
        story.extend([_pdf_paragraph(company_information, small), Spacer(1, 4)])
    details = [
        ["رقم الموظف", employee.get("emp_code"), "الاسم", employee.get("name")],
        ["القسم", employee.get("department"), "المسمى الوظيفي", employee.get("job_title")],
        ["الجنسية", employee.get("nationality"), "نوع الوثيقة", employee.get("id_type")],
        ["رقم الهوية / الإقامة / الجواز", mask_id_number(str(employee.get("id_number") or "")), "بداية الوثيقة", employee.get("id_issue_date")],
        ["انتهاء الوثيقة", employee.get("id_expiry_date"), "بداية كرت العمل", employee.get("work_permit_start_date")],
        [
            "انتهاء كرت العمل", employee.get("work_permit_expiry_date"),
            "بداية التأمين الطبي", employee.get("medical_insurance_start_date"),
        ],
        ["انتهاء التأمين الطبي", employee.get("medical_insurance_expiry_date"), "اسم البنك", employee.get("bank_name")],
        [
            "رقم الحساب", mask_bank_account(str(employee.get("bank_account_number") or "")),
            "الآيبان", mask_iban(str(employee.get("iban") or "")),
        ],
        ["تاريخ الانضمام", employee.get("join_date"), "الحالة", employee.get("status")],
        ["موقع العمل", employee.get("work_location"), "الراتب", _money_text(employee.get("salary"))],
        ["الجوال", employee.get("phone"), "البريد الإلكتروني", employee.get("email")],
    ]
    story.extend([_pdf_table(details, [35 * mm, 58 * mm, 35 * mm, 58 * mm], body, header=False)])
    if custom_fields:
        custom_rows = []
        for index in range(0, len(custom_fields), 2):
            left = custom_fields[index]
            right = custom_fields[index + 1] if index + 1 < len(custom_fields) else {}
            custom_rows.append(
                [
                    left.get("label") or "—",
                    left.get("value_text") or "—",
                    right.get("label") or "—",
                    right.get("value_text") or "—",
                ]
            )
        story.extend(
            [
                _pdf_paragraph("البيانات المخصصة", heading),
                _pdf_table(custom_rows, [35 * mm, 58 * mm, 35 * mm, 58 * mm], body, header=False),
            ]
        )
    status_counts = Counter(str(row.get("status") or "غير محدد") for row in attendance)
    summary_text = "، ".join(f"{key}: {value}" for key, value in sorted(status_counts.items())) or "لا توجد سجلات"
    story.extend(
        [
            _pdf_paragraph("ملخص الحضور", heading),
            _pdf_table(
                [
                    ["عدد السجلات", "إجمالي التأخير", "إجمالي ساعات العمل", "الحالات"],
                    [
                        len(attendance), f"{sum(int(row.get('late_minutes') or 0) for row in attendance)} دقيقة",
                        f"{sum(int(row.get('worked_minutes') or 0) for row in attendance) / 60:.1f}",
                        summary_text,
                    ],
                ],
                [30 * mm, 40 * mm, 40 * mm, 76 * mm], small,
            ),
            _pdf_paragraph("سجل الحضور", heading),
        ]
    )
    attendance_rows = [["التاريخ", "الحالة", "الدخول", "الخروج", "التأخير", "الملاحظة"]]
    attendance_rows.extend(
        [
            row.get("attendance_date"), row.get("status"), row.get("check_in_time") or "—",
            row.get("check_out_time") or "—", int(row.get("late_minutes") or 0), row.get("note") or "",
        ]
        for row in attendance
    )
    if len(attendance_rows) == 1:
        attendance_rows.append(["—", "لا توجد سجلات حضور", "—", "—", "—", "—"])
    story.extend(
        [
            _pdf_table(attendance_rows, [26 * mm, 28 * mm, 25 * mm, 25 * mm, 23 * mm, 59 * mm], small),
            _pdf_paragraph("سجل الإجازات", heading),
        ]
    )
    leave_rows = [["النوع", "من", "إلى", "الأيام", "الحالة", "السبب"]]
    leave_rows.extend(
        [
            row.get("leave_type"), row.get("start_date"), row.get("end_date"),
            int(row.get("total_days") or 0), row.get("status"), row.get("reason") or "",
        ]
        for row in leaves
    )
    if len(leave_rows) == 1:
        leave_rows.append(["—", "—", "—", "—", "لا توجد إجازات", "—"])
    document_rows = [["النوع", "اسم الملف", "البداية", "النهاية", "الملاحظات"]]
    document_rows.extend(
        [
            row.get("document_type"), row.get("original_name"), row.get("start_date") or "—",
            row.get("expiry_date") or "—", row.get("notes") or "",
        ]
        for row in documents
    )
    if len(document_rows) == 1:
        document_rows.append(["—", "لا توجد وثائق", "—", "—", "—"])
    story.extend(
        [
            _pdf_table(leave_rows, [30 * mm, 27 * mm, 27 * mm, 20 * mm, 30 * mm, 52 * mm], small),
            _pdf_paragraph("وثائق الموظف", heading),
            _pdf_table(document_rows, [30 * mm, 55 * mm, 27 * mm, 27 * mm, 47 * mm], small),
            _pdf_paragraph("ملاحظات الموظف", heading),
            _pdf_table([["الملاحظات", employee.get("notes") or "لا توجد ملاحظات"]], [35 * mm, 151 * mm], body, header=False),
        ]
    )
    doc.build(
        story,
        onFirstPage=lambda canvas, current: _pdf_page_footer(canvas, current, font_name, company_name),
        onLaterPages=lambda canvas, current: _pdf_page_footer(canvas, current, font_name, company_name),
    )
    if not path.is_file() or path.stat().st_size < 1024:
        raise RuntimeError("تعذر إنشاء تقرير الموظف PDF.")
    return path


def export_printable_pdf(
    path: Path | str,
    employees: list[dict],
    stats: dict,
    company_profile: dict | None = None,
) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return _export_general_reportlab(
        path, employees, stats, company_profile=company_profile
    )


def export_employee_pdf(path: Path | str, report: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return _export_employee_reportlab(path, report)


def export_workforce_analytics_pdf(
    path: Path | str,
    analytics: dict,
    company_profile: dict | None = None,
) -> Path:
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate, Spacer

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    font_name, body, small, title, heading = _pdf_styles()
    company = normalize_company_profile(company_profile)
    company_name = company["company_name"]
    doc = SimpleDocTemplate(
        str(target), pagesize=landscape(A4), rightMargin=10 * mm, leftMargin=10 * mm,
        topMargin=9 * mm, bottomMargin=12 * mm, title="ProTech HR Smart Analytics",
        author=company_name,
    )
    filter_text = f"الفترة: {analytics.get('start_date', '')} إلى {analytics.get('end_date', '')}"
    if analytics.get("department"):
        filter_text += f" — القسم: {analytics['department']}"
    story = [
        _pdf_paragraph("التحليل الذكي للقوى العاملة", title),
        _pdf_paragraph(f"{company_name} — {filter_text}", body),
        Spacer(1, 6),
    ]
    summary = [
        ["الموظفون", "النشطون", "سجلات الحضور", "نسبة الحضور", "الغياب", "أيام الإجازة"],
        [
            analytics.get("employees", 0), analytics.get("active", 0),
            analytics.get("attendance_records", 0),
            f"{_safe_number(analytics.get('attendance_rate')):.1f}%",
            analytics.get("absent", 0), analytics.get("leave_days", 0),
        ],
    ]
    story.extend([_pdf_table(summary, [43 * mm] * 6, body), Spacer(1, 8)])
    insights = analytics.get("insights") or []
    story.append(_pdf_paragraph("قراءة المحرك المحلي", heading))
    if insights:
        insight_rows = [["المؤشر", "النتيجة"]]
        insight_rows.extend(
            [[item.get("title", ""), item.get("message", "")] for item in insights]
        )
        story.extend([_pdf_table(insight_rows, [55 * mm, 203 * mm], small), Spacer(1, 8)])
    people_rows = [["رقم الموظف", "الموظف", "القسم", "السجلات", "الحضور", "الغياب"]]
    people_rows.extend(
        [
            item.get("emp_code", ""), item.get("name", ""), item.get("department", ""),
            item.get("records", 0), item.get("attended", 0), item.get("absent", 0),
        ]
        for item in (analytics.get("people") or [])
    )
    if len(people_rows) == 1:
        people_rows.append(["—", "لا توجد بيانات", "—", "—", "—", "—"])
    story.extend([
        _pdf_paragraph("تفصيل حسب الموظف", heading),
        _pdf_table(people_rows, [30 * mm, 67 * mm, 58 * mm, 30 * mm, 30 * mm, 30 * mm], small),
    ])
    doc.build(
        story,
        onFirstPage=lambda canvas, current: _pdf_page_footer(canvas, current, font_name, company_name),
        onLaterPages=lambda canvas, current: _pdf_page_footer(canvas, current, font_name, company_name),
    )
    if not target.is_file() or target.stat().st_size < 1024:
        raise RuntimeError("تعذر إنشاء تقرير التحليل الذكي PDF.")
    return target
