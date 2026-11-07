from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# لوحة متوازنة عالية التباين وتعمل دون مكتبات واجهة خارجية.
COLORS = {
    "background": "#F3F6FB",
    "surface": "#FFFFFF",
    "surface_alt": "#EAF0F8",
    "sidebar": "#0B1220",
    "sidebar_alt": "#111C31",
    "text": "#122033",
    "muted": "#64748B",
    "border": "#D8E1EC",
    "success": "#16A34A",
    "warning": "#D97706",
    "danger": "#DC2626",
    "white": "#FFFFFF",
}

PAGE_ACCENTS = {
    "DashboardPage": "#2563EB",
    "EmployeesPage": "#0891B2",
    "MessagingPage": "#8B5CF6",
    "AttendancePage": "#0D9488",
    "LeavesPage": "#7C3AED",
    "EnterpriseHubPage": "#EA580C",
    "CalendarPage": "#DB2777",
    "ReportsPage": "#059669",
    "AuditPage": "#4F46E5",
    "UsersPage": "#C2410C",
    "BackupPage": "#475569",
    "DocumentsContractsPage": "#EA580C",
    "PayrollPage": "#16A34A",
    "ShiftsPage": "#0284C7",
    "LeaveBalancesPage": "#7C3AED",
    "NotificationsPage": "#DC2626",
    "HolidaysPage": "#DB2777",
    "SaudiCompliancePage": "#047857",
}

METRIC_COLORS = (
    "#2563EB", "#0D9488", "#7C3AED", "#EA580C", "#DB2777", "#059669",
    "#DC2626", "#D97706", "#475569",
)


def page_accent(page_or_name) -> str:
    name = page_or_name if isinstance(page_or_name, str) else page_or_name.__class__.__name__
    return PAGE_ACCENTS.get(name, "#2563EB")


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[index:index + 2], 16) for index in (0, 2, 4))


def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
    return "#" + "".join(f"{max(0, min(255, value)):02X}" for value in rgb)


def blend(color: str, other: str = "#FFFFFF", ratio: float = 0.18) -> str:
    first = _hex_to_rgb(color)
    second = _hex_to_rgb(other)
    ratio = max(0.0, min(1.0, ratio))
    return _rgb_to_hex(tuple(round(a * (1 - ratio) + b * ratio) for a, b in zip(first, second)))


def configure_styles(root: tk.Misc, style: ttk.Style) -> None:
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    root.configure(background=COLORS["background"])
    style.configure("TFrame", background=COLORS["background"])
    style.configure("Page.TFrame", background=COLORS["background"])
    style.configure("Surface.TFrame", background=COLORS["surface"])
    style.configure("Sidebar.TFrame", background=COLORS["sidebar"])
    style.configure("TLabel", background=COLORS["background"], foreground=COLORS["text"])
    style.configure("Surface.TLabel", background=COLORS["surface"], foreground=COLORS["text"])
    style.configure("Muted.TLabel", background=COLORS["background"], foreground=COLORS["muted"])
    style.configure("Status.TFrame", background=COLORS["surface_alt"], relief="flat")
    style.configure("Status.TLabel", background=COLORS["surface_alt"], foreground=COLORS["muted"])

    style.configure(
        "TButton", background="#2563EB", foreground=COLORS["white"],
        borderwidth=0, focusthickness=1, focuscolor="#93C5FD", padding=(12, 8),
        font=("Segoe UI", 10, "bold"),
    )
    style.map(
        "TButton",
        background=[("disabled", "#CBD5E1"), ("pressed", "#1D4ED8"), ("active", "#3B82F6")],
        foreground=[("disabled", "#64748B"), ("!disabled", COLORS["white"])],
    )
    style.configure("Secondary.TButton", background="#E2E8F0", foreground=COLORS["text"])
    style.map(
        "Secondary.TButton",
        background=[("disabled", "#E2E8F0"), ("pressed", "#CBD5E1"), ("active", "#F1F5F9")],
        foreground=[("disabled", "#64748B"), ("!disabled", COLORS["text"])],
    )
    style.configure("Danger.TButton", background=COLORS["danger"], foreground=COLORS["white"])
    style.map("Danger.TButton", background=[("pressed", "#991B1B"), ("active", "#EF4444")])
    style.configure("Outlook.TButton", background="#2563EB", foreground=COLORS["white"], padding=(14, 9))
    style.map(
        "Outlook.TButton",
        background=[("disabled", "#CBD5E1"), ("pressed", "#1E40AF"), ("active", "#60A5FA")],
        foreground=[("disabled", "#64748B"), ("!disabled", COLORS["white"])],
    )
    style.configure("WhatsApp.TButton", background="#16A34A", foreground=COLORS["white"], padding=(14, 9))
    style.map(
        "WhatsApp.TButton",
        background=[("disabled", "#CBD5E1"), ("pressed", "#166534"), ("active", "#4ADE80")],
        foreground=[("disabled", "#64748B"), ("!disabled", COLORS["white"])],
    )

    style.configure(
        "Treeview", background=COLORS["surface"], fieldbackground=COLORS["surface"],
        foreground=COLORS["text"], rowheight=32, bordercolor=COLORS["border"],
        lightcolor=COLORS["border"], darkcolor=COLORS["border"], borderwidth=1,
    )
    style.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", "#1E3A8A")])
    style.configure(
        "Treeview.Heading", background="#E8EEF7", foreground=COLORS["text"],
        font=("Segoe UI", 10, "bold"), relief="flat", padding=(8, 8),
    )
    style.map("Treeview.Heading", background=[("active", "#D8E4F3")])

    style.configure("TEntry", fieldbackground=COLORS["surface"], foreground=COLORS["text"], padding=7)
    style.configure("TCombobox", fieldbackground=COLORS["surface"], foreground=COLORS["text"], padding=6)
    style.map("TCombobox", fieldbackground=[("readonly", COLORS["surface"])])
    style.configure("TSpinbox", fieldbackground=COLORS["surface"], foreground=COLORS["text"], padding=6)

    style.configure(
        "TLabelframe", background=COLORS["surface"], bordercolor=COLORS["border"],
        lightcolor=COLORS["border"], darkcolor=COLORS["border"], borderwidth=1, relief="solid",
    )
    style.configure(
        "TLabelframe.Label", background=COLORS["surface"], foreground=COLORS["text"],
        font=("Segoe UI", 10, "bold"),
    )
    style.configure("TNotebook", background=COLORS["background"], borderwidth=0)
    style.configure("TNotebook.Tab", padding=(16, 9), background="#E2E8F0", foreground=COLORS["text"])
    style.map("TNotebook.Tab", background=[("selected", COLORS["surface"]), ("active", "#F1F5F9")])
    style.configure("Vertical.TScrollbar", background="#CBD5E1", troughcolor="#EEF2F7", borderwidth=0)
    style.configure("Horizontal.TScrollbar", background="#CBD5E1", troughcolor="#EEF2F7", borderwidth=0)


def ensure_section_button_style(style: ttk.Style, style_name: str, accent: str) -> str:
    name = f"{style_name}.TButton"
    style.configure(
        name, background=accent, foreground=COLORS["white"], borderwidth=0,
        padding=(12, 8), font=("Segoe UI", 10, "bold"),
        focusthickness=1, focuscolor=blend(accent, "#FFFFFF", 0.58),
    )
    style.map(
        name,
        background=[
            ("disabled", "#CBD5E1"),
            ("pressed", blend(accent, "#000000", 0.22)),
            ("active", blend(accent, "#FFFFFF", 0.18)),
        ],
        foreground=[("disabled", "#64748B"), ("!disabled", COLORS["white"])],
    )
    return name


class NavButton(tk.Button):
    """زر تنقل حديث بإضاءة واضحة عند مرور مؤشر الفأرة."""

    def __init__(self, master, *, text: str, accent: str, command, **kwargs):
        self.accent = accent
        self.normal_bg = COLORS["sidebar"]
        self.hover_bg = blend(accent, "#FFFFFF", 0.08)
        self.selected = False
        super().__init__(
            master, text=text, command=command, anchor="e", padx=16, pady=7,
            bg=self.normal_bg, fg="#D9E4F2", activebackground=self.hover_bg,
            activeforeground=COLORS["white"], relief="flat", bd=0,
            highlightthickness=0, cursor="hand2", font=("Segoe UI", 10, "bold"),
            **kwargs,
        )
        self.bind("<Enter>", self._enter, add="+")
        self.bind("<Leave>", self._leave, add="+")

    def _enter(self, _event=None):
        self.configure(bg=self.hover_bg, fg=COLORS["white"])

    def _leave(self, _event=None):
        self.configure(bg=self.accent if self.selected else self.normal_bg, fg=COLORS["white"] if self.selected else "#D9E4F2")

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self._leave()


class ScrollableSidebar(tk.Frame):
    """منطقة تنقل قابلة للتمرير مع إبقاء الرأس والتذييل ظاهرين."""

    def __init__(self, master, **kwargs):
        super().__init__(master, bg=COLORS["sidebar"], **kwargs)
        self.canvas = tk.Canvas(self, bg=COLORS["sidebar"], highlightthickness=0, bd=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=COLORS["sidebar"])
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._sync_scroll_region, add="+")
        self.canvas.bind("<Configure>", self._sync_width, add="+")
        self.canvas.bind("<Enter>", self._bind_wheel, add="+")
        self.canvas.bind("<Leave>", self._unbind_wheel, add="+")

    def _sync_scroll_region(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        content_height = self.inner.winfo_reqheight()
        canvas_height = self.canvas.winfo_height()
        if content_height <= canvas_height:
            self.scrollbar.pack_forget()
        elif not self.scrollbar.winfo_ismapped():
            self.scrollbar.pack(side="right", fill="y")

    def _sync_width(self, event):
        self.canvas.itemconfigure(self.window_id, width=event.width)

    def _bind_wheel(self, _event=None):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel, add="+")
        self.canvas.bind_all("<Button-4>", self._on_linux_wheel, add="+")
        self.canvas.bind_all("<Button-5>", self._on_linux_wheel, add="+")

    def _unbind_wheel(self, _event=None):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _on_linux_wheel(self, event):
        self.canvas.yview_scroll(-1 if event.num == 4 else 1, "units")


def build_page_header(parent: tk.Misc, title: str, accent: str, subtitle: str = "") -> tk.Frame:
    header = tk.Frame(parent, bg=accent, height=70, highlightthickness=0)
    header.pack(fill="x", pady=(0, 14))
    header.pack_propagate(False)
    text_frame = tk.Frame(header, bg=accent)
    text_frame.pack(side="right", fill="both", expand=True, padx=22, pady=10)
    tk.Label(
        text_frame, text=title, bg=accent, fg=COLORS["white"],
        font=("Segoe UI", 18, "bold"), anchor="e",
    ).pack(fill="x")
    if subtitle:
        tk.Label(
            text_frame, text=subtitle, bg=accent, fg=blend(COLORS["white"], accent, 0.25),
            font=("Segoe UI", 9), anchor="e",
        ).pack(fill="x", pady=(2, 0))
    tk.Frame(header, bg=blend(accent, "#FFFFFF", 0.35), width=7).pack(side="right", fill="y")
    return header
