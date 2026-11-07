from __future__ import annotations

import calendar as calendar_module
import csv
import json
import math
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

from .tk_runtime import configure_tk_runtime

configure_tk_runtime()

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from .calendar_picker import DatePicker
from .attendance_import import attendance_template_rows, read_attendance_file
from .config import (
    APP_ICON_PATH, APP_NAME, APP_VERSION, ATTENDANCE_STATUSES, BACKUP_DIR, COMPANY_NAME,
    COPYRIGHT_TEXT, EMPLOYEE_STATUSES, EXPORT_DIR, LEAVE_STATUSES, LEAVE_TYPES,
    PAGE_SIZE, RELEASE_CHANNEL, ROLES, DATA_DIR, is_portable_mode, developer_profile,
)
from .database import Database, DatabaseError, PermissionError
from .company import COMPANY_PROFILE_FIELDS, COMPANY_PROFILE_LABELS, normalize_company_profile
from .enterprise import EnterpriseService
from .enterprise_ui import (
    EmployeeDocumentsDialog, EnterpriseHubPage, NotificationsPage, AllowancesDialog, PayrollPage,
    ManagerApprovalsPage,
)
from .excel_export import export_employee_xlsx, export_xlsx
from .excel_import import FIELD_LABELS, parse_employee_sheet, read_employee_file, template_rows
from .important_links import IMPORTANT_LINKS, validate_official_url
from .report_export import (
    export_employee_pdf, export_printable_html, export_printable_pdf,
    export_workforce_analytics_pdf,
)
from .security import mask_bank_account, mask_iban, mask_id_number
from .identity_rules import IDENTITY_TYPES, identity_type_for_nationality, identity_number_label, identity_expiry_label
from .employee_metrics import (
    RATING_OPTIONS, format_identity_expiry_status, format_service_duration,
    rating_from_label, rating_to_stars,
)
from .messaging import (
    PLACEHOLDERS, build_whatsapp_url, is_valid_email, normalize_whatsapp_phone,
    open_email_draft, open_outlook_draft, open_whatsapp_chat,
    open_whatsapp_desktop, render_template,
)
from .modern_ui import (
    COLORS, METRIC_COLORS, NavButton, ScrollableSidebar, build_page_header,
    configure_styles, ensure_section_button_style, page_accent,
)
from .resilience import (
    clear_form_draft, load_form_draft, load_session_state, save_form_draft,
    save_session_state, setup_logging, write_crash_report,
)
from .windowing import install_adaptive_dialogs
from .saudi_hr import SaudiHRService
from .saudi_ui import SaudiCompliancePage

FONT = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
TITLE_FONT = ("Segoe UI", 18, "bold")


def clear_frame(frame: tk.Misc) -> None:
    for child in frame.winfo_children():
        child.destroy()


def open_path(path: Path | str) -> None:
    path = str(path)
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        webbrowser.open(Path(path).resolve().as_uri())


class AsyncMixin:
    executor: ThreadPoolExecutor

    def run_async(self, function, on_success=None, on_error=None, busy_text="جارٍ التنفيذ..."):
        self.set_status(busy_text)
        future = self.executor.submit(function)

        def poll():
            try:
                if not self.winfo_exists():
                    return
            except tk.TclError:
                return
            if not future.done():
                self.after(60, poll)
                return
            try:
                result = future.result()
            except Exception as exc:
                self._handle_async_error(exc, on_error)
            else:
                self._handle_async_success(result, on_success)

        self.after(60, poll)
        return future

    def _handle_async_error(self, exc: Exception, callback):
        self.set_status("تعذر إكمال العملية")
        setup_logging().error("فشلت عملية خلفية: %s", exc, exc_info=True)
        try:
            if callback:
                callback(exc)
            else:
                message = str(exc) or "حدث خطأ غير متوقع أثناء تنفيذ العملية."
                if getattr(exc, "winerror", None) == 32:
                    message = "يوجد ملف قيد الاستخدام مؤقتًا. أُوقفت العملية بأمان؛ أغلق الملف المفتوح ثم أعد المحاولة."
                messagebox.showerror("تعذر إكمال العملية", message, parent=self)
        except tk.TclError:
            pass

    def _handle_async_success(self, result, callback):
        self.set_status("جاهز")
        if callback:
            try:
                callback(result)
            except tk.TclError:
                pass
            except Exception as exc:
                setup_logging().error("فشل تحديث الواجهة بعد نجاح العملية: %s", exc, exc_info=True)

    def set_status(self, text: str):
        if hasattr(self, "status_var"):
            try:
                self.status_var.set(text)
            except tk.TclError:
                pass


class HRApplication(tk.Tk, AsyncMixin):
    def __init__(self):
        super().__init__()
        self.logger = setup_logging()
        self.title(f"{APP_NAME} — {COMPANY_NAME} — v{APP_VERSION}")
        try:
            if APP_ICON_PATH.is_file():
                self.iconbitmap(default=str(APP_ICON_PATH))
        except tk.TclError:
            self.logger.warning("تعذر تحميل أيقونة التطبيق: %s", APP_ICON_PATH)
        self._configure_window()
        self.option_add("*Font", FONT)
        self.style = ttk.Style(self)
        configure_styles(self, self.style)
        install_adaptive_dialogs(self)
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hr-worker")
        self.db = Database()
        self.refresh_company_identity()
        self.enterprise = EnterpriseService(self.db)
        self.saudi = SaudiHRService(self.db)
        self.current_user: dict | None = None
        self.current_page = None
        self.nav_buttons: dict[type, NavButton] = {}
        self.last_activity = time.monotonic()
        self.session_timeout_seconds = self.db.session_timeout_minutes() * 60
        self.bind_all("<KeyPress>", self.mark_activity, add="+")
        self.bind_all("<Button>", self.mark_activity, add="+")
        self.after(60_000, self.check_session_timeout)
        self.status_var = tk.StringVar(value="جاهز")
        self.container = ttk.Frame(self, style="Page.TFrame")
        self.container.pack(fill="both", expand=True)
        self._install_exception_guards()
        self.show_entry_screen()

    @property
    def company_name(self) -> str:
        return self.db.company_profile().get("company_name") or COMPANY_NAME

    def refresh_company_identity(self) -> None:
        company_name = self.company_name
        self.title(f"{APP_NAME} — {company_name} — v{APP_VERSION}")
        if hasattr(self, "sidebar_company_var"):
            self.sidebar_company_var.set(company_name)

    def _configure_window(self):
        try:
            width = min(1500, max(1120, int(self.winfo_screenwidth() * 0.92)))
            height = min(920, max(700, int(self.winfo_screenheight() * 0.88)))
            x = max(0, (self.winfo_screenwidth() - width) // 2)
            y = max(0, (self.winfo_screenheight() - height) // 2)
            self.geometry(f"{width}x{height}+{x}+{y}")
        except tk.TclError:
            self.geometry("1320x780")
        self.minsize(1050, 650)

    def _install_exception_guards(self):
        def sys_hook(exc_type, exc_value, exc_tb):
            path = write_crash_report(exc_type, exc_value, exc_tb, "sys.excepthook")
            self.logger.error("خطأ غير معالج. التقرير: %s", path)

        sys.excepthook = sys_hook
        if hasattr(threading, "excepthook"):
            def thread_hook(args):
                write_crash_report(args.exc_type, args.exc_value, args.exc_traceback, f"thread:{args.thread.name}")
            threading.excepthook = thread_hook

    def report_callback_exception(self, exc_type, exc_value, exc_tb):
        """يمسك أخطاء Tkinter بدل إغلاق التطبيق فجأة."""
        path = write_crash_report(exc_type, exc_value, exc_tb, "tkinter_callback")
        try:
            messagebox.showerror(
                "حدث خطأ غير متوقع",
                "تم احتواء الخطأ ولم يُغلق البرنامج. يُنصح بحفظ العمل ثم إعادة المحاولة.\n\n"
                f"سُجلت التفاصيل في:\n{path}",
                parent=self,
            )
        except tk.TclError:
            pass

    def mark_activity(self, _event=None):
        self.last_activity = time.monotonic()

    def check_session_timeout(self):
        try:
            if not self.winfo_exists():
                return
            if self.current_user and time.monotonic() - self.last_activity >= self.session_timeout_seconds:
                self.current_user = None
                self.show_entry_screen()
                messagebox.showinfo("انتهاء الجلسة", "تم تسجيل الخروج تلقائيًا بعد 30 دقيقة من عدم النشاط.", parent=self)
            self.after(60_000, self.check_session_timeout)
        except tk.TclError:
            return

    def show_entry_screen(self):
        clear_frame(self.container)
        if self.db.has_users():
            LoginView(self.container, self).pack(fill="both", expand=True)
        else:
            InitialAdminView(self.container, self).pack(fill="both", expand=True)

    def restore_backup_before_login(self, parent: tk.Misc):
        if self.db.has_users():
            messagebox.showerror(
                "يلزم تسجيل الدخول",
                "الاستعادة من شاشة الدخول متاحة أثناء الإعداد الأول فقط. "
                "سجّل الدخول بحساب مدير ثم افتح شاشة النسخ الاحتياطي والاستعادة.",
                parent=parent,
            )
            return
        path = filedialog.askopenfilename(
            parent=parent,
            title="اختر نسخة برو تك الكاملة للاستعادة قبل تسجيل الدخول",
            filetypes=[("نسخ برو تك", "*.protechhr *.db"), ("جميع الملفات", "*.*")],
        )
        if not path:
            return
        if not messagebox.askyesno(
            "تأكيد الاستعادة قبل الدخول",
            "سيتحقق البرنامج من سلامة النسخة، وينشئ نسخة أمان للحالة الحالية، "
            "ثم يستعيد المستخدمين والموظفين والمرفقات والإعدادات.\n\n"
            "هل تريد المتابعة؟",
            parent=parent,
        ):
            return

        def success(_):
            messagebox.showinfo(
                "اكتملت الاستعادة",
                "تمت الاستعادة بنجاح مع الاحتفاظ بنسخة أمان للحالة السابقة. "
                "يمكنك الآن تسجيل الدخول ببيانات النسخة المستعادة.",
                parent=self,
            )
            self.show_entry_screen()

        self.run_async(
            lambda: self.enterprise.restore_pre_login_backup(path),
            success,
            busy_text="التحقق من النسخة واستعادتها بأمان...",
        )

    def login(self, username: str, password: str):
        def success(user):
            if not user:
                messagebox.showerror("تعذر الدخول", "اسم المستخدم أو كلمة المرور غير صحيحة.", parent=self)
                return
            self.current_user = user
            self.mark_activity()
            self.run_async(
                lambda: self.enterprise.create_full_backup(user, automatic=True),
                on_success=lambda _path: self._finish_authenticated_session(),
                on_error=self._session_backup_error,
                busy_text="إنشاء نسخة أمان لهذه الجلسة قبل فتح النظام...",
            )

        self.run_async(lambda: self.db.authenticate(username, password), success, busy_text="جارٍ تسجيل الدخول...")

    def _finish_authenticated_session(self):
        if not self.current_user:
            return
        self.build_main_shell()
        self.after(900, self.show_identity_expiry_reminders)

    def _session_backup_error(self, exc: Exception):
        self.logger.error("تعذر إنشاء نسخة الجلسة: %s", exc, exc_info=True)
        self._finish_authenticated_session()
        messagebox.showwarning(
            "تنبيه النسخ الاحتياطي",
            "تم تسجيل الدخول، لكن تعذر إنشاء نسخة أمان لهذه الجلسة. "
            "لم تُمس بياناتك؛ راجع مساحة القرص أو صلاحيات مجلد البيانات ثم "
            "أنشئ نسخة من صفحة النسخ الاحتياطي.",
            parent=self,
        )

    def show_identity_expiry_reminders(self):
        if not self.current_user:
            return
        def render(rows):
            if not rows:
                return
            lines = []
            for row in rows[:12]:
                lines.append(f"• {row['employee']} — {row['message']} — الانتهاء: {row['date']}")
            if len(rows) > 12:
                lines.append(f"… و{len(rows) - 12} تنبيهات أخرى في مركز التنبيهات.")
            messagebox.showwarning(
                "تنبيهات صلاحية الهوية",
                "توجد هويات تحتاج المتابعة:\n\n" + "\n".join(lines),
                parent=self,
            )
        self.run_async(
            lambda: self.enterprise.identity_expiry_alerts(only_unseen=True, mark_shown=True),
            render, busy_text="التحقق من صلاحية الهويات...",
        )

    def _background_backup_error(self, exc: Exception):
        # النسخ التلقائي لا يوقف المستخدم ولا يفتح نافذة خطأ عند الدخول.
        self.logger.warning("تعذر النسخ التلقائي وسيعاد في التشغيل التالي: %s", exc, exc_info=True)
        self.set_status("جاهز — تعذر النسخ التلقائي وسيُعاد لاحقًا")

    def build_main_shell(self):
        clear_frame(self.container)
        shell = tk.Frame(self.container, bg=COLORS["background"])
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, bg=COLORS["sidebar"], width=260)
        sidebar.pack(side="right", fill="y")
        sidebar.pack_propagate(False)
        content_area = tk.Frame(shell, bg=COLORS["background"])
        content_area.pack(side="left", fill="both", expand=True)

        # رأس ثابت، ومنطقة قابلة للتمرير، وتذييل ثابت: لذلك لا يختفي آخر زر مهما كان ارتفاع الشاشة.
        sidebar_header = tk.Frame(sidebar, bg=COLORS["sidebar_alt"], height=122)
        sidebar_header.pack(fill="x", side="top")
        sidebar_header.pack_propagate(False)
        tk.Label(
            sidebar_header, text="PRO TECH", bg=COLORS["sidebar_alt"], fg="#FFFFFF",
            font=("Segoe UI", 19, "bold"), anchor="center",
        ).pack(fill="x", pady=(16, 2))
        self.sidebar_company_var = tk.StringVar(value=self.company_name)
        tk.Label(
            sidebar_header, textvariable=self.sidebar_company_var, bg=COLORS["sidebar_alt"], fg="#9CC8FF",
            font=("Segoe UI", 10, "bold"), anchor="center",
        ).pack(fill="x", pady=(0, 8))
        tk.Label(
            sidebar_header,
            text=f"{self.current_user['full_name']}  •  {ROLES[self.current_user['role']]}",
            bg="#172640", fg="#FFFFFF", font=("Segoe UI", 9, "bold"), anchor="center", padx=8, pady=7,
        ).pack(fill="x", padx=14)

        footer = tk.Frame(sidebar, bg=COLORS["sidebar_alt"], padx=9, pady=5)
        footer.pack(fill="x", side="bottom")
        footer_buttons = (
            ("ℹ  حول البرنامج", "#334155", self.show_about),
            ("↪  تسجيل الخروج", "#475569", self.logout),
            ("✕  إغلاق", "#B91C1C", self.on_close),
        )
        for text, accent, command in footer_buttons:
            NavButton(footer, text=text, accent=accent, command=command).pack(fill="x", pady=2)

        navigation = ScrollableSidebar(sidebar)
        navigation.pack(fill="both", expand=True, padx=(0, 2), pady=4)

        self.page_host = ttk.Frame(content_area, padding=(18, 16), style="Page.TFrame")
        self.page_host.pack(fill="both", expand=True)
        status_bar = ttk.Frame(content_area, style="Status.TFrame")
        status_bar.pack(fill="x", side="bottom")
        tk.Label(
            status_bar, text=RELEASE_CHANNEL, bg="#E9EEF4", fg="#8390A0",
            font=("Segoe UI", 7), padx=5, pady=1,
        ).pack(side="left", anchor="sw")
        ttk.Label(status_bar, textvariable=self.status_var, anchor="w", padding=6, style="Status.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Label(status_bar, text=f"v{APP_VERSION}  |  {COPYRIGHT_TEXT}", anchor="e", padding=6, style="Status.TLabel").pack(side="right")

        buttons = [
            ("▣  لوحة المعلومات", DashboardPage),
            ("◉  التحليل الذكي", SmartAnalyticsPage),
            ("●  الموظفون", EmployeesPage),
            ("🔔  المتابعة والتنبيهات", NotificationsPage),
            ("✉  المراسلات", MessagingPage),
            ("✓  الحضور", AttendancePage),
            ("◆  الإجازات", LeavesPage),
            ("▦  الإدارة المتقدمة", EnterpriseHubPage),
            ("▤  التقويم الرقمي", CalendarPage),
            ("▥  التقارير", ReportsPage),
            ("🔗  روابط مهمة", ImportantLinksPage),
        ]
        if self.current_user["role"] == "viewer":
            buttons = [item for item in buttons if item[1] is not MessagingPage]
        if self.current_user["role"] in {"admin", "hr"}:
            buttons.insert(2, ("⌕  مركز البحث والمخاطر", OperationsCenterPage))
            buttons.insert(3, ("⚖  الامتثال السعودي", SaudiCompliancePage))
            buttons.insert(6, ("◈  الرواتب والاعتمادات", PayrollPage))
        if self.current_user["role"] == "admin":
            buttons.insert(2, ("✓  اعتمادات المدير", ManagerApprovalsPage))
            buttons.extend([
                ("▣  بيانات الشركة", CompanyProfilePage),
                ("◎  سجل التدقيق", AuditPage),
                ("♙  المستخدمون", UsersPage),
            ])
        if self.current_user["role"] in {"admin", "hr"}:
            buttons.append(("⬒  النسخ الاحتياطي", BackupPage))

        self.nav_buttons = {}
        for text, page_cls in buttons:
            accent = page_accent(page_cls.__name__)
            button = NavButton(
                navigation.inner, text=text, accent=accent,
                command=lambda cls=page_cls: self.show_page(cls),
            )
            button.pack(fill="x", padx=8, pady=1)
            self.nav_buttons[page_cls] = button
        page_to_restore = DashboardPage
        if os.environ.pop("PROTECH_HR_RECOVERY_PENDING", "") == "1":
            saved_name = str(load_session_state().get("last_page") or "")
            available = {page_cls.__name__: page_cls for _, page_cls in buttons}
            page_to_restore = available.get(saved_name, DashboardPage)
            self.after(
                700,
                lambda: messagebox.showinfo(
                    "استعادة الجلسة",
                    "اكتشف البرنامج أن الجلسة السابقة لم تُغلق بصورة طبيعية. "
                    "تم فتح آخر قسم آمن؛ راجع البيانات غير المحفوظة قبل المتابعة.",
                    parent=self,
                ),
            )
        self.show_page(page_to_restore)

    def _apply_section_theme(self, root: tk.Misc, accent: str):
        style_name = ensure_section_button_style(self.style, f"Section{accent.replace('#', '')}", accent)
        destructive = ("حذف", "رفض", "إلغاء", "إنهاء", "إغلاق")
        stack = [root]
        while stack:
            widget = stack.pop()
            try:
                children = widget.winfo_children()
            except tk.TclError:
                continue
            stack.extend(children)
            if isinstance(widget, ttk.Button):
                try:
                    text = str(widget.cget("text"))
                    current = str(widget.cget("style"))
                    if any(word in text for word in destructive):
                        widget.configure(style="Danger.TButton")
                    elif current in {"", "TButton"}:
                        widget.configure(style=style_name)
                except tk.TclError:
                    pass

    def show_page(self, page_cls):
        clear_frame(self.page_host)
        self.current_page = page_cls(self.page_host, self)
        self.current_page.pack(fill="both", expand=True)
        for cls, button in self.nav_buttons.items():
            button.set_selected(cls is page_cls)
        accent = page_accent(page_cls.__name__)
        save_session_state(page_cls.__name__)
        self.after_idle(lambda: self._apply_section_theme(self.current_page, accent))

    def show_about(self):
        profile = developer_profile()
        company = self.db.company_profile()
        lines = [
            APP_NAME,
            f"الإصدار: {APP_VERSION}",
            f"حالة الإصدار: {RELEASE_CHANNEL}",
            f"الجهة المالكة: {company['company_name']}",
        ]
        for key in COMPANY_PROFILE_FIELDS[1:]:
            if company.get(key):
                lines.append(f"{COMPANY_PROFILE_LABELS[key]}: {company[key]}")
        lines.extend([
            "",
            "بيانات المطور",
            f"الاسم: {profile.get('developer') or '—'}",
            f"الهاتف: {profile.get('phone') or '—'}",
            f"البريد الإلكتروني: {profile.get('email') or '—'}",
            "",
            f"وضع التشغيل: {'محمول' if is_portable_mode() else 'مثبّت ببيانات مشتركة آمنة'}",
            f"مجلد البيانات: {DATA_DIR}",
            "قاعدة البيانات SQLite والواجهة Tk تعملان دون Microsoft Access أو Office.",
            "يُحفظ سجل الأعطال تلقائيًا بدل إغلاق البرنامج دون تفسير.",
            "",
            COPYRIGHT_TEXT,
        ])
        messagebox.showinfo("حول البرنامج", "\n".join(lines), parent=self)

    def logout(self):
        self.current_user = None
        self.nav_buttons = {}
        self.show_entry_screen()

    def on_close(self):
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        finally:
            self.destroy()


class CenterCard(ttk.Frame):
    def __init__(self, master):
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.card = ttk.LabelFrame(self, padding=25)
        self.card.grid(row=0, column=0)


class InitialAdminView(CenterCard):
    def __init__(self, master, app: HRApplication):
        super().__init__(master)
        self.app = app
        self.card.configure(text="الإعداد الأول")
        ttk.Label(
            self.card,
            text=f"{APP_NAME}\nتهيئة الشركة وإنشاء حساب المدير",
            font=TITLE_FONT,
            justify="center",
        ).grid(row=0, column=0, columnspan=2, pady=(0, 20))
        self.entries = {}
        fields = [("اسم الشركة المخصص (اختياري)", "company_name", False),
                  ("اسم المستخدم", "username", False), ("الاسم الكامل", "full_name", False),
                  ("كلمة المرور", "password", True), ("تأكيد كلمة المرور", "confirm", True)]
        for row, (label, key, secret) in enumerate(fields, start=1):
            ttk.Label(self.card, text=label).grid(row=row, column=1, sticky="e", padx=8, pady=7)
            entry = ttk.Entry(self.card, width=32, show="*" if secret else "")
            entry.grid(row=row, column=0, sticky="ew", padx=8, pady=7)
            self.entries[key] = entry
        next_row = len(fields) + 1
        ttk.Label(self.card, text="لن يُنشأ حساب افتراضي حفاظًا على الأمان.", foreground="#666").grid(
            row=next_row, column=0, columnspan=2, pady=8
        )
        ttk.Label(
            self.card,
            text="كلمة المرور: 8 أحرف على الأقل، وتتضمن حرفًا ورقمًا.",
            foreground="#64748B",
        ).grid(row=next_row + 1, column=0, columnspan=2, pady=(0, 6))
        ttk.Button(self.card, text="إنشاء الحساب وبدء الاستخدام", command=self.create_admin).grid(
            row=next_row + 2, column=0, columnspan=2, pady=(8, 6), ipady=5
        )
        ttk.Label(self.card, text="— أو —", foreground="#64748B").grid(
            row=next_row + 3, column=0, columnspan=2, pady=4
        )
        ttk.Button(
            self.card, text="استعادة نسخة احتياطية كاملة من جهاز سابق",
            command=self.restore_existing_backup, style="Secondary.TButton",
        ).grid(row=next_row + 4, column=0, columnspan=2, pady=(4, 8), ipady=5)
        ttk.Label(
            self.card, text="استخدم هذا الخيار بعد تهيئة الجهاز لاستعادة المستخدمين والموظفين والمرفقات وكل الإعدادات.",
            foreground="#64748B", wraplength=460, justify="center",
        ).grid(row=next_row + 5, column=0, columnspan=2, pady=(0, 4))

    def restore_existing_backup(self):
        self.app.restore_backup_before_login(self)

    def create_admin(self):
        company_name = self.entries["company_name"].get().strip()
        username = self.entries["username"].get().strip()
        full_name = self.entries["full_name"].get().strip()
        password = self.entries["password"].get()
        if password != self.entries["confirm"].get():
            messagebox.showerror("خطأ", "كلمتا المرور غير متطابقتين.", parent=self)
            return

        try:
            company_profile = normalize_company_profile(
                {"company_name": company_name}, include_defaults=False
            )
        except ValueError as exc:
            messagebox.showerror("بيانات الشركة غير صحيحة", str(exc), parent=self)
            return

        def create():
            user_id = self.app.db.create_user(username, full_name, password, "admin")
            if company_name:
                self.app.db.save_company_profile(
                    company_profile,
                    {"id": user_id, "username": username, "role": "admin"},
                )
            return company_name or COMPANY_NAME

        def success(active_company):
            self.app.refresh_company_identity()
            messagebox.showinfo(
                "تم",
                f"تم إنشاء حساب المدير وتفعيل اسم الشركة: {active_company}. سجّل الدخول الآن.",
                parent=self,
            )
            self.app.show_entry_screen()

        self.app.run_async(
            create,
            success,
            busy_text="جارٍ تهيئة الشركة وإنشاء حساب المدير...",
        )


class LoginView(CenterCard):
    def __init__(self, master, app: HRApplication):
        super().__init__(master)
        self.app = app
        self.card.configure(text="تسجيل الدخول")
        ttk.Label(self.card, text=f"{APP_NAME}\n{app.company_name}", font=TITLE_FONT, justify="center").grid(row=0, column=0, columnspan=2, pady=(0, 20))
        ttk.Label(self.card, text="اسم المستخدم").grid(row=1, column=1, sticky="e", padx=8, pady=8)
        self.username = ttk.Entry(self.card, width=32)
        self.username.grid(row=1, column=0, padx=8, pady=8)
        ttk.Label(self.card, text="كلمة المرور").grid(row=2, column=1, sticky="e", padx=8, pady=8)
        self.password = ttk.Entry(self.card, width=32, show="*")
        self.password.grid(row=2, column=0, padx=8, pady=8)
        self.password.bind("<Return>", lambda _: self.submit())
        ttk.Button(self.card, text="دخول", command=self.submit).grid(row=3, column=0, columnspan=2, pady=12, ipady=5)
        ttk.Label(
            self.card,
            text="النظام رقم 1 لإدارة الموارد البشرية",
            foreground="#2563EB",
            font=("Segoe UI", 10, "bold"),
        ).grid(row=4, column=0, columnspan=2, pady=(2, 0))
        remembered_username = self.app.db.get_setting("last_username", "").strip()
        if remembered_username:
            self.username.insert(0, remembered_username)
            self.password.focus_set()
        else:
            self.username.focus_set()

    def submit(self):
        if not self.username.get().strip() or not self.password.get():
            messagebox.showwarning("تنبيه", "أدخل اسم المستخدم وكلمة المرور.", parent=self)
            return
        self.app.login(self.username.get(), self.password.get())


class Page(ttk.Frame):
    def __init__(self, master, app: HRApplication, title: str):
        super().__init__(master, style="Page.TFrame")
        self.app = app
        self.accent = page_accent(self.__class__.__name__)
        subtitles = {
            "لوحة المعلومات": "ملخص حي لأهم مؤشرات الموارد البشرية",
            "إدارة الموظفين": "بحث وتصفية وإدارة ملفات الموظفين بأمان",
            "الحضور والانصراف": "تسجيل ومراجعة الحضور اليومي",
            "الإجازات": "الطلبات والأرصدة ومسار الاعتماد",
            "التقويم الرقمي": "الحضور والإجازات والملاحظات في عرض شهري",
            "التقارير والتصدير": "تقارير قابلة للتصفية والطباعة والتصدير",
            "بيانات الشركة": "هوية المنشأة الاختيارية المستخدمة في الواجهة والمخرجات",
            "سجل التدقيق": "تتبع العمليات والتغييرات الحساسة",
            "إدارة المستخدمين": "الحسابات والأدوار والصلاحيات",
            "النسخ الاحتياطي والاستعادة": "نسخ ذرية تشمل قاعدة البيانات والمستندات",
        }
        build_page_header(self, title, self.accent, subtitles.get(title, "نظام برو تك للموارد البشرية"))


class CompanyProfilePage(Page):
    field_groups = (
        (
            "الهوية النظامية",
            (
                "company_name", "legal_name", "entity_type", "activity",
                "registration_number", "unified_number", "tax_number",
            ),
        ),
        (
            "العنوان والاتصال",
            ("address", "city", "postal_code", "country", "phone", "email", "website"),
        ),
    )

    def __init__(self, master, app):
        super().__init__(master, app, "بيانات الشركة")
        self.variables = {
            key: tk.StringVar() for key in COMPANY_PROFILE_FIELDS
        }
        ttk.Label(
            self,
            text=(
                "جميع الحقول اختيارية. عند ترك اسم الشركة فارغًا يستخدم النظام الاسم الافتراضي، "
                "وتظهر البيانات المحفوظة في النوافذ والتقارير وExcel وقسائم الرواتب والمراسلات."
            ),
            foreground=COLORS["muted"],
            anchor="e",
            justify="right",
            wraplength=980,
        ).pack(fill="x", pady=(4, 10))

        form = ttk.Frame(self)
        form.pack(fill="both", expand=True)
        for column, (title, fields) in enumerate(self.field_groups):
            panel = ttk.LabelFrame(form, text=title, padding=14)
            panel.grid(row=0, column=column, sticky="nsew", padx=7)
            panel.columnconfigure(0, weight=1)
            for row, key in enumerate(fields):
                ttk.Label(panel, text=COMPANY_PROFILE_LABELS[key]).grid(
                    row=row, column=1, sticky="e", padx=(8, 0), pady=7
                )
                ttk.Entry(panel, textvariable=self.variables[key], width=38).grid(
                    row=row, column=0, sticky="ew", pady=7
                )
        form.columnconfigure(0, weight=1, uniform="company")
        form.columnconfigure(1, weight=1, uniform="company")

        self.status_text = tk.StringVar()
        ttk.Label(
            self, textvariable=self.status_text, foreground=COLORS["muted"], anchor="e"
        ).pack(fill="x", pady=(10, 4))
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=6)
        ttk.Button(buttons, text="حفظ بيانات الشركة", command=self.save).pack(
            side="right", padx=5, ipadx=12, ipady=5
        )
        ttk.Button(
            buttons, text="مسح البيانات المخصصة", command=self.clear_profile,
            style="Danger.TButton",
        ).pack(side="right", padx=5, ipadx=8, ipady=5)
        self.load()

    def load(self):
        profile = self.app.db.company_profile(include_defaults=False)
        for key, variable in self.variables.items():
            variable.set(profile.get(key, ""))
        if any(profile.values()):
            self.status_text.set("بيانات شركة مخصصة محفوظة ومفعلة.")
        else:
            self.status_text.set(f"لا توجد بيانات مخصصة؛ الاسم الافتراضي الحالي: {COMPANY_NAME}")

    def save(self):
        payload = {key: variable.get() for key, variable in self.variables.items()}

        def success(profile):
            self.app.refresh_company_identity()
            self.status_text.set(f"تم الحفظ. الهوية النشطة: {profile['company_name']}")
            messagebox.showinfo("تم", "تم حفظ بيانات الشركة وربطها بمخرجات النظام.", parent=self)

        self.app.run_async(
            lambda: self.app.db.save_company_profile(payload, self.app.current_user),
            success,
            busy_text="حفظ بيانات الشركة...",
        )

    def clear_profile(self):
        if not messagebox.askyesno(
            "مسح البيانات المخصصة",
            "سيعود النظام إلى اسم الشركة الافتراضي، ولن تتأثر بيانات الموظفين. متابعة؟",
            parent=self,
        ):
            return
        for variable in self.variables.values():
            variable.set("")
        self.save()


class DashboardPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "لوحة المعلومات")
        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.app.run_async(
            lambda: {**self.app.db.dashboard_stats(), "alerts": len(self.app.enterprise.notifications(120))},
            self.render, busy_text="تحميل لوحة المعلومات..."
        )

    def render(self, stats):
        clear_frame(self.body)
        cards = tk.Frame(self.body, bg=COLORS["background"])
        cards.pack(fill="x", pady=(2, 12))
        values = [
            ("إجمالي الموظفين", stats["total"]),
            ("الموظفون النشطون", stats["active"] or 0),
            ("إجمالي الرواتب", f"{stats['salaries']:,.2f}"),
            ("متوسط الراتب", f"{stats['avg_salary']:,.2f}"),
            ("في إجازة اليوم", stats.get("leave_today", 0)),
            ("تنبيهات قادمة", stats.get("alerts", 0)),
            (
                "الإقامات: منتهية | قريبة",
                f"{stats.get('residency_expired', 0)} | {stats.get('residency_expiring', 0)}",
            ),
            (
                "كرت العمل: منتهي | قريب",
                f"{stats.get('work_permit_expired', 0)} | {stats.get('work_permit_expiring', 0)}",
            ),
            (
                "التأمين الطبي: منتهي | قريب",
                f"{stats.get('medical_insurance_expired', 0)} | {stats.get('medical_insurance_expiring', 0)}",
            ),
            ("تواريخ متابعة ناقصة", stats.get("missing_compliance_dates", 0)),
            (
                "وثائق أخرى: منتهية | قريبة",
                f"{stats.get('other_documents_expired', 0)} | {stats.get('other_documents_expiring', 0)}",
            ),
            ("بيانات بنكية ناقصة", stats.get("missing_bank_details", 0)),
            ("ملفات موظفين غير مكتملة", stats.get("incomplete_profiles", 0)),
            ("تعارضات آخر 4 أرقام", stats.get("employee_code_conflicts", 0)),
        ]
        for column in range(4):
            cards.columnconfigure(column, weight=1, uniform="metrics")
        for index, (title, value) in enumerate(values):
            color = METRIC_COLORS[index % len(METRIC_COLORS)]
            row, column = divmod(index, 4)
            outer = tk.Frame(cards, bg=color, bd=0, highlightthickness=0)
            outer.grid(row=row, column=column, padx=7, pady=7, sticky="nsew")
            inner = tk.Frame(outer, bg=COLORS["surface"], padx=16, pady=13)
            inner.pack(fill="both", expand=True, padx=(5, 0))
            tk.Label(
                inner, text=title, bg=COLORS["surface"], fg=COLORS["muted"],
                font=("Segoe UI", 9, "bold"), anchor="e",
            ).pack(fill="x")
            tk.Label(
                inner, text=str(value), bg=COLORS["surface"], fg=color,
                font=("Segoe UI", 21, "bold"), anchor="e",
            ).pack(fill="x", pady=(5, 0))

        payroll_context = (
            f"مصدر إجمالي الرواتب: {stats.get('salary_source') or 'ملفات الموظفين'}"
            f"  •  الموظفون المستحقون: {int(stats.get('payroll_employees') or 0)}"
        )
        run_period = str(stats.get("payroll_period") or "")
        if run_period:
            payroll_context += (
                f"  •  أحدث مسير: {run_period} ({stats.get('payroll_status') or '—'})"
                f" ويضم {int(stats.get('payroll_run_employees') or 0)} موظف"
            )
        tk.Label(
            cards,
            text=payroll_context,
            bg=COLORS["background"],
            fg="#475569",
            font=("Segoe UI", 9, "bold"),
            anchor="e",
            padx=8,
            pady=3,
        ).grid(row=3, column=0, columnspan=4, sticky="ew", padx=7, pady=(0, 4))

        lower = tk.Frame(self.body, bg=COLORS["background"])
        lower.pack(fill="both", expand=True, pady=4)
        lower.columnconfigure(0, weight=1, uniform="dashboard-lower")
        lower.columnconfigure(1, weight=1, uniform="dashboard-lower")
        lower.columnconfigure(2, weight=1, uniform="dashboard-lower")
        lower.rowconfigure(0, weight=1)

        def panel(parent, title, accent, column):
            frame = tk.Frame(parent, bg=COLORS["surface"], highlightbackground=COLORS["border"], highlightthickness=1)
            frame.grid(row=0, column=column, sticky="nsew", padx=7, pady=5)
            header = tk.Frame(frame, bg=accent, height=42)
            header.pack(fill="x")
            header.pack_propagate(False)
            tk.Label(header, text=title, bg=accent, fg="#FFFFFF", font=("Segoe UI", 11, "bold"), anchor="e", padx=14).pack(fill="both", expand=True)
            body = tk.Frame(frame, bg=COLORS["surface"], padx=14, pady=10)
            body.pack(fill="both", expand=True)
            return body

        recent = panel(lower, "أحدث الموظفين", "#0891B2", 0)
        departments = panel(lower, "توزيع الأقسام", "#7C3AED", 1)
        document_follow_up = panel(lower, "متابعة وثائق الموظفين", "#C2410C", 2)
        for item in stats["departments"]:
            tk.Label(
                departments, text=f"{item['department']}: {item['count']}",
                bg=COLORS["surface"], fg=COLORS["text"], anchor="e", font=("Segoe UI", 10),
            ).pack(fill="x", pady=4)
        if not stats["departments"]:
            tk.Label(departments, text="لا توجد بيانات", bg=COLORS["surface"], fg=COLORS["muted"]).pack(pady=12)
        for item in stats["recent"]:
            tk.Label(
                recent, text=f"{item['emp_code']} — {item['name']} — {item['job_title']}",
                bg=COLORS["surface"], fg=COLORS["text"], anchor="e", font=("Segoe UI", 10),
            ).pack(fill="x", pady=4)
        if not stats["recent"]:
            tk.Label(recent, text="لا توجد بيانات", bg=COLORS["surface"], fg=COLORS["muted"]).pack(pady=12)
        for item in stats.get("document_follow_up", []):
            text = (
                f"{item['emp_code']} — {item['document_type']}\n"
                f"{item.get('start_date') or '—'} إلى {item.get('end_date') or '—'}"
            )
            tk.Label(
                document_follow_up, text=text, bg=COLORS["surface"], fg=COLORS["text"],
                anchor="e", justify="right", font=("Segoe UI", 9),
            ).pack(fill="x", pady=3)
        if not stats.get("document_follow_up"):
            tk.Label(
                document_follow_up, text="لا توجد بيانات وثائق", bg=COLORS["surface"],
                fg=COLORS["muted"],
            ).pack(pady=12)


class SmartAnalyticsPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "التحليل الذكي للقوى العاملة")
        self.analytics: dict = {}
        values = self.app.db.employee_filter_values()
        employees = values["employees"]
        self.employee_by_label = {
            f"{item['emp_code']} — {item['name']}": int(item["id"])
            for item in employees
        }
        saved_states = load_session_state().get("page_states", {})
        saved = saved_states.get("SmartAnalyticsPage", {}) if isinstance(saved_states, dict) else {}
        if not isinstance(saved, dict):
            saved = {}

        filters = ttk.Frame(self)
        filters.pack(fill="x", pady=(3, 8))
        ttk.Label(filters, text="من تاريخ").pack(side="right", padx=(4, 2))
        self.start_var = tk.StringVar(
            value=str(saved.get("start_date") or (date.today() - timedelta(days=29)).isoformat())
        )
        ttk.Entry(filters, textvariable=self.start_var, width=12).pack(side="right", padx=2)
        ttk.Label(filters, text="إلى تاريخ").pack(side="right", padx=(8, 2))
        self.end_var = tk.StringVar(value=str(saved.get("end_date") or date.today().isoformat()))
        ttk.Entry(filters, textvariable=self.end_var, width=12).pack(side="right", padx=2)
        ttk.Label(filters, text="القسم").pack(side="right", padx=(8, 2))
        self.department_var = tk.StringVar(value=str(saved.get("department") or "الكل"))
        department_combo = ttk.Combobox(
            filters,
            textvariable=self.department_var,
            values=("الكل", *values["departments"]),
            state="readonly",
            width=15,
        )
        department_combo.pack(side="right", padx=2)
        ttk.Label(filters, text="الموظف").pack(side="right", padx=(8, 2))
        self.employee_var = tk.StringVar(value=str(saved.get("employee") or "الكل"))
        employee_combo = ttk.Combobox(
            filters,
            textvariable=self.employee_var,
            values=("الكل", *self.employee_by_label.keys()),
            state="readonly",
            width=29,
        )
        employee_combo.pack(side="right", padx=2)
        ttk.Button(filters, text="تطبيق", command=self.load).pack(side="right", padx=6)
        ttk.Button(filters, text="آخر 30 يومًا", command=self.last_thirty_days).pack(side="right", padx=3)
        ttk.Button(filters, text="PDF", command=self.export_pdf).pack(side="left", padx=3)
        ttk.Button(filters, text="Excel", command=self.export_excel).pack(side="left", padx=3)
        for combo in (department_combo, employee_combo):
            combo.bind("<<ComboboxSelected>>", lambda _event: self.load())

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True)
        self.load()

    def last_thirty_days(self):
        self.end_var.set(date.today().isoformat())
        self.start_var.set((date.today() - timedelta(days=29)).isoformat())
        self.load()

    def load(self):
        start_date = self.start_var.get().strip()
        end_date = self.end_var.get().strip()
        department = self.department_var.get()
        employee_label = self.employee_var.get()
        employee_id = self.employee_by_label.get(employee_label)
        save_session_state(
            "SmartAnalyticsPage",
            {
                "start_date": start_date,
                "end_date": end_date,
                "department": department,
                "employee": employee_label,
            },
        )
        self.app.run_async(
            lambda: self.app.db.workforce_analytics(
                start_date=start_date,
                end_date=end_date,
                department=department,
                employee_id=employee_id,
            ),
            self.render,
            busy_text="تشغيل التحليل المحلي...",
        )

    def render(self, analytics):
        self.analytics = analytics
        clear_frame(self.body)
        cards = tk.Frame(self.body, bg=COLORS["background"])
        cards.pack(fill="x", pady=(0, 8))
        metrics = (
            ("الموظفون", analytics["employees"]),
            ("سجلات الحضور", analytics["attendance_records"]),
            ("نسبة الحضور", f"{analytics['attendance_rate']:.1f}%"),
            ("الغياب", analytics["absent"]),
            ("العمل عن بُعد", analytics["remote"]),
            ("أيام الإجازة", analytics["leave_days"]),
        )
        for column in range(6):
            cards.columnconfigure(column, weight=1, uniform="smart-metric")
        for column, (title, value) in enumerate(metrics):
            accent = METRIC_COLORS[column % len(METRIC_COLORS)]
            frame = tk.Frame(cards, bg=COLORS["surface"], highlightbackground=accent, highlightthickness=2)
            frame.grid(row=0, column=column, sticky="nsew", padx=4, pady=3)
            tk.Label(frame, text=title, bg=COLORS["surface"], fg=COLORS["muted"], font=FONT_BOLD).pack(pady=(9, 2))
            tk.Label(frame, text=str(value), bg=COLORS["surface"], fg=accent, font=("Segoe UI", 18, "bold")).pack(pady=(0, 9))

        content = tk.Frame(self.body, bg=COLORS["background"])
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=3)
        content.columnconfigure(1, weight=2)
        content.rowconfigure(0, weight=1)

        chart_panel = ttk.LabelFrame(content, text="اتجاه الغياب خلال الفترة", padding=8)
        chart_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        chart = tk.Canvas(chart_panel, bg="#FFFFFF", highlightthickness=0, height=280)
        chart.pack(fill="both", expand=True)
        daily = list(analytics.get("daily") or [])

        def draw(_event=None):
            chart.delete("all")
            width = max(320, chart.winfo_width())
            height = max(220, chart.winfo_height())
            margin = 34
            chart.create_line(margin, height - margin, width - 10, height - margin, fill="#94A3B8")
            if not daily:
                chart.create_text(width / 2, height / 2, text="لا توجد بيانات ضمن الفترة", fill="#64748B", font=FONT_BOLD)
                return
            maximum = max(1, max(int(item.get("absent") or 0) for item in daily))
            available = max(1, width - margin - 20)
            bar_width = max(3, min(28, available / max(1, len(daily)) - 3))
            step = available / max(1, len(daily))
            for index, item in enumerate(daily):
                value = int(item.get("absent") or 0)
                x1 = margin + index * step + 2
                x2 = x1 + bar_width
                y2 = height - margin
                y1 = y2 - (height - margin * 2) * value / maximum
                chart.create_rectangle(x1, y1, x2, y2, fill="#DC5A4B", outline="")
                if len(daily) <= 18 or index % max(1, len(daily) // 12) == 0:
                    chart.create_text(x1 + bar_width / 2, y2 + 12, text=str(item.get("date", ""))[5:], fill="#64748B", font=("Segoe UI", 7))
                if value:
                    chart.create_text(x1 + bar_width / 2, max(10, y1 - 8), text=str(value), fill="#9F2F27", font=("Segoe UI", 8, "bold"))
        chart.bind("<Configure>", draw)
        self.after_idle(draw)

        insight_panel = ttk.LabelFrame(content, text="قراءة الذكاء الداخلي", padding=10)
        insight_panel.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        for item in analytics.get("insights", []):
            color = {"success": "#0F766E", "warning": "#B45309", "info": "#1D4ED8"}.get(item.get("level"), "#475569")
            block = tk.Frame(insight_panel, bg="#FFFFFF", highlightbackground=color, highlightthickness=1)
            block.pack(fill="x", pady=4)
            tk.Label(block, text=item.get("title", ""), bg="#FFFFFF", fg=color, font=FONT_BOLD, anchor="e").pack(fill="x", padx=8, pady=(6, 1))
            tk.Label(block, text=item.get("message", ""), bg="#FFFFFF", fg=COLORS["text"], justify="right", anchor="e", wraplength=330).pack(fill="x", padx=8, pady=(0, 7))
        forecast = analytics.get("forecast") or {}
        if forecast.get("available") and forecast.get("lower_bound") is not None:
            tk.Label(
                insight_panel,
                text=(
                    f"نطاق التوقع: {float(forecast['lower_bound']):.1f} إلى "
                    f"{float(forecast['upper_bound']):.1f}"
                ),
                foreground="#1D4ED8",
                anchor="e",
                font=FONT_BOLD,
            ).pack(fill="x", pady=(8, 1))
        tk.Label(
            insight_panel,
            text=f"المحرك: {forecast.get('model', 'local-neural-v2')} — المعالجة محلية بالكامل",
            foreground=COLORS["muted"],
            anchor="e",
        ).pack(fill="x", pady=(8, 2))

    def export_excel(self):
        if not self.analytics:
            return
        filename = EXPORT_DIR / f"ProTech_HR_Smart_Analytics_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
        summary = [{
            "الفترة من": self.analytics.get("start_date"),
            "الفترة إلى": self.analytics.get("end_date"),
            "الموظفون": self.analytics.get("employees"),
            "سجلات الحضور": self.analytics.get("attendance_records"),
            "نسبة الحضور": self.analytics.get("attendance_rate"),
            "الغياب": self.analytics.get("absent"),
            "أيام الإجازة": self.analytics.get("leave_days"),
        }]
        data = {
            "SmartSummary": summary,
            "Daily": self.analytics.get("daily", []),
            "Employees": self.analytics.get("people", []),
            "Departments": self.analytics.get("departments", []),
            "Insights": self.analytics.get("insights", []),
        }
        self.app.run_async(
            lambda: export_xlsx(filename, data, company_profile=self.app.db.company_profile()),
            lambda path: (messagebox.showinfo("تم", f"تم إنشاء التقرير:\n{path}", parent=self), open_path(path)),
            busy_text="إنشاء تقرير التحليل Excel...",
        )

    def export_pdf(self):
        if not self.analytics:
            return
        filename = EXPORT_DIR / f"ProTech_HR_Smart_Analytics_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        analytics = dict(self.analytics)
        self.app.run_async(
            lambda: export_workforce_analytics_pdf(filename, analytics, self.app.db.company_profile()),
            lambda path: (messagebox.showinfo("تم", f"تم إنشاء التقرير:\n{path}", parent=self), open_path(path)),
            busy_text="إنشاء تقرير التحليل PDF...",
        )


class OperationsCenterPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "مركز البحث والمخاطر")
        search_bar = ttk.Frame(self)
        search_bar.pack(fill="x", pady=(4, 10))
        ttk.Label(search_bar, text="بحث موحد").pack(side="right", padx=(6, 3))
        self.query_var = tk.StringVar()
        query_entry = ttk.Entry(search_bar, textvariable=self.query_var, width=46)
        query_entry.pack(side="right", padx=3)
        query_entry.bind("<Return>", lambda _event: self.search())
        ttk.Button(search_bar, text="بحث", command=self.search).pack(side="right", padx=4)
        ttk.Button(search_bar, text="تحديث سجل المخاطر", command=self.load_risks).pack(side="left", padx=4)

        panes = ttk.Panedwindow(self, orient="vertical")
        panes.pack(fill="both", expand=True)
        search_panel = ttk.LabelFrame(panes, text="نتائج البحث الموحد", padding=8)
        risk_panel = ttk.LabelFrame(panes, text="المخاطر التشغيلية القابلة للتفسير", padding=8)
        panes.add(search_panel, weight=2)
        panes.add(risk_panel, weight=3)

        self.results_tree = ttk.Treeview(
            search_panel,
            columns=("kind", "title", "details"),
            show="headings",
            height=8,
        )
        for key, label, width in (
            ("kind", "النوع", 120),
            ("title", "النتيجة", 280),
            ("details", "التفاصيل", 520),
        ):
            self.results_tree.heading(key, text=label)
            self.results_tree.column(key, width=width, anchor="e")
        results_scroll = ttk.Scrollbar(search_panel, orient="vertical", command=self.results_tree.yview)
        self.results_tree.configure(yscrollcommand=results_scroll.set)
        self.results_tree.pack(side="right", fill="both", expand=True)
        results_scroll.pack(side="left", fill="y")

        self.risk_tree = ttk.Treeview(
            risk_panel,
            columns=("code", "name", "department", "score", "level", "factors", "action"),
            show="headings",
            height=10,
        )
        for key, label, width in (
            ("code", "الرقم", 90),
            ("name", "الموظف", 170),
            ("department", "القسم", 120),
            ("score", "الدرجة", 70),
            ("level", "المستوى", 80),
            ("factors", "العوامل", 390),
            ("action", "الإجراء المقترح", 300),
        ):
            self.risk_tree.heading(key, text=label)
            self.risk_tree.column(key, width=width, anchor="center" if key in {"code", "score", "level"} else "e")
        risk_y = ttk.Scrollbar(risk_panel, orient="vertical", command=self.risk_tree.yview)
        risk_x = ttk.Scrollbar(risk_panel, orient="horizontal", command=self.risk_tree.xview)
        self.risk_tree.configure(yscrollcommand=risk_y.set, xscrollcommand=risk_x.set)
        self.risk_tree.grid(row=0, column=0, sticky="nsew")
        risk_y.grid(row=0, column=1, sticky="ns")
        risk_x.grid(row=1, column=0, sticky="ew")
        risk_panel.rowconfigure(0, weight=1)
        risk_panel.columnconfigure(0, weight=1)
        self.load_risks()

    def search(self):
        query = self.query_var.get().strip()
        if len(query) < 2:
            messagebox.showinfo("البحث الموحد", "أدخل حرفين على الأقل.", parent=self)
            return
        self.app.run_async(
            lambda: self.app.enterprise.global_search(query, self.app.current_user),
            self.render_results,
            busy_text="البحث في بيانات النظام...",
        )

    def render_results(self, rows):
        self.results_tree.delete(*self.results_tree.get_children())
        for index, row in enumerate(rows):
            self.results_tree.insert(
                "",
                "end",
                iid=f"result-{index}",
                values=(row["kind"], row["title"], row["details"]),
            )
        self.app.set_status(f"نتائج البحث الموحد: {len(rows)}")

    def load_risks(self):
        self.app.run_async(
            lambda: self.app.enterprise.workforce_risk_register(self.app.current_user),
            self.render_risks,
            busy_text="تحليل المخاطر التشغيلية محلياً...",
        )

    def render_risks(self, rows):
        self.risk_tree.delete(*self.risk_tree.get_children())
        for row in rows:
            self.risk_tree.insert(
                "",
                "end",
                iid=str(row["employee_id"]),
                values=(
                    row["emp_code"],
                    row["name"],
                    row["department"] or "—",
                    row["score"],
                    row["level"],
                    "؛ ".join(row["factors"]),
                    row["recommendation"],
                ),
            )
        self.app.set_status(f"تم تحليل {len(rows)} ملف موظف دون إرسال بيانات خارج الجهاز.")


class EmployeesPage(Page):
    columns = (
        ("emp_code", "رقم الموظف", 105),
        ("name", "الاسم", 180),
        ("id_type", "نوع الوثيقة", 105),
        ("id_number", "رقم الهوية / الإقامة / الجواز", 155),
        ("id_issue_date", "تاريخ بداية الهوية", 130),
        ("id_expiry_date", "تاريخ انتهاء الهوية", 130),
        ("identity_status", "حالة الهوية", 170),
        ("work_permit_start_date", "بداية كرت العمل", 130),
        ("work_permit_expiry_date", "انتهاء كرت العمل", 130),
        ("medical_insurance_start_date", "بداية التأمين الطبي", 140),
        ("medical_insurance_expiry_date", "انتهاء التأمين الطبي", 140),
        ("department", "القسم", 140),
        ("job_title", "المسمى الوظيفي", 160),
        ("service_duration", "مدة الخدمة", 175),
        ("performance_rating", "التقييم", 145),
        ("salary", "الراتب", 100),
        ("status", "الحالة", 110),
    )

    def __init__(self, master, app):
        super().__init__(master, app, "إدارة الموظفين")
        page_states = load_session_state().get("page_states", {})
        saved_state = page_states.get("EmployeesPage", {}) if isinstance(page_states, dict) else {}
        if not isinstance(saved_state, dict):
            saved_state = {}
        filter_values = self.app.db.employee_filter_values()
        self.page = 1
        self.page_size = int(saved_state.get("page_size") or PAGE_SIZE)
        self.total = 0
        self.sort_by = "created_at"
        self.descending = True
        self.search_after_id = None
        self.load_token = 0
        filters = ttk.Frame(self)
        filters.pack(fill="x", pady=(5, 2))
        ttk.Label(filters, text="بحث فوري:").pack(side="right", padx=5)
        self.search_var = tk.StringVar(value=str(saved_state.get("search") or ""))
        search = ttk.Entry(filters, textvariable=self.search_var, width=36)
        search.pack(side="right", padx=5)
        self.search_var.trace_add("write", self.schedule_search)
        ttk.Label(filters, text="حالة الهوية:").pack(side="right", padx=(12, 4))
        self.identity_filter_var = tk.StringVar(value=str(saved_state.get("identity_status") or "الكل"))
        identity_filter = ttk.Combobox(
            filters, textvariable=self.identity_filter_var, state="readonly", width=20,
            values=("الكل", "سارية", "تنتهي خلال 90 يومًا", "منتهية", "غير مسجلة"),
        )
        identity_filter.pack(side="right", padx=4)
        identity_filter.bind("<<ComboboxSelected>>", lambda _e: (setattr(self, "page", 1), self.load()))
        ttk.Button(filters, text="تحديث", command=self.load).pack(side="left", padx=4)

        advanced = ttk.Frame(self)
        advanced.pack(fill="x", pady=(2, 2))
        ttk.Label(advanced, text="القسم").pack(side="right", padx=(5, 2))
        self.department_filter_var = tk.StringVar(value=str(saved_state.get("department") or "الكل"))
        department_combo = ttk.Combobox(
            advanced,
            textvariable=self.department_filter_var,
            values=("الكل", *filter_values["departments"]),
            state="readonly",
            width=15,
        )
        department_combo.pack(side="right", padx=2)
        ttk.Label(advanced, text="الحالة").pack(side="right", padx=(8, 2))
        self.employee_status_filter_var = tk.StringVar(value=str(saved_state.get("employee_status") or "الكل"))
        status_combo = ttk.Combobox(
            advanced,
            textvariable=self.employee_status_filter_var,
            values=("الكل", *EMPLOYEE_STATUSES),
            state="readonly",
            width=12,
        )
        status_combo.pack(side="right", padx=2)
        ttk.Label(advanced, text="الالتحاق من").pack(side="right", padx=(8, 2))
        self.joined_from_var = tk.StringVar(value=str(saved_state.get("joined_from") or ""))
        ttk.Entry(advanced, textvariable=self.joined_from_var, width=11).pack(side="right", padx=2)
        ttk.Label(advanced, text="إلى").pack(side="right", padx=(5, 2))
        self.joined_to_var = tk.StringVar(value=str(saved_state.get("joined_to") or ""))
        ttk.Entry(advanced, textvariable=self.joined_to_var, width=11).pack(side="right", padx=2)
        ttk.Label(advanced, text="الراتب من").pack(side="right", padx=(8, 2))
        self.salary_min_var = tk.StringVar(value=str(saved_state.get("salary_min") or ""))
        ttk.Entry(advanced, textvariable=self.salary_min_var, width=9).pack(side="right", padx=2)
        ttk.Label(advanced, text="إلى").pack(side="right", padx=(5, 2))
        self.salary_max_var = tk.StringVar(value=str(saved_state.get("salary_max") or ""))
        ttk.Entry(advanced, textvariable=self.salary_max_var, width=9).pack(side="right", padx=2)
        ttk.Button(advanced, text="تطبيق", command=self.apply_advanced_filters).pack(side="left", padx=3)
        ttk.Button(advanced, text="مسح", command=self.reset_advanced_filters).pack(side="left", padx=3)

        custom_bar = ttk.Frame(self)
        custom_bar.pack(fill="x", pady=(1, 2))
        self.custom_field_by_label = {
            str(item["label"]): int(item["id"])
            for item in filter_values["custom_fields"]
        }
        ttk.Label(custom_bar, text="حقل مخصص").pack(side="right", padx=(5, 2))
        self.custom_field_filter_var = tk.StringVar(value=str(saved_state.get("custom_field") or "الكل"))
        custom_combo = ttk.Combobox(
            custom_bar,
            textvariable=self.custom_field_filter_var,
            values=("الكل", *self.custom_field_by_label.keys()),
            state="readonly",
            width=19,
        )
        custom_combo.pack(side="right", padx=2)
        ttk.Label(custom_bar, text="القيمة تحتوي").pack(side="right", padx=(8, 2))
        self.custom_value_filter_var = tk.StringVar(value=str(saved_state.get("custom_value") or ""))
        custom_value_entry = ttk.Entry(custom_bar, textvariable=self.custom_value_filter_var, width=23)
        custom_value_entry.pack(side="right", padx=2)
        custom_value_entry.bind("<Return>", lambda _e: self.apply_advanced_filters())
        for combo in (department_combo, status_combo, custom_combo):
            combo.bind("<<ComboboxSelected>>", lambda _e: self.apply_advanced_filters())

        saved_views_bar = ttk.Frame(self)
        saved_views_bar.pack(fill="x", pady=(1, 2))
        self.saved_view_by_label: dict[str, dict] = {}
        self.saved_view_var = tk.StringVar()
        ttk.Button(saved_views_bar, text="حذف المنظر", command=self.delete_saved_view).pack(side="left", padx=2)
        ttk.Button(saved_views_bar, text="حفظ المنظر", command=self.save_current_view).pack(side="left", padx=2)
        ttk.Button(saved_views_bar, text="تطبيق المنظر", command=self.apply_saved_view).pack(side="left", padx=2)
        self.saved_view_combo = ttk.Combobox(
            saved_views_bar,
            textvariable=self.saved_view_var,
            state="readonly",
            width=26,
        )
        self.saved_view_combo.pack(side="left", padx=3)
        ttk.Label(saved_views_bar, text="مناظر محفوظة").pack(side="left", padx=(3, 2))
        self.refresh_saved_views()

        # لا تجمع أدوات البحث وكل الإجراءات في صف واحد؛ عند دقة 1050px كانت
        # Tk تخفي الأزرار الواقعة خارج العرض، ومنها استيراد الموظفين.
        employee_actions = ttk.Frame(self)
        employee_actions.pack(fill="x", pady=(2, 1))
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(employee_actions, text="إضافة موظف", command=self.add_employee).pack(side="right", padx=4)
            ttk.Button(employee_actions, text="تعديل المحدد", command=self.edit_selected).pack(side="right", padx=4)
            ttk.Button(
                employee_actions, text="استيراد الموظفين من Excel / CSV",
                command=self.import_employees,
            ).pack(side="right", padx=4)
            ttk.Button(
                employee_actions, text="تنزيل نموذج الاستيراد",
                command=self.save_import_template,
            ).pack(side="right", padx=4)
        if app.current_user["role"] == "admin":
            ttk.Button(
                employee_actions,
                text="الحقول المخصصة",
                command=self.manage_custom_fields,
            ).pack(side="right", padx=4)
        if app.current_user["role"] == "admin":
            ttk.Button(employee_actions, text="حذف المحدد", command=self.delete_selected).pack(side="left", padx=4)

        report_actions = ttk.Frame(self)
        report_actions.pack(fill="x", pady=(1, 2))
        ttk.Button(
            report_actions, text="تقرير الموظف PDF", command=self.export_employee_report
        ).pack(side="right", padx=4)
        ttk.Button(
            report_actions, text="تقرير الموظف Excel فاخر",
            command=self.export_employee_excel_report,
        ).pack(side="right", padx=4)
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(
                report_actions, text="✉ بريد المحدد",
                command=lambda: self.message_selected("email"),
            ).pack(side="right", padx=4)
            ttk.Button(
                report_actions, text="◉ واتساب المحدد",
                command=lambda: self.message_selected("whatsapp"),
            ).pack(side="right", padx=4)

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, pady=8)
        self.tree = ttk.Treeview(table_frame, columns=[c[0] for c in self.columns], show="headings", selectmode="extended")
        for key, label, width in self.columns:
            self.tree.heading(key, text=label, command=lambda k=key: self.sort(k))
            self.tree.column(
                key, width=width,
                anchor="center" if key in {
                    "emp_code", "id_type", "id_number", "id_expiry_date",
                    "id_issue_date", "identity_status", "work_permit_start_date",
                    "work_permit_expiry_date", "medical_insurance_start_date",
                    "medical_insurance_expiry_date", "salary", "status",
                } else "e",
            )
        ybar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _: self.edit_selected())

        pager = ttk.Frame(self)
        pager.pack(fill="x")
        ttk.Button(pager, text="السابق", command=self.previous_page).pack(side="right", padx=5)
        self.page_var = tk.StringVar(value="صفحة 1")
        ttk.Label(pager, textvariable=self.page_var).pack(side="right", padx=10)
        ttk.Button(pager, text="التالي", command=self.next_page).pack(side="right", padx=5)
        ttk.Label(pager, text="عدد الصفوف:").pack(side="left", padx=4)
        self.page_size_var = tk.StringVar(value=str(self.page_size))
        size_combo = ttk.Combobox(pager, textvariable=self.page_size_var, values=(10, 25, 50, 100), width=5, state="readonly")
        size_combo.pack(side="left")
        size_combo.bind("<<ComboboxSelected>>", lambda _: self.change_page_size())
        self.load()

    def current_filter_state(self) -> dict[str, str]:
        return {
            "search": self.search_var.get(),
            "identity_status": self.identity_filter_var.get(),
            "department": self.department_filter_var.get(),
            "employee_status": self.employee_status_filter_var.get(),
            "joined_from": self.joined_from_var.get().strip(),
            "joined_to": self.joined_to_var.get().strip(),
            "salary_min": self.salary_min_var.get().strip(),
            "salary_max": self.salary_max_var.get().strip(),
            "custom_field": self.custom_field_filter_var.get(),
            "custom_value": self.custom_value_filter_var.get().strip(),
            "page_size": str(self.page_size),
        }

    def refresh_saved_views(self):
        views = self.app.db.list_saved_views("employees", self.app.current_user)
        self.saved_view_by_label = {}
        for view in views:
            label = str(view["name"])
            if view.get("is_shared"):
                label += f" (مشترك: {view.get('owner_name') or 'المدير'})"
            if view.get("is_default"):
                label = f"★ {label}"
            self.saved_view_by_label[label] = view
        labels = tuple(self.saved_view_by_label)
        self.saved_view_combo.configure(values=labels)
        if self.saved_view_var.get() not in self.saved_view_by_label:
            self.saved_view_var.set(labels[0] if labels else "")

    def save_current_view(self):
        name = simpledialog.askstring(
            "حفظ منظر البحث",
            "اسم المنظر:",
            parent=self,
        )
        if not name:
            return
        shared = False
        if self.app.current_user.get("role") == "admin":
            shared = messagebox.askyesno(
                "مشاركة المنظر",
                "هل تريد إتاحة هذا المنظر لجميع المستخدمين؟",
                parent=self,
            )
        make_default = messagebox.askyesno(
            "المنظر الافتراضي",
            "هل تريد جعله المنظر الافتراضي لهذا الحساب؟",
            parent=self,
        )
        try:
            self.app.db.save_view(
                "employees",
                name,
                self.current_filter_state(),
                self.app.current_user,
                is_shared=shared,
                is_default=make_default,
            )
        except (DatabaseError, ValueError) as exc:
            messagebox.showerror("تعذر الحفظ", str(exc), parent=self)
            return
        self.refresh_saved_views()
        self.app.set_status(f"تم حفظ منظر البحث: {name.strip()}")

    def apply_saved_view(self):
        view = self.saved_view_by_label.get(self.saved_view_var.get())
        if not view:
            return
        filters = view.get("filters") or {}
        self.search_var.set(str(filters.get("search") or ""))
        self.identity_filter_var.set(str(filters.get("identity_status") or "الكل"))
        self.department_filter_var.set(str(filters.get("department") or "الكل"))
        self.employee_status_filter_var.set(str(filters.get("employee_status") or "الكل"))
        self.joined_from_var.set(str(filters.get("joined_from") or ""))
        self.joined_to_var.set(str(filters.get("joined_to") or ""))
        self.salary_min_var.set(str(filters.get("salary_min") or ""))
        self.salary_max_var.set(str(filters.get("salary_max") or ""))
        self.custom_field_filter_var.set(str(filters.get("custom_field") or "الكل"))
        self.custom_value_filter_var.set(str(filters.get("custom_value") or ""))
        self.page = 1
        self.load()

    def delete_saved_view(self):
        view = self.saved_view_by_label.get(self.saved_view_var.get())
        if not view:
            return
        if not messagebox.askyesno(
            "حذف المنظر",
            f"حذف منظر البحث «{view['name']}»؟",
            parent=self,
        ):
            return
        try:
            self.app.db.delete_saved_view(int(view["id"]), self.app.current_user)
        except DatabaseError as exc:
            messagebox.showerror("تعذر الحذف", str(exc), parent=self)
            return
        self.refresh_saved_views()
        self.app.set_status("تم حذف منظر البحث.")

    def schedule_search(self, *_):
        if self.search_after_id:
            self.after_cancel(self.search_after_id)
        self.search_after_id = self.after(350, self.apply_search)

    def apply_search(self):
        self.page = 1
        self.load()

    def apply_advanced_filters(self):
        self.page = 1
        self.load()

    def reset_advanced_filters(self):
        self.department_filter_var.set("الكل")
        self.employee_status_filter_var.set("الكل")
        self.joined_from_var.set("")
        self.joined_to_var.set("")
        self.salary_min_var.set("")
        self.salary_max_var.set("")
        self.custom_field_filter_var.set("الكل")
        self.custom_value_filter_var.set("")
        self.apply_advanced_filters()

    def load(self):
        query = self.search_var.get()
        page = self.page
        page_size = self.page_size
        sort_by = self.sort_by
        descending = self.descending
        identity_status_map = {
            "الكل": "all", "سارية": "valid", "تنتهي خلال 90 يومًا": "expiring",
            "منتهية": "expired", "غير مسجلة": "missing",
        }
        identity_status = identity_status_map.get(self.identity_filter_var.get(), "all")
        custom_field_label = self.custom_field_filter_var.get()
        custom_field_id = self.custom_field_by_label.get(custom_field_label)
        department = self.department_filter_var.get()
        employee_status = self.employee_status_filter_var.get()
        joined_from = self.joined_from_var.get().strip()
        joined_to = self.joined_to_var.get().strip()
        salary_min = self.salary_min_var.get().strip()
        salary_max = self.salary_max_var.get().strip()
        custom_value = self.custom_value_filter_var.get().strip()
        page_state = {
            "search": query,
            "identity_status": self.identity_filter_var.get(),
            "department": department,
            "employee_status": employee_status,
            "joined_from": joined_from,
            "joined_to": joined_to,
            "salary_min": salary_min,
            "salary_max": salary_max,
            "custom_field": custom_field_label,
            "custom_value": custom_value,
            "page_size": page_size,
        }
        save_session_state("EmployeesPage", page_state)
        self.load_token += 1
        token = self.load_token

        def render_if_current(result):
            if token == self.load_token and self.winfo_exists():
                self.render(result)

        self.app.run_async(
            lambda: self.app.db.list_employees(
                search=query, page=page, page_size=page_size,
                sort_by=sort_by, descending=descending, identity_status=identity_status,
                department=department,
                employee_status=employee_status,
                joined_from=joined_from,
                joined_to=joined_to,
                salary_min=salary_min,
                salary_max=salary_max,
                custom_field_id=custom_field_id,
                custom_value=custom_value,
            ),
            render_if_current,
            busy_text="تحميل الموظفين...",
        )

    def render(self, result):
        rows, self.total = result
        self.tree.delete(*self.tree.get_children())
        for emp in rows:
            self.tree.insert("", "end", iid=str(emp["id"]), values=(
                emp["emp_code"], emp["name"], emp.get("id_type") or "—",
                mask_id_number(emp["id_number"]), emp.get("id_issue_date") or "—",
                emp.get("id_expiry_date") or "—",
                format_identity_expiry_status(emp.get("id_expiry_date")),
                emp.get("work_permit_start_date") or "—",
                emp.get("work_permit_expiry_date") or "—",
                emp.get("medical_insurance_start_date") or "—",
                emp.get("medical_insurance_expiry_date") or "—",
                emp["department"],
                emp["job_title"], format_service_duration(emp.get("join_date")),
                rating_to_stars(emp.get("performance_rating", 0)),
                f"{emp['salary']:,.2f}", emp["status"],
            ))
        pages = max(1, math.ceil(self.total / self.page_size))
        if self.page > pages:
            self.page = pages
            self.load()
            return
        self.page_var.set(f"صفحة {self.page} من {pages} — إجمالي {self.total}")

    def selected_id(self) -> int | None:
        selected = self.tree.selection()
        return int(selected[0]) if selected else None

    def selected_ids(self) -> list[int]:
        return [int(item) for item in self.tree.selection()]

    def message_selected(self, channel: str = "email"):
        employee_ids = self.selected_ids()
        if not employee_ids:
            messagebox.showwarning("تنبيه", "اختر موظفًا أو أكثر أولًا.", parent=self)
            return

        def open_dialog(recipients):
            if recipients:
                MessageComposerDialog(self, self.app, recipients, preset_channel=channel)

        self.app.run_async(
            lambda: self.app.db.list_message_recipients(employee_ids=employee_ids, status="الكل"),
            open_dialog, busy_text="تحميل بيانات المستلمين...",
        )

    def export_employee_report(self):
        employee_id = self.selected_id()
        if not employee_id:
            messagebox.showwarning("تنبيه", "اختر موظفًا واحدًا أولًا.", parent=self)
            return

        def work():
            report = self.app.db.employee_report_data(employee_id)
            employee = report["employee"]
            code = "".join(ch for ch in str(employee.get("emp_code") or employee_id) if ch.isalnum() or ch in "-_")
            filename = EXPORT_DIR / f"ProTech_HR_Employee_{code}_{datetime.now():%Y%m%d_%H%M%S}.pdf"
            return export_employee_pdf(filename, report)

        self.app.run_async(
            work,
            lambda path: (messagebox.showinfo("تم", f"تم إنشاء تقرير الموظف:\n{path}", parent=self), open_path(path)),
            busy_text="إنشاء تقرير الموظف...",
        )

    def export_employee_excel_report(self):
        employee_id = self.selected_id()
        if not employee_id:
            messagebox.showwarning("تنبيه", "اختر موظفًا واحدًا أولًا.", parent=self)
            return

        def work():
            report = self.app.db.employee_report_data(employee_id)
            employee = report["employee"]
            code = "".join(
                ch for ch in str(employee.get("emp_code") or employee_id)
                if ch.isalnum() or ch in "-_"
            )
            filename = EXPORT_DIR / f"ProTech_HR_Employee_{code}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
            return export_employee_xlsx(filename, report)

        self.app.run_async(
            work,
            lambda path: (
                messagebox.showinfo(
                    "تم", f"تم إنشاء تقرير الموظف Excel:\n{path}", parent=self
                ),
                open_path(path),
            ),
            busy_text="إنشاء تقرير الموظف Excel...",
        )

    def add_employee(self):
        EmployeeDialog(self, self.app, on_saved=self.load)

    def manage_custom_fields(self):
        EmployeeCustomFieldsManager(self, self.app, on_saved=self.load)

    def import_employees(self):
        ImportEmployeesDialog(self, self.app, on_imported=self.load)

    def save_import_template(self):
        suggested = f"ProTech_Employee_Import_Template_{date.today():%Y%m%d}.xlsx"
        path = filedialog.asksaveasfilename(
            parent=self, title="حفظ نموذج استيراد الموظفين", initialfile=suggested,
            defaultextension=".xlsx", filetypes=[("Excel Workbook", "*.xlsx")],
        )
        if not path:
            return
        try:
            saved = export_xlsx(
                path, template_rows(), company_profile=self.app.db.company_profile()
            )
            messagebox.showinfo("تم", f"تم إنشاء النموذج:\n{saved}", parent=self)
            open_path(Path(saved).parent)
        except Exception as exc:
            messagebox.showerror("خطأ", str(exc), parent=self)

    def edit_selected(self):
        employee_id = self.selected_id()
        if not employee_id:
            messagebox.showwarning("تنبيه", "اختر موظفًا أولًا.", parent=self)
            return

        def open_dialog(employee):
            if employee:
                EmployeeDialog(self, self.app, employee=employee, on_saved=self.load)

        self.app.run_async(lambda: self.app.db.get_employee(employee_id), open_dialog, busy_text="تحميل بيانات الموظف...")

    def delete_selected(self):
        employee_id = self.selected_id()
        if not employee_id:
            messagebox.showwarning("تنبيه", "اختر موظفًا أولًا.", parent=self)
            return
        if not messagebox.askyesno("تأكيد الحذف", "سيُحذف الموظف وسجلات حضوره وإجازاته. هل تريد المتابعة؟", parent=self):
            return
        self.app.run_async(
            lambda: self.app.db.delete_employee(employee_id, self.app.current_user),
            lambda _: (messagebox.showinfo("تم", "تم حذف الموظف.", parent=self), self.load()),
            busy_text="حذف الموظف...",
        )

    def sort(self, key):
        map_key = "created_at" if key == "id_number" else ("id_expiry_date" if key == "identity_status" else key)
        if self.sort_by == map_key:
            self.descending = not self.descending
        else:
            self.sort_by = map_key
            self.descending = False
        self.page = 1
        self.load()

    def previous_page(self):
        if self.page > 1:
            self.page -= 1
            self.load()

    def next_page(self):
        if self.page * self.page_size < self.total:
            self.page += 1
            self.load()

    def change_page_size(self):
        self.page_size = int(self.page_size_var.get())
        self.page = 1
        self.load()


CHANNEL_LABELS = {"email": "البريد الإلكتروني", "whatsapp": "واتساب", "both": "البريد وواتساب"}
CHANNEL_VALUES = {value: key for key, value in CHANNEL_LABELS.items()}


class MessagingPage(Page):
    """مركز موحد للمراسلات الفردية والجماعية وقوالب الرسائل."""

    def __init__(self, master, app):
        super().__init__(master, app, "مركز المراسلات")
        self.rows: dict[str, dict] = {}
        self.search_after_id = None
        self.load_token = 0

        filters = ttk.LabelFrame(self, text="اختيار المستلمين", padding=10)
        filters.pack(fill="x", pady=(4, 8))
        ttk.Label(filters, text="بحث:").pack(side="right", padx=(8, 3))
        self.search_var = tk.StringVar()
        search = ttk.Entry(filters, textvariable=self.search_var, width=25)
        search.pack(side="right", padx=3)
        self.search_var.trace_add("write", self.schedule_load)

        ttk.Label(filters, text="القسم:").pack(side="right", padx=(12, 3))
        self.department_var = tk.StringVar(value="الكل")
        self.department_combo = ttk.Combobox(
            filters, textvariable=self.department_var, state="readonly", width=20, values=("الكل",)
        )
        self.department_combo.pack(side="right", padx=3)
        self.department_combo.bind("<<ComboboxSelected>>", lambda _event: self.load())

        ttk.Label(filters, text="الحالة:").pack(side="right", padx=(12, 3))
        self.status_var_filter = tk.StringVar(value="نشط")
        status_combo = ttk.Combobox(
            filters, textvariable=self.status_var_filter, state="readonly", width=14,
            values=("الكل", *EMPLOYEE_STATUSES),
        )
        status_combo.pack(side="right", padx=3)
        status_combo.bind("<<ComboboxSelected>>", lambda _event: self.load())

        ttk.Button(filters, text="تحديث", command=self.load).pack(side="left", padx=4)
        ttk.Button(filters, text="إدارة القوالب", command=self.manage_templates).pack(side="left", padx=4)
        ttk.Button(filters, text="رسالة جديدة للمحدد", command=self.compose_selected).pack(side="left", padx=4)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(0, 5))
        ttk.Button(actions, text="تحديد الكل الظاهر", command=self.select_all).pack(side="right", padx=4)
        ttk.Button(actions, text="إلغاء التحديد", command=self.clear_selection).pack(side="right", padx=4)
        self.selection_var = tk.StringVar(value="لم يتم تحديد مستلمين")
        ttk.Label(actions, textvariable=self.selection_var, foreground=COLORS["muted"]).pack(side="right", padx=12)
        ttk.Label(
            actions,
            text="يمكن تحديد عدة موظفين باستخدام Ctrl أو Shift.",
            foreground=COLORS["muted"],
        ).pack(side="left", padx=4)

        pane = ttk.Panedwindow(self, orient="vertical")
        pane.pack(fill="both", expand=True)

        recipients_frame = ttk.LabelFrame(pane, text="الموظفون", padding=6)
        pane.add(recipients_frame, weight=3)
        columns = (
            ("emp_code", "رقم الموظف", 105),
            ("name", "الاسم", 180),
            ("department", "القسم", 150),
            ("email", "البريد", 210),
            ("phone", "الجوال / واتساب", 135),
            ("status", "الحالة", 100),
        )
        self.tree = ttk.Treeview(
            recipients_frame, columns=[item[0] for item in columns], show="headings", selectmode="extended"
        )
        for key, label, width in columns:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="e" if key in {"name", "department", "email"} else "center")
        ybar = ttk.Scrollbar(recipients_frame, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(recipients_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        recipients_frame.rowconfigure(0, weight=1)
        recipients_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self.update_selection_label())
        self.tree.bind("<Double-1>", lambda _event: self.compose_selected())

        history_frame = ttk.LabelFrame(pane, text="آخر عمليات فتح الرسائل", padding=6)
        pane.add(history_frame, weight=1)
        history_columns = (
            ("created_at", "التاريخ", 145), ("employee", "الموظف", 180),
            ("channel", "القناة", 100), ("subject", "العنوان", 220),
            ("status", "الحالة", 120), ("username", "بواسطة", 110),
        )
        self.history = ttk.Treeview(
            history_frame, columns=[item[0] for item in history_columns], show="headings", height=6
        )
        for key, label, width in history_columns:
            self.history.heading(key, text=label)
            self.history.column(key, width=width, anchor="e" if key in {"employee", "subject"} else "center")
        history_y = ttk.Scrollbar(history_frame, orient="vertical", command=self.history.yview)
        self.history.configure(yscrollcommand=history_y.set)
        self.history.grid(row=0, column=0, sticky="nsew")
        history_y.grid(row=0, column=1, sticky="ns")
        history_frame.rowconfigure(0, weight=1)
        history_frame.columnconfigure(0, weight=1)
        self.load()

    def schedule_load(self, *_):
        if self.search_after_id:
            self.after_cancel(self.search_after_id)
        self.search_after_id = self.after(350, self.load)

    def load(self):
        self.load_token += 1
        token = self.load_token
        search = self.search_var.get()
        department = self.department_var.get()
        status = self.status_var_filter.get()

        def operation():
            return {
                "recipients": self.app.db.list_message_recipients(
                    search=search, department=department, status=status
                ),
                "departments": self.app.db.list_departments(),
                "history": self.app.db.list_communication_logs(120),
            }

        def render(result):
            if token != self.load_token or not self.winfo_exists():
                return
            values = ["الكل", *result["departments"]]
            self.department_combo.configure(values=values)
            if self.department_var.get() not in values:
                self.department_var.set("الكل")
            self.rows = {str(row["id"]): row for row in result["recipients"]}
            self.tree.delete(*self.tree.get_children())
            for iid, employee in self.rows.items():
                email = employee.get("email") or "—"
                phone = employee.get("phone") or "—"
                self.tree.insert("", "end", iid=iid, values=(
                    employee["emp_code"], employee["name"], employee.get("department") or "",
                    email, phone, employee.get("status") or "",
                ))
            self.history.delete(*self.history.get_children())
            for row in result["history"]:
                employee_name = row.get("employee_name") or "موظف محذوف"
                if row.get("emp_code"):
                    employee_name = f"{row['emp_code']} — {employee_name}"
                self.history.insert("", "end", values=(
                    row.get("created_at") or "", employee_name,
                    CHANNEL_LABELS.get(row.get("channel"), row.get("channel") or ""),
                    row.get("subject") or "بدون عنوان", row.get("status") or "",
                    row.get("username") or "SYSTEM",
                ))
            self.update_selection_label()

        self.app.run_async(operation, render, busy_text="تحميل مركز المراسلات...")

    def selected_ids(self) -> list[int]:
        return [int(item) for item in self.tree.selection()]

    def select_all(self):
        self.tree.selection_set(self.tree.get_children())
        self.update_selection_label()

    def clear_selection(self):
        self.tree.selection_remove(self.tree.selection())
        self.update_selection_label()

    def update_selection_label(self):
        count = len(self.tree.selection())
        self.selection_var.set(f"تم تحديد {count} موظف" if count else "لم يتم تحديد مستلمين")

    def compose_selected(self):
        selected = [self.rows[item] for item in self.tree.selection() if item in self.rows]
        if not selected:
            messagebox.showwarning("تنبيه", "اختر موظفًا واحدًا أو أكثر.", parent=self)
            return
        MessageComposerDialog(self, self.app, selected, on_logged=self.load)

    def manage_templates(self):
        MessageTemplateManagerDialog(self, self.app)


class MessageComposerDialog(tk.Toplevel):
    def __init__(self, master, app: HRApplication, recipients: list[dict], preset_channel: str = "email", on_logged=None):
        super().__init__(master)
        self.app = app
        self.recipients = recipients
        self.on_logged = on_logged
        self.templates: dict[str, dict] = {}
        self.current_template_id: int | None = None
        self.title(f"إنشاء رسالة — {app.company_name}")
        self.company_profile = app.db.company_profile()
        self.geometry("930x730")
        self.minsize(780, 620)
        self.transient(master)
        self.grab_set()

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="إنشاء رسالة فردية أو جماعية", font=TITLE_FONT).pack(anchor="e")
        self.recipient_summary = tk.StringVar()
        ttk.Label(outer, textvariable=self.recipient_summary, foreground=COLORS["muted"], anchor="e").pack(
            fill="x", pady=(3, 10)
        )

        options = ttk.LabelFrame(outer, text="خيارات الرسالة", padding=10)
        options.pack(fill="x")
        ttk.Label(options, text="القناة:").grid(row=0, column=5, sticky="e", padx=4, pady=4)
        self.channel_var = tk.StringVar(value=CHANNEL_LABELS.get(preset_channel, CHANNEL_LABELS["email"]))
        self.channel_combo = ttk.Combobox(
            options, textvariable=self.channel_var, state="readonly", width=18,
            values=(CHANNEL_LABELS["email"], CHANNEL_LABELS["whatsapp"]),
        )
        self.channel_combo.grid(row=0, column=4, sticky="ew", padx=4, pady=4)
        self.channel_combo.bind("<<ComboboxSelected>>", lambda _event: self.channel_changed())

        ttk.Label(options, text="القالب:").grid(row=0, column=3, sticky="e", padx=4, pady=4)
        self.template_var = tk.StringVar(value="رسالة جديدة")
        self.template_combo = ttk.Combobox(options, textvariable=self.template_var, state="readonly", width=30)
        self.template_combo.grid(row=0, column=2, sticky="ew", padx=4, pady=4)
        self.template_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_template())

        ttk.Label(options, text="طريقة الإرسال:").grid(row=0, column=1, sticky="e", padx=4, pady=4)
        self.mode_var = tk.StringVar()
        self.mode_combo = ttk.Combobox(options, textvariable=self.mode_var, state="readonly", width=28)
        self.mode_combo.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        options.columnconfigure(2, weight=1)
        options.columnconfigure(4, weight=1)

        recipient_frame = ttk.Frame(outer, padding=(0, 7))
        recipient_frame.pack(fill="x")
        ttk.Label(recipient_frame, text="المستلمون تلقائيًا:").pack(side="right", padx=(6, 0))
        self.recipient_detail_var = tk.StringVar()
        recipient_entry = ttk.Entry(recipient_frame, textvariable=self.recipient_detail_var, state="readonly")
        recipient_entry.pack(side="right", fill="x", expand=True, padx=5)
        ttk.Label(
            recipient_frame,
            text="يتم جلب البريد أو الجوال من بيانات الموظفين المحددين ولا يلزم إدخاله يدويًا.",
            foreground=COLORS["muted"],
        ).pack(side="left", padx=5)

        action_bar = ttk.Frame(outer, padding=(0, 8))
        action_bar.pack(fill="x")
        ttk.Button(
            action_bar, text="✉ إرسال / فتح في Outlook", command=self.open_in_outlook,
            style="Outlook.TButton",
        ).pack(side="right", padx=5, ipadx=12)
        ttk.Button(
            action_bar, text="◉ إرسال / فتح في WhatsApp", command=self.open_in_whatsapp,
            style="WhatsApp.TButton",
        ).pack(side="right", padx=5, ipadx=12)
        ttk.Button(action_bar, text="حفظ كقالب", command=self.save_as_template).pack(side="right", padx=5)
        ttk.Button(action_bar, text="إدارة القوالب", command=self.manage_templates).pack(side="right", padx=5)
        ttk.Button(action_bar, text="إغلاق", command=self.destroy, style="Secondary.TButton").pack(side="left", padx=5)

        subject_frame = ttk.Frame(outer)
        subject_frame.pack(fill="x", pady=(10, 5))
        ttk.Label(subject_frame, text="عنوان البريد:").pack(side="right", padx=(6, 0))
        self.subject_var = tk.StringVar()
        self.subject_entry = ttk.Entry(subject_frame, textvariable=self.subject_var)
        self.subject_entry.pack(side="right", fill="x", expand=True, padx=5)

        body_frame = ttk.LabelFrame(outer, text="نص الرسالة", padding=8)
        body_frame.pack(fill="both", expand=True, pady=5)
        self.body = tk.Text(body_frame, wrap="word", height=18, font=("Segoe UI", 11), undo=True)
        body_scroll = ttk.Scrollbar(body_frame, orient="vertical", command=self.body.yview)
        self.body.configure(yscrollcommand=body_scroll.set)
        self.body.grid(row=0, column=0, sticky="nsew")
        body_scroll.grid(row=0, column=1, sticky="ns")
        body_frame.rowconfigure(0, weight=1)
        body_frame.columnconfigure(0, weight=1)

        placeholder_text = "  |  ".join(f"{{{key}}} = {label}" for key, label in PLACEHOLDERS.items())
        ttk.Label(
            outer, text=f"الحقول المتغيرة: {placeholder_text}", foreground=COLORS["muted"],
            wraplength=860, justify="right", anchor="e",
        ).pack(fill="x", pady=(3, 8))

        note = (
            "البريد الجماعي يستخدم BCC لحماية عناوين الموظفين. واتساب يفتح المحادثات واحدة تلو الأخرى "
            "ولا يضغط زر الإرسال تلقائيًا؛ الإرسال الآلي الكامل يحتاج WhatsApp Business API معتمدًا."
        )
        ttk.Label(outer, text=note, foreground="#B45309", wraplength=850, justify="right", anchor="e").pack(fill="x")

        self.load_templates()
        self.channel_changed()
        self.update_recipient_summary()

    def channel_key(self) -> str:
        return CHANNEL_VALUES.get(self.channel_var.get(), "email")

    def load_templates(self, select_name: str | None = None):
        channel = self.channel_key()
        rows = self.app.db.list_message_templates(channel=channel)
        self.templates = {row["name"]: row for row in rows}
        values = ["رسالة جديدة", *self.templates.keys()]
        self.template_combo.configure(values=values)
        if select_name and select_name in self.templates:
            self.template_var.set(select_name)
            self.apply_template()
        elif self.template_var.get() not in values:
            self.template_var.set("رسالة جديدة")
            self.current_template_id = None

    def channel_changed(self):
        channel = self.channel_key()
        if channel == "email":
            modes = ("رسالة جماعية واحدة (BCC)", "رسالة منفصلة لكل موظف") if len(self.recipients) > 1 else ("رسالة فردية",)
            self.subject_entry.configure(state="normal")
        else:
            modes = ("قائمة إرسال واتساب",)
            self.subject_entry.configure(state="disabled")
        self.mode_combo.configure(values=modes)
        self.mode_var.set(modes[0])
        self.load_templates()
        self.update_recipient_summary()

    def update_recipient_summary(self):
        valid_emails = [row for row in self.recipients if is_valid_email(row.get("email") or "")]
        valid_whatsapp = [row for row in self.recipients if normalize_whatsapp_phone(row.get("phone") or "")]
        self.recipient_summary.set(
            f"المحددون: {len(self.recipients)}  |  بريد صالح: {len(valid_emails)}  |  واتساب صالح: {len(valid_whatsapp)}"
        )
        if self.channel_key() == "email":
            values = [f"{row.get('name', '')} <{row.get('email', '')}>" for row in valid_emails]
            missing = len(self.recipients) - len(valid_emails)
            detail = "; ".join(values)
            if missing:
                detail += ("  |  " if detail else "") + f"{missing} بدون بريد صالح"
        else:
            values = [
                f"{row.get('name', '')} (+{normalize_whatsapp_phone(row.get('phone') or '')})"
                for row in valid_whatsapp
            ]
            missing = len(self.recipients) - len(valid_whatsapp)
            detail = "; ".join(values)
            if missing:
                detail += ("  |  " if detail else "") + f"{missing} بدون رقم واتساب صالح"
        self.recipient_detail_var.set(detail or "لا توجد بيانات اتصال صالحة")

    def apply_template(self):
        row = self.templates.get(self.template_var.get())
        if not row:
            self.current_template_id = None
            return
        self.current_template_id = int(row["id"])
        self.subject_var.set(row.get("subject") or "")
        self.body.delete("1.0", "end")
        self.body.insert("1.0", row.get("body") or "")

    def save_as_template(self):
        initial = {
            "name": "",
            "channel": self.channel_key(),
            "subject": self.subject_var.get(),
            "body": self.body.get("1.0", "end").strip(),
            "is_active": 1,
        }
        TemplateEditorDialog(
            self, self.app, initial=initial,
            on_saved=lambda name: self.load_templates(select_name=name),
        )

    def manage_templates(self):
        MessageTemplateManagerDialog(self, self.app, on_changed=self.load_templates)

    def _body_text(self) -> str | None:
        body_text = self.body.get("1.0", "end").strip()
        if not body_text:
            messagebox.showwarning("تنبيه", "اكتب نص الرسالة أولًا.", parent=self)
            return None
        return body_text

    def open_in_outlook(self):
        """يفتح الرسالة داخل Outlook مباشرة، مع بديل آمن إن لم يكن متاحًا."""
        self.open_for_sending(force_channel="email", preferred_app="outlook")

    def open_in_whatsapp(self):
        """يفتح تطبيق WhatsApp Desktop مباشرة، أو WhatsApp Web كبديل."""
        self.open_for_sending(force_channel="whatsapp", preferred_app="whatsapp")

    def open_for_sending(self, force_channel: str | None = None, preferred_app: str = ""):
        body_text = self._body_text()
        if body_text is None:
            return
        channel = force_channel or self.channel_key()
        subject = self.subject_var.get().strip()
        if channel == "email":
            valid = [row for row in self.recipients if is_valid_email(row.get("email") or "")]
            if not valid:
                messagebox.showerror("تعذر الإرسال", "لا يوجد بريد إلكتروني صالح للمستلمين المحددين.", parent=self)
                return
            missing = len(self.recipients) - len(valid)
            if missing and not messagebox.askyesno(
                "عناوين ناقصة",
                f"سيتم تجاهل {missing} موظف لعدم وجود بريد صالح. هل تريد المتابعة؟",
                parent=self,
            ):
                return

            # عند الضغط على زر Outlook من قناة أخرى نستخدم BCC افتراضيًا للمجموعة.
            mode = self.mode_var.get() if self.channel_key() == "email" else (
                "رسالة جماعية واحدة (BCC)" if len(valid) > 1 else "رسالة فردية"
            )
            if len(valid) == 1 or mode == "رسالة جماعية واحدة (BCC)":
                group = len(valid) > 1
                employee = None if group else valid[0]
                rendered_subject = render_template(
                    subject, employee, group=group, company_profile=self.company_profile
                )
                rendered_body = render_template(
                    body_text, employee, group=group, company_profile=self.company_profile
                )
                try:
                    opener = open_outlook_draft if preferred_app == "outlook" else open_email_draft
                    method, _target = opener(
                        [row["email"] for row in valid], rendered_subject, rendered_body,
                        use_bcc=group, open_file_callback=open_path,
                    )
                    status = {
                        "outlook_com": "OUTLOOK_OPENED",
                        "outlook_macos": "OUTLOOK_OPENED",
                        "mailto": "DRAFT_OPENED",
                        "eml": "EML_CREATED",
                    }.get(method, "DRAFT_OPENED")
                    self.app.db.log_communication(
                        [row["id"] for row in valid], "email", rendered_subject,
                        "group_bcc" if group else "individual", status,
                        self.app.current_user, self.current_template_id,
                    )
                except Exception as exc:
                    messagebox.showerror("تعذر فتح Outlook", str(exc), parent=self)
                    return

                if method in {"outlook_com", "outlook_macos"}:
                    detail = "تم فتح الرسالة داخل Microsoft Outlook. راجعها ثم اضغط إرسال."
                elif method == "mailto":
                    detail = "تم فتح الرسالة في برنامج البريد المسجل في الجهاز. اجعل Outlook التطبيق الافتراضي لفتحه دائمًا."
                else:
                    detail = "تم إنشاء ملف مسودة EML وفتحه. راجع الرسالة ثم اضغط إرسال."
                messagebox.showinfo("تم تجهيز الرسالة", detail, parent=self)
                if self.on_logged:
                    self.on_logged()
            else:
                MessageQueueDialog(
                    self, self.app, "email", valid, subject, body_text,
                    template_id=self.current_template_id, on_logged=self.on_logged,
                    preferred_app=preferred_app or "outlook",
                )
        else:
            valid = [row for row in self.recipients if normalize_whatsapp_phone(row.get("phone") or "")]
            if not valid:
                messagebox.showerror("تعذر الإرسال", "لا يوجد رقم جوال صالح لواتساب ضمن المستلمين.", parent=self)
                return
            missing = len(self.recipients) - len(valid)
            if missing and not messagebox.askyesno(
                "أرقام ناقصة",
                f"سيتم تجاهل {missing} موظف لعدم وجود رقم واتساب صالح. هل تريد المتابعة؟",
                parent=self,
            ):
                return

            if len(valid) == 1:
                employee = valid[0]
                rendered_body = render_template(
                    body_text, employee, company_profile=self.company_profile
                )
                try:
                    method, _target = open_whatsapp_desktop(employee.get("phone") or "", rendered_body)
                    self.app.db.log_communication(
                        [employee["id"]], "whatsapp", "", "individual",
                        "WHATSAPP_APP_OPENED" if method == "whatsapp_app" else "WHATSAPP_WEB_OPENED",
                        self.app.current_user, self.current_template_id,
                    )
                except Exception as exc:
                    messagebox.showerror("تعذر فتح WhatsApp", str(exc), parent=self)
                    return
                detail = (
                    "تم فتح المحادثة في تطبيق WhatsApp. راجع الرسالة ثم اضغط إرسال."
                    if method == "whatsapp_app"
                    else "تعذر تشغيل تطبيق WhatsApp، لذلك تم فتح WhatsApp Web كبديل."
                )
                messagebox.showinfo("تم تجهيز رسالة واتساب", detail, parent=self)
                if self.on_logged:
                    self.on_logged()
            else:
                MessageQueueDialog(
                    self, self.app, "whatsapp", valid, "", body_text,
                    template_id=self.current_template_id, on_logged=self.on_logged,
                    preferred_app=preferred_app or "whatsapp",
                )


class MessageQueueDialog(tk.Toplevel):
    """طابور آمن يفتح المسودات واحدة تلو الأخرى دون إرسال صامت أو جماعي مخالف."""

    def __init__(self, master, app, channel: str, recipients: list[dict], subject: str, body: str,
                 template_id: int | None = None, on_logged=None, preferred_app: str = ""):
        super().__init__(master)
        self.app = app
        self.channel = channel
        self.recipients = recipients
        self.subject = subject
        self.body_text = body
        self.template_id = template_id
        self.on_logged = on_logged
        self.preferred_app = preferred_app
        self.index = 0
        self.opened: set[int] = set()
        self.title("طابور إرسال البريد" if channel == "email" else "طابور إرسال واتساب")
        self.geometry("690x430")
        self.transient(master)
        self.grab_set()

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=self.title(), font=TITLE_FONT).pack(anchor="e")
        self.progress_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.progress_var, foreground=COLORS["muted"]).pack(anchor="e", pady=(3, 12))

        card = ttk.LabelFrame(frame, text="المستلم الحالي", padding=12)
        card.pack(fill="x")
        self.name_var = tk.StringVar()
        self.contact_var = tk.StringVar()
        ttk.Label(card, textvariable=self.name_var, font=("Segoe UI", 14, "bold"), anchor="e").pack(fill="x")
        ttk.Label(card, textvariable=self.contact_var, foreground=COLORS["muted"], anchor="e").pack(fill="x", pady=4)

        preview_frame = ttk.LabelFrame(frame, text="معاينة", padding=8)
        preview_frame.pack(fill="both", expand=True, pady=10)
        self.preview = tk.Text(preview_frame, wrap="word", height=8, state="disabled")
        self.preview.pack(fill="both", expand=True)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        button_text = "فتح في Outlook" if channel == "email" else "فتح في WhatsApp"
        button_style = "Outlook.TButton" if channel == "email" else "WhatsApp.TButton"
        self.open_button = ttk.Button(buttons, text=button_text, command=self.open_current, style=button_style)
        self.open_button.pack(side="right", padx=5)
        ttk.Button(buttons, text="تخطي", command=self.skip).pack(side="right", padx=5)
        ttk.Button(buttons, text="السابق", command=self.previous).pack(side="right", padx=5)
        ttk.Button(buttons, text="إغلاق", command=self.destroy).pack(side="left", padx=5)
        self.refresh()

    def current(self) -> dict | None:
        if 0 <= self.index < len(self.recipients):
            return self.recipients[self.index]
        return None

    def refresh(self):
        employee = self.current()
        if employee is None:
            self.progress_var.set(f"اكتمل الطابور — تم فتح {len(self.opened)} من {len(self.recipients)}")
            self.name_var.set("لا يوجد مستلم آخر")
            self.contact_var.set("")
            self.open_button.configure(state="disabled")
            self.set_preview("تم الوصول إلى نهاية القائمة.")
            if self.on_logged:
                self.on_logged()
            return
        self.progress_var.set(f"المستلم {self.index + 1} من {len(self.recipients)} — تم فتح {len(self.opened)}")
        self.name_var.set(f"{employee.get('emp_code', '')} — {employee.get('name', '')}")
        contact = employee.get("email") if self.channel == "email" else employee.get("phone")
        self.contact_var.set(str(contact or ""))
        subject = render_template(
            self.subject, employee, company_profile=self.app.db.company_profile()
        )
        body = render_template(
            self.body_text, employee, company_profile=self.app.db.company_profile()
        )
        preview = f"العنوان: {subject}\n\n{body}" if self.channel == "email" else body
        self.set_preview(preview)
        self.open_button.configure(state="normal")

    def set_preview(self, text: str):
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", text)
        self.preview.configure(state="disabled")

    def open_current(self):
        employee = self.current()
        if not employee:
            return
        try:
            profile = self.app.db.company_profile()
            subject = render_template(self.subject, employee, company_profile=profile)
            body = render_template(self.body_text, employee, company_profile=profile)
            if self.channel == "email":
                opener = open_outlook_draft if self.preferred_app == "outlook" else open_email_draft
                method, _target = opener(
                    [employee.get("email") or ""], subject, body,
                    open_file_callback=open_path,
                )
                status = {
                    "outlook_com": "OUTLOOK_OPENED",
                    "outlook_macos": "OUTLOOK_OPENED",
                    "mailto": "DRAFT_OPENED",
                    "eml": "EML_CREATED",
                }.get(method, "DRAFT_OPENED")
            else:
                method, _target = open_whatsapp_desktop(employee.get("phone") or "", body)
                status = "WHATSAPP_APP_OPENED" if method == "whatsapp_app" else "WHATSAPP_WEB_OPENED"
            self.app.db.log_communication(
                [employee["id"]], self.channel, subject,
                "individual_queue", status, self.app.current_user, self.template_id,
            )
            self.opened.add(int(employee["id"]))
            self.index += 1
            self.refresh()
        except Exception as exc:
            messagebox.showerror("تعذر فتح الرسالة", str(exc), parent=self)

    def skip(self):
        if self.current() is not None:
            self.index += 1
            self.refresh()

    def previous(self):
        if self.index > 0:
            self.index -= 1
            self.refresh()


class MessageTemplateManagerDialog(tk.Toplevel):
    def __init__(self, master, app, on_changed=None):
        super().__init__(master)
        self.app = app
        self.on_changed = on_changed
        self.rows: dict[str, dict] = {}
        self.title("إدارة قوالب الرسائل")
        self.geometry("820x520")
        self.transient(master)
        self.grab_set()
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="قوالب الرسائل", font=TITLE_FONT).pack(anchor="e", pady=(0, 10))

        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True)
        columns = (("name", "اسم القالب", 260), ("channel", "القناة", 140), ("active", "الحالة", 100),
                   ("updated", "آخر تحديث", 160))
        self.tree = ttk.Treeview(table_frame, columns=[c[0] for c in columns], show="headings", selectmode="browse")
        for key, label, width in columns:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="e" if key == "name" else "center")
        bar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self.edit_selected())

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="قالب جديد", command=self.new_template).pack(side="right", padx=4)
        ttk.Button(buttons, text="تعديل", command=self.edit_selected).pack(side="right", padx=4)
        ttk.Button(buttons, text="حذف", command=self.delete_selected).pack(side="right", padx=4)
        ttk.Button(buttons, text="إغلاق", command=self.destroy).pack(side="left", padx=4)
        self.load()

    def load(self):
        rows = self.app.db.list_message_templates(active_only=False)
        self.rows = {str(row["id"]): row for row in rows}
        self.tree.delete(*self.tree.get_children())
        for iid, row in self.rows.items():
            self.tree.insert("", "end", iid=iid, values=(
                row["name"], CHANNEL_LABELS.get(row["channel"], row["channel"]),
                "مفعّل" if row["is_active"] else "معطّل", row.get("updated_at") or "",
            ))
        if self.on_changed:
            self.on_changed()

    def selected(self) -> dict | None:
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def new_template(self):
        TemplateEditorDialog(self, self.app, on_saved=lambda _name: self.load())

    def edit_selected(self):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر قالبًا أولًا.", parent=self)
            return
        TemplateEditorDialog(self, self.app, initial=row, template_id=int(row["id"]), on_saved=lambda _name: self.load())

    def delete_selected(self):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر قالبًا أولًا.", parent=self)
            return
        if not messagebox.askyesno("تأكيد الحذف", f"هل تريد حذف القالب: {row['name']}؟", parent=self):
            return
        try:
            self.app.db.delete_message_template(int(row["id"]), self.app.current_user)
            self.load()
        except Exception as exc:
            messagebox.showerror("خطأ", str(exc), parent=self)


class TemplateEditorDialog(tk.Toplevel):
    def __init__(self, master, app, initial: dict | None = None, template_id: int | None = None, on_saved=None):
        super().__init__(master)
        self.app = app
        self.template_id = template_id
        self.on_saved = on_saved
        initial = initial or {}
        self.title("تعديل قالب" if template_id else "قالب جديد")
        self.geometry("720x610")
        self.transient(master)
        self.grab_set()
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=self.title(), font=TITLE_FONT).pack(anchor="e", pady=(0, 10))

        form = ttk.Frame(frame)
        form.pack(fill="x")
        ttk.Label(form, text="اسم القالب:").grid(row=0, column=3, sticky="e", padx=5, pady=5)
        self.name_var = tk.StringVar(value=str(initial.get("name") or ""))
        ttk.Entry(form, textvariable=self.name_var).grid(row=0, column=2, sticky="ew", padx=5, pady=5)
        ttk.Label(form, text="القناة:").grid(row=0, column=1, sticky="e", padx=5, pady=5)
        self.channel_var = tk.StringVar(value=CHANNEL_LABELS.get(str(initial.get("channel") or "both"), CHANNEL_LABELS["both"]))
        ttk.Combobox(
            form, textvariable=self.channel_var, state="readonly", width=18,
            values=(CHANNEL_LABELS["both"], CHANNEL_LABELS["email"], CHANNEL_LABELS["whatsapp"]),
        ).grid(row=0, column=0, sticky="ew", padx=5, pady=5)
        ttk.Label(form, text="عنوان البريد:").grid(row=1, column=3, sticky="e", padx=5, pady=5)
        self.subject_var = tk.StringVar(value=str(initial.get("subject") or ""))
        ttk.Entry(form, textvariable=self.subject_var).grid(row=1, column=0, columnspan=3, sticky="ew", padx=5, pady=5)
        form.columnconfigure(0, weight=1)
        form.columnconfigure(2, weight=1)

        body_frame = ttk.LabelFrame(frame, text="نص القالب", padding=8)
        body_frame.pack(fill="both", expand=True, pady=8)
        self.body = tk.Text(body_frame, wrap="word", font=("Segoe UI", 11), undo=True)
        self.body.pack(fill="both", expand=True)
        self.body.insert("1.0", str(initial.get("body") or ""))

        self.active_var = tk.BooleanVar(value=bool(initial.get("is_active", 1)))
        ttk.Checkbutton(frame, text="القالب مفعّل", variable=self.active_var).pack(anchor="e")
        placeholder_text = "  |  ".join(f"{{{key}}}" for key in PLACEHOLDERS)
        ttk.Label(frame, text=f"الحقول المتاحة: {placeholder_text}", foreground=COLORS["muted"], anchor="e").pack(fill="x", pady=5)

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="حفظ", command=self.save).pack(side="right", padx=5)
        ttk.Button(buttons, text="إغلاق", command=self.destroy).pack(side="left", padx=5)

    def save(self):
        data = {
            "name": self.name_var.get().strip(),
            "channel": CHANNEL_VALUES.get(self.channel_var.get(), "both"),
            "subject": self.subject_var.get().strip(),
            "body": self.body.get("1.0", "end").strip(),
            "is_active": self.active_var.get(),
        }
        try:
            self.app.db.save_message_template(data, self.app.current_user, self.template_id)
        except Exception as exc:
            messagebox.showerror("تعذر الحفظ", str(exc), parent=self)
            return
        if self.on_saved:
            self.on_saved(data["name"])
        self.destroy()



class ImportEmployeesDialog(tk.Toplevel):
    PREVIEW_LIMIT = 1000

    def __init__(self, master, app: HRApplication, on_imported=None):
        super().__init__(master)
        self.app = app
        self.on_imported = on_imported
        self.parsed_sheets = []
        self.preview_result: dict | None = None
        self.analysis_token = 0
        self.title(f"استيراد الموظفين — {app.company_name}")
        self.geometry("1180x720")
        self.minsize(920, 600)
        self.transient(master)
        self.grab_set()

        outer = ttk.Frame(self, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="استيراد بيانات الموظفين من Excel أو CSV", font=TITLE_FONT).pack(anchor="e")
        ttk.Label(
            outer,
            text="يدعم XLSX وXLS وCSV، ويكتشف العناوين وترتيب الأعمدة تلقائيًا حتى عند امتدادها على عدة صفوف.",
            foreground="#555", anchor="e",
        ).pack(fill="x", pady=(2, 10))

        file_frame = ttk.LabelFrame(outer, text="الملف", padding=10)
        file_frame.pack(fill="x")
        self.file_var = tk.StringVar()
        ttk.Entry(file_frame, textvariable=self.file_var, state="readonly").pack(side="right", fill="x", expand=True, padx=5)
        ttk.Button(file_frame, text="اختيار ملف", command=self.choose_file).pack(side="right", padx=5)
        ttk.Button(file_frame, text="إنشاء نموذج Excel", command=self.save_template).pack(side="right", padx=5)

        options = ttk.Frame(outer)
        options.pack(fill="x", pady=8)
        ttk.Label(options, text="ورقة العمل:").pack(side="right", padx=(6, 2))
        self.sheet_var = tk.StringVar()
        self.sheet_combo = ttk.Combobox(options, textvariable=self.sheet_var, state="readonly", width=32)
        self.sheet_combo.pack(side="right", padx=4)
        self.sheet_combo.bind("<<ComboboxSelected>>", lambda _: self.analyze_current())
        ttk.Label(options, text="عند تكرار رقم الهوية/الإقامة/الجواز:").pack(side="right", padx=(20, 2))
        self.duplicate_mode = tk.StringVar(value="skip")
        ttk.Radiobutton(
            options, text="تخطي الموجود", value="skip", variable=self.duplicate_mode,
            command=self.analyze_current,
        ).pack(side="right", padx=4)
        ttk.Radiobutton(
            options, text="تحديث الموجود", value="update", variable=self.duplicate_mode,
            command=self.analyze_current,
        ).pack(side="right", padx=4)

        self.info_var = tk.StringVar(value="اختر ملفًا لعرض معاينة البيانات قبل الاستيراد.")
        ttk.Label(outer, textvariable=self.info_var, anchor="e", justify="right", padding=5).pack(fill="x")

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True, pady=6)
        columns = ("row", "emp_code", "name", "id_type", "id_number", "department", "job_title", "salary", "state", "message")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        specs = (
            ("row", "الصف", 55), ("emp_code", "رقم الموظف", 100), ("name", "الاسم", 150),
            ("id_type", "نوع الوثيقة", 100), ("id_number", "رقم الوثيقة", 105), ("department", "القسم", 120),
            ("job_title", "المسمى", 135), ("salary", "الراتب", 85),
            ("state", "النتيجة", 90), ("message", "التفاصيل", 260),
        )
        for key, label, width in specs:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center" if key not in {"name", "department", "job_title", "message"} else "e")
        ybar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x", pady=(8, 0))
        self.import_button = ttk.Button(buttons, text="استيراد وحفظ", command=self.start_import, state="disabled")
        self.import_button.pack(side="right", padx=5, ipadx=18, ipady=5)
        ttk.Button(buttons, text="إغلاق", command=self.destroy).pack(side="right", padx=5, ipadx=18, ipady=5)

    def choose_file(self):
        path = filedialog.askopenfilename(
            parent=self, title="اختيار ملف الموظفين",
            filetypes=[
                ("Excel / CSV", "*.xlsx *.xls *.csv"),
                ("Excel Workbook", "*.xlsx *.xls"),
                ("Excel حديث", "*.xlsx"),
                ("Excel قديم", "*.xls"),
                ("CSV", "*.csv"),
            ],
        )
        if not path:
            return
        self.file_var.set(path)
        self.import_button.configure(state="disabled")
        self.info_var.set("جارٍ قراءة الملف وتحليل أوراق العمل...")
        self.tree.delete(*self.tree.get_children())

        def work():
            parsed = []
            errors = []
            for sheet in read_employee_file(path):
                try:
                    result = parse_employee_sheet(sheet)
                    if result.records:
                        parsed.append(result)
                except Exception as exc:
                    errors.append(f"{sheet.name}: {exc}")
            if not parsed:
                details = "\n".join(errors[:5])
                raise ValueError("لم يتم العثور على ورقة تحتوي بيانات موظفين مفهومة." + (f"\n{details}" if details else ""))
            return parsed, errors

        self.app.run_async(work, self.file_loaded, busy_text="تحليل ملف الاستيراد...")

    def file_loaded(self, result):
        parsed, errors = result
        self.parsed_sheets = parsed
        values = [f"{item.sheet_name} — {len(item.records)} صف" for item in parsed]
        self.sheet_combo["values"] = values
        self.sheet_combo.current(0)
        if errors:
            self.info_var.set(f"تم العثور على {len(parsed)} ورقة قابلة للاستيراد، وتعذر فهم {len(errors)} ورقة أخرى.")
        self.analyze_current()

    def current_sheet(self):
        index = self.sheet_combo.current()
        if 0 <= index < len(self.parsed_sheets):
            return self.parsed_sheets[index]
        return None

    def analyze_current(self):
        sheet = self.current_sheet()
        if not sheet:
            return
        self.analysis_token += 1
        token = self.analysis_token
        self.import_button.configure(state="disabled")
        mode = self.duplicate_mode.get()
        self.info_var.set("جارٍ مقارنة البيانات مع قاعدة الموظفين الحالية...")

        def completed(result):
            if token == self.analysis_token and self.winfo_exists():
                self.preview_result = result
                self.render_preview(result, sheet)

        self.app.run_async(
            lambda: self.app.db.analyze_employee_import(sheet.records, mode),
            completed, busy_text="فحص بيانات الاستيراد...",
        )

    def render_preview(self, result, sheet):
        self.tree.delete(*self.tree.get_children())
        for item in result["rows"][: self.PREVIEW_LIMIT]:
            self.tree.insert("", "end", values=(
                item.get("row", ""), item.get("emp_code", ""), item.get("name", ""),
                item.get("id_type", ""), mask_id_number(str(item.get("id_number", ""))),
                item.get("department", ""),
                item.get("job_title", ""), item.get("salary", ""), item.get("state", ""),
                item.get("message", ""),
            ))
        counts = result["counts"]
        ignored = f" — أعمدة إضافية محفوظة دون استخدام: {len(sheet.ignored_headers)}" if sheet.ignored_headers else ""
        missing = ""
        if sheet.missing_required:
            labels = "، ".join(FIELD_LABELS.get(field, field) for field in sheet.missing_required)
            missing = f" — بيانات غير متوفرة في الملف: {labels}"
        limited = f" — المعروض أول {self.PREVIEW_LIMIT} صف" if result["total"] > self.PREVIEW_LIMIT else ""
        self.info_var.set(
            f"الورقة: {sheet.sheet_name} — بداية العناوين: {sheet.header_row}"
            f" ({sheet.header_rows} صف) — الإجمالي: {result['total']} — "
            f"جديد: {counts['new']} — موجود: {counts['duplicate']} — أخطاء: {counts['invalid']}"
            f"{missing}{ignored}{limited}"
        )
        allowed = counts["new"] + counts["duplicate"] > 0
        self.import_button.configure(state="normal" if allowed else "disabled")

    def start_import(self):
        sheet = self.current_sheet()
        if not sheet:
            return
        mode = self.duplicate_mode.get()
        action = "تحديث الموظفين الموجودين" if mode == "update" else "تخطي الموظفين الموجودين"
        if not messagebox.askyesno(
            "تأكيد الاستيراد",
            f"سيتم استيراد وحفظ {len(sheet.records)} صف من الورقة «{sheet.sheet_name}» مع {action}.\n"
            "سيحفظ النظام البيانات المتاحة، ويضع قيمًا آمنة للحقول غير الموجودة دون اختلاق بيانات شخصية.\n"
            "ستُسجل جميع الإضافات والتحديثات في سجل التدقيق. هل تريد المتابعة؟",
            parent=self,
        ):
            return
        self.import_button.configure(state="disabled")

        def done(summary):
            if self.on_imported:
                self.on_imported()
            ImportResultDialog(self, summary)
            self.analyze_current()

        self.app.run_async(
            lambda: self.app.db.import_employees(sheet.records, self.app.current_user, mode),
            done, busy_text="استيراد الموظفين...",
        )

    def save_template(self):
        suggested = f"ProTech_Employee_Import_Template_{date.today():%Y%m%d}.xlsx"
        path = filedialog.asksaveasfilename(
            parent=self, title="حفظ نموذج استيراد الموظفين", initialfile=suggested,
            defaultextension=".xlsx", filetypes=[("Excel Workbook", "*.xlsx")],
        )
        if not path:
            return
        try:
            saved = export_xlsx(
                path, template_rows(), company_profile=self.app.db.company_profile()
            )
            messagebox.showinfo("تم", f"تم إنشاء نموذج الاستيراد:\n{saved}", parent=self)
        except Exception as exc:
            messagebox.showerror("خطأ", str(exc), parent=self)


class ImportResultDialog(tk.Toplevel):
    def __init__(self, master, summary: dict):
        super().__init__(master)
        self.summary = summary
        self.title("نتيجة استيراد الموظفين")
        self.geometry("900x580")
        self.transient(master)
        self.grab_set()
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="اكتمل استيراد الموظفين", font=TITLE_FONT).pack(anchor="e")
        ttk.Label(
            frame,
            text=(
                f"الإجمالي: {summary['total']}   |   المضاف: {summary['created']}   |   "
                f"المحدّث: {summary['updated']}   |   المتخطى: {summary['skipped']}   |   "
                f"الفاشل: {summary['failed']}"
            ),
            anchor="e", padding=8,
        ).pack(fill="x")
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        columns = ("row", "emp_code", "name", "status", "message")
        tree = ttk.Treeview(table, columns=columns, show="headings")
        for key, label, width in (
            ("row", "الصف", 60), ("emp_code", "رقم الموظف", 110), ("name", "الاسم", 180),
            ("status", "النتيجة", 100), ("message", "التفاصيل", 360),
        ):
            tree.heading(key, text=label)
            tree.column(key, width=width, anchor="center" if key in {"row", "emp_code", "status"} else "e")
        for item in summary["rows"]:
            tree.insert("", "end", values=(
                item.get("row", ""), item.get("emp_code", ""), item.get("name", ""),
                item.get("status", ""), item.get("message", ""),
            ))
        bar = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=bar.set)
        tree.pack(side="right", fill="both", expand=True)
        bar.pack(side="left", fill="y")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(10, 0))
        ttk.Button(buttons, text="حفظ التقرير CSV", command=self.save_csv).pack(side="right", padx=5, ipadx=12)
        ttk.Button(buttons, text="إغلاق", command=self.destroy).pack(side="right", padx=5, ipadx=12)

    def save_csv(self):
        default = f"ProTech_Import_Result_{datetime.now():%Y%m%d_%H%M%S}.csv"
        path = filedialog.asksaveasfilename(
            parent=self, title="حفظ تقرير الاستيراد", initialfile=default,
            defaultextension=".csv", filetypes=[("CSV", "*.csv")],
        )
        if not path:
            return
        with open(path, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["الصف", "رقم الموظف", "الاسم", "النتيجة", "التفاصيل"])
            for item in self.summary["rows"]:
                writer.writerow([
                    item.get("row", ""), item.get("emp_code", ""), item.get("name", ""),
                    item.get("status", ""), item.get("message", ""),
                ])
        messagebox.showinfo("تم", f"تم حفظ التقرير:\n{path}", parent=self)


class EmployeeCustomFieldsManager(tk.Toplevel):
    TYPE_LABELS = {
        "text": "نص",
        "number": "رقم",
        "date": "تاريخ",
        "choice": "اختيار",
    }
    TYPE_VALUES = {label: key for key, label in TYPE_LABELS.items()}

    def __init__(self, master, app: HRApplication, on_saved=None):
        super().__init__(master)
        self.app = app
        self.on_saved = on_saved
        self.selected_id: int | None = None
        self.title("إدارة حقول الموظفين المخصصة")
        self.geometry("930x620")
        self.minsize(780, 520)
        self.transient(master)
        self.grab_set()
        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            body,
            columns=("label", "type", "required", "active", "order", "options"),
            show="headings",
            selectmode="browse",
            height=11,
        )
        for key, label, width in (
            ("label", "اسم الحقل", 190),
            ("type", "النوع", 90),
            ("required", "مطلوب", 70),
            ("active", "نشط", 70),
            ("order", "الترتيب", 70),
            ("options", "الخيارات", 300),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.load_selected)

        form = ttk.LabelFrame(body, text="تعريف الحقل", padding=10)
        form.pack(fill="x", pady=(10, 0))
        self.label_var = tk.StringVar()
        self.type_var = tk.StringVar(value=self.TYPE_LABELS["text"])
        self.options_var = tk.StringVar()
        self.order_var = tk.StringVar(value="100")
        self.required_var = tk.BooleanVar(value=False)
        self.active_var = tk.BooleanVar(value=True)
        ttk.Label(form, text="الاسم").grid(row=0, column=5, sticky="e", padx=5, pady=5)
        ttk.Entry(form, textvariable=self.label_var, width=24).grid(row=0, column=4, sticky="ew", padx=5)
        ttk.Label(form, text="النوع").grid(row=0, column=3, sticky="e", padx=5)
        ttk.Combobox(
            form,
            textvariable=self.type_var,
            values=list(self.TYPE_VALUES),
            state="readonly",
            width=12,
        ).grid(row=0, column=2, sticky="ew", padx=5)
        ttk.Label(form, text="الترتيب").grid(row=0, column=1, sticky="e", padx=5)
        ttk.Spinbox(form, from_=0, to=10000, textvariable=self.order_var, width=8).grid(row=0, column=0, padx=5)
        ttk.Label(form, text="خيارات مفصولة بفاصلة").grid(row=1, column=5, sticky="e", padx=5, pady=5)
        ttk.Entry(form, textvariable=self.options_var).grid(row=1, column=2, columnspan=3, sticky="ew", padx=5)
        ttk.Checkbutton(form, text="حقل مطلوب", variable=self.required_var).grid(row=1, column=1, padx=5)
        ttk.Checkbutton(form, text="نشط", variable=self.active_var).grid(row=1, column=0, padx=5)
        form.columnconfigure(4, weight=1)
        form.columnconfigure(2, weight=1)
        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=10)
        ttk.Button(actions, text="حقل جديد", command=self.clear_form).pack(side="right", padx=4)
        ttk.Button(actions, text="حفظ", command=self.save).pack(side="right", padx=4)
        ttk.Button(actions, text="إغلاق", command=self.destroy).pack(side="left", padx=4)
        self.refresh()

    def refresh(self):
        self.fields = {
            int(field["id"]): field
            for field in self.app.db.list_employee_custom_fields()
        }
        self.tree.delete(*self.tree.get_children())
        for field in self.fields.values():
            self.tree.insert(
                "",
                "end",
                iid=str(field["id"]),
                values=(
                    field["label"],
                    self.TYPE_LABELS.get(field["field_type"], field["field_type"]),
                    "نعم" if field["is_required"] else "لا",
                    "نعم" if field["is_active"] else "لا",
                    field["sort_order"],
                    "، ".join(field["options"]),
                ),
            )

    def clear_form(self):
        self.selected_id = None
        self.label_var.set("")
        self.type_var.set(self.TYPE_LABELS["text"])
        self.options_var.set("")
        self.order_var.set("100")
        self.required_var.set(False)
        self.active_var.set(True)
        self.tree.selection_remove(*self.tree.selection())

    def load_selected(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_id = int(selection[0])
        field = self.fields[self.selected_id]
        self.label_var.set(field["label"])
        self.type_var.set(self.TYPE_LABELS.get(field["field_type"], "نص"))
        self.options_var.set("، ".join(field["options"]))
        self.order_var.set(str(field["sort_order"]))
        self.required_var.set(bool(field["is_required"]))
        self.active_var.set(bool(field["is_active"]))

    def save(self):
        data = {
            "label": self.label_var.get(),
            "field_type": self.TYPE_VALUES.get(self.type_var.get(), "text"),
            "options": self.options_var.get(),
            "sort_order": self.order_var.get(),
            "is_required": self.required_var.get(),
            "is_active": self.active_var.get(),
        }

        def success(saved_id):
            self.selected_id = int(saved_id)
            self.refresh()
            self.tree.selection_set(str(saved_id))
            if self.on_saved:
                self.on_saved()
            messagebox.showinfo("تم", "تم حفظ الحقل المخصص.", parent=self)

        self.app.run_async(
            lambda: self.app.db.save_employee_custom_field(
                data,
                self.app.current_user,
                self.selected_id,
            ),
            success,
            busy_text="حفظ الحقل المخصص...",
        )


class EmployeeDialog(tk.Toplevel):
    field_specs = [
        ("name", "الاسم", "entry"), ("phone", "الجوال", "entry"),
        ("nationality", "الجنسية", "entry"), ("id_type", "نوع الوثيقة", "id_type"),
        ("id_number", "رقم الهوية / الإقامة / الجواز", "entry"), ("id_issue_date", "تاريخ بداية الوثيقة", "date"),
        ("id_expiry_date", "تاريخ انتهاء الهوية", "date"),
        ("work_permit_start_date", "تاريخ بداية كرت العمل", "date"),
        ("work_permit_expiry_date", "تاريخ انتهاء كرت العمل", "date"),
        ("medical_insurance_start_date", "تاريخ بداية التأمين الطبي", "date"),
        ("medical_insurance_expiry_date", "تاريخ انتهاء التأمين الطبي", "date"),
        ("birth_date", "تاريخ الميلاد", "date"), ("join_date", "تاريخ الالتحاق", "date"), ("department", "القسم", "entry"),
        ("job_title", "المسمى الوظيفي", "entry"), ("salary", "الراتب", "entry"),
        ("work_location", "مكان العمل", "entry"), ("email", "البريد الإلكتروني", "entry"),
        ("bank_name", "اسم البنك", "entry"), ("bank_account_number", "رقم الحساب البنكي", "entry"),
        ("iban", "رقم الآيبان", "entry"),
        ("performance_rating", "تقييم الموظف", "rating"), ("status", "الحالة", "status"),
    ]

    def __init__(self, master, app: HRApplication, employee: dict | None = None, on_saved=None):
        super().__init__(master)
        self.app = app
        self.employee = employee
        self.on_saved = on_saved
        self.can_edit = app.current_user["role"] in {"admin", "hr"}
        self.title("بيانات الموظف" if employee else "إضافة موظف")
        self.geometry("940x850")
        self.minsize(820, 680)
        self.transient(master)
        self.grab_set()
        self.entries: dict[str, tk.Widget] = {}
        self.field_labels: dict[str, ttk.Label] = {}
        self.custom_fields = app.db.list_employee_custom_fields(active_only=True)
        self.custom_entries: dict[int, tk.Widget] = {}
        self.draft_owner = str(
            app.current_user.get("id") or app.current_user.get("username") or "local"
        )
        self._draft_job: str | None = None
        self.identity_path = tk.StringVar()
        self._selected_identity_path = ""
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="left", fill="y")
        canvas.pack(side="right", fill="both", expand=True)
        frame = ttk.Frame(canvas, padding=15)
        window_id = canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind(
            "<Configure>",
            lambda _event: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda event: canvas.itemconfigure(window_id, width=event.width),
        )
        ttk.Label(frame, text="بيانات الموظف", font=TITLE_FONT).grid(row=0, column=0, columnspan=4, pady=(0, 6))
        self.employee_code_var = tk.StringVar()
        ttk.Label(
            frame,
            textvariable=self.employee_code_var,
            foreground=COLORS["muted"],
            anchor="e",
        ).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(0, 8))
        for index, (key, label, kind) in enumerate(self.field_specs):
            row = index // 2 + 2
            col = (index % 2) * 2
            label_widget = ttk.Label(frame, text=f"{label} *" if key == "name" else label)
            label_widget.grid(row=row, column=col + 1, sticky="e", padx=6, pady=6)
            self.field_labels[key] = label_widget
            holder = ttk.Frame(frame)
            holder.grid(row=row, column=col, sticky="ew", padx=6, pady=6)
            holder.columnconfigure(0, weight=1)
            if kind == "status":
                widget = ttk.Combobox(holder, values=EMPLOYEE_STATUSES, state="readonly", width=25)
                widget.grid(row=0, column=0, sticky="ew")
            elif kind == "id_type":
                widget = ttk.Combobox(holder, values=IDENTITY_TYPES, state="readonly", width=25)
                widget.grid(row=0, column=0, sticky="ew")
                widget.bind("<<ComboboxSelected>>", lambda _e: self.after_idle(self.sync_identity_type))
            elif kind == "rating":
                widget = ttk.Combobox(holder, values=RATING_OPTIONS, state="readonly", width=25)
                widget.grid(row=0, column=0, sticky="ew")
            else:
                widget = ttk.Entry(holder, width=28)
                widget.grid(row=0, column=0, sticky="ew")
                if kind == "date":
                    ttk.Button(holder, text="📅", width=3, command=lambda e=widget, k=key: self.open_calendar(e, k)).grid(
                        row=0, column=1, padx=(4, 0)
                    )
            self.entries[key] = widget
        identity_row = len(self.field_specs) // 2 + 2
        ttk.Label(frame, text="مدة الخدمة").grid(row=identity_row, column=3, sticky="e", padx=6, pady=6)
        self.service_duration_var = tk.StringVar(value="تُحتسب من تاريخ الالتحاق")
        service_label = ttk.Label(frame, textvariable=self.service_duration_var)
        service_label.grid(row=identity_row, column=2, sticky="ew", padx=6, pady=6)
        ttk.Label(frame, text="حالة الهوية").grid(row=identity_row, column=1, sticky="e", padx=6, pady=6)
        self.identity_status_var = tk.StringVar(value="غير مسجلة")
        ttk.Label(frame, textvariable=self.identity_status_var).grid(
            row=identity_row, column=0, sticky="ew", padx=6, pady=6
        )
        self.entries["join_date"].bind("<KeyRelease>", lambda _e: self.update_service_duration())
        self.entries["join_date"].bind("<<ComboboxSelected>>", lambda _e: self.update_service_duration())
        self.entries["nationality"].bind("<KeyRelease>", lambda _e: self.sync_identity_type())
        self.entries["nationality"].bind("<FocusOut>", lambda _e: self.sync_identity_type())
        self.entries["id_number"].bind("<KeyRelease>", lambda _e: self.update_employee_code_preview())
        self.entries["id_number"].bind("<FocusOut>", lambda _e: self.update_employee_code_preview())
        self.entries["id_expiry_date"].bind("<KeyRelease>", lambda _e: self.update_identity_status())
        self.entries["id_expiry_date"].bind("<FocusOut>", lambda _e: self.update_identity_status())

        identity_row += 1
        ttk.Label(frame, text="نسخة الهوية").grid(row=identity_row, column=3, sticky="e", padx=6, pady=6)
        identity_holder = ttk.Frame(frame)
        identity_holder.grid(row=identity_row, column=0, columnspan=3, sticky="ew", padx=6, pady=6)
        ttk.Entry(identity_holder, textvariable=self.identity_path, state="readonly").pack(
            side="right", fill="x", expand=True
        )
        if self.can_edit:
            ttk.Button(identity_holder, text="اختيار صورة / PDF", command=self.choose_identity).pack(
                side="left", padx=(6, 0)
            )
            if employee:
                ttk.Button(identity_holder, text="استعراض الهوية", command=self.open_identity).pack(
                    side="left", padx=(6, 0)
                )
        ttk.Label(
            frame,
            text=(
                "يمكن حفظ الملف بالاسم فقط واستكمال بقية البيانات لاحقًا. عند تسجيل الهوية يصبح رقم الموظف "
                "تلقائيًا آخر أربعة أرقام منها، وتُحفظ المرفقات داخل قاعدة البيانات والنسخ الاحتياطية."
            ),
            foreground="#555",
            wraplength=820,
            justify="right",
        ).grid(row=identity_row + 1, column=0, columnspan=4, sticky="e", padx=6)

        notes_row = identity_row + 2
        ttk.Label(frame, text="ملاحظات").grid(row=notes_row, column=3, sticky="ne", padx=6, pady=6)
        self.notes = tk.Text(frame, height=5, width=55, wrap="word")
        self.notes.grid(row=notes_row, column=0, columnspan=3, sticky="ew", padx=6, pady=6)
        custom_row = notes_row + 1
        if self.custom_fields:
            custom_frame = ttk.LabelFrame(frame, text="البيانات المخصصة", padding=9)
            custom_frame.grid(row=custom_row, column=0, columnspan=4, sticky="ew", padx=6, pady=8)
            custom_frame.columnconfigure(0, weight=1)
            custom_frame.columnconfigure(2, weight=1)
            for index, field in enumerate(self.custom_fields):
                row = index // 2
                col = (index % 2) * 2
                label = str(field["label"]) + (" *" if field["is_required"] else "")
                ttk.Label(custom_frame, text=label).grid(
                    row=row, column=col + 1, sticky="e", padx=5, pady=5
                )
                holder = ttk.Frame(custom_frame)
                holder.grid(row=row, column=col, sticky="ew", padx=5, pady=5)
                holder.columnconfigure(0, weight=1)
                if field["field_type"] == "choice":
                    widget = ttk.Combobox(
                        holder,
                        values=field["options"],
                        state="readonly",
                    )
                    widget.grid(row=0, column=0, sticky="ew")
                else:
                    widget = ttk.Entry(holder)
                    widget.grid(row=0, column=0, sticky="ew")
                    if field["field_type"] == "date":
                        ttk.Button(
                            holder,
                            text="📅",
                            width=3,
                            command=lambda entry=widget: DatePicker(
                                self,
                                entry.get(),
                                lambda value, target=entry: self.set_entry(target, value),
                                min_year=1950,
                                max_year=date.today().year + 30,
                                title="اختر التاريخ",
                            ),
                        ).grid(row=0, column=1, padx=(4, 0))
                self.custom_entries[int(field["id"])] = widget
            custom_row += 1
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(2, weight=1)
        buttons = ttk.Frame(frame)
        buttons.grid(row=custom_row, column=0, columnspan=4, pady=15)
        if self.can_edit:
            ttk.Button(buttons, text="حفظ", command=self.save).pack(side="right", padx=5, ipadx=15, ipady=4)
            if employee:
                ttk.Button(buttons, text="✉ بريد", command=lambda: self.open_message("email")).pack(
                    side="right", padx=5, ipadx=8, ipady=4
                )
                ttk.Button(buttons, text="◉ واتساب", command=lambda: self.open_message("whatsapp")).pack(
                    side="right", padx=5, ipadx=8, ipady=4
                )
                ttk.Button(buttons, text="💰 البدلات", command=self.open_allowances).pack(
                    side="right", padx=5, ipadx=8, ipady=4
                )
        if employee:
            ttk.Button(buttons, text="وثائق الموظف", command=self.open_documents).pack(
                side="right", padx=5, ipadx=8, ipady=4
            )
        ttk.Button(buttons, text="إغلاق", command=self.close_without_saving).pack(side="right", padx=5, ipadx=15, ipady=4)
        if employee:
            self.populate(employee)
        else:
            self.entries["performance_rating"].set(RATING_OPTIONS[0])
            self.entries["status"].set("نشط")
            self.sync_identity_type()
            self.update_employee_code_preview()
        self.update_service_duration()
        self.update_identity_status()
        if not self.can_edit:
            self.make_readonly()
        else:
            self.protocol("WM_DELETE_WINDOW", self.close_without_saving)
            self.after(500, self.offer_draft_recovery)
            self._draft_job = self.after(1500, self.auto_save_draft)

    def draft_payload(self) -> dict:
        return {
            "employee_id": int(self.employee["id"]) if self.employee else None,
            "fields": {key: widget.get().strip() for key, widget in self.entries.items()},
            "notes": self.notes.get("1.0", "end").strip(),
            "custom_values": {
                str(field_id): widget.get().strip()
                for field_id, widget in self.custom_entries.items()
            },
            "identity_path": self._selected_identity_path.strip(),
        }

    def auto_save_draft(self) -> None:
        self._draft_job = None
        if not self.can_edit or not self.winfo_exists():
            return
        try:
            save_form_draft("employee", self.draft_owner, self.draft_payload())
        except (OSError, tk.TclError):
            pass
        try:
            self._draft_job = self.after(1500, self.auto_save_draft)
        except tk.TclError:
            self._draft_job = None

    def offer_draft_recovery(self) -> None:
        loaded = load_form_draft("employee", self.draft_owner)
        if not loaded:
            return
        payload = loaded.get("payload", {})
        expected_id = int(self.employee["id"]) if self.employee else None
        if payload.get("employee_id") != expected_id:
            return
        if not messagebox.askyesno(
            "استعادة مسودة الموظف",
            f"توجد مسودة محفوظة تلقائيًا بتاريخ {loaded.get('saved_at', '')}. هل تريد استعادتها؟",
            parent=self,
        ):
            clear_form_draft("employee", self.draft_owner)
            return
        fields = payload.get("fields", {})
        if isinstance(fields, dict):
            for key, value in fields.items():
                widget = self.entries.get(str(key))
                if widget is None:
                    continue
                if isinstance(widget, ttk.Combobox):
                    widget.set(str(value or ""))
                else:
                    self.set_entry(widget, str(value or ""))
        self.notes.delete("1.0", "end")
        self.notes.insert("1.0", str(payload.get("notes") or ""))
        custom_values = payload.get("custom_values", {})
        if isinstance(custom_values, dict):
            for field_id, widget in self.custom_entries.items():
                value = str(custom_values.get(str(field_id), "") or "")
                if isinstance(widget, ttk.Combobox):
                    widget.set(value)
                else:
                    self.set_entry(widget, value)
        identity_path = str(payload.get("identity_path") or "")
        if identity_path and Path(identity_path).is_file():
            self._selected_identity_path = identity_path
            self.identity_path.set(identity_path)
        self.sync_identity_type()
        self.update_employee_code_preview()
        self.update_service_duration()
        self.update_identity_status()

    def close_without_saving(self) -> None:
        if self.can_edit:
            try:
                payload = self.draft_payload()
                has_data = bool(
                    payload["notes"]
                    or payload["identity_path"]
                    or any(payload["fields"].values())
                    or any(payload["custom_values"].values())
                )
            except tk.TclError:
                has_data = False
            if has_data and not messagebox.askyesno(
                "إغلاق النموذج",
                "سيتم تجاهل مسودة هذا النموذج. هل تريد الإغلاق؟",
                parent=self,
            ):
                return
            clear_form_draft("employee", self.draft_owner)
        if self._draft_job is not None:
            try:
                self.after_cancel(self._draft_job)
            except tk.TclError:
                pass
        self.destroy()

    def sync_identity_type(self):
        try:
            nationality = self.entries["nationality"].get().strip()
            type_widget = self.entries["id_type"]
            current = type_widget.get().strip()
            if nationality and current != "جواز سفر":
                current = identity_type_for_nationality(nationality)
                type_widget.set(current)
            elif current not in IDENTITY_TYPES:
                current = ""
                type_widget.set("")
            self.field_labels["id_number"].configure(
                text=identity_number_label(current) if current else "رقم الهوية / الإقامة / الجواز"
            )
            self.field_labels["id_expiry_date"].configure(
                text=identity_expiry_label(current) if current else "تاريخ انتهاء الوثيقة"
            )
        except (KeyError, tk.TclError):
            return

    def update_employee_code_preview(self):
        try:
            identity = "".join(
                character for character in self.entries["id_number"].get() if character.isdigit()
            )
        except (KeyError, tk.TclError):
            identity = ""
        if len(identity) == 10:
            self.employee_code_var.set(f"رقم الموظف عند الحفظ: {identity[-4:]}")
        elif self.employee:
            self.employee_code_var.set(
                f"رقم الموظف الحالي: {self.employee.get('emp_code') or 'غير مسجل'}"
            )
        else:
            self.employee_code_var.set("رقم الموظف: مؤقت حتى تسجيل هوية من 10 أرقام")

    def open_message(self, channel: str):
        if not self.employee:
            return
        recipient = {
            key: self.employee.get(key)
            for key in ("id", "emp_code", "name", "phone", "email", "department", "job_title", "status")
        }
        MessageComposerDialog(self, self.app, [recipient], preset_channel=channel)

    def open_allowances(self):
        if not self.employee:
            return
        AllowancesDialog(self, self.app, employee_id=int(self.employee["id"]))

    def open_documents(self):
        if self.employee:
            EmployeeDocumentsDialog(self, self.app, self.employee)

    def update_service_duration(self):
        try:
            value = self.entries["join_date"].get().strip()
        except Exception:
            value = ""
        self.service_duration_var.set(format_service_duration(value))

    def update_identity_status(self):
        try:
            value = self.entries["id_expiry_date"].get().strip()
        except Exception:
            value = ""
        self.identity_status_var.set(format_identity_expiry_status(value))

    def choose_identity(self):
        path = filedialog.askopenfilename(
            parent=self,
            title="اختر صورة أو PDF للهوية",
            filetypes=[
                ("صور الهوية وPDF", "*.jpg *.jpeg *.png *.webp *.pdf"),
                ("جميع الملفات", "*.*"),
            ],
        )
        if path:
            self._selected_identity_path = path
            self.identity_path.set(path)

    def open_identity(self):
        if not self.employee:
            return
        def operation():
            document = self.app.enterprise.latest_identity_document(self.employee["id"])
            if not document:
                return None
            return self.app.enterprise.document_path(document["id"], self.app.current_user)
        def success(path):
            if not path:
                messagebox.showinfo("نسخة الهوية", "لا توجد نسخة هوية محفوظة لهذا الموظف.", parent=self)
                return
            open_path(path)
        self.app.run_async(operation, success, busy_text="استعراض نسخة الهوية...")

    def open_calendar(self, entry, field_key=""):
        if field_key == "birth_date":
            DatePicker(
                self, entry.get(), lambda value: self.set_entry(entry, value),
                min_year=1900, max_year=date.today().year, title="اختر تاريخ الميلاد",
            )
        elif field_key in {
            "id_issue_date", "id_expiry_date", "work_permit_start_date",
            "work_permit_expiry_date", "medical_insurance_start_date",
            "medical_insurance_expiry_date",
        }:
            picker_titles = {
                "id_issue_date": "اختر تاريخ بداية الهوية أو الإقامة أو الجواز",
                "id_expiry_date": "اختر تاريخ انتهاء الهوية أو الإقامة أو الجواز",
                "work_permit_start_date": "اختر تاريخ بداية كرت العمل",
                "work_permit_expiry_date": "اختر تاريخ انتهاء كرت العمل",
                "medical_insurance_start_date": "اختر تاريخ بداية التأمين الطبي",
                "medical_insurance_expiry_date": "اختر تاريخ انتهاء التأمين الطبي",
            }
            DatePicker(
                self, entry.get(),
                lambda value: (
                    self.set_entry(entry, value),
                    self.update_identity_status()
                    if field_key == "id_expiry_date" else None,
                ),
                min_year=date.today().year - 10, max_year=date.today().year + 20,
                title=picker_titles[field_key],
            )
        else:
            DatePicker(
                self, entry.get(), lambda value: (self.set_entry(entry, value), self.update_service_duration()),
                min_year=1950, max_year=date.today().year + 5, title="اختر تاريخ الالتحاق",
            )

    @staticmethod
    def set_entry(entry, value):
        entry.delete(0, tk.END)
        entry.insert(0, value)

    def populate(self, employee):
        for key, _, _ in self.field_specs:
            value = employee.get(key) or ""
            if key == "id_number" and not self.can_edit:
                value = mask_id_number(str(value))
            elif key == "bank_account_number" and not self.can_edit:
                value = mask_bank_account(str(value))
            elif key == "iban" and not self.can_edit:
                value = mask_iban(str(value))
            if key == "performance_rating":
                value = rating_to_stars(value)
            widget = self.entries[key]
            if isinstance(widget, ttk.Combobox):
                widget.set(str(value))
            else:
                widget.insert(0, str(value))
        self.notes.insert("1.0", employee.get("notes") or "")
        custom_values = employee.get("custom_values") or {}
        for field_id, widget in self.custom_entries.items():
            value = str(custom_values.get(field_id, custom_values.get(str(field_id), "")) or "")
            if isinstance(widget, ttk.Combobox):
                widget.set(value)
            else:
                widget.insert(0, value)
        self.sync_identity_type()
        self.update_employee_code_preview()
        self.update_identity_status()
        try:
            identity = self.app.enterprise.latest_identity_document(employee["id"])
            if identity:
                self.identity_path.set(f"محفوظ داخل قاعدة البيانات: {identity['original_name']}")
        except Exception:
            pass

    def make_readonly(self):
        for widget in self.entries.values():
            try:
                widget.configure(state="disabled")
            except tk.TclError:
                pass
        for widget in self.custom_entries.values():
            try:
                widget.configure(state="disabled")
            except tk.TclError:
                pass
        self.notes.configure(state="disabled")

    def save(self):
        data = {key: widget.get().strip() for key, widget in self.entries.items()}
        data["performance_rating"] = rating_from_label(data.get("performance_rating", ""))
        data["notes"] = self.notes.get("1.0", "end").strip()
        custom_values = {
            field_id: widget.get().strip()
            for field_id, widget in self.custom_entries.items()
        }
        try:
            self.app.db.validate_employee_custom_values(custom_values)
        except ValueError as exc:
            messagebox.showerror("بيانات مخصصة غير صحيحة", str(exc), parent=self)
            return
        identity_path = self._selected_identity_path.strip()
        identity_start = data.get("id_issue_date", "")
        identity_expiry = data.get("id_expiry_date", "")
        if self.employee:
            message = "تم تحديث بيانات الموظف."
            def operation():
                self.app.db.update_employee(self.employee["id"], data, self.app.current_user)
                self.app.db.save_employee_custom_values(
                    self.employee["id"], custom_values, self.app.current_user
                )
                if identity_path:
                    self.app.enterprise.save_identity_copy(
                        self.employee["id"], identity_path, self.app.current_user,
                        identity_expiry, start_date=identity_start,
                    )
                return self.app.db.get_employee(self.employee["id"])
        else:
            message = "تمت إضافة الموظف."
            def operation():
                employee_id = self.app.db.add_employee(data, self.app.current_user)
                self.app.db.save_employee_custom_values(
                    employee_id, custom_values, self.app.current_user
                )
                if identity_path:
                    self.app.enterprise.save_identity_copy(
                        employee_id, identity_path, self.app.current_user,
                        identity_expiry, start_date=identity_start,
                    )
                return self.app.db.get_employee(employee_id)

        def success(saved_employee):
            clear_form_draft("employee", self.draft_owner)
            employee_code = (saved_employee or {}).get("emp_code") or "غير مسجل"
            messagebox.showinfo(
                "تم", f"{message}\nرقم الموظف: {employee_code}", parent=self
            )
            if self.on_saved:
                self.on_saved()
            self.destroy()

        self.app.run_async(operation, success, busy_text="حفظ بيانات الموظف...")


class AttendancePage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "تسجيل ومتابعة الحضور")
        self.choice_map: dict[str, int] = {}
        form = ttk.LabelFrame(self, text="تسجيل الحالة", padding=12)
        form.pack(fill="x", pady=6)
        ttk.Label(form, text="الموظف").grid(row=0, column=5, padx=5, pady=5)
        self.employee_var = tk.StringVar()
        self.employee_combo = ttk.Combobox(form, textvariable=self.employee_var, state="readonly", width=35)
        self.employee_combo.grid(row=0, column=4, padx=5, pady=5)
        self.employee_combo.bind("<<ComboboxSelected>>", lambda _: self.load_records())
        ttk.Label(form, text="التاريخ").grid(row=0, column=3, padx=5, pady=5)
        self.date_entry = ttk.Entry(form, width=13)
        self.date_entry.grid(row=0, column=2, padx=5, pady=5)
        self.date_entry.insert(0, date.today().isoformat())
        ttk.Button(form, text="📅", width=3, command=self.pick_date).grid(row=0, column=1, padx=2)
        ttk.Label(form, text="الحالة").grid(row=1, column=5, padx=5, pady=5)
        self.status_var = tk.StringVar(value=ATTENDANCE_STATUSES[0])
        ttk.Combobox(form, textvariable=self.status_var, values=ATTENDANCE_STATUSES, state="readonly", width=20).grid(
            row=1, column=4, padx=5, pady=5
        )
        ttk.Label(form, text="ملاحظة").grid(row=1, column=3, padx=5, pady=5)
        self.note_entry = ttk.Entry(form, width=38)
        self.note_entry.grid(row=1, column=1, columnspan=2, padx=5, pady=5, sticky="ew")
        ttk.Label(form, text="وقت الحضور").grid(row=2, column=5, padx=5, pady=5)
        self.check_in_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.check_in_var, width=12).grid(
            row=2, column=4, padx=5, pady=5
        )
        ttk.Label(form, text="وقت الانصراف").grid(row=2, column=3, padx=5, pady=5)
        self.check_out_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.check_out_var, width=12).grid(
            row=2, column=2, padx=5, pady=5
        )
        ttk.Label(form, text="HH:MM", foreground="#64748B").grid(
            row=2, column=1, padx=5, pady=5
        )
        ttk.Label(
            form,
            text="يمكن ترك الحضور أو الانصراف فارغًا وإضافته لاحقًا.",
            foreground="#0F766E",
        ).grid(row=3, column=1, columnspan=5, sticky="e", padx=5, pady=(0, 5))
        form.columnconfigure(2, weight=1)
        form.columnconfigure(4, weight=1)
        if app.current_user["role"] in {"admin", "hr"}:
            actions = ttk.Frame(self)
            actions.pack(fill="x", pady=(2, 4))
            # الأزرار في صف مستقل حتى لا يغطي زر الانصراف زر الحفظ على الشاشات الصغيرة.
            ttk.Button(actions, text="حفظ / تحديث السجل", command=self.save).pack(
                side="right", padx=4, ipadx=10, ipady=4
            )
            ttk.Button(
                actions, text="الحضور: الآن", command=lambda: self.record_now("in")
            ).pack(side="right", padx=4, ipadx=8, ipady=4)
            ttk.Button(
                actions, text="الانصراف: الآن", command=lambda: self.record_now("out")
            ).pack(side="right", padx=4, ipadx=8, ipady=4)
            time_buttons = ttk.Frame(self)
            time_buttons.pack(fill="x", pady=(2, 0))
            ttk.Button(
                time_buttons, text="الحضور: وقت اختياري", command=lambda: self.record_custom("in")
            ).pack(side="right", padx=4, ipadx=6, ipady=3)
            ttk.Button(
                time_buttons, text="الانصراف: وقت اختياري", command=lambda: self.record_custom("out")
            ).pack(side="right", padx=4, ipadx=6, ipady=3)
            import_buttons = ttk.Frame(self)
            import_buttons.pack(fill="x", pady=(2, 0))
            ttk.Button(import_buttons, text="استيراد حضور Excel / CSV", command=self.import_attendance).pack(side="right", padx=4)
            ttk.Button(import_buttons, text="نموذج استيراد الحضور", command=self.save_attendance_template).pack(side="right", padx=4)

        table_frame = ttk.Frame(self)
        table_frame.pack(fill="both", expand=True, pady=10)
        self.tree = ttk.Treeview(
            table_frame,
            columns=("date", "status", "check_in", "check_out", "late", "note", "updated"),
            show="headings",
        )
        for key, label, width in (
            ("date", "التاريخ", 110),
            ("status", "الحالة", 110),
            ("check_in", "الحضور", 85),
            ("check_out", "الانصراف", 85),
            ("late", "التأخير", 85),
            ("note", "الملاحظة", 260),
            ("updated", "آخر تحديث", 150),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center" if key != "note" else "e")
        bar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.pack(side="right", fill="both", expand=True)
        bar.pack(side="left", fill="y")
        self.load_choices()

    def import_attendance(self):
        path = filedialog.askopenfilename(
            parent=self, title="اختر ملف الحضور",
            filetypes=[("Excel وCSV", "*.xlsx *.csv"), ("جميع الملفات", "*.*")],
        )
        if not path:
            return
        def work():
            records, parse_errors, sheet_name = read_attendance_file(path)
            result = self.app.enterprise.import_attendance(records, self.app.current_user)
            result["parse_errors"] = parse_errors
            result["sheet_name"] = sheet_name
            return result
        def done(result):
            details = (
                f"الورقة: {result['sheet_name']}\n"
                f"جديد: {result['created']}\nمحدّث: {result['updated']}\n"
                f"فشل: {result['failed']}\nأخطاء القراءة: {len(result['parse_errors'])}"
            )
            errors = [*result.get("parse_errors", []), *result.get("errors", [])]
            if errors:
                details += "\n\nأول الأخطاء:\n" + "\n".join(errors[:10])
            messagebox.showinfo("نتيجة استيراد الحضور", details, parent=self)
            self.load_records()
        self.app.run_async(work, done, busy_text="استيراد سجلات الحضور...")

    def save_attendance_template(self):
        path = filedialog.asksaveasfilename(
            parent=self, title="حفظ نموذج الحضور",
            initialfile=f"ProTech_Attendance_Template_{date.today():%Y%m%d}.xlsx",
            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        try:
            saved = export_xlsx(
                path, attendance_template_rows(), company_profile=self.app.db.company_profile()
            )
            messagebox.showinfo("تم", f"تم إنشاء النموذج:\n{saved}", parent=self)
            open_path(Path(saved).parent)
        except Exception as exc:
            messagebox.showerror("خطأ", str(exc), parent=self)

    def pick_date(self):
        DatePicker(
            self, self.date_entry.get(),
            self._date_selected,
            min_year=date.today().year - 10, max_year=date.today().year + 2,
            title="اختر تاريخ الحضور",
        )

    def _date_selected(self, value: str):
        EmployeeDialog.set_entry(self.date_entry, value)
        self.load_records()

    def load_choices(self):
        def render(choices):
            self.choice_map = {f"{item['emp_code']} — {item['name']} — {item['department']}": item["id"] for item in choices}
            values = list(self.choice_map)
            self.employee_combo["values"] = values
            if values:
                self.employee_combo.current(0)
                self.load_records()

        self.app.run_async(lambda: self.app.db.list_employee_choices(active_only=False), render, busy_text="تحميل قائمة الموظفين...")

    def current_employee_id(self):
        return self.choice_map.get(self.employee_var.get())

    def record_now(self, kind: str):
        if not self.current_employee_id():
            messagebox.showwarning("تنبيه", "اختر موظفًا.", parent=self)
            return
        EmployeeDialog.set_entry(self.date_entry, date.today().isoformat())
        now = datetime.now().strftime("%H:%M")
        self.status_var.set("حاضر")
        self.check_in_var.set("")
        self.check_out_var.set("")
        self.note_entry.delete(0, "end")
        if kind == "in":
            self.check_in_var.set(now)
        else:
            self.check_out_var.set(now)
        self.save(preserve_missing_times=True, preserve_blank_note=True, entry_mode="now")

    def record_custom(self, kind: str):
        if not self.current_employee_id():
            messagebox.showwarning("تنبيه", "اختر موظفًا.", parent=self)
            return
        label = "الحضور" if kind == "in" else "الانصراف"
        selected = simpledialog.askstring(
            f"وقت {label} الاختياري",
            f"أدخل وقت {label} بصيغة HH:MM:",
            parent=self,
        )
        if selected is None:
            return
        self.status_var.set("حاضر")
        self.check_in_var.set("")
        self.check_out_var.set("")
        if kind == "in":
            self.check_in_var.set(selected.strip())
        else:
            self.check_out_var.set(selected.strip())
        self.save(
            preserve_missing_times=True,
            preserve_blank_note=True,
            entry_mode="custom",
        )

    def save(
        self,
        preserve_missing_times: bool = False,
        preserve_blank_note: bool = False,
        entry_mode: str = "manual",
    ):
        employee_id = self.current_employee_id()
        if not employee_id:
            messagebox.showwarning("تنبيه", "اختر موظفًا.", parent=self)
            return
        attendance_date = self.date_entry.get().strip()
        status = self.status_var.get()
        note = self.note_entry.get()
        self.app.run_async(
            lambda: self.app.db.upsert_attendance(
                employee_id,
                attendance_date,
                status,
                note,
                self.app.current_user,
                self.check_in_var.get(),
                self.check_out_var.get(),
                preserve_missing_times=preserve_missing_times,
                preserve_blank_note=preserve_blank_note,
                entry_mode=entry_mode,
            ),
            lambda _: (messagebox.showinfo("تم", "تم حفظ سجل الحضور.", parent=self), self.load_records()),
            busy_text="حفظ الحضور...",
        )

    def load_records(self):
        employee_id = self.current_employee_id()
        if not employee_id:
            return

        def render(records):
            self.tree.delete(*self.tree.get_children())
            selected_date = self.date_entry.get().strip()
            selected_record = None
            for record in records:
                late = int(record.get("late_minutes") or 0)
                late_label = f"{late} د" if late else "—"
                self.tree.insert(
                    "",
                    "end",
                    values=(
                        record["attendance_date"],
                        record["status"],
                        record.get("check_in_time") or "—",
                        record.get("check_out_time") or "—",
                        late_label,
                        record.get("note") or "",
                        record["updated_at"],
                    ),
                )
                if record["attendance_date"] == selected_date:
                    selected_record = record
            self.check_in_var.set(
                str(selected_record.get("check_in_time") or "") if selected_record else ""
            )
            self.check_out_var.set(
                str(selected_record.get("check_out_time") or "") if selected_record else ""
            )
            if selected_record:
                self.status_var.set(str(selected_record["status"]))
                self.note_entry.delete(0, "end")
                self.note_entry.insert(0, str(selected_record.get("note") or ""))
            else:
                self.status_var.set(ATTENDANCE_STATUSES[0])
                self.note_entry.delete(0, "end")

        self.app.run_async(lambda: self.app.db.attendance_for_employee(employee_id), render, busy_text="تحميل سجل الحضور...")



class LeavesPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "نظام الإجازات")
        self.rows: dict[str, dict] = {}
        filters = ttk.LabelFrame(self, text="البحث والتصفية", padding=8)
        filters.pack(fill="x", pady=5)

        ttk.Label(filters, text="بحث").grid(row=0, column=7, padx=4, pady=4)
        self.search_var = tk.StringVar()
        search = ttk.Entry(filters, textvariable=self.search_var, width=24)
        search.grid(row=0, column=6, padx=4, pady=4)
        search.bind("<Return>", lambda _event: self.load())

        ttk.Label(filters, text="الحالة").grid(row=0, column=5, padx=4, pady=4)
        self.status_var = tk.StringVar(value="الكل")
        ttk.Combobox(
            filters, textvariable=self.status_var, values=("الكل", *LEAVE_STATUSES),
            state="readonly", width=12,
        ).grid(row=0, column=4, padx=4, pady=4)

        ttk.Label(filters, text="من").grid(row=0, column=3, padx=4, pady=4)
        self.from_entry = ttk.Entry(filters, width=12)
        self.from_entry.grid(row=0, column=2, padx=2, pady=4)
        ttk.Button(filters, text="📅", width=3, command=lambda: self.pick_filter_date(self.from_entry)).grid(row=0, column=1)

        ttk.Label(filters, text="إلى").grid(row=1, column=3, padx=4, pady=4)
        self.to_entry = ttk.Entry(filters, width=12)
        self.to_entry.grid(row=1, column=2, padx=2, pady=4)
        ttk.Button(filters, text="📅", width=3, command=lambda: self.pick_filter_date(self.to_entry)).grid(row=1, column=1)
        ttk.Button(filters, text="تطبيق", command=self.load).grid(row=0, column=0, rowspan=2, padx=8, ipadx=8, ipady=5)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=5)
        ttk.Button(actions, text="تحديث", command=self.load).pack(side="right", padx=4)
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(actions, text="إضافة إجازة", command=self.add_leave).pack(side="right", padx=4)
            ttk.Button(actions, text="إلغاء", command=lambda: self.set_status("ملغاة")).pack(side="right", padx=4)
        if app.current_user["role"] == "admin":
            ttk.Button(actions, text="اعتماد", command=lambda: self.set_status("معتمدة")).pack(side="right", padx=4)
            ttk.Button(actions, text="رفض", command=lambda: self.set_status("مرفوضة")).pack(side="right", padx=4)
            ttk.Button(actions, text="حذف", command=self.delete_selected).pack(side="left", padx=4)

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True, pady=5)
        columns = ("employee", "type", "start", "end", "days", "status", "reason", "updated")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse")
        specs = (
            ("employee", "الموظف", 210), ("type", "نوع الإجازة", 110),
            ("start", "من", 105), ("end", "إلى", 105), ("days", "الأيام", 65),
            ("status", "الحالة", 90), ("reason", "السبب", 230), ("updated", "آخر تحديث", 150),
        )
        for key, label, width in specs:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center" if key != "reason" else "e")
        ybar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _event: self.show_details())
        self.load()

    def pick_filter_date(self, entry):
        DatePicker(
            self, entry.get(), lambda value: EmployeeDialog.set_entry(entry, value),
            min_year=date.today().year - 10, max_year=date.today().year + 10,
            title="اختر تاريخ التصفية",
        )

    def load(self):
        search = self.search_var.get()
        status = self.status_var.get()
        date_from = self.from_entry.get().strip()
        date_to = self.to_entry.get().strip()

        def render(rows):
            self.rows = {str(row["id"]): row for row in rows}
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                self.tree.insert("", "end", iid=str(row["id"]), values=(
                    f"{row['emp_code']} — {row['name']}", row["leave_type"],
                    row["start_date"], row["end_date"], row["total_days"], row["status"],
                    row.get("reason") or "", row["updated_at"],
                ))

        self.app.run_async(
            lambda: self.app.db.list_leaves(search, status, date_from, date_to),
            render, busy_text="تحميل الإجازات...",
        )

    def selected(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def add_leave(self):
        LeaveDialog(self, self.app, on_saved=self.load)

    def set_status(self, status):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر طلب إجازة أولًا.", parent=self)
            return
        note = simpledialog.askstring("ملاحظة القرار", "ملاحظة اختيارية:", parent=self) or ""
        self.app.run_async(
            lambda: self.app.db.update_leave_status(row["id"], status, self.app.current_user, note),
            lambda _result: (messagebox.showinfo("تم", f"تم تغيير الحالة إلى: {status}", parent=self), self.load()),
            busy_text="تحديث حالة الإجازة...",
        )

    def delete_selected(self):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر طلب إجازة أولًا.", parent=self)
            return
        if not messagebox.askyesno("تأكيد الحذف", "هل تريد حذف طلب الإجازة نهائيًا؟", parent=self):
            return
        self.app.run_async(
            lambda: self.app.db.delete_leave(row["id"], self.app.current_user),
            lambda _result: (messagebox.showinfo("تم", "تم حذف الطلب.", parent=self), self.load()),
            busy_text="حذف طلب الإجازة...",
        )

    def show_details(self):
        row = self.selected()
        if not row:
            return
        details = (
            f"الموظف: {row['emp_code']} — {row['name']}\n"
            f"القسم: {row.get('department') or ''}\n"
            f"النوع: {row['leave_type']}\n"
            f"الفترة: {row['start_date']} إلى {row['end_date']} ({row['total_days']} يوم)\n"
            f"الحالة: {row['status']}\n"
            f"السبب: {row.get('reason') or ''}\n"
            f"الملاحظات: {row.get('notes') or ''}"
        )
        messagebox.showinfo("تفاصيل الإجازة", details, parent=self)


class LeaveDialog(tk.Toplevel):
    def __init__(self, master, app, on_saved=None):
        super().__init__(master)
        self.app = app
        self.on_saved = on_saved
        self.choice_map: dict[str, int] = {}
        self.title("إضافة طلب إجازة")
        self.geometry("620x480")
        self.transient(master)
        self.grab_set()
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="إضافة إجازة", font=TITLE_FONT).grid(row=0, column=0, columnspan=3, pady=(0, 15))
        ttk.Label(frame, text="الموظف").grid(row=1, column=2, sticky="e", padx=5, pady=6)
        self.employee_var = tk.StringVar()
        self.employee_combo = ttk.Combobox(frame, textvariable=self.employee_var, state="readonly", width=42)
        self.employee_combo.grid(row=1, column=0, columnspan=2, sticky="ew", padx=5, pady=6)

        ttk.Label(frame, text="نوع الإجازة").grid(row=2, column=2, sticky="e", padx=5, pady=6)
        self.type_var = tk.StringVar(value=LEAVE_TYPES[0])
        ttk.Combobox(frame, textvariable=self.type_var, values=LEAVE_TYPES, state="readonly", width=22).grid(
            row=2, column=1, sticky="ew", padx=5, pady=6
        )

        self.start_entry = ttk.Entry(frame, width=14)
        self.end_entry = ttk.Entry(frame, width=14)
        self.start_entry.insert(0, date.today().isoformat())
        self.end_entry.insert(0, date.today().isoformat())
        ttk.Label(frame, text="تاريخ البداية").grid(row=3, column=2, sticky="e", padx=5, pady=6)
        self.start_entry.grid(row=3, column=1, sticky="ew", padx=5, pady=6)
        ttk.Button(frame, text="📅", width=3, command=lambda: self.pick_date(self.start_entry, "بداية الإجازة")).grid(row=3, column=0)
        ttk.Label(frame, text="تاريخ النهاية").grid(row=4, column=2, sticky="e", padx=5, pady=6)
        self.end_entry.grid(row=4, column=1, sticky="ew", padx=5, pady=6)
        ttk.Button(frame, text="📅", width=3, command=lambda: self.pick_date(self.end_entry, "نهاية الإجازة")).grid(row=4, column=0)

        ttk.Label(frame, text="السبب").grid(row=5, column=2, sticky="e", padx=5, pady=6)
        self.reason_entry = ttk.Entry(frame, width=45)
        self.reason_entry.grid(row=5, column=0, columnspan=2, sticky="ew", padx=5, pady=6)
        ttk.Label(frame, text="ملاحظات").grid(row=6, column=2, sticky="ne", padx=5, pady=6)
        self.notes = tk.Text(frame, height=6, wrap="word")
        self.notes.grid(row=6, column=0, columnspan=2, sticky="nsew", padx=5, pady=6)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(6, weight=1)
        buttons = ttk.Frame(frame)
        buttons.grid(row=7, column=0, columnspan=3, pady=12)
        ttk.Button(buttons, text="حفظ", command=self.save).pack(side="right", padx=5, ipadx=15)
        ttk.Button(buttons, text="إغلاق", command=self.destroy).pack(side="right", padx=5, ipadx=15)
        self.load_choices()

    def pick_date(self, entry, label):
        DatePicker(
            self, entry.get(), lambda value: EmployeeDialog.set_entry(entry, value),
            min_year=date.today().year - 5, max_year=date.today().year + 10,
            title=f"اختر {label}",
        )

    def load_choices(self):
        def render(choices):
            self.choice_map = {
                f"{item['emp_code']} — {item['name']} — {item['department']}": item["id"] for item in choices
            }
            values = list(self.choice_map)
            self.employee_combo["values"] = values
            if values:
                self.employee_combo.current(0)
        self.app.run_async(
            lambda: self.app.db.list_employee_choices(active_only=False),
            render, busy_text="تحميل قائمة الموظفين...",
        )

    def save(self):
        employee_id = self.choice_map.get(self.employee_var.get())
        if not employee_id:
            messagebox.showwarning("تنبيه", "اختر موظفًا.", parent=self)
            return
        operation = lambda: self.app.db.create_leave(
            employee_id, self.type_var.get(), self.start_entry.get(), self.end_entry.get(),
            self.reason_entry.get(), self.notes.get("1.0", "end"), self.app.current_user,
        )

        def success(_result):
            messagebox.showinfo("تم", "تمت إضافة طلب الإجازة بحالة معلّقة.", parent=self)
            if self.on_saved:
                self.on_saved()
            self.destroy()
        self.app.run_async(operation, success, busy_text="حفظ الإجازة...")


class CalendarPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "التقويم الرقمي اليومي")
        today = date.today()
        self.year = today.year
        self.month = today.month
        self.selected_date = today.isoformat()
        self.month_data: dict = {}
        self.day_buttons: dict[str, ttk.Button] = {}
        self.clock_var = tk.StringVar()
        self.month_load_token = 0
        self.day_load_token = 0

        top = ttk.Frame(self)
        top.pack(fill="x", pady=4)
        ttk.Label(top, textvariable=self.clock_var, font=("Segoe UI", 13, "bold")).pack(side="left", padx=5)
        ttk.Button(top, text="اليوم", command=self.go_today).pack(side="right", padx=3)
        ttk.Button(top, text="◀ الشهر السابق", command=lambda: self.shift_month(-1)).pack(side="right", padx=3)
        ttk.Button(top, text="الشهر التالي ▶", command=lambda: self.shift_month(1)).pack(side="right", padx=3)
        ttk.Button(top, text="-10 س", command=lambda: self.shift_year(-10)).pack(side="right", padx=2)
        ttk.Button(top, text="-1 س", command=lambda: self.shift_year(-1)).pack(side="right", padx=2)
        ttk.Button(top, text="+1 س", command=lambda: self.shift_year(1)).pack(side="right", padx=2)
        ttk.Button(top, text="+10 س", command=lambda: self.shift_year(10)).pack(side="right", padx=2)
        self.month_var = tk.StringVar()
        month_names = (
            "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
            "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"
        )
        self.month_names = month_names
        month_combo = ttk.Combobox(top, textvariable=self.month_var, values=month_names, state="readonly", width=10)
        month_combo.pack(side="right", padx=3)
        month_combo.bind("<<ComboboxSelected>>", lambda _event: self.jump_to_controls())
        self.year_var = tk.StringVar(value=str(self.year))
        year_spin = ttk.Spinbox(top, from_=1930, to=2100, textvariable=self.year_var, width=7, command=self.jump_to_controls)
        year_spin.pack(side="right", padx=3)
        year_spin.bind("<Return>", lambda _event: self.jump_to_controls())
        ttk.Button(top, text="انتقال", command=self.jump_to_controls).pack(side="right", padx=3)

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True, pady=6)
        self.details_frame = ttk.LabelFrame(body, text="تفاصيل اليوم", padding=10)
        self.calendar_frame = ttk.LabelFrame(body, text="الشهر", padding=8)
        body.add(self.details_frame, weight=2)
        body.add(self.calendar_frame, weight=3)

        self.selected_var = tk.StringVar(value=self.selected_date)
        ttk.Label(self.details_frame, textvariable=self.selected_var, font=("Segoe UI", 12, "bold"), anchor="e").pack(fill="x")
        ttk.Label(self.details_frame, text="الحضور والإجازات", anchor="e").pack(fill="x", pady=(8, 3))
        self.details_text = tk.Text(self.details_frame, height=12, wrap="word", state="disabled")
        self.details_text.pack(fill="both", expand=True)
        ttk.Label(self.details_frame, text="ملاحظات اليوم", anchor="e").pack(fill="x", pady=(8, 3))
        self.note_text = tk.Text(self.details_frame, height=6, wrap="word")
        self.note_text.pack(fill="x")
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(self.details_frame, text="حفظ ملاحظة اليوم", command=self.save_note).pack(anchor="e", pady=6)
        else:
            self.note_text.configure(state="disabled")

        for col, label in enumerate(("ن", "ث", "ر", "خ", "ج", "س", "ح")):
            self.calendar_frame.columnconfigure(col, weight=1)
            ttk.Label(self.calendar_frame, text=label, anchor="center", font=FONT_BOLD).grid(
                row=0, column=col, sticky="ew", padx=2, pady=2
            )
        for row in range(1, 7):
            self.calendar_frame.rowconfigure(row, weight=1)
        self.month_var.set(self.month_names[self.month - 1])
        self.update_clock()
        self.load_month()

    def update_clock(self):
        if not self.winfo_exists():
            return
        self.clock_var.set(datetime.now().strftime("%Y-%m-%d  %H:%M:%S"))
        self.after(1000, self.update_clock)

    def go_today(self):
        today = date.today()
        self.year, self.month = today.year, today.month
        self.selected_date = today.isoformat()
        self.sync_controls()
        self.load_month()

    def shift_month(self, amount):
        self.month += amount
        if self.month < 1:
            self.month = 12
            self.year -= 1
        elif self.month > 12:
            self.month = 1
            self.year += 1
        self.sync_controls()
        self.load_month()

    def shift_year(self, amount):
        self.year = min(max(self.year + amount, 1930), 2100)
        self.sync_controls()
        self.load_month()

    def sync_controls(self):
        self.year_var.set(str(self.year))
        self.month_var.set(self.month_names[self.month - 1])

    def jump_to_controls(self):
        try:
            self.year = min(max(int(self.year_var.get()), 1930), 2100)
        except ValueError:
            self.year = date.today().year
        try:
            self.month = self.month_names.index(self.month_var.get()) + 1
        except ValueError:
            self.month = date.today().month
        self.sync_controls()
        self.load_month()

    def load_month(self):
        year, month = self.year, self.month
        self.month_load_token += 1
        token = self.month_load_token

        def render_if_current(data):
            if token == self.month_load_token and self.winfo_exists():
                self.render_month(data)

        self.app.run_async(
            lambda: self.app.db.calendar_month(year, month),
            render_if_current, busy_text="تحميل التقويم...",
        )

    def render_month(self, data):
        self.month_data = data
        for widget in list(self.calendar_frame.winfo_children()):
            info = widget.grid_info()
            if info and int(info.get("row", 0)) > 0:
                widget.destroy()
        self.day_buttons.clear()
        cal = calendar_module.Calendar(firstweekday=0)
        today = date.today().isoformat()
        for row_index, week in enumerate(cal.monthdayscalendar(self.year, self.month), start=1):
            for col, day_number in enumerate(week):
                if not day_number:
                    spacer = ttk.Label(self.calendar_frame, text="")
                    spacer.grid(row=row_index, column=col, sticky="nsew", padx=2, pady=2)
                    continue
                day_key = f"{self.year:04d}-{self.month:02d}-{day_number:02d}"
                parts = [str(day_number)]
                attendance = data.get("attendance", {}).get(day_key, {})
                leaves = data.get("leaves", {}).get(day_key, [])
                if attendance:
                    parts.append(f"حضور {sum(attendance.values())}")
                if leaves:
                    parts.append(f"إجازات {len(leaves)}")
                if day_key in data.get("notes", {}):
                    parts.append("📝 ملاحظة")
                holiday = data.get("holidays", {}).get(day_key)
                if holiday:
                    parts.append(f"🎉 {holiday['name']}")
                if day_key == today:
                    parts[0] = f"● {parts[0]}"
                button = ttk.Button(
                    self.calendar_frame, text="\n".join(parts),
                    command=lambda value=day_key: self.select_day(value),
                )
                button.grid(row=row_index, column=col, sticky="nsew", padx=2, pady=2, ipady=6)
                self.day_buttons[day_key] = button
        if self.selected_date[:7] != f"{self.year:04d}-{self.month:02d}":
            self.selected_date = f"{self.year:04d}-{self.month:02d}-01"
        self.select_day(self.selected_date)

    def select_day(self, day_key):
        self.selected_date = day_key
        self.selected_var.set(f"التاريخ المحدد: {day_key}")
        self.day_load_token += 1
        token = self.day_load_token

        def render_if_current(details):
            if token == self.day_load_token and day_key == self.selected_date and self.winfo_exists():
                self.render_day(details)

        self.app.run_async(
            lambda: self.app.db.calendar_day_details(day_key),
            render_if_current, busy_text="تحميل تفاصيل اليوم...",
        )

    def render_day(self, details):
        lines = []
        if details.get("holiday"):
            lines.append(f"🎉 عطلة: {details['holiday']['name']}")
            if details["holiday"].get("notes"):
                lines.append(details["holiday"]["notes"])
            lines.append("")
        if details["attendance"]:
            lines.append("الحضور:")
            for item in details["attendance"]:
                suffix = f" — {item['note']}" if item.get("note") else ""
                lines.append(f"• {item['emp_code']} — {item['name']}: {item['status']}{suffix}")
        else:
            lines.append("لا توجد سجلات حضور لهذا اليوم.")
        lines.append("")
        if details["leaves"]:
            lines.append("الإجازات:")
            for item in details["leaves"]:
                lines.append(
                    f"• {item['emp_code']} — {item['name']}: {item['leave_type']} "
                    f"({item['status']}) من {item['start_date']} إلى {item['end_date']}"
                )
        else:
            lines.append("لا توجد إجازات لهذا اليوم.")
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", "\n".join(lines))
        self.details_text.configure(state="disabled")
        can_edit = self.app.current_user["role"] in {"admin", "hr"}
        self.note_text.configure(state="normal")
        self.note_text.delete("1.0", "end")
        self.note_text.insert("1.0", details.get("note") or "")
        if not can_edit:
            self.note_text.configure(state="disabled")

    def save_note(self):
        note = self.note_text.get("1.0", "end").strip()
        self.app.run_async(
            lambda: self.app.db.upsert_calendar_note(self.selected_date, note, self.app.current_user),
            lambda _result: (messagebox.showinfo("تم", "تم حفظ ملاحظة اليوم.", parent=self), self.load_month()),
            busy_text="حفظ ملاحظة اليوم...",
        )


class ImportantLinksPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "روابط مهمة للموارد البشرية")
        ttk.Label(
            self,
            text=(
                "اختصارات للخدمات الحكومية والأنظمة السعودية الرسمية. "
                "لا يحفظ البرنامج أسماء المستخدمين أو كلمات المرور، وقد تتغير صفحات الجهات مستقبلاً."
            ),
            foreground="#526274",
            justify="right",
            anchor="e",
            wraplength=960,
            padding=(0, 0, 0, 10),
        ).pack(fill="x")

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        categories = []
        for item in IMPORTANT_LINKS:
            if item.category not in categories:
                categories.append(item.category)

        for category in categories:
            tab = ttk.Frame(notebook, padding=10)
            notebook.add(tab, text=category)
            canvas = tk.Canvas(tab, highlightthickness=0, bg=COLORS["background"])
            scrollbar = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
            body = ttk.Frame(canvas)
            window = canvas.create_window((0, 0), window=body, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.pack(side="right", fill="both", expand=True)
            scrollbar.pack(side="left", fill="y")
            body.bind("<Configure>", lambda _event, c=canvas: c.configure(scrollregion=c.bbox("all")))
            canvas.bind(
                "<Configure>",
                lambda event, c=canvas, w=window: c.itemconfigure(w, width=event.width),
            )

            for item in (link for link in IMPORTANT_LINKS if link.category == category):
                row = ttk.LabelFrame(body, text=item.title, padding=(14, 10))
                row.pack(fill="x", padx=4, pady=5)
                ttk.Button(
                    row,
                    text="فتح الموقع الرسمي",
                    command=lambda url=item.url: self.open_official_link(url),
                ).pack(side="left", padx=(0, 12), ipadx=8)
                ttk.Label(
                    row,
                    text=item.description,
                    justify="right",
                    anchor="e",
                    wraplength=720,
                ).pack(side="right", fill="x", expand=True)

    def open_official_link(self, url: str):
        try:
            safe_url = validate_official_url(url)
            if not webbrowser.open_new_tab(safe_url):
                raise RuntimeError("تعذر تسليم الرابط إلى المتصفح.")
        except Exception as exc:
            setup_logging().error("تعذر فتح رابط رسمي: %s", exc, exc_info=True)
            messagebox.showerror(
                "تعذر فتح الرابط",
                f"لم يتمكن البرنامج من فتح الموقع الرسمي.\n\n{exc}",
                parent=self,
            )


class ReportsPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "التقارير والتصدير")
        self.stats_var = tk.StringVar(value="تحميل الإحصائيات...")
        self.employee_report_map: dict[str, int] = {}
        ttk.Label(
            self, textvariable=self.stats_var, justify="right", anchor="e", padding=12
        ).pack(fill="x")

        general = ttk.LabelFrame(self, text="التقارير العامة", padding=14)
        general.pack(fill="x", padx=6, pady=(8, 6))
        ttk.Label(
            general,
            text="تقرير كامل لجميع بيانات الموارد البشرية بصيغ جاهزة للطباعة والتحليل.",
            foreground=COLORS["muted"], anchor="e",
        ).pack(fill="x", pady=(0, 10))
        buttons = ttk.Frame(general)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="تصدير Excel", command=self.export_excel).pack(side="right", padx=8, ipadx=15, ipady=8)
        ttk.Button(buttons, text="تصدير PDF", command=self.export_pdf).pack(side="right", padx=8, ipadx=15, ipady=8)
        ttk.Button(buttons, text="تصدير HTML", command=self.export_html).pack(side="right", padx=8, ipadx=15, ipady=8)
        ttk.Button(buttons, text="فتح مجلد التقارير", command=lambda: open_path(EXPORT_DIR)).pack(side="right", padx=8, ipadx=15, ipady=8)

        employee_card = ttk.LabelFrame(self, text="تقرير موظف مفصل", padding=14)
        employee_card.pack(fill="x", padx=6, pady=6)
        ttk.Label(
            employee_card,
            text=(
                "اختر الموظف ثم صدّر ملفًا مفصلًا يشمل بياناته والحضور والإجازات "
                "والمستندات والحقول المخصصة."
            ),
            foreground=COLORS["muted"], anchor="e", justify="right",
        ).pack(fill="x", pady=(0, 10))
        employee_actions = ttk.Frame(employee_card)
        employee_actions.pack(fill="x")
        ttk.Label(employee_actions, text="الموظف:").pack(side="right", padx=(6, 2))
        self.employee_report_var = tk.StringVar(value="تحميل قائمة الموظفين...")
        self.employee_report_combo = ttk.Combobox(
            employee_actions,
            textvariable=self.employee_report_var,
            state="readonly",
            width=52,
        )
        self.employee_report_combo.pack(side="right", fill="x", expand=True, padx=5)
        ttk.Button(
            employee_actions,
            text="تصدير PDF فاخر",
            command=lambda: self.export_individual_employee("pdf"),
        ).pack(side="right", padx=5, ipadx=10, ipady=5)
        ttk.Button(
            employee_actions,
            text="تصدير Excel فاخر",
            command=lambda: self.export_individual_employee("xlsx"),
        ).pack(side="right", padx=5, ipadx=10, ipady=5)
        ttk.Button(
            employee_actions,
            text="تحديث القائمة",
            command=self.load_employee_report_choices,
        ).pack(side="left", padx=5)
        ttk.Label(
            self,
            text=(
                "يُنشئ النظام PDF جاهزًا للطباعة وExcel منسقًا باتجاه عربي، "
                "وأُعيد تصدير HTML المتوافق مع المتصفح من الإصدارات السابقة.\n"
                "تُخفى أرقام الهوية والحسابات البنكية من واجهات العرض والتقارير العامة."
            ),
            foreground="#555", justify="right",
        ).pack(anchor="e", pady=15)
        self.app.run_async(self.app.db.dashboard_stats, self.render_stats, busy_text="تحميل الإحصائيات...")
        self.load_employee_report_choices()

    def render_stats(self, stats):
        self.stats_var.set(
            f"إجمالي الموظفين: {stats['total']}\n"
            f"الموظفون النشطون: {stats['active'] or 0}\n"
            f"إجمالي الرواتب: {stats['salaries']:,.2f}\n"
            f"متوسط الراتب: {stats['avg_salary']:,.2f}"
        )

    def load_employee_report_choices(self):
        self.app.run_async(
            lambda: self.app.db.list_employee_choices(active_only=False),
            self.render_employee_report_choices,
            busy_text="تحميل قائمة الموظفين...",
        )

    def render_employee_report_choices(self, employees):
        self.employee_report_map = {}
        for employee in employees:
            department = str(employee.get("department") or "بدون قسم")
            label = f"{employee.get('emp_code') or '—'} — {employee.get('name') or '—'} — {department}"
            self.employee_report_map[label] = int(employee["id"])
        values = list(self.employee_report_map)
        self.employee_report_combo.configure(values=values)
        self.employee_report_var.set(values[0] if values else "لا يوجد موظفون")

    def export_individual_employee(self, file_type: str):
        employee_id = self.employee_report_map.get(self.employee_report_var.get())
        if not employee_id:
            messagebox.showwarning(
                "اختر موظفًا", "اختر موظفًا من القائمة أولًا.", parent=self
            )
            return

        def work():
            report = self.app.db.employee_report_data(employee_id)
            employee = report["employee"]
            code = "".join(
                ch for ch in str(employee.get("emp_code") or employee_id)
                if ch.isalnum() or ch in "-_"
            )
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            if file_type == "pdf":
                return export_employee_pdf(
                    EXPORT_DIR / f"ProTech_HR_Employee_{code}_{stamp}.pdf", report
                )
            return export_employee_xlsx(
                EXPORT_DIR / f"ProTech_HR_Employee_{code}_{stamp}.xlsx", report
            )

        label = "PDF" if file_type == "pdf" else "Excel"
        self.app.run_async(
            work,
            lambda path: self.export_done(path, True),
            busy_text=f"إنشاء تقرير الموظف {label}...",
        )

    def export_excel(self):
        filename = EXPORT_DIR / f"ProTech_HR_Report_{datetime.now():%Y%m%d_%H%M%S}.xlsx"

        def work():
            data = self.app.db.all_export_data()
            data.update(self.app.enterprise.export_data())
            for employee in data["Employees"]:
                employee["id_number"] = mask_id_number(employee.get("id_number", ""))
                employee["bank_account_number"] = mask_bank_account(
                    employee.get("bank_account_number", "")
                )
                employee["iban"] = mask_iban(employee.get("iban", ""))
                employee.pop("id_hash", None)
            return export_xlsx(filename, data, company_profile=self.app.db.company_profile())

        self.app.run_async(work, lambda path: self.export_done(path), busy_text="إنشاء ملف Excel...")

    def export_pdf(self):
        filename = EXPORT_DIR / f"ProTech_HR_Print_Report_{datetime.now():%Y%m%d_%H%M%S}.pdf"

        def work():
            data = self.app.db.all_export_data()["Employees"]
            stats = self.app.db.dashboard_stats()
            return export_printable_pdf(
                filename, data, stats, company_profile=self.app.db.company_profile()
            )

        self.app.run_async(work, lambda path: self.export_done(path, True), busy_text="إنشاء ملف PDF...")

    def export_html(self):
        filename = EXPORT_DIR / f"ProTech_HR_Print_Report_{datetime.now():%Y%m%d_%H%M%S}.html"

        def work():
            data = self.app.db.all_export_data()["Employees"]
            stats = self.app.db.dashboard_stats()
            return export_printable_html(
                filename, data, stats, company_profile=self.app.db.company_profile()
            )

        self.app.run_async(work, lambda path: self.export_done(path, True), busy_text="إنشاء ملف HTML...")

    def export_done(self, path, open_file=False):
        messagebox.showinfo("تم", f"تم إنشاء التقرير:\n{path}", parent=self)
        open_path(path if open_file else path.parent)


class AuditPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "سجل التدقيق")
        top = ttk.Frame(self)
        top.pack(fill="x", pady=5)
        ttk.Label(top, text="بحث:").pack(side="right")
        self.search_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.search_var, width=35)
        entry.pack(side="right", padx=5)
        ttk.Button(top, text="بحث", command=self.load).pack(side="right", padx=5)
        table = ttk.Frame(self)
        table.pack(fill="both", expand=True)
        columns = ("created_at", "username", "action", "entity_type", "entity_id", "details")
        self.tree = ttk.Treeview(table, columns=columns, show="headings")
        specs = (("created_at", "الوقت", 155), ("username", "المستخدم", 120), ("action", "الإجراء", 100),
                 ("entity_type", "النوع", 100), ("entity_id", "المعرّف", 100), ("details", "التفاصيل", 380))
        for key, label, width in specs:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="e")
        ybar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.load()

    def load(self):
        def render(rows):
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                details = row.get("details") or ""
                try:
                    details = json.dumps(json.loads(details), ensure_ascii=False)
                except (ValueError, TypeError):
                    pass
                self.tree.insert("", "end", values=(row["created_at"], row["username"], row["action"],
                                                     row["entity_type"], row.get("entity_id") or "", details))
        query = self.search_var.get()
        self.app.run_async(lambda: self.app.db.list_audit_logs(query), render, busy_text="تحميل سجل التدقيق...")


class UsersPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "إدارة المستخدمين والصلاحيات")
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=5)
        ttk.Button(buttons, text="إضافة مستخدم", command=self.add_user).pack(side="right", padx=5)
        ttk.Button(buttons, text="تعديل المحدد", command=self.edit_user).pack(side="right", padx=5)
        ttk.Button(buttons, text="تحديث", command=self.load).pack(side="right", padx=5)
        settings = ttk.LabelFrame(self, text="تخصيص أمان الجلسة", padding=8)
        settings.pack(fill="x", pady=(5, 8))
        ttk.Label(settings, text="تسجيل الخروج بعد عدم النشاط (دقيقة)").pack(side="right", padx=6)
        self.timeout_var = tk.StringVar(value=str(app.db.session_timeout_minutes()))
        ttk.Combobox(
            settings,
            textvariable=self.timeout_var,
            values=("5", "15", "30", "60", "120", "240"),
            state="readonly",
            width=8,
        ).pack(side="right", padx=6)
        ttk.Button(settings, text="حفظ المهلة", command=self.save_timeout).pack(side="right", padx=6)
        self.tree = ttk.Treeview(self, columns=("username", "full_name", "role", "active", "created"), show="headings")
        for key, label, width in (("username", "اسم المستخدم", 150), ("full_name", "الاسم", 220),
                                  ("role", "الصلاحية", 150), ("active", "نشط", 80), ("created", "تاريخ الإنشاء", 170)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True, pady=8)
        self.load()

    def load(self):
        def render(users):
            self.users = {str(u["id"]): u for u in users}
            self.tree.delete(*self.tree.get_children())
            for user in users:
                self.tree.insert("", "end", iid=str(user["id"]), values=(
                    user["username"], user["full_name"], ROLES[user["role"]],
                    "نعم" if user["is_active"] else "لا", user["created_at"],
                ))
        self.app.run_async(self.app.db.list_users, render, busy_text="تحميل المستخدمين...")

    def selected_user(self):
        selected = self.tree.selection()
        return self.users.get(selected[0]) if selected else None

    def add_user(self):
        UserDialog(self, self.app, on_saved=self.load)

    def edit_user(self):
        user = self.selected_user()
        if not user:
            messagebox.showwarning("تنبيه", "اختر مستخدمًا.", parent=self)
            return
        UserDialog(self, self.app, user=user, on_saved=self.load)

    def save_timeout(self):
        minutes = int(self.timeout_var.get())

        def success(saved_minutes):
            self.app.session_timeout_seconds = int(saved_minutes) * 60
            messagebox.showinfo(
                "تم الحفظ",
                f"تم ضبط مهلة الخمول على {saved_minutes} دقيقة.",
                parent=self,
            )

        self.app.run_async(
            lambda: self.app.db.set_session_timeout_minutes(
                minutes,
                self.app.current_user,
            ),
            success,
            busy_text="حفظ إعداد الجلسة...",
        )


class UserDialog(tk.Toplevel):
    def __init__(self, master, app, user=None, on_saved=None):
        super().__init__(master)
        self.app = app
        self.user = user
        self.on_saved = on_saved
        self.title("تعديل مستخدم" if user else "إضافة مستخدم")
        self.transient(master)
        self.grab_set()
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        self.username = ttk.Entry(frame, width=30)
        self.full_name = ttk.Entry(frame, width=30)
        self.password = ttk.Entry(frame, width=30, show="*")
        self.role = ttk.Combobox(frame, values=list(ROLES), state="readonly", width=27)
        self.active = tk.BooleanVar(value=True)
        fields = (("اسم المستخدم", self.username), ("الاسم الكامل", self.full_name),
                  ("كلمة المرور" + (" الجديدة (اختياري)" if user else ""), self.password), ("الصلاحية", self.role))
        for row, (label, widget) in enumerate(fields):
            ttk.Label(frame, text=label).grid(row=row, column=1, sticky="e", padx=6, pady=7)
            widget.grid(row=row, column=0, padx=6, pady=7)
        ttk.Label(
            frame,
            text="كلمة المرور: 8 أحرف على الأقل، وتتضمن حرفًا ورقمًا.",
            foreground="#64748B",
        ).grid(row=4, column=0, columnspan=2, pady=(0, 5))
        ttk.Checkbutton(frame, text="الحساب نشط", variable=self.active).grid(row=5, column=0, columnspan=2, pady=7)
        ttk.Button(frame, text="حفظ", command=self.save).grid(row=6, column=0, columnspan=2, pady=12, ipadx=15)
        if user:
            self.username.insert(0, user["username"])
            self.username.configure(state="disabled")
            self.full_name.insert(0, user["full_name"])
            self.full_name.configure(state="disabled")
            self.role.set(user["role"])
            self.active.set(bool(user["is_active"]))
        else:
            self.role.set("viewer")

    def save(self):
        role = self.role.get()
        is_active = self.active.get()
        password = self.password.get()
        username = self.username.get()
        full_name = self.full_name.get()
        if self.user:
            operation = lambda: self.app.db.update_user(
                self.user["id"], role=role, is_active=is_active,
                new_password=password or None, actor=self.app.current_user,
            )
        else:
            operation = lambda: self.app.db.create_user(
                username, full_name, password, role, self.app.current_user,
            )

        def success(_):
            messagebox.showinfo("تم", "تم حفظ المستخدم.", parent=self)
            if self.on_saved:
                self.on_saved()
            self.destroy()

        self.app.run_async(operation, success, busy_text="حفظ المستخدم...")


class BackupPage(Page):
    def __init__(self, master, app):
        super().__init__(master, app, "النسخ الاحتياطي والاستعادة")
        buttons = ttk.Frame(self)
        buttons.pack(fill="x", pady=5)
        ttk.Button(buttons, text="إنشاء نسخة الآن", command=self.create_backup).pack(side="right", padx=5)
        ttk.Button(buttons, text="تصدير نسخة خارجية...", command=self.export_backup).pack(side="right", padx=5)
        ttk.Button(buttons, text="فتح مجلد النسخ", command=lambda: open_path(BACKUP_DIR)).pack(side="right", padx=5)
        ttk.Button(buttons, text="تحديث القائمة", command=self.load).pack(side="right", padx=5)
        if app.current_user["role"] == "admin":
            ttk.Button(buttons, text="استعادة المحدد", command=self.restore).pack(side="right", padx=5)
            ttk.Button(buttons, text="استعادة من ملف...", command=self.restore_from_file).pack(side="right", padx=5)
            ttk.Button(buttons, text="نسخة حماية وصيانة", command=self.maintain).pack(side="right", padx=5)
        health_bar = ttk.Frame(self)
        health_bar.pack(fill="x", pady=(8, 2))
        self.health_var = tk.StringVar(value="حالة قاعدة البيانات: جارٍ الفحص...")
        ttk.Label(health_bar, textvariable=self.health_var, anchor="e").pack(side="right", fill="x", expand=True)
        ttk.Button(health_bar, text="فحص السلامة", command=self.check_health).pack(side="left", padx=5)
        ttk.Button(health_bar, text="تصدير تقرير الفحص", command=self.export_diagnostics).pack(side="left", padx=5)
        ttk.Label(
            self,
            text="ينشئ النظام نسخة كاملة تلقائيًا عند كل جلسة دخول، وتشمل قاعدة البيانات والعقود وصور الهوية والمستندات.",
            foreground="#555", anchor="e",
        ).pack(fill="x", pady=8)
        self.tree = ttk.Treeview(self, columns=("name", "date", "size"), show="headings")
        for key, label, width in (("name", "اسم الملف", 360), ("date", "تاريخ النسخة", 180), ("size", "الحجم", 120)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.load()

    def load(self):
        def render(paths):
            self.paths = {str(index): path for index, path in enumerate(paths)}
            self.tree.delete(*self.tree.get_children())
            for index, path in enumerate(paths):
                stat = path.stat()
                self.tree.insert("", "end", iid=str(index), values=(
                    path.name, datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    f"{stat.st_size / 1024:.1f} KB",
                ))
            self.check_health()
        self.app.run_async(self.app.enterprise.list_full_backups, render, busy_text="تحميل النسخ الاحتياطية الكاملة...")

    def check_health(self):
        def render(result):
            size_mb = int(result["database_size_bytes"]) / (1024 * 1024)
            if result["ok"]:
                self.health_var.set(
                    f"حالة قاعدة البيانات: سليمة | {result['tables']} جدول | "
                    f"{size_mb:.2f} MB | {str(result['journal_mode']).upper()}"
                )
            else:
                self.health_var.set(
                    f"حالة قاعدة البيانات: تحتاج مراجعة | "
                    f"مشكلات العلاقات: {len(result['foreign_key_issues'])}"
                )
        self.app.run_async(
            self.app.db.health_check,
            render,
            busy_text="فحص سلامة قاعدة البيانات...",
        )

    def maintain(self):
        if not messagebox.askyesno(
            "تأكيد الصيانة الآمنة",
            "سيُنشئ النظام نسخة كاملة أولًا، ثم يفحص قاعدة البيانات ويضغطها ويحسن أداءها.\n"
            "قد تستغرق العملية بعض الوقت حسب حجم المرفقات. هل تريد المتابعة؟",
            parent=self,
        ):
            return

        def success(result):
            reclaimed_mb = int(result["reclaimed_bytes"]) / (1024 * 1024)
            messagebox.showinfo(
                "اكتملت الصيانة",
                "اكتمل فحص قاعدة البيانات وتحسينها بنجاح.\n"
                f"المساحة المستعادة: {reclaimed_mb:.2f} MB\n"
                f"نسخة الحماية: {result['backup']}",
                parent=self,
            )
            self.load()

        self.app.run_async(
            lambda: self.app.enterprise.maintain_database(self.app.current_user),
            success,
            busy_text="إنشاء نسخة حماية وصيانة قاعدة البيانات...",
        )

    def export_diagnostics(self):
        default = f"ProTech_HR_Diagnostics_{datetime.now():%Y%m%d_%H%M%S}.json"
        path = filedialog.asksaveasfilename(
            parent=self,
            title="تصدير تقرير فحص خالٍ من بيانات الموظفين الحساسة",
            initialfile=default,
            defaultextension=".json",
            filetypes=[("تقرير JSON", "*.json")],
        )
        if not path:
            return

        def success(saved):
            messagebox.showinfo(
                "تم التصدير",
                f"تم إنشاء تقرير الفحص بنجاح:\n{saved}",
                parent=self,
            )

        self.app.run_async(
            lambda: self.app.enterprise.export_diagnostic_report(
                path,
                self.app.current_user,
            ),
            success,
            busy_text="إنشاء تقرير الفحص...",
        )

    def create_backup(self):
        def success(path):
            messagebox.showinfo(
                "تم إنشاء النسخة",
                f"تم إنشاء نسخة كاملة بنجاح وتشمل قاعدة البيانات والمستندات وصور الهوية:\n{path}",
                parent=self,
            )
            self.load()

        self.app.run_async(
            lambda: self.app.enterprise.create_full_backup(self.app.current_user, automatic=False),
            success,
            busy_text="إنشاء نسخة احتياطية كاملة وآمنة...",
        )

    def export_backup(self):
        default = f"ProTech_HR_Full_Backup_{datetime.now():%Y%m%d_%H%M%S}.protechhr"
        path = filedialog.asksaveasfilename(
            parent=self, title="تصدير نسخة احتياطية كاملة إلى قرص خارجي أو سحابة",
            initialfile=default, defaultextension=".protechhr",
            filetypes=[("نسخة برو تك الكاملة", "*.protechhr")],
        )
        if not path:
            return
        def success(saved):
            messagebox.showinfo(
                "اكتمل التصدير",
                "تم تصدير قاعدة البيانات والمرفقات والهويات والعقود والإعدادات والمستخدمين بنجاح:\n" + str(saved),
                parent=self,
            )
            self.load()
        self.app.run_async(
            lambda: self.app.enterprise.export_full_backup(path, self.app.current_user),
            success, busy_text="تصدير نسخة احتياطية قابلة للاستعادة بعد تهيئة الجهاز...",
        )

    def restore_from_file(self):
        path = filedialog.askopenfilename(
            parent=self, title="اختر نسخة برو تك الكاملة للاستعادة",
            filetypes=[("نسخ برو تك", "*.protechhr *.db"), ("جميع الملفات", "*.*")],
        )
        if path:
            self._restore_path(Path(path))

    def restore(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("تنبيه", "اختر نسخة احتياطية.", parent=self)
            return
        self._restore_path(self.paths[selected[0]])

    def _restore_path(self, path: Path):
        if not messagebox.askyesno(
            "تأكيد الاستعادة الكاملة",
            "سيتم التحقق من سلامة النسخة، ثم إنشاء نسخة أمان للحالة الحالية واستعادة جميع البيانات والمرفقات.\n"
            "سيتم تسجيل خروجك بعد الاستعادة. هل تريد المتابعة؟",
            parent=self,
        ):
            return

        def success(_):
            messagebox.showinfo("تم", "تمت استعادة جميع البيانات والمرفقات بنجاح. سجّل الدخول مجددًا.", parent=self)
            self.app.logout()

        self.app.run_async(
            lambda: self.app.enterprise.restore_full_backup(path, self.app.current_user),
            success, busy_text="التحقق من النسخة واستعادة جميع البيانات...",
        )
