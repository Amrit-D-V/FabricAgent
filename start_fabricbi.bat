@echo off
setlocal enabledelayedexpansion

echo ===================================================
echo       Starting FabricAgent (FabricBI) Engine
echo ===================================================

set PYTHON_EXE=venv\Scripts\python.exe
if not exist "%PYTHON_EXE%" (
    set PYTHON_EXE=.venv\Scripts\python.exe
)

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python virtual environment not found!
    echo Please create it: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

echo [1/3] Checking environment and database preflight...
%PYTHON_EXE% -c "import os, sys; from db.database import init_db; from api.server import preflight_check; init_db(); preflight_check(); print('       Database and storage verified.')"

echo [2/3] Checking dependencies...
%PYTHON_EXE% -c "import starlette, uvicorn; print('       Core server packages ready.')"

echo [3/3] Launching FabricAgent API Server...
echo API listening on http://localhost:8756
%PYTHON_EXE% -m api.server
