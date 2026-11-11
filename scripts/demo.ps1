<#
.SYNOPSIS
  End-to-end demo without Docker: install, run the tests, start the API, replay the
  datasets, generate a report.

.EXAMPLE
  .\scripts\demo.ps1 -DataDir C:\Users\me\Downloads\cow_data -ReportDate 2024-07-10
  .\scripts\demo.ps1 -DataDir C:\data\cow_data -Limit 20000     # quick partial replay
#>
param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$ReportDate = "2024-07-10",
    [int]$Port = 8000,
    [int]$Limit = 0,
    [int]$Concurrency = 16
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if (-not (Test-Path ".venv")) { python -m venv .venv }
$py = Join-Path $root ".venv\Scripts\python.exe"
& $py -m pip install --quiet --upgrade pip
& $py -m pip install --quiet -e ".[dev]"

Write-Host "`n== Tests ==" -ForegroundColor Cyan
& $py -m pytest
if ($LASTEXITCODE -ne 0) { throw "tests failed" }

# Bulk replay: NORMAL sync is ~10x faster and still loses nothing on an application restart.
$env:COWSHED_DATABASE_URL = "sqlite:///./data/cowshed.db"
$env:COWSHED_SQLITE_SYNCHRONOUS = "NORMAL"

Write-Host "`n== Starting API on port $Port ==" -ForegroundColor Cyan
$api = Start-Process -FilePath $py -ArgumentList "-m", "cowshed.cli", "serve", "--port", $Port -PassThru -WindowStyle Hidden
try {
    for ($i = 0; $i -lt 30; $i++) {
        try { Invoke-RestMethod "http://127.0.0.1:$Port/health" | Out-Null; break } catch { Start-Sleep 1 }
    }

    Write-Host "`n== Replaying datasets (one request per record) ==" -ForegroundColor Cyan
    $simArgs = @("-m", "cowshed.cli", "simulate", "--data-dir", $DataDir, "--api-url", "http://127.0.0.1:$Port",
                 "--concurrency", $Concurrency)
    if ($Limit -gt 0) { $simArgs += @("--limit", $Limit) }
    & $py @simArgs

    Write-Host "`n== GET /cows/{id} ==" -ForegroundColor Cyan
    Invoke-RestMethod "http://127.0.0.1:$Port/cows/33eb52d5-db1e-4a68-a911-f78574f12254" | ConvertTo-Json -Depth 5

    Write-Host "`n== Daily report $ReportDate ==" -ForegroundColor Cyan
    & $py -m cowshed.cli report --date $ReportDate --publish reports
    & $py -m cowshed.cli report --date $ReportDate
}
finally {
    Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
}
