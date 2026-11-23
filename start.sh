#!/bin/zsh
# Update the databases to the last completed trading day, then open the app.
#   ./start.sh            update + app
#   ./start.sh --quick    prices + technicals only (skip index lists, SEC fundamentals, market cap)
cd "$(dirname "$0")"
source .venv/bin/activate
if [[ "$1" == "--quick" ]]; then
  python scripts/daily_update.py --no-membership --no-fundamentals --no-screen --no-market-cap
else
  python scripts/daily_update.py
fi
echo
echo "Starting the app (close it with Ctrl+C) ..."
streamlit run app/Home.py
