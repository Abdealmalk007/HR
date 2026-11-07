from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urlencode

from .config import COMPANY_NAME, EXPORT_DIR
from .company import normalize_company_profile

EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

PLACEHOLDERS = {
    "name": "اسم الموظف",
    "emp_code": "رقم الموظف",
    "department": "القسم",
    "job_title": "المسمى الوظيفي",
    "company": "اسم الشركة",
    "company_legal_name": "الاسم القانوني للشركة",
    "company_phone": "هاتف الشركة",
    "company_email": "بريد الشركة",
    "date": "تاريخ اليوم",
    "phone": "رقم الجوال",
    "email": "البريد الإلكتروني",
}


def is_valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match(str(value or "").strip()))


def normalize_whatsapp_phone(value: str, default_country_code: str = "966") -> str:
    """يعيد رقمًا دوليًا بلا علامة + مناسبًا لواتساب.

    الأرقام المحلية السعودية 05xxxxxxxx تتحول إلى 9665xxxxxxxx.
    الأرقام الدولية الأخرى تُحفظ إذا كانت بطول مقبول ولا تبدأ بصفر.
    """
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 10 and digits.startswith("05"):
        digits = default_country_code + digits[1:]
    elif len(digits) == 9 and digits.startswith("5"):
        digits = default_country_code + digits
    if not (8 <= len(digits) <= 15) or digits.startswith("0"):
        return ""
    return digits


def template_context(
    employee: dict[str, Any] | None = None,
    *,
    group: bool = False,
    company_profile: dict[str, Any] | None = None,
) -> dict[str, str]:
    employee = employee or {}
    company = normalize_company_profile(company_profile)
    return {
        "name": "الزملاء والزميلات" if group else str(employee.get("name") or ""),
        "emp_code": "" if group else str(employee.get("emp_code") or ""),
        "department": "" if group else str(employee.get("department") or ""),
        "job_title": "" if group else str(employee.get("job_title") or ""),
        "company": company.get("company_name") or COMPANY_NAME,
        "company_legal_name": company.get("legal_name") or company.get("company_name") or COMPANY_NAME,
        "company_phone": company.get("phone") or "",
        "company_email": company.get("email") or "",
        "date": date.today().isoformat(),
        "phone": "" if group else str(employee.get("phone") or ""),
        "email": "" if group else str(employee.get("email") or ""),
    }


def render_template(
    text: str,
    employee: dict[str, Any] | None = None,
    *,
    group: bool = False,
    company_profile: dict[str, Any] | None = None,
) -> str:
    context = template_context(
        employee, group=group, company_profile=company_profile
    )

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return context.get(key, match.group(0))

    return PLACEHOLDER_RE.sub(replace, str(text or ""))


def build_mailto_url(
    recipients: Iterable[str], subject: str, body: str, *, use_bcc: bool = False
) -> str:
    emails = [str(item).strip() for item in recipients if is_valid_email(str(item))]
    if not emails:
        raise ValueError("لا يوجد بريد إلكتروني صالح ضمن المستلمين.")
    params: dict[str, str] = {"subject": subject or "", "body": body or ""}
    if use_bcc or len(emails) > 1:
        target = ""
        params["bcc"] = ",".join(emails)
    else:
        target = quote(emails[0], safe="@._+-")
    return f"mailto:{target}?{urlencode(params, quote_via=quote)}"


def build_whatsapp_url(phone: str, body: str, *, desktop: bool = False) -> str:
    normalized = normalize_whatsapp_phone(phone)
    if not normalized:
        raise ValueError("رقم الجوال غير صالح لواتساب.")
    scheme = "whatsapp://send" if desktop else f"https://wa.me/{normalized}"
    if desktop:
        return f"{scheme}?phone={normalized}&text={quote(body or '', safe='')}"
    return f"{scheme}?text={quote(body or '', safe='')}"


def create_eml_draft(
    recipients: Iterable[str], subject: str, body: str, *, use_bcc: bool = False,
    output_dir: Path | str = EXPORT_DIR,
) -> Path:
    emails = [str(item).strip() for item in recipients if is_valid_email(str(item))]
    if not emails:
        raise ValueError("لا يوجد بريد إلكتروني صالح ضمن المستلمين.")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    message = EmailMessage()
    message["Subject"] = subject or ""
    if use_bcc or len(emails) > 1:
        message["Bcc"] = ", ".join(emails)
    else:
        message["To"] = emails[0]
    message.set_content(body or "", charset="utf-8")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = output_dir / f"ProTech_Email_Draft_{stamp}.eml"
    path.write_bytes(message.as_bytes())
    return path


def _open_uri(uri: str) -> bool:
    """يفتح رابطًا/بروتوكولًا في التطبيق المسجل مع بدائل متعددة."""
    try:
        if os.name == "nt":
            os.startfile(uri)  # type: ignore[attr-defined]
            return True
        if sys.platform == "darwin":
            subprocess.Popen(["open", uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        opener = shutil.which("xdg-open") or shutil.which("gio")
        if opener:
            command = [opener, uri] if Path(opener).name != "gio" else [opener, "open", uri]
            subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        return bool(webbrowser.open(uri))
    except Exception:
        return False


def _outlook_com_via_pywin32(
    recipients: list[str], subject: str, body: str, *, use_bcc: bool
) -> bool:
    if os.name != "nt":
        return False
    try:
        import win32com.client  # type: ignore

        outlook = win32com.client.Dispatch("Outlook.Application")
        mail = outlook.CreateItem(0)
        recipient_type = 3 if (use_bcc or len(recipients) > 1) else 1  # 1=To, 3=BCC
        for address in recipients:
            recipient = mail.Recipients.Add(address)
            recipient.Type = recipient_type
        if not mail.Recipients.ResolveAll():
            # بعض إصدارات Outlook الجديدة لا تُرجع ResolveAll بشكل موثوق، لذا نحفظ الحقل أيضًا.
            addresses = "; ".join(recipients)
            if recipient_type == 3:
                mail.BCC = addresses
            else:
                mail.To = addresses
        mail.Subject = subject or ""
        mail.Body = body or ""
        mail.Save()
        mail.Display(False)
        return True
    except Exception:
        return False


def _outlook_com_via_powershell(
    recipients: list[str], subject: str, body: str, *, use_bcc: bool
) -> bool:
    """يفتح مسودة في Outlook الكلاسيكي دون اشتراط pywin32."""
    if os.name != "nt":
        return False
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return False

    def b64(value: str) -> str:
        return base64.b64encode(value.encode("utf-8")).decode("ascii")

    addresses = "; ".join(recipients)
    recipient_property = "BCC" if use_bcc or len(recipients) > 1 else "To"
    script = f"""
$ErrorActionPreference = 'Stop'
function Decode([string]$value) {{
  [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($value))
}}
$outlook = New-Object -ComObject Outlook.Application
$mail = $outlook.CreateItem(0)
$addresses = (Decode('{b64(addresses)}')).Split(';')
$recipientType = {3 if use_bcc or len(recipients) > 1 else 1}
foreach ($address in $addresses) {{
  $clean = $address.Trim()
  if ($clean) {{
    $recipient = $mail.Recipients.Add($clean)
    $recipient.Type = $recipientType
  }}
}}
[void]$mail.Recipients.ResolveAll()
$mail.Subject = Decode('{b64(subject or '')}')
$mail.Body = Decode('{b64(body or '')}')
$mail.Save()
$mail.Display($false)
""".strip()
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            creationflags=creationflags,
        )
        return completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def open_outlook_draft(
    recipients: Iterable[str], subject: str, body: str, *, use_bcc: bool = False,
    open_file_callback=None,
) -> tuple[str, str]:
    """يفتح مسودة داخل Microsoft Outlook متى كان مثبتًا.

    يبدأ بـ Outlook COM على Windows، ثم Outlook على macOS، ثم تطبيق البريد
    الافتراضي، وأخيرًا ملف EML كبديل آمن.
    """
    emails = [str(item).strip() for item in recipients if is_valid_email(str(item))]
    if not emails:
        raise ValueError("لا يوجد بريد إلكتروني صالح ضمن المستلمين.")

    if _outlook_com_via_pywin32(emails, subject, body, use_bcc=use_bcc):
        return "outlook_com", "Microsoft Outlook"
    if _outlook_com_via_powershell(emails, subject, body, use_bcc=use_bcc):
        return "outlook_com", "Microsoft Outlook"

    mailto = build_mailto_url(emails, subject, body, use_bcc=use_bcc)
    if sys.platform == "darwin" and shutil.which("open"):
        try:
            completed = subprocess.run(
                ["open", "-a", "Microsoft Outlook", mailto],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, check=False,
            )
            if completed.returncode == 0:
                return "outlook_macos", mailto
        except (OSError, subprocess.SubprocessError):
            pass

    # ShellExecute/mailto يفتح Outlook الجديد إذا كان تطبيق البريد الافتراضي.
    if len(mailto) <= 1800 and _open_uri(mailto):
        return "mailto", mailto

    path = create_eml_draft(emails, subject, body, use_bcc=use_bcc)
    if open_file_callback:
        open_file_callback(path)
    elif not _open_uri(path.resolve().as_uri()):
        raise RuntimeError(f"تم إنشاء المسودة لكن تعذر فتحها تلقائيًا:\n{path}")
    return "eml", str(path)


def open_email_draft(
    recipients: Iterable[str], subject: str, body: str, *, use_bcc: bool = False,
    open_file_callback=None,
) -> tuple[str, str]:
    """يفتح مسودة البريد في التطبيق الافتراضي، مع EML للروابط الطويلة."""
    url = build_mailto_url(recipients, subject, body, use_bcc=use_bcc)
    if len(url) <= 1800 and _open_uri(url):
        return "mailto", url
    path = create_eml_draft(recipients, subject, body, use_bcc=use_bcc)
    if open_file_callback:
        open_file_callback(path)
    elif not _open_uri(path.resolve().as_uri()):
        raise RuntimeError(f"تم إنشاء المسودة لكن تعذر فتحها تلقائيًا:\n{path}")
    return "eml", str(path)


def open_whatsapp_desktop(phone: str, body: str) -> tuple[str, str]:
    """يفتح تطبيق WhatsApp Desktop، ثم يعود إلى WhatsApp Web عند الحاجة."""
    desktop_url = build_whatsapp_url(phone, body, desktop=True)
    if _open_uri(desktop_url):
        return "whatsapp_app", desktop_url
    web_url = build_whatsapp_url(phone, body)
    if not _open_uri(web_url):
        raise RuntimeError("تعذر فتح تطبيق واتساب أو واتساب ويب.")
    return "whatsapp_web", web_url


def open_whatsapp_chat(phone: str, body: str) -> str:
    _method, target = open_whatsapp_desktop(phone, body)
    return target
