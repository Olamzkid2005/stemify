#!/usr/bin/env bash
# Start the Stemify web app in development mode.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d node_modules ]; then
  echo "Installing dependencies..."
  npm install
fi

echo "Stemify dev server: http://localhost:3000 (Ctrl+C to stop)"
npm run dev -w apps/web
