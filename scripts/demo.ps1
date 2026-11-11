<#
.SYNOPSIS
  End-to-end demo without Docker: install, run the tests, start the API, replay the
  datasets, query a cow and generate a report.

.EXAMPLE
  .\scripts\demo.ps1 -DataDir C:\Users\me\Downloads\cow_data             # June-July 2024, ~5 min
  .\scripts\demo.ps1 -DataDir C:\Users\me\Downloads\cow_data -Since ""   # full replay (1.5-2.5 h)

  By default only readings from 2024-06-01 are replayed (~31k requests): enough for the
  30-day window and the illness baseline of the 2024-07-10 report.
#>
param(
    [Parameter(Mandatory = $true)][string]$DataDir,
    [string]$ReportDate = "2024-07-10",
    [int]$Port = 8000,
    [string]$Since = "2024-06-01",
    [int]$Limit = 0,
    [int]$Concurrency = 16,
    [switch]$SkipTests
)
# Native tools log to stderr; with "Stop", Windows PowerShell 5.1 would turn every log
# line into a terminating error. Exit codes are checked explicitly instead.
$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Invoke-Step([string]$Title, [scriptblock]$Command) {
    Write-Host "`n== $Title ==" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "step failed: $Title (exit code $LASTEXITCODE)" }
}

if (-not (Test-Path (Join-Path $DataDir "measurements.parquet"))) {
    throw "measurements.parquet not found in $DataDir"
}

if (-not (Test-Path ".venv")) { Invoke-Step "Creating virtual environment" { python -m venv .venv } }
$py = Join-Path $root ".venv\Scripts\python.exe"
Invoke-Step "Installing" { & $py -m pip install --quiet --disable-pip-version-check -e ".[dev]" }
if (-not $SkipTests) { Invoke-Step "Tests" { & $py -m pytest } }

# Fresh demo database. NORMAL sync is ~10x faster for a bulk replay and still loses
# nothing on an application restart (only a power cut could lose the last commits).
$env:COWSHED_DATABASE_URL = "sqlite:///./data/demo.db"
$env:COWSHED_SQLITE_SYNCHRONOUS = "NORMAL"
New-Item -ItemType Directory -Force ".\data" | Out-Null
Remove-Item -Force -ErrorAction SilentlyContinue ".\data\demo.db", ".\data\demo.db-wal", ".\data\demo.db-shm"

Write-Host "`n== Starting API on http://127.0.0.1:$Port (docs: /docs) ==" -ForegroundColor Cyan
$api = Start-Process -FilePath $py -ArgumentList "-m", "cowshed.cli", "serve", "--port", $Port `
    -PassThru -WindowStyle Hidden -RedirectStandardError ".\data\demo-api.log"
try {
    $ready = $false
    for ($i = 0; $i -lt 60 -and -not $ready; $i++) {
        try { Invoke-RestMethod "http://127.0.0.1:$Port/health" | Out-Null; $ready = $true } catch { Start-Sleep 1 }
    }
    if (-not $ready) { throw "API did not start, see data\demo-api.log" }

    $simArgs = @("-m", "cowshed.cli", "simulate", "--data-dir", $DataDir, "--api-url", "http://127.0.0.1:$Port",
                 "--concurrency", $Concurrency)
    if ($Since) { $simArgs += @("--since", $Since) }
    if ($Limit -gt 0) { $simArgs += @("--limit", $Limit) }
    Invoke-Step "Replaying datasets (one request per record)" { & $py @simArgs }

    Write-Host "`n== GET /cows/{id} ==" -ForegroundColor Cyan
    Invoke-RestMethod "http://127.0.0.1:$Port/cows/33eb52d5-db1e-4a68-a911-f78574f12254" | ConvertTo-Json -Depth 5

    Invoke-Step "Daily report $ReportDate (files)" { & $py -m cowshed.cli report --date $ReportDate --publish reports }
    Invoke-Step "Daily report $ReportDate" { & $py -m cowshed.cli report --date $ReportDate }
}
finally {
    Stop-Process -Id $api.Id -Force -ErrorAction SilentlyContinue
}
