#!/usr/bin/env bash
# ==============================================================================
# FabricAgent — Autonomous AI Agent for Power BI & Microsoft Fabric
# One-Liner Automated Installer for Linux and macOS
# ==============================================================================

set -euo pipefail

# ANSI Color codes
BOLD="\033[1m"
GREEN="\033[0;32m"
CYAN="\033[0;36m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
DIM="\033[2m"
RESET="\033[0m"

echo -e "${BOLD}${CYAN}"
cat << 'EOF'
  _____     _          _            _                    _   
 |  ___|_ _| |__  _ __(_) ___      / \   __ _  ___ _ __ | |_ 
 | |_ / _` | '_ \| '__| |/ __|    / _ \ / _` |/ _ \ '_ \| __|
 |  _| (_| | |_) | |  | | (__    / ___ \ (_| |  __/ | | | |_ 
 |_|  \__,_|_.__/|_|  |_|\___|  /_/   \_\__, |\___|_| |_|\__|
                                        |___/                
EOF
echo -e "${RESET}${BOLD} FabricAgent — Automated One-Liner Installer${RESET}"
echo -e "${DIM} Autonomous Open-Source AI Agent for Power BI & Microsoft Fabric${RESET}\n"

# ------------------------------------------------------------------------------
# 1. Check if running inside cloned repo or curl piped
# ------------------------------------------------------------------------------
REPO_URL="https://github.com/Amrit-D-V/FabricAgent.git"
INSTALL_DIR="FabricAgent"

if [ ! -f "pyproject.toml" ] || ! grep -q "fabricagent" "pyproject.toml" 2>/dev/null; then
    echo -e "${CYAN}[1/5] Cloning FabricAgent repository...${RESET}"
    if command -v git >/dev/null 2>&1; then
        git clone "$REPO_URL" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    else
        echo -e "${RED}[ERROR] git is required to install FabricAgent.${RESET}"
        echo -e "Please install git or clone the repository manually."
        exit 1
    fi
else
    echo -e "${GREEN}[1/5] Running inside FabricAgent repository directory.${RESET}"
fi

# ------------------------------------------------------------------------------
# 2. Check Python 3.11+
# ------------------------------------------------------------------------------
echo -e "${CYAN}[2/5] Checking Python interpreter (>= 3.11 required)...${RESET}"

PYTHON_BIN=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        ver_check=$("$candidate" -c 'import sys; print(1 if sys.version_info >= (3, 11) else 0)' 2>/dev/null || echo "0")
        if [ "$ver_check" = "1" ]; then
            PYTHON_BIN=$(command -v "$candidate")
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo -e "${RED}[ERROR] Python 3.11 or newer was not found!${RESET}"
    echo -e "FabricAgent requires Python 3.11, 3.12, 3.13, or 3.14."
    echo -e "\nTo install Python:"
    echo -e "  ${YELLOW}Ubuntu/Debian:${RESET} sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    echo -e "  ${YELLOW}Fedora/RHEL:${RESET}   sudo dnf install -y python3 python3-pip"
    echo -e "  ${YELLOW}Arch Linux:${RESET}    sudo pacman -S python python-pip"
    echo -e "  ${YELLOW}macOS:${RESET}         brew install python@3.12"
    exit 1
fi

PYTHON_VERSION=$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
echo -e "${GREEN}✓ Found Python ${PYTHON_VERSION} (${PYTHON_BIN})${RESET}"

# ------------------------------------------------------------------------------
# 3. Setup Python Virtual Environment (.venv)
# ------------------------------------------------------------------------------
echo -e "${CYAN}[3/5] Setting up virtual environment (.venv)...${RESET}"

if [ ! -d ".venv" ]; then
    "$PYTHON_BIN" -m venv .venv
    echo -e "${GREEN}✓ Created isolated virtual environment in .venv/${RESET}"
else
    echo -e "${GREEN}✓ Existing .venv/ detected.${RESET}"
fi

VENV_PYTHON=".venv/bin/python"
VENV_PIP=".venv/bin/pip"

# Ensure pip is up-to-date
"$VENV_PIP" install --quiet --upgrade pip setuptools wheel

# ------------------------------------------------------------------------------
# 4. Install Dependencies & FabricAgent CLI
# ------------------------------------------------------------------------------
echo -e "${CYAN}[4/5] Installing dependencies and FabricAgent CLI...${RESET}"

"$VENV_PIP" install --quiet -r requirements.txt
"$VENV_PIP" install --quiet -e . --no-deps

echo -e "${GREEN}✓ Dependencies and FabricAgent package installed.${RESET}"

# ------------------------------------------------------------------------------
# 5. Bootstrap Environment & Launch Setup Wizard
# ------------------------------------------------------------------------------
echo -e "${CYAN}[5/5] Launching interactive setup wizard...${RESET}\n"

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
    else
        touch .env
    fi
fi

# Run setup wizard
.venv/bin/fabricagent setup "$@"

echo -e "\n${BOLD}${GREEN}===================================================================${RESET}"
echo -e "${BOLD}${GREEN}        Installation and Setup Complete!                           ${RESET}"
echo -e "${BOLD}${GREEN}===================================================================${RESET}"
echo -e "To activate the environment in your shell:"
echo -e "  ${CYAN}source .venv/bin/activate${RESET}"
echo -e "\nTo run FabricAgent:"
echo -e "  ${CYAN}fabricagent tui${RESET}         # Interactive terminal assistant"
echo -e "  ${CYAN}fabricagent server${RESET}      # REST API server"
echo -e "  ${CYAN}fabricagent --help${RESET}      # View all available commands"
