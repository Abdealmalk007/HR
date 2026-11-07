#Requires -Version 5.1
<#
.SYNOPSIS
  Verify a Windows Authenticode-signed release binary.
.DESCRIPTION
  This is a fail-closed pre-publish gate. It NEVER signs, installs,
  runs, bypasses SmartScreen, or changes the release.
.EXAMPLE
  powershell.exe -NoProfile -File tools\Test-ReleaseAuthenticode.ps1 -Path "C:\Release\ProTech_HR_Setup.exe"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true, ValueFromPipeline = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$Path
)
begin {
    $ErrorActionPreference = 'Stop'
    $failed = 0
}
process {
    foreach ($p in $Path) {
        try {
            $item = Get-Item -LiteralPath $p -ErrorAction Stop
            if ($item.PSIsContainer -or $item.Extension -notin @('.exe', '.dll', '.msi', '.msix')) {
                throw 'Expected an existing EXE, DLL, MSI or MSIX file'
            }
            $signature = Get-AuthenticodeSignature -LiteralPath $item.FullName -ErrorAction Stop
            $cert = $signature.SignerCertificate
            $hasCodeSigningEku = $false
            if ($null -ne $cert) {
                $hasCodeSigningEku = @($cert.EnhancedKeyUsageList | Where-Object {
                    $_.ObjectId.Value -eq '1.3.6.1.5.5.7.3.3'
                }).Count -gt 0
            }
            $validity = ($null -ne $cert -and
                         $cert.NotBefore -le (Get-Date) -and
                         $cert.NotAfter -ge (Get-Date))
            $ok = ($signature.Status -eq [System.Management.Automation.SignatureStatus]::Valid -and
                   $hasCodeSigningEku -and $validity)
            $summary = [PSCustomObject]@{
                File = $item.FullName
                Sha256 = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
                SignatureStatus = [string]$signature.Status
                Publisher = if ($cert) { $cert.Subject } else { '<unsigned>' }
                TrustedSignatureAndCodeSigningEku = $ok
            }
            $summary | Format-List | Out-String | Write-Host
            if (-not $ok) {
                Write-Host 'BLOCKED: Do not publish as a signed release.' -ForegroundColor Red
                $failed += 1
            }
        } catch {
            Write-Host ('BLOCKED: ' + $p + ' — ' + $_.Exception.Message) -ForegroundColor Red
            $failed += 1
        }
    }
}
end {
    if ($failed -gt 0) {
        Write-Host ('FAIL: ' + $failed + ' release file(s) failed validation.') -ForegroundColor Red
        exit 1
    }
    Write-Host 'PASS: Authenticode verification completed.' -ForegroundColor Green
    Write-Host 'Note: a valid signature does not guarantee a positive SmartScreen reputation.' -ForegroundColor Yellow
    exit 0
}
