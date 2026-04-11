$ws = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = $ws.CreateShortcut("$desktop\SFlow.lnk")
$shortcut.TargetPath = "C:\Antigravity\SFlow - Voz a Texto\sflow\dist\SFlow\SFlow.exe"
$shortcut.WorkingDirectory = "C:\Antigravity\SFlow - Voz a Texto\sflow\dist\SFlow"
$shortcut.Description = "SFlow - Voz a Texto"
$shortcut.Save()
Write-Host "Acceso directo creado en el Escritorio"
