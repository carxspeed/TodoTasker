param(
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = 'C:\Users\ardaa\Documents\TodoTasker'
$Python = Join-Path $ProjectRoot 'venv\Scripts\pythonw.exe'
$Runner = Join-Path $ProjectRoot 'scripts\run-scheduled.pyw'
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Windowless Python was not found at $Python"
}
if (-not (Test-Path -LiteralPath $Runner -PathType Leaf)) {
    throw "The hidden scheduled-task runner was not found at $Runner"
}

$Definitions = @(
    @{
        Name = 'Daily Brief - Canvas Auth Check'
        EntryPoint = 'canvas.py'
        Arguments = 'auth-check --notify'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '20:30' })
    },
    @{
        Name = 'Daily Brief - Evening Check-in'
        EntryPoint = 'checkin.py'
        Arguments = 'send'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '21:00' })
    },
    @{
        Name = 'Daily Brief - Process Check-in'
        EntryPoint = 'checkin.py'
        Arguments = 'process'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '21:30' })
    },
    @{
        Name = 'Daily Brief - Prepare'
        EntryPoint = 'brief.py'
        Arguments = 'prepare'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '21:50' })
    },
    @{
        Name = 'Daily Brief - Deliver'
        EntryPoint = 'brief.py'
        Arguments = 'deliver'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '06:30' })
    },
    @{
        Name = 'Daily Brief - Watchdog'
        EntryPoint = 'brief.py'
        Arguments = 'watchdog'
        TriggerSpecs = @(
            @{ Type = 'Daily'; At = '07:30' }
            @{ Type = 'LogOn' }
        )
    }
)

if ($Apply) {
    $Settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -WakeToRun `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -RestartCount 3 `
        -RestartInterval (New-TimeSpan -Minutes 10) `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    $Principal = New-ScheduledTaskPrincipal `
        -UserId $UserId `
        -LogonType Interactive `
        -RunLevel Limited
}

foreach ($Definition in $Definitions) {
    $TriggerSummary = ($Definition.TriggerSpecs | ForEach-Object {
        if ($_.Type -eq 'Daily') { "daily $($_.At)" } else { 'at logon' }
    }) -join ', '
    $ActionArguments = '"{0}" {1} {2}' -f $Runner, $Definition.EntryPoint, $Definition.Arguments
    Write-Host "$($Definition.Name) [$TriggerSummary]: $Python $ActionArguments"
    if ($Apply) {
        $Triggers = @($Definition.TriggerSpecs | ForEach-Object {
            if ($_.Type -eq 'Daily') {
                New-ScheduledTaskTrigger -Daily -At $_.At
            } else {
                New-ScheduledTaskTrigger -AtLogOn -User $UserId
            }
        })
        $Action = New-ScheduledTaskAction `
            -Execute $Python `
            -Argument $ActionArguments `
            -WorkingDirectory $ProjectRoot
        Register-ScheduledTask `
            -TaskName $Definition.Name `
            -Action $Action `
            -Trigger $Triggers `
            -Settings $Settings `
            -Principal $Principal `
            -Force
    }
}

if (-not $Apply) {
    Write-Host 'Preview only. Re-run with -Apply after reviewing and approving every definition.'
}
