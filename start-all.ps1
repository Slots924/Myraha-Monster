$taskProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskEnvPath = Join-Path $taskProjectRoot ".env"
$taskNgrokConfigPath = Join-Path $taskProjectRoot "ngrok.yml"

if (-not (Test-Path -LiteralPath $taskEnvPath)) {
    throw ".env was not found: $taskEnvPath"
}

$taskConfig = @{}
foreach ($taskLine in Get-Content -LiteralPath $taskEnvPath -Encoding UTF8) {
    $taskTrimmedLine = $taskLine.Trim()
    if (-not $taskTrimmedLine -or $taskTrimmedLine.StartsWith("#") -or -not $taskTrimmedLine.Contains("=")) {
        continue
    }
    $taskParts = $taskTrimmedLine.Split("=", 2)
    $taskConfig[$taskParts[0].Trim()] = $taskParts[1].Trim().Trim('"').Trim("'")
}

$taskNgrokPath = $taskConfig["NGROK_PATH"]
$taskNgrokToken = $taskConfig["NGROK_AUTHTOKEN"]
if (-not $taskNgrokToken) {
    $taskNgrokToken = $taskConfig["NGROK_TOKEN"]
}

if (-not $taskNgrokPath -or -not (Test-Path -LiteralPath $taskNgrokPath -PathType Leaf)) {
    throw "NGROK_PATH is missing or ngrok.exe was not found."
}
if (-not $taskNgrokToken) {
    throw "Add NGROK_TOKEN or NGROK_AUTHTOKEN to .env."
}

$env:NGROK_AUTHTOKEN = $taskNgrokToken
$taskNgrokArguments = "start facebook-webhook --config `"$taskNgrokConfigPath`""
$taskNgrokProcess = Start-Process -FilePath $taskNgrokPath `
    -ArgumentList $taskNgrokArguments `
    -WorkingDirectory $taskProjectRoot -WindowStyle Normal -PassThru

Write-Host "Myraha Monster is starting: http://localhost" -ForegroundColor Green
Write-Host "ngrok is running in a separate window." -ForegroundColor DarkGray

try {
    Push-Location $taskProjectRoot
    & python index.py
}
finally {
    Pop-Location
    if ($taskNgrokProcess -and -not $taskNgrokProcess.HasExited) {
        Stop-Process -Id $taskNgrokProcess.Id
    }
}
