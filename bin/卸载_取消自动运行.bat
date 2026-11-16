@echo off
chcp 936 >nul 2>&1
title LANPush 被控端 卸载
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v LANPushAgent /f >nul 2>&1
taskkill /im LANPush-Agent.exe /f >nul 2>&1
echo.
echo   已取消开机自启，并停止了正在运行的被控端。
echo.
pause
