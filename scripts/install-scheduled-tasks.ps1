param(
    [switch]$Apply
)

$ProjectRoot = 'C:\Users\ardaa\Documents\TodoTasker'
$Python = Join-Path $ProjectRoot 'venv\Scripts\python.exe'
$UserId = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
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

$Definitions = @(
    @{
        Name = 'Daily Brief - Evening Check-in'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\checkin.py" send'
        Triggers = @(New-ScheduledTaskTrigger -Daily -At '21:00')
    },
    @{
        Name = 'Daily Brief - Process Check-in'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\checkin.py" process'
        Triggers = @(New-ScheduledTaskTrigger -Daily -At '21:30')
    },
    @{
        Name = 'Daily Brief - Prepare'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\brief.py" prepare'
        Triggers = @(New-ScheduledTaskTrigger -Daily -At '21:50')
    },
    @{
        Name = 'Daily Brief - Deliver'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\brief.py" deliver'
        Triggers = @(New-ScheduledTaskTrigger -Daily -At '06:30')
    },
    @{
        Name = 'Daily Brief - Watchdog'
        Arguments = '"C:\Users\ardaa\Documents\TodoTasker\brief.py" watchdog'
        Triggers = @(
            New-ScheduledTaskTrigger -Daily -At '07:30'
            New-ScheduledTaskTrigger -AtLogOn -User $UserId
        )
    }
)

foreach ($Definition in $Definitions) {
    Write-Host "$($Definition.Name): $Python $($Definition.Arguments)"
    if ($Apply) {
        $Action = New-ScheduledTaskAction `
            -Execute $Python `
            -Argument $Definition.Arguments `
            -WorkingDirectory $ProjectRoot
        Register-ScheduledTask `
            -TaskName $Definition.Name `
            -Action $Action `
            -Trigger $Definition.Triggers `
            -Settings $Settings `
            -Principal $Principal `
            -Force
    }
}

if (-not $Apply) {
    Write-Host 'Preview only. Re-run with -Apply after reviewing and approving every definition.'
}

