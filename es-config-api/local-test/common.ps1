# Shared settings for the local Windows test scripts. Dot-sourced by the others.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest downloads much faster

$EsVersion   = "8.17.1"
$EsHome      = Join-Path $env:USERPROFILE "es-local"                  # outside Desktop/OneDrive
$EsDir       = Join-Path $EsHome "elasticsearch-$EsVersion"
$EsUrl       = "http://127.0.0.1:9200"
$EsUser      = "elastic"
$SvcUser     = "es_console_api"      # the API's own Elasticsearch account (no other users are created)
$ProjectDir  = Split-Path -Parent $PSScriptRoot
$ApiPort     = 8080
$AdminUser   = "latish.madapada@fenixcommerce.com"

# Passwords are generated on the first run (nothing is hard-coded) and kept in
# local-test\.secrets\local-passwords.json, which is never committed.
$SecretsDir    = Join-Path $PSScriptRoot ".secrets"
$PasswordsFile = Join-Path $SecretsDir "local-passwords.json"

function New-Password([int]$Length = 24) {
    $chars = [char[]]"ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    $bytes = New-Object byte[] $Length
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

if (Test-Path $PasswordsFile) {
    $Passwords = Get-Content -Raw $PasswordsFile | ConvertFrom-Json
} else {
    New-Item -ItemType Directory -Force -Path $SecretsDir | Out-Null
    # An Elasticsearch installed by an older version of these scripts keeps its old 'elastic'
    # password; delete %USERPROFILE%\es-local for a completely fresh install.
    $oldInstall = Test-Path (Join-Path $EsDir "bin\elasticsearch.bat")
    $Passwords = [pscustomobject]@{
        elastic = $(if ($oldInstall) { "changeme123" } else { New-Password })
        service = New-Password
        admin   = New-Password
    }
    $Passwords | ConvertTo-Json | Set-Content -Encoding ascii $PasswordsFile
}
$EsPassword    = $Passwords.elastic
$SvcPassword   = $Passwords.service
$AdminPassword = $Passwords.admin    # first console password; change it at first sign-in

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Ok($msg)   { Write-Host "    OK  $msg" -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "    !!  $msg" -ForegroundColor Yellow }

function Get-EsAuthHeader {
    $pair = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${EsUser}:${EsPassword}"))
    return @{ Authorization = "Basic $pair" }
}

function Invoke-Es($Method, $Path, $Body = $null) {
    $params = @{ Method = $Method; Uri = "$EsUrl$Path"; Headers = (Get-EsAuthHeader);
               ContentType = "application/json"; UseBasicParsing = $true }
    if ($null -ne $Body) { $params.Body = ($Body | ConvertTo-Json -Depth 20 -Compress) }
    return Invoke-RestMethod @params
}

function Test-EsUp {
    try { Invoke-Es GET "/" | Out-Null; return $true } catch { return $false }
}

function Find-Python {
    foreach ($cmd in @("py -3.12", "py -3.11", "py -3.10", "python", "python3")) {
        $exe, $rest = $cmd.Split(" ", 2)
        if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) { continue }
        try {
            $v = & $exe @($rest | Where-Object { $_ }) -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$v -ge [version]"3.10") { return $cmd }
        } catch {}
    }
    return $null
}
