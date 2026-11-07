from __future__ import annotations

import calendar
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import zipfile
from contextlib import closing
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from .config import (
    ATTENDANCE_STATUSES,
    BACKUP_DIR,
    CONTRACT_STATUSES,
    CONTRACT_TYPES,
    DB_PATH,
    DOCUMENTS_DIR,
    DOCUMENT_TYPES,
    IDENTITY_DIR,
    QIWA_CONTRACT_STATUSES,
    VIEW_CACHE_DIR,
)
from .database import Database, DatabaseError, PermissionError
from .identity_rules import IDENTITY_TYPES, identity_type_for_nationality
from .local_ai import workforce_risk_profile
from .money import money, money_float
from .resilience import atomic_replace, retry_file_operation, safe_rmtree, safe_unlink, setup_logging

_ALLOWED_DOCUMENT_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf", ".webp"}
_MAX_BACKUP_DATABASE_BYTES = 1024 * 1024 * 1024
_MAX_BACKUP_MANIFEST_BYTES = 2 * 1024 * 1024
_MAX_BACKUP_MEMBER_BYTES = 1024 * 1024 * 1024
_MAX_BACKUP_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
_MAX_BACKUP_MEMBERS = 50_000
_MAX_BACKUP_COMPRESSION_RATIO = 500


def _valid_document_signature(data: bytes, extension: str) -> bool:
    if extension in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if extension == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if extension == ".pdf":
        return data.startswith(b"%PDF-")
    if extension == ".webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_zip_member(archive: zipfile.ZipFile, name: str) -> str:
    digest = hashlib.sha256()
    with archive.open(name, "r") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_backup_members(
    archive: zipfile.ZipFile,
) -> list[tuple[zipfile.ZipInfo, str]]:
    """Validate names and expansion bounds before reading archive payloads."""
    infos = archive.infolist()
    if len(infos) > _MAX_BACKUP_MEMBERS:
        raise DatabaseError("النسخة الاحتياطية تحتوي عدد ملفات أكبر من الحد الآمن.")

    validated: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    total_size = 0
    for info in infos:
        raw_name = str(info.filename or "")
        if not raw_name or "\x00" in raw_name or "\\" in raw_name:
            raise DatabaseError("تحتوي النسخة على اسم ملف غير آمن.")
        normalized = raw_name.rstrip("/")
        clean = PurePosixPath(normalized)
        parts = clean.parts
        if (
            not normalized
            or clean.is_absolute()
            or ".." in parts
            or any(part in {"", "."} for part in parts)
            or (parts and (":" in parts[0] or parts[0].startswith("~")))
        ):
            raise DatabaseError("تحتوي النسخة على مسار ملف غير آمن.")
        canonical = clean.as_posix()
        if canonical in seen:
            raise DatabaseError("تحتوي النسخة على أسماء ملفات مكررة.")
        seen.add(canonical)

        file_type = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(file_type):
            raise DatabaseError("لا تسمح النسخ الاحتياطية بروابط رمزية.")
        if info.file_size < 0 or info.file_size > _MAX_BACKUP_MEMBER_BYTES:
            raise DatabaseError("تحتوي النسخة على ملف أكبر من الحد الآمن.")
        total_size += int(info.file_size)
        if total_size > _MAX_BACKUP_TOTAL_BYTES:
            raise DatabaseError("الحجم الإجمالي المفكوك للنسخة أكبر من الحد الآمن.")
        if (
            info.file_size > 1024 * 1024
            and info.compress_size > 0
            and info.file_size / info.compress_size > _MAX_BACKUP_COMPRESSION_RATIO
        ):
            raise DatabaseError("نسبة ضغط أحد ملفات النسخة غير طبيعية.")
        if (
            canonical not in {"hr_system.db", "manifest.json", "employee_documents"}
            and not canonical.startswith("employee_documents/")
        ):
            raise DatabaseError("تحتوي النسخة على ملف غير متوقع.")
        validated.append((info, canonical))

    names = {name for _info, name in validated}
    if not {"hr_system.db", "manifest.json"}.issubset(names):
        raise DatabaseError("النسخة لا تحتوي قاعدة البيانات أو ملف التعريف.")
    return validated


def _extract_validated_backup(
    archive: zipfile.ZipFile,
    destination: Path,
) -> None:
    """Extract only members that passed the archive preflight."""
    destination = destination.resolve()
    for info, canonical in _validated_backup_members(archive):
        target = (destination / Path(*PurePosixPath(canonical).parts)).resolve()
        if target != destination and destination not in target.parents:
            raise DatabaseError("تحتوي النسخة على مسار ملف غير آمن.")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, target.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)


def _safe_name(value: str, fallback: str = "employee") -> str:
    value = re.sub(r"[^\w\-\u0600-\u06FF]+", "_", str(value).strip(), flags=re.UNICODE).strip("_")
    return value[:80] or fallback


def _iso(value: str, label: str = "التاريخ") -> str:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date().isoformat()
    except ValueError as exc:
        raise ValueError(f"صيغة {label} يجب أن تكون YYYY-MM-DD.") from exc


class EnterpriseService:
    """وظائف الإصدار المؤسسي مع إبقاء قاعدة الإصدار السابق قابلة للترقية."""

    def __init__(self, db: Database):
        self.db = db
        self._backup_lock = threading.RLock()
        self.initialize()

    def initialize(self) -> None:
        with self.db.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS employee_documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    document_type TEXT NOT NULL,
                    original_name TEXT NOT NULL,
                    stored_path TEXT NOT NULL DEFAULT 'database',
                    file_data BLOB,
                    content_type TEXT,
                    file_size INTEGER NOT NULL DEFAULT 0,
                    file_sha256 TEXT,
                    start_date TEXT,
                    expiry_date TEXT,
                    notes TEXT,
                    uploaded_by INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE,
                    FOREIGN KEY(uploaded_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS contracts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    contract_number TEXT,
                    contract_type TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT,
                    probation_end_date TEXT,
                    contract_salary REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'ساري',
                    qiwa_status TEXT NOT NULL DEFAULT 'غير موثق',
                    qiwa_contract_number TEXT,
                    qiwa_documented_at TEXT,
                    notes TEXT,
                    document_id INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE,
                    FOREIGN KEY(document_id) REFERENCES employee_documents(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS leave_balances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    balance_year INTEGER NOT NULL,
                    leave_type TEXT NOT NULL DEFAULT 'سنوية',
                    entitlement REAL NOT NULL DEFAULT 30,
                    carried_forward REAL NOT NULL DEFAULT 0,
                    adjustment REAL NOT NULL DEFAULT 0,
                    notes TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(employee_id, balance_year, leave_type),
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    grace_minutes INTEGER NOT NULL DEFAULT 0,
                    work_days TEXT NOT NULL DEFAULT '0,1,2,3,4',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS employee_shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    shift_id INTEGER NOT NULL,
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(employee_id, effective_from),
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE,
                    FOREIGN KEY(shift_id) REFERENCES shifts(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS payroll_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'مسودة',
                    generated_by INTEGER,
                    approved_by INTEGER,
                    generated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    approved_at TEXT,
                    notes TEXT,
                    FOREIGN KEY(generated_by) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY(approved_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS payroll_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payroll_run_id INTEGER NOT NULL,
                    employee_id INTEGER NOT NULL,
                    base_salary REAL NOT NULL,
                    allowances REAL NOT NULL DEFAULT 0,
                    manual_allowances REAL NOT NULL DEFAULT 0,
                    overtime REAL NOT NULL DEFAULT 0,
                    overtime_hours REAL NOT NULL DEFAULT 0,
                    overtime_multiplier REAL NOT NULL DEFAULT 1.5,
                    manual_deductions REAL NOT NULL DEFAULT 0,
                    absence_days REAL NOT NULL DEFAULT 0,
                    unpaid_leave_days REAL NOT NULL DEFAULT 0,
                    apply_unpaid_leave_deduction INTEGER NOT NULL DEFAULT 0,
                    unpaid_leave_rate REAL NOT NULL DEFAULT 0,
                    unpaid_leave_deduction REAL NOT NULL DEFAULT 0,
                    apply_absence_deduction INTEGER NOT NULL DEFAULT 0,
                    absence_rate REAL NOT NULL DEFAULT 0,
                    absence_deduction REAL NOT NULL DEFAULT 0,
                    late_minutes REAL NOT NULL DEFAULT 0,
                    apply_late_deduction INTEGER NOT NULL DEFAULT 0,
                    late_hour_rate REAL NOT NULL DEFAULT 0,
                    late_deduction REAL NOT NULL DEFAULT 0,
                    net_salary REAL NOT NULL,
                    notes TEXT,
                    UNIQUE(payroll_run_id, employee_id),
                    FOREIGN KEY(payroll_run_id) REFERENCES payroll_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS payroll_adjustment_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payroll_item_id INTEGER NOT NULL,
                    payroll_run_id INTEGER NOT NULL,
                    employee_id INTEGER NOT NULL,
                    proposed_base_salary REAL NOT NULL,
                    proposed_manual_allowances REAL NOT NULL DEFAULT 0,
                    proposed_overtime REAL NOT NULL DEFAULT 0,
                    proposed_overtime_hours REAL NOT NULL DEFAULT 0,
                    proposed_overtime_multiplier REAL NOT NULL DEFAULT 1.5,
                    proposed_manual_deductions REAL NOT NULL DEFAULT 0,
                    proposed_apply_absence INTEGER NOT NULL DEFAULT 0,
                    proposed_absence_rate REAL NOT NULL DEFAULT 0,
                    proposed_absence_deduction REAL NOT NULL DEFAULT 0,
                    proposed_apply_late INTEGER NOT NULL DEFAULT 0,
                    proposed_late_hour_rate REAL NOT NULL DEFAULT 0,
                    proposed_late_deduction REAL NOT NULL DEFAULT 0,
                    proposed_apply_unpaid_leave INTEGER NOT NULL DEFAULT 0,
                    proposed_unpaid_leave_rate REAL NOT NULL DEFAULT 0,
                    proposed_unpaid_leave_deduction REAL NOT NULL DEFAULT 0,
                    proposed_total_allowances REAL NOT NULL DEFAULT 0,
                    proposed_net_salary REAL NOT NULL,
                    request_notes TEXT,
                    status TEXT NOT NULL DEFAULT 'معلق'
                        CHECK(status IN ('معلق','معتمد','مرفوض')),
                    requested_by INTEGER,
                    decided_by INTEGER,
                    decision_notes TEXT,
                    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    decided_at TEXT,
                    FOREIGN KEY(payroll_item_id) REFERENCES payroll_items(id) ON DELETE CASCADE,
                    FOREIGN KEY(payroll_run_id) REFERENCES payroll_runs(id) ON DELETE CASCADE,
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE,
                    FOREIGN KEY(requested_by) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY(decided_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS allowance_types (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL UNIQUE,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS employee_allowances (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    allowance_type_id INTEGER NOT NULL,
                    calculation_method TEXT NOT NULL DEFAULT 'fixed'
                        CHECK(calculation_method IN ('fixed','percentage')),
                    value REAL NOT NULL DEFAULT 0 CHECK(value >= 0),
                    effective_from TEXT NOT NULL,
                    effective_to TEXT,
                    notes TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE,
                    FOREIGN KEY(allowance_type_id) REFERENCES allowance_types(id) ON DELETE RESTRICT
                );

                CREATE TABLE IF NOT EXISTS payroll_allowance_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payroll_item_id INTEGER NOT NULL,
                    allowance_type_id INTEGER,
                    allowance_name TEXT NOT NULL,
                    calculation_method TEXT NOT NULL,
                    source_value REAL NOT NULL DEFAULT 0,
                    amount REAL NOT NULL DEFAULT 0,
                    FOREIGN KEY(payroll_item_id) REFERENCES payroll_items(id) ON DELETE CASCADE,
                    FOREIGN KEY(allowance_type_id) REFERENCES allowance_types(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS company_holidays (
                    holiday_date TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    notes TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_documents_employee ON employee_documents(employee_id);
                CREATE INDEX IF NOT EXISTS idx_documents_expiry ON employee_documents(expiry_date);
                CREATE INDEX IF NOT EXISTS idx_contract_employee ON contracts(employee_id);
                CREATE INDEX IF NOT EXISTS idx_contract_end ON contracts(end_date);
                CREATE INDEX IF NOT EXISTS idx_payroll_period ON payroll_runs(period);
                CREATE INDEX IF NOT EXISTS idx_payroll_adjustment_run_status
                    ON payroll_adjustment_requests(payroll_run_id,status);
                CREATE INDEX IF NOT EXISTS idx_payroll_adjustment_employee
                    ON payroll_adjustment_requests(employee_id,requested_at);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_payroll_adjustment_one_pending
                    ON payroll_adjustment_requests(payroll_item_id) WHERE status='معلق';
                CREATE INDEX IF NOT EXISTS idx_employee_allowances_employee ON employee_allowances(employee_id);
                CREATE INDEX IF NOT EXISTS idx_employee_allowances_dates ON employee_allowances(effective_from,effective_to);
                CREATE INDEX IF NOT EXISTS idx_payroll_allowance_item ON payroll_allowance_items(payroll_item_id);
                """
            )
            if not conn.execute("SELECT 1 FROM shifts LIMIT 1").fetchone():
                conn.execute(
                    "INSERT INTO shifts(name,start_time,end_time,grace_minutes,work_days) VALUES(?,?,?,?,?)",
                    ("الدوام الافتراضي", "08:00", "17:00", 15, "0,1,2,3,4"),
                )
            document_columns = {row[1] for row in conn.execute("PRAGMA table_info(employee_documents)").fetchall()}
            for column, definition in (
                ("file_data", "BLOB"),
                ("content_type", "TEXT"),
                ("file_size", "INTEGER NOT NULL DEFAULT 0"),
                ("file_sha256", "TEXT"),
                ("start_date", "TEXT"),
            ):
                if column not in document_columns:
                    conn.execute(f"ALTER TABLE employee_documents ADD COLUMN {column} {definition}")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_sha256 ON employee_documents(file_sha256)")
            contract_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(contracts)").fetchall()
            }
            for column, definition in (
                ("qiwa_status", "TEXT NOT NULL DEFAULT 'غير موثق'"),
                ("qiwa_contract_number", "TEXT"),
                ("qiwa_documented_at", "TEXT"),
            ):
                if column not in contract_columns:
                    conn.execute(f"ALTER TABLE contracts ADD COLUMN {column} {definition}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contract_qiwa_status ON contracts(qiwa_status)"
            )
            # الإصدارات السابقة خزنت جميع نسخ الوثيقة كـ "هوية وطنية"؛
            # نصحح وثائق غير السعوديين إلى "إقامة" وفق نوع الموظف المرقّى.
            conn.execute(
                """UPDATE employee_documents SET document_type='إقامة'
                   WHERE document_type='هوية وطنية' AND employee_id IN (
                       SELECT id FROM employees WHERE id_type='إقامة'
                   )"""
            )
            sync_mappings = (
                ("هوية وطنية", "id_issue_date", "id_expiry_date"),
                ("إقامة", "id_issue_date", "id_expiry_date"),
                ("جواز سفر", "id_issue_date", "id_expiry_date"),
                ("كرت عمل", "work_permit_start_date", "work_permit_expiry_date"),
                (
                    "تأمين طبي",
                    "medical_insurance_start_date",
                    "medical_insurance_expiry_date",
                ),
            )
            for document_type, employee_start, employee_end in sync_mappings:
                conn.execute(
                    f"""UPDATE employee_documents
                         SET start_date=COALESCE(start_date,(
                                 SELECT {employee_start} FROM employees e
                                 WHERE e.id=employee_documents.employee_id
                             )),
                             expiry_date=COALESCE(expiry_date,(
                                 SELECT {employee_end} FROM employees e
                                 WHERE e.id=employee_documents.employee_id
                             ))
                         WHERE document_type=?""",
                    (document_type,),
                )
                conn.execute(
                    f"""UPDATE employees
                         SET {employee_start}=COALESCE({employee_start},(
                                 SELECT d.start_date FROM employee_documents d
                                 WHERE d.employee_id=employees.id AND d.document_type=?
                                 ORDER BY d.id DESC LIMIT 1
                             )),
                             {employee_end}=COALESCE({employee_end},(
                                 SELECT d.expiry_date FROM employee_documents d
                                 WHERE d.employee_id=employees.id AND d.document_type=?
                                 ORDER BY d.id DESC LIMIT 1
                             ))
                         WHERE {'id_type=?' if document_type in IDENTITY_TYPES else '1=1'}""",
                    (
                        (document_type, document_type, document_type)
                        if document_type in IDENTITY_TYPES
                        else (document_type, document_type)
                    ),
                )

            payroll_columns = {row[1] for row in conn.execute("PRAGMA table_info(payroll_items)").fetchall()}
            if "manual_allowances" not in payroll_columns:
                conn.execute("ALTER TABLE payroll_items ADD COLUMN manual_allowances REAL NOT NULL DEFAULT 0")
                # في السجلات التاريخية كان حقل allowances يمثل القيمة اليدوية كاملة.
                conn.execute("UPDATE payroll_items SET manual_allowances=allowances WHERE manual_allowances=0")
            payroll_columns = {row[1] for row in conn.execute("PRAGMA table_info(payroll_items)").fetchall()}
            for column, definition in (
                ("apply_absence_deduction", "INTEGER NOT NULL DEFAULT 0"),
                ("absence_rate", "REAL NOT NULL DEFAULT 0"),
                ("overtime_hours", "REAL NOT NULL DEFAULT 0"),
                ("overtime_multiplier", "REAL NOT NULL DEFAULT 1.5"),
                ("apply_unpaid_leave_deduction", "INTEGER NOT NULL DEFAULT 0"),
                ("unpaid_leave_rate", "REAL NOT NULL DEFAULT 0"),
                ("unpaid_leave_deduction", "REAL NOT NULL DEFAULT 0"),
                ("late_minutes", "REAL NOT NULL DEFAULT 0"),
                ("apply_late_deduction", "INTEGER NOT NULL DEFAULT 0"),
                ("late_hour_rate", "REAL NOT NULL DEFAULT 0"),
                ("late_deduction", "REAL NOT NULL DEFAULT 0"),
            ):
                if column not in payroll_columns:
                    conn.execute(f"ALTER TABLE payroll_items ADD COLUMN {column} {definition}")

            default_allowances = (
                ("housing", "بدل السكن", 10),
                ("transport", "بدل المواصلات", 20),
                ("travel", "بدل السفر", 30),
                ("ticket", "بدل التذكرة", 40),
                ("communication", "بدل الاتصال", 50),
                ("meal", "بدل الطعام", 60),
                ("risk", "بدل الخطر", 70),
                ("shift", "بدل المناوبة", 80),
                ("other", "بدل آخر", 999),
            )
            conn.executemany(
                """INSERT OR IGNORE INTO allowance_types(code,name,sort_order)
                   VALUES(?,?,?)""",
                default_allowances,
            )
            conn.executemany(
                "INSERT OR IGNORE INTO app_settings(key,value) VALUES(?,?)",
                (
                    ("payroll_days_divisor", "30"),
                    ("payroll_daily_hours", "8"),
                    ("payroll_overtime_multiplier", "1.5"),
                ),
            )

        self._cleanup_view_cache()
        self._migrate_legacy_document_files()

    @staticmethod
    def _require_hr(actor: dict[str, Any]) -> None:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("هذه العملية متاحة للمدير والموارد البشرية فقط.")

    @staticmethod
    def _require_admin(actor: dict[str, Any]) -> None:
        if actor.get("role") != "admin":
            raise PermissionError("هذه العملية متاحة لمدير النظام فقط.")

    # ---------- Documents and identity copies ----------
    @staticmethod
    def _cleanup_view_cache(max_age_hours: int = 24) -> None:
        VIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - max(1, max_age_hours) * 3600
        for path in VIEW_CACHE_DIR.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                continue

    def _migrate_legacy_document_files(self) -> int:
        """ينقل مرفقات الإصدارات السابقة إلى BLOB داخل SQLite دون حذف الأصل فورًا."""
        migrated = 0
        try:
            with self.db.connection() as conn:
                rows = conn.execute(
                    """SELECT id,stored_path,original_name FROM employee_documents
                       WHERE file_data IS NULL AND TRIM(COALESCE(stored_path,'')) NOT IN ('','database')"""
                ).fetchall()
                for row in rows:
                    try:
                        path = (DOCUMENTS_DIR / row["stored_path"]).resolve()
                        if DOCUMENTS_DIR.resolve() not in path.parents or not path.is_file():
                            continue
                        data = path.read_bytes()
                        content_type = mimetypes.guess_type(row["original_name"] or path.name)[0] or "application/octet-stream"
                        conn.execute(
                            """UPDATE employee_documents SET file_data=?,content_type=?,file_size=?,file_sha256=?
                               WHERE id=?""",
                            (data, content_type, len(data), hashlib.sha256(data).hexdigest(), row["id"]),
                        )
                        migrated += 1
                    except OSError:
                        setup_logging().warning("تعذر ترحيل مستند قديم إلى قاعدة البيانات: %s", row["stored_path"], exc_info=True)
        except sqlite3.Error:
            setup_logging().warning("تعذر تنفيذ ترحيل المرفقات القديمة", exc_info=True)
        return migrated

    # ---------- Smart workspace ----------
    def global_search(
        self,
        query: str,
        actor: dict[str, Any],
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clean = str(query or "").strip()
        if len(clean) < 2:
            return []
        if len(clean) > 100:
            raise ValueError("عبارة البحث أطول من الحد المسموح.")
        limit = max(5, min(100, int(limit)))
        escaped = clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        results: list[dict[str, Any]] = []
        with self.db.connection() as conn:
            employees = conn.execute(
                """SELECT id,emp_code,name,department,job_title,status
                   FROM employees
                   WHERE name LIKE ? ESCAPE '\\'
                      OR emp_code LIKE ? ESCAPE '\\'
                      OR department LIKE ? ESCAPE '\\'
                      OR job_title LIKE ? ESCAPE '\\'
                   ORDER BY status='نشط' DESC,name COLLATE NOCASE
                   LIMIT ?""",
                (pattern, pattern, pattern, pattern, limit),
            ).fetchall()
            for row in employees:
                results.append(
                    {
                        "kind": "موظف",
                        "entity": "employee",
                        "id": int(row["id"]),
                        "title": f"{row['name']} ({row['emp_code']})",
                        "details": " | ".join(
                            value for value in (row["department"], row["job_title"], row["status"]) if value
                        ),
                    }
                )

            if actor.get("role") in {"admin", "hr"}:
                leaves = conn.execute(
                    """SELECT lr.id,e.name,lr.leave_type,lr.start_date,lr.end_date,lr.status
                       FROM leave_requests lr JOIN employees e ON e.id=lr.employee_id
                       WHERE e.name LIKE ? ESCAPE '\\'
                          OR lr.leave_type LIKE ? ESCAPE '\\'
                          OR lr.status LIKE ? ESCAPE '\\'
                       ORDER BY lr.start_date DESC LIMIT ?""",
                    (pattern, pattern, pattern, limit),
                ).fetchall()
                for row in leaves:
                    results.append(
                        {
                            "kind": "إجازة",
                            "entity": "leave",
                            "id": int(row["id"]),
                            "title": f"{row['name']} - {row['leave_type']}",
                            "details": f"{row['start_date']} إلى {row['end_date']} | {row['status']}",
                        }
                    )
                documents = conn.execute(
                    """SELECT d.id,e.name,d.document_type,d.original_name,d.expiry_date
                       FROM employee_documents d JOIN employees e ON e.id=d.employee_id
                       WHERE e.name LIKE ? ESCAPE '\\'
                          OR d.document_type LIKE ? ESCAPE '\\'
                          OR d.original_name LIKE ? ESCAPE '\\'
                       ORDER BY d.created_at DESC LIMIT ?""",
                    (pattern, pattern, pattern, limit),
                ).fetchall()
                for row in documents:
                    results.append(
                        {
                            "kind": "وثيقة",
                            "entity": "document",
                            "id": int(row["id"]),
                            "title": f"{row['name']} - {row['document_type']}",
                            "details": f"{row['original_name']} | الانتهاء: {row['expiry_date'] or 'غير محدد'}",
                        }
                    )

            if actor.get("role") == "admin":
                payroll = conn.execute(
                    """SELECT id,period,status,generated_at,approved_at
                       FROM payroll_runs
                       WHERE period LIKE ? ESCAPE '\\' OR status LIKE ? ESCAPE '\\'
                       ORDER BY period DESC LIMIT ?""",
                    (pattern, pattern, limit),
                ).fetchall()
                for row in payroll:
                    results.append(
                        {
                            "kind": "مسير رواتب",
                            "entity": "payroll",
                            "id": int(row["id"]),
                            "title": f"مسير {row['period']}",
                            "details": f"الحالة: {row['status']} | الإنشاء: {row['generated_at']}",
                        }
                    )
        kind_order = {"موظف": 0, "إجازة": 1, "وثيقة": 2, "مسير رواتب": 3}
        return sorted(results, key=lambda item: (kind_order.get(item["kind"], 9), item["title"]))[:limit]

    def workforce_risk_register(
        self,
        actor: dict[str, Any],
        *,
        limit: int = 100,
        history_days: int = 90,
    ) -> list[dict[str, Any]]:
        self._require_hr(actor)
        limit = max(5, min(500, int(limit)))
        history_days = max(30, min(365, int(history_days)))
        today = date.today()
        start_date = (today - timedelta(days=history_days)).isoformat()

        def days_left(value: Any) -> int | None:
            clean = str(value or "").strip()
            if not clean:
                return None
            try:
                return (datetime.strptime(clean, "%Y-%m-%d").date() - today).days
            except ValueError:
                return None

        with self.db.connection() as conn:
            rows = conn.execute(
                """SELECT e.id,e.emp_code,e.name,e.department,e.job_title,
                          e.performance_rating,e.id_expiry_date,
                          (SELECT c.end_date FROM contracts c
                           WHERE c.employee_id=e.id
                           ORDER BY CASE WHEN c.end_date IS NULL THEN 1 ELSE 0 END,
                                    c.end_date DESC LIMIT 1) AS contract_end_date,
                          (SELECT COUNT(*) FROM attendance a
                           WHERE a.employee_id=e.id AND a.attendance_date>=?) AS attendance_records,
                          (SELECT COUNT(*) FROM attendance a
                           WHERE a.employee_id=e.id AND a.attendance_date>=?
                             AND a.status='غائب') AS absences,
                          (SELECT COUNT(*) FROM attendance a
                           WHERE a.employee_id=e.id AND a.attendance_date>=?
                             AND COALESCE(a.late_minutes,0)>0) AS late_days,
                          (SELECT COUNT(*) FROM leave_requests lr
                           WHERE lr.employee_id=e.id AND lr.status='معلّقة') AS pending_leaves
                   FROM employees e
                   WHERE e.status<>'منتهي الخدمة'
                   ORDER BY e.name COLLATE NOCASE""",
                (start_date, start_date, start_date),
            ).fetchall()
        register: list[dict[str, Any]] = []
        for row in rows:
            metrics = {
                "attendance_records": row["attendance_records"],
                "absences": row["absences"],
                "late_days": row["late_days"],
                "pending_leaves": row["pending_leaves"],
                "performance_rating": row["performance_rating"],
                "document_days_left": days_left(row["id_expiry_date"]),
                "contract_days_left": days_left(row["contract_end_date"]),
            }
            profile = workforce_risk_profile(metrics)
            register.append(
                {
                    "employee_id": int(row["id"]),
                    "emp_code": row["emp_code"],
                    "name": row["name"],
                    "department": row["department"],
                    "job_title": row["job_title"],
                    **profile,
                }
            )
        return sorted(
            register,
            key=lambda item: (-int(item["score"]), str(item["name"])),
        )[:limit]

    def add_document(
        self, employee_id: int, source_path: str | Path, document_type: str,
        actor: dict[str, Any], expiry_date: str = "", notes: str = "",
        start_date: str = "",
    ) -> int:
        self._require_hr(actor)
        document_type = str(document_type or "").strip()
        if document_type not in DOCUMENT_TYPES:
            raise ValueError("نوع المستند غير معتمد.")
        employee = self.db.get_employee(employee_id)
        if not employee:
            raise DatabaseError("الموظف غير موجود.")
        expected_identity_type = str(employee.get("id_type") or "").strip()
        if document_type in IDENTITY_TYPES and expected_identity_type and document_type != expected_identity_type:
            raise ValueError(
                f"نوع الوثيقة المسجل لهذا الموظف هو {expected_identity_type}."
            )
        source = Path(source_path)
        if not source.is_file():
            raise DatabaseError("الملف المحدد غير موجود.")
        extension = source.suffix.lower()
        if extension not in _ALLOWED_DOCUMENT_EXTENSIONS:
            raise ValueError("الصيغ المدعومة: JPG وPNG وWEBP وPDF.")
        size = source.stat().st_size
        if size > 25 * 1024 * 1024:
            raise ValueError("حجم المستند يجب ألا يتجاوز 25 ميجابايت.")
        start = _iso(start_date, "تاريخ البداية") if str(start_date).strip() else None
        expiry = _iso(expiry_date, "تاريخ الانتهاء") if str(expiry_date).strip() else None
        if start and expiry and start > expiry:
            raise ValueError("تاريخ بداية المستند لا يمكن أن يلي تاريخ الانتهاء.")
        data = source.read_bytes()
        if len(data) > 25 * 1024 * 1024:
            raise ValueError("حجم المستند يجب ألا يتجاوز 25 ميجابايت.")
        if not _valid_document_signature(data, extension):
            raise ValueError("محتوى الملف لا يطابق صيغة المستند المحددة.")
        digest = hashlib.sha256(data).hexdigest()
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        identity_prefixes = {
            "هوية وطنية": "national_id",
            "إقامة": "residence_id",
            "جواز سفر": "passport",
        }
        prefix = identity_prefixes.get(document_type) or _safe_name(document_type, "document")
        stored_name = f"{prefix}_{_safe_name(employee['name'])}_{_safe_name(employee['emp_code'])}_{stamp}{extension}"
        with self.db.connection() as conn:
            cur = conn.execute(
                """INSERT INTO employee_documents(
                       employee_id,document_type,original_name,stored_path,file_data,content_type,file_size,
                       file_sha256,start_date,expiry_date,notes,uploaded_by
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (employee_id, document_type, source.name, stored_name, sqlite3.Binary(data), content_type,
                 len(data), digest, start, expiry, notes.strip(), actor.get("id")),
            )
            document_id = int(cur.lastrowid)
            if document_type in IDENTITY_TYPES:
                conn.execute(
                    "UPDATE employees SET id_type=?,id_issue_date=COALESCE(?,id_issue_date),"
                    "id_expiry_date=COALESCE(?,id_expiry_date),updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (document_type, start, expiry, employee_id),
                )
            elif document_type == "كرت عمل":
                conn.execute(
                    "UPDATE employees SET work_permit_start_date="
                    "COALESCE(?,work_permit_start_date),work_permit_expiry_date="
                    "COALESCE(?,work_permit_expiry_date),updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (start, expiry, employee_id),
                )
            elif document_type == "تأمين طبي":
                conn.execute(
                    "UPDATE employees SET medical_insurance_start_date="
                    "COALESCE(?,medical_insurance_start_date),medical_insurance_expiry_date="
                    "COALESCE(?,medical_insurance_expiry_date),updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (start, expiry, employee_id),
                )
            self.db._audit_conn(conn, actor, "CREATE", "employee_document", document_id, {
                "emp_code": employee["emp_code"], "type": document_type,
                "file": stored_name, "file_size": len(data),
                "start_date": start, "expiry_date": expiry,
                "storage": "sqlite_blob",
            })
            return document_id

    def save_identity_copy(
        self, employee_id: int, source_path: str | Path, actor: dict[str, Any],
        expiry_date: str = "", start_date: str = "",
    ) -> int:
        employee = self.db.get_employee(employee_id)
        if not employee:
            raise DatabaseError("الموظف غير موجود.")
        document_type = str(employee.get("id_type") or "").strip()
        if document_type not in IDENTITY_TYPES:
            raise ValueError("حدد نوع الهوية أو الإقامة أو جواز السفر قبل إرفاق النسخة.")
        return self.add_document(
            employee_id,
            source_path,
            document_type,
            actor,
            expiry_date=expiry_date,
            start_date=start_date,
        )

    def list_documents(self, employee_id: int | None = None) -> list[dict[str, Any]]:
        where = "WHERE d.employee_id=?" if employee_id else ""
        params = (employee_id,) if employee_id else ()
        with self.db.connection() as conn:
            rows = conn.execute(
                f"""SELECT d.id,d.employee_id,d.document_type,d.original_name,d.stored_path,
                           d.content_type,d.file_size,d.file_sha256,d.start_date,d.expiry_date,
                           d.notes,d.uploaded_by,d.created_at,
                           e.emp_code,e.name
                    FROM employee_documents d JOIN employees e ON e.id=d.employee_id
                    {where} ORDER BY d.id DESC""", params,
            ).fetchall()
            return [dict(r) for r in rows]

    def latest_identity_document(self, employee_id: int) -> dict[str, Any] | None:
        employee = self.db.get_employee(employee_id)
        if not employee:
            return None
        document_type = str(employee.get("id_type") or "").strip()
        if document_type not in IDENTITY_TYPES:
            return None
        with self.db.connection() as conn:
            row = conn.execute(
                """SELECT id,employee_id,document_type,original_name,content_type,file_size,file_sha256,
                          start_date,expiry_date,notes,created_at
                   FROM employee_documents WHERE employee_id=? AND document_type=?
                   ORDER BY id DESC LIMIT 1""",
                (employee_id, document_type),
            ).fetchone()
            return dict(row) if row else None

    def document_path(self, document_id: int, actor: dict[str, Any] | None = None) -> Path:
        if actor is not None:
            self._require_hr(actor)
        with self.db.connection() as conn:
            row = conn.execute(
                """SELECT id,original_name,stored_path,file_data,file_sha256 FROM employee_documents WHERE id=?""",
                (document_id,),
            ).fetchone()
            if not row:
                raise DatabaseError("المستند غير موجود.")
            if row["file_data"] is not None:
                extension = Path(row["original_name"] or "document.bin").suffix.lower() or ".bin"
                digest = row["file_sha256"] or hashlib.sha256(bytes(row["file_data"])).hexdigest()
                logical_name = Path(str(row["stored_path"] or "")).name
                if not logical_name or logical_name == "database":
                    logical_name = f"document_{document_id}_{digest[:12]}{extension}"
                path = VIEW_CACHE_DIR / logical_name
                if not path.exists() or path.stat().st_size != len(row["file_data"]):
                    temp = path.with_suffix(path.suffix + ".partial")
                    temp.write_bytes(bytes(row["file_data"]))
                    try:
                        temp.chmod(0o600)
                    except OSError:
                        pass
                    atomic_replace(temp, path)
                return path
            stored_path = str(row["stored_path"] or "").strip()
        path = (DOCUMENTS_DIR / stored_path).resolve()
        if DOCUMENTS_DIR.resolve() not in path.parents or not path.is_file():
            raise DatabaseError("المستند غير موجود داخل قاعدة البيانات أو مجلد الإصدارات السابقة.")
        return path

    def delete_document(self, document_id: int, actor: dict[str, Any]) -> None:
        self._require_admin(actor)
        legacy_path: Path | None = None
        with self.db.connection() as conn:
            row = conn.execute(
                """SELECT d.id,d.stored_path,d.document_type,e.emp_code FROM employee_documents d
                   JOIN employees e ON e.id=d.employee_id WHERE d.id=?""", (document_id,),
            ).fetchone()
            if not row:
                return
            stored = str(row["stored_path"] or "")
            if stored and stored != "database":
                legacy_path = DOCUMENTS_DIR / stored
            conn.execute("DELETE FROM employee_documents WHERE id=?", (document_id,))
            self.db._audit_conn(conn, actor, "DELETE", "employee_document", document_id, {
                "emp_code": row["emp_code"], "type": row["document_type"], "storage": "sqlite_blob"
            })
        if legacy_path:
            try:
                legacy_path.unlink(missing_ok=True)
            except OSError:
                pass
        for cached in VIEW_CACHE_DIR.glob(f"document_{document_id}_*"):
            safe_unlink(cached)

    # ---------- Contracts ----------
    def add_contract(self, employee_id: int, data: dict[str, Any], actor: dict[str, Any]) -> int:
        self._require_hr(actor)
        if not self.db.get_employee(employee_id):
            raise DatabaseError("الموظف غير موجود.")
        start = _iso(data.get("start_date", ""), "تاريخ البداية")
        end = _iso(data["end_date"], "تاريخ النهاية") if str(data.get("end_date", "")).strip() else None
        probation = _iso(data["probation_end_date"], "نهاية التجربة") if str(data.get("probation_end_date", "")).strip() else None
        if end and end < start:
            raise ValueError("تاريخ نهاية العقد يسبق البداية.")
        if probation and probation < start:
            raise ValueError("نهاية فترة التجربة تسبق بداية العقد.")
        if probation:
            probation_days = (date.fromisoformat(probation) - date.fromisoformat(start)).days
            if probation_days > 180:
                raise ValueError(
                    "فترة التجربة المسجلة تتجاوز 180 يومًا. راجع الاستثناءات النظامية "
                    "وأدخل تاريخًا صحيحًا منصوصًا عليه في العقد."
                )
        contract_type = str(data.get("contract_type") or "محدد المدة").strip()
        status = str(data.get("status") or "ساري").strip()
        if contract_type not in CONTRACT_TYPES:
            raise ValueError("نوع العقد غير صحيح.")
        if status not in CONTRACT_STATUSES:
            raise ValueError("حالة العقد غير صحيحة.")
        salary = money(data.get("contract_salary") or 0)
        if salary < 0:
            raise ValueError("راتب العقد لا يمكن أن يكون سالبًا.")
        qiwa_status = str(data.get("qiwa_status") or "غير موثق").strip()
        if qiwa_status not in QIWA_CONTRACT_STATUSES:
            raise ValueError("حالة توثيق العقد في قوى غير صحيحة.")
        qiwa_number = str(data.get("qiwa_contract_number") or "").strip()
        qiwa_documented_at = (
            _iso(data["qiwa_documented_at"], "تاريخ توثيق قوى")
            if str(data.get("qiwa_documented_at") or "").strip()
            else None
        )
        if qiwa_status == "موثق" and (not qiwa_number or not qiwa_documented_at):
            raise ValueError("عند اختيار «موثق» يجب إدخال رقم عقد قوى وتاريخ التوثيق.")
        with self.db.connection() as conn:
            cur = conn.execute(
                """INSERT INTO contracts(employee_id,contract_number,contract_type,start_date,end_date,
                       probation_end_date,contract_salary,status,qiwa_status,qiwa_contract_number,
                       qiwa_documented_at,notes,document_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (employee_id, str(data.get("contract_number", "")).strip(), contract_type,
                 start, end, probation, float(salary), status, qiwa_status, qiwa_number,
                 qiwa_documented_at, str(data.get("notes", "")).strip(),
                 data.get("document_id")),
            )
            contract_id = int(cur.lastrowid)
            self.db._audit_conn(
                conn,
                actor,
                "CREATE",
                "contract",
                contract_id,
                {"employee_id": employee_id, "end_date": end, "qiwa_status": qiwa_status},
            )
            return contract_id

    def list_contracts(self, employee_id: int | None = None) -> list[dict[str, Any]]:
        where = "WHERE c.employee_id=?" if employee_id else ""
        params = (employee_id,) if employee_id else ()
        with self.db.connection() as conn:
            rows = conn.execute(
                f"""SELECT c.*, e.emp_code, e.name FROM contracts c JOIN employees e ON e.id=c.employee_id
                    {where} ORDER BY COALESCE(c.end_date,'9999-12-31'), c.id DESC""", params,
            ).fetchall()
            return [dict(r) for r in rows]

    def update_contract_compliance(
        self,
        contract_id: int,
        qiwa_status: str,
        qiwa_contract_number: str,
        qiwa_documented_at: str,
        actor: dict[str, Any],
    ) -> None:
        self._require_hr(actor)
        status = str(qiwa_status or "غير موثق").strip()
        if status not in QIWA_CONTRACT_STATUSES:
            raise ValueError("حالة توثيق العقد في قوى غير صحيحة.")
        number = str(qiwa_contract_number or "").strip()
        documented_at = (
            _iso(qiwa_documented_at, "تاريخ توثيق قوى")
            if str(qiwa_documented_at or "").strip()
            else None
        )
        if status == "موثق" and (not number or not documented_at):
            raise ValueError("عند اختيار «موثق» يجب إدخال رقم عقد قوى وتاريخ التوثيق.")
        with self.db.connection() as conn:
            row = conn.execute("SELECT id FROM contracts WHERE id=?", (contract_id,)).fetchone()
            if not row:
                raise DatabaseError("العقد غير موجود.")
            conn.execute(
                """UPDATE contracts SET qiwa_status=?,qiwa_contract_number=?,
                          qiwa_documented_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (status, number, documented_at, contract_id),
            )
            self.db._audit_conn(
                conn,
                actor,
                "UPDATE",
                "contract_qiwa_status",
                contract_id,
                {"qiwa_status": status, "has_qiwa_number": bool(number)},
            )

    def delete_contract(self, contract_id: int, actor: dict[str, Any]) -> None:
        self._require_admin(actor)
        with self.db.connection() as conn:
            conn.execute("DELETE FROM contracts WHERE id=?", (contract_id,))
            self.db._audit_conn(conn, actor, "DELETE", "contract", contract_id, {})

    # ---------- Leave balances ----------
    def set_leave_balance(self, employee_id: int, year: int, entitlement: float, carried: float,
                          adjustment: float, notes: str, actor: dict[str, Any]) -> None:
        self._require_hr(actor)
        if not 2000 <= int(year) <= 2200:
            raise ValueError("السنة غير صحيحة.")
        with self.db.connection() as conn:
            conn.execute(
                """INSERT INTO leave_balances(employee_id,balance_year,leave_type,entitlement,carried_forward,adjustment,notes)
                   VALUES(?,?,'سنوية',?,?,?,?)
                   ON CONFLICT(employee_id,balance_year,leave_type) DO UPDATE SET
                     entitlement=excluded.entitlement,carried_forward=excluded.carried_forward,
                     adjustment=excluded.adjustment,notes=excluded.notes,updated_at=CURRENT_TIMESTAMP""",
                (employee_id, int(year), float(entitlement), float(carried), float(adjustment), notes.strip()),
            )
            self.db._audit_conn(conn, actor, "UPDATE", "leave_balance", f"{employee_id}:{year}", {})

    def list_leave_balances(self, year: int) -> list[dict[str, Any]]:
        start, end = f"{int(year):04d}-01-01", f"{int(year):04d}-12-31"
        with self.db.connection() as conn:
            rows = conn.execute(
                """SELECT e.id employee_id,e.emp_code,e.name,e.department,
                          COALESCE(b.entitlement,30) entitlement,COALESCE(b.carried_forward,0) carried_forward,
                          COALESCE(b.adjustment,0) adjustment,COALESCE(b.notes,'') notes,
                          COALESCE((SELECT SUM(l.total_days) FROM leave_requests l
                                    WHERE l.employee_id=e.id AND l.leave_type='سنوية' AND l.status='معتمدة'
                                      AND NOT(l.end_date < ? OR l.start_date > ?)),0) used
                   FROM employees e LEFT JOIN leave_balances b
                     ON b.employee_id=e.id AND b.balance_year=? AND b.leave_type='سنوية'
                   WHERE e.status<>'منتهي الخدمة' ORDER BY e.name""", (start, end, int(year)),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["available"] = float(item["entitlement"]) + float(item["carried_forward"]) + float(item["adjustment"]) - float(item["used"])
            result.append(item)
        return result

    # ---------- Shifts ----------
    def list_shifts(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM shifts ORDER BY name").fetchall()]

    def add_shift(self, name: str, start_time: str, end_time: str, grace: int, actor: dict[str, Any]) -> int:
        self._require_hr(actor)
        for value in (start_time, end_time):
            try:
                datetime.strptime(value.strip(), "%H:%M")
            except ValueError as exc:
                raise ValueError("الوقت يجب أن يكون بصيغة HH:MM.") from exc
        with self.db.connection() as conn:
            cur = conn.execute(
                "INSERT INTO shifts(name,start_time,end_time,grace_minutes) VALUES(?,?,?,?)",
                (name.strip(), start_time.strip(), end_time.strip(), max(0, int(grace))),
            )
            shift_id = int(cur.lastrowid)
            self.db._audit_conn(conn, actor, "CREATE", "shift", shift_id, {"name": name})
            return shift_id

    def assign_shift(self, employee_id: int, shift_id: int, effective_from: str,
                     effective_to: str, actor: dict[str, Any]) -> None:
        self._require_hr(actor)
        start = _iso(effective_from, "تاريخ السريان")
        end = _iso(effective_to, "تاريخ النهاية") if effective_to.strip() else None
        with self.db.connection() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO employee_shifts(employee_id,shift_id,effective_from,effective_to) VALUES(?,?,?,?)",
                (employee_id, shift_id, start, end),
            )
            self.db._audit_conn(conn, actor, "ASSIGN", "shift", shift_id, {"employee_id": employee_id, "from": start})

    def list_shift_assignments(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """SELECT es.id,e.emp_code,e.name,s.name shift_name,s.start_time,s.end_time,
                          es.effective_from,es.effective_to
                   FROM employee_shifts es JOIN employees e ON e.id=es.employee_id
                   JOIN shifts s ON s.id=es.shift_id ORDER BY es.effective_from DESC,e.name"""
            ).fetchall()
            return [dict(r) for r in rows]


    # ---------- Attendance import ----------
    def import_attendance(self, records: list[dict[str, Any]], actor: dict[str, Any]) -> dict[str, Any]:
        self._require_hr(actor)
        result = {"total": len(records), "created": 0, "updated": 0, "failed": 0, "errors": []}
        with self.db.connection() as conn:
            employees = {str(r["emp_code"]).upper(): int(r["id"]) for r in conn.execute("SELECT id,emp_code FROM employees")}
            for record in records:
                row_num = record.get("_source_row", "?")
                try:
                    code = str(record.get("emp_code", "")).strip().upper()
                    employee_id = employees.get(code)
                    if not employee_id:
                        raise ValueError(f"رقم الموظف {code} غير موجود")
                    attendance_date = _iso(str(record.get("attendance_date", "")), "تاريخ الحضور")
                    status = str(record.get("status", "")).strip()
                    if status not in ATTENDANCE_STATUSES:
                        raise ValueError(f"حالة غير صحيحة: {status}")
                    check_in, check_in_minutes = self.db._clock_minutes(
                        str(record.get("check_in_time", "")), "وقت الحضور"
                    )
                    check_out, check_out_minutes = self.db._clock_minutes(
                        str(record.get("check_out_time", "")), "وقت الانصراف"
                    )
                    late_minutes, worked_minutes = self.db._attendance_metrics(
                        conn, employee_id, attendance_date, check_in_minutes, check_out_minutes
                    )
                    exists = conn.execute(
                        "SELECT id FROM attendance WHERE employee_id=? AND attendance_date=?",
                        (employee_id, attendance_date),
                    ).fetchone()
                    conn.execute(
                        """INSERT INTO attendance(
                               employee_id,attendance_date,status,check_in_time,check_out_time,
                               late_minutes,worked_minutes,note
                           ) VALUES(?,?,?,?,?,?,?,?)
                           ON CONFLICT(employee_id,attendance_date) DO UPDATE SET
                           status=excluded.status,check_in_time=excluded.check_in_time,
                           check_out_time=excluded.check_out_time,late_minutes=excluded.late_minutes,
                           worked_minutes=excluded.worked_minutes,note=excluded.note,
                           updated_at=CURRENT_TIMESTAMP""",
                        (
                            employee_id,
                            attendance_date,
                            status,
                            check_in or None,
                            check_out or None,
                            late_minutes,
                            worked_minutes,
                            str(record.get("note", "")).strip(),
                        ),
                    )
                    result["updated" if exists else "created"] += 1
                except Exception as exc:
                    result["failed"] += 1
                    result["errors"].append(f"الصف {row_num}: {exc}")
            self.db._audit_conn(conn, actor, "IMPORT", "attendance", "batch", {
                "total": result["total"], "created": result["created"],
                "updated": result["updated"], "failed": result["failed"],
            })
        return result

    # ---------- Company holidays ----------
    def list_holidays(self, year: int | None = None) -> list[dict[str, Any]]:
        where = "WHERE substr(holiday_date,1,4)=?" if year else ""
        params = (str(int(year)),) if year else ()
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(
                f"SELECT holiday_date,name,notes FROM company_holidays {where} ORDER BY holiday_date", params
            ).fetchall()]

    def add_holiday(self, holiday_date: str, name: str, notes: str, actor: dict[str, Any]) -> None:
        self._require_hr(actor)
        normalized = _iso(holiday_date, "تاريخ العطلة")
        if not name.strip():
            raise ValueError("اسم العطلة مطلوب.")
        with self.db.connection() as conn:
            conn.execute(
                """INSERT INTO company_holidays(holiday_date,name,notes) VALUES(?,?,?)
                   ON CONFLICT(holiday_date) DO UPDATE SET name=excluded.name,notes=excluded.notes""",
                (normalized, name.strip(), notes.strip()),
            )
            self.db._audit_conn(conn, actor, "UPSERT", "holiday", normalized, {"name": name.strip()})

    def delete_holiday(self, holiday_date: str, actor: dict[str, Any]) -> None:
        self._require_admin(actor)
        normalized = _iso(holiday_date, "تاريخ العطلة")
        with self.db.connection() as conn:
            conn.execute("DELETE FROM company_holidays WHERE holiday_date=?", (normalized,))
            self.db._audit_conn(conn, actor, "DELETE", "holiday", normalized, {})

    # ---------- Allowances ----------
    @staticmethod
    def _allowance_code(name: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", str(name).strip().lower()).strip("_")
        if normalized:
            return normalized[:40]
        digest = hashlib.sha1(str(name).encode("utf-8")).hexdigest()[:10]
        return f"custom_{digest}"

    def list_allowance_types(self, active_only: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM allowance_types"
        params: tuple[Any, ...] = ()
        if active_only:
            sql += " WHERE is_active=1"
        sql += " ORDER BY sort_order,name"
        with self.db.connection() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def save_allowance_type(self, name: str, actor: dict[str, Any], type_id: int | None = None) -> int:
        self._require_hr(actor)
        clean_name = str(name).strip()
        if not clean_name:
            raise ValueError("اسم البدل مطلوب.")
        with self.db.connection() as conn:
            if type_id:
                current = conn.execute("SELECT * FROM allowance_types WHERE id=?", (type_id,)).fetchone()
                if not current:
                    raise DatabaseError("نوع البدل غير موجود.")
                conn.execute(
                    "UPDATE allowance_types SET name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (clean_name, type_id),
                )
                self.db._audit_conn(conn, actor, "UPDATE", "allowance_type", type_id, {"name": clean_name})
                return int(type_id)
            code = self._allowance_code(clean_name)
            base_code = code
            counter = 2
            while conn.execute("SELECT 1 FROM allowance_types WHERE code=?", (code,)).fetchone():
                code = f"{base_code}_{counter}"
                counter += 1
            try:
                cur = conn.execute(
                    "INSERT INTO allowance_types(code,name,sort_order) VALUES(?,?,?)",
                    (code, clean_name, 500),
                )
            except sqlite3.IntegrityError as exc:
                raise DatabaseError("يوجد نوع بدل بالاسم نفسه.") from exc
            type_id = int(cur.lastrowid)
            self.db._audit_conn(conn, actor, "CREATE", "allowance_type", type_id, {"name": clean_name})
            return type_id

    def set_allowance_type_active(self, type_id: int, is_active: bool, actor: dict[str, Any]) -> None:
        self._require_hr(actor)
        with self.db.connection() as conn:
            if not conn.execute("SELECT 1 FROM allowance_types WHERE id=?", (type_id,)).fetchone():
                raise DatabaseError("نوع البدل غير موجود.")
            conn.execute(
                "UPDATE allowance_types SET is_active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (1 if is_active else 0, type_id),
            )
            self.db._audit_conn(
                conn, actor, "ACTIVATE" if is_active else "DEACTIVATE", "allowance_type", type_id, {}
            )

    @staticmethod
    def _validate_allowance_dates(effective_from: str, effective_to: str | None) -> tuple[str, str | None]:
        start = _iso(effective_from, "تاريخ بداية البدل")
        end = _iso(effective_to, "تاريخ نهاية البدل") if str(effective_to or "").strip() else None
        if end and end < start:
            raise ValueError("تاريخ نهاية البدل يجب ألا يسبق تاريخ البداية.")
        return start, end

    def save_employee_allowance(
        self,
        employee_id: int,
        allowance_type_id: int,
        calculation_method: str,
        value: float,
        effective_from: str,
        effective_to: str | None,
        notes: str,
        actor: dict[str, Any],
        allowance_id: int | None = None,
    ) -> int:
        self._require_hr(actor)
        method = str(calculation_method).strip().lower()
        if method not in {"fixed", "percentage"}:
            raise ValueError("طريقة احتساب البدل غير صحيحة.")
        amount = float(value)
        if amount < 0:
            raise ValueError("قيمة البدل لا يمكن أن تكون سالبة.")
        if method == "percentage" and amount > 1000:
            raise ValueError("نسبة البدل غير منطقية.")
        start, end = self._validate_allowance_dates(effective_from, effective_to)
        with self.db.connection() as conn:
            if not conn.execute("SELECT 1 FROM employees WHERE id=?", (employee_id,)).fetchone():
                raise DatabaseError("الموظف غير موجود.")
            if not conn.execute("SELECT 1 FROM allowance_types WHERE id=?", (allowance_type_id,)).fetchone():
                raise DatabaseError("نوع البدل غير موجود.")
            overlap = conn.execute(
                """SELECT id FROM employee_allowances
                   WHERE employee_id=? AND allowance_type_id=? AND is_active=1
                     AND (? IS NULL OR effective_from<=?)
                     AND (effective_to IS NULL OR effective_to>=?)
                     AND (? IS NULL OR id<>?)
                   LIMIT 1""",
                (employee_id, allowance_type_id, end, end or "9999-12-31", start,
                 allowance_id, allowance_id),
            ).fetchone()
            if overlap:
                raise DatabaseError("يوجد بدل من النوع نفسه متداخل مع هذه الفترة.")
            if allowance_id:
                current = conn.execute("SELECT 1 FROM employee_allowances WHERE id=?", (allowance_id,)).fetchone()
                if not current:
                    raise DatabaseError("بدل الموظف غير موجود.")
                conn.execute(
                    """UPDATE employee_allowances SET employee_id=?,allowance_type_id=?,calculation_method=?,
                       value=?,effective_from=?,effective_to=?,notes=?,is_active=1,updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (employee_id, allowance_type_id, method, amount, start, end,
                     str(notes).strip(), allowance_id),
                )
                record_id = int(allowance_id)
                action = "UPDATE"
            else:
                cur = conn.execute(
                    """INSERT INTO employee_allowances(employee_id,allowance_type_id,calculation_method,
                       value,effective_from,effective_to,notes) VALUES(?,?,?,?,?,?,?)""",
                    (employee_id, allowance_type_id, method, amount, start, end, str(notes).strip()),
                )
                record_id = int(cur.lastrowid)
                action = "CREATE"
            self.db._audit_conn(
                conn, actor, action, "employee_allowance", record_id,
                {"employee_id": employee_id, "allowance_type_id": allowance_type_id,
                 "method": method, "value": amount, "from": start, "to": end},
            )
            return record_id

    def list_employee_allowances(
        self, employee_id: int | None = None, include_inactive: bool = True
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if employee_id is not None:
            conditions.append("ea.employee_id=?")
            params.append(employee_id)
        if not include_inactive:
            conditions.append("ea.is_active=1")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.db.connection() as conn:
            rows = conn.execute(
                f"""SELECT ea.*,at.name allowance_name,at.code allowance_code,e.emp_code,e.name employee_name,
                    ROUND(CASE WHEN ea.calculation_method='percentage'
                         THEN e.salary*ea.value/100.0 ELSE ea.value END,2) current_amount
                    FROM employee_allowances ea
                    JOIN allowance_types at ON at.id=ea.allowance_type_id
                    JOIN employees e ON e.id=ea.employee_id
                    {where}
                    ORDER BY e.name,at.sort_order,at.name,ea.effective_from DESC""",
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    def set_employee_allowance_active(self, allowance_id: int, is_active: bool, actor: dict[str, Any]) -> None:
        self._require_hr(actor)
        with self.db.connection() as conn:
            if not conn.execute("SELECT 1 FROM employee_allowances WHERE id=?", (allowance_id,)).fetchone():
                raise DatabaseError("بدل الموظف غير موجود.")
            conn.execute(
                "UPDATE employee_allowances SET is_active=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (1 if is_active else 0, allowance_id),
            )
            self.db._audit_conn(
                conn, actor, "ACTIVATE" if is_active else "DEACTIVATE", "employee_allowance", allowance_id, {}
            )

    def payroll_allowance_breakdown(self, payroll_item_id: int) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """SELECT * FROM payroll_allowance_items
                   WHERE payroll_item_id=? ORDER BY id""",
                (payroll_item_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_payroll_allowance_details(self, run_id: int) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """SELECT pai.*,e.emp_code,e.name employee_name,r.period
                   FROM payroll_allowance_items pai
                   JOIN payroll_items pi ON pi.id=pai.payroll_item_id
                   JOIN payroll_runs r ON r.id=pi.payroll_run_id
                   JOIN employees e ON e.id=pi.employee_id
                   WHERE pi.payroll_run_id=?
                   ORDER BY e.name,pai.id""",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Payroll ----------
    def payroll_policy(self, conn: sqlite3.Connection | None = None) -> dict[str, Decimal]:
        def read(active_conn: sqlite3.Connection) -> dict[str, Decimal]:
            rows = active_conn.execute(
                """SELECT key,value FROM app_settings
                   WHERE key IN ('payroll_days_divisor','payroll_daily_hours',
                                 'payroll_overtime_multiplier')"""
            ).fetchall()
            values = {row["key"]: money(row["value"]) for row in rows}
            policy = {
                "days_divisor": values.get("payroll_days_divisor", money("30")),
                "daily_hours": values.get("payroll_daily_hours", money("8")),
                "overtime_multiplier": values.get(
                    "payroll_overtime_multiplier", money("1.5")
                ),
            }
            if min(policy.values()) <= 0:
                raise DatabaseError("سياسة حساب الرواتب تحتوي قيمة غير موجبة.")
            return policy

        if conn is not None:
            return read(conn)
        with self.db.connection() as active_conn:
            return read(active_conn)

    def set_payroll_policy(
        self,
        days_divisor: float,
        daily_hours: float,
        overtime_multiplier: float,
        actor: dict[str, Any],
    ) -> None:
        self._require_admin(actor)
        values = {
            "payroll_days_divisor": money(days_divisor),
            "payroll_daily_hours": money(daily_hours),
            "payroll_overtime_multiplier": money(overtime_multiplier),
        }
        if min(values.values()) <= 0:
            raise ValueError("قيم سياسة الرواتب يجب أن تكون أكبر من صفر.")
        with self.db.connection() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM payroll_adjustment_requests WHERE status='معلق'"
            ).fetchone()[0]
            if pending:
                raise DatabaseError(
                    "لا يمكن تغيير سياسة الحساب مع وجود طلبات تعديل معلقة. "
                    "اعتمد الطلبات أو ارفضها أولًا."
                )
            conn.executemany(
                """INSERT INTO app_settings(key,value) VALUES(?,?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                [(key, str(value)) for key, value in values.items()],
            )
            self.db._audit_conn(
                conn,
                actor,
                "UPDATE",
                "payroll_policy",
                "salary_based_calculation",
                {key: money_float(value) for key, value in values.items()},
            )

    @staticmethod
    def _salary_based_amounts(
        *,
        base_salary: Decimal,
        overtime_hours: Decimal,
        overtime_multiplier: Decimal,
        absence_days: Decimal,
        late_minutes: Decimal,
        unpaid_leave_days: Decimal,
        policy: dict[str, Decimal],
    ) -> dict[str, Decimal]:
        base = money(base_salary)
        hours = money(overtime_hours)
        multiplier = money(overtime_multiplier)
        absence = money(absence_days)
        late = money(late_minutes)
        unpaid = money(unpaid_leave_days)
        if min(base, hours, multiplier, absence, late, unpaid) < 0:
            raise ValueError("قيم حساب الرواتب لا يمكن أن تكون سالبة.")
        day_rate = money(base / policy["days_divisor"])
        hour_rate = money(day_rate / policy["daily_hours"])
        return {
            "day_rate": day_rate,
            "hour_rate": hour_rate,
            "overtime": money(hours * hour_rate * multiplier),
            "absence_deduction": money(absence * day_rate),
            "late_deduction": money((late / Decimal("60")) * hour_rate),
            "unpaid_leave_deduction": money(unpaid * day_rate),
        }

    @staticmethod
    def _period_range(period: str) -> tuple[date, date]:
        try:
            start = datetime.strptime(period, "%Y-%m").date().replace(day=1)
        except ValueError as exc:
            raise ValueError("الفترة يجب أن تكون بصيغة YYYY-MM.") from exc
        end = start.replace(day=calendar.monthrange(start.year, start.month)[1])
        return start, end

    def generate_payroll(self, period: str, actor: dict[str, Any]) -> int:
        self._require_hr(actor)
        start, end = self._period_range(period)
        with self.db.connection() as conn:
            existing = conn.execute("SELECT * FROM payroll_runs WHERE period=?", (period,)).fetchone()
            if existing and existing["status"] == "معتمد":
                raise DatabaseError("مسير هذه الفترة معتمد ولا يمكن إعادة إنشائه.")

            preserved: dict[int, dict[str, Any]] = {}
            if existing:
                run_id = int(existing["id"])
                adjustment_count = conn.execute(
                    "SELECT COUNT(*) FROM payroll_adjustment_requests WHERE payroll_run_id=?",
                    (run_id,),
                ).fetchone()[0]
                if adjustment_count:
                    raise DatabaseError(
                        "لا يمكن إعادة إنشاء مسير بدأ عليه مسار اعتماد. "
                        "حفاظًا على سجل القرارات، أنشئ فترة جديدة أو أكمل اعتماد المسير الحالي."
                    )
                old_rows = conn.execute(
                    """SELECT employee_id,manual_allowances,overtime,overtime_hours,
                              overtime_multiplier,manual_deductions,notes,
                              apply_absence_deduction,absence_rate,
                              apply_late_deduction,late_hour_rate,
                              apply_unpaid_leave_deduction,unpaid_leave_rate
                       FROM payroll_items WHERE payroll_run_id=?""",
                    (run_id,),
                ).fetchall()
                preserved = {int(r["employee_id"]): dict(r) for r in old_rows}
                conn.execute("DELETE FROM payroll_items WHERE payroll_run_id=?", (run_id,))
                conn.execute(
                    "UPDATE payroll_runs SET generated_by=?,generated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (actor.get("id"), run_id),
                )
            else:
                cur = conn.execute("INSERT INTO payroll_runs(period,generated_by) VALUES(?,?)", (period, actor.get("id")))
                run_id = int(cur.lastrowid)

            policy = self.payroll_policy(conn)
            employees = conn.execute(
                "SELECT id,emp_code,name,salary FROM employees WHERE status<>'منتهي الخدمة' ORDER BY id"
            ).fetchall()
            missing_salary = [
                f"{row['emp_code']} - {row['name']}" for row in employees
                if row["salary"] is None
            ]
            if missing_salary:
                raise DatabaseError(
                    "لا يمكن إنشاء مسير راتب بقيم افتراضية. أكمل راتب الموظفين التاليين أولًا: "
                    + "، ".join(missing_salary[:10])
                    + (f"، و{len(missing_salary) - 10} آخرين" if len(missing_salary) > 10 else "")
                )
            for emp in employees:
                employee_id = int(emp["id"])
                absent = conn.execute(
                    "SELECT COUNT(*) FROM attendance WHERE employee_id=? AND status='غائب' AND attendance_date BETWEEN ? AND ?",
                    (employee_id, start.isoformat(), end.isoformat()),
                ).fetchone()[0]
                unpaid = conn.execute(
                    """SELECT COALESCE(SUM(total_days),0) FROM leave_requests
                       WHERE employee_id=? AND leave_type='بدون راتب' AND status='معتمدة'
                         AND NOT(end_date < ? OR start_date > ?)""",
                    (employee_id, start.isoformat(), end.isoformat()),
                ).fetchone()[0]
                late_minutes = Decimal(
                    str(
                    conn.execute(
                        """SELECT COALESCE(SUM(late_minutes),0) FROM attendance
                           WHERE employee_id=? AND attendance_date BETWEEN ? AND ?""",
                        (employee_id, start.isoformat(), end.isoformat()),
                    ).fetchone()[0]
                    or 0
                    )
                )
                base = money(emp["salary"])

                assigned = conn.execute(
                    """SELECT ea.allowance_type_id,ea.calculation_method,ea.value,at.name
                       FROM employee_allowances ea
                       JOIN allowance_types at ON at.id=ea.allowance_type_id
                       WHERE ea.employee_id=? AND ea.is_active=1 AND at.is_active=1
                         AND ea.effective_from<=?
                         AND (ea.effective_to IS NULL OR ea.effective_to>=?)
                       ORDER BY at.sort_order,at.name""",
                    (employee_id, end.isoformat(), start.isoformat()),
                ).fetchall()
                breakdown: list[tuple[int | None, str, str, float, float]] = []
                structured_allowances = money(0)
                for row in assigned:
                    source_value = money(row["value"])
                    amount = money(
                        base * source_value / Decimal("100")
                        if row["calculation_method"] == "percentage"
                        else source_value
                    )
                    structured_allowances = money(structured_allowances + amount)
                    breakdown.append(
                        (int(row["allowance_type_id"]), str(row["name"]),
                         str(row["calculation_method"]), money_float(source_value), money_float(amount))
                    )

                old = preserved.get(employee_id, {})
                manual_allowances = money(old.get("manual_allowances") or 0)
                overtime_hours = money(old.get("overtime_hours") or 0)
                overtime_multiplier = money(
                    old.get("overtime_multiplier") or policy["overtime_multiplier"]
                )
                overtime = (
                    self._salary_based_amounts(
                        base_salary=base,
                        overtime_hours=overtime_hours,
                        overtime_multiplier=overtime_multiplier,
                        absence_days=0,
                        late_minutes=0,
                        unpaid_leave_days=0,
                        policy=policy,
                    )["overtime"]
                    if overtime_hours > 0
                    else money(old.get("overtime") or 0)
                )
                manual_deductions = money(old.get("manual_deductions") or 0)
                apply_absence = bool(old.get("apply_absence_deduction") or 0)
                absence_rate = money(old.get("absence_rate") or 0)
                apply_late = bool(old.get("apply_late_deduction") or 0)
                late_hour_rate = money(old.get("late_hour_rate") or 0)
                apply_unpaid_leave = bool(old.get("apply_unpaid_leave_deduction") or 0)
                unpaid_leave_rate = money(old.get("unpaid_leave_rate") or 0)
                absence_deduction = (
                    money(Decimal(str(absent)) * absence_rate) if apply_absence else money(0)
                )
                late_deduction = (
                    money((late_minutes / Decimal("60")) * late_hour_rate)
                    if apply_late else money(0)
                )
                unpaid_leave_deduction = (
                    money(Decimal(str(unpaid)) * unpaid_leave_rate)
                    if apply_unpaid_leave else money(0)
                )
                notes = str(old.get("notes") or "")
                total_allowances = money(structured_allowances + manual_allowances)
                net = max(
                    money(0),
                    money(
                        base + total_allowances + overtime - manual_deductions
                        - absence_deduction - late_deduction - unpaid_leave_deduction
                    ),
                )
                cur = conn.execute(
                    """INSERT INTO payroll_items(payroll_run_id,employee_id,base_salary,allowances,
                           manual_allowances,overtime,overtime_hours,overtime_multiplier,
                           manual_deductions,absence_days,unpaid_leave_days,
                           apply_unpaid_leave_deduction,unpaid_leave_rate,unpaid_leave_deduction,
                           apply_absence_deduction,absence_rate,absence_deduction,late_minutes,
                           apply_late_deduction,late_hour_rate,late_deduction,net_salary,notes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        run_id, employee_id, money_float(base), money_float(total_allowances),
                        money_float(manual_allowances), money_float(overtime),
                        money_float(overtime_hours), money_float(overtime_multiplier),
                        money_float(manual_deductions), absent, unpaid,
                        int(apply_unpaid_leave), money_float(unpaid_leave_rate),
                        money_float(unpaid_leave_deduction), int(apply_absence),
                        money_float(absence_rate), money_float(absence_deduction),
                        float(late_minutes), int(apply_late), money_float(late_hour_rate),
                        money_float(late_deduction), money_float(net), notes,
                    ),
                )
                payroll_item_id = int(cur.lastrowid)
                if manual_allowances:
                    breakdown.append(
                        (
                            None, "بدلات إضافية/يدوية", "fixed",
                            money_float(manual_allowances), money_float(manual_allowances),
                        )
                    )
                conn.executemany(
                    """INSERT INTO payroll_allowance_items(payroll_item_id,allowance_type_id,
                       allowance_name,calculation_method,source_value,amount) VALUES(?,?,?,?,?,?)""",
                    [(payroll_item_id, *row) for row in breakdown],
                )
            self.db._audit_conn(
                conn, actor, "GENERATE", "payroll", run_id,
                {"period": period, "employees": len(employees)},
            )
            return run_id

    def list_payroll_runs(self) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """SELECT r.*,COUNT(i.id) employee_count,COALESCE(SUM(i.net_salary),0) total_net,
                          (SELECT COUNT(*) FROM payroll_adjustment_requests a
                           WHERE a.payroll_run_id=r.id AND a.status='معلق') pending_adjustments,
                          (SELECT COUNT(*) FROM payroll_adjustment_requests a
                           WHERE a.payroll_run_id=r.id AND a.status='معتمد') approved_adjustments
                   FROM payroll_runs r LEFT JOIN payroll_items i ON i.payroll_run_id=r.id
                   GROUP BY r.id ORDER BY r.period DESC"""
            ).fetchall()
            return [dict(r) for r in rows]

    def list_payroll_items(self, run_id: int) -> list[dict[str, Any]]:
        with self.db.connection() as conn:
            rows = conn.execute(
                """SELECT i.*,e.emp_code,e.name,e.department,
                          a.id adjustment_request_id,a.status adjustment_status,
                          a.proposed_base_salary,a.proposed_manual_allowances,
                          a.proposed_overtime,a.proposed_overtime_hours,
                          a.proposed_overtime_multiplier,a.proposed_manual_deductions,
                          a.proposed_apply_absence,a.proposed_absence_rate,
                          a.proposed_absence_deduction,a.proposed_apply_late,
                          a.proposed_late_hour_rate,a.proposed_late_deduction,
                          a.proposed_apply_unpaid_leave,a.proposed_unpaid_leave_rate,
                          a.proposed_unpaid_leave_deduction,a.proposed_total_allowances,
                          a.proposed_net_salary,a.request_notes,a.requested_at
                   FROM payroll_items i
                   JOIN employees e ON e.id=i.employee_id
                   LEFT JOIN payroll_adjustment_requests a ON a.id=(
                       SELECT latest.id FROM payroll_adjustment_requests latest
                       WHERE latest.payroll_item_id=i.id ORDER BY latest.id DESC LIMIT 1
                   )
                   WHERE i.payroll_run_id=? ORDER BY e.name""",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    def _structured_allowance_proposal(
        conn: sqlite3.Connection,
        payroll_item_id: int,
        base_salary: Decimal,
    ) -> tuple[Decimal, list[tuple[int, Decimal]]]:
        rows = conn.execute(
            """SELECT id,calculation_method,source_value,amount
               FROM payroll_allowance_items
               WHERE payroll_item_id=? AND allowance_type_id IS NOT NULL
               ORDER BY id""",
            (payroll_item_id,),
        ).fetchall()
        total = money(0)
        updates: list[tuple[int, Decimal]] = []
        for row in rows:
            source = money(row["source_value"])
            amount = (
                money(base_salary * source / Decimal("100"))
                if row["calculation_method"] == "percentage"
                else money(row["amount"])
            )
            total = money(total + amount)
            updates.append((int(row["id"]), amount))
        return total, updates

    def submit_payroll_adjustment(
        self,
        item_id: int,
        base_salary: float,
        manual_allowances: float,
        overtime_hours: float,
        manual_deductions: float,
        notes: str,
        actor: dict[str, Any],
        *,
        apply_absence_deduction: bool = False,
        apply_late_deduction: bool = False,
        apply_unpaid_leave_deduction: bool = False,
        overtime_multiplier: float | None = None,
    ) -> dict[str, Any]:
        """يحفظ طلبًا معلّقًا ولا يغير أي مبلغ فعلي قبل اعتماد المدير."""
        self._require_hr(actor)
        proposed_base = money(base_salary)
        allowance_value = money(manual_allowances)
        overtime_hours_value = money(overtime_hours)
        deductions_value = money(manual_deductions)
        if min(proposed_base, allowance_value, overtime_hours_value, deductions_value) < 0:
            raise ValueError("القيم المالية لا يمكن أن تكون سالبة.")
        with self.db.connection() as conn:
            # Serialize request edits with manager decisions.  A double click or
            # two UI workers must never update the same pending request concurrently.
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """SELECT i.*,r.status run_status,r.period,e.name employee_name
                   FROM payroll_items i
                   JOIN payroll_runs r ON r.id=i.payroll_run_id
                   JOIN employees e ON e.id=i.employee_id WHERE i.id=?""",
                (item_id,),
            ).fetchone()
            if not row:
                raise DatabaseError("بند الراتب غير موجود.")
            if row["run_status"] == "معتمد":
                raise DatabaseError("لا يمكن تعديل مسير معتمد.")
            policy = self.payroll_policy(conn)
            multiplier = (
                policy["overtime_multiplier"]
                if overtime_multiplier is None
                else money(overtime_multiplier)
            )
            if multiplier <= 0:
                raise ValueError("معامل الإضافي يجب أن يكون أكبر من صفر.")
            calculated = self._salary_based_amounts(
                base_salary=proposed_base,
                overtime_hours=overtime_hours_value,
                overtime_multiplier=multiplier,
                absence_days=money(row["absence_days"] or 0),
                late_minutes=money(row["late_minutes"] or 0),
                unpaid_leave_days=money(row["unpaid_leave_days"] or 0),
                policy=policy,
            )
            absence_deduction = (
                calculated["absence_deduction"] if apply_absence_deduction else money(0)
            )
            late_deduction = (
                calculated["late_deduction"] if apply_late_deduction else money(0)
            )
            unpaid_leave_deduction = (
                calculated["unpaid_leave_deduction"]
                if apply_unpaid_leave_deduction
                else money(0)
            )
            structured, _ = self._structured_allowance_proposal(
                conn, item_id, proposed_base
            )
            total_allowances = money(structured + allowance_value)
            net = max(
                money(0),
                money(
                    proposed_base + total_allowances + calculated["overtime"]
                    - deductions_value - absence_deduction - late_deduction
                    - unpaid_leave_deduction
                ),
            )
            values = (
                money_float(proposed_base), money_float(allowance_value),
                money_float(calculated["overtime"]), money_float(overtime_hours_value),
                money_float(multiplier), money_float(deductions_value),
                int(bool(apply_absence_deduction)), money_float(calculated["day_rate"]),
                money_float(absence_deduction), int(bool(apply_late_deduction)),
                money_float(calculated["hour_rate"]), money_float(late_deduction),
                int(bool(apply_unpaid_leave_deduction)), money_float(calculated["day_rate"]),
                money_float(unpaid_leave_deduction), money_float(total_allowances),
                money_float(net), notes.strip(), actor.get("id"),
            )
            pending = conn.execute(
                """SELECT id FROM payroll_adjustment_requests
                   WHERE payroll_item_id=? AND status='معلق'""",
                (item_id,),
            ).fetchone()
            if pending:
                request_id = int(pending["id"])
                conn.execute(
                    """UPDATE payroll_adjustment_requests SET
                       proposed_base_salary=?,proposed_manual_allowances=?,
                       proposed_overtime=?,proposed_overtime_hours=?,
                       proposed_overtime_multiplier=?,proposed_manual_deductions=?,
                       proposed_apply_absence=?,proposed_absence_rate=?,
                       proposed_absence_deduction=?,proposed_apply_late=?,
                       proposed_late_hour_rate=?,proposed_late_deduction=?,
                       proposed_apply_unpaid_leave=?,proposed_unpaid_leave_rate=?,
                       proposed_unpaid_leave_deduction=?,proposed_total_allowances=?,
                       proposed_net_salary=?,request_notes=?,requested_by=?,
                       requested_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (*values, request_id),
                )
                action = "UPDATE_REQUEST"
            else:
                cur = conn.execute(
                    """INSERT INTO payroll_adjustment_requests(
                       payroll_item_id,payroll_run_id,employee_id,
                       proposed_base_salary,proposed_manual_allowances,
                       proposed_overtime,proposed_overtime_hours,
                       proposed_overtime_multiplier,proposed_manual_deductions,
                       proposed_apply_absence,proposed_absence_rate,
                       proposed_absence_deduction,proposed_apply_late,
                       proposed_late_hour_rate,proposed_late_deduction,
                       proposed_apply_unpaid_leave,proposed_unpaid_leave_rate,
                       proposed_unpaid_leave_deduction,proposed_total_allowances,
                       proposed_net_salary,request_notes,requested_by)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        item_id, int(row["payroll_run_id"]), int(row["employee_id"]),
                        *values,
                    ),
                )
                request_id = int(cur.lastrowid)
                action = "REQUEST"
            self.db._audit_conn(
                conn, actor, action, "payroll_adjustment", request_id,
                {
                    "payroll_item_id": item_id,
                    "period": row["period"],
                    "employee": row["employee_name"],
                    "current_net": money_float(money(row["net_salary"])),
                    "proposed_net": money_float(net),
                    "delta": money_float(money(net - money(row["net_salary"]))),
                },
            )
            return {
                "request_id": request_id,
                "status": "معلق",
                "current_net": money_float(money(row["net_salary"])),
                "proposed_net": money_float(net),
                "delta": money_float(money(net - money(row["net_salary"]))),
                "day_rate": money_float(calculated["day_rate"]),
                "hour_rate": money_float(calculated["hour_rate"]),
            }

    def list_payroll_adjustments(
        self,
        run_id: int | None = None,
        status: str | None = "معلق",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if run_id is not None:
            clauses.append("a.payroll_run_id=?")
            values.append(run_id)
        if status is not None:
            if status not in {"معلق", "معتمد", "مرفوض"}:
                raise ValueError("حالة طلب التعديل غير صحيحة.")
            clauses.append("a.status=?")
            values.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.db.connection() as conn:
            rows = conn.execute(
                f"""SELECT a.*,r.period,e.emp_code,e.name employee_name,
                           i.base_salary current_base_salary,i.net_salary current_net_salary,
                           requester.full_name requested_by_name,
                           decider.full_name decided_by_name
                    FROM payroll_adjustment_requests a
                    JOIN payroll_runs r ON r.id=a.payroll_run_id
                    JOIN payroll_items i ON i.id=a.payroll_item_id
                    JOIN employees e ON e.id=a.employee_id
                    LEFT JOIN users requester ON requester.id=a.requested_by
                    LEFT JOIN users decider ON decider.id=a.decided_by
                    {where}
                    ORDER BY CASE a.status WHEN 'معلق' THEN 0 ELSE 1 END,
                             a.requested_at DESC,a.id DESC""",
                values,
            ).fetchall()
            return [dict(row) for row in rows]

    def decide_payroll_adjustment(
        self,
        request_id: int,
        approve: bool,
        actor: dict[str, Any],
        decision_notes: str = "",
    ) -> dict[str, Any]:
        """يعتمد أو يرفض طلبًا؛ الاعتماد فقط يغير بند المسير داخل معاملة واحدة."""
        self._require_admin(actor)
        with self.db.connection() as conn:
            # Acquire the SQLite writer lock before reading the pending state.  This
            # turns the status check plus all payroll changes into one decision.
            conn.execute("BEGIN IMMEDIATE")
            request = conn.execute(
                """SELECT a.*,r.status run_status,r.period,
                          i.absence_days,i.late_minutes,i.unpaid_leave_days,
                          i.net_salary current_net
                   FROM payroll_adjustment_requests a
                   JOIN payroll_runs r ON r.id=a.payroll_run_id
                   JOIN payroll_items i ON i.id=a.payroll_item_id
                   WHERE a.id=?""",
                (request_id,),
            ).fetchone()
            if not request:
                raise DatabaseError("طلب تعديل الراتب غير موجود.")
            if request["status"] != "معلق":
                raise DatabaseError("تم اتخاذ قرار سابق على هذا الطلب.")
            if request["run_status"] == "معتمد":
                raise DatabaseError("لا يمكن اتخاذ قرار على طلب داخل مسير معتمد.")
            if not approve:
                conn.execute(
                    """UPDATE payroll_adjustment_requests SET status='مرفوض',
                       decided_by=?,decision_notes=?,decided_at=CURRENT_TIMESTAMP
                       WHERE id=? AND status='معلق'""",
                    (actor.get("id"), decision_notes.strip(), request_id),
                )
                self.db._audit_conn(
                    conn, actor, "REJECT", "payroll_adjustment", request_id,
                    {"period": request["period"], "payroll_item_id": request["payroll_item_id"]},
                )
                return {"request_id": request_id, "status": "مرفوض", "changed": False}

            policy = self.payroll_policy(conn)
            base = money(request["proposed_base_salary"])
            manual_allowances = money(request["proposed_manual_allowances"])
            overtime_hours = money(request["proposed_overtime_hours"])
            multiplier = money(request["proposed_overtime_multiplier"])
            manual_deductions = money(request["proposed_manual_deductions"])
            calculated = self._salary_based_amounts(
                base_salary=base,
                overtime_hours=overtime_hours,
                overtime_multiplier=multiplier,
                absence_days=money(request["absence_days"] or 0),
                late_minutes=money(request["late_minutes"] or 0),
                unpaid_leave_days=money(request["unpaid_leave_days"] or 0),
                policy=policy,
            )
            absence = (
                calculated["absence_deduction"]
                if request["proposed_apply_absence"] else money(0)
            )
            late = (
                calculated["late_deduction"]
                if request["proposed_apply_late"] else money(0)
            )
            unpaid = (
                calculated["unpaid_leave_deduction"]
                if request["proposed_apply_unpaid_leave"] else money(0)
            )
            structured, structured_updates = self._structured_allowance_proposal(
                conn, int(request["payroll_item_id"]), base
            )
            total_allowances = money(structured + manual_allowances)
            net = max(
                money(0),
                money(
                    base + total_allowances + calculated["overtime"]
                    - manual_deductions - absence - late - unpaid
                ),
            )
            conn.execute(
                """UPDATE payroll_items SET base_salary=?,allowances=?,manual_allowances=?,
                   overtime=?,overtime_hours=?,overtime_multiplier=?,manual_deductions=?,
                   apply_absence_deduction=?,absence_rate=?,absence_deduction=?,
                   apply_late_deduction=?,late_hour_rate=?,late_deduction=?,
                   apply_unpaid_leave_deduction=?,unpaid_leave_rate=?,
                   unpaid_leave_deduction=?,net_salary=?,notes=? WHERE id=?""",
                (
                    money_float(base), money_float(total_allowances),
                    money_float(manual_allowances), money_float(calculated["overtime"]),
                    money_float(overtime_hours), money_float(multiplier),
                    money_float(manual_deductions), int(bool(request["proposed_apply_absence"])),
                    money_float(calculated["day_rate"]), money_float(absence),
                    int(bool(request["proposed_apply_late"])),
                    money_float(calculated["hour_rate"]), money_float(late),
                    int(bool(request["proposed_apply_unpaid_leave"])),
                    money_float(calculated["day_rate"]), money_float(unpaid),
                    money_float(net), str(request["request_notes"] or "").strip(),
                    int(request["payroll_item_id"]),
                ),
            )
            for allowance_id, amount in structured_updates:
                conn.execute(
                    "UPDATE payroll_allowance_items SET amount=? WHERE id=?",
                    (money_float(amount), allowance_id),
                )
            conn.execute(
                """DELETE FROM payroll_allowance_items
                   WHERE payroll_item_id=? AND allowance_type_id IS NULL""",
                (int(request["payroll_item_id"]),),
            )
            if manual_allowances:
                conn.execute(
                    """INSERT INTO payroll_allowance_items(payroll_item_id,allowance_type_id,
                       allowance_name,calculation_method,source_value,amount)
                       VALUES(?,NULL,'بدلات إضافية/يدوية','fixed',?,?)""",
                    (
                        int(request["payroll_item_id"]),
                        money_float(manual_allowances), money_float(manual_allowances),
                    ),
                )
            conn.execute(
                """UPDATE payroll_adjustment_requests SET status='معتمد',decided_by=?,
                   decision_notes=?,decided_at=CURRENT_TIMESTAMP,
                   proposed_overtime=?,proposed_absence_rate=?,
                   proposed_absence_deduction=?,proposed_late_hour_rate=?,
                   proposed_late_deduction=?,proposed_unpaid_leave_rate=?,
                   proposed_unpaid_leave_deduction=?,proposed_total_allowances=?,
                   proposed_net_salary=? WHERE id=? AND status='معلق'""",
                (
                    actor.get("id"), decision_notes.strip(),
                    money_float(calculated["overtime"]), money_float(calculated["day_rate"]),
                    money_float(absence), money_float(calculated["hour_rate"]),
                    money_float(late), money_float(calculated["day_rate"]),
                    money_float(unpaid), money_float(total_allowances), money_float(net),
                    request_id,
                ),
            )
            run_total = money(
                conn.execute(
                    "SELECT COALESCE(SUM(net_salary),0) FROM payroll_items WHERE payroll_run_id=?",
                    (int(request["payroll_run_id"]),),
                ).fetchone()[0]
            )
            self.db._audit_conn(
                conn, actor, "APPROVE", "payroll_adjustment", request_id,
                {
                    "period": request["period"],
                    "payroll_item_id": request["payroll_item_id"],
                    "old_net": money_float(money(request["current_net"])),
                    "new_net": money_float(net),
                    "run_total": money_float(run_total),
                    "employee_master_salary_changed": False,
                },
            )
            return {
                "request_id": request_id,
                "status": "معتمد",
                "changed": True,
                "net_salary": money_float(net),
                "run_total": money_float(run_total),
            }

    def approve_payroll(self, run_id: int, actor: dict[str, Any]) -> None:
        self._require_admin(actor)
        with self.db.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status,period FROM payroll_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise DatabaseError("مسير الرواتب غير موجود.")
            if row["status"] == "معتمد":
                raise DatabaseError("مسير الرواتب معتمد بالفعل ولا يمكن اعتماده مرة أخرى.")
            pending = conn.execute(
                """SELECT COUNT(*) FROM payroll_adjustment_requests
                   WHERE payroll_run_id=? AND status='معلق'""",
                (run_id,),
            ).fetchone()[0]
            if pending:
                raise DatabaseError(
                    f"لا يمكن اعتماد المسير قبل اتخاذ قرار في {pending} طلب تعديل معلق."
                )
            items = conn.execute(
                """SELECT base_salary,allowances,overtime,manual_deductions,
                          absence_deduction,late_deduction,
                          unpaid_leave_deduction,net_salary
                   FROM payroll_items WHERE payroll_run_id=?""",
                (run_id,),
            ).fetchall()
            if not items:
                raise DatabaseError("لا يمكن اعتماد مسير لا يحتوي على موظفين.")
            total_net = money(0)
            for item in items:
                values = [
                    money(item["base_salary"]), money(item["allowances"]), money(item["overtime"]),
                    money(item["manual_deductions"]), money(item["absence_deduction"]),
                    money(item["late_deduction"]), money(item["unpaid_leave_deduction"]),
                    money(item["net_salary"]),
                ]
                if any(value < 0 for value in values):
                    raise DatabaseError("تعذر الاعتماد لوجود قيمة مالية سالبة.")
                expected = max(
                    money(0),
                    money(
                        values[0] + values[1] + values[2]
                        - values[3] - values[4] - values[5] - values[6]
                    ),
                )
                if expected != values[7]:
                    raise DatabaseError("تعذر الاعتماد لعدم تطابق صافي أحد الرواتب مع تفاصيله.")
                total_net = money(total_net + values[7])
            conn.execute(
                "UPDATE payroll_runs SET status='معتمد',approved_by=?,approved_at=CURRENT_TIMESTAMP WHERE id=?",
                (actor.get("id"), run_id),
            )
            self.db._audit_conn(
                conn,
                actor,
                "APPROVE",
                "payroll",
                run_id,
                {
                    "period": row["period"],
                    "employee_count": len(items),
                    "total_net": money_float(total_net),
                },
            )

    # ---------- Notifications ----------
    @staticmethod
    def _identity_stage(days_left: int) -> tuple[str, str, str] | None:
        if days_left < 0:
            return ("expired", "عاجل", f"انتهت صلاحية الهوية منذ {abs(days_left)} يوم")
        if days_left == 0:
            return ("today", "عاجل", "تنتهي صلاحية الهوية اليوم")
        if days_left <= 1:
            return ("1_day", "عاجل", "متبقي يوم واحد على انتهاء الهوية")
        if days_left <= 3:
            return ("3_days", "عاجل", f"متبقي {days_left} أيام على انتهاء الهوية")
        if days_left <= 7:
            return ("1_week", "مرتفع", f"متبقي {days_left} أيام على انتهاء الهوية")
        if days_left <= 30:
            return ("1_month", "تحذير", f"متبقي {days_left} يومًا على انتهاء الهوية")
        if days_left <= 61:
            return ("2_months", "تحذير", f"متبقي قرابة شهرين على انتهاء الهوية ({days_left} يومًا)")
        if days_left <= 92:
            return ("3_months", "تنبيه", f"متبقي قرابة 3 أشهر على انتهاء الهوية ({days_left} يومًا)")
        return None

    def identity_expiry_alerts(self, *, only_unseen: bool = False, mark_shown: bool = False) -> list[dict[str, Any]]:
        today = date.today()
        result: list[dict[str, Any]] = []
        with self.db.connection() as conn:
            rows = conn.execute(
                """SELECT id,emp_code,name,nationality,id_type,id_issue_date,id_expiry_date
                   FROM employees WHERE status<>'منتهي الخدمة'
                     AND TRIM(COALESCE(id_expiry_date,''))<>'' ORDER BY id_expiry_date"""
            ).fetchall()
            for row in rows:
                try:
                    expiry = datetime.strptime(row["id_expiry_date"], "%Y-%m-%d").date()
                except (TypeError, ValueError):
                    continue
                days_left = (expiry - today).days
                stage_info = self._identity_stage(days_left)
                if not stage_info:
                    continue
                stage, level, message = stage_info
                document_type = row["id_type"] or "غير محدد"
                key = f"identity_reminder:{row['id']}:{expiry.isoformat()}"
                previous = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
                if only_unseen and previous and previous[0] == stage:
                    continue
                result.append({
                    "employee_id": row["id"], "emp_code": row["emp_code"], "name": row["name"],
                    "date": expiry.isoformat(), "days_left": days_left, "stage": stage, "level": level,
                    "start_date": row["id_issue_date"], "end_date": expiry.isoformat(),
                    "type": "انتهاء الهوية", "document_type": document_type,
                    "employee": f"{row['emp_code']} — {row['name']}",
                    "message": f"{message} — نوع الوثيقة: {document_type}",
                })
                if mark_shown:
                    conn.execute(
                        "INSERT INTO app_settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, stage),
                    )
        return result

    def notifications(self, days: int = 90) -> list[dict[str, Any]]:
        today = date.today()
        deadline = today + timedelta(days=max(1, int(days)))
        result: list[dict[str, Any]] = []
        with self.db.connection() as conn:
            for row in conn.execute(
                """SELECT c.id,e.id AS employee_id,e.emp_code,e.name,c.start_date,c.end_date FROM contracts c
                   JOIN employees e ON e.id=c.employee_id
                   WHERE c.end_date IS NOT NULL AND c.status='ساري' AND c.end_date < ?
                   ORDER BY c.end_date LIMIT 100""",
                (today.isoformat(),),
            ):
                result.append({
                    "date": row["end_date"], "level": "عاجل", "type": "عقد منتهي",
                    "employee_id": row["employee_id"], "start_date": row["start_date"],
                    "end_date": row["end_date"],
                    "employee": f"{row['emp_code']} — {row['name']}",
                    "message": "العقد ما زال مسجلاً كساري رغم تجاوز تاريخ انتهائه",
                })
            for row in conn.execute(
                """SELECT c.id,e.id AS employee_id,e.emp_code,e.name,c.start_date,c.end_date FROM contracts c JOIN employees e ON e.id=c.employee_id
                   WHERE c.end_date IS NOT NULL AND c.status='ساري' AND c.end_date BETWEEN ? AND ? ORDER BY c.end_date""",
                (today.isoformat(), deadline.isoformat()),
            ):
                result.append({"date": row["end_date"], "level": "تحذير", "type": "انتهاء عقد",
                               "employee_id": row["employee_id"], "start_date": row["start_date"],
                               "end_date": row["end_date"],
                               "employee": f"{row['emp_code']} — {row['name']}", "message": "العقد يقترب من الانتهاء"})
            for row in conn.execute(
                """SELECT c.id,e.id AS employee_id,e.emp_code,e.name,c.start_date,c.probation_end_date FROM contracts c
                   JOIN employees e ON e.id=c.employee_id
                   WHERE c.status='ساري' AND c.probation_end_date BETWEEN ? AND ?
                   ORDER BY c.probation_end_date""",
                (today.isoformat(), deadline.isoformat()),
            ):
                result.append({
                    "date": row["probation_end_date"], "level": "مرتفع",
                    "employee_id": row["employee_id"], "start_date": row["start_date"],
                    "end_date": row["probation_end_date"],
                    "type": "نهاية فترة تجربة",
                    "employee": f"{row['emp_code']} — {row['name']}",
                    "message": "يلزم توثيق قرار التقييم قبل نهاية فترة التجربة",
                })
            expiry_fields = (
                (
                    "work_permit_start_date",
                    "work_permit_expiry_date",
                    "كرت عمل منتهي",
                    "انتهاء كرت العمل",
                    "كرت العمل",
                ),
                (
                    "medical_insurance_start_date",
                    "medical_insurance_expiry_date",
                    "تأمين طبي منتهي",
                    "انتهاء التأمين الطبي",
                    "التأمين الطبي",
                ),
            )
            for start_field, field, expired_type, upcoming_type, label in expiry_fields:
                for row in conn.execute(
                    f"""SELECT id AS employee_id,emp_code,name,{start_field} AS start_date,
                               {field} AS expiry_date FROM employees
                        WHERE status<>'منتهي الخدمة'
                          AND TRIM(COALESCE({field},''))<>''
                          AND date({field})<date(?)
                        ORDER BY {field} LIMIT 100""",
                    (today.isoformat(),),
                ):
                    result.append({
                        "date": row["expiry_date"],
                        "level": "عاجل",
                        "type": expired_type,
                        "employee_id": row["employee_id"],
                        "start_date": row["start_date"], "end_date": row["expiry_date"],
                        "employee": f"{row['emp_code']} — {row['name']}",
                        "message": f"{label} يحتاج إلى تجديد",
                    })
                for row in conn.execute(
                    f"""SELECT id AS employee_id,emp_code,name,{start_field} AS start_date,
                               {field} AS expiry_date FROM employees
                        WHERE status<>'منتهي الخدمة'
                          AND date({field}) BETWEEN date(?) AND date(?)
                        ORDER BY {field}""",
                    (today.isoformat(), deadline.isoformat()),
                ):
                    result.append({
                        "date": row["expiry_date"],
                        "level": "تحذير",
                        "type": upcoming_type,
                        "employee_id": row["employee_id"],
                        "start_date": row["start_date"], "end_date": row["expiry_date"],
                        "employee": f"{row['emp_code']} — {row['name']}",
                        "message": f"{label} يقترب من الانتهاء",
                    })
                for row in conn.execute(
                    f"""SELECT id AS employee_id,emp_code,name,{start_field} AS start_date,
                               {field} AS expiry_date FROM employees
                        WHERE status<>'منتهي الخدمة'
                          AND date({start_field}) BETWEEN date(?) AND date(?)
                        ORDER BY {start_field}""",
                    (today.isoformat(), deadline.isoformat()),
                ):
                    result.append({
                        "date": row["start_date"], "level": "معلومة",
                        "type": f"بدء {label}", "employee_id": row["employee_id"],
                        "start_date": row["start_date"], "end_date": row["expiry_date"],
                        "employee": f"{row['emp_code']} — {row['name']}",
                        "message": f"تبدأ صلاحية {label} خلال فترة المتابعة",
                    })
            for row in conn.execute(
                """SELECT d.id,e.id AS employee_id,e.emp_code,e.name,d.document_type,
                          d.start_date,d.expiry_date
                   FROM employee_documents d JOIN employees e ON e.id=d.employee_id
                   WHERE e.status<>'منتهي الخدمة'
                     AND d.document_type NOT IN ('هوية وطنية','إقامة','جواز سفر','كرت عمل','تأمين طبي')
                     AND TRIM(COALESCE(d.expiry_date,''))<>''
                     AND date(d.expiry_date)<date(?)
                   ORDER BY d.expiry_date LIMIT 100""",
                (today.isoformat(),),
            ):
                result.append({
                    "date": row["expiry_date"], "level": "عاجل", "type": "مستند منتهي",
                    "employee_id": row["employee_id"], "document_id": row["id"],
                    "start_date": row["start_date"], "end_date": row["expiry_date"],
                    "employee": f"{row['emp_code']} — {row['name']}",
                    "message": row["document_type"],
                })
            for row in conn.execute(
                """SELECT d.id,e.id AS employee_id,e.emp_code,e.name,d.document_type,
                          d.start_date,d.expiry_date FROM employee_documents d
                   JOIN employees e ON e.id=d.employee_id
                   WHERE e.status<>'منتهي الخدمة'
                     AND d.document_type NOT IN ('هوية وطنية','إقامة','جواز سفر','كرت عمل','تأمين طبي')
                     AND date(d.expiry_date) BETWEEN date(?) AND date(?) ORDER BY d.expiry_date""",
                (today.isoformat(), deadline.isoformat()),
            ):
                result.append({"date": row["expiry_date"], "level": "تحذير", "type": "انتهاء مستند",
                               "employee_id": row["employee_id"], "document_id": row["id"],
                               "start_date": row["start_date"], "end_date": row["expiry_date"],
                               "employee": f"{row['emp_code']} — {row['name']}", "message": row["document_type"]})
            for row in conn.execute(
                """SELECT d.id,e.id AS employee_id,e.emp_code,e.name,d.document_type,
                          d.start_date,d.expiry_date FROM employee_documents d
                   JOIN employees e ON e.id=d.employee_id
                   WHERE e.status<>'منتهي الخدمة'
                     AND d.document_type NOT IN ('هوية وطنية','إقامة','جواز سفر','كرت عمل','تأمين طبي')
                     AND date(d.start_date) BETWEEN date(?) AND date(?)
                   ORDER BY d.start_date""",
                (today.isoformat(), deadline.isoformat()),
            ):
                result.append({
                    "date": row["start_date"], "level": "معلومة", "type": "بدء صلاحية مستند",
                    "employee_id": row["employee_id"], "document_id": row["id"],
                    "start_date": row["start_date"], "end_date": row["expiry_date"],
                    "employee": f"{row['emp_code']} — {row['name']}", "message": row["document_type"],
                })
            pending = conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status='معلّقة'").fetchone()[0]
            if pending:
                result.append({"date": today.isoformat(), "level": "معلومة", "type": "إجازات معلّقة",
                               "employee": "—", "message": f"عدد الطلبات المعلّقة: {pending}"})
            missing_rows = conn.execute(
                """SELECT id,emp_code,name,id_type,phone,email,id_issue_date,id_expiry_date,
                          work_permit_start_date,work_permit_expiry_date,
                          medical_insurance_start_date,medical_insurance_expiry_date
                   FROM employees WHERE status<>'منتهي الخدمة' AND (
                       TRIM(COALESCE(phone,''))='' OR TRIM(COALESCE(email,''))='' OR
                       TRIM(COALESCE(id_issue_date,''))='' OR TRIM(COALESCE(id_expiry_date,''))='' OR
                       TRIM(COALESCE(medical_insurance_start_date,''))='' OR
                       TRIM(COALESCE(medical_insurance_expiry_date,''))='' OR
                       (id_type='إقامة' AND (
                           TRIM(COALESCE(work_permit_start_date,''))='' OR
                           TRIM(COALESCE(work_permit_expiry_date,''))=''
                       ))
                   ) ORDER BY emp_code LIMIT 100"""
            ).fetchall()
            field_labels = (
                ("phone", "الجوال"), ("email", "البريد الإلكتروني"),
                ("id_issue_date", "بداية الهوية أو الإقامة أو الجواز"),
                ("id_expiry_date", "نهاية الهوية أو الإقامة أو الجواز"),
                ("medical_insurance_start_date", "بداية التأمين الطبي"),
                ("medical_insurance_expiry_date", "نهاية التأمين الطبي"),
            )
            for row in missing_rows:
                missing = [label for field, label in field_labels if not str(row[field] or "").strip()]
                if row["id_type"] == "إقامة":
                    if not str(row["work_permit_start_date"] or "").strip():
                        missing.append("بداية كرت العمل")
                    if not str(row["work_permit_expiry_date"] or "").strip():
                        missing.append("نهاية كرت العمل")
                result.append({
                    "date": today.isoformat(), "level": "تنبيه", "type": "بيانات متابعة ناقصة",
                    "employee_id": row["id"], "start_date": None, "end_date": None,
                    "employee": f"{row['emp_code']} — {row['name']}",
                    "message": "الحقول الناقصة: " + "، ".join(missing),
                })
            for row in conn.execute(
                """SELECT id,emp_code,name,bank_name,iban FROM employees
                   WHERE status<>'منتهي الخدمة' AND (
                       TRIM(COALESCE(bank_name,''))='' OR TRIM(COALESCE(iban,''))=''
                   ) ORDER BY emp_code LIMIT 100"""
            ):
                missing = []
                if not str(row["bank_name"] or "").strip():
                    missing.append("اسم البنك")
                if not str(row["iban"] or "").strip():
                    missing.append("الآيبان")
                result.append({
                    "date": today.isoformat(), "level": "تنبيه", "type": "بيانات بنكية ناقصة",
                    "employee_id": row["id"], "start_date": None, "end_date": None,
                    "employee": f"{row['emp_code']} — {row['name']}",
                    "message": "الحقول الناقصة: " + "، ".join(missing),
                })
            draft_payrolls = conn.execute(
                "SELECT COUNT(*) FROM payroll_runs WHERE status='مسودة' AND period<=?",
                (today.strftime("%Y-%m"),),
            ).fetchone()[0]
            if draft_payrolls:
                result.append({
                    "date": today.isoformat(), "level": "مرتفع", "type": "مسيرات غير معتمدة",
                    "employee": "—",
                    "message": f"{draft_payrolls} مسير رواتب حالي أو سابق ما زال في حالة مسودة",
                })
            pending_adjustments = conn.execute(
                "SELECT COUNT(*) FROM payroll_adjustment_requests WHERE status='معلق'"
            ).fetchone()[0]
            if pending_adjustments:
                result.append({
                    "date": today.isoformat(), "level": "مرتفع",
                    "type": "تعديلات رواتب بانتظار المدير", "employee": "—",
                    "message": f"{pending_adjustments} طلب تعديل مالي لم يُعتمد أو يُرفض بعد",
                })
            employees = conn.execute("SELECT emp_code,name,birth_date FROM employees WHERE birth_date IS NOT NULL").fetchall()
        # تاريخ انتهاء الهوية يشمل الهوية الوطنية والإقامة ويظهر على مراحل تصاعدية.
        result.extend(self.identity_expiry_alerts())
        for emp in employees:
            try:
                born = datetime.strptime(emp["birth_date"], "%Y-%m-%d").date()
                next_birthday = born.replace(year=today.year)
                if next_birthday < today:
                    next_birthday = born.replace(year=today.year + 1)
                if next_birthday <= deadline:
                    result.append({"date": next_birthday.isoformat(), "level": "مناسبة", "type": "عيد ميلاد",
                                   "employee": f"{emp['emp_code']} — {emp['name']}", "message": "موعد عيد الميلاد"})
            except ValueError:
                continue
        return sorted(result, key=lambda x: (x["date"], x["type"]))

    # ---------- Full portable backup ----------
    def create_full_backup(self, actor: dict[str, Any] | None, automatic: bool = False) -> Path:
        with self._backup_lock:
            return self._create_full_backup_unlocked(actor, automatic)

    def _create_full_backup_unlocked(self, actor: dict[str, Any] | None, automatic: bool = False) -> Path:
        """نسخة ذرية تشمل قاعدة البيانات والمستندات ولا تترك مقابض ملفات مفتوحة على Windows."""
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        kind = "auto" if automatic else "manual"
        destination = BACKUP_DIR / f"ProTech_HR_full_{kind}_{stamp}.protechhr"
        partial = destination.with_name(destination.name + ".partial")
        safe_unlink(partial)
        tmpdir = Path(tempfile.mkdtemp(prefix="protech_backup_"))
        skipped_files: list[str] = []
        try:
            db_copy = tmpdir / "hr_system.db"
            self.db.create_snapshot(db_copy)
            with closing(sqlite3.connect(db_copy, timeout=60)) as snapshot_conn:
                integrity = snapshot_conn.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise DatabaseError("تعذر إنشاء نسخة سليمة من قاعدة البيانات.")
                document_count, document_bytes = snapshot_conn.execute(
                    "SELECT COUNT(*),COALESCE(SUM(file_size),0) FROM employee_documents"
                ).fetchone()
            manifest = {
                "format": 3,
                "app": "ProTech HR",
                "created_at": datetime.now().isoformat(),
                "database": "hr_system.db",
                "database_sha256": _sha256_file(db_copy),
                "attachments_storage": "sqlite_blob",
                "attachment_count": int(document_count),
                "attachment_bytes": int(document_bytes),
                "legacy_documents_root": "employee_documents",
                "skipped_files": skipped_files,
            }
            with zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED, compresslevel=7, allowZip64=True) as archive:
                archive.write(db_copy, "hr_system.db")
                if DOCUMENTS_DIR.exists():
                    for path in sorted(DOCUMENTS_DIR.rglob("*")):
                        if not path.is_file():
                            continue
                        arcname = f"employee_documents/{path.relative_to(DOCUMENTS_DIR).as_posix()}"
                        try:
                            retry_file_operation(lambda p=path, a=arcname: archive.write(p, a), attempts=5, initial_delay=0.12)
                        except OSError:
                            skipped_files.append(str(path.relative_to(DOCUMENTS_DIR)))
                            setup_logging().warning("تعذر إدراج مستند مفتوح في النسخة: %s", path, exc_info=True)
                # نكتب البيان أخيرًا ليحتوي قائمة الملفات المتعذر نسخها إن وجدت.
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            # تحقق سريع من قابلية فتح الأرشيف قبل اعتماده.
            with zipfile.ZipFile(partial, "r") as archive:
                if archive.testzip() is not None:
                    raise DatabaseError("فشل التحقق من سلامة ملف النسخة الكاملة.")
                if not {"hr_system.db", "manifest.json"}.issubset(set(archive.namelist())):
                    raise DatabaseError("النسخة الكاملة لا تحتوي الملفات الأساسية.")
            atomic_replace(partial, destination)
        except Exception:
            safe_unlink(partial)
            raise
        finally:
            # لا نسمح لمضاد الفيروسات أو فهرسة Windows بتحويل نجاح النسخ إلى خطأ WinError 32.
            safe_rmtree(tmpdir, suppress=True)

        try:
            with self.db.connection() as conn:
                self.db._audit_conn(
                    conn, actor, "FULL_BACKUP", "database", destination.name,
                    {"automatic": automatic, "skipped_files": skipped_files},
                )
        except Exception:
            setup_logging().warning("تم إنشاء النسخة الكاملة لكن تعذر تسجيل حدث التدقيق", exc_info=True)
        self._cleanup_full_backups()
        return destination

    def verify_full_backup(self, path: str | Path) -> dict[str, Any]:
        source = Path(path)
        if not source.is_file():
            raise DatabaseError("ملف النسخة الاحتياطية غير موجود.")
        if source.suffix.lower() == ".db":
            if source.stat().st_size <= 0 or source.stat().st_size > _MAX_BACKUP_DATABASE_BYTES:
                raise DatabaseError("حجم قاعدة البيانات الاحتياطية غير صالح.")
            with closing(sqlite3.connect(source, timeout=60)) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise DatabaseError("قاعدة البيانات الاحتياطية تالفة.")
                if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise DatabaseError("قاعدة البيانات الاحتياطية تحتوي علاقات غير سليمة.")
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if not {"users", "employees", "attendance", "audit_logs"}.issubset(
                    tables
                ):
                    raise DatabaseError("هذا الملف ليس نسخة احتياطية صالحة للنظام.")
            return {"format": 1, "database_only": True}
        try:
            with zipfile.ZipFile(source, "r") as archive:
                _validated_backup_members(archive)
                if archive.testzip() is not None:
                    raise DatabaseError("ملف النسخة الاحتياطية تالف.")
                db_info = archive.getinfo("hr_system.db")
                manifest_info = archive.getinfo("manifest.json")
                if db_info.is_dir() or db_info.file_size <= 0 or db_info.file_size > _MAX_BACKUP_DATABASE_BYTES:
                    raise DatabaseError("حجم قاعدة البيانات داخل النسخة غير صالح.")
                if (
                    manifest_info.is_dir()
                    or manifest_info.file_size <= 0
                    or manifest_info.file_size > _MAX_BACKUP_MANIFEST_BYTES
                ):
                    raise DatabaseError("حجم ملف تعريف النسخة غير صالح.")
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                if str(manifest.get("app") or "") != "ProTech HR":
                    raise DatabaseError("ملف التعريف لا يخص نظام ProTech HR.")
                expected = str(manifest.get("database_sha256") or "")
                if expected and _sha256_zip_member(archive, "hr_system.db") != expected:
                    raise DatabaseError("بصمة قاعدة البيانات داخل النسخة غير مطابقة.")
                return manifest
        except (zipfile.BadZipFile, json.JSONDecodeError) as exc:
            raise DatabaseError("ملف النسخة الاحتياطية غير صالح.") from exc

    def export_full_backup(self, destination: str | Path, actor: dict[str, Any]) -> Path:
        self._require_hr(actor)
        destination = Path(destination)
        if destination.suffix.lower() != ".protechhr":
            destination = destination.with_suffix(".protechhr")
        destination.parent.mkdir(parents=True, exist_ok=True)
        local_backup = self.create_full_backup(actor, automatic=False)
        partial = destination.with_name(destination.name + ".partial")
        safe_unlink(partial)
        try:
            retry_file_operation(lambda: shutil.copy2(local_backup, partial), attempts=8, initial_delay=0.15)
            self.verify_full_backup(partial)
            atomic_replace(partial, destination)
        except Exception:
            safe_unlink(partial)
            raise
        with self.db.connection() as conn:
            self.db._audit_conn(conn, actor, "EXPORT_BACKUP", "database", destination.name, {
                "path": str(destination), "size": destination.stat().st_size,
            })
        return destination

    def automatic_full_backup_if_due(self, actor: dict[str, Any] | None = None) -> Path | None:
        today = date.today().isoformat()
        with self.db.connection() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key='last_auto_full_backup'").fetchone()
            if row and row[0] == today:
                return None
        path = self.create_full_backup(actor, automatic=True)
        with self.db.connection() as conn:
            conn.execute("INSERT INTO app_settings(key,value) VALUES('last_auto_full_backup',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (today,))
        return path

    def list_full_backups(self) -> list[Path]:
        files = [*BACKUP_DIR.glob("ProTech_HR_full_*.protechhr"), *BACKUP_DIR.glob("hr_backup_*.db")]
        valid = []
        for path in files:
            try:
                if path.is_file() and path.stat().st_size > 0:
                    valid.append(path)
            except OSError:
                continue
        return sorted(valid, key=lambda p: p.stat().st_mtime, reverse=True)

    def maintain_database(self, actor: dict[str, Any]) -> dict[str, Any]:
        """Create a full safety backup, then compact and optimize SQLite."""
        self._require_admin(actor)
        with self._backup_lock:
            backup = self._create_full_backup_unlocked(actor, automatic=False)
            result = self.db.optimize_database(actor)
        return {**result, "backup": str(backup)}

    def export_diagnostic_report(
        self,
        destination: str | Path,
        actor: dict[str, Any],
    ) -> Path:
        """Export non-sensitive health metadata for support and auditing."""
        self._require_hr(actor)
        output = Path(destination)
        if output.suffix.lower() != ".json":
            output = output.with_suffix(".json")
        output.parent.mkdir(parents=True, exist_ok=True)
        backups = []
        for path in self.list_full_backups()[:20]:
            try:
                stat_result = path.stat()
            except OSError:
                continue
            backups.append(
                {
                    "name": path.name,
                    "size_bytes": stat_result.st_size,
                    "modified_at": datetime.fromtimestamp(stat_result.st_mtime).isoformat(),
                }
            )
        report = {
            "format": 1,
            "application": "ProTech HR",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "health": self.db.health_check(),
            "backups": backups,
        }
        partial = output.with_name(output.name + ".partial")
        safe_unlink(partial)
        partial.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        atomic_replace(partial, output)
        with self.db.connection() as conn:
            self.db._audit_conn(
                conn,
                actor,
                "EXPORT_DIAGNOSTICS",
                "database",
                output.name,
                {"path": str(output)},
            )
        return output

    def restore_full_backup(self, path: str | Path, actor: dict[str, Any]) -> None:
        with self._backup_lock:
            self._restore_full_backup_unlocked(path, actor)

    def restore_pre_login_backup(self, path: str | Path) -> None:
        """استعادة آمنة من شاشة الدخول مع نسخة أمان للحالة الحالية.

        لا يُسمح بهذا المسار إلا أثناء الإعداد الأول، قبل وجود أي مستخدم.
        بعد إنشاء الحسابات يجب أن تتم الاستعادة من داخل النظام بصلاحية مدير؛
        لأن استبدال قاعدة المستخدمين قبل التحقق من الهوية يتجاوز تسجيل الدخول.
        """
        if self.db.has_users():
            raise PermissionError(
                "الاستعادة قبل تسجيل الدخول متاحة أثناء الإعداد الأول فقط. "
                "سجّل الدخول بحساب مدير ثم استخدم شاشة النسخ الاحتياطي."
            )
        recovery_actor = {
            "id": None,
            "username": "prelogin_restore",
            "full_name": "استعادة قبل تسجيل الدخول",
            "role": "admin",
        }
        self.restore_full_backup(path, recovery_actor)

    def restore_initial_backup(self, path: str | Path) -> None:
        """اسم متوافق مع الإصدارات السابقة لمسار الاستعادة قبل الدخول."""
        self.restore_pre_login_backup(path)

    def _restore_full_backup_unlocked(self, path: str | Path, actor: dict[str, Any]) -> None:
        self._require_admin(actor)
        source = Path(path)
        if not source.is_file():
            raise DatabaseError("ملف النسخة غير موجود.")
        self.verify_full_backup(source)
        if source.suffix.lower() == ".db":
            self.db.restore_backup(source, actor)
            self.initialize()
            return

        tmpdir = Path(tempfile.mkdtemp(prefix="protech_restore_"))
        old_docs: Path | None = None
        try:
            # ننسخ المصدر إلى موقع مؤقت حتى لا يتأثر بسياسة الاحتفاظ أو قفل برنامج مزامنة خارجي.
            restore_source = tmpdir / "restore_source.protechhr"
            retry_file_operation(lambda: shutil.copy2(source, restore_source), attempts=6, initial_delay=0.12)
            self.create_full_backup(actor, automatic=False)
            try:
                with zipfile.ZipFile(restore_source) as archive:
                    _extract_validated_backup(archive, tmpdir)
            except zipfile.BadZipFile as exc:
                raise DatabaseError("ملف النسخة تالف.") from exc

            db_file = tmpdir / "hr_system.db"
            with closing(sqlite3.connect(db_file, timeout=60)) as conn:
                if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise DatabaseError("قاعدة البيانات داخل النسخة تالفة.")
                if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise DatabaseError("قاعدة البيانات داخل النسخة تحتوي علاقات غير سليمة.")
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                if not {"users", "employees", "attendance", "audit_logs"}.issubset(
                    tables
                ):
                    raise DatabaseError("قاعدة البيانات داخل النسخة لا تخص النظام.")
            temp_db = Path(str(self.db.path) + ".restore.tmp")
            safe_unlink(temp_db)
            retry_file_operation(lambda: shutil.copy2(db_file, temp_db), attempts=6, initial_delay=0.12)
            for suffix in ("-wal", "-shm"):
                safe_unlink(Path(str(self.db.path) + suffix))
            atomic_replace(temp_db, self.db.path)

            docs = tmpdir / "employee_documents"
            if docs.exists():
                old_docs = DOCUMENTS_DIR.with_name(DOCUMENTS_DIR.name + f".old_{int(time.time())}")
                if DOCUMENTS_DIR.exists():
                    retry_file_operation(lambda: DOCUMENTS_DIR.replace(old_docs), attempts=8, initial_delay=0.1)
                retry_file_operation(lambda: shutil.copytree(docs, DOCUMENTS_DIR), attempts=5, initial_delay=0.12)
                if old_docs.exists():
                    safe_rmtree(old_docs, suppress=True)
                    old_docs = None
        except Exception:
            # إذا تعثر نقل المستندات بعد إبعاد المجلد القديم نعيده قدر الإمكان.
            if old_docs and old_docs.exists() and not DOCUMENTS_DIR.exists():
                try:
                    retry_file_operation(lambda: old_docs.replace(DOCUMENTS_DIR), attempts=5, initial_delay=0.1)
                except OSError:
                    setup_logging().error("تعذر إعادة مجلد المستندات القديم بعد فشل الاستعادة", exc_info=True)
            raise
        finally:
            safe_rmtree(tmpdir, suppress=True)

        self.db.initialize()
        self.initialize()
        with self.db.connection() as conn:
            self.db._audit_conn(conn, actor, "FULL_RESTORE", "database", source.name, {})

    @staticmethod
    def _cleanup_full_backups(keep_automatic: int = 30, keep_manual: int = 50) -> None:
        """نظّف نسخ الجلسات دون أن تستهلك حصة النسخ اليدوية المهمة."""
        groups = (
            (BACKUP_DIR.glob("ProTech_HR_full_auto_*.protechhr"), keep_automatic),
            (BACKUP_DIR.glob("ProTech_HR_full_manual_*.protechhr"), keep_manual),
        )
        for paths, keep in groups:
            files = sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)
            for path in files[keep:]:
                safe_unlink(path)

    def export_data(self) -> dict[str, list[dict[str, Any]]]:
        with self.db.connection() as conn:
            queries = {
                "Contracts": "SELECT c.*,e.emp_code,e.name FROM contracts c JOIN employees e ON e.id=c.employee_id ORDER BY c.id",
                "Documents": "SELECT d.id,e.emp_code,e.name,d.document_type,d.original_name,d.start_date,d.expiry_date,d.notes,d.created_at FROM employee_documents d JOIN employees e ON e.id=d.employee_id ORDER BY d.id",
                "LeaveBalances": "SELECT * FROM leave_balances ORDER BY balance_year,employee_id",
                "Shifts": "SELECT * FROM shifts ORDER BY id",
                "ShiftAssignments": "SELECT es.*,e.emp_code,e.name,s.name shift_name FROM employee_shifts es JOIN employees e ON e.id=es.employee_id JOIN shifts s ON s.id=es.shift_id ORDER BY es.id",
                "AllowanceTypes": "SELECT * FROM allowance_types ORDER BY sort_order,name",
                "EmployeeAllowances": "SELECT ea.*,at.name allowance_name,e.emp_code,e.name FROM employee_allowances ea JOIN allowance_types at ON at.id=ea.allowance_type_id JOIN employees e ON e.id=ea.employee_id ORDER BY e.emp_code,at.sort_order",
                "PayrollRuns": "SELECT * FROM payroll_runs ORDER BY period",
                "PayrollItems": "SELECT i.*,r.period,e.emp_code,e.name FROM payroll_items i JOIN payroll_runs r ON r.id=i.payroll_run_id JOIN employees e ON e.id=i.employee_id ORDER BY r.period,e.emp_code",
                "PayrollAllowanceItems": "SELECT pai.*,r.period,e.emp_code,e.name FROM payroll_allowance_items pai JOIN payroll_items pi ON pi.id=pai.payroll_item_id JOIN payroll_runs r ON r.id=pi.payroll_run_id JOIN employees e ON e.id=pi.employee_id ORDER BY r.period,e.emp_code,pai.id",
                "PayrollAdjustmentApprovals": "SELECT a.*,r.period,e.emp_code,e.name FROM payroll_adjustment_requests a JOIN payroll_runs r ON r.id=a.payroll_run_id JOIN employees e ON e.id=a.employee_id ORDER BY r.period,e.emp_code,a.id",
            }
            return {name: [dict(r) for r in conn.execute(sql).fetchall()] for name, sql in queries.items()}
