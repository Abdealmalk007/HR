# ProTech — Trusted GitHub Releases (Windows)

Date: 2026-11-07

## Current state
The authorized Windows machine has no eligible Code Signing certificate with accessible private key. Existing Windows installers for HR v23.0.1 and DailyWorkReports v15.5.0 are unsigned and may prompt Microsoft Defender SmartScreen. Nothing can legitimately suppress all reputation prompts on every PC merely by editing installers, removing Mark-of-the-Web, or modifying GitHub releases.

## Standard publication process
1. Acquire a publicly trusted OV Code Signing certificate for Abu Omar / ProTech (or apply to SignPath Foundation as a genuine, eligible open-source project). Validation/activation requires the publisher's own authorization; free signing is not automatic.
2. Use one consistent publisher identity and certificate across Windows software. Private keys stay in hardware token/HSM; never upload them, PFX passwords, or tokens to GitHub.
3. For Inno Setup, configure SignTool at **compile time** so the Setup program, temporary self-copies, and uninstall executable are signed. Sign all internally built EXE files before packaging. Do not just sign Setup.exe after compilation.
4. Timestamp signed files (RFC 3161 + SHA-256), and validate the resulting Authenticode signature on clean Windows devices.
5. Run the signature gate for each EXE, MSI and Portable ZIP. For GitHub publishing use Publish-SignedWindowsRelease.ps1. It refuses unsigned artifacts and refuses to replace releases.
6. Publish to GitHub Releases as usual with SHA-256 checksums, without rewriting old release assets.
7. Test the signed download with SmartScreen and Defender ENABLED on a clean Windows 10/11 system; signed new files can still warn until reputation accumulates.

## Testing the current unsigned HR installer
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\ProTech\GitHub\Signing\Test-WindowsReleaseSignature.ps1 -Path E:\ProTech\GitHub\HR_v23_0_1_MIT_Build\Output\ProTech_HR_Setup_v23.0.1_MIT.exe

Expected: exit code 1 and BLOCKED.

## Publishing after trusted signing is available
powershell.exe -NoProfile -ExecutionPolicy Bypass -File E:\ProTech\GitHub\Signing\Publish-SignedWindowsRelease.ps1 -Repository Abdealmalk007/HR -Tag v23.0.2 -Assets "C:\Release\ProTech_HR_Setup_v23.0.2.exe" -NotesFile "C:\Release\RELEASE_NOTES.md"

The publisher script validates signatures before uploading. GitHub Releases remains the download distribution channel.

## Notes on Android
Android APK is not subject to Windows SmartScreen. Android APKs need Android app-signing verification with apksigner, signing certificate continuity for upgrades, and separate Play Protect compatibility checks.

## Licensing
MIT covers original ProTech-owned source code. Third-party components and Windows installer tool licenses are independent. Inno Setup installed on this machine previously printed "Non-commercial use only"; confirm the applicable license before commercial use.

## Official references
- https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/smartscreen-reputation
- https://learn.microsoft.com/en-us/windows/apps/package-and-deploy/code-signing-options
- https://jrsoftware.org/ishelp/topic_setup_signtool.htm
- https://signpath.org/terms.html
