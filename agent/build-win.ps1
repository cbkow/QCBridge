<#
.SYNOPSIS
  Build the agent and the Windows encode helper (the replica's PQ/BT.2020
  HEVC encoder behind Blender's frame ring; no screen capture since 0.3),
  and put the helper where the addon looks for it.

.DESCRIPTION
  The twin of build-mac.sh: `cargo build --release` for agent/ and for
  agent/encode-win/, then qcb-encode-win.exe is copied beside the agent
  binary (agent/target/release/) and into qcbridge/bin/ when -Ship is
  given, which is where the extension ships both. The Build Tools' cl.exe must be reachable for zstd-sys; rustup's
  msvc toolchain already needs it.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File agent\build-win.ps1
  powershell -ExecutionPolicy Bypass -File agent\build-win.ps1 -Ship
#>
param([switch] $Ship)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo = Split-Path -Parent $here

Push-Location $here
try { cargo build --release; if ($LASTEXITCODE -ne 0) { throw "agent build failed" } } finally { Pop-Location }
Push-Location (Join-Path $here "encode-win")
try { cargo build --release; if ($LASTEXITCODE -ne 0) { throw "encode-win build failed" } } finally { Pop-Location }

$agentOut = Join-Path $here "target\release"
$capture  = Join-Path $here "encode-win\target\release\qcb-encode-win.exe"
Copy-Item -Force $capture (Join-Path $agentOut "qcb-encode-win.exe")
Write-Host "agent:   $(Join-Path $agentOut 'qcbridge-agent.exe')"
Write-Host "encoder: $(Join-Path $agentOut 'qcb-encode-win.exe')"

if ($Ship) {
    $bin = Join-Path $repo "qcbridge\bin"
    New-Item -ItemType Directory -Force $bin | Out-Null
    Copy-Item -Force (Join-Path $agentOut "qcbridge-agent.exe") $bin
    Copy-Item -Force $capture $bin
    Write-Host "shipped both into $bin"
}
