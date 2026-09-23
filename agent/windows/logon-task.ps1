<#
.SYNOPSIS
  Start the QCBridge agent at logon, in the interactive session.

.DESCRIPTION
  The agent launches Blender, which needs a desktop, so this is a Scheduled
  Task with an "At log on" trigger for the current user (`-Interactive`), not
  a Windows service. It runs with the user's own rights (no elevation), starts
  the agent with its tray, and does not stop it when the task runs long.

  A task is registered per user; running this as another user registers a
  second one. Re-running replaces the existing task.

.PARAMETER AgentPath
  The qcbridge-agent.exe to run. Default: the copy beside this script's
  parent (`..\bin\qcbridge-agent.exe`, where the extension ships it), else the
  cargo release build.

.PARAMETER Role
  host | replica. Written into the agent's own config on first start if the
  config has none; passed on the command line otherwise.

.PARAMETER Remove
  Unregister the task instead.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File agent\windows\logon-task.ps1 -Role replica
  powershell -ExecutionPolicy Bypass -File agent\windows\logon-task.ps1 -Remove
#>
param(
    [string] $AgentPath = "",
    [ValidateSet("host", "replica")] [string] $Role = "replica",
    [switch] $Remove,
    [string] $TaskName = "QCBridge Agent"
)

$ErrorActionPreference = "Stop"

if ($Remove) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "removed task '$TaskName'"
    } else {
        Write-Host "no task '$TaskName'"
    }
    exit 0
}

if (-not $AgentPath) {
    $here = Split-Path -Parent $MyInvocation.MyCommand.Path
    $candidates = @(
        (Join-Path $here "..\..\qcbridge\bin\qcbridge-agent.exe"),
        (Join-Path $here "..\target\release\qcbridge-agent.exe")
    )
    $AgentPath = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $AgentPath) { throw "qcbridge-agent.exe not found; pass -AgentPath" }
}
$AgentPath = (Resolve-Path $AgentPath).Path

$action    = New-ScheduledTaskAction -Execute $AgentPath -Argument "--role $Role" `
                 -WorkingDirectory (Split-Path -Parent $AgentPath)
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Interactive: the agent must run on the user's desktop (it launches Blender
# and shows a tray icon). Limited: the user's own rights, no UAC prompt.
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) `
                 -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                 -StartWhenAvailable -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force | Out-Null
Write-Host "registered task '$TaskName': $AgentPath --role $Role, at log on of $env:USERNAME (interactive, limited)"
