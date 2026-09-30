[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$PythonExe = (Get-Command python -ErrorAction Stop).Source,
    [string]$TaskName = 'Pakuri-Collect'
)
$ErrorActionPreference = 'Stop'
$scriptFile = Join-Path $PSScriptRoot 'run_collect.py'
$resolvedPython = (Resolve-Path -LiteralPath $PythonExe).Path
$resolvedScript = (Resolve-Path -LiteralPath $scriptFile).Path
$action = New-ScheduledTaskAction -Execute $resolvedPython -Argument ('"' + $resolvedScript + '"') -WorkingDirectory (Split-Path $PSScriptRoot -Parent)
$hourly = New-ScheduledTaskTrigger -Once -At (Get-Date).AddHours(1) -RepetitionInterval (New-TimeSpan -Hours 1)
$logon = New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
if ($PSCmdlet.ShouldProcess($TaskName, 'Register hourly public GitHub collection for the signed-in user')) {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($hourly, $logon) -Principal $principal -Settings $settings -Description 'Pakuri public GitHub metadata collection; no Slack delivery.' -Force | Select-Object TaskName, State
}
