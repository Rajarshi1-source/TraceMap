#!/usr/bin/env bash
# Boot Jaeger under each storage profile and prove traces land.
#
# This is what turns "I have a fallback" into "my fallback is tested". Claiming
# an untested fallback is worse than having none, because it invites confidence
# that has never been checked -- which is exactly the failure ADR 0002 exists to
# prevent. Phase 7 runs this in CI.
#
#   ./scripts/profile-matrix.sh [profile ...]     default: badger clickhouse
set -uo pipefail

PROFILES=("${@:-}")
if [[ -z "${PROFILES[0]:-}" ]]; then PROFILES=(badger clickhouse); fi

GATEWAY="${GATEWAY_URL:-http://localhost:3000}"
JAEGER="${JAEGER_URL:-http://localhost:16686}"
fail=0

say() { printf '  %-52s %s\n' "$1" "$2"; }

echo ""
echo "  Jaeger storage-profile matrix"
echo ""

for profile in "${PROFILES[@]}"; do
  echo "  --- STORAGE_PROFILE=$profile ---"

  if ! STORAGE_PROFILE="$profile" docker compose --profile core up -d \
        --force-recreate --no-deps jaeger >/dev/null 2>&1; then
    say "boot" "FAIL"; fail=1; continue
  fi

  # Wait for health rather than sleeping a fixed guess.
  healthy=0
  for _ in $(seq 1 30); do
    sleep 3
    status="$(docker inspect -f '{{.State.Health.Status}}' tracemap-jaeger-1 2>/dev/null || echo none)"
    [[ "$status" == "healthy" ]] && { healthy=1; break; }
    [[ "$status" == "none" ]] && break
  done

  if [[ $healthy -ne 1 ]]; then
    say "becomes healthy" "FAIL"
    docker compose logs jaeger 2>&1 | grep -iE "error" | tail -2 | sed 's/^/      /'
    fail=1
    continue
  fi
  say "becomes healthy" "OK"

  # A profile that boots but stores nothing is not a working fallback.
  for i in 1 2 3; do
    curl -s -o /dev/null --max-time 15 -X POST "$GATEWAY/checkout" \
      -H 'Content-Type: application/json' \
      -d "{\"userId\":\"user-$i\",\"items\":[{\"sku\":\"SKU-$i\",\"qty\":1}],\"amount\":1999}"
  done
  sleep 30

  n="$(curl -s --max-time 10 "$JAEGER/api/services" \
       | python -c "import sys,json;print(len(json.load(sys.stdin).get('data') or []))" 2>/dev/null || echo 0)"
  if [[ "${n:-0}" -ge 5 ]]; then
    say "stores traces (services visible: $n)" "OK"
  else
    say "stores traces (services visible: ${n:-0}, want >=5)" "FAIL"; fail=1
  fi
  echo ""
done

if [[ $fail -eq 0 ]]; then echo "  PROFILE MATRIX PASS"; else echo "  PROFILE MATRIX FAIL"; fi
echo ""
exit $fail
