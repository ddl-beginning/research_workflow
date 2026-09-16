param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$productRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path

& $Python -m pip install -e $productRoot
if ($LASTEXITCODE -ne 0) {
    throw "Workflow installation failed."
}

$pythonExecutable = (& $Python -c "import sys; print(sys.executable)").Trim()
$scriptsDirectory = (& $Python -c "import sysconfig; print(sysconfig.get_path('scripts') or '')").Trim()
if ([string]::IsNullOrWhiteSpace($scriptsDirectory)) {
    $scriptsDirectory = Split-Path -Parent $pythonExecutable
}
$generatedLauncher = Join-Path $scriptsDirectory "workflow.exe"
if (-not (Test-Path -LiteralPath $generatedLauncher -PathType Leaf)) {
    throw "The Python package installed successfully but did not create workflow.exe at $generatedLauncher."
}

$localAppData = [Environment]::GetEnvironmentVariable("LOCALAPPDATA")
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
}
if ([string]::IsNullOrWhiteSpace($localAppData)) {
    throw "LOCALAPPDATA is not available; cannot create the stable Workflow launcher directory."
}
$launcherDirectory = [IO.Path]::GetFullPath((Join-Path $localAppData "ResearchWorkflow\bin"))
New-Item -ItemType Directory -Path $launcherDirectory -Force | Out-Null
$stableLauncher = Join-Path $launcherDirectory "workflow.exe"
Copy-Item -LiteralPath $generatedLauncher -Destination $stableLauncher -Force

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
$pathEntries = @(
    @($userPath -split ";") |
        ForEach-Object { $_.Trim() } |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
)
$launcherAlreadyPresent = $pathEntries | Where-Object {
    try { [IO.Path]::GetFullPath($_).TrimEnd("\") -ieq $launcherDirectory.TrimEnd("\") } catch { $_ -ieq $launcherDirectory }
}
if (-not $launcherAlreadyPresent) {
    $pathEntries += $launcherDirectory
    [Environment]::SetEnvironmentVariable("Path", ($pathEntries -join ";"), "User")
}

# Make the current installer process useful too; a new PowerShell remains the
# supported validation boundary because it reads the persisted user PATH.
$currentPathEntries = @($env:Path -split ";" | Where-Object { $_ })
if (-not ($currentPathEntries | Where-Object { $_.TrimEnd("\") -ieq $launcherDirectory.TrimEnd("\") })) {
    $env:Path = "$launcherDirectory;$env:Path"
}

Write-Host "Workflow installed."
Write-Host "WORKFLOW_LAUNCHER: $stableLauncher"
Write-Host "WORKFLOW_USER_PATH: $launcherDirectory"
Write-Host "Open a new PowerShell, then run:"
Write-Host "  Get-Command workflow.exe"
Write-Host "  workflow.exe --version"
Write-Host "  workflow.exe setup"
