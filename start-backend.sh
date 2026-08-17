#!/usr/bin/env bash
# Rush Algo — backend startup script (Mac / Linux)
# Run with:  bash start-backend.sh
set -e

cd "$(dirname "$0")/backend"

# 1. Create the virtual environment the first time only
if [ ! -d "venv" ]; then
  echo "→ First run: creating Python virtual environment..."
  python3 -m venv venv
fi

# 2. Activate it
source venv/bin/activate

# 3. Install/update dependencies
echo "→ Installing dependencies (this is quick after the first time)..."
pip install -q -r requirements.txt

# 4. Make sure there's a .env file
if [ ! -f ".env" ]; then
  echo "→ No .env found — creating one from the template (paper mode by default)."
  cp .env.example .env
fi

# 5. Start the server
echo ""
echo "================================================================"
echo "  Rush Algo backend starting on http://localhost:8000"
echo "  API docs:      http://localhost:8000/docs"
echo "  Health check:  http://localhost:8000/health"
echo "  Press Ctrl+C to stop."
echo "================================================================"
echo ""
uvicorn main:app --reload --port 8000
