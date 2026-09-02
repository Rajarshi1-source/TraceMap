#!/usr/bin/env bash
# M17 guard - proved by actually stopping Postgres, not by reading the code.
#
#   /healthz  answers "should I be restarted?"  -> must stay 200 with the DB down
#   /readyz   answers "should I get traffic?"   -> must go 503 with the DB down
#
# Getting this backwards is the most common Kubernetes mistake in interviews:
# check a dependency in liveness and one slow query restarts the whole fleet,
# turning a degradation into an outage.
set -euo pipefail

fail=0
say() { printf '  %-56s %s\n' "$1" "$2"; }

code() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$1" || echo "000"; }

SERVICES=(
  "order    http://localhost:3001"
  "payment  http://localhost:8001"
)

echo ""
echo "  M17 probe guard"
echo ""
echo "  -- baseline: database UP --"
for s in "${SERVICES[@]}"; do
  name="${s%% *}"; url="${s##* }"
  h="$(code "${url}/healthz")"; r="$(code "${url}/readyz")"
  [[ "$h" == "200" ]] && say "healthz $name = $h" "OK" || { say "healthz $name = $h (want 200)" "FAIL"; fail=1; }
  [[ "$r" == "200" ]] && say "readyz  $name = $r" "OK" || { say "readyz  $name = $r (want 200)" "FAIL"; fail=1; }
done

echo ""
echo "  -- stopping postgres --"
docker compose stop postgres >/dev/null 2>&1
sleep 6

echo "  -- database DOWN --"
for s in "${SERVICES[@]}"; do
  name="${s%% *}"; url="${s##* }"
  h="$(code "${url}/healthz")"; r="$(code "${url}/readyz")"
  [[ "$h" == "200" ]] && say "healthz $name = $h (survives, M17)" "OK" \
                      || { say "healthz $name = $h (want 200)" "FAIL"; fail=1; }
  [[ "$r" == "503" ]] && say "readyz  $name = $r (sheds traffic)" "OK" \
                      || { say "readyz  $name = $r (want 503)" "FAIL"; fail=1; }
done

echo ""
echo "  -- restarting postgres --"
docker compose start postgres >/dev/null 2>&1
sleep 8

echo ""
if [[ $fail -eq 0 ]]; then echo "  PROBE GUARD PASS"; else echo "  PROBE GUARD FAIL"; fi
echo ""
exit $fail
