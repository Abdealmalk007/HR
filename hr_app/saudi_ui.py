from __future__ import annotations

import webbrowser
from datetime import date, datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .calendar_picker import DatePicker
from .config import EXPORT_DIR
from .modern_ui import COLORS, build_page_header, page_accent
from .saudi_hr import (
    REGULATORY_REFERENCE_DATE,
    TERMINATION_REASONS,
    calculate_end_of_service,
)


class SaudiCompliancePage(ttk.Frame):
    """لوحة سعودية عملية مع إبقاء الاعتماد النهائي للمنصات الرسمية."""

    def __init__(self, master, app):
        super().__init__(master, style="Page.TFrame")
        self.app = app
        self.accent = page_accent(self.__class__.__name__)
        self.snapshot: dict = {}
        build_page_header(
            self,
            "مركز الامتثال السعودي",
            self.accent,
            "السعودة وقوى والتأمينات وحماية الأجور ومكافأة نهاية الخدمة",
        )
        ttk.Label(
            self,
            text=(
                "مؤشرات الجاهزية مشتقة من البيانات المحفوظة حتى "
                f"{REGULATORY_REFERENCE_DATE}، ولا تثبت التسجيل أو القبول لدى الجهات الرسمية. "
                "تختلف نسبة السعودة النظامية حسب نشاط المنشأة وحجمها؛ أدخل مستهدف منشأتك من قوى."
            ),
            foreground=COLORS["muted"],
            justify="right",
            anchor="e",
            wraplength=1050,
        ).pack(fill="x", pady=(2, 8))

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        compliance_tab = ttk.Frame(notebook, padding=8)
        eos_tab = ttk.Frame(notebook, padding=8)
        guidance_tab = ttk.Frame(notebook, padding=8)
        notebook.add(compliance_tab, text="لوحة الجاهزية")
        notebook.add(eos_tab, text="حاسبة نهاية الخدمة")
        notebook.add(guidance_tab, text="المرجع النظامي")
        self._build_compliance_tab(compliance_tab)
        self._build_eos_tab(eos_tab)
        self._build_guidance_tab(guidance_tab)
        self.load()

    def _build_compliance_tab(self, parent):
        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Button(controls, text="تحديث المؤشرات", command=self.load).pack(side="right", padx=4)
        ttk.Button(controls, text="تصدير قائمة المتابعة CSV", command=self.export).pack(side="right", padx=4)
        ttk.Label(controls, text="مستهدف السعودة من قوى (%):").pack(side="left", padx=(4, 2))
        self.target_var = tk.StringVar()
        self.target_entry = ttk.Entry(controls, textvariable=self.target_var, width=9)
        self.target_entry.pack(side="left", padx=4)
        if self.app.current_user["role"] == "admin":
            ttk.Button(controls, text="حفظ المستهدف", command=self.save_target).pack(side="left", padx=4)
        else:
            self.target_entry.configure(state="readonly")

        cards = ttk.Frame(parent)
        cards.pack(fill="x", pady=(0, 8))
        self.metric_vars: dict[str, tk.StringVar] = {}
        specs = (
            ("saudization", "نسبة السعودة"),
            ("qiwa", "عقود قوى الموثقة"),
            ("wps", "جاهزية حماية الأجور"),
            ("gosi", "جاهزية بيانات التأمينات"),
            ("issues", "المخاطر الحرجة والعالية"),
        )
        for column, (key, label) in enumerate(specs):
            panel = tk.Frame(cards, bg="#FFFFFF", highlightbackground="#D8E1EC", highlightthickness=1)
            panel.grid(row=0, column=column, sticky="nsew", padx=4)
            cards.columnconfigure(column, weight=1, uniform="saudi_metrics")
            tk.Label(panel, text=label, bg="#FFFFFF", fg="#64748B", font=("Segoe UI", 9)).pack(pady=(9, 2))
            variable = tk.StringVar(value="—")
            tk.Label(panel, textvariable=variable, bg="#FFFFFF", fg=self.accent, font=("Segoe UI", 14, "bold")).pack(pady=(0, 9))
            self.metric_vars[key] = variable

        tree_host = ttk.Frame(parent)
        tree_host.pack(fill="both", expand=True)
        columns = ("severity", "category", "code", "employee", "details", "action")
        self.issues_tree = ttk.Treeview(tree_host, columns=columns, show="headings")
        for key, label, width in (
            ("severity", "الخطورة", 75),
            ("category", "الفئة", 105),
            ("code", "الرقم", 75),
            ("employee", "الموظف", 160),
            ("details", "الملاحظة", 330),
            ("action", "الإجراء المقترح", 310),
        ):
            self.issues_tree.heading(key, text=label)
            self.issues_tree.column(key, width=width, anchor="e" if key in {"details", "action"} else "center")
        y_scroll = ttk.Scrollbar(tree_host, orient="vertical", command=self.issues_tree.yview)
        x_scroll = ttk.Scrollbar(tree_host, orient="horizontal", command=self.issues_tree.xview)
        self.issues_tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.issues_tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        tree_host.rowconfigure(0, weight=1)
        tree_host.columnconfigure(0, weight=1)
        self.issue_status = tk.StringVar(value="جارٍ تحميل مؤشرات الجاهزية...")
        ttk.Label(parent, textvariable=self.issue_status, foreground=COLORS["muted"], anchor="e").pack(fill="x", pady=(5, 0))

    def _build_eos_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        form = ttk.LabelFrame(parent, text="بيانات الاحتساب", padding=14)
        form.grid(row=0, column=0, sticky="new", padx=6, pady=4)
        form.columnconfigure(0, weight=1)
        self.employee_map: dict[str, dict] = {}
        self.eos_employee = tk.StringVar(value="احتساب يدوي")
        self.eos_wage = tk.StringVar()
        self.eos_start = tk.StringVar()
        self.eos_end = tk.StringVar(value=date.today().isoformat())
        self.eos_reason = tk.StringVar(value=TERMINATION_REASONS[0])
        specs = (
            ("الموظف (اختياري)", self.eos_employee, "employee"),
            ("الأجر الأخير الخاضع للاحتساب", self.eos_wage, "entry"),
            ("تاريخ بداية الخدمة", self.eos_start, "date"),
            ("تاريخ نهاية الخدمة", self.eos_end, "date"),
            ("سبب انتهاء العلاقة", self.eos_reason, "reason"),
        )
        for row, (label, variable, kind) in enumerate(specs):
            ttk.Label(form, text=label).grid(row=row, column=1, sticky="e", padx=6, pady=6)
            holder = ttk.Frame(form)
            holder.grid(row=row, column=0, sticky="ew", pady=6)
            if kind == "employee":
                self.employee_combo = ttk.Combobox(holder, textvariable=variable, state="readonly", width=44)
                self.employee_combo.pack(fill="x")
                self.employee_combo.bind("<<ComboboxSelected>>", self._select_eos_employee)
            elif kind == "reason":
                ttk.Combobox(holder, textvariable=variable, values=TERMINATION_REASONS, state="readonly", width=42).pack(fill="x")
            else:
                ttk.Entry(holder, textvariable=variable, width=36).pack(side="right", fill="x", expand=True)
                if kind == "date":
                    ttk.Button(
                        holder,
                        text="📅",
                        width=3,
                        command=lambda value=variable: DatePicker(self, value.get(), value.set),
                    ).pack(side="left", padx=4)
        ttk.Button(form, text="احتساب المكافأة", command=self.calculate_eos).grid(row=len(specs), column=0, columnspan=2, pady=10, ipady=4)

        result = ttk.LabelFrame(parent, text="النتيجة التقديرية", padding=16)
        result.grid(row=1, column=0, sticky="nsew", padx=6, pady=8)
        parent.rowconfigure(1, weight=1)
        self.eos_result = tk.StringVar(
            value="أدخل البيانات ثم اضغط «احتساب المكافأة». سيظهر أساس الحساب ونسبة الاستحقاق ومدة الخدمة."
        )
        ttk.Label(
            result,
            textvariable=self.eos_result,
            justify="right",
            anchor="ne",
            wraplength=920,
            font=("Segoe UI", 11),
        ).pack(fill="both", expand=True)
        ttk.Label(
            result,
            text=(
                "هذه نتيجة تقديرية وفق القواعد العامة. قد تتأثر النتيجة بسبب عناصر الأجر المتفق على استبعادها، "
                "أو سبب الانتهاء، أو حكم/اتفاق خاص؛ راجع الحاسبة الرسمية والعقد قبل التسوية."
            ),
            foreground=COLORS["muted"],
            justify="right",
            anchor="e",
            wraplength=920,
        ).pack(fill="x", pady=(10, 0))
        self._load_eos_employees()

    def _build_guidance_tab(self, parent):
        ttk.Label(
            parent,
            text=f"مراجع رسمية راجعناها للإصدار 23.0.1 — تاريخ المراجعة: {REGULATORY_REFERENCE_DATE}",
            foreground=COLORS["muted"],
            anchor="e",
        ).pack(fill="x", pady=(0, 8))
        self.guidance = list(self.app.saudi.guidance())
        self.guidance_tree = ttk.Treeview(parent, columns=("title", "summary"), show="headings", height=12)
        self.guidance_tree.heading("title", text="الموضوع")
        self.guidance_tree.heading("summary", text="القاعدة والمتابعة")
        self.guidance_tree.column("title", width=190, anchor="e")
        self.guidance_tree.column("summary", width=760, anchor="e")
        for index, item in enumerate(self.guidance):
            self.guidance_tree.insert("", "end", iid=str(index), values=(item["title"], item["summary"]))
        self.guidance_tree.pack(fill="both", expand=True)
        self.guidance_tree.bind("<Double-1>", lambda _event: self.open_guidance())
        ttk.Button(parent, text="فتح المرجع الرسمي المحدد", command=self.open_guidance).pack(anchor="e", pady=8)

    def _load_eos_employees(self):
        def render(result):
            rows, _total = result
            self.employee_map = {f"{row['emp_code']} — {row['name']}": row for row in rows}
            self.employee_combo.configure(values=["احتساب يدوي", *self.employee_map.keys()])
            self.eos_employee.set("احتساب يدوي")

        self.app.run_async(
            lambda: self.app.db.list_employees(page_size=200, sort_by="name", descending=False),
            render,
            busy_text="تحميل موظفي حاسبة نهاية الخدمة...",
        )

    def _select_eos_employee(self, _event=None):
        employee = self.employee_map.get(self.eos_employee.get())
        if not employee:
            return
        self.eos_wage.set(str(employee.get("salary") or ""))
        self.eos_start.set(str(employee.get("join_date") or ""))

    @staticmethod
    def _ratio(value: int, total: int) -> str:
        return f"{value}/{total}" if total else "0/0"

    def load(self):
        def render(snapshot):
            self.snapshot = snapshot
            target = snapshot.get("saudization_target")
            self.target_var.set("" if target is None else str(target))
            rate = snapshot["saudization_rate"]
            if target is None:
                saudization = f"{rate}%"
            else:
                mark = "✓" if snapshot["meets_saudization_target"] else "△"
                saudization = f"{rate}% {mark}"
            total = snapshot["active_employees"]
            self.metric_vars["saudization"].set(saudization)
            self.metric_vars["qiwa"].set(self._ratio(snapshot["qiwa_documented"], total))
            self.metric_vars["wps"].set(self._ratio(snapshot["wps_ready"], total))
            self.metric_vars["gosi"].set(self._ratio(snapshot["gosi_data_ready"], total))
            self.metric_vars["issues"].set(f"{snapshot['critical_issues']} / {snapshot['high_issues']}")
            self.issues_tree.delete(*self.issues_tree.get_children())
            for index, issue in enumerate(snapshot["issues"]):
                self.issues_tree.insert(
                    "",
                    "end",
                    iid=str(index),
                    values=(
                        issue["severity"],
                        issue["category"],
                        issue["emp_code"],
                        issue["employee"],
                        issue["details"],
                        issue["recommended_action"],
                    ),
                )
            self.issue_status.set(
                f"{len(snapshot['issues'])} بند متابعة — {total} موظف نشط — "
                f"{snapshot['saudi_employees']} سعودي — {snapshot['unknown_nationality']} دون جنسية مكتملة"
            )

        self.app.run_async(
            self.app.saudi.compliance_snapshot,
            render,
            busy_text="تحليل الجاهزية النظامية السعودية...",
        )

    def save_target(self):
        def done(_target):
            messagebox.showinfo("تم", "تم حفظ مستهدف السعودة الداخلي وإعادة احتساب المؤشر.", parent=self)
            self.load()

        self.app.run_async(
            lambda: self.app.saudi.set_saudization_target(self.target_var.get(), self.app.current_user),
            done,
            busy_text="حفظ مستهدف السعودة...",
        )

    def export(self):
        if not self.snapshot:
            messagebox.showwarning("تنبيه", "حدّث المؤشرات أولًا.", parent=self)
            return
        default = f"ProTech_Saudi_Compliance_{datetime.now():%Y%m%d_%H%M%S}.csv"
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="حفظ تقرير الامتثال السعودي",
            initialdir=str(EXPORT_DIR),
            initialfile=default,
            defaultextension=".csv",
            filetypes=[("CSV UTF-8", "*.csv")],
        )
        if not selected:
            return

        def done(path: Path):
            messagebox.showinfo("تم التصدير", f"تم حفظ التقرير في:\n{path}", parent=self)

        self.app.run_async(
            lambda: self.app.saudi.export_snapshot_csv(selected, self.snapshot, self.app.company_name),
            done,
            busy_text="تصدير تقرير الامتثال السعودي...",
        )

    def calculate_eos(self):
        try:
            result = calculate_end_of_service(
                self.eos_wage.get(),
                self.eos_start.get(),
                self.eos_end.get(),
                self.eos_reason.get(),
            )
        except Exception as exc:
            messagebox.showerror("تعذر الاحتساب", str(exc), parent=self)
            return
        factor = result["entitlement_factor"] * 100
        self.eos_result.set(
            f"مدة الخدمة: {result['years']} سنة، {result['months']} شهر، {result['days']} يوم\n\n"
            f"الأجر الأخير: {result['last_wage']:,.2f} ريال\n"
            f"المكافأة الأساسية قبل أثر سبب الانتهاء: {result['base_award']:,.2f} ريال\n"
            f"نسبة الاستحقاق المطبقة: {factor.quantize(factor.__class__('0.01'))}%\n\n"
            f"المكافأة التقديرية: {result['final_award']:,.2f} ريال"
        )

    def open_guidance(self):
        selected = self.guidance_tree.selection()
        if not selected:
            messagebox.showwarning("تنبيه", "اختر مرجعًا رسميًا أولًا.", parent=self)
            return
        item = self.guidance[int(selected[0])]
        webbrowser.open(item["url"], new=2)
