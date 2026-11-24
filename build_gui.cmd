@echo off
rem Build CoinFlow.exe and copy it to the project root.
setlocal
chcp 65001 >nul
set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"
cd /d "%~dp0" || exit /b 1
"%PY%" -m PyInstaller --noconfirm --clean CoinFlow.spec
if errorlevel 1 exit /b 1
copy /y "dist\CoinFlow.exe" "CoinFlow.exe" >nul
if errorlevel 1 exit /b 1
echo.
echo Built: %~dp0CoinFlow.exe
