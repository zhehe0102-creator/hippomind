$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $PSScriptRoot
$Python = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
$PythonArgs = if ($Python -eq "py") { @("-3") } else { @() }
$Venv = Join-Path $ProjectDir ".venv"
& $Python @PythonArgs -m venv $Venv
& (Join-Path $Venv "Scripts/python.exe") -m pip install --upgrade pip
& (Join-Path $Venv "Scripts/python.exe") -m pip install -r (Join-Path $ProjectDir "requirements.txt")
& (Join-Path $Venv "Scripts/python.exe") -m pip install -e $ProjectDir
Push-Location $ProjectDir
& (Join-Path $Venv "Scripts/python.exe") -m hippomind init
Pop-Location
Write-Host "Installed. Run: $Venv\Scripts\python.exe -m hippomind start"
