param([Nullable[int]]$Port = $null, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$previousPort = $env:BTC_UI_PORT
Push-Location $PSScriptRoot
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw 'Install and start Docker Desktop, then run this script again.'
    }
    docker info --format '{{.ServerVersion}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Start Docker Desktop and wait until its engine is ready.' }
    if ($null -ne $Port) {
        if ($Port -lt 1024 -or $Port -gt 65535) { throw 'Choose a port between 1024 and 65535.' }
        $env:BTC_UI_PORT = "$Port"
    }
    docker compose up --build --detach --wait --wait-timeout 180
    if ($LASTEXITCODE -ne 0) {
        throw 'Startup did not complete. Inspect docker compose ps and docker compose logs.'
    }
    $address = docker compose port dashboard 8765
    if ($LASTEXITCODE -ne 0) { throw 'Could not determine the dashboard port.' }
    $url = "http://$($address.Trim())/"
    Write-Host "Paper platform ready: $url"
    if (-not $NoBrowser) { Start-Process $url }
} finally { $env:BTC_UI_PORT = $previousPort; Pop-Location }
