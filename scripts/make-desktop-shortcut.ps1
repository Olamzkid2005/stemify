$ErrorActionPreference = "Stop"
$target = "C:\Users\glitchb3atz\Documents\Stemify\stemify\start-windows.cmd"
if (-not (Test-Path $target)) { throw "target not found: $target" }
$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "Stemify.lnk"
$shell = New-Object -ComObject WScript.Shell
$s = $shell.CreateShortcut($lnk)
$s.TargetPath = $target
$s.WorkingDirectory = "C:\Users\glitchb3atz\Documents\Stemify\stemify"
$s.Description = "Start the Stemify web app and worker"
$s.Save()
Write-Output "Shortcut created: $lnk"
