[CmdletBinding()]
param(
    [string]$PythonPath,
    [string]$OutputDirectory,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $PSScriptRoot 'release_exe'
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ReleaseBase = [System.IO.Path]::GetFullPath($OutputDirectory)
$ReleaseRoot = Join-Path $ReleaseBase 'DeltaMusic'
$ZipPath = Join-Path $ReleaseBase 'DeltaMusicLauncher.zip'
$ChecksumPath = Join-Path $ReleaseBase 'SHA256SUMS.txt'
$BuildRoot = Join-Path $PSScriptRoot 'build_exe'
$VenvRoot = Join-Path $BuildRoot 'venv'
$DistRoot = Join-Path $BuildRoot 'dist'
$WorkRoot = Join-Path $BuildRoot 'work'
$SpecRoot = Join-Path $BuildRoot 'spec'
$Requirements = Join-Path $PSScriptRoot 'requirements-build.txt'

$SongFolders = @(
    'croatian',
    'mariage',
    'daoxiang',
    'autumn'
)

foreach ($relative in @(
    'launcher\DeltaMusicLauncher.py',
    'launcher\player_host.py',
    'launcher\catalog.json',
    'launcher\generic_midi_player.py',
    'launcher\file_drop.py',
    'launcher\playback_control.py',
    'launcher\library.py',
    'launcher\importer.py',
    'README_EXE.md',
    'user_library_README.txt',
    'requirements-build.txt'
)) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $relative) -PathType Leaf)) {
        throw "Launcher source is incomplete: $relative"
    }
}
foreach ($folder in $SongFolders) {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot $folder) -PathType Container)) {
        throw "Expected song folder is missing: $folder"
    }
}

if ((Test-Path -LiteralPath $ReleaseRoot) -or (Test-Path -LiteralPath $ZipPath)) {
    if (-not $Force) {
        throw "EXE release already exists: $ReleaseBase`nUse -Force only if you want to rebuild this generated release."
    }
    if (Test-Path -LiteralPath $ReleaseRoot) {
        Remove-Item -LiteralPath $ReleaseRoot -Recurse -Force
    }
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    if (Test-Path -LiteralPath $ChecksumPath) {
        Remove-Item -LiteralPath $ChecksumPath -Force
    }
}

if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $candidate = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($candidate) {
        $PythonPath = $candidate.Source
    }
    else {
        $candidate = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($candidate) {
            $PythonPath = $candidate.Source
        }
    }
}
if ([string]::IsNullOrWhiteSpace($PythonPath) -or -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'A Python 3.10+ interpreter is required to build the EXE. Pass -PythonPath with its full path.'
}

& $PythonPath -c "import sys; assert sys.version_info >= (3, 10), sys.version; print(sys.version)"
if ($LASTEXITCODE -ne 0) {
    throw 'The selected Python is not version 3.10 or newer.'
}

New-Item -ItemType Directory -Force -Path $BuildRoot, $ReleaseBase | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $VenvRoot 'Scripts\python.exe') -PathType Leaf)) {
    Write-Host 'Creating isolated EXE build environment...' -ForegroundColor Cyan
    & $PythonPath -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the EXE build environment.'
    }
}

$BuildPython = Join-Path $VenvRoot 'Scripts\python.exe'
& $BuildPython -c "import PyInstaller, keyboard, mido, pydirectinput, tkinterdnd2, pypinyin; print('EXE build dependencies are ready')" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing EXE build dependencies...' -ForegroundColor Cyan
    & $BuildPython -m pip install --disable-pip-version-check -r $Requirements
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not install EXE build dependencies.'
    }
}

foreach ($folder in @($DistRoot, $WorkRoot, $SpecRoot)) {
    if (Test-Path -LiteralPath $folder) {
        Remove-Item -LiteralPath $folder -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $folder | Out-Null
}

function Build-OneFile([string]$Name, [string]$Script, [switch]$Windowed) {
    $arguments = @(
        '-m', 'PyInstaller',
        '--noconfirm', '--clean', '--onefile',
        '--name', $Name,
        '--paths', (Join-Path $PSScriptRoot 'launcher'),
        '--add-data', "$(Join-Path $PSScriptRoot 'launcher\catalog.json');launcher",
        '--collect-all', 'mido',
        '--collect-all', 'keyboard',
        '--collect-all', 'pydirectinput',
        '--distpath', $DistRoot,
        '--workpath', (Join-Path $WorkRoot $Name),
        '--specpath', $SpecRoot
    )
    if ($Windowed) {
        # These are only used by the normal-privilege GUI.  Keeping them out
        # of the elevated PlayerHost reduces the privileged executable's size
        # and dependency surface without changing playback behavior.
        $arguments += @(
            '--windowed',
            '--collect-all', 'tkinterdnd2',
            '--collect-all', 'pypinyin'
        )
    }
    $arguments += $Script
    Write-Host "Building $Name.exe..." -ForegroundColor Cyan
    & $BuildPython @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller could not build $Name.exe."
    }
}

Build-OneFile 'DeltaMusic' (Join-Path $PSScriptRoot 'launcher\DeltaMusicLauncher.py') -Windowed
Build-OneFile 'DeltaMusicPlayerHost' (Join-Path $PSScriptRoot 'launcher\player_host.py')

foreach ($file in @('DeltaMusic.exe', 'DeltaMusicPlayerHost.exe')) {
    if (-not (Test-Path -LiteralPath (Join-Path $DistRoot $file) -PathType Leaf)) {
        throw "PyInstaller output is missing: $file"
    }
}

New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
Copy-Item -LiteralPath (Join-Path $DistRoot 'DeltaMusic.exe') -Destination (Join-Path $ReleaseRoot 'DeltaMusic.exe')
Copy-Item -LiteralPath (Join-Path $DistRoot 'DeltaMusicPlayerHost.exe') -Destination (Join-Path $ReleaseRoot 'DeltaMusicPlayerHost.exe')

$ReleaseLauncher = Join-Path $ReleaseRoot 'launcher'
New-Item -ItemType Directory -Force -Path $ReleaseLauncher | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'launcher\catalog.json') -Destination (Join-Path $ReleaseLauncher 'catalog.json')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'launcher\playback_control.py') -Destination (Join-Path $ReleaseLauncher 'playback_control.py')
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README_EXE.md') -Destination (Join-Path $ReleaseRoot '00_START_HERE.md')

foreach ($folder in $SongFolders) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot $folder) -Destination (Join-Path $ReleaseRoot $folder) -Recurse
}

$LibraryPlaceholder = Join-Path $ReleaseRoot 'user_library'
New-Item -ItemType Directory -Force -Path $LibraryPlaceholder | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'user_library_README.txt') -Destination (Join-Path $LibraryPlaceholder 'README.txt')

Compress-Archive -Path (Join-Path $ReleaseRoot '*') -DestinationPath $ZipPath -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash.ToLowerInvariant()
@(
    '# DeltaMusic EXE release checksum',
    "$hash  DeltaMusicLauncher.zip"
) | Set-Content -LiteralPath $ChecksumPath -Encoding utf8

Write-Host ''
Write-Host 'DeltaMusic EXE release created:' -ForegroundColor Green
Write-Host $ReleaseRoot
Write-Host 'ZIP created:' -ForegroundColor Green
Write-Host $ZipPath
Write-Host 'SHA-256 file created:' -ForegroundColor Green
Write-Host $ChecksumPath
Write-Host ''
Write-Host 'Test by extracting the ZIP and double-clicking DeltaMusic.exe.'
