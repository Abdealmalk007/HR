from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from .config import (
    APP_VERSION,
    ATTENDANCE_STATUSES,
    BACKUP_DIR,
    COMPANY_NAME,
    DB_PATH,
    EMPLOYEE_STATUSES,
    LEAVE_STATUSES,
    LEAVE_TYPES,
    ROLES,
)
from .company import (
    COMPANY_PROFILE_FIELDS,
    COMPANY_PROFILE_LABELS,
    COMPANY_SETTING_PREFIX,
    company_profile_rows,
    normalize_company_profile,
)
from .security import (
    hash_password,
    id_fingerprint,
    normalize_bank_account,
    normalize_id_number,
    normalize_identity_document_number,
    validate_saudi_iban,
    verify_password,
)
from .employee_metrics import normalize_rating
from .identity_rules import IDENTITY_TYPES, identity_type_for_nationality, validate_identity_type
from .money import money, money_float
from .local_ai import neural_forecast, robust_anomalies, workforce_insights
from .resilience import atomic_replace, safe_unlink, setup_logging

ALLOWED_SORT_COLUMNS = {
    "emp_code", "name", "department", "job_title", "salary", "status", "join_date",
    "id_type", "id_issue_date", "id_expiry_date", "work_permit_start_date",
    "work_permit_expiry_date", "medical_insurance_start_date",
    "medical_insurance_expiry_date", "performance_rating", "created_at"
}


class DatabaseError(RuntimeError):
    pass


class PermissionError(DatabaseError):
    pass


def _clean_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


class Database:
    def __init__(self, path: Path | str = DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._backup_lock = threading.RLock()
        self.initialize()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA cache_size = -8000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    full_name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('admin','hr','viewer')),
                    password_salt TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS employees (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    id_number TEXT NOT NULL DEFAULT '',
                    id_hash TEXT UNIQUE,
                    id_type TEXT NOT NULL DEFAULT '' CHECK(id_type IN ('','هوية وطنية','إقامة','جواز سفر')),
                    id_issue_date TEXT,
                    id_expiry_date TEXT,
                    work_permit_start_date TEXT,
                    work_permit_expiry_date TEXT,
                    medical_insurance_start_date TEXT,
                    medical_insurance_expiry_date TEXT,
                    nationality TEXT NOT NULL DEFAULT '',
                    birth_date TEXT,
                    join_date TEXT NOT NULL DEFAULT '',
                    department TEXT NOT NULL DEFAULT '',
                    job_title TEXT NOT NULL DEFAULT '',
                    salary REAL CHECK(salary IS NULL OR salary >= 0),
                    work_location TEXT,
                    email TEXT,
                    bank_name TEXT,
                    bank_account_number TEXT,
                    iban TEXT,
                    status TEXT NOT NULL CHECK(status IN ('نشط','في إجازة','منتهي الخدمة')),
                    performance_rating INTEGER NOT NULL DEFAULT 0 CHECK(performance_rating BETWEEN 0 AND 5),
                    notes TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS attendance (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    attendance_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    note TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(employee_id, attendance_date),
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS leave_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER NOT NULL,
                    leave_type TEXT NOT NULL,
                    start_date TEXT NOT NULL,
                    end_date TEXT NOT NULL,
                    total_days INTEGER NOT NULL CHECK(total_days > 0),
                    status TEXT NOT NULL DEFAULT 'معلّقة',
                    reason TEXT,
                    notes TEXT,
                    created_by INTEGER,
                    approved_by INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY(approved_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS calendar_notes (
                    note_date TEXT PRIMARY KEY,
                    note TEXT NOT NULL,
                    created_by INTEGER,
                    updated_by INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY(updated_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    username TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT,
                    details TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS message_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    channel TEXT NOT NULL DEFAULT 'both' CHECK(channel IN ('email','whatsapp','both')),
                    subject TEXT,
                    body TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_by INTEGER,
                    updated_by INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL,
                    FOREIGN KEY(updated_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS communication_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    employee_id INTEGER,
                    channel TEXT NOT NULL CHECK(channel IN ('email','whatsapp')),
                    template_id INTEGER,
                    subject TEXT,
                    delivery_mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_by INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE SET NULL,
                    FOREIGN KEY(template_id) REFERENCES message_templates(id) ON DELETE SET NULL,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS employee_custom_fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    field_type TEXT NOT NULL CHECK(field_type IN ('text','number','date','choice')),
                    options_json TEXT NOT NULL DEFAULT '[]',
                    is_required INTEGER NOT NULL DEFAULT 0,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 100,
                    created_by INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(created_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS employee_custom_values (
                    employee_id INTEGER NOT NULL,
                    field_id INTEGER NOT NULL,
                    value_text TEXT NOT NULL DEFAULT '',
                    updated_by INTEGER,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(employee_id, field_id),
                    FOREIGN KEY(employee_id) REFERENCES employees(id) ON DELETE CASCADE,
                    FOREIGN KEY(field_id) REFERENCES employee_custom_fields(id) ON DELETE CASCADE,
                    FOREIGN KEY(updated_by) REFERENCES users(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS saved_views (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_user_id INTEGER NOT NULL,
                    scope TEXT NOT NULL,
                    name TEXT NOT NULL COLLATE NOCASE,
                    filters_json TEXT NOT NULL DEFAULT '{}',
                    is_shared INTEGER NOT NULL DEFAULT 0,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(owner_user_id, scope, name),
                    FOREIGN KEY(owner_user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_employees_name ON employees(name);
                CREATE INDEX IF NOT EXISTS idx_employees_department ON employees(department);
                CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(attendance_date);
                CREATE INDEX IF NOT EXISTS idx_leave_employee ON leave_requests(employee_id);
                CREATE INDEX IF NOT EXISTS idx_leave_dates ON leave_requests(start_date, end_date);
                CREATE INDEX IF NOT EXISTS idx_leave_status ON leave_requests(status);
                CREATE INDEX IF NOT EXISTS idx_calendar_notes_date ON calendar_notes(note_date);
                CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_message_templates_channel ON message_templates(channel, is_active);
                CREATE INDEX IF NOT EXISTS idx_communication_employee ON communication_logs(employee_id);
                CREATE INDEX IF NOT EXISTS idx_communication_created_at ON communication_logs(created_at);
                CREATE INDEX IF NOT EXISTS idx_custom_fields_active_order
                    ON employee_custom_fields(is_active, sort_order, label);
                CREATE INDEX IF NOT EXISTS idx_custom_values_field_value
                    ON employee_custom_values(field_id, value_text);
                CREATE INDEX IF NOT EXISTS idx_saved_views_scope_owner
                    ON saved_views(scope, owner_user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_saved_views_shared
                    ON saved_views(scope, is_shared, updated_at DESC);
                """
            )

            default_templates = (
                ("ترحيب بموظف جديد", "both", "مرحبًا بك في {company}",
                 "مرحبًا {name}،\nيسعدنا انضمامك إلى فريق {company}. نتمنى لك بداية موفقة ونجاحًا مستمرًا."),
                ("اعتماد الإجازة", "both", "تم اعتماد طلب الإجازة",
                 "مرحبًا {name}،\nتم اعتماد طلب الإجازة الخاص بك. يرجى مراجعة الموارد البشرية عند الحاجة إلى مزيد من التفاصيل."),
                ("تذكير بانتهاء العقد", "both", "تذكير بخصوص العقد",
                 "مرحبًا {name}،\nنرجو التواصل مع إدارة الموارد البشرية بخصوص تحديث بيانات العقد."),
                ("إشعار قسيمة الراتب", "both", "قسيمة الراتب متاحة",
                 "مرحبًا {name}،\nأصبحت قسيمة الراتب متاحة. يمكنك التواصل مع إدارة الموارد البشرية لأي استفسار."),
                ("إعلان عام", "both", "إعلان من {company}",
                 "الزملاء والزميلات،\nنود إبلاغكم بالتحديث التالي:\n\nمع التحية،\n{company}"),
            )
            conn.executemany(
                "INSERT OR IGNORE INTO message_templates(name, channel, subject, body) VALUES(?,?,?,?)",
                default_templates,
            )

            user_columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
            for column, definition in (
                ("failed_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("locked_until", "TEXT"),
                ("last_login", "TEXT"),
            ):
                if column not in user_columns:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")

            employee_columns = {row[1] for row in conn.execute("PRAGMA table_info(employees)").fetchall()}
            if "id_type" not in employee_columns:
                conn.execute("ALTER TABLE employees ADD COLUMN id_type TEXT NOT NULL DEFAULT 'إقامة'")
            for column in (
                "id_issue_date",
                "id_expiry_date",
                "work_permit_start_date",
                "work_permit_expiry_date",
                "medical_insurance_start_date",
                "medical_insurance_expiry_date",
                "bank_name",
                "bank_account_number",
                "iban",
            ):
                if column not in employee_columns:
                    conn.execute(f"ALTER TABLE employees ADD COLUMN {column} TEXT")
            if "performance_rating" not in employee_columns:
                conn.execute(
                    "ALTER TABLE employees ADD COLUMN performance_rating INTEGER NOT NULL DEFAULT 0"
                )
            # ترقية السجلات القديمة التي تحتوي جنسية فعلية فقط.
            rows = conn.execute("SELECT id,nationality,id_type FROM employees").fetchall()
            for row in rows:
                nationality = str(row["nationality"] or "").strip()
                if not nationality or row["id_type"] == "جواز سفر":
                    continue
                expected_type = identity_type_for_nationality(nationality)
                if row["id_type"] != expected_type:
                    conn.execute("UPDATE employees SET id_type=? WHERE id=?", (expected_type, row["id"]))
            conn.execute("CREATE INDEX IF NOT EXISTS idx_employees_id_type ON employees(id_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_employees_id_expiry ON employees(id_expiry_date)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_employees_work_permit_expiry "
                "ON employees(work_permit_expiry_date)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_employees_medical_insurance_expiry "
                "ON employees(medical_insurance_expiry_date)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_employees_rating ON employees(performance_rating)")

            attendance_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(attendance)").fetchall()
            }
            for column, definition in (
                ("check_in_time", "TEXT"),
                ("check_out_time", "TEXT"),
                ("late_minutes", "INTEGER NOT NULL DEFAULT 0"),
                ("worked_minutes", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in attendance_columns:
                    conn.execute(f"ALTER TABLE attendance ADD COLUMN {column} {definition}")
        self._migrate_employee_table_for_partial_profiles()
        self._clean_legacy_unknown_date_values()
        self._migrate_employee_codes_from_identity()
        self._ensure_performance_indexes()
        try:
            with self.connection() as conn:
                conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _ensure_performance_indexes(self) -> None:
        """Add non-critical indexes after schema migrations are complete."""
        statements = (
            "CREATE INDEX IF NOT EXISTS idx_employees_job_title ON employees(job_title)",
            "CREATE INDEX IF NOT EXISTS idx_employees_status_name ON employees(status, name)",
            "CREATE INDEX IF NOT EXISTS idx_employees_join_date ON employees(join_date)",
            "CREATE INDEX IF NOT EXISTS idx_employees_created_at ON employees(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_attendance_employee_date ON attendance(employee_id, attendance_date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_leave_employee_start ON leave_requests(employee_id, start_date DESC)",
            "CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_logs(entity_type, entity_id)",
        )
        with self.connection() as conn:
            for statement in statements:
                conn.execute(statement)

    def _migrate_employee_table_for_partial_profiles(self) -> None:
        """Allow genuinely unknown employee values without inventing placeholders."""
        with closing(sqlite3.connect(self.path, timeout=30)) as check:
            columns = {row[1]: row for row in check.execute("PRAGMA table_info(employees)")}
            id_hash_required = bool(columns.get("id_hash") and columns["id_hash"][3])
            salary_required = bool(columns.get("salary") and columns["salary"][3])
            table_sql = str(
                check.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='employees'"
                ).fetchone()[0]
            )
            passport_missing = "جواز سفر" not in table_sql
        if not (id_hash_required or salary_required or passport_missing):
            return

        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE employees_v19_partial (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    emp_code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    id_number TEXT NOT NULL DEFAULT '',
                    id_hash TEXT UNIQUE,
                    id_type TEXT NOT NULL DEFAULT '' CHECK(id_type IN ('','هوية وطنية','إقامة','جواز سفر')),
                    id_issue_date TEXT,
                    id_expiry_date TEXT,
                    work_permit_start_date TEXT,
                    work_permit_expiry_date TEXT,
                    medical_insurance_start_date TEXT,
                    medical_insurance_expiry_date TEXT,
                    nationality TEXT NOT NULL DEFAULT '',
                    birth_date TEXT,
                    join_date TEXT NOT NULL DEFAULT '',
                    department TEXT NOT NULL DEFAULT '',
                    job_title TEXT NOT NULL DEFAULT '',
                    salary REAL CHECK(salary IS NULL OR salary >= 0),
                    work_location TEXT,
                    email TEXT,
                    bank_name TEXT,
                    bank_account_number TEXT,
                    iban TEXT,
                    status TEXT NOT NULL DEFAULT 'نشط' CHECK(status IN ('نشط','في إجازة','منتهي الخدمة')),
                    performance_rating INTEGER NOT NULL DEFAULT 0 CHECK(performance_rating BETWEEN 0 AND 5),
                    notes TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.execute(
                """
                INSERT INTO employees_v19_partial(
                    id,emp_code,name,phone,id_number,id_hash,id_type,id_issue_date,id_expiry_date,
                    work_permit_start_date,work_permit_expiry_date,
                    medical_insurance_start_date,medical_insurance_expiry_date,
                    nationality,birth_date,join_date,department,job_title,salary,work_location,email,
                    bank_name,bank_account_number,iban,status,performance_rating,notes,created_at,updated_at
                )
                SELECT
                    id,emp_code,name,COALESCE(phone,''),COALESCE(id_number,''),
                    CASE WHEN TRIM(COALESCE(id_number,''))='' THEN NULL ELSE id_hash END,
                    CASE WHEN id_type IN ('هوية وطنية','إقامة','جواز سفر') THEN id_type ELSE '' END,
                    id_issue_date,id_expiry_date,work_permit_start_date,work_permit_expiry_date,
                    medical_insurance_start_date,medical_insurance_expiry_date,
                    COALESCE(nationality,''),birth_date,COALESCE(join_date,''),
                    COALESCE(department,''),COALESCE(job_title,''),salary,work_location,email,
                    bank_name,bank_account_number,iban,status,performance_rating,notes,created_at,updated_at
                FROM employees
                """
            )
            conn.execute("DROP TABLE employees")
            conn.execute("ALTER TABLE employees_v19_partial RENAME TO employees")
            for statement in (
                "CREATE INDEX idx_employees_name ON employees(name)",
                "CREATE INDEX idx_employees_department ON employees(department)",
                "CREATE INDEX idx_employees_id_type ON employees(id_type)",
                "CREATE INDEX idx_employees_id_expiry ON employees(id_expiry_date)",
                "CREATE INDEX idx_employees_work_permit_expiry ON employees(work_permit_expiry_date)",
                "CREATE INDEX idx_employees_medical_insurance_expiry ON employees(medical_insurance_expiry_date)",
                "CREATE INDEX idx_employees_rating ON employees(performance_rating)",
            ):
                conn.execute(statement)
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
            issues = conn.execute("PRAGMA foreign_key_check").fetchall()
            if issues:
                raise DatabaseError("فشل فحص العلاقات بعد ترقية ملفات الموظفين الجزئية.")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate_employee_codes_from_identity(self) -> None:
        """Align legacy employee numbers with the last four identity digits."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id,emp_code,id_number FROM employees ORDER BY id"
            ).fetchall()
            suffixes: dict[str, list[int]] = {}
            for row in rows:
                identity_digits = normalize_id_number(str(row["id_number"] or ""))
                if len(identity_digits) >= 4:
                    suffixes.setdefault(identity_digits[-4:], []).append(int(row["id"]))
            conflicts = {
                suffix: ids for suffix, ids in suffixes.items() if len(ids) > 1
            }
            eligible = [
                (int(row["id"]), normalize_id_number(str(row["id_number"] or ""))[-4:])
                for row in rows
                if len(normalize_id_number(str(row["id_number"] or ""))) >= 4
                and normalize_id_number(str(row["id_number"] or ""))[-4:] not in conflicts
            ]
            for employee_id, code in eligible:
                conn.execute(
                    "UPDATE employees SET emp_code=? WHERE id=? AND emp_code<>?",
                    (f"__V19_ID_{employee_id}", employee_id, code),
                )
            for employee_id, code in eligible:
                conn.execute(
                    "UPDATE employees SET emp_code=? WHERE id=?",
                    (code, employee_id),
                )
            conflict_value = ",".join(
                f"{suffix}:{'-'.join(str(item) for item in ids)}"
                for suffix, ids in sorted(conflicts.items())
            )
            conn.execute(
                "INSERT INTO app_settings(key,value) VALUES('migration.employee_code_conflicts',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (conflict_value,),
            )

    def _clean_legacy_unknown_date_values(self) -> None:
        date_fields = (
            "birth_date", "join_date", "id_issue_date", "id_expiry_date",
            "work_permit_start_date", "work_permit_expiry_date",
            "medical_insurance_start_date", "medical_insurance_expiry_date",
        )
        nullable = set(date_fields) - {"join_date"}
        with self.connection() as conn:
            for field in date_fields:
                replacement = None if field in nullable else ""
                conn.execute(
                    f"UPDATE employees SET {field}=? WHERE LOWER(TRIM(COALESCE({field},'')))='none'",
                    (replacement,),
                )

    def health_check(self) -> dict[str, Any]:
        """Return a read-only diagnostic snapshot suitable for local support."""
        with self.connection() as conn:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            foreign_key_issues = [
                dict(row) for row in conn.execute("PRAGMA foreign_key_check").fetchall()
            ]
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
            foreign_keys = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
            tables = int(
                conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchone()[0]
            )
            record_counts = {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "users",
                    "employees",
                    "attendance",
                    "leave_requests",
                    "audit_logs",
                )
            }
        return {
            "app_version": APP_VERSION,
            "database": str(self.path),
            "database_size_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "integrity_check": integrity,
            "foreign_keys_enabled": bool(foreign_keys),
            "foreign_key_issues": foreign_key_issues,
            "journal_mode": journal_mode,
            "tables": tables,
            "record_counts": record_counts,
            "ok": integrity == "ok" and not foreign_key_issues and bool(foreign_keys),
        }

    def optimize_database(self, actor: dict[str, Any]) -> dict[str, Any]:
        """Compact and optimize a healthy database, recording the admin action."""
        if actor.get("role") != "admin":
            raise PermissionError("صيانة قاعدة البيانات متاحة لمدير النظام فقط.")
        before = self.health_check()
        if not before["ok"]:
            raise DatabaseError(
                "تعذر بدء الصيانة لأن فحص سلامة قاعدة البيانات لم ينجح. "
                "استعد نسخة سليمة أو تواصل مع الدعم الفني."
            )
        with self._backup_lock:
            conn = sqlite3.connect(self.path, timeout=60, isolation_level=None)
            try:
                conn.execute("PRAGMA busy_timeout = 60000")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                conn.execute("PRAGMA optimize")
                conn.execute("VACUUM")
            finally:
                conn.close()
        after = self.health_check()
        if not after["ok"]:
            raise DatabaseError("لم ينجح فحص السلامة بعد صيانة قاعدة البيانات.")
        result = {
            "ok": True,
            "before_size_bytes": int(before["database_size_bytes"]),
            "after_size_bytes": int(after["database_size_bytes"]),
            "reclaimed_bytes": max(
                0,
                int(before["database_size_bytes"]) - int(after["database_size_bytes"]),
            ),
            "tables": int(after["tables"]),
            "journal_mode": str(after["journal_mode"]),
        }
        with self.connection() as conn:
            self._audit_conn(
                conn,
                actor,
                "DATABASE_MAINTENANCE",
                "database",
                self.path.name,
                result,
            )
        return result

    # ---------- Users & authentication ----------
    def has_users(self) -> bool:
        with self.connection() as conn:
            return conn.execute("SELECT EXISTS(SELECT 1 FROM users)").fetchone()[0] == 1

    def get_setting(self, key: str, default: str = "") -> str:
        """قراءة إعداد واجهة بسيط؛ لا تُستخدم هذه الآلية لحفظ كلمات المرور."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key=?",
                (str(key).strip(),),
            ).fetchone()
        return str(row[0]) if row else str(default)

    def set_setting(self, key: str, value: str) -> None:
        """حفظ قيمة نصية غير حساسة مثل اسم آخر مستخدم ناجح."""
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(key).strip(), str(value)),
            )

    def session_timeout_minutes(self) -> int:
        try:
            value = int(self.get_setting("security.session_timeout_minutes", "30"))
        except ValueError:
            value = 30
        return min(240, max(5, value))

    def set_session_timeout_minutes(self, minutes: int, actor: dict[str, Any]) -> int:
        if actor.get("role") != "admin":
            raise PermissionError("تخصيص مهلة الجلسة متاح لمدير النظام فقط.")
        value = int(minutes)
        if value < 5 or value > 240:
            raise ValueError("مهلة الجلسة يجب أن تكون بين 5 و240 دقيقة.")
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("security.session_timeout_minutes", str(value)),
            )
            self._audit_conn(
                conn,
                actor,
                "UPDATE_SESSION_TIMEOUT",
                "settings",
                "security.session_timeout_minutes",
                {"minutes": value},
            )
        return value

    def save_view(
        self,
        scope: str,
        name: str,
        filters: dict[str, Any],
        actor: dict[str, Any],
        *,
        is_shared: bool = False,
        is_default: bool = False,
    ) -> int:
        allowed_scopes = {"employees", "analytics", "audit"}
        clean_scope = _clean_text(scope).casefold()
        clean_name = _clean_text(name)
        if clean_scope not in allowed_scopes:
            raise ValueError("نوع مساحة العمل غير مدعوم.")
        if not clean_name or len(clean_name) > 80:
            raise ValueError("اسم مساحة العمل مطلوب وبحد أقصى 80 حرفاً.")
        if not isinstance(filters, dict):
            raise ValueError("مرشحات مساحة العمل غير صالحة.")
        owner_id = int(actor.get("id") or 0)
        if owner_id <= 0:
            raise PermissionError("يلزم تسجيل الدخول لحفظ مساحة العمل.")
        if is_shared and actor.get("role") != "admin":
            raise PermissionError("مشاركة مساحات العمل متاحة لمدير النظام فقط.")
        payload = json.dumps(filters, ensure_ascii=False, sort_keys=True)
        if len(payload.encode("utf-8")) > 32_000:
            raise ValueError("حجم مرشحات مساحة العمل أكبر من الحد المسموح.")
        with self.connection() as conn:
            if is_default:
                conn.execute(
                    "UPDATE saved_views SET is_default=0 WHERE owner_user_id=? AND scope=?",
                    (owner_id, clean_scope),
                )
            existing = conn.execute(
                "SELECT id FROM saved_views WHERE owner_user_id=? AND scope=? AND name=?",
                (owner_id, clean_scope, clean_name),
            ).fetchone()
            if existing:
                view_id = int(existing["id"])
                conn.execute(
                    """UPDATE saved_views
                       SET filters_json=?,is_shared=?,is_default=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (payload, int(is_shared), int(is_default), view_id),
                )
                action = "UPDATE"
            else:
                cursor = conn.execute(
                    """INSERT INTO saved_views(
                           owner_user_id,scope,name,filters_json,is_shared,is_default
                       ) VALUES(?,?,?,?,?,?)""",
                    (owner_id, clean_scope, clean_name, payload, int(is_shared), int(is_default)),
                )
                view_id = int(cursor.lastrowid)
                action = "CREATE"
            self._audit_conn(
                conn,
                actor,
                action,
                "saved_view",
                view_id,
                {"scope": clean_scope, "name": clean_name, "shared": bool(is_shared)},
            )
        return view_id

    def list_saved_views(self, scope: str, actor: dict[str, Any]) -> list[dict[str, Any]]:
        clean_scope = _clean_text(scope).casefold()
        owner_id = int(actor.get("id") or 0)
        if owner_id <= 0:
            return []
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT sv.*,u.full_name AS owner_name
                   FROM saved_views sv JOIN users u ON u.id=sv.owner_user_id
                   WHERE sv.scope=? AND (sv.owner_user_id=? OR sv.is_shared=1)
                   ORDER BY sv.is_default DESC,sv.is_shared ASC,sv.name COLLATE NOCASE""",
                (clean_scope, owner_id),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                filters = json.loads(str(item.pop("filters_json") or "{}"))
            except (TypeError, ValueError):
                filters = {}
            item["filters"] = filters if isinstance(filters, dict) else {}
            result.append(item)
        return result

    def delete_saved_view(self, view_id: int, actor: dict[str, Any]) -> None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT id,owner_user_id,scope,name FROM saved_views WHERE id=?",
                (int(view_id),),
            ).fetchone()
            if not row:
                raise DatabaseError("مساحة العمل غير موجودة.")
            is_owner = int(row["owner_user_id"]) == int(actor.get("id") or 0)
            if not is_owner and actor.get("role") != "admin":
                raise PermissionError("لا تملك صلاحية حذف مساحة العمل هذه.")
            conn.execute("DELETE FROM saved_views WHERE id=?", (int(view_id),))
            self._audit_conn(
                conn,
                actor,
                "DELETE",
                "saved_view",
                int(view_id),
                {"scope": row["scope"], "name": row["name"]},
            )

    def company_profile(self, *, include_defaults: bool = True) -> dict[str, str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT key,value FROM app_settings WHERE key LIKE ?",
                (f"{COMPANY_SETTING_PREFIX}%",),
            ).fetchall()
        values = {
            str(row["key"])[len(COMPANY_SETTING_PREFIX):]: str(row["value"] or "")
            for row in rows
            if str(row["key"])[len(COMPANY_SETTING_PREFIX):] in COMPANY_PROFILE_FIELDS
        }
        return normalize_company_profile(values, include_defaults=include_defaults)

    def save_company_profile(
        self, data: dict[str, Any], actor: dict[str, Any]
    ) -> dict[str, str]:
        if not actor or actor.get("role") != "admin":
            raise PermissionError("تعديل بيانات الشركة متاح للمدير فقط.")
        normalized = normalize_company_profile(data, include_defaults=False)
        current = self.company_profile(include_defaults=False)
        changed_fields = [
            COMPANY_PROFILE_LABELS[key]
            for key in COMPANY_PROFILE_FIELDS
            if current.get(key, "") != normalized.get(key, "")
        ]
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for key in COMPANY_PROFILE_FIELDS:
                setting_key = f"{COMPANY_SETTING_PREFIX}{key}"
                value = normalized[key]
                if value:
                    conn.execute(
                        "INSERT INTO app_settings(key,value) VALUES(?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (setting_key, value),
                    )
                else:
                    conn.execute("DELETE FROM app_settings WHERE key=?", (setting_key,))
            self._audit_conn(
                conn,
                actor,
                "UPDATE",
                "company_profile",
                None,
                {"changed_fields": changed_fields},
            )
        return self.company_profile()

    def create_user(
        self,
        username: str,
        full_name: str,
        password: str,
        role: str,
        actor: dict[str, Any] | None = None,
    ) -> int:
        username = username.strip()
        full_name = full_name.strip()
        if not username or not full_name:
            raise ValueError("اسم المستخدم والاسم الكامل مطلوبان.")
        if role not in ROLES:
            raise ValueError("الصلاحية غير صحيحة.")
        if actor and actor.get("role") != "admin":
            raise PermissionError("إدارة المستخدمين متاحة للمدير فقط.")
        salt, digest = hash_password(password)
        try:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                users_count = int(
                    conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
                )
                if actor is None:
                    if users_count:
                        raise PermissionError(
                            "إنشاء مستخدم دون جلسة مدير مسموح للحساب الأول فقط."
                        )
                    if role != "admin":
                        raise PermissionError(
                            "يجب أن يكون الحساب الأول مدير نظام."
                        )
                duplicate = conn.execute(
                    "SELECT 1 FROM users WHERE username=? COLLATE NOCASE LIMIT 1",
                    (username,),
                ).fetchone()
                if duplicate:
                    raise DatabaseError(
                        "اسم المستخدم موجود مسبقًا، حتى إن اختلفت حالة الأحرف."
                    )
                cur = conn.execute(
                    "INSERT INTO users(username, full_name, role, password_salt, password_hash) VALUES(?,?,?,?,?)",
                    (username, full_name, role, salt, digest),
                )
                user_id = int(cur.lastrowid)
                self._audit_conn(conn, actor, "CREATE", "user", user_id, {"username": username, "role": role})
                return user_id
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("اسم المستخدم موجود مسبقًا.") from exc

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        clean_username = username.strip()
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username=? COLLATE NOCASE AND is_active=1", (clean_username,)
            ).fetchone()
            now = datetime.now()
            if row and row["locked_until"]:
                try:
                    locked_until = datetime.fromisoformat(row["locked_until"])
                except ValueError:
                    locked_until = now
                if locked_until > now:
                    self._audit_conn(conn, None, "LOGIN_LOCKED", "session", clean_username, {
                        "locked_until": row["locked_until"]
                    })
                    return None
            if not row or not verify_password(password, row["password_salt"], row["password_hash"]):
                if row:
                    attempts = int(row["failed_attempts"] or 0) + 1
                    locked_until = None
                    if attempts >= 5:
                        locked_until = (now + timedelta(minutes=15)).isoformat(timespec="seconds")
                        attempts = 0
                    conn.execute(
                        "UPDATE users SET failed_attempts=?,locked_until=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (attempts, locked_until, row["id"]),
                    )
                self._audit_conn(conn, None, "LOGIN_FAILED", "session", clean_username, {})
                return None
            conn.execute(
                "UPDATE users SET failed_attempts=0,locked_until=NULL,last_login=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (now.isoformat(timespec="seconds"), row["id"]),
            )
            conn.execute(
                "INSERT INTO app_settings(key,value) VALUES('last_username',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(row["username"]),),
            )
            user = dict(row)
            user["failed_attempts"] = 0
            user["locked_until"] = None
            user["last_login"] = now.isoformat(timespec="seconds")
            user.pop("password_salt", None)
            user.pop("password_hash", None)
            self._audit_conn(conn, user, "LOGIN", "session", user["id"], {})
            return user

    def list_users(self) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, username, full_name, role, is_active, created_at FROM users ORDER BY username"
            ).fetchall()
            return [dict(r) for r in rows]

    def update_user(
        self, user_id: int, *, role: str | None = None, is_active: bool | None = None,
        new_password: str | None = None, actor: dict[str, Any]
    ) -> None:
        if actor.get("role") != "admin":
            raise PermissionError("إدارة المستخدمين متاحة للمدير فقط.")
        updates: list[str] = []
        values: list[Any] = []
        details: dict[str, Any] = {}
        if role is not None:
            if role not in ROLES:
                raise ValueError("الصلاحية غير صحيحة.")
            updates.append("role=?")
            values.append(role)
            details["role"] = role
        if is_active is not None:
            updates.append("is_active=?")
            values.append(1 if is_active else 0)
            details["is_active"] = bool(is_active)
        if new_password:
            salt, digest = hash_password(new_password)
            updates.extend(["password_salt=?", "password_hash=?"])
            values.extend([salt, digest])
            details["password_changed"] = True
        if not updates:
            return
        with self.connection() as conn:
            current = conn.execute("SELECT role, is_active FROM users WHERE id=?", (user_id,)).fetchone()
            if not current:
                raise DatabaseError("المستخدم غير موجود.")
            resulting_role = role if role is not None else current["role"]
            resulting_active = (1 if is_active else 0) if is_active is not None else current["is_active"]
            if current["role"] == "admin" and current["is_active"] and not (resulting_role == "admin" and resulting_active):
                other_admins = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1 AND id<>?", (user_id,)
                ).fetchone()[0]
                if other_admins == 0:
                    raise DatabaseError("لا يمكن تعطيل أو تخفيض صلاحية آخر مدير نشط.")
            updates.append("updated_at=CURRENT_TIMESTAMP")
            values.append(user_id)
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id=?", values)
            self._audit_conn(conn, actor, "UPDATE", "user", user_id, details)

    # ---------- Employees ----------
    @staticmethod
    def _next_draft_employee_code_conn(conn: sqlite3.Connection) -> str:
        rows = conn.execute(
            "SELECT emp_code FROM employees WHERE emp_code LIKE 'DRAFT%'"
        ).fetchall()
        numbers = []
        for row in rows:
            suffix = str(row[0])[5:]
            if suffix.isdigit():
                numbers.append(int(suffix))
        return f"DRAFT{(max(numbers, default=0) + 1):04d}"

    def next_employee_code(self) -> str:
        """Return a temporary code used only until an identity is available."""
        with self.connection() as conn:
            return self._next_draft_employee_code_conn(conn)

    @staticmethod
    def _employee_code_for_identity_conn(
        conn: sqlite3.Connection,
        id_number: str,
        *,
        employee_id: int | None = None,
        current_code: str = "",
    ) -> str:
        if not id_number:
            if current_code.startswith("DRAFT"):
                return current_code
            return Database._next_draft_employee_code_conn(conn)
        identity_digits = normalize_id_number(id_number)
        if len(identity_digits) < 4:
            raise ValueError(
                "رقم الوثيقة يجب أن يحتوي أربعة أرقام على الأقل لاشتقاق رقم الموظف."
            )
        code = identity_digits[-4:]
        candidates = conn.execute(
            "SELECT id,name,emp_code,id_number FROM employees WHERE id<>?",
            (int(employee_id or 0),),
        ).fetchall()
        for conflict in candidates:
            other_digits = normalize_id_number(str(conflict["id_number"] or ""))
            if conflict["emp_code"] == code or (
                len(other_digits) >= 4 and other_digits[-4:] == code
            ):
                raise DatabaseError(
                    f"آخر أربعة أرقام من الهوية ({code}) مستخدمة في ملف الموظف "
                    f"{conflict['name']}. يجب معالجة التعارض قبل الحفظ."
                )
        return code

    @staticmethod
    def _validate_employee(
        data: dict[str, Any], *, allow_incomplete_import: bool = False
    ) -> dict[str, Any]:
        required = ["name"]
        missing = [key for key in required if str(data.get(key, "")).strip() == ""]
        if missing:
            raise ValueError("اسم الموظف مطلوب لإنشاء الملف.")
        phone = "".join(ch for ch in str(data.get("phone", "")) if ch.isdigit())
        if phone and len(phone) != 10:
            raise ValueError("رقم الجوال يجب أن يتكون من 10 أرقام.")
        nationality = _clean_text(data.get("nationality"))
        requested_identity_type = _clean_text(data.get("id_type"))
        raw_identity = normalize_identity_document_number(
            str(data.get("id_number", "") or "")
        )
        if requested_identity_type or nationality:
            identity_type = validate_identity_type(nationality, requested_identity_type)
        elif raw_identity.isdigit() and len(raw_identity) == 10 and raw_identity[:1] in {"1", "2"}:
            identity_type = "هوية وطنية" if raw_identity.startswith("1") else "إقامة"
        else:
            identity_type = ""
        if identity_type == "جواز سفر":
            id_number = raw_identity
            if id_number and not 5 <= len(id_number) <= 20:
                raise ValueError("رقم جواز السفر يجب أن يتكون من 5 إلى 20 حرفًا أو رقمًا.")
            if id_number and len(normalize_id_number(id_number)) < 4:
                raise ValueError("رقم جواز السفر يجب أن يحتوي أربعة أرقام على الأقل لاشتقاق رقم الموظف.")
        else:
            id_number = normalize_id_number(raw_identity)
            if id_number and len(id_number) != 10:
                raise ValueError("رقم الهوية الوطنية أو الإقامة يجب أن يتكون من 10 أرقام.")
        raw_salary = data.get("salary")
        if raw_salary in (None, ""):
            salary = None
        else:
            try:
                salary_value = money(raw_salary)
            except (TypeError, ValueError) as exc:
                raise ValueError("الراتب يجب أن يكون رقمًا.") from exc
            if salary_value < 0:
                raise ValueError("الراتب لا يمكن أن يكون سالبًا.")
            salary = money_float(salary_value)
        status = str(data.get("status") or "نشط").strip()
        if status not in EMPLOYEE_STATUSES:
            raise ValueError("حالة الموظف غير صحيحة.")
        for field in (
            "birth_date", "join_date", "id_issue_date", "id_expiry_date",
            "work_permit_start_date", "work_permit_expiry_date",
            "medical_insurance_start_date", "medical_insurance_expiry_date",
        ):
            raw_value = data.get(field, "")
            value = "" if raw_value is None else str(raw_value).strip()
            if value:
                try:
                    datetime.strptime(value, "%Y-%m-%d")
                except ValueError as exc:
                    raise ValueError(f"صيغة {field} يجب أن تكون YYYY-MM-DD.") from exc
        for start_field, end_field, label in (
            ("id_issue_date", "id_expiry_date", "الهوية أو الإقامة أو الجواز"),
            ("work_permit_start_date", "work_permit_expiry_date", "كرت العمل"),
            (
                "medical_insurance_start_date",
                "medical_insurance_expiry_date",
                "التأمين الطبي",
            ),
        ):
            start_value = str(data.get(start_field, "") or "").strip()
            end_value = str(data.get(end_field, "") or "").strip()
            if start_value and end_value and start_value > end_value:
                raise ValueError(f"تاريخ بداية {label} لا يمكن أن يلي تاريخ الانتهاء.")
        bank_name = str(data.get("bank_name", "") or "").strip()
        bank_account = normalize_bank_account(data.get("bank_account_number", ""))
        if bank_account and (not bank_account.isalnum() or not 4 <= len(bank_account) <= 34):
            raise ValueError("رقم الحساب البنكي يجب أن يتكون من 4 إلى 34 حرفًا أو رقمًا.")
        iban = validate_saudi_iban(data.get("iban", ""))
        rating = normalize_rating(data.get("performance_rating", 0))
        normalized = dict(data)
        normalized.update({
            "phone": phone,
            "id_number": id_number,
            "salary": salary,
            "status": status,
            "nationality": nationality,
            "id_type": identity_type,
            "performance_rating": rating,
            "bank_name": bank_name,
            "bank_account_number": bank_account,
            "iban": iban,
            "join_date": _clean_text(data.get("join_date")),
            "department": _clean_text(data.get("department")),
            "job_title": _clean_text(data.get("job_title")),
        })
        return normalized

    @staticmethod
    def _redact_employee_audit_values(changes: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(changes)
        for field in ("id_number", "bank_account_number", "iban"):
            if field in redacted:
                redacted[field] = "<changed>"
        return redacted

    @staticmethod
    def _sync_document_dates_conn(
        conn: sqlite3.Connection, employee_id: int, employee: dict[str, Any]
    ) -> None:
        table_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='employee_documents'"
        ).fetchone()
        if not table_exists:
            return
        mappings = [
            (
                "كرت عمل",
                employee.get("work_permit_start_date"),
                employee.get("work_permit_expiry_date"),
            ),
            (
                "تأمين طبي",
                employee.get("medical_insurance_start_date"),
                employee.get("medical_insurance_expiry_date"),
            ),
        ]
        identity_type = str(employee.get("id_type") or "").strip()
        if identity_type in IDENTITY_TYPES:
            mappings.insert(
                0,
                (identity_type, employee.get("id_issue_date"), employee.get("id_expiry_date")),
            )
        for document_type, start_date, expiry_date in mappings:
            conn.execute(
                """UPDATE employee_documents SET start_date=?,expiry_date=?
                   WHERE id=(
                       SELECT id FROM employee_documents
                       WHERE employee_id=? AND document_type=?
                       ORDER BY id DESC LIMIT 1
                   )""",
                (
                    str(start_date or "").strip() or None,
                    str(expiry_date or "").strip() or None,
                    employee_id,
                    document_type,
                ),
            )

    def add_employee(self, data: dict[str, Any], actor: dict[str, Any]) -> int:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("لا تملك صلاحية إضافة موظفين.")
        data = self._validate_employee(data)
        try:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                emp_code = self._employee_code_for_identity_conn(conn, data["id_number"])
                cur = conn.execute(
                    """
                    INSERT INTO employees(
                        emp_code, name, phone, id_number, id_hash, id_type, id_issue_date, id_expiry_date,
                        work_permit_start_date, work_permit_expiry_date,
                        medical_insurance_start_date, medical_insurance_expiry_date,
                        nationality, birth_date, join_date,
                        department, job_title, salary, work_location, email,
                        bank_name, bank_account_number, iban, status, performance_rating, notes
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        emp_code, data["name"].strip(), data["phone"], data["id_number"],
                        id_fingerprint(data["id_number"]) if data["id_number"] else None, data["id_type"],
                        _clean_text(data.get("id_issue_date")) or None,
                        _clean_text(data.get("id_expiry_date")) or None,
                        _clean_text(data.get("work_permit_start_date")) or None,
                        _clean_text(data.get("work_permit_expiry_date")) or None,
                        _clean_text(data.get("medical_insurance_start_date")) or None,
                        _clean_text(data.get("medical_insurance_expiry_date")) or None,
                        data["nationality"],
                        _clean_text(data.get("birth_date")) or None, data["join_date"],
                        data["department"].strip(), data["job_title"].strip(), data["salary"],
                        _clean_text(data.get("work_location")), _clean_text(data.get("email")),
                        data["bank_name"], data["bank_account_number"], data["iban"],
                        data["status"], data["performance_rating"], _clean_text(data.get("notes")),
                    ),
                )
                employee_id = int(cur.lastrowid)
                self._audit_conn(conn, actor, "CREATE", "employee", employee_id, {"emp_code": emp_code})
                return employee_id
        except sqlite3.IntegrityError as exc:
            text = str(exc).lower()
            if "id_hash" in text:
                raise DatabaseError("رقم الهوية الوطنية أو الإقامة مستخدم لموظف آخر.") from exc
            raise DatabaseError("رقم الموظف أو بيانات فريدة أخرى موجودة مسبقًا.") from exc

    def get_employee(self, employee_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
            if not row:
                return None
            employee = dict(row)
            employee["custom_values"] = {
                int(value["field_id"]): str(value["value_text"] or "")
                for value in conn.execute(
                    "SELECT field_id,value_text FROM employee_custom_values WHERE employee_id=?",
                    (employee_id,),
                ).fetchall()
            }
            return employee

    def get_employee_by_code(self, emp_code: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
            return dict(row) if row else None

    def update_employee(self, employee_id: int, data: dict[str, Any], actor: dict[str, Any]) -> None:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("لا تملك صلاحية تعديل الموظفين.")
        current = self.get_employee(employee_id)
        if not current:
            raise DatabaseError("الموظف غير موجود.")
        merged = dict(current)
        merged.update(data)
        if "nationality" in data and "id_type" not in data:
            merged["id_type"] = (
                identity_type_for_nationality(data.get("nationality"))
                if str(data.get("nationality") or "").strip() else ""
            )
        merged = self._validate_employee(merged)
        try:
            with self.connection() as conn:
                conn.execute("BEGIN IMMEDIATE")
                employee_code = self._employee_code_for_identity_conn(
                    conn,
                    merged["id_number"],
                    employee_id=employee_id,
                    current_code=str(current.get("emp_code") or ""),
                )
                conn.execute(
                    """
                    UPDATE employees SET
                        emp_code=?, name=?, phone=?, id_number=?, id_hash=?, id_type=?, id_issue_date=?, id_expiry_date=?,
                        work_permit_start_date=?, work_permit_expiry_date=?,
                        medical_insurance_start_date=?, medical_insurance_expiry_date=?,
                        nationality=?, birth_date=?, join_date=?,
                        department=?, job_title=?, salary=?, work_location=?, email=?,
                        bank_name=?, bank_account_number=?, iban=?, status=?, performance_rating=?, notes=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (
                        employee_code, merged["name"].strip(), merged["phone"], merged["id_number"],
                        id_fingerprint(merged["id_number"]) if merged["id_number"] else None, merged["id_type"],
                        _clean_text(merged.get("id_issue_date")) or None,
                        _clean_text(merged.get("id_expiry_date")) or None,
                        _clean_text(merged.get("work_permit_start_date")) or None,
                        _clean_text(merged.get("work_permit_expiry_date")) or None,
                        _clean_text(merged.get("medical_insurance_start_date")) or None,
                        _clean_text(merged.get("medical_insurance_expiry_date")) or None,
                        merged["nationality"],
                        _clean_text(merged.get("birth_date")) or None, merged["join_date"],
                        merged["department"].strip(), merged["job_title"].strip(), merged["salary"],
                        _clean_text(merged.get("work_location")), _clean_text(merged.get("email")),
                        merged["bank_name"], merged["bank_account_number"], merged["iban"],
                        merged["status"], merged["performance_rating"], _clean_text(merged.get("notes")), employee_id,
                    ),
                )
                changed = {k: merged.get(k) for k in data.keys() if current.get(k) != merged.get(k)}
                if current.get("emp_code") != employee_code:
                    changed["emp_code"] = employee_code
                self._sync_document_dates_conn(conn, employee_id, merged)
                self._audit_conn(
                    conn,
                    actor,
                    "UPDATE",
                    "employee",
                    employee_id,
                    self._redact_employee_audit_values(changed),
                )
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("رقم الهوية الوطنية أو الإقامة مستخدم لموظف آخر.") from exc

    def delete_employee(self, employee_id: int, actor: dict[str, Any]) -> None:
        if actor.get("role") != "admin":
            raise PermissionError("حذف الموظفين متاح للمدير فقط.")
        employee = self.get_employee(employee_id)
        if not employee:
            return
        with self.connection() as conn:
            conn.execute("DELETE FROM employees WHERE id=?", (employee_id,))
            self._audit_conn(conn, actor, "DELETE", "employee", employee_id, {"emp_code": employee["emp_code"]})

    def list_employees(
        self,
        *, search: str = "", page: int = 1, page_size: int = 25,
        sort_by: str = "created_at", descending: bool = True,
        identity_status: str = "all",
        department: str = "", employee_status: str = "",
        joined_from: str = "", joined_to: str = "",
        salary_min: float | str | None = None,
        salary_max: float | str | None = None,
        custom_field_id: int | None = None, custom_value: str = "",
    ) -> tuple[list[dict[str, Any]], int]:
        sort_by = sort_by if sort_by in ALLOWED_SORT_COLUMNS else "created_at"
        direction = "DESC" if descending else "ASC"
        page = max(1, int(page))
        page_size = max(1, min(200, int(page_size)))
        query = search.strip()
        clauses: list[str] = []
        params: list[Any] = []
        if query:
            like = f"%{query}%"
            clauses.append(
                "(emp_code LIKE ? OR name LIKE ? OR phone LIKE ? OR department LIKE ? "
                "OR job_title LIKE ? OR status LIKE ? OR id_number LIKE ? "
                "OR nationality LIKE ? OR id_type LIKE ? OR EXISTS ("
                "SELECT 1 FROM employee_custom_values cv "
                "JOIN employee_custom_fields cf ON cf.id=cv.field_id "
                "WHERE cv.employee_id=employees.id AND cf.is_active=1 "
                "AND (cf.label LIKE ? OR cv.value_text LIKE ?)))"
            )
            params.extend([like] * 11)

        today = date.today()
        deadline = today + timedelta(days=90)
        status = str(identity_status or "all").strip().lower()
        if status == "expired":
            clauses.append("TRIM(COALESCE(id_expiry_date,''))<>'' AND date(id_expiry_date)<date(?)")
            params.append(today.isoformat())
        elif status == "expiring":
            clauses.append("date(id_expiry_date) BETWEEN date(?) AND date(?)")
            params.extend([today.isoformat(), deadline.isoformat()])
        elif status == "missing":
            clauses.append("TRIM(COALESCE(id_expiry_date,''))='' ")
        elif status == "valid":
            clauses.append("date(id_expiry_date)>date(?)")
            params.append(deadline.isoformat())

        if department and department != "الكل":
            clauses.append("department=?")
            params.append(department)
        if employee_status and employee_status != "الكل":
            clauses.append("status=?")
            params.append(employee_status)
        if joined_from:
            self._parse_iso_date(joined_from, "تاريخ بداية الالتحاق")
            clauses.append("date(join_date)>=date(?)")
            params.append(joined_from)
        if joined_to:
            self._parse_iso_date(joined_to, "تاريخ نهاية الالتحاق")
            clauses.append("date(join_date)<=date(?)")
            params.append(joined_to)
        if joined_from and joined_to and joined_from > joined_to:
            raise ValueError("تاريخ بداية الالتحاق يجب ألا يتجاوز تاريخ النهاية.")
        if salary_min not in (None, ""):
            try:
                minimum = float(salary_min)
            except (TypeError, ValueError) as exc:
                raise ValueError("الحد الأدنى للراتب غير صحيح.") from exc
            clauses.append("COALESCE(salary,0)>=?")
            params.append(max(0.0, minimum))
        if salary_max not in (None, ""):
            try:
                maximum = float(salary_max)
            except (TypeError, ValueError) as exc:
                raise ValueError("الحد الأعلى للراتب غير صحيح.") from exc
            clauses.append("COALESCE(salary,0)<=?")
            params.append(max(0.0, maximum))
        if salary_min not in (None, "") and salary_max not in (None, ""):
            if float(salary_min) > float(salary_max):
                raise ValueError("الحد الأدنى للراتب يجب ألا يتجاوز الحد الأعلى.")
        if custom_field_id:
            custom_clause = (
                "EXISTS (SELECT 1 FROM employee_custom_values filter_cv "
                "WHERE filter_cv.employee_id=employees.id AND filter_cv.field_id=?"
            )
            params.append(int(custom_field_id))
            if custom_value.strip():
                custom_clause += " AND filter_cv.value_text LIKE ?"
                params.append(f"%{custom_value.strip()}%")
            clauses.append(custom_clause + ")")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connection() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM employees {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT * FROM employees {where} ORDER BY {sort_by} {direction}, id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
            return [dict(r) for r in rows], int(total)

    def employee_filter_values(self) -> dict[str, list[dict[str, Any]] | list[str]]:
        with self.connection() as conn:
            departments = [
                str(row[0]) for row in conn.execute(
                    "SELECT DISTINCT department FROM employees WHERE TRIM(COALESCE(department,''))<>'' ORDER BY department"
                ).fetchall()
            ]
            employees = [
                dict(row) for row in conn.execute(
                    "SELECT id,emp_code,name FROM employees ORDER BY name,emp_code"
                ).fetchall()
            ]
        return {
            "departments": departments,
            "statuses": list(EMPLOYEE_STATUSES),
            "employees": employees,
            "custom_fields": self.list_employee_custom_fields(active_only=True),
        }

    # ---------- Custom employee fields ----------
    def list_employee_custom_fields(self, active_only: bool = False) -> list[dict[str, Any]]:
        where = "WHERE is_active=1" if active_only else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM employee_custom_fields {where} ORDER BY sort_order,label"
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            try:
                options = json.loads(str(item.pop("options_json") or "[]"))
            except (TypeError, json.JSONDecodeError):
                options = []
            item["options"] = [str(value).strip() for value in options if str(value).strip()]
            result.append(item)
        return result

    def save_employee_custom_field(
        self,
        data: dict[str, Any],
        actor: dict[str, Any],
        field_id: int | None = None,
    ) -> int:
        if actor.get("role") != "admin":
            raise PermissionError("إدارة الحقول المخصصة متاحة للمدير فقط.")
        label = _clean_text(data.get("label"))
        field_type = _clean_text(data.get("field_type")).lower() or "text"
        if not label:
            raise ValueError("اسم الحقل المخصص مطلوب.")
        if len(label) > 80:
            raise ValueError("اسم الحقل المخصص طويل جدًا.")
        if field_type not in {"text", "number", "date", "choice"}:
            raise ValueError("نوع الحقل المخصص غير صحيح.")
        raw_options = data.get("options", [])
        if isinstance(raw_options, str):
            raw_options = re.split(r"[,،\n]", raw_options)
        options = []
        for raw in raw_options if isinstance(raw_options, (list, tuple)) else []:
            value = str(raw).strip()
            if value and value.casefold() not in {item.casefold() for item in options}:
                options.append(value)
        if field_type == "choice" and not options:
            raise ValueError("أضف خيارًا واحدًا على الأقل لحقل الاختيار.")
        if field_type != "choice":
            options = []
        required = 1 if data.get("is_required") else 0
        active = 1 if data.get("is_active", True) else 0
        try:
            sort_order = max(0, min(10_000, int(data.get("sort_order", 100))))
        except (TypeError, ValueError):
            sort_order = 100
        with self.connection() as conn:
            if field_id:
                exists = conn.execute(
                    "SELECT 1 FROM employee_custom_fields WHERE id=?", (field_id,)
                ).fetchone()
                if not exists:
                    raise DatabaseError("الحقل المخصص غير موجود.")
                conn.execute(
                    """UPDATE employee_custom_fields SET label=?,field_type=?,options_json=?,
                       is_required=?,is_active=?,sort_order=?,updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (label, field_type, json.dumps(options, ensure_ascii=False), required, active, sort_order, field_id),
                )
                saved_id = int(field_id)
                action = "UPDATE_CUSTOM_FIELD"
            else:
                cursor = conn.execute(
                    """INSERT INTO employee_custom_fields(
                           label,field_type,options_json,is_required,is_active,sort_order,created_by
                       ) VALUES(?,?,?,?,?,?,?)""",
                    (label, field_type, json.dumps(options, ensure_ascii=False), required, active, sort_order, actor.get("id")),
                )
                saved_id = int(cursor.lastrowid)
                action = "CREATE_CUSTOM_FIELD"
            self._audit_conn(
                conn, actor, action, "employee_custom_field", saved_id,
                {"label": label, "field_type": field_type, "required": bool(required), "active": bool(active)},
            )
        return saved_id

    def validate_employee_custom_values(
        self,
        values: dict[int | str, Any],
    ) -> dict[int, str]:
        normalized_input = {int(key): _clean_text(value) for key, value in values.items()}
        fields = {int(field["id"]): field for field in self.list_employee_custom_fields(active_only=True)}
        unknown = set(normalized_input) - set(fields)
        if unknown:
            raise ValueError("يتضمن الطلب حقولًا مخصصة غير معروفة أو غير نشطة.")
        normalized: dict[int, str] = {}
        for field_id, field in fields.items():
            value = normalized_input.get(field_id, "")
            if field["is_required"] and not value:
                raise ValueError(f"الحقل المخصص «{field['label']}» مطلوب.")
            if value and field["field_type"] == "number":
                try:
                    value = format(Decimal(value), "f")
                except Exception as exc:
                    raise ValueError(f"الحقل «{field['label']}» يجب أن يكون رقمًا.") from exc
            elif value and field["field_type"] == "date":
                self._parse_iso_date(value, str(field["label"]))
            elif value and field["field_type"] == "choice" and value not in field["options"]:
                raise ValueError(f"قيمة الحقل «{field['label']}» ليست ضمن الخيارات المعتمدة.")
            normalized[field_id] = value
        return normalized

    def save_employee_custom_values(
        self,
        employee_id: int,
        values: dict[int | str, Any],
        actor: dict[str, Any],
    ) -> None:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("لا تملك صلاحية تعديل حقول الموظف المخصصة.")
        normalized = self.validate_employee_custom_values(values)
        with self.connection() as conn:
            if not conn.execute("SELECT 1 FROM employees WHERE id=?", (employee_id,)).fetchone():
                raise DatabaseError("الموظف غير موجود.")
            for field_id, value in normalized.items():
                if value:
                    conn.execute(
                        """INSERT INTO employee_custom_values(
                               employee_id,field_id,value_text,updated_by,updated_at
                           ) VALUES(?,?,?,?,CURRENT_TIMESTAMP)
                           ON CONFLICT(employee_id,field_id) DO UPDATE SET
                               value_text=excluded.value_text,updated_by=excluded.updated_by,
                               updated_at=CURRENT_TIMESTAMP""",
                        (employee_id, field_id, value, actor.get("id")),
                    )
                else:
                    conn.execute(
                        "DELETE FROM employee_custom_values WHERE employee_id=? AND field_id=?",
                        (employee_id, field_id),
                    )
            self._audit_conn(
                conn, actor, "UPDATE_CUSTOM_VALUES", "employee", employee_id,
                {"field_ids": sorted(normalized)},
            )

    def list_employee_choices(self, active_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE status='نشط'" if active_only else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT id, emp_code, name, department, nationality, id_type FROM employees {where} ORDER BY name"
            ).fetchall()
            return [dict(r) for r in rows]

    # ---------- Employee import ----------
    @staticmethod
    def _import_record_payload(record: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "emp_code", "name", "phone", "id_number", "id_type", "id_issue_date",
            "id_expiry_date", "nationality", "birth_date", "work_permit_start_date",
            "work_permit_expiry_date", "medical_insurance_start_date",
            "medical_insurance_expiry_date",
            "join_date", "department", "job_title", "salary", "work_location",
            "email", "bank_name", "bank_account_number", "iban",
            "status", "performance_rating", "notes",
        )
        return {field: record.get(field, "") for field in fields}

    @staticmethod
    def _import_row_number(record: dict[str, Any], fallback: int) -> int:
        try:
            return int(record.get("_source_row", fallback))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _protected_import_fields(
        mapped_fields: set[str],
        payload: dict[str, Any],
        existing: dict[str, Any],
    ) -> tuple[set[str], list[str]]:
        """Protect an existing salary from ambiguous zero-value bulk updates.

        Employee spreadsheets often contain a salary column that is populated with
        zero only because compensation was not exported.  Treating that placeholder
        as an intentional update silently collapses the company payroll total.  A
        real zero salary can still be set from the employee editor, where the intent
        is explicit.
        """
        protected_fields: list[str] = []
        safe_fields = set(mapped_fields)
        if "salary" in safe_fields:
            proposed_salary = money(payload.get("salary") or 0)
            current_salary = money(existing.get("salary") or 0)
            if proposed_salary == 0 and current_salary > 0:
                safe_fields.remove("salary")
                protected_fields.append("salary")
        return safe_fields, protected_fields

    def analyze_employee_import(
        self, records: list[dict[str, Any]], duplicate_mode: str = "skip"
    ) -> dict[str, Any]:
        """فحص صفوف الاستيراد دون تعديل قاعدة البيانات."""
        if duplicate_mode not in {"skip", "update"}:
            raise ValueError("طريقة معالجة التكرارات غير صحيحة.")
        with self.connection() as conn:
            existing_rows = conn.execute(
                "SELECT id, emp_code, name, id_hash, salary FROM employees"
            ).fetchall()
        existing_by_hash = {
            row["id_hash"]: dict(row) for row in existing_rows if row["id_hash"]
        }
        existing_by_code = {str(row["emp_code"]).upper(): dict(row) for row in existing_rows}
        seen_hashes: set[str] = set()
        preview: list[dict[str, Any]] = []
        counts = {"new": 0, "duplicate": 0, "invalid": 0}

        for index, source in enumerate(records, start=2):
            row_number = self._import_row_number(source, index)
            payload = self._import_record_payload(source)
            name = str(payload.get("name", "")).strip()
            source_code = str(payload.get("emp_code", "")).strip().upper()
            identity_digits = normalize_id_number(str(payload.get("id_number", "") or ""))
            emp_code = identity_digits[-4:] if len(identity_digits) >= 4 else source_code
            item = {
                "row": row_number,
                "name": name,
                "emp_code": emp_code,
                "id_type": str(payload.get("id_type", "")).strip() or (
                    identity_type_for_nationality(payload.get("nationality", ""))
                    if str(payload.get("nationality", "")).strip() else ""
                ),
                "id_number": str(payload.get("id_number", "")),
                "department": str(payload.get("department", "")),
                "job_title": str(payload.get("job_title", "")),
                "salary": str(payload.get("salary", "")),
            }
            try:
                normalized = self._validate_employee(payload, allow_incomplete_import=True)
                fingerprint = (
                    id_fingerprint(normalized["id_number"])
                    if normalized["id_number"] else None
                )
                if fingerprint and fingerprint in seen_hashes:
                    item.update(state="خطأ", message="رقم الهوية/الإقامة/الجواز مكرر داخل ملف الاستيراد.")
                    counts["invalid"] += 1
                elif fingerprint and fingerprint in existing_by_hash:
                    existing = existing_by_hash[fingerprint]
                    action = "سيُحدّث" if duplicate_mode == "update" else "سيُتخطى"
                    protected: list[str] = []
                    if duplicate_mode == "update":
                        mapped_fields = set(source.get("_mapped_fields") or payload.keys())
                        _safe_fields, protected = self._protected_import_fields(
                            mapped_fields, payload, existing
                        )
                    protection_note = (
                        " سيُحافظ على الراتب الحالي لأن قيمة الملف صفرية."
                        if protected
                        else ""
                    )
                    item.update(
                        state=action,
                        message=(
                            f"الموظف موجود مسبقًا برقم {existing['emp_code']}."
                            f"{protection_note}"
                        ),
                        employee_id=existing["id"],
                    )
                    counts["duplicate"] += 1
                elif emp_code and emp_code in existing_by_code:
                    existing = existing_by_code[emp_code]
                    item.update(
                        state="خطأ",
                        message=f"رقم الموظف مستخدم للموظف {existing['name']}.",
                    )
                    counts["invalid"] += 1
                else:
                    warnings = [
                        str(value)
                        for value in source.get("_import_warnings", ())
                        if str(value).strip()
                    ]
                    message = "جاهز للاستيراد."
                    if warnings:
                        message = (
                            "جاهز للاستيراد؛ بيانات غير متوفرة ستُحفظ كقيم فارغة/افتراضية: "
                            + "، ".join(warnings)
                        )
                    item.update(state="جديد", message=message)
                    counts["new"] += 1
                if fingerprint:
                    seen_hashes.add(fingerprint)
            except Exception as exc:
                item.update(state="خطأ", message=str(exc))
                counts["invalid"] += 1
            preview.append(item)
        return {"rows": preview, "counts": counts, "total": len(preview)}

    def import_employees(
        self, records: list[dict[str, Any]], actor: dict[str, Any], duplicate_mode: str = "skip"
    ) -> dict[str, Any]:
        """استيراد دفعة موظفين بمعاملة واحدة مع تقرير لكل صف وسجل تدقيق."""
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("لا تملك صلاحية استيراد الموظفين.")
        if duplicate_mode not in {"skip", "update"}:
            raise ValueError("طريقة معالجة التكرارات غير صحيحة.")

        summary: dict[str, Any] = {
            "total": len(records), "created": 0, "updated": 0,
            "skipped": 0, "failed": 0, "rows": [],
        }
        with self.connection() as conn:
            existing_rows = conn.execute("SELECT * FROM employees").fetchall()
            existing_by_hash = {
                row["id_hash"]: dict(row) for row in existing_rows if row["id_hash"]
            }
            existing_by_code = {str(row["emp_code"]).upper(): dict(row) for row in existing_rows}
            used_codes = set(existing_by_code)
            code_numbers = []
            for code in used_codes:
                if code.startswith("DRAFT") and code[5:].isdigit():
                    code_numbers.append(int(code[5:]))
            next_number = max(code_numbers, default=0) + 1
            seen_hashes: set[str] = set()

            def allocate_code() -> str:
                nonlocal next_number
                while True:
                    code = f"DRAFT{next_number:04d}"
                    next_number += 1
                    if code not in used_codes:
                        used_codes.add(code)
                        return code

            for index, source in enumerate(records, start=2):
                row_number = self._import_row_number(source, index)
                payload = self._import_record_payload(source)
                name = str(payload.get("name", "")).strip()
                result_row = {"row": row_number, "name": name, "emp_code": ""}
                try:
                    normalized = self._validate_employee(payload, allow_incomplete_import=True)
                    fingerprint = (
                        id_fingerprint(normalized["id_number"])
                        if normalized["id_number"] else None
                    )
                    if fingerprint and fingerprint in seen_hashes:
                        summary["skipped"] += 1
                        result_row.update(status="متخطى", message="رقم الهوية/الإقامة/الجواز مكرر داخل الملف.")
                        summary["rows"].append(result_row)
                        continue
                    if fingerprint:
                        seen_hashes.add(fingerprint)
                    source_code = str(payload.get("emp_code", "")).strip().upper()
                    existing = existing_by_hash.get(fingerprint) if fingerprint else None
                    if not existing and source_code:
                        existing = existing_by_code.get(source_code)

                    if existing and duplicate_mode == "skip":
                        summary["skipped"] += 1
                        result_row.update(
                            emp_code=existing["emp_code"], status="متخطى",
                            message="الموظف موجود مسبقًا.",
                        )
                        summary["rows"].append(result_row)
                        continue

                    if existing and duplicate_mode == "update":
                        mapped_fields = set(source.get("_mapped_fields") or payload.keys())
                        mapped_fields, protected_fields = self._protected_import_fields(
                            mapped_fields, payload, existing
                        )
                        merged = dict(existing)
                        for field in mapped_fields:
                            if field in payload and field not in {"emp_code", "id"}:
                                merged[field] = payload[field]
                        if "nationality" in mapped_fields and "id_type" not in mapped_fields:
                            merged["id_type"] = (
                                identity_type_for_nationality(merged.get("nationality"))
                                if str(merged.get("nationality") or "").strip() else ""
                            )
                        merged = self._validate_employee(merged, allow_incomplete_import=True)
                        employee_code = self._employee_code_for_identity_conn(
                            conn,
                            merged["id_number"],
                            employee_id=int(existing["id"]),
                            current_code=str(existing.get("emp_code") or ""),
                        )
                        conn.execute(
                            """
                            UPDATE employees SET
                                emp_code=?, name=?, phone=?, id_number=?, id_hash=?, id_type=?, id_issue_date=?, id_expiry_date=?,
                                work_permit_start_date=?, work_permit_expiry_date=?,
                                medical_insurance_start_date=?, medical_insurance_expiry_date=?,
                                nationality=?, birth_date=?, join_date=?,
                                department=?, job_title=?, salary=?, work_location=?, email=?,
                                bank_name=?, bank_account_number=?, iban=?, status=?, performance_rating=?, notes=?,
                                updated_at=CURRENT_TIMESTAMP
                            WHERE id=?
                            """,
                            (
                                employee_code, merged["name"].strip(), merged["phone"], merged["id_number"],
                                id_fingerprint(merged["id_number"]) if merged["id_number"] else None,
                                merged["id_type"],
                                _clean_text(merged.get("id_issue_date")) or None,
                                _clean_text(merged.get("id_expiry_date")) or None,
                                _clean_text(merged.get("work_permit_start_date")) or None,
                                _clean_text(merged.get("work_permit_expiry_date")) or None,
                                _clean_text(merged.get("medical_insurance_start_date")) or None,
                                _clean_text(merged.get("medical_insurance_expiry_date")) or None,
                                merged["nationality"],
                                _clean_text(merged.get("birth_date")) or None, merged["join_date"],
                                merged["department"].strip(), merged["job_title"].strip(), merged["salary"],
                                _clean_text(merged.get("work_location")), _clean_text(merged.get("email")),
                                merged["bank_name"], merged["bank_account_number"], merged["iban"],
                                merged["status"], merged["performance_rating"], _clean_text(merged.get("notes")), existing["id"],
                            ),
                        )
                        changed = {
                            key: merged.get(key)
                            for key in mapped_fields
                            if key in merged and key not in {"id_number", "id_hash", "emp_code"}
                            and existing.get(key) != merged.get(key)
                        }
                        self._sync_document_dates_conn(conn, existing["id"], merged)
                        self._audit_conn(conn, actor, "UPDATE", "employee", existing["id"], {
                            "emp_code": employee_code, "source": "excel_import",
                            "source_row": row_number,
                            "changed": self._redact_employee_audit_values(changed),
                            "protected_fields": protected_fields,
                        })
                        updated_row = dict(existing)
                        updated_row.update(merged)
                        updated_row["emp_code"] = employee_code
                        if fingerprint:
                            existing_by_hash[fingerprint] = updated_row
                        existing_by_code.pop(str(existing.get("emp_code") or "").upper(), None)
                        existing_by_code[employee_code] = updated_row
                        summary["updated"] += 1
                        warnings = [
                            str(value)
                            for value in source.get("_import_warnings", ())
                            if str(value).strip()
                        ]
                        result_row.update(
                            emp_code=employee_code, status="محدّث",
                            message="تم تحديث الموظف الموجود." + (
                                " ولم تُستبدل الحقول غير المتوفرة في الملف." if warnings else ""
                            ) + (
                                " وتم الحفاظ على الراتب الحالي لأن قيمة الملف صفرية."
                                if protected_fields
                                else ""
                            ),
                        )
                        summary["rows"].append(result_row)
                        continue

                    if normalized["id_number"]:
                        emp_code = normalize_id_number(normalized["id_number"])[-4:]
                        conflicting = existing_by_code.get(emp_code)
                        if conflicting:
                            raise DatabaseError(
                                f"آخر أربعة أرقام من الهوية ({emp_code}) مستخدمة للموظف {conflicting['name']}."
                            )
                        used_codes.add(emp_code)
                    else:
                        emp_code = allocate_code()

                    cur = conn.execute(
                        """
                        INSERT INTO employees(
                            emp_code, name, phone, id_number, id_hash, id_type, id_issue_date, id_expiry_date,
                            work_permit_start_date, work_permit_expiry_date,
                            medical_insurance_start_date, medical_insurance_expiry_date,
                            nationality, birth_date, join_date,
                            department, job_title, salary, work_location, email,
                            bank_name, bank_account_number, iban, status, performance_rating, notes
                        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            emp_code, normalized["name"].strip(), normalized["phone"],
                            normalized["id_number"], fingerprint, normalized["id_type"],
                            _clean_text(normalized.get("id_issue_date")) or None,
                            _clean_text(normalized.get("id_expiry_date")) or None,
                            _clean_text(normalized.get("work_permit_start_date")) or None,
                            _clean_text(normalized.get("work_permit_expiry_date")) or None,
                            _clean_text(normalized.get("medical_insurance_start_date")) or None,
                            _clean_text(normalized.get("medical_insurance_expiry_date")) or None,
                            normalized["nationality"],
                            _clean_text(normalized.get("birth_date")) or None,
                            normalized["join_date"], normalized["department"].strip(),
                            normalized["job_title"].strip(), normalized["salary"],
                            _clean_text(normalized.get("work_location")),
                            _clean_text(normalized.get("email")),
                            normalized["bank_name"], normalized["bank_account_number"], normalized["iban"],
                            normalized["status"],
                            normalized["performance_rating"], _clean_text(normalized.get("notes")),
                        ),
                    )
                    employee_id = int(cur.lastrowid)
                    created = dict(normalized)
                    created.update({"id": employee_id, "emp_code": emp_code, "id_hash": fingerprint})
                    if fingerprint:
                        existing_by_hash[fingerprint] = created
                    existing_by_code[emp_code] = created
                    self._audit_conn(conn, actor, "CREATE", "employee", employee_id, {
                        "emp_code": emp_code, "source": "excel_import", "source_row": row_number,
                    })
                    summary["created"] += 1
                    warnings = [
                        str(value)
                        for value in source.get("_import_warnings", ())
                        if str(value).strip()
                    ]
                    result_row.update(
                        emp_code=emp_code,
                        status="مضاف",
                        message="تمت إضافة الموظف." + (
                            " راجع الحقول التي لم تكن متوفرة في الملف." if warnings else ""
                        ),
                    )
                except Exception as exc:
                    summary["failed"] += 1
                    result_row.update(status="فشل", message=str(exc))
                summary["rows"].append(result_row)

            self._audit_conn(conn, actor, "IMPORT", "employees", None, {
                "total": summary["total"], "created": summary["created"],
                "updated": summary["updated"], "skipped": summary["skipped"],
                "failed": summary["failed"], "duplicate_mode": duplicate_mode,
            })
        return summary

    # ---------- Attendance ----------
    @staticmethod
    def _clock_minutes(value: str, field_name: str) -> tuple[str, int | None]:
        text = str(value or "").strip()
        if not text:
            return "", None
        for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p"):
            try:
                parsed = datetime.strptime(text.upper().replace("ص", "AM").replace("م", "PM"), fmt)
                return parsed.strftime("%H:%M"), parsed.hour * 60 + parsed.minute
            except ValueError:
                continue
        raise ValueError(f"{field_name} يجب أن يكون بصيغة HH:MM مثل 08:30.")

    @staticmethod
    def _attendance_metrics(
        conn: sqlite3.Connection,
        employee_id: int,
        attendance_date: str,
        check_in_minutes: int | None,
        check_out_minutes: int | None,
    ) -> tuple[int, int]:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('employee_shifts','shifts')"
            ).fetchall()
        }
        shift = None
        if {"employee_shifts", "shifts"}.issubset(tables):
            shift = conn.execute(
                """
                SELECT s.start_time,s.end_time,s.grace_minutes
                FROM employee_shifts es JOIN shifts s ON s.id=es.shift_id
                WHERE es.employee_id=? AND s.is_active=1
                  AND es.effective_from<=?
                  AND (es.effective_to IS NULL OR es.effective_to='' OR es.effective_to>=?)
                ORDER BY es.effective_from DESC,es.id DESC LIMIT 1
                """,
                (employee_id, attendance_date, attendance_date),
            ).fetchone()
            if shift is None:
                shift = conn.execute(
                    "SELECT start_time,end_time,grace_minutes FROM shifts "
                    "WHERE is_active=1 ORDER BY id LIMIT 1"
                ).fetchone()

        late_minutes = 0
        overnight = False
        if shift is not None:
            _, shift_start = Database._clock_minutes(shift["start_time"], "بداية المناوبة")
            _, shift_end = Database._clock_minutes(shift["end_time"], "نهاية المناوبة")
            overnight = (
                shift_start is not None and shift_end is not None and shift_end < shift_start
            )
            if check_in_minutes is not None and shift_start is not None:
                late_minutes = max(
                    0, check_in_minutes - shift_start - int(shift["grace_minutes"] or 0)
                )

        worked_minutes = 0
        if check_in_minutes is not None and check_out_minutes is not None:
            adjusted_checkout = check_out_minutes
            if overnight and adjusted_checkout < check_in_minutes:
                adjusted_checkout += 24 * 60
            if adjusted_checkout <= check_in_minutes:
                raise ValueError("وقت الانصراف يجب أن يكون بعد وقت الحضور.")
            worked_minutes = adjusted_checkout - check_in_minutes
        return late_minutes, worked_minutes

    def upsert_attendance(
        self, employee_id: int, attendance_date: str, status: str,
        note: str, actor: dict[str, Any], check_in_time: str = "", check_out_time: str = "",
        *, preserve_missing_times: bool = False, preserve_blank_note: bool = False,
        entry_mode: str = "manual",
    ) -> int:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("لا تملك صلاحية تسجيل الحضور.")
        try:
            datetime.strptime(attendance_date, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("صيغة التاريخ يجب أن تكون YYYY-MM-DD.") from exc
        if status not in ATTENDANCE_STATUSES:
            raise ValueError("حالة الحضور غير صحيحة.")
        normalized_check_in, check_in_minutes = self._clock_minutes(
            check_in_time, "وقت الحضور"
        )
        normalized_check_out, check_out_minutes = self._clock_minutes(
            check_out_time, "وقت الانصراف"
        )
        with self.connection() as conn:
            employee = conn.execute("SELECT emp_code FROM employees WHERE id=?", (employee_id,)).fetchone()
            if not employee:
                raise DatabaseError("الموظف غير موجود.")
            existing = conn.execute(
                "SELECT * FROM attendance WHERE employee_id=? AND attendance_date=?",
                (employee_id, attendance_date),
            ).fetchone()
            if preserve_missing_times and existing:
                if not normalized_check_in:
                    normalized_check_in, check_in_minutes = self._clock_minutes(
                        existing["check_in_time"] or "", "وقت الحضور"
                    )
                if not normalized_check_out:
                    normalized_check_out, check_out_minutes = self._clock_minutes(
                        existing["check_out_time"] or "", "وقت الانصراف"
                    )
            normalized_note = note.strip()
            if preserve_blank_note and not normalized_note and existing:
                normalized_note = str(existing["note"] or "")
            late_minutes, worked_minutes = self._attendance_metrics(
                conn, employee_id, attendance_date, check_in_minutes, check_out_minutes
            )
            conn.execute(
                """
                INSERT INTO attendance(
                    employee_id, attendance_date, status, check_in_time, check_out_time,
                    late_minutes, worked_minutes, note
                )
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(employee_id, attendance_date)
                DO UPDATE SET status=excluded.status,
                              check_in_time=excluded.check_in_time,
                              check_out_time=excluded.check_out_time,
                              late_minutes=excluded.late_minutes,
                              worked_minutes=excluded.worked_minutes,
                              note=excluded.note,
                              updated_at=CURRENT_TIMESTAMP
                """,
                (
                    employee_id,
                    attendance_date,
                    status,
                    normalized_check_in or None,
                    normalized_check_out or None,
                    late_minutes,
                    worked_minutes,
                    normalized_note,
                ),
            )
            row = conn.execute(
                "SELECT id FROM attendance WHERE employee_id=? AND attendance_date=?",
                (employee_id, attendance_date),
            ).fetchone()
            action = "UPDATE" if existing else "CREATE"
            self._audit_conn(conn, actor, action, "attendance", row["id"], {
                "emp_code": employee["emp_code"], "date": attendance_date, "status": status,
                "check_in": normalized_check_in, "check_out": normalized_check_out,
                "late_minutes": late_minutes,
                "entry_mode": entry_mode,
            })
            return int(row["id"])

    def attendance_for_employee(self, employee_id: int, limit: int = 200) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT a.*, e.emp_code, e.name
                FROM attendance a JOIN employees e ON e.id=a.employee_id
                WHERE a.employee_id=? ORDER BY a.attendance_date DESC LIMIT ?
                """,
                (employee_id, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    def employee_report_data(self, employee_id: int, limit: int = 200) -> dict[str, Any]:
        """Return one consistent snapshot for the individual employee report."""
        with self.connection() as conn:
            employee = conn.execute("SELECT * FROM employees WHERE id=?", (employee_id,)).fetchone()
            if not employee:
                raise DatabaseError("الموظف غير موجود.")
            attendance = conn.execute(
                """
                SELECT * FROM attendance
                WHERE employee_id=? ORDER BY attendance_date DESC, id DESC LIMIT ?
                """,
                (employee_id, limit),
            ).fetchall()
            leaves = conn.execute(
                """
                SELECT * FROM leave_requests
                WHERE employee_id=? ORDER BY start_date DESC, id DESC LIMIT ?
                """,
                (employee_id, limit),
            ).fetchall()
            documents: list[sqlite3.Row] = []
            if conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='employee_documents'"
            ).fetchone():
                documents = conn.execute(
                    """SELECT id,document_type,original_name,start_date,expiry_date,notes,created_at
                       FROM employee_documents WHERE employee_id=?
                       ORDER BY COALESCE(expiry_date,start_date,created_at) DESC,id DESC LIMIT ?""",
                    (employee_id, limit),
                ).fetchall()
            custom_fields = conn.execute(
                """SELECT f.id,f.label,f.field_type,f.sort_order,v.value_text
                   FROM employee_custom_fields f
                   LEFT JOIN employee_custom_values v
                     ON v.field_id=f.id AND v.employee_id=?
                   WHERE f.is_active=1
                   ORDER BY f.sort_order,f.label""",
                (employee_id,),
            ).fetchall()
            return {
                "employee": dict(employee),
                "attendance": [dict(row) for row in attendance],
                "leaves": [dict(row) for row in leaves],
                "documents": [dict(row) for row in documents],
                "custom_fields": [dict(row) for row in custom_fields],
                "company": self.company_profile(),
            }

    # ---------- Leave management ----------
    @staticmethod
    def _parse_iso_date(value: str, field_name: str = "التاريخ") -> date:
        try:
            return datetime.strptime(str(value).strip(), "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError(f"{field_name} يجب أن يكون بصيغة YYYY-MM-DD.") from exc

    def create_leave(
        self,
        employee_id: int,
        leave_type: str,
        start_date: str,
        end_date: str,
        reason: str,
        notes: str,
        actor: dict[str, Any],
    ) -> int:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("لا تملك صلاحية إضافة الإجازات.")
        if leave_type not in LEAVE_TYPES:
            raise ValueError("نوع الإجازة غير صحيح.")
        start = self._parse_iso_date(start_date, "تاريخ بداية الإجازة")
        end = self._parse_iso_date(end_date, "تاريخ نهاية الإجازة")
        if end < start:
            raise ValueError("تاريخ نهاية الإجازة يجب ألا يسبق تاريخ البداية.")
        with self.connection() as conn:
            # احتساب أيام العمل الفعلية مع استثناء الجمعة والسبت والعطلات المسجلة.
            holiday_dates: set[str] = set()
            has_holidays = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_holidays'"
            ).fetchone()
            if has_holidays:
                holiday_dates = {
                    row[0] for row in conn.execute(
                        "SELECT holiday_date FROM company_holidays WHERE holiday_date BETWEEN ? AND ?",
                        (start.isoformat(), end.isoformat()),
                    ).fetchall()
                }
            cursor_day = start
            total_days = 0
            while cursor_day <= end:
                # Python: الاثنين=0 ... الجمعة=4 والسبت=5 والأحد=6.
                if cursor_day.weekday() not in {4, 5} and cursor_day.isoformat() not in holiday_dates:
                    total_days += 1
                cursor_day += timedelta(days=1)
            if total_days <= 0:
                raise ValueError("الفترة المحددة لا تحتوي على أيام عمل.")
            employee = conn.execute(
                "SELECT emp_code, name FROM employees WHERE id=?", (employee_id,)
            ).fetchone()
            if not employee:
                raise DatabaseError("الموظف غير موجود.")
            overlap = conn.execute(
                """
                SELECT id FROM leave_requests
                WHERE employee_id=? AND status IN ('معلّقة','معتمدة')
                  AND NOT(end_date < ? OR start_date > ?)
                LIMIT 1
                """,
                (employee_id, start.isoformat(), end.isoformat()),
            ).fetchone()
            if overlap:
                raise DatabaseError("توجد إجازة معلّقة أو معتمدة متداخلة مع هذه الفترة.")
            cur = conn.execute(
                """
                INSERT INTO leave_requests(
                    employee_id, leave_type, start_date, end_date, total_days,
                    status, reason, notes, created_by
                ) VALUES(?,?,?,?,?,'معلّقة',?,?,?)
                """,
                (
                    employee_id, leave_type, start.isoformat(), end.isoformat(), total_days,
                    reason.strip(), notes.strip(), actor.get("id"),
                ),
            )
            leave_id = int(cur.lastrowid)
            self._audit_conn(conn, actor, "CREATE", "leave", leave_id, {
                "emp_code": employee["emp_code"], "type": leave_type,
                "start": start.isoformat(), "end": end.isoformat(), "days": total_days,
            })
            return leave_id

    def list_leaves(
        self,
        search: str = "",
        status: str = "الكل",
        date_from: str = "",
        date_to: str = "",
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if search.strip():
            like = f"%{search.strip()}%"
            where.append("(e.emp_code LIKE ? OR e.name LIKE ? OR l.leave_type LIKE ? OR l.reason LIKE ?)")
            params.extend([like, like, like, like])
        if status and status != "الكل":
            if status not in LEAVE_STATUSES:
                raise ValueError("حالة الإجازة غير صحيحة.")
            where.append("l.status=?")
            params.append(status)
        if date_from.strip():
            start = self._parse_iso_date(date_from, "من تاريخ").isoformat()
            where.append("l.end_date>=?")
            params.append(start)
        if date_to.strip():
            end = self._parse_iso_date(date_to, "إلى تاريخ").isoformat()
            where.append("l.start_date<=?")
            params.append(end)
        clause = "WHERE " + " AND ".join(where) if where else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"""
                SELECT l.*, e.emp_code, e.name, e.department,
                       creator.username created_by_username,
                       approver.username approved_by_username
                FROM leave_requests l
                JOIN employees e ON e.id=l.employee_id
                LEFT JOIN users creator ON creator.id=l.created_by
                LEFT JOIN users approver ON approver.id=l.approved_by
                {clause}
                ORDER BY l.start_date DESC, l.id DESC LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
            return [dict(r) for r in rows]

    def get_leave(self, leave_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT l.*, e.emp_code, e.name, e.department
                FROM leave_requests l JOIN employees e ON e.id=l.employee_id
                WHERE l.id=?
                """, (leave_id,),
            ).fetchone()
            return dict(row) if row else None

    def update_leave_status(
        self, leave_id: int, status: str, actor: dict[str, Any], note: str = ""
    ) -> None:
        role = actor.get("role")
        if role not in {"admin", "hr"}:
            raise PermissionError("لا تملك صلاحية تحديث طلبات الإجازة.")
        if status not in LEAVE_STATUSES:
            raise ValueError("حالة الإجازة غير صحيحة.")
        if status in {"معتمدة", "مرفوضة"} and role != "admin":
            raise PermissionError("اعتماد الإجازة أو رفضها متاح للمدير فقط.")
        with self.connection() as conn:
            if status in {"معتمدة", "مرفوضة"}:
                # Manager decisions are serialized so two clicks cannot record
                # conflicting terminal states or duplicate audit entries.
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT l.*, e.emp_code FROM leave_requests l
                JOIN employees e ON e.id=l.employee_id WHERE l.id=?
                """, (leave_id,),
            ).fetchone()
            if not row:
                raise DatabaseError("طلب الإجازة غير موجود.")
            if (
                status in {"معتمدة", "مرفوضة"}
                and row["status"] != "معلّقة"
            ):
                raise DatabaseError("تم اتخاذ قرار سابق على طلب الإجازة.")
            if status == "معتمدة":
                overlap = conn.execute(
                    """
                    SELECT id FROM leave_requests
                    WHERE employee_id=? AND id<>? AND status='معتمدة'
                      AND NOT(end_date < ? OR start_date > ?)
                    LIMIT 1
                    """,
                    (row["employee_id"], leave_id, row["start_date"], row["end_date"]),
                ).fetchone()
                if overlap:
                    raise DatabaseError("لا يمكن اعتماد الإجازة لوجود إجازة معتمدة متداخلة.")
                if row["leave_type"] == "سنوية":
                    balance_year = int(str(row["start_date"])[:4])
                    balance = conn.execute(
                        """SELECT entitlement, carried_forward, adjustment
                           FROM leave_balances
                           WHERE employee_id=? AND balance_year=? AND leave_type='سنوية'""",
                        (row["employee_id"], balance_year),
                    ).fetchone() if conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='leave_balances'"
                    ).fetchone() else None
                    entitlement = float(balance["entitlement"]) if balance else 30.0
                    carried = float(balance["carried_forward"]) if balance else 0.0
                    adjustment = float(balance["adjustment"]) if balance else 0.0
                    used = float(conn.execute(
                        """SELECT COALESCE(SUM(total_days),0) FROM leave_requests
                           WHERE employee_id=? AND id<>? AND leave_type='سنوية' AND status='معتمدة'
                             AND substr(start_date,1,4)=?""",
                        (row["employee_id"], leave_id, str(balance_year)),
                    ).fetchone()[0])
                    available = entitlement + carried + adjustment - used
                    if float(row["total_days"]) > available:
                        raise DatabaseError(
                            f"رصيد الإجازة السنوية غير كافٍ. المتاح {available:g} يومًا، والمطلوب {row['total_days']} يومًا."
                        )
            approved_by = actor.get("id") if status in {"معتمدة", "مرفوضة"} else None
            merged_notes = (row["notes"] or "").strip()
            if note.strip():
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                merged_notes = (merged_notes + f"\n[{stamp}] {note.strip()}").strip()
            conn.execute(
                """
                UPDATE leave_requests
                SET status=?, notes=?, approved_by=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """, (status, merged_notes, approved_by, leave_id),
            )
            self._audit_conn(conn, actor, "STATUS", "leave", leave_id, {
                "emp_code": row["emp_code"], "status": status,
            })

    def delete_leave(self, leave_id: int, actor: dict[str, Any]) -> None:
        if actor.get("role") != "admin":
            raise PermissionError("حذف الإجازات متاح للمدير فقط.")
        with self.connection() as conn:
            row = conn.execute(
                """SELECT l.id, e.emp_code FROM leave_requests l
                   JOIN employees e ON e.id=l.employee_id WHERE l.id=?""",
                (leave_id,),
            ).fetchone()
            if not row:
                raise DatabaseError("طلب الإجازة غير موجود.")
            conn.execute("DELETE FROM leave_requests WHERE id=?", (leave_id,))
            self._audit_conn(conn, actor, "DELETE", "leave", leave_id, {"emp_code": row["emp_code"]})

    # ---------- Digital calendar & daily notes ----------
    def get_calendar_note(self, note_date: str) -> dict[str, Any] | None:
        normalized = self._parse_iso_date(note_date).isoformat()
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT n.*, creator.username created_by_username, updater.username updated_by_username
                FROM calendar_notes n
                LEFT JOIN users creator ON creator.id=n.created_by
                LEFT JOIN users updater ON updater.id=n.updated_by
                WHERE n.note_date=?
                """, (normalized,),
            ).fetchone()
            return dict(row) if row else None

    def upsert_calendar_note(self, note_date: str, note: str, actor: dict[str, Any]) -> None:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("لا تملك صلاحية تعديل ملاحظات التقويم.")
        normalized = self._parse_iso_date(note_date).isoformat()
        clean = note.strip()
        with self.connection() as conn:
            existing = conn.execute("SELECT 1 FROM calendar_notes WHERE note_date=?", (normalized,)).fetchone()
            if not clean:
                conn.execute("DELETE FROM calendar_notes WHERE note_date=?", (normalized,))
                action = "DELETE" if existing else "NOOP"
            else:
                conn.execute(
                    """
                    INSERT INTO calendar_notes(note_date, note, created_by, updated_by)
                    VALUES(?,?,?,?)
                    ON CONFLICT(note_date) DO UPDATE SET
                        note=excluded.note,
                        updated_by=excluded.updated_by,
                        updated_at=CURRENT_TIMESTAMP
                    """, (normalized, clean, actor.get("id"), actor.get("id")),
                )
                action = "UPDATE" if existing else "CREATE"
            self._audit_conn(conn, actor, action, "calendar_note", normalized, {})

    def calendar_month(self, year: int, month: int) -> dict[str, Any]:
        if not 1 <= int(month) <= 12:
            raise ValueError("الشهر غير صحيح.")
        start = date(int(year), int(month), 1)
        if month == 12:
            end = date(int(year) + 1, 1, 1) - timedelta(days=1)
        else:
            end = date(int(year), int(month) + 1, 1) - timedelta(days=1)
        start_s, end_s = start.isoformat(), end.isoformat()
        with self.connection() as conn:
            attendance_rows = conn.execute(
                """
                SELECT attendance_date day, status, COUNT(*) count
                FROM attendance
                WHERE attendance_date BETWEEN ? AND ?
                GROUP BY attendance_date, status
                """, (start_s, end_s),
            ).fetchall()
            leaves = conn.execute(
                """
                SELECT l.id, l.start_date, l.end_date, l.leave_type, l.status,
                       e.emp_code, e.name
                FROM leave_requests l JOIN employees e ON e.id=l.employee_id
                WHERE l.status IN ('معلّقة','معتمدة')
                  AND NOT(l.end_date < ? OR l.start_date > ?)
                ORDER BY l.start_date
                """, (start_s, end_s),
            ).fetchall()
            notes = conn.execute(
                "SELECT note_date, note FROM calendar_notes WHERE note_date BETWEEN ? AND ?",
                (start_s, end_s),
            ).fetchall()
            holidays = []
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_holidays'").fetchone():
                holidays = conn.execute(
                    "SELECT holiday_date, name, notes FROM company_holidays WHERE holiday_date BETWEEN ? AND ?",
                    (start_s, end_s),
                ).fetchall()
        attendance: dict[str, dict[str, int]] = {}
        for row in attendance_rows:
            attendance.setdefault(row["day"], {})[row["status"]] = int(row["count"])
        leave_by_day: dict[str, list[dict[str, Any]]] = {}
        for row in leaves:
            item = dict(row)
            cursor = max(self._parse_iso_date(item["start_date"]), start)
            final = min(self._parse_iso_date(item["end_date"]), end)
            while cursor <= final:
                leave_by_day.setdefault(cursor.isoformat(), []).append(item)
                cursor += timedelta(days=1)
        return {
            "year": int(year), "month": int(month),
            "attendance": attendance,
            "leaves": leave_by_day,
            "notes": {row["note_date"]: row["note"] for row in notes},
            "holidays": {row["holiday_date"]: {"name": row["name"], "notes": row["notes"] or ""} for row in holidays},
        }

    def calendar_day_details(self, day: str) -> dict[str, Any]:
        normalized = self._parse_iso_date(day).isoformat()
        with self.connection() as conn:
            attendance = [dict(r) for r in conn.execute(
                """
                SELECT a.status, a.note, e.emp_code, e.name
                FROM attendance a JOIN employees e ON e.id=a.employee_id
                WHERE a.attendance_date=? ORDER BY e.name
                """, (normalized,),
            ).fetchall()]
            leaves = [dict(r) for r in conn.execute(
                """
                SELECT l.id, l.leave_type, l.status, l.start_date, l.end_date,
                       e.emp_code, e.name
                FROM leave_requests l JOIN employees e ON e.id=l.employee_id
                WHERE l.status IN ('معلّقة','معتمدة')
                  AND ? BETWEEN l.start_date AND l.end_date
                ORDER BY e.name
                """, (normalized,),
            ).fetchall()]
            note = conn.execute("SELECT note FROM calendar_notes WHERE note_date=?", (normalized,)).fetchone()
            holiday = None
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_holidays'").fetchone():
                holiday = conn.execute(
                    "SELECT name, notes FROM company_holidays WHERE holiday_date=?", (normalized,)
                ).fetchone()
        return {
            "date": normalized, "attendance": attendance, "leaves": leaves,
            "note": note[0] if note else "",
            "holiday": dict(holiday) if holiday else None,
        }

    # ---------- Dashboard, reports & audit ----------
    @staticmethod
    def _resolved_salary_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
        """Return a resilient, explainable snapshot of the payable salary total.

        A payroll run can legitimately be incomplete (for example, it was generated
        before a bulk employee import).  Replacing the dashboard total with that
        run's SUM(net_salary) made one employee look like the whole company.  Start
        from every current employee, then overlay only approved payroll values.
        """
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        employee_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(employees)").fetchall()
        }
        has_total_salary = "total_salary" in employee_columns
        salary_columns = "id,salary"
        if has_total_salary:
            salary_columns += ",total_salary"
        employee_rows = conn.execute(
            f"""SELECT {salary_columns}
                FROM employees
                WHERE status<>'منتهي الخدمة'"""
        ).fetchall()
        salary_by_employee: dict[int, Any] = {}
        invalid_salary_rows = 0
        for row in employee_rows:
            baseline = None
            if has_total_salary:
                try:
                    total_salary = money(row["total_salary"])
                    if total_salary > 0:
                        baseline = total_salary
                except ValueError:
                    invalid_salary_rows += 1
            if baseline is None:
                try:
                    baseline = money(row["salary"])
                except ValueError:
                    baseline = money(0)
                    invalid_salary_rows += 1
            salary_by_employee[int(row["id"])] = baseline

        run_period = ""
        run_status = ""
        run_employee_count = 0
        overlay_count = 0
        if {"payroll_runs", "payroll_items"}.issubset(tables):
            # لا يجوز لمسير قديم معتمد أن يطغى على ملفات الموظفين الحالية
            # عند وجود فترة أحدث. نستخدم أحدث فترة فقط، ثم نطبّق منها القيم
            # المعتمدة: جميع البنود إن كان المسير معتمدًا، أو البنود التي
            # اعتمد المدير تعديلها فقط إن كان ما زال مسودة.
            run = conn.execute(
                """
                SELECT r.id,r.status,r.period,
                       (SELECT COUNT(*) FROM payroll_items i
                        WHERE i.payroll_run_id=r.id) employee_count
                FROM payroll_runs r
                ORDER BY r.period DESC,r.id DESC LIMIT 1
                """
            ).fetchone()
            if run:
                run_period = str(run["period"] or "")
                run_status = str(run["status"] or "")
                run_employee_count = int(run["employee_count"] or 0)
                if run["status"] == "معتمد":
                    item_condition = ""
                elif "payroll_adjustment_requests" in tables:
                    item_condition = """
                        AND EXISTS (
                            SELECT 1 FROM payroll_adjustment_requests a
                            WHERE a.payroll_item_id=i.id AND a.status='معتمد'
                        )
                    """
                else:
                    item_condition = None
                approved_items = (
                    conn.execute(
                        f"""SELECT i.employee_id,i.net_salary
                            FROM payroll_items i
                            WHERE i.payroll_run_id=? {item_condition}""",
                        (int(run["id"]),),
                    ).fetchall()
                    if item_condition is not None
                    else []
                )
                for item in approved_items:
                    employee_id = int(item["employee_id"])
                    if employee_id in salary_by_employee:
                        try:
                            salary_by_employee[employee_id] = money(item["net_salary"])
                            overlay_count += 1
                        except ValueError:
                            invalid_salary_rows += 1

        salary_total = sum(salary_by_employee.values(), money(0))
        if salary_by_employee or salary_total > 0:
            if run_status == "معتمد" and overlay_count:
                source = f"مسير {run_period} المعتمد مع استكمال غير المدرجين"
            elif overlay_count:
                source = f"اعتمادات المدير في مسير {run_period} مع ملفات الموظفين"
            else:
                source = "ملفات الموظفين"
            return {
                "total": money_float(salary_total),
                "source": source,
                "payable_employees": len(salary_by_employee),
                "payroll_period": run_period,
                "payroll_status": run_status,
                "payroll_run_employees": run_employee_count,
                "payroll_overlay_employees": overlay_count,
                "invalid_salary_rows": invalid_salary_rows,
            }

        if "payroll_summary_lines" in tables:
            row = conn.execute(
                """
                SELECT total_salary FROM payroll_summary_lines
                WHERE batch_id=(SELECT MAX(batch_id) FROM payroll_summary_lines)
                ORDER BY source_row DESC,id DESC LIMIT 1
                """
            ).fetchone()
            if row:
                try:
                    legacy_total = money(row[0])
                except ValueError:
                    legacy_total = money(0)
                    invalid_salary_rows += 1
                if legacy_total > 0:
                    return {
                        "total": money_float(legacy_total),
                        "source": "ملخص رواتب قديم",
                        "payable_employees": 0,
                        "payroll_period": "",
                        "payroll_status": "",
                        "payroll_run_employees": 0,
                        "payroll_overlay_employees": 0,
                        "invalid_salary_rows": invalid_salary_rows,
                    }

        return {
            "total": 0.0,
            "source": "لا توجد بيانات رواتب",
            "payable_employees": 0,
            "payroll_period": run_period,
            "payroll_status": run_status,
            "payroll_run_employees": run_employee_count,
            "payroll_overlay_employees": overlay_count,
            "invalid_salary_rows": invalid_salary_rows,
        }

    @staticmethod
    def _resolved_salary_total(conn: sqlite3.Connection) -> float:
        """Backward-compatible total-only helper used by older integrations."""
        return float(Database._resolved_salary_snapshot(conn)["total"])

    def workforce_analytics(
        self,
        *,
        start_date: str = "",
        end_date: str = "",
        department: str = "",
        employee_id: int | None = None,
    ) -> dict[str, Any]:
        """Return filtered workforce metrics plus local-only intelligent insights."""
        end = self._parse_iso_date(end_date, "إلى تاريخ") if end_date else date.today()
        start = (
            self._parse_iso_date(start_date, "من تاريخ")
            if start_date
            else end - timedelta(days=29)
        )
        if start > end:
            raise ValueError("تاريخ بداية التقرير يجب ألا يتجاوز تاريخ النهاية.")
        employee_conditions = ["1=1"]
        employee_params: list[Any] = []
        if department and department != "الكل":
            employee_conditions.append("e.department=?")
            employee_params.append(department)
        if employee_id:
            employee_conditions.append("e.id=?")
            employee_params.append(int(employee_id))
        employee_where = " AND ".join(employee_conditions)
        attendance_params: list[Any] = [start.isoformat(), end.isoformat(), *employee_params]
        with self.connection() as conn:
            employees_summary = conn.execute(
                f"""SELECT COUNT(*) AS employees,
                           SUM(CASE WHEN e.status='نشط' THEN 1 ELSE 0 END) AS active,
                           COALESCE(AVG(CASE WHEN e.status<>'منتهي الخدمة' THEN e.salary END),0) AS avg_salary
                    FROM employees e WHERE {employee_where}""",
                employee_params,
            ).fetchone()
            attendance = conn.execute(
                f"""SELECT COUNT(*) AS records,
                           SUM(CASE WHEN a.status IN ('حاضر','عمل عن بُعد','مهمة عمل') THEN 1 ELSE 0 END) AS attended,
                           SUM(CASE WHEN a.status='غائب' THEN 1 ELSE 0 END) AS absent,
                           SUM(CASE WHEN a.status='عمل عن بُعد' THEN 1 ELSE 0 END) AS remote,
                           SUM(CASE WHEN a.status='إجازة' THEN 1 ELSE 0 END) AS attendance_leave
                    FROM attendance a JOIN employees e ON e.id=a.employee_id
                    WHERE date(a.attendance_date) BETWEEN date(?) AND date(?)
                      AND {employee_where}""",
                attendance_params,
            ).fetchone()
            daily = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT a.attendance_date AS date, COUNT(*) AS records,
                               SUM(CASE WHEN a.status='غائب' THEN 1 ELSE 0 END) AS absent,
                               SUM(CASE WHEN a.status IN ('حاضر','عمل عن بُعد','مهمة عمل') THEN 1 ELSE 0 END) AS attended
                        FROM attendance a JOIN employees e ON e.id=a.employee_id
                        WHERE date(a.attendance_date) BETWEEN date(?) AND date(?)
                          AND {employee_where}
                        GROUP BY a.attendance_date ORDER BY a.attendance_date""",
                    attendance_params,
                ).fetchall()
            ]
            people = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT e.id,e.emp_code,e.name,e.department,
                               COUNT(a.id) AS records,
                               SUM(CASE WHEN a.status='غائب' THEN 1 ELSE 0 END) AS absent,
                               SUM(CASE WHEN a.status IN ('حاضر','عمل عن بُعد','مهمة عمل') THEN 1 ELSE 0 END) AS attended
                        FROM employees e
                        LEFT JOIN attendance a ON a.employee_id=e.id
                          AND date(a.attendance_date) BETWEEN date(?) AND date(?)
                        WHERE {employee_where}
                        GROUP BY e.id,e.emp_code,e.name,e.department
                        ORDER BY absent DESC, records DESC, e.name LIMIT 100""",
                    attendance_params,
                ).fetchall()
            ]
            leave_row = conn.execute(
                f"""SELECT COUNT(*) AS requests,
                           COALESCE(SUM(
                               julianday(MIN(l.end_date, ?)) -
                               julianday(MAX(l.start_date, ?)) + 1
                           ),0) AS days
                    FROM leave_requests l JOIN employees e ON e.id=l.employee_id
                    WHERE l.status='معتمدة'
                      AND date(l.end_date)>=date(?) AND date(l.start_date)<=date(?)
                      AND {employee_where}""",
                [end.isoformat(), start.isoformat(), start.isoformat(), end.isoformat(), *employee_params],
            ).fetchone()
            departments = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT COALESCE(NULLIF(TRIM(e.department),''),'غير محدد') AS label,
                               COUNT(*) AS employees,
                               COALESCE(AVG(e.salary),0) AS avg_salary
                        FROM employees e WHERE {employee_where}
                        GROUP BY label ORDER BY employees DESC,label""",
                    employee_params,
                ).fetchall()
            ]
        attendance_records = int(attendance["records"] or 0)
        attended = int(attendance["attended"] or 0)
        analytics: dict[str, Any] = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "department": department if department != "الكل" else "",
            "employee_id": int(employee_id) if employee_id else None,
            "employees": int(employees_summary["employees"] or 0),
            "active": int(employees_summary["active"] or 0),
            "average_salary": round(float(employees_summary["avg_salary"] or 0), 2),
            "attendance_records": attendance_records,
            "attended": attended,
            "absent": int(attendance["absent"] or 0),
            "remote": int(attendance["remote"] or 0),
            "attendance_leave": int(attendance["attendance_leave"] or 0),
            "attendance_rate": round(attended * 100 / attendance_records, 2) if attendance_records else 0.0,
            "leave_requests": int(leave_row["requests"] or 0),
            "leave_days": round(float(leave_row["days"] or 0), 1),
            "daily": daily,
            "people": people,
            "departments": departments,
        }
        analytics["anomalies"] = robust_anomalies(
            daily, value_key="absent", label_key="date"
        )
        analytics["forecast"] = neural_forecast(
            (row.get("absent", 0) for row in daily)
        )
        analytics["insights"] = workforce_insights(analytics)
        return analytics

    def dashboard_stats(self) -> dict[str, Any]:
        with self.connection() as conn:
            summary = conn.execute(
                """
                SELECT COUNT(*) total,
                       SUM(CASE WHEN status='نشط' THEN 1 ELSE 0 END) active
                FROM employees
                """
            ).fetchone()
            summary_data = dict(summary)
            salary_snapshot = self._resolved_salary_snapshot(conn)
            salary_total = float(salary_snapshot["total"])
            summary_data["salaries"] = salary_total
            payable_employees = int(salary_snapshot["payable_employees"])
            summary_data["payroll_employees"] = payable_employees
            summary_data["salary_source"] = salary_snapshot["source"]
            summary_data["payroll_period"] = salary_snapshot["payroll_period"]
            summary_data["payroll_status"] = salary_snapshot["payroll_status"]
            summary_data["payroll_run_employees"] = salary_snapshot[
                "payroll_run_employees"
            ]
            summary_data["payroll_overlay_employees"] = salary_snapshot[
                "payroll_overlay_employees"
            ]
            summary_data["invalid_salary_rows"] = salary_snapshot[
                "invalid_salary_rows"
            ]
            summary_data["avg_salary"] = (
                salary_total / payable_employees
                if payable_employees > 0
                else 0.0
            )
            departments = conn.execute(
                "SELECT department, COUNT(*) count FROM employees GROUP BY department ORDER BY count DESC LIMIT 10"
            ).fetchall()
            recent = conn.execute(
                "SELECT emp_code, name, job_title, created_at FROM employees ORDER BY id DESC LIMIT 5"
            ).fetchall()
            today_date = date.today()
            today = today_date.isoformat()
            identity_deadline = (today_date + timedelta(days=90)).isoformat()
            today_attendance = conn.execute(
                "SELECT status, COUNT(*) count FROM attendance WHERE attendance_date=? GROUP BY status",
                (today,),
            ).fetchall()
            leave_today = conn.execute(
                """
                SELECT COUNT(*) FROM leave_requests
                WHERE status='معتمدة' AND ? BETWEEN start_date AND end_date
                """, (today,),
            ).fetchone()[0]
            identity_expired = conn.execute(
                "SELECT COUNT(*) FROM employees WHERE TRIM(COALESCE(id_expiry_date,''))<>'' "
                "AND status<>'منتهي الخدمة' AND date(id_expiry_date)<date(?)", (today,),
            ).fetchone()[0]
            identity_expiring = conn.execute(
                "SELECT COUNT(*) FROM employees WHERE status<>'منتهي الخدمة' "
                "AND date(id_expiry_date) BETWEEN date(?) AND date(?)",
                (today, identity_deadline),
            ).fetchone()[0]
            residency_expired = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE status<>'منتهي الخدمة' AND id_type='إقامة'
                     AND TRIM(COALESCE(id_expiry_date,''))<>''
                     AND date(id_expiry_date)<date(?)""",
                (today,),
            ).fetchone()[0]
            residency_expiring = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE status<>'منتهي الخدمة' AND id_type='إقامة'
                     AND date(id_expiry_date) BETWEEN date(?) AND date(?)""",
                (today, identity_deadline),
            ).fetchone()[0]
            work_permit_expired = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE status<>'منتهي الخدمة'
                     AND TRIM(COALESCE(work_permit_expiry_date,''))<>''
                     AND date(work_permit_expiry_date)<date(?)""",
                (today,),
            ).fetchone()[0]
            work_permit_expiring = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE status<>'منتهي الخدمة'
                     AND date(work_permit_expiry_date) BETWEEN date(?) AND date(?)""",
                (today, identity_deadline),
            ).fetchone()[0]
            medical_insurance_expired = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE status<>'منتهي الخدمة'
                     AND TRIM(COALESCE(medical_insurance_expiry_date,''))<>''
                     AND date(medical_insurance_expiry_date)<date(?)""",
                (today,),
            ).fetchone()[0]
            medical_insurance_expiring = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE status<>'منتهي الخدمة'
                     AND date(medical_insurance_expiry_date) BETWEEN date(?) AND date(?)""",
                (today, identity_deadline),
            ).fetchone()[0]
            other_documents_expired = 0
            other_documents_expiring = 0
            other_documents_not_started = 0
            document_follow_up: list[dict[str, Any]] = []
            document_table_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='employee_documents'"
            ).fetchone()
            if document_table_exists:
                excluded_types = ("هوية وطنية", "إقامة", "جواز سفر", "كرت عمل", "تأمين طبي")
                placeholders = ",".join("?" for _ in excluded_types)
                other_documents_expired = conn.execute(
                    f"""SELECT COUNT(*) FROM employee_documents d
                         JOIN employees e ON e.id=d.employee_id
                         WHERE e.status<>'منتهي الخدمة'
                           AND d.document_type NOT IN ({placeholders})
                           AND TRIM(COALESCE(d.expiry_date,''))<>''
                           AND date(d.expiry_date)<date(?)""",
                    (*excluded_types, today),
                ).fetchone()[0]
                other_documents_expiring = conn.execute(
                    f"""SELECT COUNT(*) FROM employee_documents d
                         JOIN employees e ON e.id=d.employee_id
                         WHERE e.status<>'منتهي الخدمة'
                           AND d.document_type NOT IN ({placeholders})
                           AND date(d.expiry_date) BETWEEN date(?) AND date(?)""",
                    (*excluded_types, today, identity_deadline),
                ).fetchone()[0]
                other_documents_not_started = conn.execute(
                    f"""SELECT COUNT(*) FROM employee_documents d
                         JOIN employees e ON e.id=d.employee_id
                         WHERE e.status<>'منتهي الخدمة'
                           AND d.document_type NOT IN ({placeholders})
                           AND date(d.start_date)>date(?)""",
                    (*excluded_types, today),
                ).fetchone()[0]
                document_follow_up = [
                    dict(row)
                    for row in conn.execute(
                        f"""WITH document_dates AS (
                            SELECT id AS employee_id,emp_code,name,id_type AS document_type,
                                   id_issue_date AS start_date,id_expiry_date AS end_date
                            FROM employees WHERE status<>'منتهي الخدمة'
                            UNION ALL
                            SELECT id,emp_code,name,'كرت عمل',work_permit_start_date,work_permit_expiry_date
                            FROM employees WHERE status<>'منتهي الخدمة'
                            UNION ALL
                            SELECT id,emp_code,name,'تأمين طبي',medical_insurance_start_date,medical_insurance_expiry_date
                            FROM employees WHERE status<>'منتهي الخدمة'
                            UNION ALL
                            SELECT e.id,e.emp_code,e.name,d.document_type,d.start_date,d.expiry_date
                            FROM employee_documents d JOIN employees e ON e.id=d.employee_id
                            WHERE e.status<>'منتهي الخدمة'
                              AND d.document_type NOT IN ({placeholders})
                        )
                        SELECT employee_id,emp_code,name,document_type,start_date,end_date
                        FROM document_dates
                        WHERE TRIM(COALESCE(start_date,''))<>'' OR TRIM(COALESCE(end_date,''))<>''
                        ORDER BY
                            CASE
                                WHEN date(end_date)<date(?) THEN 0
                                WHEN date(end_date) BETWEEN date(?) AND date(?) THEN 1
                                ELSE 2
                            END,
                            COALESCE(end_date,start_date),emp_code
                        LIMIT 10""",
                        (*excluded_types, today, today, identity_deadline),
                    ).fetchall()
                ]
            incomplete_profiles = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE TRIM(COALESCE(phone,''))=''
                      OR TRIM(COALESCE(id_number,''))=''
                      OR TRIM(COALESCE(id_expiry_date,''))=''
                      OR TRIM(COALESCE(email,''))=''
                      OR TRIM(COALESCE(nationality,''))=''
                      OR TRIM(COALESCE(join_date,''))=''
                      OR TRIM(COALESCE(department,''))=''
                      OR TRIM(COALESCE(job_title,''))=''
                      OR salary IS NULL"""
            ).fetchone()[0]
            missing_compliance_dates = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE status<>'منتهي الخدمة' AND (
                       TRIM(COALESCE(id_issue_date,''))=''
                       OR TRIM(COALESCE(id_expiry_date,''))=''
                       OR TRIM(COALESCE(medical_insurance_start_date,''))=''
                       OR TRIM(COALESCE(medical_insurance_expiry_date,''))=''
                       OR (
                           id_type='إقامة' AND (
                               TRIM(COALESCE(work_permit_start_date,''))=''
                               OR TRIM(COALESCE(work_permit_expiry_date,''))=''
                           )
                       )
                   )"""
            ).fetchone()[0]
            missing_bank_details = conn.execute(
                """SELECT COUNT(*) FROM employees
                   WHERE status<>'منتهي الخدمة' AND (
                       TRIM(COALESCE(bank_name,''))=''
                       OR TRIM(COALESCE(iban,''))=''
                   )"""
            ).fetchone()[0]
            employee_code_conflicts = str(
                conn.execute(
                    "SELECT value FROM app_settings WHERE key='migration.employee_code_conflicts'"
                ).fetchone()[0]
                if conn.execute(
                    "SELECT 1 FROM app_settings WHERE key='migration.employee_code_conflicts'"
                ).fetchone()
                else ""
            )
            return {
                **summary_data,
                "departments": [dict(r) for r in departments],
                "recent": [dict(r) for r in recent],
                "today_attendance": [dict(r) for r in today_attendance],
                "leave_today": int(leave_today),
                "identity_expired": int(identity_expired),
                "identity_expiring": int(identity_expiring),
                "residency_expired": int(residency_expired),
                "residency_expiring": int(residency_expiring),
                "work_permit_expired": int(work_permit_expired),
                "work_permit_expiring": int(work_permit_expiring),
                "medical_insurance_expired": int(medical_insurance_expired),
                "medical_insurance_expiring": int(medical_insurance_expiring),
                "other_documents_expired": int(other_documents_expired),
                "other_documents_expiring": int(other_documents_expiring),
                "other_documents_not_started": int(other_documents_not_started),
                "document_follow_up": document_follow_up,
                "incomplete_profiles": int(incomplete_profiles),
                "missing_compliance_dates": int(missing_compliance_dates),
                "missing_bank_details": int(missing_bank_details),
                "employee_code_conflicts": len(
                    [item for item in employee_code_conflicts.split(",") if item]
                ),
            }

    def list_audit_logs(self, search: str = "", limit: int = 500) -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if search.strip():
            like = f"%{search.strip()}%"
            where = "WHERE username LIKE ? OR action LIKE ? OR entity_type LIKE ? OR details LIKE ?"
            params = [like] * 4
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM audit_logs {where} ORDER BY id DESC LIMIT ?", [*params, limit]
            ).fetchall()
            return [dict(r) for r in rows]

    def all_export_data(self) -> dict[str, list[dict[str, Any]]]:
        with self.connection() as conn:
            employees = [dict(r) for r in conn.execute("SELECT * FROM employees ORDER BY emp_code").fetchall()]
            attendance = [dict(r) for r in conn.execute(
                """SELECT a.id, e.emp_code, e.name, a.attendance_date, a.status, a.note,
                          a.created_at, a.updated_at
                   FROM attendance a JOIN employees e ON e.id=a.employee_id
                   ORDER BY a.attendance_date DESC, e.emp_code"""
            ).fetchall()]
            leaves = [dict(r) for r in conn.execute(
                """SELECT l.id, e.emp_code, e.name, l.leave_type, l.start_date, l.end_date,
                          l.total_days, l.status, l.reason, l.notes, l.created_at, l.updated_at
                   FROM leave_requests l JOIN employees e ON e.id=l.employee_id
                   ORDER BY l.start_date DESC"""
            ).fetchall()]
            calendar_notes = [dict(r) for r in conn.execute(
                "SELECT note_date, note, created_at, updated_at FROM calendar_notes ORDER BY note_date DESC"
            ).fetchall()]
            message_templates = [dict(r) for r in conn.execute(
                "SELECT id, name, channel, subject, body, is_active, created_at, updated_at "
                "FROM message_templates ORDER BY name"
            ).fetchall()]
            communication_logs = [dict(r) for r in conn.execute(
                """SELECT c.id, e.emp_code, e.name, c.channel, t.name AS template_name,
                          c.subject, c.delivery_mode, c.status, u.username, c.created_at
                   FROM communication_logs c
                   LEFT JOIN employees e ON e.id=c.employee_id
                   LEFT JOIN message_templates t ON t.id=c.template_id
                   LEFT JOIN users u ON u.id=c.created_by
                   ORDER BY c.id DESC"""
            ).fetchall()]
            custom_fields = [dict(r) for r in conn.execute(
                """SELECT id,label,field_type,options_json,is_required,is_active,sort_order,
                          created_at,updated_at
                   FROM employee_custom_fields ORDER BY sort_order,label"""
            ).fetchall()]
            custom_values = [dict(r) for r in conn.execute(
                """SELECT e.emp_code,e.name,f.label,f.field_type,v.value_text,v.updated_at
                   FROM employee_custom_values v
                   JOIN employees e ON e.id=v.employee_id
                   JOIN employee_custom_fields f ON f.id=v.field_id
                   ORDER BY e.emp_code,f.sort_order,f.label"""
            ).fetchall()]
            saved_views = [dict(r) for r in conn.execute(
                """SELECT sv.id,u.username AS owner,sv.scope,sv.name,sv.filters_json,
                          sv.is_shared,sv.is_default,sv.created_at,sv.updated_at
                   FROM saved_views sv JOIN users u ON u.id=sv.owner_user_id
                   ORDER BY sv.scope,u.username,sv.name"""
            ).fetchall()]
            company = company_profile_rows(self.company_profile())
            return {"Company": company, "Employees": employees, "Attendance": attendance,
                    "Leaves": leaves, "CalendarNotes": calendar_notes,
                    "MessageTemplates": message_templates, "Communications": communication_logs,
                    "EmployeeCustomFields": custom_fields, "EmployeeCustomValues": custom_values,
                    "SavedViews": saved_views}

    # ---------- Backups ----------
    def create_snapshot(self, destination: Path | str) -> Path:
        with self._backup_lock:
            return self._create_snapshot_unlocked(destination)

    def _create_snapshot_unlocked(self, destination: Path | str) -> Path:
        """ينشئ لقطة SQLite متسقة مع إغلاق المقابض صراحةً لتفادي WinError 32.

        مدير السياق في sqlite3 ينفذ commit/rollback لكنه لا يغلق الاتصال؛ لذلك لا
        نستخدم ``with sqlite3.connect(...)`` هنا. الإغلاق الصريح مهم خصوصًا على Windows.
        """
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".partial")
        safe_unlink(partial)
        last_error: Exception | None = None
        for attempt in range(6):
            source: sqlite3.Connection | None = None
            target: sqlite3.Connection | None = None
            try:
                source = sqlite3.connect(self.path, timeout=60, check_same_thread=False)
                source.execute("PRAGMA busy_timeout = 60000")
                target = sqlite3.connect(partial, timeout=60, check_same_thread=False)
                target.execute("PRAGMA busy_timeout = 60000")
                source.backup(target, pages=256, sleep=0.05)
                target.commit()
                integrity = target.execute("PRAGMA quick_check").fetchone()[0]
                if integrity != "ok":
                    raise DatabaseError("فشل التحقق من سلامة لقطة قاعدة البيانات.")
                target.close(); target = None
                source.close(); source = None
                atomic_replace(partial, destination)
                return destination
            except (sqlite3.Error, OSError, DatabaseError) as exc:
                last_error = exc
                if target is not None:
                    try: target.close()
                    except sqlite3.Error: pass
                if source is not None:
                    try: source.close()
                    except sqlite3.Error: pass
                safe_unlink(partial)
                if attempt < 5:
                    time.sleep(0.12 * (attempt + 1))
            finally:
                if target is not None:
                    try: target.close()
                    except sqlite3.Error: pass
                if source is not None:
                    try: source.close()
                    except sqlite3.Error: pass
        raise DatabaseError(f"تعذر إنشاء نسخة قاعدة البيانات بعد عدة محاولات: {last_error}")

    def create_backup(self, actor: dict[str, Any] | None, automatic: bool = False) -> Path:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        kind = "auto" if automatic else "manual"
        destination = BACKUP_DIR / f"hr_backup_{kind}_{stamp}.db"
        self.create_snapshot(destination)
        try:
            with self.connection() as conn:
                self._audit_conn(conn, actor, "BACKUP", "database", destination.name, {"automatic": automatic})
        except Exception:
            # النسخة نفسها صحيحة؛ لا نفشل العملية بسبب تعذر تسجيل حدث التدقيق.
            setup_logging().warning("تم إنشاء النسخة لكن تعذر تسجيلها في سجل التدقيق", exc_info=True)
        self._cleanup_old_backups()
        return destination

    def automatic_backup_if_due(self, actor: dict[str, Any] | None = None) -> Path | None:
        today = date.today().isoformat()
        with self.connection() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key='last_auto_backup'").fetchone()
            if row and row[0] == today:
                return None
        path = self.create_backup(actor, automatic=True)
        with self.connection() as conn:
            conn.execute(
                "INSERT INTO app_settings(key,value) VALUES('last_auto_backup',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (today,),
            )
        return path

    def list_backups(self) -> list[Path]:
        return sorted(BACKUP_DIR.glob("hr_backup_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)

    def restore_backup(self, backup_path: Path | str, actor: dict[str, Any]) -> None:
        if actor.get("role") != "admin":
            raise PermissionError("استعادة النسخ الاحتياطية متاحة للمدير فقط.")
        backup_path = Path(backup_path)
        if not backup_path.exists():
            raise DatabaseError("ملف النسخة الاحتياطية غير موجود.")
        with closing(sqlite3.connect(backup_path, timeout=60)) as test_conn:
            integrity = test_conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise DatabaseError("ملف النسخة الاحتياطية تالف.")
            required = {r[0] for r in test_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"users", "employees", "attendance", "audit_logs"}.issubset(required):
                raise DatabaseError("هذا الملف ليس نسخة احتياطية صالحة للنظام.")
        safety = self.create_backup(actor, automatic=False)
        temp = self.path.with_suffix(".restore.tmp")
        safe_unlink(temp)
        shutil.copy2(backup_path, temp)
        for suffix in ("-wal", "-shm"):
            safe_unlink(Path(str(self.path) + suffix))
        atomic_replace(temp, self.path)
        self.initialize()
        with self.connection() as conn:
            self._audit_conn(conn, actor, "RESTORE", "database", backup_path.name, {"safety_backup": safety.name})

    @staticmethod
    def _cleanup_old_backups(keep: int = 30) -> None:
        backups = sorted(BACKUP_DIR.glob("hr_backup_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in backups[keep:]:
            try:
                old.unlink()
            except OSError:
                pass

    # ---------- Messaging ----------
    def list_departments(self) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT department FROM employees WHERE TRIM(COALESCE(department,''))<>'' ORDER BY department"
            ).fetchall()
            return [str(row[0]) for row in rows]

    def list_message_recipients(
        self, *, search: str = "", department: str = "", status: str = "نشط",
        employee_ids: Iterable[int] | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if search.strip():
            like = f"%{search.strip()}%"
            where.append("(emp_code LIKE ? OR name LIKE ? OR department LIKE ? OR job_title LIKE ?)")
            params.extend([like, like, like, like])
        if department.strip() and department != "الكل":
            where.append("department=?")
            params.append(department.strip())
        if status.strip() and status != "الكل":
            if status not in EMPLOYEE_STATUSES:
                raise ValueError("حالة الموظف غير صحيحة.")
            where.append("status=?")
            params.append(status)
        ids = [int(item) for item in (employee_ids or [])]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            where.append(f"id IN ({placeholders})")
            params.extend(ids)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT id, emp_code, name, phone, email, department, job_title, status "
                f"FROM employees{clause} ORDER BY name", params,
            ).fetchall()
            return [dict(row) for row in rows]

    def list_message_templates(self, channel: str = "", active_only: bool = True) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if channel:
            if channel not in {"email", "whatsapp", "both"}:
                raise ValueError("قناة القالب غير صحيحة.")
            if channel != "both":
                where.append("channel IN (?, 'both')")
                params.append(channel)
            else:
                where.append("channel='both'")
        if active_only:
            where.append("is_active=1")
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM message_templates{clause} ORDER BY name", params
            ).fetchall()
            return [dict(row) for row in rows]

    def get_message_template(self, template_id: int) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM message_templates WHERE id=?", (template_id,)).fetchone()
            return dict(row) if row else None

    def save_message_template(
        self, data: dict[str, Any], actor: dict[str, Any], template_id: int | None = None
    ) -> int:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("إدارة قوالب الرسائل متاحة للمدير والموارد البشرية فقط.")
        name = str(data.get("name", "")).strip()
        channel = str(data.get("channel", "both")).strip()
        subject = str(data.get("subject", "")).strip()
        body = str(data.get("body", "")).strip()
        is_active = 1 if bool(data.get("is_active", True)) else 0
        if not name:
            raise ValueError("اسم القالب مطلوب.")
        if channel not in {"email", "whatsapp", "both"}:
            raise ValueError("قناة القالب غير صحيحة.")
        if not body:
            raise ValueError("نص الرسالة مطلوب.")
        try:
            with self.connection() as conn:
                if template_id:
                    exists = conn.execute("SELECT 1 FROM message_templates WHERE id=?", (template_id,)).fetchone()
                    if not exists:
                        raise DatabaseError("القالب غير موجود.")
                    conn.execute(
                        """UPDATE message_templates SET name=?,channel=?,subject=?,body=?,is_active=?,
                           updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (name, channel, subject, body, is_active, actor.get("id"), template_id),
                    )
                    saved_id = int(template_id)
                    action = "UPDATE"
                else:
                    cur = conn.execute(
                        """INSERT INTO message_templates(name,channel,subject,body,is_active,created_by,updated_by)
                           VALUES(?,?,?,?,?,?,?)""",
                        (name, channel, subject, body, is_active, actor.get("id"), actor.get("id")),
                    )
                    saved_id = int(cur.lastrowid)
                    action = "CREATE"
                self._audit_conn(conn, actor, action, "message_template", saved_id, {
                    "name": name, "channel": channel, "active": bool(is_active)
                })
                return saved_id
        except sqlite3.IntegrityError as exc:
            raise DatabaseError("يوجد قالب آخر بالاسم نفسه.") from exc

    def delete_message_template(self, template_id: int, actor: dict[str, Any]) -> None:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("إدارة قوالب الرسائل متاحة للمدير والموارد البشرية فقط.")
        with self.connection() as conn:
            row = conn.execute("SELECT name FROM message_templates WHERE id=?", (template_id,)).fetchone()
            if not row:
                return
            conn.execute("DELETE FROM message_templates WHERE id=?", (template_id,))
            self._audit_conn(conn, actor, "DELETE", "message_template", template_id, {"name": row[0]})

    def log_communication(
        self, employee_ids: Iterable[int], channel: str, subject: str,
        delivery_mode: str, status: str, actor: dict[str, Any], template_id: int | None = None,
    ) -> int:
        if actor.get("role") not in {"admin", "hr"}:
            raise PermissionError("إرسال الرسائل متاح للمدير والموارد البشرية فقط.")
        if channel not in {"email", "whatsapp"}:
            raise ValueError("قناة المراسلة غير صحيحة.")
        ids = sorted({int(item) for item in employee_ids})
        if not ids:
            return 0
        with self.connection() as conn:
            existing = {
                int(row[0]) for row in conn.execute(
                    f"SELECT id FROM employees WHERE id IN ({','.join('?' for _ in ids)})", ids
                ).fetchall()
            }
            rows = [
                (employee_id, channel, template_id, subject[:250], delivery_mode[:80], status[:80], actor.get("id"))
                for employee_id in ids if employee_id in existing
            ]
            conn.executemany(
                """INSERT INTO communication_logs(
                       employee_id,channel,template_id,subject,delivery_mode,status,created_by
                   ) VALUES(?,?,?,?,?,?,?)""", rows,
            )
            self._audit_conn(conn, actor, "OPEN_MESSAGE", "communication", None, {
                "channel": channel, "count": len(rows), "mode": delivery_mode,
                "template_id": template_id, "status": status,
            })
            return len(rows)

    def list_communication_logs(self, limit: int = 300) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """SELECT c.*, e.emp_code, e.name AS employee_name, u.username,
                          t.name AS template_name
                   FROM communication_logs c
                   LEFT JOIN employees e ON e.id=c.employee_id
                   LEFT JOIN users u ON u.id=c.created_by
                   LEFT JOIN message_templates t ON t.id=c.template_id
                   ORDER BY c.id DESC LIMIT ?""", (max(1, min(int(limit), 2000)),)
            ).fetchall()
            return [dict(row) for row in rows]

    # ---------- Helpers ----------
    def _audit_conn(
        self, conn: sqlite3.Connection, actor: dict[str, Any] | None,
        action: str, entity_type: str, entity_id: Any, details: dict[str, Any]
    ) -> None:
        username = actor.get("username", "SYSTEM") if actor else "SYSTEM"
        user_id = actor.get("id") if actor else None
        if user_id is not None:
            exists = conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone()
            if not exists:
                user_id = None
        conn.execute(
            "INSERT INTO audit_logs(user_id, username, action, entity_type, entity_id, details) VALUES(?,?,?,?,?,?)",
            (user_id, username, action, entity_type, str(entity_id) if entity_id is not None else None,
             json.dumps(details, ensure_ascii=False, default=str)),
        )
