#!/usr/bin/env bash
# Layered smoke tests for the ATLAS stack, run against the isolated compose
# project in tests/test.env (own name, ports, volumes).
#
# Usage: tests/smoke.sh base|vocab|solr|all
#        tests/smoke.sh down      # remove the test project and its volumes
set -uo pipefail
cd "$(dirname "$0")/.."

export ENV_FILE=tests/test.env
set -a; . "./$ENV_FILE"; set +a

WEBAPI="http://localhost:${WEBAPI_PORT}/WebAPI"
FIXTURE=./tests/fixtures/vocab-mini
FAILS=0

dc()   { docker compose --env-file "$ENV_FILE" "$@"; }
pass() { printf '  ok   %s\n' "$1"; }
fail() { printf '  FAIL %s\n' "$1"; FAILS=$((FAILS + 1)); }
assert_contains() { # name haystack needle
  if [[ "$2" == *"$3"* ]]; then pass "$1"; else fail "$1 (missing: $3)"; fi
}
assert_eq() { # name actual expected
  if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1 (got: $2, want: $3)"; fi
}
psql_q() { dc exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$1"; }
json()   { python3 -c "import sys, json; d = json.load(sys.stdin); print($1)"; }

layer_base() {
  echo "== base"
  if dc up -d --wait; then pass "stack starts healthy"; else fail "stack starts healthy"; return; fi
  assert_contains "WebAPI /info reports a version" \
    "$(curl -fsS "$WEBAPI/info")" '"version"'
  assert_contains "ATLAS config points at WebAPI" \
    "$(curl -fsS "http://localhost:${ATLAS_PORT}/atlas/js/config-local.js")" "$WEBAPI_PUBLIC_URL"
}

case "${1:-}" in
  base) layer_base ;;
  all)  layer_base ;;
  down) dc --profile load down -v; exit 0 ;;
  *)    echo "usage: $0 base|vocab|solr|all|down" >&2; exit 2 ;;
esac

echo
if (( FAILS )); then echo "$FAILS check(s) failed"; exit 1; fi
echo "all checks passed"
