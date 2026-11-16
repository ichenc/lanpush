@echo off
chcp 936 >nul 2>&1
if not exist "%~dp0data\agent.log" (
  echo.
  echo   还没有日志文件，说明程序可能没运行过。
  echo.
  pause
  exit /b 1
)
notepad "%~dp0data\agent.log"
