#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "=== AUSTRO AI setup ==="

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f ".env" ]; then
  echo "Creating .env from .env.example..."
  cp .env.example .env
  echo ">>> Edit .env and set your BOT_TOKEN, then run: python main.py"
else
  echo ".env already exists - leaving it untouched."
fi

echo "=== Setup complete. Run: python main.py ==="