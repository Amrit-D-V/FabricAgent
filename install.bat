@echo off
setlocal enabledelayedexpansion

echo ===================================================================
echo   FabricAgent — Automated One-Liner Installer for Windows
echo   Autonomous Open-Source AI Agent for Power BI ^& Microsoft Fabric
echo ===================================================================
echo.

:: 1. Check Python installation
echo [1/4] Checking Python interpreter (Python 3.11+ required)...
set PYTHON_CMD=
for %%P in (python3.exe python.exe) do (
    where %%P >nul 2>&1
    if not errorlevel 1 (
        for /f "tokens=*" %%V in ('%%P -c "import sys; print(1 if sys.version_info >= (3, 11) else 0)" 2^>nul') do (
            if "%%V"=="1" (
                set PYTHON_CMD=%%P
                goto :found_python
            )
        )
    )
)

:found_python
if "%PYTHON_CMD%"=="" (
    echo [ERROR] Python 3.11 or newer was not found!
    echo Please download and install Python from: https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    exit /b 1
)

for /f "tokens=*" %%V in ('%PYTHON_CMD% -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')"') do (
    echo [OK] Found Python %%V (%PYTHON_CMD%)
)

:: 2. Create or verify virtual environment
echo.
echo [2/4] Setting up virtual environment (.venv)...
if not exist ".venv" (
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create virtual environment.
        exit /b 1
    )
    echo [OK] Created virtual environment in .venv\
) else (
    echo [OK] Existing .venv\ found.
)

set VENV_PYTHON=.venv\Scripts\python.exe
set VENV_PIP=.venv\Scripts\pip.exe

:: 3. Install dependencies and CLI package
echo.
echo [3/4] Installing dependencies and FabricAgent CLI...
%VENV_PIP% install --quiet --upgrade pip setuptools wheel
%VENV_PIP% install --quiet -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Failed to install requirements.
    exit /b 1
)
%VENV_PIP% install --quiet -e . --no-deps
if errorlevel 1 (
    echo [ERROR] Failed to install FabricAgent package.
    exit /b 1
)
echo [OK] FabricAgent CLI and dependencies installed successfully.

:: 4. Ensure .env exists and launch setup wizard
echo.
echo [4/4] Launching setup wizard...
if not exist ".env" (
    if exist ".env.example" (
        copy .env.example .env >nul
    ) else (
        type nul > .env
    )
)

.venv\Scripts\fabricagent.exe setup %*

echo.
echo ===================================================================
echo         Installation and Setup Complete!
echo ===================================================================
echo To activate the environment in PowerShell or CMD:
echo   .venv\Scripts\activate
echo.
echo To run FabricAgent:
echo   fabricagent tui         # Interactive terminal assistant
echo   fabricagent server      # Start REST API server
echo   fabricagent --help      # Show all available commands
echo ===================================================================
