# ProTech HR v23.0.1

Human-resources, payroll, attendance and reporting application for Windows,
with Arabic-oriented UI.

Developer: Abu Omar / ProTech
Email: protech.2025.2026@gmail.com
License: MIT for original ProTech-owned code; third-party materials remain
under their own licenses.

## Downloads
The Windows installer and Portable archive are published under GitHub Releases,
not committed as binaries to Git history.

## Build
Source is in hr_app/ and run_hr.pyw. Python dependencies are pinned in
requirements.txt; build configuration is in build_assets/.
To create the installer, build/copy the portable distribution into dist
and use Inno Setup. The public build script displays the MIT license rather
than the obsolete restrictive EULA.

## Notes
The original release report recorded 26 passing source tests, successful
EXE smoke and export checks, but no clean-machine Setup installation test.
Do not treat successful compilation as full acceptance.

Never upload actual employee details, payroll records, bank information,
personal files, databases, credentials or production backup archives.
