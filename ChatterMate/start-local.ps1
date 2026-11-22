$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

function Wait-Ready($description, [scriptblock]$probe, $seconds = 90) {
    $deadline = (Get-Date).AddSeconds($seconds)
    do {
        if (& $probe) { return }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "Timed out waiting for $description"
}

$agent = Split-Path $PSScriptRoot
if (!(Test-Path -LiteralPath (Join-Path $agent 'app/contract_bridge.py'))) {
    $agent = Join-Path $agent 'analysis-agent'
}
$python = Join-Path $agent '.venv/Scripts/python.exe'
$runtime = Join-Path $agent 'runtime'
$data = Join-Path $env:LOCALAPPDATA 'analysis-agent/payments-demo-data'
$mysql = 'C:/Program Files/MySQL/MySQL Server 8.4/bin/mysqld.exe'
$composeFiles = @('-f', 'docker-compose.yml', '-f', 'docker-compose.override.yml', '-f', 'compose.images-local.yml')
$config = Get-Content -LiteralPath 'backend/.env' -Raw
$token = [regex]::Match($config, '(?m)^ANALYSIS_AGENT_BRIDGE_TOKEN=([a-f0-9]{64})\s*$').Groups[1].Value
$bridgeEnvironment = @{
    ANALYSIS_AGENT_BRIDGE_TOKEN = $token
    ANALYSIS_AGENT_BRIDGE_SOURCE = 'payment_demo'
}
foreach ($setting in 'ANALYSIS_AGENT_BRIDGE_DIRECTORY', 'ANALYSIS_AGENT_BRIDGE_QUERY_SOURCE') {
    $configuredValue = [regex]::Match($config, "(?m)^${setting}=([^\r\n]+)").Groups[1].Value.Trim()
    if ($configuredValue) { $bridgeEnvironment[$setting] = $configuredValue }
}
if (!$token -or $config -notmatch '(?m)^ANALYSIS_AGENT_BRIDGE_ORG_ID=[a-f0-9-]{36}\s*$') {
    throw 'Configure the bridge token and organization in backend/.env first.'
}

docker version --format '{{.Server.Version}}' 2>$null | Out-Null
if ($LASTEXITCODE) {
    docker desktop start --timeout 55
    if ($LASTEXITCODE) { throw 'Docker Desktop failed to start.' }
}
if (!(Get-NetTCPConnection -State Listen -LocalPort 33316 -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $mysql -ArgumentList @('--no-defaults', ('--datadir="{0}"' -f $data), '--mysqlx=OFF', '--bind-address=127.0.0.1', '--port=33316', '--skip-name-resolve') -WindowStyle Hidden
}
Wait-Ready 'demo MySQL' { [bool](Get-NetTCPConnection -State Listen -LocalPort 33316 -ErrorAction SilentlyContinue) } 30
if (!(Get-NetTCPConnection -State Listen -LocalPort 8015 -ErrorAction SilentlyContinue)) {
    Start-Process -FilePath $python -WorkingDirectory $agent -ArgumentList @('-m','uvicorn','app.contract_bridge:app','--host','127.0.0.1','--port','8015') -Environment $bridgeEnvironment -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'contract-bridge-local.out.log') -RedirectStandardError (Join-Path $runtime 'contract-bridge-local.err.log')
}
docker compose @composeFiles up -d --wait --wait-timeout 120
if ($LASTEXITCODE) { throw 'ChatterMate failed to start.' }
Wait-Ready 'ChatterMate website' {
    curl.exe --silent --fail --output NUL --max-time 4 http://localhost:3000/
    $LASTEXITCODE -eq 0
}
$check = @'
import os, urllib.request, urllib.error
request = urllib.request.Request(
    os.environ["ANALYSIS_AGENT_BRIDGE_URL"] + "/investigations/00000000-0000-0000-0000-000000000000",
    headers={"Authorization": "Bearer " + os.environ["ANALYSIS_AGENT_BRIDGE_TOKEN"]},
)
try:
    urllib.request.urlopen(request, timeout=5)
except urllib.error.HTTPError as error:
    if error.code == 404:
        raise SystemExit(0)
    raise
raise SystemExit("Unexpected response to the bridge readiness check")
'@
Wait-Ready 'authenticated analysis bridge' {
    $check | docker exec -i chattermate-backend-1 python - 2>$null | Out-Null
    $LASTEXITCODE -eq 0
}
Write-Host 'ChatterMate is ready at http://localhost:3000/workbench'
