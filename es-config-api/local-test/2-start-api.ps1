# Step 2: set up Python dependencies (first run only) and start the API on http://localhost:8080
# Storage: local JSON files in local-test\.store by default. Run with -S3 to use S3 instead
# (credentials from the AWS profile given by -AwsProfile, bucket/prefix from -Bucket / -Prefix).
param(
    [switch]$S3,
    [string]$Bucket = "fenix-ecr-logs",
    [string]$Prefix = "cron-migration/",
    [string]$AwsProfile = "fenix-prod",
    [string]$Region = "us-west-2"
)
. "$PSScriptRoot\common.ps1"

if (-not (Test-EsUp)) { throw "Elasticsearch is not running - run local-test\1-start-elasticsearch.cmd first" }

$venv = Join-Path $ProjectDir ".venv"
$py = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Step "Creating a Python virtual environment in $venv"
    $found = Find-Python
    if (-not $found) {
        Write-Warn2 "Python 3.10+ not found."
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host "    Installing Python 3.12 with winget..."
            winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
            Write-Warn2 "Python installed. Close this window, open a new Command Prompt and run this script again."
            exit 1
        }
        throw "Install Python 3.12 from https://www.python.org/downloads/ (tick 'Add to PATH'), then run again"
    }
    $exe, $rest = $found.Split(" ", 2)
    & $exe @($rest | Where-Object { $_ }) -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment" }
}
Write-Step "Installing / checking Python packages"
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $ProjectDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed (a corporate proxy may need PIP_INDEX_URL / PIP_CERT)" }
Write-Ok "Packages ready"

$clusters = Join-Path $PSScriptRoot "clusters.local.yaml"
@"
clusters:
  - id: local
    name: Local Elasticsearch $EsVersion
    description: Single node on this PC for testing
    url: $EsUrl
    auth:
      type: basic
      username: $SvcUser
      password: `${ES_LOCAL_PASSWORD}
"@ | Set-Content -Encoding ascii $clusters

$env:CLUSTERS_FILE = $clusters
$env:ES_LOCAL_PASSWORD = $SvcPassword
$env:BOOTSTRAP_ADMINS = $AdminUser
$env:PYTHONDONTWRITEBYTECODE = "1"
if ($S3) {
    $env:STORAGE_BACKEND = "s3"; $env:S3_BUCKET = $Bucket; $env:S3_PREFIX = $Prefix
    $env:AWS_PROFILE = $AwsProfile; $env:AWS_REGION = $Region
    $where = "s3://$Bucket/$Prefix (profile $AwsProfile)"
} else {
    $env:STORAGE_BACKEND = "local"; $env:LOCAL_STORE_DIR = Join-Path $PSScriptRoot ".store"
    $where = $env:LOCAL_STORE_DIR
}

Write-Step "Starting the API"
Write-Host "    Storage : $where"
Write-Host "    Admin   : $AdminUser"
Write-Host "    Open    : http://localhost:$ApiPort/docs   (Ctrl+C here to stop)" -ForegroundColor White
Set-Location $ProjectDir
& $py -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port $ApiPort
