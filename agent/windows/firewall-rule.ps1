<#
.SYNOPSIS
  Windows Firewall rule for the QCBridge agent's discovery port (UDP/4246).

.DESCRIPTION
  A replica answers unicast probes and multicast beacons on UDP/4246; without
  an inbound rule, Windows Firewall drops them silently and the replica is
  invisible to every host on the LAN or over the VPN. Same shape as
  MinRender's rule for UDP/4243 (installer/minrender_installer.iss there):
  unscoped on purpose. `remoteip=localsubnet` would break peers reaching the
  machine over a VPN from another subnet, and would not help on public Wi-Fi
  anyway, where the hostile network IS the local subnet. What the port exposes
  is a beacon reply (name, role, port, fingerprint); the QUIC session behind
  it is authenticated by the token and the certificate fingerprint.

  The QUIC control port (default UDP/19990) needs the same treatment on a
  replica, since the host connects in; pass -ControlPort to add it.

  Needs an elevated shell (netsh advfirewall). An installer runs this at
  post-install and the -Remove form at uninstall.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File agent\windows\firewall-rule.ps1
  powershell -ExecutionPolicy Bypass -File agent\windows\firewall-rule.ps1 -ControlPort 19990
  powershell -ExecutionPolicy Bypass -File agent\windows\firewall-rule.ps1 -Remove
#>
param(
    [int] $DiscoveryPort = 4246,
    [int] $ControlPort = 0,
    [switch] $Remove
)

$ErrorActionPreference = "Stop"
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "run from an elevated shell: firewall rules need administrator rights" }

$rules = @(@{ name = "QCBridge Agent discovery"; port = $DiscoveryPort })
if ($ControlPort -gt 0) { $rules += @{ name = "QCBridge Agent control"; port = $ControlPort } }

foreach ($r in $rules) {
    & netsh advfirewall firewall delete rule name="$($r.name)" | Out-Null
    if (-not $Remove) {
        & netsh advfirewall firewall add rule name="$($r.name)" dir=in action=allow protocol=udp localport=$($r.port) enable=yes | Out-Null
        Write-Host "added inbound rule '$($r.name)' UDP/$($r.port)"
    } else {
        Write-Host "removed rule '$($r.name)'"
    }
}
