# Shared settings for the local Windows test scripts. Dot-sourced by the others.
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest downloads much faster

$EsVersion   = "8.17.1"
$EsHome      = Join-Path $env:USERPROFILE "es-local"                  # outside Desktop/OneDrive
$EsDir       = Join-Path $EsHome "elasticsearch-$EsVersion"
$EsUrl       = "http://127.0.0.1:9200"
$EsUser      = "elastic"
$EsPassword  = "changeme123"        # local test only
$SvcUser     = "config_api"
$SvcPassword = "svc-pass-123"       # local test only
$ProjectDir  = Split-Path -Parent $PSScriptRoot
$ApiPort     = 8080
$AdminUser   = "latish.madapada@fenixcommerce.com"
$AdminPassword = "Local-Test-Admin-2026"   # local test only; change it at first sign-in

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
