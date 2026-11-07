# توقيع ProTech HR وتقليل تحذيرات Microsoft Defender SmartScreen

**الحالة الحالية في 7 نوفمبر 2026:** المثبّت المنشور `v23.0.1` غير موقّع (`Authenticode: NotSigned`). لم يُعثر على شهادة `Code Signing` بملكية مفتاح خاص في مخزني شهادات المستخدم والجهاز وقت الفحص. لذلك لا يجوز وصف الإصدار بأنه خالٍ من تحذيرات SmartScreen.

## القاعدة التقنية

- رخصة **MIT** هي رخصة استخدام الكود، وليست شهادة هوية ناشر.
- التوقيع بشهادة موثوقة يثبت هوية الناشر وسلامة الملف، لكنه لا يضمن إزالة SmartScreen فورًا: سمعة التطبيق والناشر تتراكم بمرور الوقت.
- شهادة `Self-signed` ليست بديلًا مناسبًا للنشر العام، ولا يُنصح بتعطيل Windows Defender أو تجاوز التحذيرات برمجيًا.
- ميّز بين تحذير SmartScreen والتحقق من هوية الناشر في نافذة UAC؛ حتى التطبيق الموقّع قد يعرض UAC عندما يطلب صلاحيات المدير.

## مسارات النشر الموثوقة

1. **Microsoft Store بنظام MSIX:** Microsoft تعيد توقيع حزمة MSIX المقبولة. يجب التأكد أولًا من توافق البرنامج الحالي مع MSIX، خصوصًا المسارات والنسخ الاحتياطية والبيانات والترقية.
2. **GitHub Releases مع توقيع ناشر موثوق:** الحصول على شهادة `OV Code Signing` من جهة إصدار موثوقة (متاحة عالميًا) بعد التحقق من هوية الناشر؛ `EV` لا تضمن تخطي SmartScreen. التوقيع بـ `SHA-256` وإضافة ختم زمني `RFC 3161`.
3. **SignPath Foundation للمشاريع المفتوحة المصدر المؤهلة:** يتطلب التقديم ومراجعة شروط المصدر والمكونات، وليس مضمون القبول. الرابط: https://signpath.org/ .

## سير إصدار ProTech HR بعد الحصول على الشهادة

1. التحقق من أن الشهادة صالحة لتوقيع الكود وأن مفتاحها الخاص مخزن بطريقة آمنة. لا تضع `PFX` أو كلمات مروره داخل Git أو مجلد عام.
2. توقيع ملفات EXE/DLL الداخلية التي تتطلب التوقيع قبل تعبئة المثبّت، مع الحفاظ على شهادات المكتبات الخارجية وعدم استبدالها.
3. استخدم `SignTool` داخل **Inno Setup** أثناء التجميع لتوقيع `Setup` و`Uninstall` والنسخ المؤقتة؛ توقيع `Setup.exe` بعد التجميع فقط لا يغطي كل هذه الملفات.
4. تحقق بواسطة `Get-AuthenticodeSignature` أو `signtool verify /pa /all /v` من التوقيع والسلسلة والشهادة، وشغّل فحص `tools/Test-ReleaseAuthenticode.ps1`.
5. اختبر التثبيت والإزالة والترقية وحفظ قاعدة البيانات على جهاز Windows نظيف.
6. بعد نجاح الفحوصات، أنشئ **إصدار GitHub جديدًا** بالبصمات الجديدة. لا تستبدل ملف `v23.0.1` دون مراجعة واضحة، ولا تعلن ضمان إزالة SmartScreen.

الأداة الحالية للتوقيع `Inno Setup 6.7.3` طبعت `Non-commercial use only`؛ تحقق من استحقاق ترخيص استخدام أداة البناء قبل أي توزيع تجاري.

## مثال لفحص المثبّت الحالي

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".\tools\Test-ReleaseAuthenticode.ps1" -Path "E:\ProTech\GitHub\HR_v23_0_1_MIT_Build\Output\ProTech_HR_Setup_v23.0.1_MIT.exe"
```

يجب أن يُرجع هذا الفحص **FAIL** للإصدار الحالي لأنه غير موقّع. ومن الخطأ تحويل ذلك إلى نجاح دون شهادة حقيقية.

**المراجع:**
- Microsoft Learn: https://learn.microsoft.com/windows/apps/package-and-deploy/smartscreen-reputation
- Microsoft Learn: https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options
- Inno Setup: https://jrsoftware.org/ishelp/topic_setup_signtool.htm
