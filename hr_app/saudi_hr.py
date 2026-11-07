from __future__ import annotations

import csv
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .database import Database, PermissionError
from .identity_rules import is_saudi_nationality
from .money import money


SAUDIZATION_TARGET_KEY = "saudi.compliance.saudization_target"
REGULATORY_REFERENCE_DATE = "2026-08-16"

TERMINATION_REASONS = (
    "انتهاء العقد أو إنهاء من صاحب العمل",
    "استقالة",
    "قوة قاهرة أو استحقاق كامل نظامًا",
    "إنهاء خلال فترة التجربة",
)

REGULATORY_GUIDANCE = (
    {
        "title": "توثيق عقود العمل",
        "summary": "تابع حالة توثيق العقد في قوى؛ التسجيل هنا سجل داخلي ولا يرسل البيانات إلى المنصة.",
        "url": "https://www.qiwa.sa/ar/business-owners/manage-current-employees/how-authenticate-contracts",
    },
    {
        "title": "فترة التجربة",
        "summary": "يجب النص عليها صراحة في العقد وألا يتجاوز مجموعها 180 يومًا، مع مراعاة الاستثناءات النظامية.",
        "url": "https://www.hrsd.gov.sa/علاقات-العمل",
    },
    {
        "title": "الإجازة السنوية",
        "summary": "الحد الأدنى 21 يومًا، ويرتفع إلى 30 يومًا بعد خمس سنوات متصلة لدى صاحب العمل.",
        "url": "https://www.hrsd.gov.sa/شروط-العمل-وظروفه",
    },
    {
        "title": "ساعات العمل",
        "summary": "الأصل 8 ساعات يوميًا أو 48 أسبوعيًا، وتخفض للمسلمين في رمضان إلى 6 ساعات أو 36 أسبوعيًا.",
        "url": "https://www.hrsd.gov.sa/شروط-العمل-وظروفه",
    },
    {
        "title": "مكافأة نهاية الخدمة",
        "summary": "نصف أجر شهر عن كل سنة من الخمس الأولى، وأجر شهر عن كل سنة تالية، مع أحكام خاصة بالاستقالة.",
        "url": "https://www.hrsd.gov.sa/ministry-services/services/end-service-benefit-calculator",
    },
    {
        "title": "التأمينات الاجتماعية",
        "summary": "تحقق من التسجيل والأجر الخاضع للاشتراك والتغطيات مباشرة في التأمينات؛ النظام يقيس اكتمال البيانات فقط.",
        "url": "https://www.gosi.gov.sa/GOSIOnline/FAQ_Employer",
    },
    {
        "title": "حماية الأجور",
        "summary": "اكتمال الراتب والهوية والآيبان مؤشر جاهزية للملف، وليس إثبات رفعه أو قبوله في مُدد.",
        "url": "https://mudad.com.sa/",
    },
)


def _parse_date(value: date | str, label: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value or "").strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"صيغة {label} يجب أن تكون YYYY-MM-DD.") from exc


def _anniversary(value: date, year: int) -> date:
    try:
        return value.replace(year=year)
    except ValueError:
        return value.replace(year=year, day=28)


def _add_months(value: date, months: int) -> date:
    total = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))


def service_duration(start_date: date | str, end_date: date | str) -> dict[str, Any]:
    start = _parse_date(start_date, "تاريخ بداية الخدمة")
    end = _parse_date(end_date, "تاريخ نهاية الخدمة")
    if end < start:
        raise ValueError("تاريخ نهاية الخدمة لا يمكن أن يسبق تاريخ البداية.")

    years = end.year - start.year
    if _anniversary(start, start.year + years) > end:
        years -= 1
    anchor = _anniversary(start, start.year + years)
    months = 0
    while months < 11 and _add_months(anchor, months + 1) <= end:
        months += 1
    month_anchor = _add_months(anchor, months)
    days = (end - month_anchor).days
    decimal_years = (
        Decimal(years)
        + (Decimal(months) / Decimal(12))
        + (Decimal(days) / Decimal(365))
    )
    return {
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "years": years,
        "months": months,
        "days": days,
        "total_days": (end - start).days,
        "decimal_years": decimal_years,
    }


def annual_leave_minimum(join_date: date | str, as_of: date | str | None = None) -> int:
    reference = _parse_date(as_of or date.today(), "تاريخ الاحتساب")
    duration = service_duration(join_date, reference)
    return 30 if duration["decimal_years"] >= Decimal(5) else 21


def calculate_end_of_service(
    last_wage: Any,
    start_date: date | str,
    end_date: date | str,
    reason: str,
) -> dict[str, Any]:
    wage = money(last_wage)
    if wage <= 0:
        raise ValueError("الأجر الأخير يجب أن يكون أكبر من صفر.")
    if reason not in TERMINATION_REASONS:
        raise ValueError("سبب انتهاء العلاقة غير معتمد في الحاسبة.")

    duration = service_duration(start_date, end_date)
    years = duration["decimal_years"]
    first_five = min(years, Decimal(5))
    following = max(Decimal(0), years - Decimal(5))
    base_award = wage * Decimal("0.5") * first_five + wage * following

    if reason == "استقالة":
        if years < Decimal(2):
            factor = Decimal(0)
        elif years <= Decimal(5):
            factor = Decimal(1) / Decimal(3)
        elif years < Decimal(10):
            factor = Decimal(2) / Decimal(3)
        else:
            factor = Decimal(1)
    elif reason == "إنهاء خلال فترة التجربة":
        factor = Decimal(0)
    else:
        factor = Decimal(1)

    quantum = Decimal("0.01")
    base_award = base_award.quantize(quantum, rounding=ROUND_HALF_UP)
    final_award = (base_award * factor).quantize(quantum, rounding=ROUND_HALF_UP)
    return {
        **duration,
        "last_wage": wage,
        "reason": reason,
        "base_award": base_award,
        "entitlement_factor": factor,
        "final_award": final_award,
        "reference_date": REGULATORY_REFERENCE_DATE,
    }


class SaudiHRService:
    """قراءات امتثال داخلية مشتقة من البيانات المحفوظة، وليست ربطًا حكوميًا."""

    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def guidance() -> tuple[dict[str, str], ...]:
        return REGULATORY_GUIDANCE

    def saudization_target(self) -> Decimal | None:
        raw = self.db.get_setting(SAUDIZATION_TARGET_KEY, "").strip()
        if not raw:
            return None
        try:
            target = Decimal(raw)
        except Exception:
            return None
        return target if Decimal(0) <= target <= Decimal(100) else None

    def set_saudization_target(self, value: Any, actor: dict[str, Any]) -> Decimal | None:
        if actor.get("role") != "admin":
            raise PermissionError("تحديد نسبة السعودة المستهدفة متاح للمدير فقط.")
        text = str(value if value is not None else "").strip()
        if text:
            try:
                target = Decimal(text)
            except Exception as exc:
                raise ValueError("نسبة السعودة المستهدفة يجب أن تكون رقمًا.") from exc
            if not Decimal(0) <= target <= Decimal(100):
                raise ValueError("نسبة السعودة المستهدفة يجب أن تكون بين 0 و100.")
            stored = format(target.quantize(Decimal("0.01")), "f")
        else:
            target = None
            stored = ""
        with self.db.connection() as conn:
            conn.execute(
                "INSERT INTO app_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SAUDIZATION_TARGET_KEY, stored),
            )
            self.db._audit_conn(
                conn,
                actor,
                "UPDATE",
                "saudi_compliance_settings",
                SAUDIZATION_TARGET_KEY,
                {"saudization_target": stored or None},
            )
        return target

    @staticmethod
    def _table_exists(conn, table: str) -> bool:
        return bool(
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
        )

    def compliance_snapshot(self, as_of: date | str | None = None) -> dict[str, Any]:
        reference = _parse_date(as_of or date.today(), "تاريخ القياس")
        today = reference.isoformat()
        deadline = (reference + timedelta(days=90)).isoformat()
        current_year = reference.year
        with self.db.connection() as conn:
            employees = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM employees WHERE status<>'منتهي الخدمة' ORDER BY name,emp_code"
                ).fetchall()
            ]
            contracts: list[dict[str, Any]] = []
            if self._table_exists(conn, "contracts"):
                contract_columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(contracts)").fetchall()
                }
                qiwa_status = "qiwa_status" if "qiwa_status" in contract_columns else "'غير موثق' AS qiwa_status"
                qiwa_number = (
                    "qiwa_contract_number"
                    if "qiwa_contract_number" in contract_columns
                    else "'' AS qiwa_contract_number"
                )
                contracts = [
                    dict(row)
                    for row in conn.execute(
                        f"""SELECT id,employee_id,contract_number,contract_type,start_date,end_date,
                                   probation_end_date,status,{qiwa_status},{qiwa_number}
                            FROM contracts ORDER BY start_date DESC,id DESC"""
                    ).fetchall()
                ]
            balances: dict[int, float] = {}
            if self._table_exists(conn, "leave_balances"):
                balances = {
                    int(row["employee_id"]): float(row["entitlement"] or 0)
                    for row in conn.execute(
                        "SELECT employee_id,entitlement FROM leave_balances "
                        "WHERE balance_year=? AND leave_type='سنوية'",
                        (current_year,),
                    ).fetchall()
                }

        current_contracts: dict[int, dict[str, Any]] = {}
        for contract in contracts:
            employee_id = int(contract["employee_id"])
            start = str(contract.get("start_date") or "")
            end = str(contract.get("end_date") or "")
            if (
                contract.get("status") == "ساري"
                and start <= today
                and (not end or end >= today)
                and employee_id not in current_contracts
            ):
                current_contracts[employee_id] = contract

        issues: list[dict[str, Any]] = []

        def add_issue(employee: dict[str, Any], severity: str, category: str, details: str, action: str) -> None:
            issues.append(
                {
                    "employee_id": int(employee["id"]),
                    "emp_code": str(employee.get("emp_code") or ""),
                    "employee": str(employee.get("name") or ""),
                    "severity": severity,
                    "category": category,
                    "details": details,
                    "recommended_action": action,
                }
            )

        saudi_count = 0
        non_saudi_count = 0
        unknown_nationality = 0
        wps_ready = 0
        gosi_data_ready = 0
        qiwa_documented = 0
        contract_covered = 0

        for employee in employees:
            nationality = str(employee.get("nationality") or "").strip()
            if not nationality:
                unknown_nationality += 1
                add_issue(
                    employee,
                    "متوسط",
                    "بيانات الموظف",
                    "الجنسية غير مسجلة؛ لا يمكن تصنيف الموظف في مؤشر السعودة.",
                    "استكمال الجنسية ونوع الوثيقة من ملف الموظف.",
                )
            elif is_saudi_nationality(nationality):
                saudi_count += 1
            else:
                non_saudi_count += 1

            identity_expiry = str(employee.get("id_expiry_date") or "").strip()
            if not identity_expiry:
                add_issue(employee, "عالي", "الهوية", "تاريخ انتهاء الهوية أو الإقامة غير مسجل.", "استكمال تاريخ الانتهاء وإرفاق نسخة الوثيقة.")
            elif identity_expiry < today:
                add_issue(employee, "حرج", "الهوية", f"انتهت الهوية أو الإقامة في {identity_expiry}.", "بدء التجديد والتحقق من الجهة الرسمية فورًا.")
            elif identity_expiry <= deadline:
                add_issue(employee, "متوسط", "الهوية", f"تنتهي الهوية أو الإقامة خلال 90 يومًا ({identity_expiry}).", "جدولة التجديد قبل تاريخ الانتهاء.")

            medical_expiry = str(employee.get("medical_insurance_expiry_date") or "").strip()
            if not medical_expiry:
                add_issue(employee, "عالي", "التأمين الطبي", "تاريخ انتهاء التأمين الطبي غير مسجل.", "التحقق من التغطية وإضافة تاريخ الانتهاء.")
            elif medical_expiry < today:
                add_issue(employee, "حرج", "التأمين الطبي", f"انتهى التأمين الطبي في {medical_expiry}.", "مراجعة شركة التأمين وتجديد التغطية فورًا.")
            elif medical_expiry <= deadline:
                add_issue(employee, "متوسط", "التأمين الطبي", f"ينتهي التأمين الطبي خلال 90 يومًا ({medical_expiry}).", "بدء إجراءات التجديد.")

            if nationality and not is_saudi_nationality(nationality):
                permit_expiry = str(employee.get("work_permit_expiry_date") or "").strip()
                if not permit_expiry:
                    add_issue(employee, "عالي", "رخصة العمل", "تاريخ انتهاء رخصة العمل غير مسجل لغير السعودي.", "التحقق في قوى واستكمال التاريخ.")
                elif permit_expiry < today:
                    add_issue(employee, "حرج", "رخصة العمل", f"انتهت رخصة العمل في {permit_expiry}.", "بدء التجديد والتحقق في قوى فورًا.")
                elif permit_expiry <= deadline:
                    add_issue(employee, "متوسط", "رخصة العمل", f"تنتهي رخصة العمل خلال 90 يومًا ({permit_expiry}).", "جدولة التجديد في قوى.")

            has_wps_data = all(
                (
                    str(employee.get("id_number") or "").strip(),
                    str(employee.get("bank_name") or "").strip(),
                    str(employee.get("iban") or "").strip(),
                )
            ) and float(employee.get("salary") or 0) > 0
            if has_wps_data:
                wps_ready += 1
            else:
                add_issue(employee, "عالي", "حماية الأجور", "بيانات الراتب أو الهوية أو البنك أو الآيبان غير مكتملة.", "استكمال البيانات ثم التحقق من ملف مُدد قبل الرفع.")

            has_gosi_data = all(
                (
                    nationality,
                    str(employee.get("id_number") or "").strip(),
                    str(employee.get("join_date") or "").strip(),
                )
            ) and float(employee.get("salary") or 0) > 0
            if has_gosi_data:
                gosi_data_ready += 1
            else:
                add_issue(employee, "عالي", "التأمينات", "بيانات التسجيل الأساسية أو الأجر غير مكتملة.", "استكمال الملف ومطابقته مع بيانات التأمينات.")

            contract = current_contracts.get(int(employee["id"]))
            if not contract:
                add_issue(employee, "عالي", "العقد", "لا يوجد عقد ساري مسجل في النظام.", "إضافة العقد ومطابقته مع العقد الموثق في قوى.")
            else:
                contract_covered += 1
                if str(contract.get("qiwa_status") or "غير موثق") == "موثق":
                    qiwa_documented += 1
                else:
                    add_issue(employee, "عالي", "قوى", f"حالة توثيق العقد في السجل الداخلي: {contract.get('qiwa_status') or 'غير موثق'}.", "تحديث حالة قوى بعد التحقق من المنصة الرسمية.")
                probation = str(contract.get("probation_end_date") or "").strip()
                if probation:
                    try:
                        probation_days = (
                            _parse_date(probation, "نهاية التجربة")
                            - _parse_date(contract["start_date"], "بداية العقد")
                        ).days
                    except ValueError:
                        probation_days = 0
                    if probation_days > 180:
                        add_issue(employee, "حرج", "فترة التجربة", f"مدة التجربة المسجلة {probation_days} يومًا وتتجاوز 180 يومًا.", "مراجعة نص العقد والاستثناءات النظامية وتصحيح التاريخ.")

            join_date = str(employee.get("join_date") or "").strip()
            if join_date:
                try:
                    legal_minimum = annual_leave_minimum(join_date, reference)
                except ValueError:
                    legal_minimum = 0
                recorded = balances.get(int(employee["id"]))
                if legal_minimum and recorded is None:
                    add_issue(employee, "متوسط", "الإجازة السنوية", f"لا يوجد رصيد سنوي مسجل لعام {current_year}; الحد الأدنى الإرشادي {legal_minimum} يومًا.", "إنشاء رصيد السنة ومراجعة سياسة المنشأة.")
                elif legal_minimum and recorded is not None and recorded < legal_minimum:
                    add_issue(employee, "عالي", "الإجازة السنوية", f"الاستحقاق المسجل {recorded:g} يومًا وأقل من الحد الأدنى الإرشادي {legal_minimum}.", "مراجعة الرصيد والسياسة قبل الاعتماد.")

        active_count = len(employees)
        saudization_rate = (
            (Decimal(saudi_count) * Decimal(100) / Decimal(active_count)).quantize(Decimal("0.01"))
            if active_count
            else Decimal(0)
        )
        target = self.saudization_target()
        severity_order = {"حرج": 0, "عالي": 1, "متوسط": 2, "منخفض": 3}
        issues.sort(key=lambda row: (severity_order.get(row["severity"], 9), row["employee"], row["category"]))
        return {
            "as_of": today,
            "reference_date": REGULATORY_REFERENCE_DATE,
            "active_employees": active_count,
            "saudi_employees": saudi_count,
            "non_saudi_employees": non_saudi_count,
            "unknown_nationality": unknown_nationality,
            "saudization_rate": saudization_rate,
            "saudization_target": target,
            "saudization_gap": (
                max(Decimal(0), target - saudization_rate) if target is not None else None
            ),
            "meets_saudization_target": (
                saudization_rate >= target if target is not None else None
            ),
            "contract_covered": contract_covered,
            "qiwa_documented": qiwa_documented,
            "wps_ready": wps_ready,
            "gosi_data_ready": gosi_data_ready,
            "critical_issues": sum(1 for row in issues if row["severity"] == "حرج"),
            "high_issues": sum(1 for row in issues if row["severity"] == "عالي"),
            "issues": issues,
        }

    @staticmethod
    def export_snapshot_csv(path: Path | str, snapshot: dict[str, Any], company_name: str) -> Path:
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["تقرير الامتثال السعودي", company_name])
            writer.writerow(["تاريخ القياس", snapshot.get("as_of", "")])
            writer.writerow(["نسبة السعودة", f"{snapshot.get('saudization_rate', 0)}%"])
            writer.writerow(["المستهدف", snapshot.get("saudization_target") or "غير محدد"])
            writer.writerow([])
            writer.writerow(["الخطورة", "الفئة", "رقم الموظف", "الموظف", "التفاصيل", "الإجراء المقترح"])
            for issue in snapshot.get("issues", []):
                writer.writerow(
                    [
                        issue.get("severity", ""),
                        issue.get("category", ""),
                        issue.get("emp_code", ""),
                        issue.get("employee", ""),
                        issue.get("details", ""),
                        issue.get("recommended_action", ""),
                    ]
                )
        return output
