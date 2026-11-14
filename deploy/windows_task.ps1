# mafc-nav 每日同步 · Windows 计划任务（以管理员 PowerShell 运行）
# 若工程不在默认路径，修改下面 $ProjectRoot
$ProjectRoot = "/workspace/mafc-nav"
$Python      = "python"
$Script      = Join-Path $ProjectRoot "src\sync_daily.py"
$Action      = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $ProjectRoot
$Trigger     = New-ScheduledTaskTrigger -Daily -At "09:30"
$Settings    = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName "mafc-nav-daily-sync" -Action $Action -Trigger $Trigger `
  -Settings $Settings -Description "策略模型接口与看板数据每日自动同步" -Force
Get-ScheduledTask -TaskName "mafc-nav-daily-sync" | Select-Object TaskName,State,NextRunTime
