param(
    [string]$PythonPath = 'python.exe',
    [string]$SshHost = 'jarvis-vps',
    [string]$StateDirectory = (Join-Path $env:LOCALAPPDATA 'RentMonitor\home-route')
)
$ErrorActionPreference = 'Stop'
$taskSupervisor = Join-Path $PSScriptRoot 'home-route-supervisor.ps1'
$taskPython = (Get-Command $PythonPath -ErrorAction Stop).Source
$taskUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$taskArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -PythonPath "{1}" -SshHost "{2}" -StateDirectory "{3}"' -f $taskSupervisor,$taskPython,$SshHost,$StateDirectory
$taskAction = New-ScheduledTaskAction -Execute 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -Argument $taskArguments
$taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
$taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName 'RentMonitor-HomeRoute' -Action $taskAction -Trigger $taskTrigger -Principal $taskPrincipal -Settings $taskSettings -Description 'Домашнее подключение для мониторинга квартир на VPS' -Force | Out-Null
Write-Output 'Задача зарегистрирована. После остановки предыдущего supervisor запустите Start-ScheduledTask -TaskName RentMonitor-HomeRoute.'
