from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
import os
import platform
from pathlib import Path
import re
import shutil
import sys
import time
import traceback
from typing import Callable

from .config import APP_VERSION, DATA_DIR, LOG_DIR

LOGGER_NAME = "protech_hr"
SESSION_MARKER = DATA_DIR / ".session_running"
SESSION_STATE = DATA_DIR / "session_state.json"
SESSION_HISTORY_DIR = DATA_DIR / "session_history"
FORM_DRAFT_DIR = DATA_DIR / "recovery_drafts"


def _file_handler(path: Path, level: int) -> RotatingFileHandler:
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(threadName)s | %(message)s"))
    return handler


def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(_file_handler(LOG_DIR / "app.log", logging.INFO))
        logger.addHandler(_file_handler(LOG_DIR / "errors.log", logging.ERROR))
    return logger


def _redact_sensitive(text: str) -> str:
    patterns = (
        r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*([^\s,;]+)",
        r"(?i)(كلمة\s*المرور|رمز\s*الدخول)\s*[:=]\s*([^\s,;]+)",
    )
    for pattern in patterns:
        text = re.sub(pattern, r"\1=<redacted>", text)
    text = re.sub(
        r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])",
        "<redacted-email>",
        text,
    )
    text = re.sub(
        r"(?<!\d)(?:\+?9665\d{8}|05\d{8})(?!\d)",
        "<redacted-phone>",
        text,
    )
    text = re.sub(
        r"(?<!\d)[12]\d{9}(?!\d)",
        "<redacted-id>",
        text,
    )
    return text


def write_crash_report(exc_type, exc_value, exc_tb, context: str = "unexpected") -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = LOG_DIR / f"crash_{stamp}.log"
    details = _redact_sensitive("".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    path.write_text(
        f"ProTech HR: {APP_VERSION}\n"
        f"Context: {context}\n"
        f"Python: {sys.version}\n"
        f"Platform: {platform.platform()}\n"
        f"Executable: {sys.executable}\n\n{details}",
        encoding="utf-8",
    )
    setup_logging().error("Unhandled error (%s):\n%s", context, details)
    return path


def begin_session() -> bool:
    """Mark the process active and report whether the previous session was interrupted."""
    interrupted = SESSION_MARKER.exists()
    SESSION_MARKER.write_text(
        json.dumps(
            {"version": APP_VERSION, "pid": os.getpid(), "started_at": time.strftime("%Y-%m-%dT%H:%M:%S")},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return interrupted


def end_session() -> None:
    SESSION_MARKER.unlink(missing_ok=True)


def _atomic_json_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    os.replace(temporary, path)


def save_session_state(page_name: str, page_state: dict | None = None) -> None:
    allowed = {
        "DashboardPage", "EmployeesPage", "NotificationsPage", "MessagingPage",
        "AttendancePage", "LeavesPage", "EnterpriseHubPage", "CalendarPage",
        "ReportsPage", "SmartAnalyticsPage", "OperationsCenterPage", "ImportantLinksPage",
        "AuditPage", "UsersPage", "BackupPage",
    }
    if page_name not in allowed:
        return
    loaded = load_session_state()
    states = loaded.get("page_states")
    if not isinstance(states, dict):
        states = {}
    if isinstance(page_state, dict):
        states[page_name] = page_state
    payload = {
        "version": APP_VERSION,
        "last_page": page_name,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "page_states": states,
    }
    _atomic_json_write(SESSION_STATE, payload)
    SESSION_HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    checkpoint = SESSION_HISTORY_DIR / f"session_{time.time_ns()}.json"
    _atomic_json_write(checkpoint, payload)
    history = sorted(
        SESSION_HISTORY_DIR.glob("session_*.json"),
        key=lambda item: item.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in history[20:]:
        safe_unlink(stale)


def load_session_state() -> dict:
    candidates = [SESSION_STATE]
    try:
        candidates.extend(
            sorted(
                SESSION_HISTORY_DIR.glob("session_*.json"),
                key=lambda item: item.stat().st_mtime_ns,
                reverse=True,
            )
        )
    except OSError:
        pass
    for candidate in candidates:
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if (
            isinstance(loaded, dict)
            and isinstance(loaded.get("page_states", {}), dict)
            and isinstance(loaded.get("last_page", ""), str)
        ):
            return loaded
    return {}


def _draft_path(kind: str, owner: str) -> Path:
    import hashlib

    safe_kind = re.sub(r"[^a-z0-9_-]", "", str(kind).casefold()) or "form"
    owner_key = hashlib.sha256(str(owner).encode("utf-8")).hexdigest()[:16]
    return FORM_DRAFT_DIR / f"{safe_kind}_{owner_key}.json"


def save_form_draft(kind: str, owner: str, payload: dict) -> Path:
    path = _draft_path(kind, owner)
    _atomic_json_write(
        path,
        {
            "version": APP_VERSION,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "payload": payload,
        },
    )
    return path


def load_form_draft(kind: str, owner: str) -> dict | None:
    path = _draft_path(kind, owner)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(loaded, dict) or not isinstance(loaded.get("payload"), dict):
        return None
    return loaded


def clear_form_draft(kind: str, owner: str) -> None:
    safe_unlink(_draft_path(kind, owner))


def retry_file_operation(
    operation: Callable[[], object], *, attempts: int = 8, initial_delay: float = 0.08,
    retriable: tuple[type[BaseException], ...] = (PermissionError, OSError),
):
    last_error: BaseException | None = None
    delay = initial_delay
    for attempt in range(attempts):
        try:
            return operation()
        except retriable as exc:
            last_error = exc
            if attempt + 1 >= attempts:
                break
            time.sleep(delay)
            delay = min(delay * 1.8, 1.0)
    if last_error:
        raise last_error
    return None


def safe_rmtree(path: Path | str, *, suppress: bool = True) -> bool:
    target = Path(path)
    if not target.exists():
        return True
    try:
        retry_file_operation(lambda: shutil.rmtree(target), attempts=10, initial_delay=0.1)
        return True
    except OSError:
        setup_logging().warning("تعذر حذف المجلد المؤقت: %s", target, exc_info=True)
        if not suppress:
            raise
        return False


def safe_unlink(path: Path | str, *, suppress: bool = True) -> bool:
    target = Path(path)
    if not target.exists():
        return True
    try:
        retry_file_operation(target.unlink, attempts=8, initial_delay=0.08)
        return True
    except OSError:
        setup_logging().warning("تعذر حذف الملف: %s", target, exc_info=True)
        if not suppress:
            raise
        return False


def atomic_replace(source: Path | str, destination: Path | str) -> None:
    source_path = Path(source)
    destination_path = Path(destination)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    retry_file_operation(lambda: os.replace(source_path, destination_path), attempts=10, initial_delay=0.1)
