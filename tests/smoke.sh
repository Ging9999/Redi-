#!/usr/bin/env bash
# Curl smoke test for a running coordination server (spec milestone 1:
# "Testable with curl alone"). Starts nothing — point it at a live server.
#
#   COORD_URL and COORD_TOKEN are read from the environment.
#   Defaults: http://127.0.0.1:8787, no token.
#
# Usage:
#   python3 server/coordinator.py &      # in another shell
#   ./tests/smoke.sh
set -euo pipefail

URL="${COORD_URL:-http://127.0.0.1:8787}"
AUTH=()
if [[ -n "${COORD_TOKEN:-}" ]]; then
  AUTH=(-H "Authorization: Bearer ${COORD_TOKEN}")
fi
JSON=(-H "Content-Type: application/json")
REPO="github.com/acme/api"
REPO_ENC="github.com%2Facme%2Fapi"

say() { printf "\n=== %s ===\n" "$1"; }

say "health"
curl -fsS "${URL}/healthz"; echo

say "Sam declares intent (session sam-1)"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST \
  "${URL}/sessions/sam-1/intent" \
  -d '{"intent":"add per-IP rate limiting to the auth middleware"}'; echo

say "Sam registers a claim on src/auth/middleware.ts"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "${URL}/claims" -d "{
  \"repo_key\":\"${REPO}\",
  \"file_path\":\"src/auth/middleware.ts\",
  \"session_id\":\"sam-1\",
  \"machine_id\":\"sam-laptop\",
  \"display_name\":\"Sam\",
  \"branch\":\"feat/rate-limit\"
}"; echo

say "Priya checks the same file (should see Sam's intent as a conflict)"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "${URL}/claims/check" -d "{
  \"repo_key\":\"${REPO}\",
  \"file_path\":\"src/auth/middleware.ts\",
  \"session_id\":\"priya-1\",
  \"machine_id\":\"priya-laptop\"
}"; echo

say "Priya checks a different file (should be no conflict)"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "${URL}/claims/check" -d "{
  \"repo_key\":\"${REPO}\",
  \"file_path\":\"src/other.ts\",
  \"session_id\":\"priya-1\",
  \"machine_id\":\"priya-laptop\"
}"; echo

say "activity for the repo"
curl -fsS "${AUTH[@]}" "${URL}/repos/${REPO_ENC}/activity"; echo

say "Sam's session stops (release)"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "${URL}/sessions/sam-1/release" -d '{}'; echo

say "Priya re-checks (conflict gone)"
curl -fsS "${AUTH[@]}" "${JSON[@]}" -X POST "${URL}/claims/check" -d "{
  \"repo_key\":\"${REPO}\",
  \"file_path\":\"src/auth/middleware.ts\",
  \"session_id\":\"priya-1\",
  \"machine_id\":\"priya-laptop\"
}"; echo

printf "\n=== smoke test complete ===\n"
