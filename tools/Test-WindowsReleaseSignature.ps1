#Requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string[]]$Path)
$ErrorActionPreference='Stop'
$script:failures=0
$script:passed=0
function Check-Binary([string]$candidate) {
    $sig=Get-AuthenticodeSignature -LiteralPath $candidate
    $cert=$sig.SignerCertificate
    $eku=$false
    if($null -ne $cert){
        $eku=@($cert.EnhancedKeyUsageList|Where-Object{$_.ObjectId.Value -eq '1.3.6.1.5.5.7.3.3'}).Count -gt 0
    }
    if($sig.Status -eq 'Valid' -and $null -ne $cert -and $eku -and $cert.Subject -ne $cert.Issuer){
        Write-Host ('PASS: '+$candidate) -ForegroundColor Green;$script:passed++
    }else{
        Write-Host ('BLOCKED: '+$candidate+' | Authenticode='+$sig.Status) -ForegroundColor Red;$script:failures++
    }
}
foreach($p in $Path){
    $item=Get-Item -LiteralPath $p -ErrorAction Stop
    if($item.PSIsContainer){throw 'Expected a file'}
    switch($item.Extension.ToLowerInvariant()){
        '.exe' {Check-Binary $item.FullName}
        '.msi' {Check-Binary $item.FullName}
        '.dll' {Check-Binary $item.FullName}
        '.zip' {
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $archive=[System.IO.Compression.ZipFile]::OpenRead($item.FullName)
            $count=0
            try{
                if($archive.Entries.Count -gt 10000){throw 'Too many archive entries'}
                foreach($entry in $archive.Entries){
                    if($entry.Name -notmatch '(?i)\.(exe|msi)$'){continue}
                    $count++
                    if($entry.Length -gt 700MB){throw 'Executable too large'}
                    $tmp=Join-Path ([IO.Path]::GetTempPath()) ([Guid]::NewGuid().ToString('N')+[IO.Path]::GetExtension($entry.Name))
                    try{
                        $src=$entry.Open()
                        try{$dst=[IO.File]::Create($tmp);try{$src.CopyTo($dst)}finally{$dst.Dispose()}}finally{$src.Dispose()}
                        Write-Host ('ZIP member: '+$entry.FullName)
                        Check-Binary $tmp
                    }finally{Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue}
                }
            }finally{$archive.Dispose()}
            if($count -eq 0){Write-Host 'BLOCKED: ZIP has no executable' -ForegroundColor Red;$script:failures++}
        }
        default {throw ('Unsupported artifact: '+$item.Name)}
    }
}
if($script:failures -gt 0 -or $script:passed -eq 0){
    Write-Host ('BLOCKED: failed='+$script:failures+', signed='+$script:passed) -ForegroundColor Red;exit 1
}
Write-Host 'PASS: Trusted Authenticode signatures. SmartScreen reputation still not guaranteed.' -ForegroundColor Green
exit 0
