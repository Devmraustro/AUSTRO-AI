@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo === AUSTRO AI setup ===

if not exist ".venv" (
  echo Creating virtual environment...
  python -m venv .venv
)

call .venv\Scripts\activate.bat

echo Installing dependencies...
python -m pip install --upgrade pip
pip install -r requirements.txt

if not exist ".env" (
  echo Creating .env from .env.example...
  copy .env.example .env
  echo Edit .env and set your BOT_TOKEN, then run: python main.py
) else (
  echo .env already exists - leaving it untouched.
)

echo === Setup complete. Run: python main.py ===