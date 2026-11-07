from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class ImportantLink:
    category: str
    title: str
    description: str
    url: str


IMPORTANT_LINKS = (
    ImportantLink("الخدمات الحكومية", "قوى", "خدمات العمل والعقود والمنشآت.", "https://www.qiwa.sa/"),
    ImportantLink("الخدمات الحكومية", "مقيم", "خدمات المقيمين للمنشآت.", "https://muqeem.sa/"),
    ImportantLink("الخدمات الحكومية", "التأمينات الاجتماعية", "خدمات المشتركين وأصحاب العمل.", "https://www.gosi.gov.sa/"),
    ImportantLink("الخدمات الحكومية", "مُدد", "حماية الأجور وإدارة الرواتب.", "https://mudad.com.sa/"),
    ImportantLink("الخدمات الحكومية", "أبشر أعمال", "خدمات المنشآت المرتبطة بالجوازات.", "https://absher.sa/wps/portal/business/Home/homepublic"),
    ImportantLink("الخدمات الحكومية", "أجير", "تنظيم العمل المؤقت وإعارة خدمات العاملين.", "https://ajeer.qiwa.sa/"),
    ImportantLink("الخدمات الحكومية", "وزارة الموارد البشرية", "بوابة الوزارة وخدمات العمل.", "https://www.hrsd.gov.sa/"),
    ImportantLink("الخدمات الحكومية", "مجلس الضمان الصحي", "دليل خدمات التأمين الصحي.", "https://www.chi.gov.sa/ServicesDirectory/Pages/Default.aspx"),
    ImportantLink("الخدمات الحكومية", "المركز السعودي للأعمال", "خدمات الأعمال والمنشآت.", "https://www.saudibusiness.gov.sa/Identity/Account/Login"),
    ImportantLink(
        "الأنظمة والأدلة الرسمية",
        "نظام العمل السعودي",
        "النص الرسمي لدى هيئة الخبراء بمجلس الوزراء.",
        "https://laws.boe.gov.sa/BoeLaws/Laws/LawDetails/08381293-6388-48e2-8ad2-a9a700f2aa94/1",
    ),
    ImportantLink(
        "الأنظمة والأدلة الرسمية",
        "لوائح تنظيم العمل",
        "صفحة اللوائح الرسمية بوزارة الموارد البشرية.",
        "https://www.hrsd.gov.sa/en/node/838894",
    ),
    ImportantLink(
        "الأنظمة والأدلة الرسمية",
        "اللائحة التنفيذية لنظام العمل",
        "نسخة PDF الرسمية من الوزارة.",
        "https://www.hrsd.gov.sa/sites/default/files/2025-04/%D8%A7%D9%84%D9%84%D8%A7%D8%A6%D8%AD%D8%A9%20%D8%A7%D9%84%D8%AA%D9%86%D9%81%D9%8A%D8%B0%D9%8A%D8%A9%20%D9%84%D9%86%D8%B8%D8%A7%D9%85%20%D8%A7%D9%84%D8%B9%D9%85%D9%84%20%D9%88%D9%85%D9%84%D8%AD%D9%82%D8%A7%D8%AA%D9%87%D8%A7.pdf",
    ),
    ImportantLink(
        "الأنظمة والأدلة الرسمية",
        "حاسبة مكافأة نهاية الخدمة",
        "الحاسبة الرسمية الاسترشادية للوزارة.",
        "https://www.hrsd.gov.sa/ministry-services/services/end-service-benefit-calculator?s=09",
    ),
    ImportantLink(
        "الأنظمة والأدلة الرسمية",
        "السلامة والصحة المهنية",
        "إرشادات ومراجع الوزارة للسلامة في بيئة العمل.",
        "https://www.hrsd.gov.sa/care-about-you/health-and-safety",
    ),
)

ALLOWED_HOSTS = frozenset(
    {
        "www.qiwa.sa",
        "muqeem.sa",
        "www.gosi.gov.sa",
        "mudad.com.sa",
        "absher.sa",
        "ajeer.qiwa.sa",
        "www.hrsd.gov.sa",
        "www.chi.gov.sa",
        "www.saudibusiness.gov.sa",
        "laws.boe.gov.sa",
    }
)


def validate_official_url(url: str) -> str:
    parsed = urlparse(str(url).strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.casefold() not in ALLOWED_HOSTS:
        raise ValueError("الرابط ليس ضمن قائمة النطاقات الرسمية المعتمدة.")
    if parsed.username or parsed.password:
        raise ValueError("لا يُسمح بروابط تحتوي على بيانات دخول.")
    return url
