@echo off
rem Windows launcher: runs claudemon.py with pythonw (no console window).
rem components.md: windows-linux/claudemon.bat, architecture.md security-sensitive area #6
rem (the repo path may contain spaces, so every path is quoted).

set "HERE=%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%HERE%claudemon.py" %*
    goto :eof
)

where pyw >nul 2>nul
if %errorlevel%==0 (
    start "" pyw -3 "%HERE%claudemon.py" %*
    goto :eof
)

echo Could not find "pythonw" or "pyw" on PATH.
echo Install Python 3.9 or newer from https://www.python.org/ (with "tcl/tk" included) and try again.
pause
