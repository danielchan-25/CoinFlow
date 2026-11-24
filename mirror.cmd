@echo off
rem CoinFlow mirror launcher: starts scrcpy only.
rem Usage:
rem   mirror.cmd                                use ADB_SERIAL or one online device
rem   mirror.cmd DEVICE_SERIAL                  choose a connected device
rem   mirror.cmd DEVICE_SERIAL --no-audio
rem   mirror.cmd --max-size 1024                 pass arguments through to scrcpy
setlocal
chcp 65001 >nul
where adb >nul 2>nul || set "PATH=%LOCALAPPDATA%\Android\platform-tools;%PATH%"
where scrcpy >nul 2>nul || set "PATH=%LOCALAPPDATA%\scrcpy;%PATH%"

where adb >nul 2>nul
if errorlevel 1 (
  echo [mirror] adb not found. Install Android platform-tools or add adb to PATH.
  exit /b 1
)

where scrcpy >nul 2>nul
if errorlevel 1 (
  echo [mirror] scrcpy not found.
  echo [mirror] install it or add it to PATH: https://github.com/Genymobile/scrcpy
  exit /b 1
)

set "DEVICE="
set "EXTRA="

if "%~1"=="" goto detect
rem A leading dash means "no serial given, pass it to scrcpy".
set "FIRST=%~1"
if "%FIRST:~0,1%"=="-" goto collect
set "DEVICE=%~1"
shift

:collect
if "%~1"=="" goto detect
set "EXTRA=%EXTRA% %1"
shift
goto collect

:detect
if defined DEVICE goto ready
if defined ADB_SERIAL set "DEVICE=%ADB_SERIAL%"
if not defined DEVICE if defined ANDROID_SERIAL set "DEVICE=%ANDROID_SERIAL%"
if defined DEVICE goto ready

set "MULTIPLE_DEVICES="
for /f "skip=1 tokens=1,2" %%D in ('adb devices 2^>nul') do if /i "%%E"=="device" (
  if defined DEVICE set "MULTIPLE_DEVICES=1"
  set "DEVICE=%%D"
)
if defined MULTIPLE_DEVICES (
  echo [mirror] multiple online devices. Pass a serial or set ADB_SERIAL.
  adb devices
  exit /b 1
)
if defined DEVICE goto ready
echo [mirror] no online authorized device. Check USB or wireless debugging.
adb devices
exit /b 1

:ready
adb -s "%DEVICE%" get-state 2>nul | findstr /i /x /c:"device" >nul
if not errorlevel 1 goto launch
rem Only a caller-supplied network address can require an adb connect.
if "%DEVICE::=%"=="%DEVICE%" goto unavailable
adb connect "%DEVICE%"
adb -s "%DEVICE%" get-state 2>nul | findstr /i /x /c:"device" >nul
if errorlevel 1 goto unavailable

:launch
echo [mirror] device : %DEVICE%
echo [mirror] stop   : close the mirror window or press Ctrl+C
scrcpy -s "%DEVICE%" --stay-awake --window-title "CoinFlow mirror %DEVICE%"%EXTRA%
exit /b %ERRORLEVEL%

:unavailable
echo [mirror] the selected device is not online and authorized.
adb devices
exit /b 1
