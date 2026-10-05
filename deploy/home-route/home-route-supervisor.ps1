param(
    [string]$PythonPath = 'python.exe',
    [string]$SshHost = 'jarvis-vps',
    [string]$StateDirectory = (Join-Path $env:LOCALAPPDATA 'RentMonitor\home-route'),
    [string]$RelayScript = (Join-Path $PSScriptRoot 'home-connect-relay.py')
)
$ErrorActionPreference = 'Stop'
$taskRoot = $StateDirectory
New-Item -ItemType Directory -Force -Path $taskRoot | Out-Null
$taskRelayProcess = $null
$taskTunnelProcess = $null
$taskMutex = [Threading.Mutex]::new($false, 'Local\RentMonitorHomeRoute')
if (-not $taskMutex.WaitOne(0)) { exit 0 }
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
public static class RentMonitorPower {
    [DllImport("kernel32.dll")]
    public static extern uint SetThreadExecutionState(uint flags);
}
'@
function Write-RouteLog([string]$Text) {
    $taskLog = Join-Path $taskRoot 'home-route.log'
    if ((Test-Path -LiteralPath $taskLog) -and (Get-Item -LiteralPath $taskLog).Length -gt 1048576) {
        Move-Item -LiteralPath $taskLog -Destination (Join-Path $taskRoot 'home-route.previous.log') -Force
    }
    Add-Content -LiteralPath $taskLog -Value "$(Get-Date -Format o) $Text" -Encoding utf8
}
try {
    Write-RouteLog 'supervisor started'
    [void][RentMonitorPower]::SetThreadExecutionState([uint32]2147483649)
    $taskStatePath = Join-Path $taskRoot 'processes.json'
    if (Test-Path -LiteralPath $taskStatePath) {
        $taskPrevious = Get-Content -LiteralPath $taskStatePath -Raw | ConvertFrom-Json
        $taskPreviousRelay = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskPrevious.relay_pid)" -ErrorAction SilentlyContinue
        if ($null -ne $taskPreviousRelay -and $taskPreviousRelay.CommandLine.Contains($RelayScript)) {
            $taskRelayProcess = Get-Process -Id $taskPrevious.relay_pid
            Write-RouteLog "adopted relay pid=$($taskRelayProcess.Id)"
        }
        $taskPreviousTunnel = Get-CimInstance Win32_Process -Filter "ProcessId=$($taskPrevious.tunnel_pid)" -ErrorAction SilentlyContinue
        if ($null -ne $taskPreviousTunnel -and $taskPreviousTunnel.CommandLine -like "*127.0.0.1:18782:127.0.0.1:18781*$SshHost*") {
            $taskTunnelProcess = Get-Process -Id $taskPrevious.tunnel_pid
            Write-RouteLog "adopted tunnel pid=$($taskTunnelProcess.Id)"
        }
    }
    while ($true) {
        if ($null -eq $taskRelayProcess -or $taskRelayProcess.HasExited) {
            $taskRelayProcess = Start-Process -FilePath $PythonPath -ArgumentList @('-u',('"'+$RelayScript+'"')) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRoot 'relay.stdout.log') -RedirectStandardError (Join-Path $taskRoot 'relay.stderr.log')
            Write-RouteLog "relay started pid=$($taskRelayProcess.Id)"
            Start-Sleep -Seconds 2
        }
        if ($null -eq $taskTunnelProcess -or $taskTunnelProcess.HasExited) {
            if ($null -ne $taskTunnelProcess) { Write-RouteLog "tunnel exited code=$($taskTunnelProcess.ExitCode); retrying" }
            $taskTunnelProcess = Start-Process -FilePath 'C:\Windows\System32\OpenSSH\ssh.exe' -ArgumentList @('-N','-o','BatchMode=yes','-o','ConnectTimeout=15','-o','ExitOnForwardFailure=yes','-o','ServerAliveInterval=20','-o','ServerAliveCountMax=3','-R','127.0.0.1:18782:127.0.0.1:18781',$SshHost) -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $taskRoot 'tunnel.stdout.log') -RedirectStandardError (Join-Path $taskRoot 'tunnel.stderr.log')
            Write-RouteLog "tunnel started pid=$($taskTunnelProcess.Id)"
        }
        @{relay_pid=$taskRelayProcess.Id;tunnel_pid=$taskTunnelProcess.Id;updated_at=(Get-Date -Format o)} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $taskRoot 'processes.json') -Encoding utf8
        Start-Sleep -Seconds 10
    }
} finally {
    [void][RentMonitorPower]::SetThreadExecutionState([uint32]2147483648)
    foreach ($taskChild in @($taskTunnelProcess,$taskRelayProcess)) {
        if ($null -ne $taskChild -and -not $taskChild.HasExited) { Stop-Process -Id $taskChild.Id -ErrorAction SilentlyContinue }
    }
    $taskMutex.ReleaseMutex()
    $taskMutex.Dispose()
}
