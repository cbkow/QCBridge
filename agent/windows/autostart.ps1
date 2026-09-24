<#
.SYNOPSIS
  Start the QCBridge agent at logon, from the current user's Run key.

.DESCRIPTION
  The agent launches Blender and shows a tray icon, so it must run on the
  user's desktop. A value under HKCU\...\CurrentVersion\Run does exactly
  that: Explorer starts it after logon, in the interactive session, with
  the user's own rights and no console. (The earlier scheduled-task route,
  logon-task.ps1, gave the agent a console of its own, and anything that
  closed that console ended the agent with 0xC000013A; the agent is now a
  windowless executable and the Run key is the plain path.)

  One value per user; re-running replaces it. If the old "QCBridge Agent"
  scheduled task is still registered, it is removed so the two do not race.

.PARAMETER AgentPath
  The qcbridge-agent.exe to run. Default: the copy the extension ships
  (`..\..\qcbridge\bin\qcbridge-agent.exe`), else the cargo release build.

.PARAMETER Role
  host | replica. Passed on the command line, so it wins over agent.toml.

.PARAMETER StartNow
  Also start the agent now. Only from a desktop session: a process started
  over SSH has no desktop and ends with that session.

.PARAMETER Remove
  Delete the Run value instead.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File agent\windows\autostart.ps1 -Role replica -StartNow
  powershell -ExecutionPolicy Bypass -File agent\windows\autostart.ps1 -Remove
#>
param(
    [string] $AgentPath = "",
    [ValidateSet("host", "replica")] [string] $Role = "replica",
    [switch] $StartNow,
    [switch] $Remove,
    [string] $Name = "QCBridge Agent"
)

$ErrorActionPreference = "Stop"
$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

# The scheduled task this replaces.
if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    Write-Host "removed the old scheduled task '$Name'"
}

if ($Remove) {
    if (Get-ItemProperty -Path $runKey -Name $Name -ErrorAction SilentlyContinue) {
        Remove-ItemProperty -Path $runKey -Name $Name
        Write-Host "removed Run value '$Name'"
    } else {
        Write-Host "no Run value '$Name'"
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

$command = "`"$AgentPath`" --role $Role"
New-Item -Path $runKey -Force | Out-Null
Set-ItemProperty -Path $runKey -Name $Name -Value $command
Write-Host "Run value '$Name': $command (at log on of $env:USERNAME)"

if ($StartNow) {
    Start-Process -FilePath $AgentPath -ArgumentList "--role", $Role -WorkingDirectory (Split-Path -Parent $AgentPath)
    Write-Host "started"
}
