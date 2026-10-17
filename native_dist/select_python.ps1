# Read-only discovery; also works before Python or project packages exist.
$ErrorActionPreference = 'Stop'
$candidates = [System.Collections.Generic.List[string]]::new()
if ($env:MULTIOMICS_PYTHON) { $candidates.Add($env:MULTIOMICS_PYTHON) }
$candidates.Add((Join-Path (Split-Path $PSScriptRoot) '.venv\Scripts\python.exe'))
$launcher = Get-Command py -ErrorAction SilentlyContinue
if ($launcher) {
    foreach ($line in (& $launcher.Source -0p 2>$null)) {
        if ($line -match '([A-Za-z]:\\.*python(?:\d*)\.exe)\s*$') { $candidates.Add($Matches[1]) }
    }
}
foreach ($name in @('python3.13','python3.14','python3.12','python3','python')) {
    $command = Get-Command $name -ErrorAction SilentlyContinue
    if ($command) { $candidates.Add($command.Source) }
}
foreach ($hive in @('HKCU:\Software\Python\PythonCore','HKLM:\Software\Python\PythonCore')) {
    if (Test-Path $hive) {
        foreach ($version in (Get-ChildItem $hive)) {
            $key = Join-Path $version.PSPath 'InstallPath'
            if (Test-Path $key) {
                $props = Get-ItemProperty $key
                if ($props.ExecutablePath) { $candidates.Add($props.ExecutablePath) }
                elseif ($props.'(default)') { $candidates.Add((Join-Path $props.'(default)' 'python.exe')) }
            }
        }
    }
}
foreach ($base in @("$env:LOCALAPPDATA\Programs\Python",$env:ProgramFiles)) {
    if (Test-Path $base) {
        foreach ($file in (Get-ChildItem "$base\Python*\python.exe" -ErrorAction SilentlyContinue)) {
            $candidates.Add($file.FullName)
        }
    }
}
foreach ($path in ($candidates | Select-Object -Unique)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    if ($path -like '*\WindowsApps\*') { continue }
    try {
        $info = [System.Diagnostics.ProcessStartInfo]::new()
        $info.FileName = $path
        $info.Arguments = '-B -c "import sys;sys.exit(0 if (3,12)<=sys.version_info[:2]<(3,15) else 1)"'
        $info.UseShellExecute = $false
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $process = [System.Diagnostics.Process]::Start($info)
        if (-not $process.WaitForExit(15000)) { $process.Kill(); continue }
        if ($process.ExitCode -eq 0) { Write-Output $path; exit 0 }
    } catch { continue }
}
Write-Error '未发现兼容 Python，请阅读 AGENT_INSTALL_PROMPT_zh.md。'
exit 1
