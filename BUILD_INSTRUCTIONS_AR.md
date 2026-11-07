# تعليمات البناء — ProTech HR v23.0.1

هذا الإصدار مستقر، ويُبنى على Windows 10/11 بمعمارية 64-bit فقط.

## المتطلبات

- Python 3.12 ‏64-bit.
- Inno Setup 7.
- حزم `requirements.txt` و`requirements-build.txt` بالإصدارات المثبتة فيهما.
- Windows SDK عند الرغبة في التوقيع بواسطة `signtool.exe`.

ملاحظة ترخيص: يجب توفير ترخيص Inno Setup مناسب قبل بناء مُثبّت للاستخدام
أو التوزيع التجاري. أداة البناء ليست جزءًا من حزمة التطبيق.

## فحص واختبار المصدر

```powershell
python tools\run_all_tests_v23.py
```

أو:

```text
release_docs\RUN_TESTS_v23.0.1.cmd
```

## بناء EXE وPortable

```powershell
python -m pip install -r requirements.txt -r requirements-build.txt
python -m PyInstaller --noconfirm --clean build_assets\ProTech_HR.spec
```

يجب استخدام Python ‏64-bit؛ يوقف ملف `spec` البناء إن كانت البيئة 32-bit.
الناتج:

```text
dist\ProTech_HR_Portable\ProTech_HR.exe
```

أنشئ ملفًا فارغًا باسم `portable_mode.flag` داخل مجلد Portable فقط. عند وجوده
يحفظ البرنامج بيانات النسخة المحمولة في `ProTech_HR_Data` بجوار التطبيق.

## بناء Setup

لا تضع `portable_mode.flag` أو أي قاعدة بيانات داخل Setup. بعد اكتمال بناء
Portable شغّل:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" build_assets\ProTech_HR_Setup.iss
```

الناتج:

```text
build_assets\Output\ProTech_HR_Setup_v23.0.1.exe
```

إذا كان المشروع داخل مسار Windows طويل جدًا، انسخ مجلد Portable مؤقتًا إلى
مسار أقصر ثم مرره للمترجم دون تعديل ملف `iss`:

```powershell
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" `
  "/DSourceDir=C:\Build\ProTech_HR_Portable" `
  build_assets\ProTech_HR_Setup.iss
```

يثبت التطبيق في `C:\Program Files\ProTech\ProTech HR`، بينما تحفظ بيانات
النسخة المثبتة في `C:\ProgramData\ProTech\HR\Data`. يمنح المثبت مستخدمي
Windows صلاحية التعديل على مجلد البيانات فقط، ولا يضع البيانات في Program Files.

## الترقية الآمنة

- لا يستبدل البرنامج قاعدة موجودة في ProgramData.
- عند أول تشغيل من الإصدار الجديد يبحث عن المواقع القديمة المعروفة فقط.
- تُنسخ قاعدة SQLite عبر آلية backup المتسقة، وتُفحص سلامتها.
- تنشأ نسخة داخل `migration_backups` مع ملف JSON يحتوي البصمة ومصدر الترحيل.
- لا يُحذف الموقع القديم.
- يمنع سكربت Setup تثبيت نسخة أقدم إذا وجد إصدارًا أحدث مسجلًا.
- لا تحذف أداة إزالة التثبيت قاعدة البيانات أو النسخ أو المرفقات.

## التوقيع الرقمي

لم تُضمّن شهادة خاصة في المصدر. بعد تزويد شهادة Code Signing صالحة استخدم
أحد الخيارين:

```powershell
.\build_assets\sign_release.ps1 -CertificateThumbprint "THUMBPRINT"
```

أو:

```powershell
.\build_assets\sign_release.ps1 -CertificatePath ".\certificate.pfx" -CertificatePassword (Read-Host -AsSecureString)
```

يوقّع السكربت كل EXE وDLL وSetup باستخدام SHA-256 وTimestamp، ثم يتحقق من
كل توقيع. لا تحفظ الشهادة أو كلمة مرورها في المشروع أو الحزمة.

## حدود الاعتماد

نجاح البناء والاختبارات المحلية لا يثبت نجاح التثبيت على أجهزة Windows 10/11
نظيفة. يلزم اختبار قبول منفصل على أجهزة نظيفة، بحساب قياسي، ومسار مستخدم
عربي، وترقية فعلية من نسخة إنتاجية قبل اعتماد الإصدار للإنتاج.
