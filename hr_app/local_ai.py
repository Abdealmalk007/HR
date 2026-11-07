from __future__ import annotations

import math
import random
from statistics import median
from typing import Any, Iterable


def _finite_values(values: Iterable[Any]) -> list[float]:
    result: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            result.append(number)
    return result


def robust_anomalies(
    rows: list[dict[str, Any]],
    *,
    value_key: str,
    label_key: str,
    threshold: float = 3.5,
) -> list[dict[str, Any]]:
    """Detect unusual points with a median/MAD score that tolerates small data sets."""
    values = _finite_values(row.get(value_key) for row in rows)
    if len(values) < 5:
        return []
    center = median(values)
    deviations = [abs(value - center) for value in values]
    mad = median(deviations)
    if mad <= 1e-9:
        return []
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = float(row.get(value_key, 0))
        except (TypeError, ValueError):
            continue
        score = 0.6745 * (value - center) / mad
        if abs(score) >= threshold:
            result.append(
                {
                    "label": str(row.get(label_key, "")),
                    "value": value,
                    "score": round(score, 2),
                    "direction": "مرتفع" if score > 0 else "منخفض",
                }
            )
    return sorted(result, key=lambda item: abs(item["score"]), reverse=True)


def neural_forecast(values: Iterable[Any], *, lookback: int = 3) -> dict[str, Any]:
    """Train a deterministic, tiny local neural regressor and predict the next point.

    The model is intentionally small: it is quick enough for a desktop UI, keeps all
    data on-device, and declines to predict when the history is too short.
    """
    series = _finite_values(values)
    lookback = max(2, min(7, int(lookback)))
    if len(series) < lookback + 4:
        return {
            "available": False,
            "prediction": None,
            "lower_bound": None,
            "upper_bound": None,
            "confidence": 0,
            "samples": max(0, len(series) - lookback),
            "reason": "يلزم سبعة أيام مسجلة على الأقل لإنتاج توقع محلي موثوق.",
            "model": "local-neural-v2",
        }
    lower, upper = min(series), max(series)
    span = upper - lower
    if span <= 1e-9:
        return {
            "available": True,
            "prediction": round(lower, 2),
            "lower_bound": round(lower, 2),
            "upper_bound": round(lower, 2),
            "confidence": 95,
            "samples": len(series) - lookback,
            "reason": "السلسلة التاريخية مستقرة.",
            "model": "local-neural-v2",
        }
    normalized = [(value - lower) / span for value in series]
    training = [
        (normalized[index - lookback : index], normalized[index])
        for index in range(lookback, len(normalized))
    ]
    hidden_size = min(6, lookback + 2)
    generator = random.Random(2100 + len(series) * 17 + lookback)
    weights_input = [
        [generator.uniform(-0.35, 0.35) for _ in range(lookback)]
        for _ in range(hidden_size)
    ]
    bias_hidden = [0.0] * hidden_size
    weights_output = [generator.uniform(-0.35, 0.35) for _ in range(hidden_size)]
    bias_output = 0.0
    rate = 0.045
    for _epoch in range(420):
        for inputs, expected in training:
            hidden = [
                math.tanh(
                    bias_hidden[node]
                    + sum(
                        weights_input[node][offset] * inputs[offset]
                        for offset in range(lookback)
                    )
                )
                for node in range(hidden_size)
            ]
            output = bias_output + sum(
                weights_output[node] * hidden[node] for node in range(hidden_size)
            )
            error = max(-2.0, min(2.0, output - expected))
            old_output_weights = list(weights_output)
            for node in range(hidden_size):
                weights_output[node] -= rate * error * hidden[node]
            bias_output -= rate * error
            for node in range(hidden_size):
                hidden_gradient = (
                    error * old_output_weights[node] * (1.0 - hidden[node] ** 2)
                )
                for offset in range(lookback):
                    weights_input[node][offset] -= (
                        rate * hidden_gradient * inputs[offset]
                    )
                bias_hidden[node] -= rate * hidden_gradient
        rate = max(0.012, rate * 0.995)

    def predict(inputs: list[float]) -> float:
        hidden = [
            math.tanh(
                bias_hidden[node]
                + sum(
                    weights_input[node][offset] * inputs[offset]
                    for offset in range(lookback)
                )
            )
            for node in range(hidden_size)
        ]
        return bias_output + sum(
            weights_output[node] * hidden[node] for node in range(hidden_size)
        )

    errors = [abs(predict(inputs) - expected) for inputs, expected in training]
    mean_error = sum(errors) / max(1, len(errors))
    raw_prediction = max(0.0, min(1.5, predict(normalized[-lookback:])))
    prediction = max(0.0, lower + raw_prediction * span)
    sample_factor = min(1.0, len(training) / 24.0)
    confidence = int(max(25, min(92, (1.0 - min(1.0, mean_error)) * 75 + sample_factor * 17)))
    return {
        "available": True,
        "prediction": round(prediction, 2),
        "lower_bound": round(max(0.0, prediction - mean_error * span * 1.65), 2),
        "upper_bound": round(max(0.0, prediction + mean_error * span * 1.65), 2),
        "confidence": confidence,
        "samples": len(training),
        "reason": "توقع محلي مع نطاق احتمالي محسوب من خطأ التدريب، ويزداد اعتماده مع تراكم البيانات.",
        "model": "local-neural-v2",
    }


def workforce_risk_profile(metrics: dict[str, Any]) -> dict[str, Any]:
    """Produce an explainable local operational-risk score without sensitive fields."""
    attendance_records = max(0, int(metrics.get("attendance_records") or 0))
    absences = max(0, int(metrics.get("absences") or 0))
    late_days = max(0, int(metrics.get("late_days") or 0))
    pending_leaves = max(0, int(metrics.get("pending_leaves") or 0))
    performance = max(0, min(5, int(metrics.get("performance_rating") or 0)))

    score = 0.0
    factors: list[str] = []
    if attendance_records:
        absence_rate = absences / attendance_records
        late_rate = late_days / attendance_records
        absence_points = min(35.0, absence_rate * 100.0)
        late_points = min(20.0, late_rate * 65.0)
        score += absence_points + late_points
        if absence_rate >= 0.15:
            factors.append(f"غياب {absence_rate * 100:.0f}% ضمن السجل الحديث")
        if late_rate >= 0.2:
            factors.append(f"تأخر في {late_rate * 100:.0f}% من الأيام")

    contract_days = metrics.get("contract_days_left")
    if contract_days is not None:
        contract_days = int(contract_days)
        if contract_days < 0:
            score += 30
            factors.append("العقد منتهي")
        elif contract_days <= 30:
            score += 22
            factors.append("العقد ينتهي خلال 30 يوماً")
        elif contract_days <= 90:
            score += 10
            factors.append("العقد ينتهي خلال 90 يوماً")

    document_days = metrics.get("document_days_left")
    if document_days is None:
        score += 8
        factors.append("لا يوجد تاريخ انتهاء لوثيقة الهوية")
    else:
        document_days = int(document_days)
        if document_days < 0:
            score += 25
            factors.append("وثيقة الهوية منتهية")
        elif document_days <= 30:
            score += 16
            factors.append("وثيقة الهوية تنتهي خلال 30 يوماً")
        elif document_days <= 90:
            score += 7
            factors.append("وثيقة الهوية تنتهي خلال 90 يوماً")

    if performance and performance <= 2:
        score += 12
        factors.append("تقييم الأداء منخفض")
    if pending_leaves >= 2:
        score += min(8, pending_leaves * 2)
        factors.append(f"طلبات إجازة معلقة: {pending_leaves}")

    score = int(round(max(0.0, min(100.0, score))))
    if score >= 70:
        level = "مرتفع"
        recommendation = "مراجعة الحالة فوراً ووضع إجراء متابعة موثق."
    elif score >= 40:
        level = "متوسط"
        recommendation = "مراجعة العوامل خلال هذا الأسبوع وتحديث الوثائق أو الخطة."
    else:
        level = "منخفض"
        recommendation = "لا يلزم إجراء عاجل؛ تستمر المتابعة الدورية."
    if not factors:
        factors.append("لا توجد مؤشرات تشغيلية غير اعتيادية")
    return {
        "score": score,
        "level": level,
        "factors": factors,
        "recommendation": recommendation,
        "model": "workforce-risk-v2",
    }


def workforce_insights(analytics: dict[str, Any]) -> list[dict[str, str]]:
    insights: list[dict[str, str]] = []
    records = int(analytics.get("attendance_records") or 0)
    rate = float(analytics.get("attendance_rate") or 0)
    if records == 0:
        insights.append(
            {"level": "info", "title": "بيانات الحضور", "message": "لا توجد سجلات حضور ضمن الفترة المختارة."}
        )
    elif rate < 75:
        insights.append(
            {"level": "warning", "title": "معدل الحضور", "message": f"معدل الحضور {rate:.1f}% ويستحسن مراجعة أسباب الغياب."}
        )
    else:
        insights.append(
            {"level": "success", "title": "معدل الحضور", "message": f"معدل الحضور ضمن الفترة {rate:.1f}%."}
        )
    anomalies = analytics.get("anomalies") or []
    if anomalies:
        labels = "، ".join(str(item.get("label", "")) for item in anomalies[:3])
        insights.append(
            {"level": "warning", "title": "أيام غير اعتيادية", "message": f"رصد المحرك تغيرًا غير معتاد في: {labels}."}
        )
    forecast = analytics.get("forecast") or {}
    if forecast.get("available"):
        insights.append(
            {
                "level": "info",
                "title": "توقع الغياب القادم",
                "message": (
                    f"يتوقع النموذج المحلي نحو {float(forecast['prediction']):.1f} حالة "
                    f"بثقة تقديرية {int(forecast['confidence'])}%."
                ),
            }
        )
    else:
        insights.append(
            {"level": "info", "title": "التعلم المحلي", "message": str(forecast.get("reason", ""))}
        )
    return insights
