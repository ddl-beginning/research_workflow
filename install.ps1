param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$productRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path

& $Python -m pip install -e $productRoot
if ($LASTEXITCODE -ne 0) {
    throw "Workflow installation failed."
}

Write-Host "Workflow installed. Open a new terminal, then run:"
Write-Host "  workflow.exe setup"
