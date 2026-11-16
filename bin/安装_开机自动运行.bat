@echo off
chcp 936 >nul 2>&1
title LANPush 被控端 安装
cd /d "%~dp0"
set "EXE=%~dp0LANPush-Agent.exe"
if not exist "%EXE%" (
  echo.
  echo   [错误] 没找到 LANPush-Agent.exe
  echo   请把本文件和 LANPush-Agent.exe 放在同一个文件夹里再运行。
  echo.
  pause
  exit /b 1
)
echo %EXE% | find " " >nul
if not errorlevel 1 (
  echo.
  echo   [提示] 当前文件夹路径里带空格：
  echo     %EXE%
  echo   开机自启在带空格的路径下不稳定，建议把整个文件夹移到 C:/LANPush 再运行本文件。
  echo.
  pause
  exit /b 1
)
echo   正在设置开机自动运行...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v LANPushAgent /t REG_SZ /d "%EXE%" /f >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [错误] 写入开机自启失败，可能被安全软件拦截。
  echo   可以跳过自启：直接双击 LANPush-Agent.exe 也能用，只是每次开机要手动开一次。
  echo.
  pause
  exit /b 1
)
start "" "%EXE%"
echo.
echo   完成：登录 Windows 后会自动在后台运行（无窗口），不用每次手动开。
echo.
echo   程序位置：%EXE%
echo   接收目录：data\agent_config.json 里的 saveDir
echo   运行日志：双击 查看日志.bat
echo.
echo   已启动一次，请到主控端网页确认设备是否出现。
echo.
pause
