@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1"
set "RESULT=%ERRORLEVEL%"

if not "%RESULT%"=="0" (
    echo.
    echo 启动器未能完成初始化，错误代码：%RESULT%
    echo 请截图此窗口内容，并查看 launcher\logs 目录中的日志。
    pause
)

endlocal

