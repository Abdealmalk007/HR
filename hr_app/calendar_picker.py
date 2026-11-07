from __future__ import annotations

import calendar
from datetime import date, datetime
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable

MONTH_NAMES = (
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
)


class DatePicker(tk.Toplevel):
    """منتقي تاريخ سريع يدعم القفز المباشر بين السنوات والإدخال الرقمي."""

    def __init__(
        self,
        master,
        initial: str,
        on_select: Callable[[str], None],
        *,
        min_year: int = 1920,
        max_year: int | None = None,
        title: str = "اختر التاريخ",
    ):
        super().__init__(master)
        self.title(title)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.on_select = on_select
        self.min_year = min_year
        self.max_year = max_year or (date.today().year + 20)
        try:
            current = datetime.strptime(initial, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            current = date.today()
        self.year = min(max(current.year, self.min_year), self.max_year)
        self.month = current.month
        self.selected_day = current.day
        self._build()
        self._render()
        self.update_idletasks()
        x = master.winfo_rootx() + 50
        y = master.winfo_rooty() + 50
        self.geometry(f"+{x}+{y}")
        self.bind("<Escape>", lambda _event: self.destroy())
        self.bind("<Return>", lambda _event: self._apply_direct_date())
        self.bind("<Prior>", lambda _event: self._shift_year(-1))
        self.bind("<Next>", lambda _event: self._shift_year(1))

    def _build(self):
        navigation = ttk.LabelFrame(self, text="انتقال سريع", padding=8)
        navigation.pack(fill="x", padx=8, pady=(8, 4))

        ttk.Button(navigation, text="-10 سنوات", command=lambda: self._shift_year(-10)).grid(row=0, column=0, padx=2)
        ttk.Button(navigation, text="-1", width=4, command=lambda: self._shift_year(-1)).grid(row=0, column=1, padx=2)

        self.year_var = tk.StringVar(value=str(self.year))
        year_spin = ttk.Spinbox(
            navigation,
            from_=self.min_year,
            to=self.max_year,
            textvariable=self.year_var,
            width=7,
            justify="center",
            command=self._jump_from_controls,
        )
        year_spin.grid(row=0, column=2, padx=4)
        year_spin.bind("<Return>", lambda _event: self._jump_from_controls())
        year_spin.bind("<FocusOut>", lambda _event: self._jump_from_controls())

        self.month_var = tk.StringVar(value=MONTH_NAMES[self.month - 1])
        month_combo = ttk.Combobox(
            navigation,
            textvariable=self.month_var,
            values=MONTH_NAMES,
            state="readonly",
            width=11,
            justify="center",
        )
        month_combo.grid(row=0, column=3, padx=4)
        month_combo.bind("<<ComboboxSelected>>", lambda _event: self._jump_from_controls())

        ttk.Button(navigation, text="+1", width=4, command=lambda: self._shift_year(1)).grid(row=0, column=4, padx=2)
        ttk.Button(navigation, text="+10 سنوات", command=lambda: self._shift_year(10)).grid(row=0, column=5, padx=2)

        header = ttk.Frame(self, padding=(8, 4))
        header.pack(fill="x")
        ttk.Button(header, text="◀ شهر", command=self._previous_month).pack(side="left")
        self.title_var = tk.StringVar()
        ttk.Label(header, textvariable=self.title_var, anchor="center", font=("Segoe UI", 11, "bold")).pack(
            side="left", fill="x", expand=True, padx=8
        )
        ttk.Button(header, text="شهر ▶", command=self._next_month).pack(side="right")

        self.days_frame = ttk.Frame(self, padding=(8, 0, 8, 8))
        self.days_frame.pack()

        direct = ttk.LabelFrame(self, text="إدخال رقمي مباشر", padding=8)
        direct.pack(fill="x", padx=8, pady=4)
        self.direct_var = tk.StringVar(value=f"{self.year:04d}-{self.month:02d}-{self.selected_day:02d}")
        direct_entry = ttk.Entry(direct, textvariable=self.direct_var, width=16, justify="center")
        direct_entry.pack(side="right", padx=4)
        ttk.Label(direct, text="YYYY-MM-DD").pack(side="right", padx=4)
        ttk.Button(direct, text="اعتماد", command=self._apply_direct_date).pack(side="left", padx=4)

        footer = ttk.Frame(self, padding=8)
        footer.pack(fill="x")
        ttk.Button(footer, text="اليوم", command=self._choose_today).pack(side="left")
        ttk.Button(footer, text="إلغاء", command=self.destroy).pack(side="right")

    def _render(self):
        self.year = min(max(int(self.year), self.min_year), self.max_year)
        self.month = min(max(int(self.month), 1), 12)
        self.year_var.set(str(self.year))
        self.month_var.set(MONTH_NAMES[self.month - 1])
        for child in self.days_frame.winfo_children():
            child.destroy()
        self.title_var.set(f"{MONTH_NAMES[self.month - 1]} {self.year}")
        if hasattr(self, "direct_var"):
            safe_day = min(self.selected_day, calendar.monthrange(self.year, self.month)[1])
            self.direct_var.set(f"{self.year:04d}-{self.month:02d}-{safe_day:02d}")
        weekdays = ["ن", "ث", "ر", "خ", "ج", "س", "ح"]
        for col, name in enumerate(weekdays):
            ttk.Label(self.days_frame, text=name, anchor="center", width=5).grid(row=0, column=col, padx=1, pady=1)
        cal = calendar.Calendar(firstweekday=0)
        today = date.today()
        for row_index, week in enumerate(cal.monthdayscalendar(self.year, self.month), start=1):
            for col, day_number in enumerate(week):
                if day_number == 0:
                    ttk.Label(self.days_frame, text="", width=5).grid(row=row_index, column=col)
                    continue
                label = str(day_number)
                if (self.year, self.month, day_number) == (today.year, today.month, today.day):
                    label = f"● {day_number}"
                btn = ttk.Button(
                    self.days_frame,
                    text=label,
                    width=5,
                    command=lambda d=day_number: self._select(d),
                )
                btn.grid(row=row_index, column=col, padx=1, pady=1)

    def _jump_from_controls(self):
        try:
            year = int(self.year_var.get())
        except (TypeError, ValueError):
            year = self.year
        self.year = min(max(year, self.min_year), self.max_year)
        try:
            self.month = MONTH_NAMES.index(self.month_var.get()) + 1
        except ValueError:
            pass
        self._render()

    def _shift_year(self, amount: int):
        self.year = min(max(self.year + amount, self.min_year), self.max_year)
        self._render()

    def _previous_month(self):
        self.month -= 1
        if self.month == 0:
            self.month = 12
            self.year -= 1
        self.year = max(self.min_year, self.year)
        self._render()

    def _next_month(self):
        self.month += 1
        if self.month == 13:
            self.month = 1
            self.year += 1
        self.year = min(self.max_year, self.year)
        self._render()

    def _choose_today(self):
        self.on_select(date.today().isoformat())
        self.destroy()

    def _apply_direct_date(self):
        raw = self.direct_var.get().strip()
        try:
            selected = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            messagebox.showerror("تاريخ غير صحيح", "اكتب التاريخ بصيغة YYYY-MM-DD.", parent=self)
            return
        if not self.min_year <= selected.year <= self.max_year:
            messagebox.showerror(
                "تاريخ خارج النطاق",
                f"السنة يجب أن تكون بين {self.min_year} و{self.max_year}.",
                parent=self,
            )
            return
        self.on_select(selected.isoformat())
        self.destroy()

    def _select(self, day_number: int):
        selected = date(self.year, self.month, day_number)
        self.on_select(selected.isoformat())
        self.destroy()


def attach_date_picker(master, entry: ttk.Entry, **kwargs) -> ttk.Button:
    def open_picker():
        DatePicker(master, entry.get(), lambda value: _replace_entry(entry, value), **kwargs)

    return ttk.Button(master, text="📅", width=3, command=open_picker)


def _replace_entry(entry: ttk.Entry, value: str):
    entry.delete(0, tk.END)
    entry.insert(0, value)
