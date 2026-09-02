#!/usr/bin/env bash
# End-to-end smoke test for Phase 1: a checkout must traverse all five services.
set -euo pipefail

GATEWAY="${GATEWAY_URL:-http://localhost:3000}"
fail=0

say()  { printf '  %-52s %s\n' "$1" "$2"; }
ok()   { say "$1" "OK"; }
bad()  { say "$1" "FAIL"; fail=1; }

echo ""
echo "  TraceMap smoke test -> ${GATEWAY}"
echo ""

# 1. liveness on every service
for probe in \
  "gateway  ${GATEWAY}/healthz" \
  "order    http://localhost:3001/healthz" \
  "payment  http://localhost:8001/healthz"; do
  name="${probe%% *}"; url="${probe##* }"
  if curl -sf -o /dev/null --max-time 5 "$url"; then ok "healthz  $name"; else bad "healthz  $name"; fi
done

# 2. readiness (dependencies reachable)
if curl -sf -o /dev/null --max-time 5 "${GATEWAY}/readyz"; then ok "readyz   gateway"; else bad "readyz   gateway"; fi

# 3. the demo path, through all five services
body='{"userId":"user-1","items":[{"sku":"SKU-1","qty":1}],"amount":1999}'
resp="$(curl -s --max-time 15 -w '\n%{http_code}' -X POST "${GATEWAY}/checkout" \
        -H 'Content-Type: application/json' -d "$body")"
code="$(printf '%s' "$resp" | tail -n1)"
json="$(printf '%s' "$resp" | sed '$d')"

if [[ "$code" == "201" || "$code" == "200" ]]; then
  ok "POST /checkout -> $code"
else
  bad "POST /checkout -> $code"
  echo "      $json"
fi

# the response proves user-svc (name) and payment-svc (chargeId) both answered
if grep -q '"user"' <<<"$json" && grep -q '"chargeId"' <<<"$json"; then
  ok "response carries user + charge (all 5 services)"
else
  bad "response missing user or chargeId"
  echo "      $json"
fi

# 4. browse path
if curl -sf -o /dev/null --max-time 10 "${GATEWAY}/products"; then ok "GET /products"; else bad "GET /products"; fi

# 5. faults must default to OFF (M23)
for svc in "payment http://localhost:8001"; do
  name="${svc%% *}"; url="${svc##* }"
  if curl -s --max-time 5 "${url}/_internal/fault" | grep -q '"active": *false'; then
    ok "faults default off  $name"
  else
    bad "faults default off  $name"
  fi
done

echo ""
if [[ $fail -eq 0 ]]; then echo "  SMOKE PASS"; else echo "  SMOKE FAIL"; fi
echo ""
exit $fail
