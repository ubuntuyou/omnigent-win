#requires -Version 5.1
<#
.SYNOPSIS
    Launch the Omnigent server bound to the LAN so other machines on the
    network can connect to it.

.DESCRIPTION
    Binds to 0.0.0.0 and re-enables the single-user (no-login) fallback plus
    the WebSocket origin allowlist that a non-loopback bind otherwise disables
    as a fail-closed guardrail (see omnigent/cli.py:2942).

    >>> SECURITY <<<
    This runs an UNAUTHENTICATED agent server reachable by ANY device on your
    LAN. Anyone who can reach this host:port can drive the agent, read every
    session, attach to terminals, and run shell commands on this machine.
    The origin allowlist only deters browser-based hijacking; a direct client
    (curl/script) sends no Origin and bypasses it. Only run this on a network
    you trust. For untrusted networks, use OIDC/accounts mode or a proxy.

.EXAMPLE
    .\start-omnigent.ps1
        Auto-detects the LAN IP, frees the port, and starts the server.

.EXAMPLE
    .\start-omnigent.ps1 -LanIp 10.1.1.12 -Port 6767
        Pins the address used for the browser-origin allowlist.

.EXAMPLE
    .\start-omnigent.ps1 -ExtraOrigins "http://omnibox:6767","http://10.1.1.5:6767"
        Adds extra browser origins (e.g. a hostname, a second NIC).
#>
[CmdletBinding()]
param(
    [int]$Port = 6767,
    [string]$BindHost = "0.0.0.0",
    [string]$LanIp,                                                  # auto-detected when omitted
    [string[]]$ExtraOrigins = @(),                                   # extra allowed browser origins
    [switch]$NoStopExisting,                                         # skip freeing the port first
    [switch]$NoHostDaemon,                                           # skip the local host daemon (server only)
    [string]$DatabaseUri = "sqlite:///$env:USERPROFILE\.omnigent\chat.db",
    [string]$ArtifactLocation = "$env:USERPROFILE\.omnigent\artifacts"
)

$ErrorActionPreference = "Stop"

# Run from the project dir so `uv run` resolves this project's environment.
Set-Location -LiteralPath $PSScriptRoot

# --- Resolve the LAN IP used for the WebSocket origin allowlist ------------
# (The allowlist needs the address the *browser* shows in its URL bar, i.e.
# this host's LAN IP — not the connecting client's IP.)
if (-not $LanIp) {
    $LanIp = Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object {
            $_.IPAddress -notlike '127.*' -and
            $_.IPAddress -notlike '169.254.*' -and
            $_.InterfaceAlias -notlike '*VMware*' -and
            $_.InterfaceAlias -notlike '*Loopback*'
        } |
        Sort-Object SkipAsSource |
        Select-Object -First 1 -ExpandProperty IPAddress
}
if (-not $LanIp) {
    Write-Warning "Could not auto-detect a LAN IPv4 address. Pass -LanIp <addr> so remote browsers are allowlisted."
}

# --- Build the comma-separated origin allowlist ---------------------------
$origins = @()
if ($LanIp) { $origins += "http://${LanIp}:$Port" }
$origins += $ExtraOrigins
$allowed = (($origins | Where-Object { $_ }) | Select-Object -Unique) -join ","

# --- Free the port if a previous server instance still holds it -----------
if (-not $NoStopExisting) {
    $owners = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($procId in $owners) {
        Write-Host "Stopping existing listener on port $Port (PID $procId)..."
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}

# --- Stop any existing local host daemon(s) -------------------------------
# The host daemon (`omni host --server …`) is what spawns the per-session
# runner that native harnesses (Claude/Codex/OpenCode/Pi/Goose) attach to.
# A leftover daemon from a previous launch duplicates this one and the two
# recycle each other's tunnel (endless reconnect, server sees `runners=[]`),
# so a session can never get a runner. Match on `host --server` so the
# server's own `server --host` is never caught.
if (-not $NoHostDaemon) {
    Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='omni.exe'" |
        Where-Object { $_.CommandLine -match 'host\s+--server' } |
        ForEach-Object {
            Write-Host "Stopping existing host daemon (PID $($_.ProcessId))..."
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

# --- Security posture: re-enable no-auth single-user + origin allowlist ----
# (Read the SECURITY note in this file's header before relying on this.)
$env:OMNIGENT_LOCAL_SINGLE_USER = "1"
if ($allowed) { $env:OMNIGENT_WS_ALLOWED_ORIGINS = $allowed }

# --- Launch the local host daemon (background) ----------------------------
# Runs alongside the foreground server so native-harness sessions can attach.
# It retries the connection until the server below is listening, so launch
# order doesn't matter. Stopped again in the finally when the server exits.
if (-not $NoHostDaemon) {
    $daemonServer = if ($LanIp) { "http://${LanIp}:$Port" } else { "http://127.0.0.1:$Port" }
    $omni = Join-Path $PSScriptRoot ".venv\Scripts\omni.exe"
    if (Test-Path $omni) {
        $daemon = Start-Process -FilePath $omni `
            -ArgumentList 'host', '--server', $daemonServer `
            -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    } else {
        $daemon = Start-Process -FilePath 'uv' `
            -ArgumentList 'run', 'omni', 'host', '--server', $daemonServer `
            -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
    }
}

Write-Host ""
Write-Host "Starting Omnigent (UNAUTHENTICATED - LAN exposed)" -ForegroundColor Yellow
Write-Host "  Bind:            ${BindHost}:$Port"
Write-Host "  Connect from:    http://${LanIp}:$Port"
Write-Host "  Allowed origins: $allowed"
if (-not $NoHostDaemon) { Write-Host "  Host daemon:     PID $($daemon.Id) -> $daemonServer (native harnesses)" }
Write-Host "  Anyone on your LAN can reach this with no login. Ctrl+C to stop." -ForegroundColor Yellow
Write-Host ""

try {
    uv run omnigent server `
        --host $BindHost `
        --port $Port `
        --database-uri $DatabaseUri `
        --artifact-location $ArtifactLocation
}
finally {
    # Best-effort: stop the daemon we started so it doesn't orphan. The
    # startup cleanup above is the real guarantee: even if Ctrl+C skips
    # this finally, the next launch kills any leftover daemon first.
    if (-not $NoHostDaemon) {
        Write-Host "`nServer stopped - shutting down host daemon..."
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='omni.exe'" |
            Where-Object { $_.CommandLine -match 'host\s+--server' } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}
