[CmdletBinding()]
param(
    [switch]$BuildRelease
)

$ErrorActionPreference = 'Stop'
$ReleaseRoot = Join-Path $PSScriptRoot 'release\DeltaMusic'
$ScriptPath = Join-Path $PSScriptRoot 'installer\DeltaMusic.iss'

if ($BuildRelease -or -not (Test-Path -LiteralPath $ReleaseRoot -PathType Container)) {
    & (Join-Path $PSScriptRoot 'build_release.ps1') -Zip -Force:$BuildRelease
    if ($LASTEXITCODE -ne 0) {
        throw 'Release build failed; installer was not created.'
    }
}

$candidates = @(
    (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'),
    (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
)
$compiler = $candidates | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
if (-not $compiler) {
    throw 'Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php, then run this script again.'
}

& $compiler "/DSourcePath=$ReleaseRoot" $ScriptPath
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup failed with exit code $LASTEXITCODE."
}
Write-Host 'Installer created in release\installer.' -ForegroundColor Green

