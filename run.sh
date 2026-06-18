#!/bin/bash
set -e

cd "$(dirname "$0")"

# Copy .env.example if no .env exists
if [ ! -f backend/.env ]; then
  cp backend/.env.example backend/.env
  echo "⚙  Created backend/.env — add your GROQ_API_KEY before first briefing."
fi

# Install Python deps
echo "→ Installing dependencies..."
pip install -q -r backend/requirements.txt

# Install Playwright browser
echo "→ Installing Playwright Chromium..."
playwright install chromium

# Launch
echo "→ Starting JARVIS on http://localhost:8000"
open "http://localhost:8000" 2>/dev/null || true
uvicorn main:app --reload --port 8000 --app-dir backend
