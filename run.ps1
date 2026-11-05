# Starts DocMind on Windows. Run it with: .\run.ps1
# Packages are only installed when the virtual environment is new or requirements.txt changed.
$ErrorActionPreference = "Stop"

# always work from the folder this script lives in
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ActivateScript = Join-Path $ProjectRoot ".venv\Scripts\Activate.ps1"

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating Python virtual environment..." -ForegroundColor Cyan
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv .venv
    } else {
        python -m venv .venv
    }
}

if (-not (Test-Path $VenvPython)) {
    throw "Could not create the virtual environment. Install Python 3 and try again."
}

. $ActivateScript

# install packages on the first run, or when requirements.txt has changed
$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"
$InstallMarker = Join-Path $ProjectRoot ".venv\requirements-installed.sha256"
$CurrentHash = (Get-FileHash $RequirementsFile -Algorithm SHA256).Hash
$PreviousHash = if (Test-Path $InstallMarker) { (Get-Content $InstallMarker -Raw).Trim() } else { "" }

if ($CurrentHash -ne $PreviousHash) {
    Write-Host "Installing project dependencies (first run or requirements changed)..." -ForegroundColor Cyan
    python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. Fix the pip error and run .\run.ps1 again."
    }
    Set-Content -Path $InstallMarker -Value $CurrentHash -NoNewline
} else {
    Write-Host "Dependencies already installed. Skipping pip installation." -ForegroundColor Green
}

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. You can edit it if needed." -ForegroundColor Yellow
}

# make sure Ollama is installed and pull any model that is missing
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Warning "Ollama is not installed or is not on PATH. Install it from https://ollama.com/download."
} else {
    $models = ollama list 2>$null | Out-String
    foreach ($model in @("llama3.2", "nomic-embed-text")) {
        if ($models -notmatch [regex]::Escape($model)) {
            Write-Host "Downloading missing Ollama model: $model" -ForegroundColor Cyan
            ollama pull $model
            if ($LASTEXITCODE -ne 0) { throw "Could not download Ollama model $model." }
        }
    }
}

Write-Host "Starting DocMind at http://localhost:8000 (API docs: /docs)" -ForegroundColor Green
python -m uvicorn app.main:app --reload --port 8000
