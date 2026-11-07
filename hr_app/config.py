from __future__ import annotations

import json
import hashlib
import os
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

APP_ID = "HRSystemPortable"
APP_NAME = "نظام برو تك لإدارة الموارد البشرية"
APP_VERSION = "23.0.1"
RELEASE_CHANNEL = "إصدار مستقر"
COMPANY_NAME = "شركة برو تك"
DEVELOPER_NAME = "عبدالملك علي حمزة"
DEVELOPER_EMAIL = "abdulmalkroot@gmail.com"
DEVELOPER_PHONE = "+966503276927"
DEFAULT_DEVELOPER_PROFILE = {
    "company": COMPANY_NAME,
    "developer": DEVELOPER_NAME,
    "phone": DEVELOPER_PHONE,
    "email": DEVELOPER_EMAIL,
    "website": "",
}


def _application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _migrate_legacy_user_data(target: Path, app_dir: Path) -> Path:
    r"""ينسخ بيانات نسخة قديمة إلى ProgramData دون حذف الأصل.

    بعض حزم 17.10.x كانت تحفظ ``ProTech_HR_Data`` بجوار ملف التشغيل. الإصدار
    المثبّت الجديد يحفظ البيانات المشتركة في ``C:\ProgramData\ProTech\HR\Data``.
    لا يحدث النقل إلا عندما لا توجد قاعدة في الموقع الجديد، ويُنشئ نسخة SQLite
    متسقة وبيان ترحيل قبل اعتماد القاعدة الجديدة. لا يُحذف المصدر القديم مطلقًا.
    """
    if os.name != "nt" or (target / "hr_system.db").exists():
        return target

    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    candidates = [
        app_dir / "ProTech_HR_Data",
        local_app_data / APP_ID,
        local_app_data / "Programs" / "ProTechHR" / "ProTech_HR_Data",
        local_app_data / "Programs" / "ProTech HR" / "ProTech_HR_Data",
        local_app_data / "ProTechHR" / "ProTech_HR_Data",
    ]
    # إصدارات أقدم كانت تُثبَّت أحيانًا تحت Program Files باسم تنفيذي مختلف.
    # نبحث في المواقع المعروفة فقط، ونترك الأصل كما هو بعد النسخ الآمن.
    for environment_name in ("ProgramFiles", "ProgramFiles(x86)"):
        program_files = os.environ.get(environment_name, "").strip()
        if not program_files:
            continue
        root = Path(program_files)
        candidates.extend(
            (
                root / "PRO TECH" / "ProTech HR" / "ProTech_HR_Data",
                root / "ProTech" / "ProTech HR" / "ProTech_HR_Data",
                root / "ProTechHR" / "ProTech_HR_Data",
            )
        )
    def usable_legacy_source(candidate: Path) -> bool:
        try:
            return (
                candidate.resolve() != target.resolve()
                and (candidate / "hr_system.db").is_file()
            )
        except OSError:
            return False

    source = next((candidate for candidate in candidates if usable_legacy_source(candidate)), None)
    if source is None:
        return target

    target.mkdir(parents=True, exist_ok=True)
    source_db = source / "hr_system.db"
    target_db = target / "hr_system.db"
    migration_dir = target / "migration_backups"
    migration_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    migration_db = migration_dir / f"legacy_{stamp}.db"
    migration_manifest = migration_dir / f"legacy_{stamp}.json"
    try:
        source_connection = sqlite3.connect(source_db, timeout=60)
        backup_connection = sqlite3.connect(migration_db, timeout=60)
        target_connection = sqlite3.connect(target_db, timeout=60)
        try:
            source_connection.backup(backup_connection, pages=512, sleep=0.05)
            backup_connection.commit()
            if backup_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("تعذر التحقق من النسخة الاحتياطية للقاعدة القديمة.")
            source_connection.backup(target_connection, pages=512, sleep=0.05)
            target_connection.commit()
            if target_connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("تعذر التحقق من قاعدة البيانات المنقولة.")
        finally:
            target_connection.close()
            backup_connection.close()
            source_connection.close()
    except (OSError, sqlite3.Error):
        # لا ننسخ ملف SQLite خامًا عند فشل النسخ المتسق؛ فقد تكون له ملفات WAL
        # نشطة. نترك المصدر القديم محفوظًا ونبدأ بقاعدة نظيفة قابلة للاستعادة.
        target_db.unlink(missing_ok=True)
        migration_db.unlink(missing_ok=True)
        return target

    digest = hashlib.sha256()
    with source_db.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    migration_manifest.write_text(
        json.dumps(
            {
                "release": APP_VERSION,
                "migrated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "source_data_directory": str(source),
                "source_database_sha256": digest.hexdigest(),
                "consistent_backup": str(migration_db),
                "destination_database": str(target_db),
                "source_deleted": False,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    for name in (
        "backups",
        "employee_documents",
        "view_cache",
        "logs",
    ):
        old_dir = source / name
        if old_dir.is_dir():
            shutil.copytree(old_dir, target / name, dirs_exist_ok=True)
    for name in ("developer_profile.json", "installation_profile.json"):
        old_file = source / name
        if old_file.is_file() and not (target / name).exists():
            shutil.copy2(old_file, target / name)
    return target


def _user_data_dir() -> Path:
    explicit = os.environ.get("PROTECH_HR_DATA_DIR", "").strip()
    if explicit:
        return Path(explicit).expanduser().resolve()

    app_dir = _application_dir()
    portable_root = os.environ.get("PROTECH_HR_PORTABLE_ROOT", "").strip()
    if portable_root:
        return Path(portable_root).expanduser().resolve() / "ProTech_HR_Data"
    if (app_dir / "portable_mode.flag").exists():
        return app_dir / "ProTech_HR_Data"

    if os.name == "nt":
        program_data = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        return _migrate_legacy_user_data(program_data / "ProTech" / "HR" / "Data", app_dir)
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    elif xdg_data_home := os.environ.get("XDG_DATA_HOME"):
        root = Path(xdg_data_home)
    else:
        root = Path.home() / ".local" / "share"
    return root / APP_ID


APP_DIR = _application_dir()
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
APP_ICON_PATH = RESOURCE_DIR / "assets" / "protech_hr.ico"
DATA_DIR = _user_data_dir()
DB_PATH = DATA_DIR / "hr_system.db"
BACKUP_DIR = DATA_DIR / "backups"
MIGRATION_BACKUP_DIR = DATA_DIR / "migration_backups"
LOG_DIR = DATA_DIR / "logs"
DOCUMENTS_DIR = DATA_DIR / "employee_documents"
IDENTITY_DIR = DOCUMENTS_DIR / "identity_copies"
VIEW_CACHE_DIR = DATA_DIR / "view_cache"
EXPORT_DIR = Path.home() / "Documents" / "ProTechHRExports"
BRANDING_PATH = DATA_DIR / "developer_profile.json"

for directory in (
    DATA_DIR, BACKUP_DIR, MIGRATION_BACKUP_DIR, LOG_DIR, DOCUMENTS_DIR,
    IDENTITY_DIR, VIEW_CACHE_DIR, EXPORT_DIR,
):
    directory.mkdir(parents=True, exist_ok=True)


def is_portable_mode() -> bool:
    return DATA_DIR.name == "ProTech_HR_Data" or bool(os.environ.get("PROTECH_HR_PORTABLE_ROOT"))


def developer_profile() -> dict[str, str]:
    profile = dict(DEFAULT_DEVELOPER_PROFILE)
    try:
        if BRANDING_PATH.exists():
            loaded = json.loads(BRANDING_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                for key in profile:
                    value = str(loaded.get(key, "")).strip()
                    if key == "developer" and value in {"", COMPANY_NAME, "PRO TECH"}:
                        continue
                    if value:
                        profile[key] = value
        BRANDING_PATH.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass
    return profile


COPYRIGHT_TEXT = f"© 2025-{datetime.now().year} ProTech — جميع الحقوق محفوظة"

ROLES = {"admin": "المدير - صلاحيات كاملة", "hr": "الموارد البشرية", "viewer": "قراءة فقط"}
ATTENDANCE_STATUSES = ("حاضر", "غائب", "إجازة", "عمل عن بُعد", "مهمة عمل")
EMPLOYEE_STATUSES = ("نشط", "في إجازة", "منتهي الخدمة")
LEAVE_TYPES = ("سنوية", "مرضية", "اضطرارية", "بدون راتب", "أمومة", "أبوة", "زواج", "وفاة", "أخرى")
LEAVE_STATUSES = ("معلّقة", "معتمدة", "مرفوضة", "ملغاة")
CONTRACT_TYPES = ("محدد المدة", "غير محدد المدة", "جزئي", "مؤقت", "تدريب")
CONTRACT_STATUSES = ("ساري", "منتهي", "معلّق", "ملغي")
QIWA_CONTRACT_STATUSES = (
    "غير موثق", "مسودة", "مرسل للموظف", "بانتظار موافقة الموظف",
    "موثق", "مرفوض", "ملغي",
)
DOCUMENT_TYPES = (
    "هوية وطنية", "إقامة", "جواز سفر", "كرت عمل", "تأمين طبي",
    "عقد", "شهادة", "رخصة", "تأمين", "أخرى",
)
PAYROLL_STATUSES = ("مسودة", "معتمد")
PAGE_SIZE = 25
