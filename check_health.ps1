# Lee las variables del .env (base + override por hostname, igual que env_loader.py)
function Read-EnvFile($path) {
    $vars = @{}
    if (Test-Path $path) {
        Get-Content $path | ForEach-Object {
            if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)\s*$') {
                $vars[$Matches[1]] = $Matches[2]
            }
        }
    }
    return $vars
}

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$env_base     = Join-Path $scriptDir ".env"
$env_host     = Join-Path $scriptDir (".env." + $env:COMPUTERNAME)

$vars = Read-EnvFile $env_base
foreach ($kv in (Read-EnvFile $env_host).GetEnumerator()) { $vars[$kv.Key] = $kv.Value }

$apiUrl = $vars["API_URL"]
if (-not $apiUrl) {
    Write-Output "ERROR - API_URL no encontrada en .env - $(Get-Date)"
    exit 1
}

$healthUrl = $apiUrl.TrimEnd("/") + "/api/docs/health"

try {
    $response = Invoke-WebRequest `
        -Uri $healthUrl `
        -UseBasicParsing `
        -TimeoutSec 30

    Write-Output "OK - $(Get-Date)"
}
catch {
    Write-Output "ERROR - $(Get-Date)"
}

exit 0