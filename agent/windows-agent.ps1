# Monitoring agent for Windows: reports CPU, memory, disk and uptime to a push monitor.
#
# Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File windows-agent.ps1 -Url <push-url>
#
# Run every minute as a scheduled task (PowerShell as Administrator):
#   $action  = New-ScheduledTaskAction -Execute "powershell.exe" `
#              -Argument "-NoProfile -ExecutionPolicy Bypass -File C:\monitor\windows-agent.ps1 -Url <push-url>"
#   $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 1)
#   Register-ScheduledTask -TaskName "MonitorAgent" -Action $action -Trigger $trigger -User "SYSTEM" -RunLevel Highest

param(
    [string]$Url = $env:MONITOR_URL,
    [string[]]$Drives = @("C:")
)

$ErrorActionPreference = "Stop"
if (-not $Url) {
    Write-Error "usage: windows-agent.ps1 -Url <push-url>"
    exit 1
}

$cpu = (Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average
$os = Get-CimInstance Win32_OperatingSystem
$mem = [math]::Round((1 - $os.FreePhysicalMemory / $os.TotalVisibleMemorySize) * 100, 1)
$uptime = [int]((Get-Date) - $os.LastBootUpTime).TotalSeconds
$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
$procs = (Get-Process).Count

$metrics = [ordered]@{
    cpu    = [math]::Round([double]$cpu, 1)
    mem    = $mem
    cores  = $cores
    uptime = $uptime
    procs  = $procs
}

foreach ($drive in $Drives) {
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$drive'"
    if ($disk -and $disk.Size -gt 0) {
        $name = if ($drive -eq "C:") { "disk" } else { "disk_" + $drive.TrimEnd(":").ToLower() }
        $metrics[$name] = [math]::Round((1 - $disk.FreeSpace / $disk.Size) * 100, 1)
    }
}

$body = @{
    status  = "up"
    message = $env:COMPUTERNAME
    metrics = $metrics
} | ConvertTo-Json -Depth 3

Invoke-RestMethod -Uri $Url -Method Post -Body $body -ContentType "application/json" -TimeoutSec 15 | Out-Null
