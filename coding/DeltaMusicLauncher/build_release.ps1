[CmdletBinding()]
param(
    [string]$OutputDirectory,
    [switch]$Zip,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $PSScriptRoot 'release'
}
$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$ReleaseBase = [System.IO.Path]::GetFullPath($OutputDirectory)
$ReleaseRoot = Join-Path $ReleaseBase 'DeltaMusic'
$ZipPath = Join-Path $ReleaseBase 'DeltaMusic.zip'

$SongFolders = @(
    'croatian',
    'mariage',
    'daoxiang',
    'take_my_breath_away',
    'autumn'
)
$RequiredLauncherFiles = @(
    'Start_DeltaMusic.cmd',
    'bootstrap.ps1',
    'requirements.txt',
    'README_Launcher.md',
    'user_library_README.txt',
    'launcher\DeltaMusicLauncher.py',
    'launcher\player_host.py',
    'launcher\generic_midi_player.py',
    'launcher\library.py',
    'launcher\importer.py',
    'launcher\catalog.json'
)

foreach ($relative in $RequiredLauncherFiles) {
    $candidate = Join-Path $PSScriptRoot $relative
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Launcher source is incomplete: $relative"
    }
}
foreach ($folder in $SongFolders) {
    $candidate = Join-Path $ProjectRoot $folder
    if (-not (Test-Path -LiteralPath $candidate -PathType Container)) {
        throw "Expected song folder is missing: $folder"
    }
}

if (Test-Path -LiteralPath $ReleaseRoot) {
    if (-not $Force) {
        throw "Release folder already exists: $ReleaseRoot`nUse -Force only if you want to rebuild this generated release folder."
    }
    Remove-Item -LiteralPath $ReleaseRoot -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $ReleaseRoot | Out-Null
$ReleaseLauncher = Join-Path $ReleaseRoot 'launcher'
New-Item -ItemType Directory -Force -Path $ReleaseLauncher | Out-Null

foreach ($file in @('Start_DeltaMusic.cmd', 'bootstrap.ps1', 'requirements.txt')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination (Join-Path $ReleaseRoot $file)
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'README_Launcher.md') -Destination (Join-Path $ReleaseRoot '00_START_HERE.md')

foreach ($file in @('__init__.py', 'DeltaMusicLauncher.py', 'player_host.py', 'generic_midi_player.py', 'library.py', 'importer.py', 'catalog.json')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot "launcher\$file") -Destination (Join-Path $ReleaseLauncher $file)
}

foreach ($folder in $SongFolders) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot $folder) -Destination (Join-Path $ReleaseRoot $folder) -Recurse
}

foreach ($document in Get-ChildItem -LiteralPath $ProjectRoot -File) {
    if ($document.Extension -in @('.md', '.pdf')) {
        Copy-Item -LiteralPath $document.FullName -Destination (Join-Path $ReleaseRoot $document.Name)
    }
}

$LibraryPlaceholder = Join-Path $ReleaseRoot 'user_library'
New-Item -ItemType Directory -Force -Path $LibraryPlaceholder | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'user_library_README.txt') -Destination (Join-Path $LibraryPlaceholder 'README.txt')

if ($Zip) {
    if (Test-Path -LiteralPath $ZipPath) {
        if (-not $Force) {
            throw "Release ZIP already exists: $ZipPath`nUse -Force only if you want to replace this generated ZIP."
        }
        Remove-Item -LiteralPath $ZipPath -Force
    }
    Compress-Archive -Path (Join-Path $ReleaseRoot '*') -DestinationPath $ZipPath -CompressionLevel Optimal
}

Write-Host ''
Write-Host 'DeltaMusic release created:' -ForegroundColor Green
Write-Host $ReleaseRoot
if ($Zip) {
    Write-Host 'ZIP created:' -ForegroundColor Green
    Write-Host $ZipPath
}
Write-Host ''
Write-Host 'Test the release by extracting the ZIP, then double-click Start_DeltaMusic.cmd.'
