#!/usr/bin/env bash
set -euo pipefail

echo "==================================================="
echo "          Starting FabricAgent AI Engine"
echo "==================================================="

PYTHON_BIN=".venv/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="venv/bin/python"
fi

if [ ! -f "$PYTHON_BIN" ]; then
    echo "[ERROR] Python virtual environment not found!"
    echo "Please run: ./install.sh"
    exit 1
fi

echo "[1/3] Checking environment and database preflight..."
$PYTHON_BIN -c "
from db.database import init_db
from api.server import preflight_check
try:
    init_db()
    preflight_check()
    print('       Database and storage verified.')
except Exception as e:
    print(f'       [WARNING] Preflight check notice: {e}')
"

echo "[2/3] Checking core server packages..."
$PYTHON_BIN -c "import starlette, uvicorn; print('       Core server packages ready.')"

echo "[3/3] Launching FabricAgent API Server..."
echo "API listening on http://localhost:8756"
exec $PYTHON_BIN -m api.server
