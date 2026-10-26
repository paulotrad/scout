param(
    [switch]$SetGmailPassword
)

$ErrorActionPreference = 'Stop'
$scoutRoot = $PSScriptRoot
$runsDir = Join-Path $scoutRoot 'runs'
$passwordFile = Join-Path $runsDir 'gmail_smtp_password.xml'
New-Item -ItemType Directory -Path $runsDir -Force | Out-Null

if ($SetGmailPassword -or -not (Test-Path -LiteralPath $passwordFile)) {
    Write-Host 'Create an app password for fca.21050@gmail.com at https://myaccount.google.com/apppasswords'
    Write-Host 'Enter that app password here. It will not be printed or saved in Git.'
    $securePassword = Read-Host 'Gmail app password' -AsSecureString
    if ($securePassword.Length -eq 0) {
        throw 'No app password entered.'
    }
    $securePassword | Export-Clixml -LiteralPath $passwordFile
}
else {
    $securePassword = Import-Clixml -LiteralPath $passwordFile
}

$pythonCommand = Get-Command python -ErrorAction Stop
$pythonExe = $pythonCommand.Source
$credential = [System.Net.NetworkCredential]::new('', $securePassword)

$env:SCOUT_SMTP_HOST = 'smtp.gmail.com'
$env:SCOUT_SMTP_PORT = '587'
$env:SCOUT_SMTP_USER = 'fca.21050@gmail.com'
$env:SCOUT_SMTP_FROM = 'fca.21050@gmail.com'
$env:SCOUT_SMTP_PASSWORD = $credential.Password
$env:SCOUT_PUBLIC_HOST = 'app.thedeveloperpaul.com'
$env:SCOUT_PUBLIC_WRITES = '1'
$env:SCOUT_CF_TEAM_DOMAIN = 'sparkling-frost-1ea7.cloudflareaccess.com'
$env:SCOUT_CF_AUD = '9d46018cd7f9357ee641888bcc3b6a8e3cd6b1a11bed64821be951df9fa268ae'

try {
    & $pythonExe (Join-Path $scoutRoot 'marketplace_email.py') check
    if ($LASTEXITCODE -ne 0) {
        throw 'Gmail SMTP login failed. The running Scout server was not changed.'
    }

    $servers = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*marketplace_web.py --port 8765*' })
    if ($servers.Count -gt 0) {
        try {
            $state = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/scans' -TimeoutSec 5
        }
        catch {
            throw 'Could not confirm whether a scan is active. Scout was not restarted.'
        }
        if ($state.active_scan_id) {
            throw "Scan $($state.active_scan_id) is running. Restart after it finishes."
        }
        foreach ($server in $servers) {
            Stop-Process -Id $server.ProcessId
        }
        Start-Sleep -Seconds 2
    }

    $process = Start-Process -FilePath $pythonExe `
        -ArgumentList 'marketplace_web.py --port 8765' `
        -WorkingDirectory $scoutRoot -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $runsDir 'server.out.log') `
        -RedirectStandardError (Join-Path $runsDir 'server.err.log') `
        -PassThru
    Start-Sleep -Seconds 2
    if ($process.HasExited) {
        throw 'Scout exited during startup. Check runs/server.err.log.'
    }
    Write-Host 'Scout is running with Gmail match emails enabled.'
    Write-Host 'Only scan batches with new matching items create an email.'
}
finally {
    Remove-Item Env:SCOUT_SMTP_PASSWORD -ErrorAction SilentlyContinue
}
