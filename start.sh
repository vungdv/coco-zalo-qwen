#!/usr/bin/env bash
# Start the Zalo Order Assistant after checking its prerequisites. Stop with Ctrl+C.
set -euo pipefail
cd "$(dirname "$0")"

fail() { echo "start.sh: $*" >&2; exit 1; }

command -v uv >/dev/null || fail "uv is not installed (brew install uv)"
command -v playwright-cli >/dev/null || fail "playwright-cli is not installed (see README, Setup)"
[ -f .env ] || fail ".env is missing: cp .env.example .env and fill it in"
grep -Eq '^SUPABASE_URL=.+' .env || fail "SUPABASE_URL is not set in .env"
grep -Eq '^SUPABASE_API_KEY=.+' .env || fail "SUPABASE_API_KEY is not set in .env"

ollama_url=$(grep -E '^OLLAMA_URL=' .env | cut -d= -f2- | tr -d ' "' || true)
ollama_url=${ollama_url:-http://localhost:11434}
curl -fsS -m 3 "$ollama_url/api/tags" >/dev/null || fail "Ollama is not reachable at $ollama_url (start Ollama)"

mkdir -p data
exec uv run zalo-order-assistant
