from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

APP_MERGED_VERSION = "23.0.1"
_INSTANCE_MUTEX = None

# مكتبات vendor المضمّنة بجانب هذا الملف يجب أن تكون قابلة للاستيراد.
PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "vendor"))


def enable_high_dpi() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def acquire_single_instance() -> bool:
    """Keep one application instance per Windows session."""
    global _INSTANCE_MUTEX
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, "Local\\ProTechHR-v18-application")
        if not handle:
            return True
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _INSTANCE_MUTEX = handle
        return True
    except Exception:
        # A failure to create the advisory mutex must not block the HR system.
        return True


def show_startup_error(message: str) -> None:
    try:
        from hr_app.tk_runtime import configure_tk_runtime
        configure_tk_runtime()
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("تعذر تشغيل نظام برو تك", message)
        root.destroy()
    except Exception:
        pass


def run_frozen_report_smoke_test() -> bool:
    """Create PDF, HTML and employee Excel from the frozen release."""
    target_text = os.environ.get("PROTECH_HR_REPORT_SMOKE_DIR", "").strip()
    if not target_text:
        return False
    from hr_app.excel_export import export_employee_xlsx
    from hr_app.report_export import (
        export_employee_pdf, export_printable_html, export_printable_pdf,
    )

    target = Path(target_text).resolve()
    target.mkdir(parents=True, exist_ok=True)
    company = {
        "company_name": "شركة الاختبار المخصصة",
        "legal_name": "شركة الاختبار للموارد البشرية",
        "registration_number": "1010123456",
        "tax_number": "310123456700003",
        "city": "الرياض",
        "country": "المملكة العربية السعودية",
        "phone": "+966 11 000 0000",
        "email": "hr@company.example",
    }
    employee = {
        "emp_code": "4567", "name": "موظف اختبار", "department": "الموارد البشرية",
        "job_title": "أخصائي", "salary": 5000, "status": "نشط",
        "id_number": "P1234567", "id_type": "جواز سفر", "join_date": "2024-01-01",
        "id_issue_date": "2026-01-01", "id_expiry_date": "2031-01-01",
        "work_permit_start_date": "2026-10-31",
        "work_permit_expiry_date": "2027-10-31",
        "medical_insurance_start_date": "2026-08-31",
        "medical_insurance_expiry_date": "2027-08-31",
        "bank_name": "بنك الاختبار", "bank_account_number": "608010167519",
        "iban": "SA0380000000608010167519",
    }
    general = export_printable_pdf(
        target / "general.pdf", [employee],
        {"total": 1, "active": 1, "salaries": 5000, "avg_salary": 5000, "leave_today": 0},
        company_profile=company,
    )
    report = {
        "employee": employee,
        "company": company,
        "attendance": [],
        "leaves": [],
        "custom_fields": [],
        "documents": [
            {
                "document_type": "تأمين طبي",
                "original_name": "medical_insurance.pdf",
                "start_date": "2026-08-31",
                "expiry_date": "2027-08-31",
                "notes": "فحص إصدار داخلي",
            }
        ],
    }
    individual = export_employee_pdf(target / "employee.pdf", report)
    employee_excel = export_employee_xlsx(target / "employee.xlsx", report)
    browser_report = export_printable_html(
        target / "general.html", [employee],
        {"total": 1, "active": 1, "salaries": 5000, "avg_salary": 5000, "leave_today": 0},
        company_profile=company,
    )
    (target / "PASS.txt").write_text(
        (
            f"{general.stat().st_size},{individual.stat().st_size},"
            f"{employee_excel.stat().st_size},{browser_report.stat().st_size}\n"
        ),
        encoding="ascii",
    )
    return True


def main() -> int:
    enable_high_dpi()
    verification_mode = (
        os.environ.get("PROTECH_HR_UI_SMOKE_TEST", "").strip() == "1"
        or os.environ.get("PROTECH_HR_MANAGER_UI_SMOKE_TEST", "").strip() == "1"
        or bool(os.environ.get("PROTECH_HR_REPORT_SMOKE_DIR", "").strip())
    )
    # فحوص الإصدار تستخدم قاعدة منفصلة ويجب أن تعمل حتى لو كان المستخدم
    # قد ترك نسخته اليومية مفتوحة. التشغيل الطبيعي يبقى محميًا بقفل النسخة الواحدة.
    if not verification_mode and not acquire_single_instance():
        show_startup_error("البرنامج يعمل بالفعل. أغلق النسخة المفتوحة قبل تشغيل نسخة أخرى.")
        return 0
    try:
        from hr_app.resilience import begin_session, end_session, setup_logging, write_crash_report

        logger = setup_logging()
        interrupted = begin_session()
        if interrupted:
            os.environ["PROTECH_HR_RECOVERY_PENDING"] = "1"
            logger.warning("اكتُشفت جلسة سابقة لم تُغلق بصورة طبيعية؛ ستُعرض آخر صفحة آمنة بعد الدخول.")
        logger.info("بدء تشغيل نظام برو تك (حزمة مدمجة %s)", APP_MERGED_VERSION)

        # الإصدار 23.0.1 يشغّل الوظائف الأساسية مباشرة بلا أغلفة ترقيع
        # متداخلة؛ هذا يمنع تعارض تواقيع التصدير ويحسّن زمن بدء التشغيل.
        from hr_app import config
        config.APP_VERSION = APP_MERGED_VERSION
        if run_frozen_report_smoke_test():
            logger.info("نجح فحص PDF وExcel وHTML للنسخة التنفيذية")
            end_session()
            return 0

        from hr_app.ui import HRApplication

        app = HRApplication()
        if os.environ.get("PROTECH_HR_MANAGER_UI_SMOKE_TEST", "").strip() == "1":
            from hr_app.enterprise_ui import ManagerApprovalsPage
            from hr_app.saudi_ui import SaudiCompliancePage
            from hr_app.ui import ReportsPage

            app.current_user = {
                "id": None,
                "username": "release_verifier",
                "full_name": "فاحص الإصدار",
                "role": "admin",
            }
            for page_class in (ManagerApprovalsPage, SaudiCompliancePage, ReportsPage):
                for child in app.container.winfo_children():
                    child.destroy()
                page = page_class(app.container, app)
                page.pack(fill="both", expand=True)
                app.withdraw()
                for _ in range(30):
                    app.update()
                    time.sleep(0.05)
            app.on_close()
            end_session()
            logger.info("نجح فحص واجهات اعتمادات المدير والامتثال السعودي والتقارير في النسخة التنفيذية")
            return 0
        if os.environ.get("PROTECH_HR_UI_SMOKE_TEST", "").strip() == "1":
            app.withdraw()
            app.update_idletasks()
            app.update()
            app.on_close()
            end_session()
            logger.info("نجح فحص بدء واجهة النسخة التنفيذية")
            return 0
        app.mainloop()
        end_session()
        logger.info("إغلاق نظام برو تك بصورة طبيعية")
        return 0
    except Exception as exc:
        try:
            from hr_app.resilience import write_crash_report
            report = write_crash_report(type(exc), exc, exc.__traceback__, "startup")
            show_startup_error(
                f"تعذر بدء البرنامج، ولم تُمس بياناتك.\n\n{exc}\n\nتم حفظ تقرير فني في:\n{report}"
            )
        except Exception:
            show_startup_error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
