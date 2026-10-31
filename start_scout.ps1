param(
    [switch]$SetGmailPassword,
    [switch]$EnsureRunning
)

$ErrorActionPreference = 'Stop'
$scoutRoot = $PSScriptRoot
$runsDir = Join-Path $scoutRoot 'runs'
$passwordFile = Join-Path $runsDir 'gmail_smtp_password.xml'
$unhealthySinceFile = Join-Path $runsDir 'scout_unhealthy_since.txt'
$restartRequestedFile = Join-Path $runsDir 'scout_restart_requested.txt'
New-Item -ItemType Directory -Path $runsDir -Force | Out-Null

if ($EnsureRunning -and -not $SetGmailPassword) {
    try {
        $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/scans' -TimeoutSec 5
        if ($null -ne $health.scans -and $null -ne $health.queue -and
            -not (Test-Path -LiteralPath $restartRequestedFile)) {
            Remove-Item -LiteralPath $unhealthySinceFile -ErrorAction SilentlyContinue
            return
        }
    }
    catch {
        # The watchdog continues to the normal startup path below.
    }
}

if ($SetGmailPassword -or -not (Test-Path -LiteralPath $passwordFile)) {
    if ($EnsureRunning -and -not $SetGmailPassword) {
        $securePassword = $null
        Write-Warning 'Gmail password is not saved. Starting Scout; match emails will wait in the outbox.'
    }
    else {
        Write-Host 'Create an app password for fca.21050@gmail.com at https://myaccount.google.com/apppasswords'
        Write-Host 'Enter that app password here. It will not be printed or saved in Git.'
        $securePassword = Read-Host 'Gmail app password' -AsSecureString
        if ($securePassword.Length -eq 0) {
            throw 'No app password entered.'
        }
        $securePassword | Export-Clixml -LiteralPath $passwordFile
    }
}
else {
    $securePassword = Import-Clixml -LiteralPath $passwordFile
}

$pythonCommand = Get-Command python -ErrorAction Stop
$pythonExe = $pythonCommand.Source
if ($null -ne $securePassword) {
    $credential = [System.Net.NetworkCredential]::new('', $securePassword)
    $env:SCOUT_SMTP_HOST = 'smtp.gmail.com'
    $env:SCOUT_SMTP_PORT = '587'
    $env:SCOUT_SMTP_USER = 'fca.21050@gmail.com'
    $env:SCOUT_SMTP_FROM = 'fca.21050@gmail.com'
    $env:SCOUT_SMTP_PASSWORD = $credential.Password
}
else {
    Remove-Item Env:SCOUT_SMTP_PASSWORD -ErrorAction SilentlyContinue
}
$env:SCOUT_PUBLIC_HOST = 'app.thedeveloperpaul.com'
$env:SCOUT_PUBLIC_WRITES = '1'
$env:SCOUT_CF_TEAM_DOMAIN = 'sparkling-frost-1ea7.cloudflareaccess.com'
$env:SCOUT_CF_AUD = '9d46018cd7f9357ee641888bcc3b6a8e3cd6b1a11bed64821be951df9fa268ae'

try {
    if (-not $EnsureRunning) {
        & $pythonExe (Join-Path $scoutRoot 'marketplace_email.py') check
        if ($LASTEXITCODE -ne 0) {
            throw 'Gmail SMTP login failed. The running Scout server was not changed.'
        }
    }

    $servers = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*marketplace_web.py --port 8765*' })
    if ($servers.Count -gt 0) {
        try {
            $state = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/scans' -TimeoutSec 5
        }
        catch {
            if (-not $EnsureRunning) {
                throw 'Could not confirm whether a scan is active. Scout was not restarted.'
            }
            if (-not (Test-Path -LiteralPath $unhealthySinceFile)) {
                (Get-Date).ToString('o') | Set-Content -LiteralPath $unhealthySinceFile
                throw 'Scout did not answer. The health check will try again before restarting it.'
            }
            try {
                $unhealthySince = [datetimeoffset]::Parse(
                    (Get-Content -LiteralPath $unhealthySinceFile -Raw).Trim())
            }
            catch {
                $unhealthySince = [datetimeoffset]::Now
                $unhealthySince.ToString('o') | Set-Content -LiteralPath $unhealthySinceFile
            }
            if ([datetimeoffset]::Now - $unhealthySince -lt [timespan]::FromMinutes(4)) {
                throw 'Scout still did not answer. Waiting for the next health check.'
            }
            Write-Warning 'Scout stayed unresponsive across health checks; restarting it.'
        }
        if ($null -ne $state -and $state.active_scan_id) {
            if ($EnsureRunning -and (Test-Path -LiteralPath $restartRequestedFile)) {
                Write-Host 'Update waiting for the active scan to finish.'
                return
            }
            throw "Scan $($state.active_scan_id) is running. Restart after it finishes."
        }
        if ($null -ne $state) {
            $research = Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/price-research' -TimeoutSec 5
            $runningResearch = @($research.jobs | Where-Object { $_.status -eq 'running' })
            if ($runningResearch.Count -gt 0) {
                if ($EnsureRunning -and (Test-Path -LiteralPath $restartRequestedFile)) {
                    Write-Host 'Update waiting for price research to finish.'
                    return
                }
                throw 'Price research is running. Restart after the current area check finishes.'
            }
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
    Remove-Item -LiteralPath $unhealthySinceFile -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $restartRequestedFile -ErrorAction SilentlyContinue
    if ($null -ne $securePassword) {
        Write-Host 'Scout is running with Gmail match emails enabled.'
    }
    else {
        Write-Host 'Scout is running. Match emails will wait for a Gmail password.'
    }
    Write-Host 'Only scan batches with new matching items create an email.'
}
finally {
    Remove-Item Env:SCOUT_SMTP_PASSWORD -ErrorAction SilentlyContinue
}
