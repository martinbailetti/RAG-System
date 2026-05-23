try {
    $response = Invoke-WebRequest `
        -Uri "https://smiapi.smi2000.net:6767/api/smidocs/health" `
        -UseBasicParsing `
        -TimeoutSec 30

    Write-Output "OK - $(Get-Date)"
}
catch {
    Write-Output "ERROR - $(Get-Date)"
}

exit 0