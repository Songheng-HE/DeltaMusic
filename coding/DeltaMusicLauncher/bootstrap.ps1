[CmdletBinding()]
param(
    [switch]$Repair,
    [switch]$NoLaunch
)

# This script is intentionally the only part of the ZIP that can install
# Python.  Imports and the GUI remain per-user and non-administrator.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSCommandPath
$Requirements = Join-Path $Root 'requirements.txt'
$Launcher = Join-Path $Root 'launcher\DeltaMusicLauncher.py'
$LocalData = if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { Join-Path $env:USERPROFILE 'AppData\Local' }
$RuntimeRoot = Join-Path $LocalData 'DeltaMusic\runtime'
$VenvDir = Join-Path $RuntimeRoot 'venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPythonw = Join-Path $VenvDir 'Scripts\pythonw.exe'

function Test-PythonPath {
    param([string]$Path)

    if (-not $Path -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $versionText = (& $Path -c "import sys; print('.'.join(map(str, sys.version_info[:2])))" 2>$null | Select-Object -Last 1).Trim()
        if ($LASTEXITCODE -ne 0) {
            return $null
        }
        $version = [version]$versionText
        if ($version -lt [version]'3.10') {
            return $null
        }
        return (Resolve-Path -LiteralPath $Path).Path
    }
    catch {
        return $null
    }
}

function Find-SystemPython {
    $fromVenv = Test-PythonPath $VenvPython
    if ($fromVenv) {
        return $fromVenv
    }

    $pyCommand = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if (-not $pyCommand) {
        $pyCommand = Get-Command 'py' -ErrorAction SilentlyContinue
    }
    if ($pyCommand) {
        try {
            $reported = (& $pyCommand.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1).Trim()
            $found = Test-PythonPath $reported
            if ($found) {
                return $found
            }
        }
        catch {
            # Continue with other known locations.
        }
    }

    $pythonCommand = Get-Command 'python.exe' -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command 'python' -ErrorAction SilentlyContinue
    }
    if ($pythonCommand) {
        $found = Test-PythonPath $pythonCommand.Source
        if ($found) {
            return $found
        }
    }

    $knownLocations = @(
        (Join-Path $env:ProgramFiles 'Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python311\python.exe'),
        (Join-Path $LocalData 'Programs\Python\Python312\python.exe'),
        (Join-Path $LocalData 'Programs\Python\Python311\python.exe')
    )
    foreach ($candidate in $knownLocations) {
        $found = Test-PythonPath $candidate
        if ($found) {
            return $found
        }
    }
    return $null
}

function Install-OfficialPython {
    Write-Host ''
    Write-Host 'Python 3.10 or newer was not found.' -ForegroundColor Yellow
    Write-Host 'DeltaMusic can download the official Python 3.12 installer from python.org.'
    Write-Host 'Windows will show a UAC administrator-permission prompt for the installation.'
    $answer = Read-Host 'Download and install it now? [Y/n]'
    if ($answer -and $answer -notmatch '^[Yy]') {
        throw 'Python is required. Install Python 3.10+ from https://www.python.org/downloads/windows/ and start this file again.'
    }

    if (-not [Environment]::Is64BitOperatingSystem) {
        throw 'This release currently includes an x64 Python installation route only.'
    }

    $version = '3.12.10'
    $url = "https://www.python.org/ftp/python/$version/python-$version-amd64.exe"
    $installer = Join-Path $env:TEMP "DeltaMusic-python-$version-amd64.exe"
    try {
        Write-Host 'Downloading official Python installer...'
        Invoke-WebRequest -Uri $url -OutFile $installer
        Write-Host 'Starting the official installer. Approve the Windows UAC prompt if you want to continue.'
        Start-Process -FilePath $installer -ArgumentList @(
            '/quiet',
            'InstallAllUsers=1',
            'PrependPath=1',
            'Include_pip=1',
            'Include_launcher=1'
        ) -Verb RunAs -Wait
    }
    finally {
        if (Test-Path -LiteralPath $installer) {
            Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        }
    }

    $installed = Find-SystemPython
    if (-not $installed) {
        throw 'Python installation finished but Python could not be located. Restart Windows, then run Start_DeltaMusic.cmd again.'
    }
    return $installed
}

function Test-Dependencies {
    param([string]$Python)
    # A missing import writes a Python traceback to stderr.  Do not let the
    # global Stop preference turn that expected probe failure into a setup
    # failure before pip gets a chance to repair the environment.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        & $Python -c 'import keyboard, mido, pydirectinput' 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

try {
    if (-not (Test-Path -LiteralPath $Requirements -PathType Leaf)) {
        throw 'requirements.txt is missing. Re-download and fully extract the DeltaMusic release.'
    }
    if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
        throw 'launcher\DeltaMusicLauncher.py is missing. Re-download and fully extract the DeltaMusic release.'
    }

    New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
    $Python = Find-SystemPython
    if (-not $Python) {
        $Python = Install-OfficialPython
    }

    $VenvReady = Test-PythonPath $VenvPython
    if (-not $VenvReady) {
        Write-Host 'Creating a private DeltaMusic Python environment...'
        & $Python -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not create the private Python environment.'
        }
        $VenvReady = Test-PythonPath $VenvPython
    }
    if (-not $VenvReady) {
        throw 'The private DeltaMusic Python environment is unavailable.'
    }

    if ($Repair -or -not (Test-Dependencies $VenvReady)) {
        Write-Host 'Installing or repairing DeltaMusic dependencies...'
        & $VenvReady -m pip install --upgrade pip
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not update pip.'
        }
        & $VenvReady -m pip install --upgrade -r $Requirements
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not install DeltaMusic dependencies. Check your network connection and try again.'
        }
    }

    if (-not (Test-Dependencies $VenvReady)) {
        throw 'Python dependencies are still unavailable after installation.'
    }

    if ($NoLaunch) {
        Write-Host 'Environment ready. -NoLaunch was requested.'
        exit 0
    }

    $GuiPython = if (Test-Path -LiteralPath $VenvPythonw -PathType Leaf) { $VenvPythonw } else { $VenvReady }
    Write-Host 'Environment ready. Opening DeltaMusic Launcher...'
    Start-Process -FilePath $GuiPython -ArgumentList @($Launcher) -WorkingDirectory $Root
    exit 0
}
catch {
    Write-Host ''
    Write-Host 'DeltaMusic setup failed:' -ForegroundColor Red
    Write-Host $_.Exception.ToString() -ForegroundColor Red
    Write-Host ''
    Write-Host 'For manual Python installation: https://www.python.org/downloads/windows/'
    Read-Host 'Press Enter to close'
    exit 1
}
