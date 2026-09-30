# Step 1: install (first run only) and start Elasticsearch 8.17.1 locally.
# Leaves Elasticsearch running in its own window; close that window to stop it.
. "$PSScriptRoot\common.ps1"

if (Test-EsUp) {
    $v = (Invoke-Es GET "/").version.number
    Write-Ok "Elasticsearch $v is already running at $EsUrl"
} else {
    if (-not (Test-Path "$EsDir\bin\elasticsearch.bat")) {
        Write-Step "Elasticsearch $EsVersion not found in $EsHome - installing"
        New-Item -ItemType Directory -Force -Path $EsHome | Out-Null
        $zip = Join-Path $EsHome "elasticsearch-$EsVersion-windows-x86_64.zip"
        $base = "https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-$EsVersion-windows-x86_64.zip"
        if (-not (Test-Path $zip)) {
            Write-Host "    Downloading (~480 MB, a few minutes)..."
            Invoke-WebRequest -Uri $base -OutFile "$zip.part" -UseBasicParsing
            Move-Item "$zip.part" $zip
        }
        Write-Host "    Verifying checksum..."
        $expected = ((Invoke-WebRequest -Uri "$base.sha512" -UseBasicParsing).Content -split "\s+")[0]
        $actual = (Get-FileHash -Algorithm SHA512 $zip).Hash.ToLower()
        if ($expected.ToLower() -ne $actual) { Remove-Item $zip; throw "Checksum mismatch - download removed, run again" }
        Write-Ok "Checksum matches"
        Write-Host "    Extracting..."
        tar.exe -xf $zip -C $EsHome
        if ($LASTEXITCODE -ne 0) { Expand-Archive -Path $zip -DestinationPath $EsHome -Force }

        $data = ($EsHome -replace "\\", "/") + "/data"
        $logs = ($EsHome -replace "\\", "/") + "/logs"
        @"
cluster.name: local-test
node.name: node-1
discovery.type: single-node
network.host: 127.0.0.1
http.port: 9200
xpack.security.enabled: true
xpack.security.http.ssl.enabled: false
xpack.security.transport.ssl.enabled: false
xpack.security.enrollment.enabled: false
xpack.ml.enabled: false
path.data: $data
path.logs: $logs
"@ | Set-Content -Encoding ascii "$EsDir\config\elasticsearch.yml"
        "-Xms1g`r`n-Xmx1g" | Set-Content -Encoding ascii "$EsDir\config\jvm.options.d\heap.options"

        Write-Host "    Setting the 'elastic' password..."
        $EsPassword | & "$EsDir\bin\elasticsearch-keystore.bat" add -x -f bootstrap.password
        if ($LASTEXITCODE -ne 0) { throw "Could not set the bootstrap password" }
        Write-Ok "Installed to $EsDir"
    }

    Write-Step "Starting Elasticsearch in a new window (close that window to stop it)"
    $env:ES_JAVA_OPTS = ""
    Start-Process -FilePath "$EsDir\bin\elasticsearch.bat" -WorkingDirectory $EsDir -WindowStyle Minimized
    $deadline = (Get-Date).AddSeconds(180)
    while (-not (Test-EsUp)) {
        if ((Get-Date) -gt $deadline) { throw "Elasticsearch did not start within 3 minutes - check the Elasticsearch window / $EsHome\logs" }
        Start-Sleep -Seconds 3; Write-Host -NoNewline "."
    }
    Write-Host ""
    Write-Ok "Elasticsearch $((Invoke-Es GET '/').version.number) is up at $EsUrl"
}

Write-Step "Creating the API's service account (safe to repeat)"
Invoke-Es PUT "/_security/role/config_api_writer" @{
    cluster = @("monitor", "manage", "manage_ilm", "manage_index_templates", "manage_pipeline")
    indices = @(@{ names = @("*"); privileges = @("monitor", "view_index_metadata", "manage", "read", "write") })
} | Out-Null
Invoke-Es POST "/_security/user/$SvcUser" @{ password = $SvcPassword; roles = @("config_api_writer") } | Out-Null
# Older versions of this script created two demo users, 'config_api' and 'reader': remove them
foreach ($old in @("config_api", "reader")) {
    try { Invoke-Es DELETE "/_security/user/$old" | Out-Null; Write-Ok "Removed the old demo user '$old'" } catch {}
}
try { Invoke-Es DELETE "/_security/role/config_reader" | Out-Null } catch {}
Write-Ok "$SvcUser (the API's account) is ready; passwords are in $PasswordsFile"

Write-Step "Creating demo data (safe to repeat)"
try { Invoke-Es GET "/products-demo" | Out-Null } catch {
    Invoke-Es PUT "/products-demo" @{
        settings = @{ number_of_replicas = 0 }
        mappings = @{ properties = @{ name = @{ type = "text" }; sku = @{ type = "keyword" }; price = @{ type = "float" } } }
    } | Out-Null
}
Invoke-Es PUT "/_ilm/policy/demo-logs-policy" @{ policy = @{ phases = @{ hot = @{ actions = @{ rollover = @{ max_age = "7d" } } } } } } | Out-Null
Invoke-Es PUT "/_ingest/pipeline/demo-pipeline" @{ description = "demo"; processors = @(@{ set = @{ field = "env"; value = "local" } }) } | Out-Null
Write-Ok "index products-demo, ILM policy demo-logs-policy, ingest pipeline demo-pipeline"

Write-Host "`nNext: run  local-test\2-start-api.cmd" -ForegroundColor White
