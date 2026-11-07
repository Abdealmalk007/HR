from __future__ import annotations

import tkinter as tk


HORIZONTAL_MARGIN = 48
TOP_MARGIN = 36
BOTTOM_MARGIN = 88


def _clamp(value: int, minimum: int, maximum: int) -> int:
    if maximum < minimum:
        return minimum
    return max(minimum, min(maximum, value))


def fit_dialog_to_screen(dialog: tk.Toplevel) -> None:
    """اجعل النافذة المنبثقة مرئية ومتمركزة دون تغيير محتواها."""
    try:
        if not dialog.winfo_exists() or not dialog.winfo_viewable():
            return
        dialog.update_idletasks()
        screen_width = max(640, int(dialog.winfo_screenwidth()))
        screen_height = max(480, int(dialog.winfo_screenheight()))
        maximum_width = max(420, screen_width - HORIZONTAL_MARGIN * 2)
        maximum_height = max(320, screen_height - TOP_MARGIN - BOTTOM_MARGIN)

        requested_width = max(int(dialog.winfo_reqwidth()), int(dialog.winfo_width()))
        requested_height = max(int(dialog.winfo_reqheight()), int(dialog.winfo_height()))
        width = min(maximum_width, max(360, requested_width))
        height = min(maximum_height, max(220, requested_height))

        minimum_width, minimum_height = dialog.minsize()
        dialog.minsize(
            min(maximum_width, max(300, int(minimum_width))),
            min(maximum_height, max(180, int(minimum_height))),
        )

        # منتقي التاريخ صغير ومربوط بموضع الحقل؛ احتفظ بموضعه مع ضمان بقائه
        # داخل الشاشة. بقية النوافذ تتمركز فوق النافذة الأم لسهولة الاستخدام.
        preserve_position = dialog.__class__.__name__ == "DatePicker"
        if preserve_position:
            x = _clamp(int(dialog.winfo_x()), 8, screen_width - width - 8)
            y = _clamp(int(dialog.winfo_y()), 8, screen_height - height - 48)
        else:
            parent = getattr(dialog, "master", None)
            try:
                parent = parent.winfo_toplevel() if parent is not None else None
                usable_parent = (
                    parent is not None
                    and parent is not dialog
                    and parent.winfo_exists()
                    and parent.winfo_viewable()
                )
            except tk.TclError:
                usable_parent = False
            if usable_parent:
                x = int(parent.winfo_rootx()) + (int(parent.winfo_width()) - width) // 2
                y = int(parent.winfo_rooty()) + (int(parent.winfo_height()) - height) // 2
            else:
                x = (screen_width - width) // 2
                y = (screen_height - height) // 2
            x = _clamp(x, 8, screen_width - width - 8)
            y = _clamp(y, TOP_MARGIN, screen_height - height - 48)

        dialog.geometry(f"{width}x{height}+{x}+{y}")
        dialog._protech_screen_fit_done = True
    except (tk.TclError, AttributeError, TypeError, ValueError):
        return


def install_adaptive_dialogs(root: tk.Misc) -> None:
    """فعّل ملاءمة تلقائية لكل نوافذ Toplevel الحالية والمستقبلية."""
    def on_map(event) -> None:
        dialog = event.widget
        if not isinstance(dialog, tk.Toplevel):
            return
        if getattr(dialog, "_protech_screen_fit_done", False):
            return
        if getattr(dialog, "_protech_screen_fit_scheduled", False):
            return
        dialog._protech_screen_fit_scheduled = True

        def apply() -> None:
            try:
                dialog._protech_screen_fit_scheduled = False
                fit_dialog_to_screen(dialog)
            except tk.TclError:
                return

        dialog.after_idle(apply)

    root.bind_class("Toplevel", "<Map>", on_map, add="+")
    root._protech_dialog_map_handler = on_map
