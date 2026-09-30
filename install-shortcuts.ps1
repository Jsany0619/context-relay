[CmdletBinding()]
param(
    [string]$PythonExe,
    [string]$DesktopDir,
    [string]$ProgramsDir,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$RepoRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSCommandPath))
$Launcher = Join-Path $RepoRoot "start-manager.pyw"
$Marker = "Context Relay shortcut v1 | repo=$RepoRoot | launcher=start-manager.pyw"
$ProbeCode = 'import json,pathlib,sys,tkinter;p=pathlib.Path(sys.executable).resolve();w=p.with_name("pythonw.exe");print(json.dumps({"version":list(sys.version_info[:3]),"executable":str(p),"pythonw":str(w),"pythonw_exists":w.is_file()}))'

if ([string]::IsNullOrWhiteSpace($DesktopDir)) {
    $DesktopDir = [Environment]::GetFolderPath("DesktopDirectory")
}
if ([string]::IsNullOrWhiteSpace($ProgramsDir)) {
    $ProgramsDir = [Environment]::GetFolderPath("Programs")
}
$DesktopDir = [IO.Path]::GetFullPath($DesktopDir)
$ProgramsDir = [IO.Path]::GetFullPath($ProgramsDir)
$MenuDir = Join-Path $ProgramsDir "Context Relay"
$Specs = @(
    [pscustomobject]@{ Path = Join-Path $DesktopDir "Context Relay.lnk"; Check = $false },
    [pscustomobject]@{ Path = Join-Path $MenuDir "Context Relay.lnk"; Check = $false },
    [pscustomobject]@{ Path = Join-Path $MenuDir "Context Relay Check.lnk"; Check = $true }
)
$Shell = New-Object -ComObject WScript.Shell

function Test-OwnedShortcut([object]$Spec) {
    if (-not (Test-Path -LiteralPath $Spec.Path -PathType Leaf)) {
        return $false
    }
    try {
        $Shortcut = $Shell.CreateShortcut($Spec.Path)
        $ExpectedArguments = '-I "' + $Launcher + '"' + $(if ($Spec.Check) { " --check" } else { "" })
        return $Shortcut.Description -ceq $Marker -and
            $Shortcut.WorkingDirectory -ieq $RepoRoot -and
            $Shortcut.Arguments -ceq $ExpectedArguments -and
            [IO.Path]::GetFileName($Shortcut.TargetPath) -ieq "pythonw.exe"
    }
    catch {
        return $false
    }
}

if ($Uninstall) {
    foreach ($Spec in $Specs) {
        if (Test-OwnedShortcut -Spec $Spec) {
            Remove-Item -LiteralPath $Spec.Path -Force
        }
    }
    Write-Output "Context Relay shortcuts removed for this repository."
    exit 0
}

if (-not (Test-Path -LiteralPath $Launcher -PathType Leaf)) {
    throw "Context Relay launcher is missing: $Launcher"
}
foreach ($Directory in @($DesktopDir, $ProgramsDir)) {
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) {
        throw "Shortcut directory does not exist: $Directory"
    }
}
if ((Test-Path -LiteralPath $MenuDir) -and
        -not (Test-Path -LiteralPath $MenuDir -PathType Container)) {
    throw "Start Menu destination is not a directory: $MenuDir"
}
foreach ($Spec in $Specs) {
    if ((Test-Path -LiteralPath $Spec.Path) -and -not (Test-OwnedShortcut -Spec $Spec)) {
        throw "Refusing to replace a shortcut not owned by this Context Relay repository: $($Spec.Path)"
    }
}

function Invoke-PythonProbe([string]$Executable, [bool]$UseLauncher) {
    try {
        if ($UseLauncher) {
            $Lines = @($ProbeCode | & $Executable -3 -I - 2>$null)
        }
        else {
            $Lines = @($ProbeCode | & $Executable -I - 2>$null)
        }
        if ($LASTEXITCODE -ne 0 -or $Lines.Count -eq 0) {
            return $null
        }
        $Result = $Lines[-1] | ConvertFrom-Json
        if ($Result.version[0] -lt 3 -or
                ($Result.version[0] -eq 3 -and $Result.version[1] -lt 10) -or
                -not $Result.pythonw_exists -or
                -not (Test-Path -LiteralPath ([string]$Result.pythonw) -PathType Leaf)) {
            return $null
        }
        return $Result
    }
    catch {
        return $null
    }
}

$Candidates = @()
if (-not [string]::IsNullOrWhiteSpace($PythonExe)) {
    if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
        throw "Python executable does not exist: $PythonExe"
    }
    $Candidates += [pscustomobject]@{ Executable = [IO.Path]::GetFullPath($PythonExe); UseLauncher = $false }
}
else {
    $PythonCommand = Get-Command "python.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $PythonCommand) {
        $Candidates += [pscustomobject]@{ Executable = $PythonCommand.Source; UseLauncher = $false }
    }
    $PyCommand = Get-Command "py.exe" -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $PyCommand) {
        $Candidates += [pscustomobject]@{ Executable = $PyCommand.Source; UseLauncher = $true }
    }
}

$Probe = $null
foreach ($Candidate in $Candidates) {
    $Probe = Invoke-PythonProbe -Executable ([string]$Candidate.Executable) `
        -UseLauncher ([bool]$Candidate.UseLauncher)
    if ($null -ne $Probe) {
        break
    }
}
if ($null -eq $Probe) {
    throw "Python 3.10 or newer with tkinter and pythonw.exe was not found."
}
$PythonW = [IO.Path]::GetFullPath([string]$Probe.pythonw)

if (-not (Test-Path -LiteralPath $MenuDir)) {
    New-Item -ItemType Directory -Path $MenuDir | Out-Null
}
foreach ($Spec in $Specs) {
    $Shortcut = $Shell.CreateShortcut($Spec.Path)
    $Shortcut.TargetPath = $PythonW
    $Shortcut.Arguments = '-I "' + $Launcher + '"' + $(if ($Spec.Check) { " --check" } else { "" })
    $Shortcut.WorkingDirectory = $RepoRoot
    $Shortcut.IconLocation = "$PythonW,0"
    $Shortcut.Description = $Marker
    $Shortcut.Save()
}
Write-Output "Context Relay shortcuts installed for the current user."
