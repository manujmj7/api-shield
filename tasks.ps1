# PowerShell automation tasks for API Shield

param (
    [Parameter(Position=0)]
    [string]$Target = "help"
)

$Python = ".\.venv\Scripts\python.exe"
$Pytest = ".\.venv\Scripts\pytest.exe"
$Ruff   = ".\.venv\Scripts\ruff.exe"

switch ($Target.ToLower()) {
    "install" {
        Write-Host "Creating virtual environment and installing dependencies..." -ForegroundColor Cyan
        if (-not (Test-Path ".venv")) {
            python -m venv .venv
        }
        & $Python -m pip install --upgrade pip
        & $Python -m pip install -r requirements-dev.txt
        Write-Host "Dependencies successfully installed!" -ForegroundColor Green
    }
    "run" {
        Write-Host "Starting API Shield application..." -ForegroundColor Cyan
        & $Python app.py
    }
    "test" {
        Write-Host "Running unit and concurrency test suite with coverage..." -ForegroundColor Cyan
        & $Pytest --cov=. --cov-report=term-missing
    }
    "lint" {
        Write-Host "Running Ruff linting and formatting check..." -ForegroundColor Cyan
        & $Ruff check .
        & $Ruff format --check .
    }
    "format" {
        Write-Host "Auto-formatting code with Ruff..." -ForegroundColor Cyan
        & $Ruff check --fix .
        & $Ruff format .
    }
    "demo" {
        Write-Host "Running end-to-end verification demo script..." -ForegroundColor Cyan
        & $Python scripts/demo.py
    }
    default {
        Write-Host "API Shield Task Runner Usage:" -ForegroundColor Yellow
        Write-Host "  .\tasks.ps1 install   - Install runtime and dev dependencies in .venv"
        Write-Host "  .\tasks.ps1 run       - Start API Shield Flask server"
        Write-Host "  .\tasks.ps1 test      - Run full test suite with coverage report"
        Write-Host "  .\tasks.ps1 lint      - Run Ruff linter and formatter verification"
        Write-Host "  .\tasks.ps1 format    - Auto-fix linting and formatting"
        Write-Host "  .\tasks.ps1 demo      - Execute end-to-end verification demo against live server"
    }
}
