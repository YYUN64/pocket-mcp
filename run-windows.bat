@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem One-click Windows launcher. Extra args are passed through, for example:
rem   run-windows.bat --port 9000 --workspace D:\mcp-workspace
rem The default workspace is this directory unless MCP_WORKSPACE is set.

where py >nul 2>nul
if errorlevel 1 goto try_python
py -3 --version >nul 2>nul
if errorlevel 1 goto try_python
echo [deepseek-lite-mcp] using: py -3
py -3 "%~dp0server.py" --host 0.0.0.0 %*
exit /b %errorlevel%

:try_python
where python >nul 2>nul
if errorlevel 1 goto no_python
python --version >nul 2>nul
if errorlevel 1 goto no_python
echo [deepseek-lite-mcp] using: python
python "%~dp0server.py" --host 0.0.0.0 %*
exit /b %errorlevel%

:no_python
echo [ERROR] Python 3 was not found.
echo Install Python 3.8+ from https://www.python.org/downloads/windows/
echo During installation, enable "Add python.exe to PATH".
pause
exit /b 1
