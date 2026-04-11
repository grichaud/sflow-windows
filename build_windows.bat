@echo off
echo ============================
echo  Construyendo SFlow.exe
echo ============================

:: Instalar PyInstaller si no está
pip show pyinstaller >nul 2>&1 || pip install pyinstaller

:: Limpiar builds anteriores
if exist dist\SFlow rmdir /s /q dist\SFlow
if exist build\SFlow rmdir /s /q build\SFlow

:: Construir el .exe
pyinstaller sflow_windows.spec --noconfirm

if errorlevel 1 (
    echo.
    echo ERROR: El build falló.
    pause
    exit /b 1
)

echo.
echo ============================
echo  Build exitoso!
echo  Ejecutable: dist\SFlow\SFlow.exe
echo ============================

:: Crear acceso directo en el Escritorio
echo Creando acceso directo en el Escritorio...
powershell -Command "$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\SFlow.lnk'); $s.TargetPath = '%CD%\dist\SFlow\SFlow.exe'; $s.WorkingDirectory = '%CD%\dist\SFlow'; $s.Description = 'SFlow - Voz a Texto'; $s.Save()"

echo Acceso directo creado en el Escritorio.
echo.
echo Para fijarlo en la barra de tareas: clic derecho en el acceso directo ^> "Anclar a la barra de tareas"
echo.
pause
