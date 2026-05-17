# Register / unregister a Windows Task Scheduler task that runs post_chart.py.
#
#   .\setup_scheduler.ps1                 # register (default name: OiTimeChartPost)
#   .\setup_scheduler.ps1 -Unregister     # remove
#   .\setup_scheduler.ps1 -RunNow         # register then run immediately
#
# Default: runs every 6 hours starting at 00:05 local time
# → 00:05 / 06:05 / 12:05 / 18:05 local (= 1日4回)

[CmdletBinding()]
param(
    [string]$TaskName = "OiTimeChartPost",
    [int]$IntervalHours = 6,
    [int]$Minute = 5,
    [switch]$Unregister,
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

if ($Unregister) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Unregistered task: $TaskName"
    } else {
        Write-Host "Task not found: $TaskName"
    }
    return
}

$ProjectDir = $PSScriptRoot
$Script = Join-Path $ProjectDir "post_chart.py"

if (-not (Test-Path $Script)) {
    throw "post_chart.py not found in $ProjectDir"
}

# Resolve a python launcher
$PyCmd = (Get-Command pythonw.exe -ErrorAction SilentlyContinue)
if (-not $PyCmd) { $PyCmd = (Get-Command python.exe -ErrorAction SilentlyContinue) }
if (-not $PyCmd) { $PyCmd = (Get-Command py.exe -ErrorAction SilentlyContinue) }
if (-not $PyCmd) { throw "No python.exe / pythonw.exe / py.exe found on PATH" }

$PythonPath = $PyCmd.Source
Write-Host "Using python: $PythonPath"

$Action = New-ScheduledTaskAction `
    -Execute $PythonPath `
    -Argument "`"$Script`"" `
    -WorkingDirectory $ProjectDir

# Start at the next aligned slot (e.g. with 6h interval: next 00/06/12/18 + Minute)
$Now = Get-Date
$NextHour = [Math]::Ceiling(($Now.Hour + $Now.Minute / 60.0) / $IntervalHours) * $IntervalHours
$Start = (Get-Date -Hour 0 -Minute $Minute -Second 0).AddHours($NextHour)
if ($Start -le $Now) { $Start = $Start.AddHours($IntervalHours) }

$Trigger = New-ScheduledTaskTrigger -Once -At $Start `
    -RepetitionInterval (New-TimeSpan -Hours $IntervalHours)

$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$Principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Principal $Principal `
    -Description "Post latest BTC OI/liquidation chart to Discord & X (every $IntervalHours h)" | Out-Null

Write-Host "Registered task: $TaskName"
Write-Host "  First run: $Start"
Write-Host "  Then every $IntervalHours hour(s) at minute $Minute"

if ($RunNow) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Triggered task immediately"
}
