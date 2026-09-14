param(
    [switch]$Apply
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ProjectRoot = 'C:\Users\ardaa\Documents\TodoTasker'
$Python = Join-Path $ProjectRoot 'venv\Scripts\python.exe'
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$Definitions = @(
    @{
        Name = 'Daily Brief - Canvas Auth Check'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\canvas.py" auth-check --notify'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '20:30' })
    },
    @{
        Name = 'Daily Brief - Evening Check-in'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\checkin.py" send'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '21:00' })
    },
    @{
        Name = 'Daily Brief - Process Check-in'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\checkin.py" process'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '21:30' })
    },
    @{
        Name = 'Daily Brief - Prepare'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\brief.py" prepare'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '21:50' })
    },
    @{
        Name = 'Daily Brief - Deliver'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\brief.py" deliver'
        TriggerSpecs = @(@{ Type = 'Daily'; At = '06:30' })
    },
    @{
        Name = 'Daily Brief - Watchdog'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\brief.py" watchdog'
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
    Write-Host "$($Definition.Name) [$TriggerSummary]: $Python $($Definition.Arguments)"
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
            -Argument $Definition.Arguments `
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
