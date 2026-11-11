# ProTech HR v23.0.1

> **Current license / الترخيص الحالي: All Rights Reserved.**
> © 2026 Abu Omar / ProTech. Prior written permission is required for use,
> copying, modification, publication or distribution of current ProTech-owned material.
> جميع الحقوق محفوظة. يلزم إذن كتابي مسبق للاستخدام أو النسخ أو التعديل أو التوزيع.
> Third-party licenses and previously distributed MIT copies remain unaffected.
> See [LICENSE](LICENSE) and [licensing transition](LICENSING_TRANSITION.md).

Human-resources, payroll, attendance and reporting application for Windows,
with Arabic-oriented UI.

Developer: Abu Omar / ProTech
Email: protech.2025.2026@gmail.com
License: All Rights Reserved for original ProTech-owned code; third-party materials remain
under their own licenses.

## Downloads
The Windows installer and Portable archive are published under GitHub Releases,
not committed as binaries to Git history.

## Build
Source is in hr_app/ and run_hr.pyw. Python dependencies are pinned in
requirements.txt; build configuration is in build_assets/.
To create the installer, build/copy the portable distribution into dist
and use Inno Setup. For a new distribution, verify that the installer presents the current root LICENSE. Previously published installers remain unchanged.

## Notes
The original release report recorded 26 passing source tests, successful
EXE smoke and export checks, but no clean-machine Setup installation test.
Do not treat successful compilation as full acceptance.

Never upload actual employee details, payroll records, bank information,
personal files, databases, credentials or production backup archives.

## Official Project Website / الموقع الرسمي

For features, product status and official download information, see [the Alghad Digital product page](https://alghad.digital/programs/hr/).

التفاصيل وروابط التنزيل الرسمية: [صفحة البرنامج](https://alghad.digital/programs/hr/).
