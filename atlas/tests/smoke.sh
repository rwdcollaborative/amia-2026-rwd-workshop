#!/usr/bin/env bash
# Layered smoke tests for the ATLAS stack, run against the isolated compose
# project in tests/test.env (own name, ports, volumes).
#
# Usage: tests/smoke.sh base|vocab|solr|all
#        tests/smoke.sh down      # remove the test project and its volumes
set -uo pipefail
cd "$(dirname "$0")/.."

export ENV_FILE=tests/test.env
case "$ENV_FILE" in /*) env_path=$ENV_FILE ;; *) env_path="./$ENV_FILE" ;; esac
set -a; . "$env_path"; set +a

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

layer_vocab() {
  echo "== vocab"
  if ./load-vocab.sh "$FIXTURE"; then pass "loader succeeds on fixture"; else fail "loader succeeds on fixture"; return; fi

  assert_contains "VOCAB source registered" \
    "$(curl -fsS "$WEBAPI/source/sources")" '"sourceKey":"VOCAB"'
  assert_contains "search finds T2DM (201826)" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/search?query=diabetes")" '"CONCEPT_ID":201826'
  # JSON-escaped form of: Fixture "quoted" name with \backslash
  assert_contains "quote and backslash survive loading" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/concept/9000001")" 'Fixture \"quoted\" name with \\backslash'
  assert_contains "descendants resolve via concept_ancestor" \
    "$(curl -fsS -X POST -H 'Content-Type: application/json' \
        -d '{"items":[{"concept":{"CONCEPT_ID":201820},"isExcluded":false,"includeDescendants":true,"includeMapped":false}]}' \
        "$WEBAPI/vocabulary/VOCAB/resolveConceptSetExpression")" '4193704'

  local cs_id
  cs_id=$(curl -fsS -X POST -H 'Content-Type: application/json' \
            -d "{\"name\":\"smoke-$(date +%s)\"}" "$WEBAPI/conceptset/" | json 'd["id"]')
  if [[ "$cs_id" =~ ^[0-9]+$ ]]; then pass "concept set created (id $cs_id)"; else fail "concept set created"; return; fi
  assert_eq "concept set items saved" \
    "$(curl -fsS -X PUT -H 'Content-Type: application/json' \
        -d '[{"conceptId":201820,"isExcluded":0,"includeDescendants":1,"includeMapped":0}]' \
        "$WEBAPI/conceptset/$cs_id/items")" "true"
  assert_eq "concept set expression reads back" \
    "$(curl -fsS "$WEBAPI/conceptset/$cs_id/expression" \
        | json '(d["items"][0]["concept"]["CONCEPT_ID"], d["items"][0]["includeDescendants"])')" \
    "(201820, True)"

  if ./load-vocab.sh "$FIXTURE" >/dev/null; then pass "loader rerun succeeds"; else fail "loader rerun succeeds"; fi
  assert_eq "rerun leaves exactly one VOCAB source" \
    "$(psql_q "select count(*) from webapi.source where source_key = 'VOCAB'")" "1"
  assert_eq "rerun leaves one vocabulary daimon" \
    "$(psql_q "select count(*) from webapi.source_daimon d join webapi.source s using (source_id) where s.source_key = 'VOCAB'")" "1"
  if curl -fsS "$WEBAPI/conceptset/$cs_id" >/dev/null; then pass "concept set survives reload"; else fail "concept set survives reload"; fi

  local tmp out
  tmp=$(mktemp -d)
  cp "$FIXTURE"/*.csv "$tmp"/
  rm "$tmp/CONCEPT_ANCESTOR.csv" "$tmp/DOMAIN.csv"
  if out=$(./load-vocab.sh "$tmp" 2>&1); then fail "incomplete bundle is rejected"; else pass "incomplete bundle is rejected"; fi
  assert_contains "error names CONCEPT_ANCESTOR.csv" "$out" "CONCEPT_ANCESTOR.csv"
  assert_contains "error names DOMAIN.csv" "$out" "DOMAIN.csv"
  assert_eq "existing vocab untouched" "$(psql_q "select count(*) from vocab.concept")" "6"
  rm -rf "$tmp"
}

layer_solr() {
  echo "== solr"
  local core="v5.0_FIXTURE-01-JAN-26" solr="http://localhost:${SOLR_PORT}/solr"
  if ./load-vocab.sh "$FIXTURE"; then pass "loader succeeds with solr"; else fail "loader succeeds with solr"; return; fi

  assert_contains "core named from vocab version exists" \
    "$(curl -fsS "$solr/admin/cores?action=STATUS&core=$core&wt=json")" "\"name\":\"$core\""
  assert_eq "core is populated" \
    "$(curl -fsS "$solr/$core/select?q=query:diabetes&rows=0&wt=json" | json 'd["response"]["numFound"] >= 4')" "True"
  assert_contains "WebAPI search returns results" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/search?query=diabetes")" '"CONCEPT_ID":201826'

  # WebAPI has no SQL fallback once a Solr core matches: if Solr is down,
  # search is empty. That proves WebAPI is really using Solr.
  dc stop solr >/dev/null 2>&1
  assert_eq "WebAPI search is empty with Solr stopped (uses Solr)" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/search?query=diabetes")" "[]"
  dc up -d --wait solr >/dev/null 2>&1
  assert_contains "WebAPI search recovers when Solr returns" \
    "$(curl -fsS "$WEBAPI/vocabulary/VOCAB/search?query=diabetes")" '"CONCEPT_ID":201826'
}

layer_proxy() {
  echo "== proxy"
  local proxy="http://localhost:${PROXY_HTTP_PORT}" auth="workshop:${WORKSHOP_PASSWORD}"
  # Server config: ATLAS reaches WebAPI through the proxy by a relative URL.
  if WEBAPI_PUBLIC_URL=/WebAPI/ dc --profile proxy up -d --wait; then
    pass "stack starts with proxy"
  else
    fail "stack starts with proxy"; return
  fi
  code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

  assert_eq "no password is rejected" "$(code "$proxy/atlas/")" "401"
  assert_eq "wrong password is rejected" "$(code -u workshop:nope "$proxy/atlas/")" "401"
  assert_eq "WebAPI needs the password too" "$(code "$proxy/WebAPI/info")" "401"
  assert_eq "/ redirects to ATLAS" "$(code -u "$auth" "$proxy/")" "302"
  assert_eq "ATLAS served" "$(code -u "$auth" "$proxy/atlas/")" "200"
  assert_contains "ATLAS config uses relative WebAPI URL" \
    "$(curl -fsS -u "$auth" "$proxy/atlas/js/config-local.js")" 'var webapi_url = "/WebAPI/"'
  assert_contains "WebAPI proxied" "$(curl -fsS -u "$auth" "$proxy/WebAPI/info")" '"version"'
  assert_eq "source list readable" "$(code -u "$auth" "$proxy/WebAPI/source/sources")" "200"

  # ATLAS sends "Authorization: null" on some calls; the session cookie covers them.
  local cookie
  cookie=$(curl -fsS -o /dev/null -D - -u "$auth" "$proxy/atlas/" | tr -d '\r' \
             | sed -n 's/^[Ss]et-[Cc]ookie: \(atlas_session=[^;]*\).*/\1/p')
  assert_contains "login sets session cookie" "$cookie" "atlas_session="
  assert_eq "no session cookie on a 401" \
    "$(curl -s -o /dev/null -D - "$proxy/atlas/" | grep -ci '^set-cookie')" "0"
  assert_eq "cookie admits 'Authorization: null' calls" \
    "$(code -H 'Authorization: null' -b "$cookie" "$proxy/WebAPI/info")" "200"
  assert_eq "bogus cookie is rejected" "$(code -b atlas_session=nope "$proxy/WebAPI/info")" "401"
  assert_eq "cookie does not bypass source block" \
    "$(code -b "$cookie" -X DELETE "$proxy/WebAPI/source/1")" "403"

  local json='Content-Type: application/json'
  assert_eq "source create blocked" "$(code -u "$auth" -X POST -H "$json" -d '{}' "$proxy/WebAPI/source/")" "403"
  assert_eq "source delete blocked" "$(code -u "$auth" -X DELETE "$proxy/WebAPI/source/1")" "403"
  assert_eq "source update blocked" "$(code -u "$auth" -X PUT -H "$json" -d '{}' "$proxy/WebAPI/source/1")" "403"
  assert_eq "source details blocked" "$(code -u "$auth" "$proxy/WebAPI/source/details/VOCAB")" "403"
  assert_eq "blocked regardless of case" "$(code -u "$auth" -X DELETE "$proxy/WebAPI/SOURCE/1")" "403"
  assert_eq "blocked with doubled slash" "$(code -u "$auth" -X DELETE --path-as-is "$proxy/WebAPI//source/1")" "403"
  assert_eq "blocked with encoded path" "$(code -u "$auth" -X DELETE --path-as-is "$proxy/WebAPI/%73ource/1")" "403"
  assert_eq "path params rejected" "$(code -u "$auth" -X DELETE --path-as-is "$proxy/WebAPI;x/source/1")" "400"
  assert_contains "concept set create still allowed" \
    "$(curl -fsS -u "$auth" -X POST -H "$json" -d "{\"name\":\"proxy-$(date +%s)\"}" "$proxy/WebAPI/conceptset/")" '"id"'
}

case "${1:-}" in
  base)  layer_base ;;
  vocab) layer_base && layer_vocab ;;
  solr)  layer_base && layer_solr ;;
  proxy) layer_base && layer_proxy ;;
  all)   layer_base && layer_vocab && layer_solr && layer_proxy ;;
  down)  dc --profile load --profile proxy down -v; exit 0 ;;
  *)     echo "usage: $0 base|vocab|solr|proxy|all|down" >&2; exit 2 ;;
esac

echo
if (( FAILS )); then echo "$FAILS check(s) failed"; exit 1; fi
echo "all checks passed"
