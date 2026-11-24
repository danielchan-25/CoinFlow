@echo off
rem CoinFlow launcher: UTF-8 console + adb/python on PATH.
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
where adb >nul 2>nul || set "PATH=%LOCALAPPDATA%\Android\platform-tools;%PATH%"
set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0kuaishou_rewards.py" %*
