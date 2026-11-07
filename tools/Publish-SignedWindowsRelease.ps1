#Requires -Version 5.1
[CmdletBinding()]
param(
 [Parameter(Mandatory=$true)][string]$Repository,
 [Parameter(Mandatory=$true)][string]$Tag,
 [Parameter(Mandatory=$true)][string[]]$Assets,
 [Parameter(Mandatory=$true)][string]$NotesFile,
 [string]$Title='ProTech signed release',
 [switch]$Stable
)
$ErrorActionPreference='Stop'
$gh='C:\Program Files\GitHub CLI\gh.exe'
if(-not (Test-Path -LiteralPath $gh)){throw 'GitHub CLI not found'}
if($Repository -notmatch '^[A-Za-z0-9-]+/[A-Za-z0-9_.-]+$'){throw 'Invalid repository'}
if($Tag -notmatch '^v[0-9]+\.[0-9]+\.[0-9]+(?:[-.][A-Za-z0-9.-]+)?$'){throw 'Invalid tag'}
if(-not (Test-Path -LiteralPath $NotesFile -PathType Leaf)){throw 'Missing release notes'}
$resolved=@()
foreach($p in $Assets){
 $file=Get-Item -LiteralPath $p -ErrorAction Stop
 if($file.Extension.ToLowerInvariant() -notin @('.exe','.msi','.dll','.zip')){throw 'Unsupported artifact'}
 $resolved+=$file.FullName
}
if(-not $resolved.Count){throw 'No release assets'}
& (Join-Path $PSScriptRoot 'Test-WindowsReleaseSignature.ps1') -Path $resolved
if($LASTEXITCODE -ne 0){throw 'Unsigned release BLOCKED before upload'}
& $gh repo view $Repository --json nameWithOwner --jq .nameWithOwner | Out-Host
if($LASTEXITCODE -ne 0){throw 'Repository not accessible'}
$releaseData = & $gh release list --repo $Repository --limit 100 --json tagName
if($LASTEXITCODE -ne 0){throw 'Unable to check existing releases'}
$existing=@(($releaseData -join [Environment]::NewLine)|ConvertFrom-Json)
if(@($existing|Where-Object {$_.tagName -eq $Tag}).Count -gt 0){throw 'Release already exists'}
$hash=Join-Path ([IO.Path]::GetTempPath()) ('SHA256SUMS-'+[Guid]::NewGuid().ToString('N')+'.txt')
try{
 $lines=@(foreach($p in $resolved){(Get-FileHash -LiteralPath $p -Algorithm SHA256).Hash.ToLowerInvariant()+'  '+(Split-Path $p -Leaf)})
 [IO.File]::WriteAllLines($hash,[string[]]$lines,(New-Object System.Text.UTF8Encoding($false)))
 $opts=@('release','create',$Tag)+$resolved+@($hash,'--repo',$Repository,'--title',$Title,'--notes-file',$NotesFile,'--target','main','--draft')
 if(-not $Stable){$opts+='--prerelease'}
 & $gh @opts | Out-Host
 if($LASTEXITCODE -ne 0){throw 'Draft release creation failed'}
 & $gh release edit $Tag --repo $Repository --draft=false | Out-Host
 if($LASTEXITCODE -ne 0){throw 'Draft remains; publish failed'}
 Write-Host ('SIGNED RELEASE PUBLISHED: '+$Repository+' '+$Tag) -ForegroundColor Green
}finally{
 Remove-Item -LiteralPath $hash -Force -ErrorAction SilentlyContinue
}
