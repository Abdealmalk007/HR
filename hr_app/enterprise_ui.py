from __future__ import annotations

import os
from datetime import date, datetime
from html import escape
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
import webbrowser

from .calendar_picker import DatePicker
from .config import (
    CONTRACT_STATUSES, CONTRACT_TYPES, DOCUMENT_TYPES, EXPORT_DIR,
    QIWA_CONTRACT_STATUSES,
)
from .excel_export import export_xlsx
from .company import company_details
from .modern_ui import build_page_header, page_accent
from .identity_rules import identity_type_for_nationality

FONT_BOLD = ("Segoe UI", 10, "bold")
TITLE_FONT = ("Segoe UI", 18, "bold")


def open_path(path: Path | str) -> None:
    path = Path(path)
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.resolve().as_uri())


class EnterprisePage(ttk.Frame):
    def __init__(self, master, app, title: str):
        super().__init__(master, style="Page.TFrame")
        self.app = app
        self.accent = page_accent(self.__class__.__name__)
        build_page_header(self, title, self.accent, "إدارة مؤسسية مترابطة وآمنة")

    def employees(self, active_only=False):
        return self.app.db.list_employee_choices(active_only=active_only)


class DocumentsContractsPage(EnterprisePage):
    def __init__(self, master, app):
        super().__init__(master, app, "العقود والمستندات ونسخ الهوية والإقامة والجواز")
        self.employee_map = {}
        selector = ttk.Frame(self)
        selector.pack(fill="x", pady=4)
        ttk.Label(selector, text="الموظف:").pack(side="right", padx=4)
        self.employee_var = tk.StringVar(value="جميع الموظفين")
        self.employee_combo = ttk.Combobox(selector, textvariable=self.employee_var, state="readonly", width=48)
        self.employee_combo.pack(side="right", padx=4)
        self.employee_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_all())
        ttk.Button(selector, text="تحديث", command=self.load_all).pack(side="left", padx=4)
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(selector, text="حفظ الهوية / الإقامة / الجواز", command=self.add_identity).pack(side="left", padx=4)
            ttk.Button(selector, text="إضافة مستند", command=self.add_document).pack(side="left", padx=4)
            ttk.Button(selector, text="إضافة عقد", command=self.add_contract).pack(side="left", padx=4)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, pady=6)
        docs_tab = ttk.Frame(notebook, padding=6)
        contracts_tab = ttk.Frame(notebook, padding=6)
        notebook.add(docs_tab, text="المستندات")
        notebook.add(contracts_tab, text="العقود")

        self.docs_tree = ttk.Treeview(
            docs_tab, columns=("employee", "type", "file", "start", "expiry", "created"), show="headings"
        )
        for key, label, width in (
            ("employee", "الموظف", 220), ("type", "نوع المستند", 130),
            ("file", "اسم الملف الأصلي", 240), ("start", "تاريخ البداية", 110),
            ("expiry", "تاريخ الانتهاء", 110),
            ("created", "تاريخ الإضافة", 160),
        ):
            self.docs_tree.heading(key, text=label)
            self.docs_tree.column(key, width=width, anchor="center")
        self.docs_tree.pack(fill="both", expand=True)
        docs_buttons = ttk.Frame(docs_tab)
        docs_buttons.pack(fill="x", pady=5)
        if app.current_user["role"] in {"admin", "hr"}:
            self.docs_tree.bind("<Double-1>", lambda _e: self.open_document())
            ttk.Button(docs_buttons, text="فتح المستند", command=self.open_document).pack(side="right", padx=4)
            ttk.Button(docs_buttons, text="استعراض الهوية / الإقامة / الجواز", command=self.open_latest_identity).pack(side="right", padx=4)
        else:
            ttk.Label(docs_buttons, text="فتح الملفات الحساسة غير متاح لمستخدم القراءة فقط.", foreground="#666").pack(side="right")
        if app.current_user["role"] == "admin":
            ttk.Button(docs_buttons, text="حذف المستند", command=self.delete_document).pack(side="right", padx=4)

        self.contracts_tree = ttk.Treeview(
            contracts_tab,
            columns=("employee", "number", "type", "start", "end", "salary", "status", "qiwa"), show="headings"
        )
        for key, label, width in (
            ("employee", "الموظف", 220), ("number", "رقم العقد", 110), ("type", "النوع", 130),
            ("start", "البداية", 100), ("end", "النهاية", 100), ("salary", "راتب العقد", 110),
            ("status", "الحالة", 90), ("qiwa", "حالة قوى", 135),
        ):
            self.contracts_tree.heading(key, text=label)
            self.contracts_tree.column(key, width=width, anchor="center")
        self.contracts_tree.pack(fill="both", expand=True)
        contract_buttons = ttk.Frame(contracts_tab)
        contract_buttons.pack(fill="x", pady=5)
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(
                contract_buttons, text="تحديث حالة التوثيق في قوى", command=self.update_qiwa_status
            ).pack(side="right", padx=4)
        if app.current_user["role"] == "admin":
            ttk.Button(
                contract_buttons, text="حذف العقد المحدد", command=self.delete_contract
            ).pack(side="right", padx=4)
        self.load_employees()

    def load_employees(self):
        def render(rows):
            self.employee_map = {f"{r['emp_code']} — {r['name']}": r for r in rows}
            self.employee_combo.configure(values=["جميع الموظفين", *self.employee_map.keys()])
            self.employee_var.set("جميع الموظفين")
            self.load_all()
        self.app.run_async(lambda: self.employees(False), render, busy_text="تحميل قائمة الموظفين...")

    def selected_employee_id(self, required=False):
        row = self.employee_map.get(self.employee_var.get())
        if required and not row:
            messagebox.showwarning("تنبيه", "اختر موظفًا محددًا أولًا.", parent=self)
            return None
        return row["id"] if row else None

    def load_all(self):
        employee_id = self.selected_employee_id(False)
        def work():
            return (self.app.enterprise.list_documents(employee_id), self.app.enterprise.list_contracts(employee_id))
        def render(result):
            docs, contracts = result
            self.docs = {str(r["id"]): r for r in docs}
            self.docs_tree.delete(*self.docs_tree.get_children())
            for row in docs:
                self.docs_tree.insert("", "end", iid=str(row["id"]), values=(
                    f"{row['emp_code']} — {row['name']}", row["document_type"], row["original_name"],
                    row.get("start_date") or "—", row.get("expiry_date") or "—", row["created_at"],
                ))
            self.contracts = {str(r["id"]): r for r in contracts}
            self.contracts_tree.delete(*self.contracts_tree.get_children())
            for row in contracts:
                self.contracts_tree.insert("", "end", iid=str(row["id"]), values=(
                    f"{row['emp_code']} — {row['name']}", row.get("contract_number") or "—",
                    row["contract_type"], row["start_date"], row.get("end_date") or "—",
                    f"{row['contract_salary']:,.2f}", row["status"], row.get("qiwa_status") or "غير موثق",
                ))
        self.app.run_async(work, render, busy_text="تحميل العقود والمستندات...")

    def add_identity(self):
        employee_id = self.selected_employee_id(True)
        if not employee_id:
            return
        employee = self.employee_map.get(self.employee_var.get(), {})
        document_type = str(employee.get("id_type") or "").strip()
        if not document_type:
            messagebox.showwarning(
                "نوع الوثيقة غير مسجل",
                "افتح ملف الموظف وحدد نوع الهوية أو الإقامة أو جواز السفر أولًا.",
                parent=self,
            )
            return
        DocumentDialog(
            self, self.app, employee_id, on_saved=self.load_all,
            preset_document_type=document_type,
        )

    def open_latest_identity(self):
        employee_id = self.selected_employee_id(True)
        if not employee_id:
            return
        try:
            document = self.app.enterprise.latest_identity_document(employee_id)
            if not document:
                messagebox.showinfo("نسخة الوثيقة", "لا توجد نسخة هوية أو إقامة أو جواز محفوظة لهذا الموظف.", parent=self)
                return
            open_path(self.app.enterprise.document_path(document["id"], self.app.current_user))
        except Exception as exc:
            messagebox.showerror("تعذر استعراض الهوية", str(exc), parent=self)

    def add_document(self):
        employee_id = self.selected_employee_id(True)
        if not employee_id:
            return
        DocumentDialog(self, self.app, employee_id, on_saved=self.load_all)

    def add_contract(self):
        employee_id = self.selected_employee_id(True)
        if not employee_id:
            return
        ContractDialog(self, self.app, employee_id, on_saved=self.load_all)

    def open_document(self):
        selected = self.docs_tree.selection()
        if not selected:
            messagebox.showwarning("تنبيه", "اختر مستندًا.", parent=self)
            return
        try:
            open_path(self.app.enterprise.document_path(int(selected[0]), self.app.current_user))
        except Exception as exc:
            messagebox.showerror("خطأ", str(exc), parent=self)

    def delete_document(self):
        selected = self.docs_tree.selection()
        if not selected:
            return
        if not messagebox.askyesno("تأكيد", "هل تريد حذف المستند وملفه المحفوظ؟", parent=self):
            return
        self.app.run_async(
            lambda: self.app.enterprise.delete_document(int(selected[0]), self.app.current_user),
            lambda _r: self.load_all(), busy_text="حذف المستند...",
        )

    def delete_contract(self):
        selected = self.contracts_tree.selection()
        if not selected:
            return
        if not messagebox.askyesno("تأكيد", "هل تريد حذف العقد المحدد؟", parent=self):
            return
        self.app.run_async(
            lambda: self.app.enterprise.delete_contract(int(selected[0]), self.app.current_user),
            lambda _r: self.load_all(), busy_text="حذف العقد...",
        )

    def update_qiwa_status(self):
        selected = self.contracts_tree.selection()
        if not selected:
            messagebox.showwarning("تنبيه", "اختر عقدًا لتحديث حالة توثيقه.", parent=self)
            return
        contract = self.contracts.get(selected[0])
        if contract:
            QiwaStatusDialog(self, self.app, contract, on_saved=self.load_all)


class DocumentDialog(tk.Toplevel):
    def __init__(self, master, app, employee_id, on_saved=None, preset_document_type: str | None = None):
        super().__init__(master)
        self.app, self.employee_id, self.on_saved = app, employee_id, on_saved
        self.title("إضافة مستند موظف")
        self.transient(master); self.grab_set()
        frame = ttk.Frame(self, padding=16); frame.pack(fill="both", expand=True)
        self.path_var = tk.StringVar(); self.type_var = tk.StringVar(value=preset_document_type or DOCUMENT_TYPES[0])
        self.start_var = tk.StringVar(); self.expiry_var = tk.StringVar()
        ttk.Label(frame, text="نوع المستند").grid(row=0, column=1, sticky="e", padx=5, pady=6)
        type_combo = ttk.Combobox(frame, textvariable=self.type_var, values=DOCUMENT_TYPES, state="readonly", width=28)
        type_combo.grid(row=0, column=0, sticky="ew")
        if preset_document_type:
            type_combo.configure(state="disabled")
        ttk.Label(frame, text="الملف").grid(row=1, column=1, sticky="e", padx=5, pady=6)
        holder = ttk.Frame(frame); holder.grid(row=1, column=0, sticky="ew")
        ttk.Entry(holder, textvariable=self.path_var, width=38, state="readonly").pack(side="right", fill="x", expand=True)
        ttk.Button(holder, text="اختيار", command=self.choose).pack(side="left", padx=4)
        ttk.Label(frame, text="تاريخ البداية").grid(row=2, column=1, sticky="e", padx=5, pady=6)
        start_holder = ttk.Frame(frame); start_holder.grid(row=2, column=0, sticky="ew")
        ttk.Entry(start_holder, textvariable=self.start_var).pack(side="right", fill="x", expand=True)
        ttk.Button(start_holder, text="📅", width=3, command=lambda: DatePicker(self, self.start_var.get(), self.start_var.set)).pack(side="left", padx=4)
        ttk.Label(frame, text="تاريخ الانتهاء").grid(row=3, column=1, sticky="e", padx=5, pady=6)
        holder2 = ttk.Frame(frame); holder2.grid(row=3, column=0, sticky="ew")
        entry = ttk.Entry(holder2, textvariable=self.expiry_var); entry.pack(side="right", fill="x", expand=True)
        ttk.Button(holder2, text="📅", width=3, command=lambda: DatePicker(self, self.expiry_var.get(), self.expiry_var.set)).pack(side="left", padx=4)
        ttk.Label(frame, text="ملاحظات").grid(row=4, column=1, sticky="ne", padx=5, pady=6)
        self.notes = tk.Text(frame, width=40, height=5); self.notes.grid(row=4, column=0)
        ttk.Button(frame, text="حفظ", command=self.save).grid(row=5, column=0, columnspan=2, pady=12)

    def choose(self):
        path = filedialog.askopenfilename(parent=self, filetypes=[("الصور وPDF", "*.jpg *.jpeg *.png *.webp *.pdf")])
        if path: self.path_var.set(path)

    def save(self):
        if not self.path_var.get():
            messagebox.showwarning("تنبيه", "اختر ملفًا.", parent=self); return
        op = lambda: self.app.enterprise.add_document(
            self.employee_id, self.path_var.get(), self.type_var.get(), self.app.current_user,
            expiry_date=self.expiry_var.get(),
            notes=self.notes.get("1.0", "end").strip(),
            start_date=self.start_var.get(),
        )
        def done(_):
            if self.on_saved: self.on_saved()
            self.destroy()
        self.app.run_async(op, done, busy_text="حفظ المستند...")


class EmployeeDocumentsDialog(tk.Toplevel):
    def __init__(self, master, app, employee: dict):
        super().__init__(master)
        self.app = app
        self.employee = employee
        self.title(f"وثائق الموظف - {employee.get('name') or ''}")
        self.geometry("920x520")
        self.minsize(760, 420)
        self.transient(master)
        self.grab_set()
        frame = ttk.Frame(self, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"{employee.get('emp_code') or ''} — {employee.get('name') or ''}",
            font=TITLE_FONT,
        ).pack(anchor="e", pady=(0, 10))
        columns = ("type", "file", "start", "end", "created")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings")
        for key, label, width in (
            ("type", "نوع المستند", 140), ("file", "اسم الملف", 260),
            ("start", "تاريخ البداية", 115), ("end", "تاريخ الانتهاء", 115),
            ("created", "تاريخ الإضافة", 160),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(buttons, text="إضافة مستند", command=self.add).pack(side="right", padx=4)
            ttk.Button(buttons, text="فتح المحدد", command=self.open_selected).pack(side="right", padx=4)
            self.tree.bind("<Double-1>", lambda _event: self.open_selected())
        if app.current_user["role"] == "admin":
            ttk.Button(buttons, text="حذف المحدد", command=self.delete_selected).pack(side="right", padx=4)
        ttk.Button(buttons, text="إغلاق", command=self.destroy).pack(side="left", padx=4)
        self.load()

    def load(self):
        def render(rows):
            self.rows = {str(row["id"]): row for row in rows}
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                self.tree.insert("", "end", iid=str(row["id"]), values=(
                    row["document_type"], row["original_name"], row.get("start_date") or "—",
                    row.get("expiry_date") or "—", row["created_at"],
                ))
        self.app.run_async(
            lambda: self.app.enterprise.list_documents(int(self.employee["id"])),
            render,
            busy_text="تحميل وثائق الموظف...",
        )

    def add(self):
        DocumentDialog(self, self.app, int(self.employee["id"]), on_saved=self.load)

    def open_selected(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showwarning("تنبيه", "اختر مستندًا أولًا.", parent=self)
            return
        try:
            open_path(self.app.enterprise.document_path(int(selected[0]), self.app.current_user))
        except Exception as exc:
            messagebox.showerror("تعذر فتح المستند", str(exc), parent=self)

    def delete_selected(self):
        selected = self.tree.selection()
        if not selected:
            return
        if not messagebox.askyesno("تأكيد", "هل تريد حذف المستند المحدد؟", parent=self):
            return
        self.app.run_async(
            lambda: self.app.enterprise.delete_document(int(selected[0]), self.app.current_user),
            lambda _result: self.load(),
            busy_text="حذف المستند...",
        )


class ContractDialog(tk.Toplevel):
    def __init__(self, master, app, employee_id, on_saved=None):
        super().__init__(master)
        self.app, self.employee_id, self.on_saved = app, employee_id, on_saved
        self.title("إضافة عقد")
        self.transient(master); self.grab_set()
        frame = ttk.Frame(self, padding=16); frame.pack(fill="both", expand=True)
        specs = [("contract_number", "رقم العقد"), ("contract_type", "نوع العقد"), ("start_date", "تاريخ البداية"),
                 ("end_date", "تاريخ النهاية"), ("probation_end_date", "نهاية التجربة"),
                 ("contract_salary", "راتب العقد"), ("status", "الحالة"),
                 ("qiwa_status", "حالة التوثيق في قوى"),
                 ("qiwa_contract_number", "رقم عقد قوى"),
                 ("qiwa_documented_at", "تاريخ توثيق قوى")]
        self.vars = {key: tk.StringVar() for key, _ in specs}
        self.vars["contract_type"].set(CONTRACT_TYPES[0]); self.vars["status"].set(CONTRACT_STATUSES[0])
        self.vars["qiwa_status"].set(QIWA_CONTRACT_STATUSES[0])
        self.vars["start_date"].set(date.today().isoformat())
        for row, (key, label) in enumerate(specs):
            ttk.Label(frame, text=label).grid(row=row, column=1, sticky="e", padx=5, pady=5)
            holder = ttk.Frame(frame); holder.grid(row=row, column=0, sticky="ew")
            if key == "contract_type":
                ttk.Combobox(holder, textvariable=self.vars[key], values=CONTRACT_TYPES, state="readonly", width=28).pack(fill="x")
            elif key == "status":
                ttk.Combobox(holder, textvariable=self.vars[key], values=CONTRACT_STATUSES, state="readonly", width=28).pack(fill="x")
            elif key == "qiwa_status":
                ttk.Combobox(holder, textvariable=self.vars[key], values=QIWA_CONTRACT_STATUSES, state="readonly", width=28).pack(fill="x")
            else:
                ttk.Entry(holder, textvariable=self.vars[key], width=32).pack(side="right", fill="x", expand=True)
                if key.endswith("date") or key in {"qiwa_documented_at"}:
                    ttk.Button(holder, text="📅", width=3, command=lambda k=key: DatePicker(self, self.vars[k].get(), self.vars[k].set)).pack(side="left", padx=4)
        ttk.Label(frame, text="ملاحظات").grid(row=len(specs), column=1, sticky="ne", padx=5, pady=5)
        self.notes = tk.Text(frame, width=38, height=5); self.notes.grid(row=len(specs), column=0)
        ttk.Button(frame, text="حفظ العقد", command=self.save).grid(row=len(specs)+1, column=0, columnspan=2, pady=12)

    def save(self):
        data = {k: v.get().strip() for k, v in self.vars.items()}; data["notes"] = self.notes.get("1.0", "end").strip()
        op = lambda: self.app.enterprise.add_contract(self.employee_id, data, self.app.current_user)
        def done(_):
            if self.on_saved: self.on_saved()
            self.destroy()
        self.app.run_async(op, done, busy_text="حفظ العقد...")


class QiwaStatusDialog(tk.Toplevel):
    def __init__(self, master, app, contract, on_saved=None):
        super().__init__(master)
        self.app, self.contract, self.on_saved = app, contract, on_saved
        self.title("تحديث حالة توثيق العقد في قوى")
        self.transient(master)
        self.grab_set()
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"{contract['emp_code']} — {contract['name']} — العقد {contract.get('contract_number') or contract['id']}",
            font=FONT_BOLD,
        ).grid(row=0, column=0, columnspan=2, pady=(0, 12))
        self.status = tk.StringVar(value=contract.get("qiwa_status") or "غير موثق")
        self.number = tk.StringVar(value=contract.get("qiwa_contract_number") or "")
        self.documented_at = tk.StringVar(value=contract.get("qiwa_documented_at") or "")
        for row, (label, variable, kind) in enumerate(
            (
                ("الحالة بعد التحقق من قوى", self.status, "status"),
                ("رقم عقد قوى", self.number, "entry"),
                ("تاريخ التوثيق", self.documented_at, "date"),
            ),
            start=1,
        ):
            ttk.Label(frame, text=label).grid(row=row, column=1, sticky="e", padx=6, pady=6)
            holder = ttk.Frame(frame)
            holder.grid(row=row, column=0, sticky="ew", pady=6)
            if kind == "status":
                ttk.Combobox(holder, textvariable=variable, values=QIWA_CONTRACT_STATUSES, state="readonly", width=34).pack(fill="x")
            else:
                ttk.Entry(holder, textvariable=variable, width=38).pack(side="right", fill="x", expand=True)
                if kind == "date":
                    ttk.Button(holder, text="📅", width=3, command=lambda: DatePicker(self, variable.get(), variable.set)).pack(side="left", padx=4)
        ttk.Label(
            frame,
            text="حدّث هذه الحالة بعد التحقق من منصة قوى؛ لا يجري النظام اتصالًا آليًا بالمنصة.",
            foreground="#64748B",
            wraplength=430,
            justify="right",
        ).grid(row=4, column=0, columnspan=2, pady=(8, 2))
        ttk.Button(frame, text="حفظ حالة قوى", command=self.save).grid(row=5, column=0, columnspan=2, pady=12, ipady=4)

    def save(self):
        def done(_):
            if self.on_saved:
                self.on_saved()
            self.destroy()

        self.app.run_async(
            lambda: self.app.enterprise.update_contract_compliance(
                int(self.contract["id"]),
                self.status.get(),
                self.number.get(),
                self.documented_at.get(),
                self.app.current_user,
            ),
            done,
            busy_text="حفظ حالة عقد قوى...",
        )


class AllowanceAssignmentDialog(tk.Toplevel):
    def __init__(self, parent, app, on_saved, record=None, employee_id: int | None = None):
        super().__init__(parent)
        self.app = app
        self.on_saved = on_saved
        self.record = record or {}
        self.title("إضافة / تعديل بدل موظف")
        self.geometry("620x460")
        self.minsize(560, 420)
        self.transient(parent)
        self.grab_set()
        self.columnconfigure(1, weight=1)

        employees = self.app.db.list_employee_choices(active_only=False)
        self.employee_map = {f"{e['emp_code']} — {e['name']}": e for e in employees}
        employee_values = list(self.employee_map)
        types = self.app.enterprise.list_allowance_types(active_only=True)
        self.type_map = {t["name"]: t for t in types}

        current_employee = ""
        if self.record:
            current_employee = f"{self.record.get('emp_code', '')} — {self.record.get('employee_name', '')}"
        elif employee_id is not None:
            for label, employee in self.employee_map.items():
                if int(employee["id"]) == int(employee_id):
                    current_employee = label
                    break
        self.employee_var = tk.StringVar(value=current_employee or (employee_values[0] if employee_values else ""))
        self.type_var = tk.StringVar(value=self.record.get("allowance_name") or (types[0]["name"] if types else ""))
        self.method_var = tk.StringVar(
            value="نسبة من الراتب الأساسي" if self.record.get("calculation_method") == "percentage" else "مبلغ ثابت"
        )
        self.value_var = tk.StringVar(value=str(self.record.get("value", "")))
        self.from_var = tk.StringVar(value=self.record.get("effective_from") or date.today().isoformat())
        self.to_var = tk.StringVar(value=self.record.get("effective_to") or "")

        fields = ttk.Frame(self, padding=18)
        fields.grid(row=0, column=0, columnspan=2, sticky="nsew")
        fields.columnconfigure(1, weight=1)
        ttk.Label(fields, text="الموظف:").grid(row=0, column=0, sticky="e", padx=6, pady=7)
        ttk.Combobox(fields, textvariable=self.employee_var, values=employee_values,
                     state="readonly", width=42).grid(row=0, column=1, sticky="ew", padx=6, pady=7)
        ttk.Label(fields, text="نوع البدل:").grid(row=1, column=0, sticky="e", padx=6, pady=7)
        ttk.Combobox(fields, textvariable=self.type_var, values=list(self.type_map),
                     state="readonly", width=42).grid(row=1, column=1, sticky="ew", padx=6, pady=7)
        ttk.Label(fields, text="طريقة الاحتساب:").grid(row=2, column=0, sticky="e", padx=6, pady=7)
        method_combo = ttk.Combobox(fields, textvariable=self.method_var,
                                    values=["مبلغ ثابت", "نسبة من الراتب الأساسي"], state="readonly")
        method_combo.grid(row=2, column=1, sticky="ew", padx=6, pady=7)
        ttk.Label(fields, text="القيمة:").grid(row=3, column=0, sticky="e", padx=6, pady=7)
        ttk.Entry(fields, textvariable=self.value_var).grid(row=3, column=1, sticky="ew", padx=6, pady=7)
        ttk.Label(fields, text="تاريخ البداية:").grid(row=4, column=0, sticky="e", padx=6, pady=7)
        start_row = ttk.Frame(fields); start_row.grid(row=4, column=1, sticky="ew", padx=6, pady=7)
        ttk.Entry(start_row, textvariable=self.from_var).pack(side="right", fill="x", expand=True)
        ttk.Button(start_row, text="📅", width=4,
                   command=lambda: DatePicker(self, self.from_var.get(), self.from_var.set)).pack(side="left", padx=(5, 0))
        ttk.Label(fields, text="تاريخ النهاية (اختياري):").grid(row=5, column=0, sticky="e", padx=6, pady=7)
        end_row = ttk.Frame(fields); end_row.grid(row=5, column=1, sticky="ew", padx=6, pady=7)
        ttk.Entry(end_row, textvariable=self.to_var).pack(side="right", fill="x", expand=True)
        ttk.Button(end_row, text="📅", width=4,
                   command=lambda: DatePicker(self, self.to_var.get(), self.to_var.set)).pack(side="left", padx=(5, 0))
        ttk.Button(end_row, text="مسح", width=6, command=lambda: self.to_var.set("")).pack(side="left", padx=4)
        ttk.Label(fields, text="ملاحظات:").grid(row=6, column=0, sticky="ne", padx=6, pady=7)
        self.notes = tk.Text(fields, height=4, wrap="word")
        self.notes.grid(row=6, column=1, sticky="nsew", padx=6, pady=7)
        self.notes.insert("1.0", self.record.get("notes") or "")
        fields.rowconfigure(6, weight=1)

        hint = ttk.Label(
            self,
            text="المبلغ الثابت يضاف شهريًا كما هو، والنسبة تُحسب من الراتب الأساسي عند إنشاء المسير.",
            foreground="#5f6b7a",
        )
        hint.grid(row=1, column=0, columnspan=2, pady=(0, 8))
        actions = ttk.Frame(self, padding=(18, 5, 18, 16))
        actions.grid(row=2, column=0, columnspan=2, sticky="ew")
        ttk.Button(actions, text="حفظ البدل", command=self.save).pack(side="right", padx=5)
        ttk.Button(actions, text="إلغاء", command=self.destroy).pack(side="right", padx=5)

    def save(self):
        employee = self.employee_map.get(self.employee_var.get())
        allowance_type = self.type_map.get(self.type_var.get())
        if not employee or not allowance_type:
            messagebox.showwarning("تنبيه", "اختر الموظف ونوع البدل.", parent=self)
            return
        try:
            value = float(self.value_var.get().replace(",", "").strip())
        except ValueError:
            messagebox.showerror("خطأ", "قيمة البدل يجب أن تكون رقمًا.", parent=self)
            return
        method = "percentage" if self.method_var.get().startswith("نسبة") else "fixed"
        args = (
            int(employee["id"]), int(allowance_type["id"]), method, value,
            self.from_var.get(), self.to_var.get() or None,
            self.notes.get("1.0", "end").strip(), self.app.current_user,
            int(self.record["id"]) if self.record else None,
        )
        def done(_result):
            self.destroy()
            self.on_saved()
        self.app.run_async(
            lambda: self.app.enterprise.save_employee_allowance(*args),
            done,
            busy_text="حفظ بدل الموظف...",
        )


class AllowancesDialog(tk.Toplevel):
    def __init__(self, parent, app, on_changed=None, employee_id: int | None = None):
        super().__init__(parent)
        self.app = app
        self.on_changed = on_changed
        self.initial_employee_id = employee_id
        self.title("إدارة بدلات الموظفين")
        self.geometry("1120x680")
        self.minsize(900, 560)
        self.transient(parent)
        self.employee_filter_map = {"جميع الموظفين": None}
        self.records = {}
        self.types = {}

        header = ttk.Frame(self, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="إدارة البدلات", font=TITLE_FONT).pack(side="right")
        ttk.Label(
            header,
            text="السكن، المواصلات، السفر، التذاكر وبدلات مخصصة — تُحتسب آليًا في مسير الرواتب",
            foreground="#596579",
        ).pack(side="right", padx=14)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        assignments = ttk.Frame(notebook, padding=8)
        type_tab = ttk.Frame(notebook, padding=8)
        notebook.add(assignments, text="بدلات الموظفين")
        notebook.add(type_tab, text="أنواع البدلات")

        controls = ttk.Frame(assignments); controls.pack(fill="x", pady=(0, 7))
        ttk.Label(controls, text="الموظف:").pack(side="right", padx=4)
        self.employee_var = tk.StringVar(value="جميع الموظفين")
        self.employee_combo = ttk.Combobox(controls, textvariable=self.employee_var, state="readonly", width=36)
        self.employee_combo.pack(side="right", padx=4)
        self.employee_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_assignments())
        ttk.Button(controls, text="إضافة بدل", command=self.add_assignment).pack(side="right", padx=4)
        ttk.Button(controls, text="تعديل", command=self.edit_assignment).pack(side="right", padx=4)
        ttk.Button(controls, text="تفعيل / إيقاف", command=self.toggle_assignment).pack(side="right", padx=4)
        ttk.Button(controls, text="تحديث", command=self.load_all).pack(side="left", padx=4)

        cols = ("employee", "type", "method", "value", "amount", "from", "to", "status", "notes")
        self.tree = ttk.Treeview(assignments, columns=cols, show="headings")
        for key, label, width in (
            ("employee", "الموظف", 210), ("type", "نوع البدل", 145),
            ("method", "الاحتساب", 120), ("value", "القيمة", 90),
            ("amount", "القيمة الحالية", 115), ("from", "من", 95),
            ("to", "إلى", 95), ("status", "الحالة", 75), ("notes", "ملاحظات", 180),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<Double-1>", lambda _e: self.edit_assignment())

        type_controls = ttk.Frame(type_tab); type_controls.pack(fill="x", pady=(0, 7))
        ttk.Button(type_controls, text="نوع بدل جديد", command=self.add_type).pack(side="right", padx=4)
        ttk.Button(type_controls, text="إعادة تسمية", command=self.edit_type).pack(side="right", padx=4)
        ttk.Button(type_controls, text="تفعيل / إيقاف", command=self.toggle_type).pack(side="right", padx=4)
        self.types_tree = ttk.Treeview(type_tab, columns=("name", "code", "status"), show="headings")
        for key, label, width in (("name", "اسم البدل", 260), ("code", "الرمز الداخلي", 220), ("status", "الحالة", 100)):
            self.types_tree.heading(key, text=label)
            self.types_tree.column(key, width=width, anchor="center")
        self.types_tree.pack(fill="both", expand=True)
        self.load_all()

    def load_all(self):
        employees = self.app.db.list_employee_choices(active_only=False)
        self.employee_filter_map = {"جميع الموظفين": None}
        self.employee_filter_map.update({f"{e['emp_code']} — {e['name']}": e for e in employees})
        self.employee_combo.configure(values=list(self.employee_filter_map))
        if self.initial_employee_id is not None:
            for label, employee in self.employee_filter_map.items():
                if employee and int(employee["id"]) == int(self.initial_employee_id):
                    self.employee_var.set(label)
                    break
            self.initial_employee_id = None
        elif self.employee_var.get() not in self.employee_filter_map:
            self.employee_var.set("جميع الموظفين")
        self.load_assignments()
        self.load_types()

    def load_assignments(self):
        employee = self.employee_filter_map.get(self.employee_var.get())
        employee_id = int(employee["id"]) if employee else None
        def render(rows):
            self.records = {str(r["id"]): r for r in rows}
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                method = "نسبة" if row["calculation_method"] == "percentage" else "ثابت"
                value = f"{row['value']:,.2f}%" if row["calculation_method"] == "percentage" else f"{row['value']:,.2f}"
                self.tree.insert(
                    "", "end", iid=str(row["id"]),
                    values=(f"{row['emp_code']} — {row['employee_name']}", row["allowance_name"],
                            method, value, f"{row['current_amount']:,.2f}", row["effective_from"],
                            row.get("effective_to") or "مستمر", "نشط" if row["is_active"] else "متوقف",
                            row.get("notes") or ""),
                )
        self.app.run_async(
            lambda: self.app.enterprise.list_employee_allowances(employee_id, include_inactive=True),
            render,
            busy_text="تحميل بدلات الموظفين...",
        )

    def load_types(self):
        def render(rows):
            self.types = {str(r["id"]): r for r in rows}
            self.types_tree.delete(*self.types_tree.get_children())
            for row in rows:
                self.types_tree.insert("", "end", iid=str(row["id"]),
                                       values=(row["name"], row["code"], "نشط" if row["is_active"] else "متوقف"))
        self.app.run_async(self.app.enterprise.list_allowance_types, render, busy_text="تحميل أنواع البدلات...")

    def selected_record(self):
        selected = self.tree.selection()
        return self.records.get(selected[0]) if selected else None

    def selected_type(self):
        selected = self.types_tree.selection()
        return self.types.get(selected[0]) if selected else None

    def add_assignment(self):
        employee = self.employee_filter_map.get(self.employee_var.get())
        employee_id = int(employee["id"]) if employee else None
        AllowanceAssignmentDialog(self, self.app, self._changed, employee_id=employee_id)

    def edit_assignment(self):
        row = self.selected_record()
        if not row:
            messagebox.showwarning("تنبيه", "اختر بدلًا لتعديله.", parent=self)
            return
        AllowanceAssignmentDialog(self, self.app, self._changed, row)

    def toggle_assignment(self):
        row = self.selected_record()
        if not row:
            messagebox.showwarning("تنبيه", "اختر بدلًا أولًا.", parent=self)
            return
        target = not bool(row["is_active"])
        self.app.run_async(
            lambda: self.app.enterprise.set_employee_allowance_active(int(row["id"]), target, self.app.current_user),
            lambda _r: self._changed(),
            busy_text="تحديث حالة البدل...",
        )

    def add_type(self):
        name = simpledialog.askstring("نوع بدل جديد", "اسم البدل:", parent=self)
        if not name:
            return
        self.app.run_async(
            lambda: self.app.enterprise.save_allowance_type(name, self.app.current_user),
            lambda _r: self._changed(),
            busy_text="إضافة نوع البدل...",
        )

    def edit_type(self):
        row = self.selected_type()
        if not row:
            messagebox.showwarning("تنبيه", "اختر نوع بدل أولًا.", parent=self)
            return
        name = simpledialog.askstring("إعادة تسمية البدل", "اسم البدل:", initialvalue=row["name"], parent=self)
        if not name:
            return
        self.app.run_async(
            lambda: self.app.enterprise.save_allowance_type(name, self.app.current_user, int(row["id"])),
            lambda _r: self._changed(),
            busy_text="تحديث نوع البدل...",
        )

    def toggle_type(self):
        row = self.selected_type()
        if not row:
            messagebox.showwarning("تنبيه", "اختر نوع بدل أولًا.", parent=self)
            return
        target = not bool(row["is_active"])
        self.app.run_async(
            lambda: self.app.enterprise.set_allowance_type_active(int(row["id"]), target, self.app.current_user),
            lambda _r: self._changed(),
            busy_text="تحديث نوع البدل...",
        )

    def _changed(self):
        self.load_all()
        if self.on_changed:
            self.on_changed()


class PayrollPage(EnterprisePage):
    def __init__(self, master, app):
        super().__init__(master, app, "مسيرات الرواتب")
        top = ttk.Frame(self); top.pack(fill="x", pady=4)
        self.period_var = tk.StringVar(value=date.today().strftime("%Y-%m"))
        ttk.Label(top, text="الفترة YYYY-MM:").pack(side="right", padx=3)
        ttk.Entry(top, textvariable=self.period_var, width=10).pack(side="right", padx=3)
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(top, text="إنشاء/تحديث المسير", command=self.generate).pack(side="right", padx=5)
            ttk.Button(top, text="إدارة البدلات", command=self.manage_allowances).pack(side="right", padx=5)
            ttk.Button(top, text="طلب تعديل بند", command=self.edit_item).pack(side="left", padx=5)
        if app.current_user["role"] == "admin":
            ttk.Button(
                top,
                text="اعتماد/رفض الخصومات والتعديلات",
                command=self.approval_requests,
            ).pack(side="right", padx=5)
            ttk.Button(top, text="سياسة الحساب", command=self.payroll_policy).pack(side="right", padx=5)
            ttk.Button(top, text="اعتماد المسير", command=self.approve).pack(side="right", padx=5)
        ttk.Button(top, text="تصدير المسير", command=self.export).pack(side="left", padx=5)
        ttk.Button(top, text="قسيمة راتب", command=self.payslip).pack(side="left", padx=5)
        ttk.Button(top, text="تفاصيل البدلات", command=self.allowance_details).pack(side="left", padx=5)
        pane = ttk.Panedwindow(self, orient="vertical"); pane.pack(fill="both", expand=True)
        runs_frame = ttk.LabelFrame(pane, text="المسيرات", padding=5); items_frame = ttk.LabelFrame(pane, text="تفاصيل المسير", padding=5)
        pane.add(runs_frame, weight=1); pane.add(items_frame, weight=3)
        self.runs_tree = ttk.Treeview(runs_frame, columns=("period","status","pending","count","total","generated","approved"), show="headings", height=5)
        for key,label,width in (("period","الفترة",90),("status","الحالة",90),("pending","طلبات معلقة",95),("count","الموظفون",80),("total","صافي الإجمالي",130),("generated","تاريخ الإنشاء",155),("approved","تاريخ الاعتماد",155)):
            self.runs_tree.heading(key,text=label); self.runs_tree.column(key,width=width,anchor="center")
        self.runs_tree.pack(fill="both", expand=True); self.runs_tree.bind("<<TreeviewSelect>>", lambda _e: self.load_items())
        self.items_tree = ttk.Treeview(items_frame, columns=("employee","approval","base","allow","overtime","absence","late","deduct","net"), show="headings")
        for key,label,width in (("employee","الموظف",210),("approval","آخر قرار",85),("base","الأساسي",90),("allow","البدلات",85),("overtime","الإضافي",85),("absence","أيام الغياب",80),("late","التأخير",80),("deduct","الخصومات المطبقة",115),("net","الصافي",105)):
            self.items_tree.heading(key,text=label); self.items_tree.column(key,width=width,anchor="center")
        self.items_tree.pack(fill="both", expand=True)
        if app.current_user["role"] == "admin":
            self.pending_var = tk.StringVar(value="طلبات المدير المعلقة: جارٍ التحميل...")
            ttk.Label(
                self,
                textvariable=self.pending_var,
                foreground="#B45309",
                anchor="e",
            ).pack(fill="x", padx=8, pady=(3, 0), before=pane)
        self.load()

    def load(self):
        def render(rows):
            self.runs={str(r['id']):r for r in rows}; self.runs_tree.delete(*self.runs_tree.get_children())
            for r in rows:
                self.runs_tree.insert("","end",iid=str(r['id']),values=(r['period'],r['status'],r.get('pending_adjustments',0),r['employee_count'],f"{r['total_net']:,.2f}",r['generated_at'],r.get('approved_at') or '—'))
            if hasattr(self, "pending_var"):
                pending_total = sum(int(r.get("pending_adjustments") or 0) for r in rows)
                self.pending_var.set(
                    f"طلبات المدير المعلقة: {pending_total} — "
                    "اختر «اعتماد/رفض الخصومات والتعديلات» لاتخاذ القرار."
                )
            if rows:
                preferred = next(
                    (r for r in rows if int(r.get("pending_adjustments") or 0) > 0),
                    rows[0],
                )
                self.runs_tree.selection_set(str(preferred['id']))
                self.load_items()
        self.app.run_async(self.app.enterprise.list_payroll_runs, render, busy_text="تحميل مسيرات الرواتب...")

    def selected_run(self):
        s=self.runs_tree.selection(); return self.runs.get(s[0]) if s else None

    def load_items(self):
        run=self.selected_run()
        if not run: return
        def render(rows):
            self.items={str(r['id']):r for r in rows}; self.items_tree.delete(*self.items_tree.get_children())
            for r in rows:
                late_hours = float(r.get("late_minutes") or 0) / 60.0
                applied = (
                    float(r["manual_deductions"])
                    + float(r.get("absence_deduction") or 0)
                    + float(r.get("late_deduction") or 0)
                    + float(r.get("unpaid_leave_deduction") or 0)
                )
                approval = r.get("adjustment_status") or "—"
                self.items_tree.insert("","end",iid=str(r['id']),values=(f"{r['emp_code']} — {r['name']}",approval,f"{r['base_salary']:,.2f}",f"{r['allowances']:,.2f}",f"{r['overtime']:,.2f}",f"{float(r['absence_days']):g}",f"{late_hours:.2f} س",f"{applied:,.2f}",f"{r['net_salary']:,.2f}"))
        self.app.run_async(lambda:self.app.enterprise.list_payroll_items(run['id']),render,busy_text="تحميل تفاصيل الرواتب...")

    def manage_allowances(self):
        AllowancesDialog(self, self.app, on_changed=self.load)

    def allowance_details(self):
        selected = self.items_tree.selection()
        if not selected:
            messagebox.showwarning("تنبيه", "اختر موظفًا من تفاصيل المسير.", parent=self)
            return
        item = self.items[selected[0]]
        def render(rows):
            if not rows:
                messagebox.showinfo("تفاصيل البدلات", "لا توجد بدلات لهذا الموظف في المسير المحدد.", parent=self)
                return
            lines = []
            for row in rows:
                method = "نسبة من الأساسي" if row["calculation_method"] == "percentage" else "مبلغ ثابت"
                source = f"{row['source_value']:,.2f}%" if row["calculation_method"] == "percentage" else f"{row['source_value']:,.2f}"
                lines.append(f"• {row['allowance_name']}: {row['amount']:,.2f} ريال ({method}: {source})")
            lines.append(f"\nإجمالي البدلات: {item['allowances']:,.2f} ريال")
            messagebox.showinfo("تفاصيل البدلات", "\n".join(lines), parent=self)
        self.app.run_async(
            lambda: self.app.enterprise.payroll_allowance_breakdown(int(item["id"])),
            render,
            busy_text="تحميل تفاصيل البدلات...",
        )

    def generate(self):
        self.app.run_async(lambda:self.app.enterprise.generate_payroll(self.period_var.get(),self.app.current_user),lambda _r:self.load(),busy_text="إنشاء مسير الرواتب...")

    def edit_item(self):
        s=self.items_tree.selection()
        if not s: messagebox.showwarning("تنبيه","اختر موظفًا من المسير.",parent=self); return
        item=self.items[s[0]]
        PayrollItemDialog(self, self.app, item, on_saved=self.load)

    def approval_requests(self):
        run = self.selected_run()
        if not run:
            messagebox.showwarning("تنبيه", "اختر مسير رواتب أولًا.", parent=self)
            return
        if int(run.get("pending_adjustments") or 0) == 0:
            run = next(
                (
                    candidate for candidate in self.runs.values()
                    if int(candidate.get("pending_adjustments") or 0) > 0
                ),
                None,
            )
            if not run:
                messagebox.showinfo(
                    "اعتمادات الرواتب",
                    "لا توجد حاليًا خصومات أو تعديلات معلقة تنتظر قرار المدير.",
                    parent=self,
                )
                return
            self.runs_tree.selection_set(str(run["id"]))
        PayrollApprovalsDialog(self, self.app, int(run["id"]), on_changed=self.load)

    def payroll_policy(self):
        PayrollPolicyDialog(self, self.app, on_saved=self.load)

    def approve(self):
        run=self.selected_run()
        if not run:return
        if not messagebox.askyesno("اعتماد المسير","بعد الاعتماد لن يمكن تعديل المسير. متابعة؟",parent=self):return
        self.app.run_async(lambda:self.app.enterprise.approve_payroll(run['id'],self.app.current_user),lambda _r:self.load(),busy_text="اعتماد المسير...")

    def export(self):
        run=self.selected_run()
        if not run:return
        path=EXPORT_DIR/f"ProTech_Payroll_{run['period']}.xlsx"
        self.app.run_async(
            lambda: export_xlsx(
                path,
                {
                    "Payroll": self.app.enterprise.list_payroll_items(run["id"]),
                    "AllowanceDetails": self.app.enterprise.list_payroll_allowance_details(run["id"]),
                    "AdjustmentApprovals": self.app.enterprise.list_payroll_adjustments(run["id"], status=None),
                },
                company_profile=self.app.db.company_profile(),
            ),
            lambda p: (messagebox.showinfo("تم", f"تم إنشاء الملف:\n{p}", parent=self), open_path(Path(p).parent)),
            busy_text="تصدير المسير والبدلات...",
        )

    def payslip(self):
        selected = self.items_tree.selection()
        run = self.selected_run()
        if not selected or not run:
            messagebox.showwarning("تنبيه", "اختر موظفًا من تفاصيل المسير.", parent=self)
            return
        item = self.items[selected[0]]
        company = self.app.db.company_profile()
        company_name = escape(company["company_name"])
        company_information = escape(company_details(company))
        breakdown = self.app.enterprise.payroll_allowance_breakdown(int(item["id"]))
        allowance_rows = "".join(
            f"<tr><th>{escape(str(row['allowance_name']))}</th><td>{row['amount']:,.2f}</td></tr>"
            for row in breakdown
        )
        if not allowance_rows:
            allowance_rows = "<tr><th>البدلات</th><td>0.00</td></tr>"
        safe_code = item["emp_code"].replace("/", "_")
        path = EXPORT_DIR / f"ProTech_Payslip_{run['period']}_{safe_code}.html"
        html = f"""<!doctype html><html dir='rtl' lang='ar'><meta charset='utf-8'>
        <title>قسيمة راتب {escape(str(item['name']))}</title>
        <style>body{{font-family:Tahoma,Arial;margin:40px}}table{{border-collapse:collapse;width:100%;max-width:700px}}td,th{{border:1px solid #999;padding:10px;text-align:right}}h1,h2{{text-align:center}}</style>
        <h1>{company_name}</h1>{f'<p style="text-align:center">{company_information}</p>' if company_information else ''}<h2>قسيمة راتب — {escape(str(run['period']))}</h2>
        <table><tr><th>الموظف</th><td>{escape(str(item['emp_code']))} — {escape(str(item['name']))}</td></tr>
        <tr><th>القسم</th><td>{escape(str(item['department'] or '—'))}</td></tr>
        <tr><th>الراتب الأساسي</th><td>{item['base_salary']:,.2f}</td></tr>
        {allowance_rows}
        <tr><th>إجمالي البدلات</th><td><b>{item['allowances']:,.2f}</b></td></tr>
        <tr><th>الإضافي</th><td>{item['overtime']:,.2f}</td></tr>
        <tr><th>خصم الغياب المطبق</th><td>{item['absence_deduction']:,.2f}</td></tr>
        <tr><th>خصم ساعات التأخير المطبق</th><td>{float(item.get('late_deduction') or 0):,.2f}</td></tr>
        <tr><th>خصم الإجازة دون راتب المطبق</th><td>{float(item.get('unpaid_leave_deduction') or 0):,.2f}</td></tr>
        <tr><th>أيام الإجازة بدون راتب</th><td>{float(item.get('unpaid_leave_days') or 0):g}</td></tr>
        <tr><th>استقطاعات أخرى</th><td>{item['manual_deductions']:,.2f}</td></tr>
        <tr><th>صافي الراتب</th><td><b>{item['net_salary']:,.2f}</b></td></tr></table>
        <p>المطور: عبدالملك علي حمزة — PRO TECH</p></html>"""
        path.write_text(html, encoding="utf-8")
        open_path(path)


class PayrollApprovalsDialog(tk.Toplevel):
    def __init__(self, master, app, run_id: int, on_changed=None):
        super().__init__(master)
        self.app = app
        self.run_id = run_id
        self.on_changed = on_changed
        self.rows: dict[str, dict] = {}
        self.title("طلبات اعتماد تعديلات الرواتب")
        self.geometry("1120x560")
        self.transient(master)
        self.grab_set()
        ttk.Label(
            self,
            text="لا يدخل أي طلب في المسير أو لوحة المعلومات قبل اعتماد المدير.",
            foreground="#475569",
        ).pack(fill="x", padx=12, pady=(12, 4))
        self.tree = ttk.Treeview(
            self,
            columns=("employee", "requester", "current", "proposed", "delta", "requested"),
            show="headings",
        )
        for key, label, width in (
            ("employee", "الموظف", 220),
            ("requester", "مقدم الطلب", 150),
            ("current", "الصافي الحالي", 120),
            ("proposed", "الصافي المقترح", 120),
            ("delta", "الفرق", 110),
            ("requested", "وقت الطلب", 170),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True, padx=12, pady=8)
        self.tree.bind("<Double-1>", lambda _event: self.details())
        buttons = ttk.Frame(self, padding=10)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="اعتماد المحدد", command=self.approve).pack(side="right", padx=5)
        ttk.Button(buttons, text="رفض المحدد", command=self.reject).pack(side="right", padx=5)
        ttk.Button(buttons, text="عرض التفاصيل", command=self.details).pack(side="right", padx=5)
        ttk.Button(buttons, text="تحديث", command=self.load).pack(side="left", padx=5)
        ttk.Button(buttons, text="إغلاق", command=self.destroy).pack(side="left", padx=5)
        self.load()

    def selected(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def load(self):
        def render(rows):
            self.rows = {str(row["id"]): row for row in rows}
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                delta = float(row["proposed_net_salary"]) - float(row["current_net_salary"])
                self.tree.insert(
                    "", "end", iid=str(row["id"]),
                    values=(
                        f"{row['emp_code']} — {row['employee_name']}",
                        row.get("requested_by_name") or "—",
                        f"{row['current_net_salary']:,.2f}",
                        f"{row['proposed_net_salary']:,.2f}",
                        f"{delta:+,.2f}",
                        row["requested_at"],
                    ),
                )
        self.app.run_async(
            lambda: self.app.enterprise.list_payroll_adjustments(self.run_id, "معلق"),
            render,
            busy_text="تحميل طلبات الاعتماد...",
        )

    def details(self):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر طلبًا.", parent=self)
            return
        messagebox.showinfo(
            "تفاصيل طلب التعديل",
            (
                f"الموظف: {row['emp_code']} — {row['employee_name']}\n"
                f"الفترة: {row['period']}\n"
                f"الراتب الأساسي: {row['current_base_salary']:,.2f} ← "
                f"{row['proposed_base_salary']:,.2f}\n"
                f"البدلات اليدوية: {row['proposed_manual_allowances']:,.2f}\n"
                f"الإضافي: {row['proposed_overtime_hours']:g} ساعة = "
                f"{row['proposed_overtime']:,.2f}\n"
                f"خصومات أخرى: {row['proposed_manual_deductions']:,.2f}\n"
                f"خصم الغياب: {row['proposed_absence_deduction']:,.2f}\n"
                f"خصم التأخير: {row['proposed_late_deduction']:,.2f}\n"
                f"خصم الإجازة دون راتب: {row['proposed_unpaid_leave_deduction']:,.2f}\n"
                f"الصافي: {row['current_net_salary']:,.2f} ← "
                f"{row['proposed_net_salary']:,.2f}\n\n"
                f"السبب/الملاحظات:\n{row.get('request_notes') or '—'}"
            ),
            parent=self,
        )

    def approve(self):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر طلبًا.", parent=self)
            return
        if not messagebox.askyesno(
            "تأكيد الاعتماد",
            "سيُعاد الحساب حسب راتب الموظف وسياسة الشركة ثم يتحدث المسير والإجمالي. متابعة؟",
            parent=self,
        ):
            return
        note = simpledialog.askstring("ملاحظة القرار", "ملاحظة اعتماد اختيارية:", parent=self) or ""
        def done(result):
            messagebox.showinfo(
                "تم الاعتماد",
                f"صافي الموظف: {result['net_salary']:,.2f}\n"
                f"إجمالي المسير بعد الاعتماد: {result['run_total']:,.2f}",
                parent=self,
            )
            self.load()
            if self.on_changed:
                self.on_changed()
        self.app.run_async(
            lambda: self.app.enterprise.decide_payroll_adjustment(
                int(row["id"]), True, self.app.current_user, note
            ),
            done,
            busy_text="إعادة الحساب واعتماد التعديل...",
        )

    def reject(self):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر طلبًا.", parent=self)
            return
        reason = simpledialog.askstring("سبب الرفض", "اكتب سبب الرفض:", parent=self)
        if reason is None:
            return
        if not reason.strip():
            messagebox.showwarning("تنبيه", "سبب الرفض مطلوب.", parent=self)
            return
        def done(_result):
            messagebox.showinfo("تم", "رُفض الطلب ولم تتغير أي قيمة مالية.", parent=self)
            self.load()
            if self.on_changed:
                self.on_changed()
        self.app.run_async(
            lambda: self.app.enterprise.decide_payroll_adjustment(
                int(row["id"]), False, self.app.current_user, reason
            ),
            done,
            busy_text="رفض الطلب...",
        )


class ManagerApprovalsPage(EnterprisePage):
    """صندوق موحد لقرارات المدير ذات الأثر المالي أو الوظيفي."""

    def __init__(self, master, app):
        super().__init__(master, app, "اعتمادات المدير")
        self.rows: dict[str, dict] = {}
        ttk.Label(
            self,
            text=(
                "لا يؤثر أي طلب معلّق في لوحة المعلومات أو مسير الرواتب. "
                "تسجيل الحضور اليومي يبقى فوريًا، بينما الاستثناءات ذات الأثر المالي "
                "تمر عبر اعتماد المدير."
            ),
            foreground="#475569",
            wraplength=1050,
            justify="right",
        ).pack(fill="x", padx=8, pady=(2, 8))

        metrics = ttk.LabelFrame(self, text="الرقابة والمطابقة", padding=8)
        metrics.pack(fill="x", padx=4, pady=4)
        self.pending_var = tk.StringVar(value="الطلبات المعلقة: جارٍ التحميل...")
        self.payroll_match_var = tk.StringVar(value="مطابقة المسير: جارٍ التحميل...")
        self.dashboard_var = tk.StringVar(value="إجمالي لوحة المعلومات: جارٍ التحميل...")
        ttk.Label(metrics, textvariable=self.pending_var, foreground="#B45309").pack(
            side="right", padx=12
        )
        ttk.Label(metrics, textvariable=self.payroll_match_var).pack(
            side="right", padx=12
        )
        ttk.Label(metrics, textvariable=self.dashboard_var, foreground="#0F766E").pack(
            side="right", padx=12
        )

        table = ttk.Frame(self)
        table.pack(fill="both", expand=True, padx=4, pady=6)
        columns = ("category", "employee", "details", "requester", "source", "requested")
        self.tree = ttk.Treeview(
            table, columns=columns, show="headings", selectmode="browse"
        )
        for key, label, width in (
            ("category", "نوع الاعتماد", 230),
            ("employee", "الموظف", 210),
            ("details", "الأثر المختصر", 290),
            ("requester", "مقدم الطلب", 145),
            ("source", "المصدر/الفترة", 130),
            ("requested", "وقت الطلب", 160),
        ):
            self.tree.heading(key, text=label)
            self.tree.column(
                key, width=width, anchor="e" if key == "details" else "center"
            )
        ybar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=ybar.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", lambda _event: self.details())

        actions = ttk.Frame(self, padding=(4, 6))
        actions.pack(fill="x")
        ttk.Button(actions, text="اعتماد المحدد", command=self.approve).pack(
            side="right", padx=5
        )
        ttk.Button(actions, text="رفض المحدد", command=self.reject).pack(
            side="right", padx=5
        )
        ttk.Button(actions, text="عرض التفاصيل", command=self.details).pack(
            side="right", padx=5
        )
        ttk.Button(actions, text="تحديث ومطابقة", command=self.load).pack(
            side="left", padx=5
        )
        self.load()

    @staticmethod
    def payroll_categories(row: dict) -> str:
        categories: list[str] = []
        current_base = float(row.get("current_base_salary") or 0)
        proposed_base = float(row.get("proposed_base_salary") or 0)
        if proposed_base > current_base:
            categories.append("زيادة راتب")
        elif proposed_base < current_base:
            categories.append("تخفيض/تصحيح راتب")
        if float(row.get("proposed_manual_allowances") or 0) > 0:
            categories.append("بدلات أو مكافآت")
        if float(row.get("proposed_overtime_hours") or 0) > 0:
            categories.append("عمل إضافي")
        if float(row.get("proposed_manual_deductions") or 0) > 0:
            categories.append("خصم/سلفة/جزاء")
        if bool(row.get("proposed_apply_absence")):
            categories.append("غياب")
        if bool(row.get("proposed_apply_late")):
            categories.append("تأخير")
        if bool(row.get("proposed_apply_unpaid_leave")):
            categories.append("إجازة دون راتب")
        return "، ".join(categories) or "تعديل مسير راتب"

    def selected(self):
        selected = self.tree.selection()
        return self.rows.get(selected[0]) if selected else None

    def load(self):
        def work():
            return {
                "payroll": self.app.enterprise.list_payroll_adjustments(None, "معلق"),
                "leaves": self.app.db.list_leaves(status="معلّقة", limit=2000),
                "stats": self.app.db.dashboard_stats(),
                "runs": self.app.enterprise.list_payroll_runs(),
            }

        def render(result):
            rows: list[dict] = []
            for item in result["payroll"]:
                current_net = float(item.get("current_net_salary") or 0)
                proposed_net = float(item.get("proposed_net_salary") or 0)
                rows.append(
                    {
                        **item,
                        "key": f"payroll:{item['id']}",
                        "kind": "payroll",
                        "category": self.payroll_categories(item),
                        "employee": f"{item['emp_code']} — {item['employee_name']}",
                        "details": (
                            f"الصافي {current_net:,.2f} ← {proposed_net:,.2f} "
                            f"({proposed_net - current_net:+,.2f})"
                        ),
                        "requester": item.get("requested_by_name") or "—",
                        "source": f"رواتب {item['period']}",
                        "requested": item.get("requested_at") or "",
                    }
                )
            for leave in result["leaves"]:
                rows.append(
                    {
                        **leave,
                        "key": f"leave:{leave['id']}",
                        "kind": "leave",
                        "category": f"إجازة — {leave['leave_type']}",
                        "employee": f"{leave['emp_code']} — {leave['name']}",
                        "details": (
                            f"{leave['start_date']} إلى {leave['end_date']} "
                            f"({float(leave['total_days']):g} يوم)"
                        ),
                        "requester": leave.get("created_by_username") or "—",
                        "source": "نظام الإجازات",
                        "requested": leave.get("created_at") or "",
                    }
                )
            rows.sort(key=lambda row: str(row.get("requested") or ""), reverse=True)
            self.rows = {row["key"]: row for row in rows}
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                self.tree.insert(
                    "", "end", iid=row["key"],
                    values=(
                        row["category"], row["employee"], row["details"],
                        row["requester"], row["source"], row["requested"],
                    ),
                )

            payroll_pending = len(result["payroll"])
            leave_pending = len(result["leaves"])
            self.pending_var.set(
                f"المعلقة: {len(rows)} "
                f"(رواتب وآثار حضور: {payroll_pending}، إجازات: {leave_pending})"
            )
            stats = result["stats"]
            runs = result["runs"]
            latest = runs[0] if runs else {}
            payable = int(stats.get("payroll_employees") or 0)
            in_run = int(latest.get("employee_count") or 0)
            period = latest.get("period") or "لا يوجد"
            match_text = f"أحدث مسير {period}: {in_run} من {payable} موظف"
            self.payroll_match_var.set(match_text)
            self.dashboard_var.set(
                f"إجمالي اللوحة: {float(stats.get('salaries') or 0):,.2f}"
                f" — {stats.get('salary_source') or 'ملفات الموظفين'}"
            )
            self.payroll_match_var.set(
                match_text + (" ✓" if in_run == payable and payable else " ⚠")
            )

        self.app.run_async(work, render, busy_text="تحميل اعتمادات المدير ومطابقة النتائج...")

    def details(self):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر طلب اعتماد.", parent=self)
            return
        if row["kind"] == "payroll":
            details = (
                f"النوع: {row['category']}\n"
                f"الموظف: {row['employee']}\n"
                f"الفترة: {row['period']}\n"
                f"الأساسي: {row['current_base_salary']:,.2f} ← "
                f"{row['proposed_base_salary']:,.2f}\n"
                f"البدلات اليدوية: {row['proposed_manual_allowances']:,.2f}\n"
                f"الإضافي: {row['proposed_overtime']:,.2f}\n"
                f"الخصومات الأخرى: {row['proposed_manual_deductions']:,.2f}\n"
                f"خصم الغياب: {row['proposed_absence_deduction']:,.2f}\n"
                f"خصم التأخير: {row['proposed_late_deduction']:,.2f}\n"
                f"خصم الإجازة دون راتب: "
                f"{row['proposed_unpaid_leave_deduction']:,.2f}\n"
                f"الصافي: {row['current_net_salary']:,.2f} ← "
                f"{row['proposed_net_salary']:,.2f}\n\n"
                f"الملاحظات: {row.get('request_notes') or '—'}"
            )
        else:
            details = (
                f"النوع: {row['category']}\n"
                f"الموظف: {row['employee']}\n"
                f"الفترة: {row['start_date']} إلى {row['end_date']}\n"
                f"أيام العمل: {float(row['total_days']):g}\n"
                f"السبب: {row.get('reason') or '—'}\n"
                f"الملاحظات: {row.get('notes') or '—'}"
            )
        messagebox.showinfo("تفاصيل اعتماد المدير", details, parent=self)

    def approve(self):
        self.decide(True)

    def reject(self):
        self.decide(False)

    def decide(self, approve: bool):
        row = self.selected()
        if not row:
            messagebox.showwarning("تنبيه", "اختر طلب اعتماد.", parent=self)
            return
        decision = "اعتماد" if approve else "رفض"
        if not messagebox.askyesno(
            f"تأكيد {decision}",
            f"{decision} الطلب التالي؟\n\n{row['category']}\n{row['employee']}\n{row['details']}",
            parent=self,
        ):
            return
        note = simpledialog.askstring(
            "ملاحظة القرار",
            "اكتب ملاحظة القرار:" if not approve else "ملاحظة اختيارية:",
            parent=self,
        )
        if note is None:
            return
        if not approve and not note.strip():
            messagebox.showwarning("تنبيه", "سبب الرفض مطلوب.", parent=self)
            return

        def work():
            if row["kind"] == "payroll":
                return self.app.enterprise.decide_payroll_adjustment(
                    int(row["id"]), approve, self.app.current_user, note
                )
            self.app.db.update_leave_status(
                int(row["id"]),
                "معتمدة" if approve else "مرفوضة",
                self.app.current_user,
                note,
            )
            return {"status": "معتمدة" if approve else "مرفوضة"}

        def done(_result):
            messagebox.showinfo(
                "تم القرار",
                f"تم {decision} الطلب، وأُعيدت مطابقة لوحة المعلومات والمسيرات.",
                parent=self,
            )
            self.load()

        self.app.run_async(
            work, done, busy_text=f"{decision} الطلب وإعادة مطابقة النتائج..."
        )


class PayrollPolicyDialog(tk.Toplevel):
    def __init__(self, master, app, on_saved=None):
        super().__init__(master)
        self.app = app
        self.on_saved = on_saved
        self.title("سياسة حساب الرواتب")
        self.geometry("520x300")
        self.transient(master)
        self.grab_set()
        policy = self.app.enterprise.payroll_policy()
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="تستخدم هذه القيم لحساب كل موظف من راتبه الأساسي الخاص.",
            foreground="#475569",
        ).grid(row=0, column=0, columnspan=2, pady=(0, 14))
        self.days = tk.StringVar(value=f"{float(policy['days_divisor']):g}")
        self.hours = tk.StringVar(value=f"{float(policy['daily_hours']):g}")
        self.multiplier = tk.StringVar(value=f"{float(policy['overtime_multiplier']):g}")
        for row, (label, variable) in enumerate(
            (
                ("مقسوم أيام الشهر", self.days),
                ("ساعات العمل اليومية", self.hours),
                ("معامل العمل الإضافي", self.multiplier),
            ),
            start=1,
        ):
            ttk.Label(frame, text=label).grid(row=row, column=1, sticky="e", padx=8, pady=7)
            ttk.Entry(frame, textvariable=variable, width=18).grid(row=row, column=0, padx=8, pady=7)
        ttk.Button(frame, text="حفظ السياسة", command=self.save).grid(row=5, column=1, pady=18)
        ttk.Button(frame, text="إلغاء", command=self.destroy).grid(row=5, column=0, pady=18)

    def save(self):
        try:
            values = [float(value.get().strip()) for value in (self.days, self.hours, self.multiplier)]
            if min(values) <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("خطأ", "كل قيم السياسة يجب أن تكون أرقامًا أكبر من صفر.", parent=self)
            return
        def done(_result):
            if self.on_saved:
                self.on_saved()
            self.destroy()
        self.app.run_async(
            lambda: self.app.enterprise.set_payroll_policy(
                values[0], values[1], values[2], self.app.current_user
            ),
            done,
            busy_text="حفظ سياسة الحساب...",
        )


class PayrollItemDialog(tk.Toplevel):
    """إنشاء طلب تعديل دون المساس بالبند الفعلي قبل اعتماد المدير."""

    def __init__(self, master, app, item: dict, on_saved=None):
        super().__init__(master)
        self.app = app
        self.item = item
        self.on_saved = on_saved
        self.title("طلب تعديل بند راتب")
        self.geometry("720x700")
        self.minsize(650, 600)
        self.transient(master)
        self.grab_set()
        pending = item.get("adjustment_status") == "معلق"
        policy = self.app.enterprise.payroll_policy()

        shell = ttk.Frame(self)
        shell.pack(fill="both", expand=True)
        footer = ttk.Frame(shell, padding=(12, 8))
        footer.pack(side="bottom", fill="x")
        body = ttk.Frame(shell, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=f"{item['emp_code']} — {item['name']}", font=TITLE_FONT).grid(
            row=0, column=0, columnspan=3, pady=(0, 8)
        )
        ttk.Label(
            body,
            text=(
                "هذا طلب فقط. لن يتغير المسير أو إجمالي الشاشة الرئيسية إلا بعد اعتماد المدير. "
                "الراتب المعدل يخص هذا المسير ولا يغير ملف الموظف."
            ),
            foreground="#475569", wraplength=640, justify="right",
        ).grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        if pending:
            ttk.Label(
                body, text="يوجد طلب معلّق؛ الحفظ سيحدّث الطلب نفسه.",
                foreground="#B45309",
            ).grid(row=2, column=0, columnspan=3, pady=(0, 8))

        def proposed(name, current):
            value = item.get(name) if pending else current
            return float(value or 0)

        self.base_salary = tk.StringVar(
            value=f"{proposed('proposed_base_salary', item['base_salary']):.2f}"
        )
        self.manual_allowances = tk.StringVar(
            value=f"{proposed('proposed_manual_allowances', item['manual_allowances']):.2f}"
        )
        self.overtime_hours = tk.StringVar(
            value=f"{proposed('proposed_overtime_hours', item.get('overtime_hours')):.2f}"
        )
        self.overtime_multiplier = tk.StringVar(
            value=f"{proposed('proposed_overtime_multiplier', policy['overtime_multiplier']):.2f}"
        )
        self.manual_deductions = tk.StringVar(
            value=f"{proposed('proposed_manual_deductions', item['manual_deductions']):.2f}"
        )
        fields = (
            ("الراتب الأساسي لهذا المسير", self.base_salary, "ريال"),
            ("بدلات/مكافآت إضافية", self.manual_allowances, "ريال"),
            ("ساعات العمل الإضافي", self.overtime_hours, "ساعة"),
            ("معامل الإضافي", self.overtime_multiplier, "مرة"),
            ("خصومات أو سلف أو جزاءات أخرى", self.manual_deductions, "ريال"),
        )
        start_row = 3
        for index, (label, variable, unit) in enumerate(fields, start=start_row):
            ttk.Label(body, text=label).grid(row=index, column=2, sticky="e", padx=6, pady=5)
            ttk.Entry(body, textvariable=variable, width=20).grid(
                row=index, column=1, sticky="ew", padx=6, pady=5
            )
            ttk.Label(body, text=unit).grid(row=index, column=0, sticky="w", padx=6)

        def proposed_bool(name, current):
            return bool(item.get(name)) if pending else bool(current)

        options = ttk.LabelFrame(body, text="آثار الحضور والإجازات — كلها تنتظر اعتماد المدير", padding=10)
        options.grid(row=8, column=0, columnspan=3, sticky="ew", pady=12)
        self.apply_absence = tk.BooleanVar(
            value=proposed_bool("proposed_apply_absence", item.get("apply_absence_deduction"))
        )
        self.apply_late = tk.BooleanVar(
            value=proposed_bool("proposed_apply_late", item.get("apply_late_deduction"))
        )
        self.apply_unpaid = tk.BooleanVar(
            value=proposed_bool(
                "proposed_apply_unpaid_leave", item.get("apply_unpaid_leave_deduction")
            )
        )
        ttk.Checkbutton(
            options,
            text=f"خصم الغياب: {float(item.get('absence_days') or 0):g} يوم حسب أجر يوم الموظف",
            variable=self.apply_absence,
        ).pack(anchor="e", pady=4)
        ttk.Checkbutton(
            options,
            text=f"خصم التأخير: {float(item.get('late_minutes') or 0) / 60:.2f} ساعة حسب أجر ساعة الموظف",
            variable=self.apply_late,
        ).pack(anchor="e", pady=4)
        ttk.Checkbutton(
            options,
            text=f"خصم إجازة دون راتب: {float(item.get('unpaid_leave_days') or 0):g} يوم حسب أجر يوم الموظف",
            variable=self.apply_unpaid,
        ).pack(anchor="e", pady=4)
        ttk.Label(
            options,
            text=(
                f"السياسة الحالية: الراتب ÷ {float(policy['days_divisor']):g} يوم، "
                f"ثم ÷ {float(policy['daily_hours']):g} ساعات. "
                f"معامل الإضافي الافتراضي {float(policy['overtime_multiplier']):g}."
            ),
            foreground="#0F766E", wraplength=610, justify="right",
        ).pack(anchor="e", pady=(8, 2))

        ttk.Label(body, text="سبب التعديل وملاحظاته").grid(row=9, column=2, sticky="ne", padx=6)
        self.notes = tk.Text(body, height=5, wrap="word")
        self.notes.grid(row=9, column=0, columnspan=2, sticky="nsew", padx=6)
        self.notes.insert(
            "1.0",
            str((item.get("request_notes") if pending else item.get("notes")) or ""),
        )

        buttons = ttk.Frame(footer)
        buttons.pack()
        ttk.Button(buttons, text="إرسال طلب الاعتماد", command=self.save).pack(
            side="right", padx=6, ipadx=12
        )
        ttk.Button(buttons, text="إلغاء", command=self.destroy).pack(
            side="right", padx=6, ipadx=12
        )
        body.columnconfigure(1, weight=1)
        body.rowconfigure(9, weight=1)

    @staticmethod
    def _number(value: str, label: str) -> float:
        try:
            result = float(str(value).strip() or 0)
        except ValueError as exc:
            raise ValueError(f"{label} يجب أن يكون رقمًا صحيحًا.") from exc
        if result < 0:
            raise ValueError(f"{label} لا يمكن أن يكون سالبًا.")
        return result

    def save(self):
        try:
            base_salary = self._number(self.base_salary.get(), "الراتب الأساسي")
            manual_allowances = self._number(self.manual_allowances.get(), "البدلات")
            overtime_hours = self._number(self.overtime_hours.get(), "ساعات الإضافي")
            overtime_multiplier = self._number(
                self.overtime_multiplier.get(), "معامل الإضافي"
            )
            manual_deductions = self._number(
                self.manual_deductions.get(), "الخصومات الأخرى"
            )
            if overtime_multiplier <= 0:
                raise ValueError("معامل الإضافي يجب أن يكون أكبر من صفر.")
        except ValueError as exc:
            messagebox.showerror("قيمة غير صحيحة", str(exc), parent=self)
            return

        operation = lambda: self.app.enterprise.submit_payroll_adjustment(
            self.item["id"],
            base_salary,
            manual_allowances,
            overtime_hours,
            manual_deductions,
            self.notes.get("1.0", "end").strip(),
            self.app.current_user,
            apply_absence_deduction=self.apply_absence.get(),
            apply_late_deduction=self.apply_late.get(),
            apply_unpaid_leave_deduction=self.apply_unpaid.get(),
            overtime_multiplier=overtime_multiplier,
        )

        def done(result):
            messagebox.showinfo(
                "تم إرسال الطلب",
                f"الصافي الحالي: {result['current_net']:,.2f}\n"
                f"الصافي المقترح: {result['proposed_net']:,.2f}\n"
                f"الفرق: {result['delta']:+,.2f}\n\n"
                "لن يطبق الفرق قبل اعتماد المدير.",
                parent=self,
            )
            if self.on_saved:
                self.on_saved()
            self.destroy()

        self.app.run_async(operation, done, busy_text="حساب المقترح وإرسال طلب الاعتماد...")


class ShiftsPage(EnterprisePage):
    def __init__(self, master, app):
        super().__init__(master, app, "المناوبات وأوقات الدوام")
        top=ttk.Frame(self);top.pack(fill="x",pady=4)
        if app.current_user['role'] in {'admin','hr'}:
            ttk.Button(top,text="إضافة مناوبة",command=self.add_shift).pack(side="right",padx=4)
            ttk.Button(top,text="إسناد مناوبة لموظف",command=self.assign).pack(side="right",padx=4)
        ttk.Button(top,text="تحديث",command=self.load).pack(side="left",padx=4)
        notebook=ttk.Notebook(self);notebook.pack(fill="both",expand=True)
        a=ttk.Frame(notebook,padding=5);b=ttk.Frame(notebook,padding=5);notebook.add(a,text="المناوبات");notebook.add(b,text="الإسنادات")
        self.shift_tree=ttk.Treeview(a,columns=("name","start","end","grace","active"),show="headings")
        for k,l,w in (("name","الاسم",220),("start","البداية",100),("end","النهاية",100),("grace","دقائق السماح",100),("active","نشطة",80)):
            self.shift_tree.heading(k,text=l);self.shift_tree.column(k,width=w,anchor="center")
        self.shift_tree.pack(fill="both",expand=True)
        self.assign_tree=ttk.Treeview(b,columns=("employee","shift","time","from","to"),show="headings")
        for k,l,w in (("employee","الموظف",220),("shift","المناوبة",160),("time","الوقت",130),("from","من",100),("to","إلى",100)):
            self.assign_tree.heading(k,text=l);self.assign_tree.column(k,width=w,anchor="center")
        self.assign_tree.pack(fill="both",expand=True);self.load()

    def load(self):
        def render(result):
            shifts,assignments=result;self.shifts={str(r['id']):r for r in shifts};self.shift_tree.delete(*self.shift_tree.get_children())
            for r in shifts:self.shift_tree.insert("","end",iid=str(r['id']),values=(r['name'],r['start_time'],r['end_time'],r['grace_minutes'],'نعم' if r['is_active'] else 'لا'))
            self.assign_tree.delete(*self.assign_tree.get_children())
            for r in assignments:self.assign_tree.insert("","end",values=(f"{r['emp_code']} — {r['name']}",r['shift_name'],f"{r['start_time']}–{r['end_time']}",r['effective_from'],r.get('effective_to') or '—'))
        self.app.run_async(lambda:(self.app.enterprise.list_shifts(),self.app.enterprise.list_shift_assignments()),render,busy_text="تحميل المناوبات...")

    def add_shift(self):
        name=simpledialog.askstring("المناوبة","اسم المناوبة:",parent=self)
        if not name:return
        start=simpledialog.askstring("البداية","وقت البداية HH:MM:",initialvalue="08:00",parent=self)
        end=simpledialog.askstring("النهاية","وقت النهاية HH:MM:",initialvalue="17:00",parent=self)
        grace=simpledialog.askinteger("السماح","دقائق السماح:",initialvalue=15,minvalue=0,parent=self)
        if not start or not end or grace is None:return
        self.app.run_async(lambda:self.app.enterprise.add_shift(name,start,end,grace,self.app.current_user),lambda _r:self.load(),busy_text="إضافة المناوبة...")

    def assign(self):
        employees=self.app.db.list_employee_choices(False);shifts=self.app.enterprise.list_shifts()
        if not employees or not shifts:return
        EmployeeShiftDialog(self,self.app,employees,shifts,on_saved=self.load)


class EmployeeShiftDialog(tk.Toplevel):
    def __init__(self,master,app,employees,shifts,on_saved=None):
        super().__init__(master);self.app=app;self.on_saved=on_saved;self.title("إسناد مناوبة");self.transient(master);self.grab_set()
        f=ttk.Frame(self,padding=15);f.pack(fill="both",expand=True)
        self.emap={f"{r['emp_code']} — {r['name']}":r for r in employees};self.smap={r['name']:r for r in shifts}
        self.ev=tk.StringVar(value=next(iter(self.emap)));self.sv=tk.StringVar(value=next(iter(self.smap)));self.fv=tk.StringVar(value=date.today().isoformat());self.tv=tk.StringVar()
        for row,(label,var,values) in enumerate((("الموظف",self.ev,list(self.emap)),("المناوبة",self.sv,list(self.smap)))):
            ttk.Label(f,text=label).grid(row=row,column=1,padx=5,pady=5);ttk.Combobox(f,textvariable=var,values=values,state="readonly",width=35).grid(row=row,column=0)
        for row,(label,var) in enumerate((("من تاريخ",self.fv),("إلى تاريخ (اختياري)",self.tv)),start=2):
            ttk.Label(f,text=label).grid(row=row,column=1,padx=5,pady=5);h=ttk.Frame(f);h.grid(row=row,column=0);ttk.Entry(h,textvariable=var,width=30).pack(side="right");ttk.Button(h,text="📅",width=3,command=lambda v=var:DatePicker(self,v.get(),v.set)).pack(side="left")
        ttk.Button(f,text="حفظ",command=self.save).grid(row=4,column=0,columnspan=2,pady=10)
    def save(self):
        employee=self.emap[self.ev.get()];shift=self.smap[self.sv.get()]
        self.app.run_async(lambda:self.app.enterprise.assign_shift(employee['id'],shift['id'],self.fv.get(),self.tv.get(),self.app.current_user),lambda _r:(self.on_saved and self.on_saved(),self.destroy()),busy_text="إسناد المناوبة...")


class LeaveBalancesPage(EnterprisePage):
    def __init__(self,master,app):
        super().__init__(master,app,"أرصدة الإجازات السنوية")
        top=ttk.Frame(self);top.pack(fill="x",pady=4);self.year_var=tk.StringVar(value=str(date.today().year))
        ttk.Label(top,text="السنة:").pack(side="right");ttk.Spinbox(top,from_=2000,to=2200,textvariable=self.year_var,width=7,command=self.load).pack(side="right",padx=4)
        ttk.Button(top,text="تحديث",command=self.load).pack(side="right",padx=4)
        if app.current_user['role'] in {'admin','hr'}:ttk.Button(top,text="تعديل رصيد المحدد",command=self.edit).pack(side="left",padx=4)
        self.tree=ttk.Treeview(self,columns=("employee","dept","entitlement","carried","adjust","used","available"),show="headings")
        for k,l,w in (("employee","الموظف",220),("dept","القسم",150),("entitlement","الاستحقاق",90),("carried","المرحّل",90),("adjust","التسوية",90),("used","المستخدم",90),("available","المتاح",90)):
            self.tree.heading(k,text=l);self.tree.column(k,width=w,anchor="center")
        self.tree.pack(fill="both",expand=True);self.load()
    def load(self):
        try:year=int(self.year_var.get())
        except ValueError:return
        def render(rows):
            self.rows={str(r['employee_id']):r for r in rows};self.tree.delete(*self.tree.get_children())
            for r in rows:self.tree.insert("","end",iid=str(r['employee_id']),values=(f"{r['emp_code']} — {r['name']}",r['department'],r['entitlement'],r['carried_forward'],r['adjustment'],r['used'],r['available']))
        self.app.run_async(lambda:self.app.enterprise.list_leave_balances(year),render,busy_text="تحميل أرصدة الإجازات...")
    def edit(self):
        s=self.tree.selection()
        if not s:return
        r=self.rows[s[0]];ent=simpledialog.askfloat("الاستحقاق","الاستحقاق السنوي:",initialvalue=float(r['entitlement']),parent=self)
        if ent is None:return
        carried=simpledialog.askfloat("المرحّل","الرصيد المرحّل:",initialvalue=float(r['carried_forward']),parent=self)
        if carried is None:return
        adj=simpledialog.askfloat("التسوية","تسوية الرصيد (+/-):",initialvalue=float(r['adjustment']),parent=self)
        if adj is None:return
        notes=simpledialog.askstring("ملاحظات","ملاحظات:",initialvalue=r.get('notes') or '',parent=self) or ''
        self.app.run_async(lambda:self.app.enterprise.set_leave_balance(r['employee_id'],int(self.year_var.get()),ent,carried,adj,notes,self.app.current_user),lambda _r:self.load(),busy_text="حفظ الرصيد...")


class NotificationsPage(EnterprisePage):
    def __init__(self,master,app):
        super().__init__(master,app,"مركز التنبيهات")
        top=ttk.Frame(self);top.pack(fill="x",pady=4);ttk.Label(top,text="الفترة القادمة بالأيام:").pack(side="right");self.days=tk.StringVar(value="120");ttk.Spinbox(top,from_=7,to=365,textvariable=self.days,width=6).pack(side="right",padx=4);ttk.Button(top,text="تحديث",command=self.load).pack(side="right",padx=4)
        self.tree=ttk.Treeview(self,columns=("date","start","end","level","type","employee","message"),show="headings")
        for k,l,w in (("date","تاريخ التنبيه",105),("start","البداية",100),("end","النهاية",100),("level","الأهمية",85),("type","النوع",140),("employee","الموظف",220),("message","التفاصيل",300)):
            self.tree.heading(k,text=l);self.tree.column(k,width=w,anchor="center")
        self.tree.pack(fill="both",expand=True);self.load()
    def load(self):
        try:days=int(self.days.get())
        except ValueError:days=120
        def render(rows):
            self.tree.delete(*self.tree.get_children())
            for r in rows:self.tree.insert("","end",values=(r['date'],r.get('start_date') or '—',r.get('end_date') or '—',r['level'],r['type'],r['employee'],r['message']))
        self.app.run_async(lambda:self.app.enterprise.notifications(days),render,busy_text="تحميل التنبيهات...")

class EnterpriseHubPage(EnterprisePage):
    """واجهة موحدة حتى تبقى القائمة الجانبية مناسبة للشاشات الصغيرة."""
    def __init__(self, master, app):
        super().__init__(master, app, "الإدارة المتقدمة")
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)
        pages = (
            ("العقود والمستندات", DocumentsContractsPage),
            ("الرواتب", PayrollPage),
            ("المناوبات", ShiftsPage),
            ("أرصدة الإجازات", LeaveBalancesPage),
            ("العطلات", HolidaysPage),
            ("التنبيهات", NotificationsPage),
        )
        for label, cls in pages:
            host = ttk.Frame(notebook)
            notebook.add(host, text=label)
            cls(host, app).pack(fill="both", expand=True, padx=5, pady=5)

class HolidaysPage(EnterprisePage):
    def __init__(self, master, app):
        super().__init__(master, app, "العطلات الرسمية وأيام الشركة")
        top = ttk.Frame(self); top.pack(fill="x", pady=4)
        self.year_var = tk.StringVar(value=str(date.today().year))
        ttk.Label(top, text="السنة:").pack(side="right")
        ttk.Spinbox(top, from_=2000, to=2200, textvariable=self.year_var, width=7, command=self.load).pack(side="right", padx=4)
        ttk.Button(top, text="تحديث", command=self.load).pack(side="right", padx=4)
        if app.current_user["role"] in {"admin", "hr"}:
            ttk.Button(top, text="إضافة عطلة", command=self.add).pack(side="left", padx=4)
        if app.current_user["role"] == "admin":
            ttk.Button(top, text="حذف المحدد", command=self.delete).pack(side="left", padx=4)
        self.tree = ttk.Treeview(self, columns=("date", "name", "notes"), show="headings")
        for key, label, width in (("date", "التاريخ", 120), ("name", "اسم العطلة", 240), ("notes", "ملاحظات", 360)):
            self.tree.heading(key, text=label); self.tree.column(key, width=width, anchor="center")
        self.tree.pack(fill="both", expand=True)
        self.load()

    def load(self):
        try: year = int(self.year_var.get())
        except ValueError: return
        def render(rows):
            self.rows = {r["holiday_date"]: r for r in rows}
            self.tree.delete(*self.tree.get_children())
            for row in rows:
                self.tree.insert("", "end", iid=row["holiday_date"], values=(row["holiday_date"], row["name"], row.get("notes") or ""))
        self.app.run_async(lambda: self.app.enterprise.list_holidays(year), render, busy_text="تحميل العطلات...")

    def add(self):
        dialog = tk.Toplevel(self); dialog.title("إضافة عطلة"); dialog.transient(self); dialog.grab_set()
        frame = ttk.Frame(dialog, padding=15); frame.pack(fill="both", expand=True)
        date_var = tk.StringVar(value=date.today().isoformat()); name_var = tk.StringVar()
        ttk.Label(frame, text="التاريخ").grid(row=0, column=1, padx=5, pady=5)
        holder = ttk.Frame(frame); holder.grid(row=0, column=0)
        ttk.Entry(holder, textvariable=date_var, width=28).pack(side="right")
        ttk.Button(holder, text="📅", width=3, command=lambda: DatePicker(dialog, date_var.get(), date_var.set)).pack(side="left")
        ttk.Label(frame, text="الاسم").grid(row=1, column=1, padx=5, pady=5)
        ttk.Entry(frame, textvariable=name_var, width=34).grid(row=1, column=0)
        ttk.Label(frame, text="ملاحظات").grid(row=2, column=1, padx=5, pady=5)
        notes = tk.Text(frame, width=34, height=4); notes.grid(row=2, column=0)
        def save():
            self.app.run_async(
                lambda: self.app.enterprise.add_holiday(date_var.get(), name_var.get(), notes.get("1.0", "end").strip(), self.app.current_user),
                lambda _r: (dialog.destroy(), self.load()), busy_text="حفظ العطلة..."
            )
        ttk.Button(frame, text="حفظ", command=save).grid(row=3, column=0, columnspan=2, pady=10)

    def delete(self):
        selected = self.tree.selection()
        if not selected: return
        self.app.run_async(
            lambda: self.app.enterprise.delete_holiday(selected[0], self.app.current_user),
            lambda _r: self.load(), busy_text="حذف العطلة..."
        )
