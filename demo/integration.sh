#!/usr/bin/env bash
# End-to-end run on the store demo (Postgres in Docker, vitest, Playwright): what a real agent session goes
# through, checked line by line. Needs npm and Docker. From the repo root: bash demo/integration.sh
set -euo pipefail
source demo/setup.sh store >/dev/null
export CLAUDE_PROJECT_DIR="$PWD"
SID="ci-$$-$RANDOM"  # a fresh agent session each run
step() { printf '\n== %s\n' "$*"; }
expect() { grep -qF -- "$1" <<<"$2" || { printf 'expected to find: %s\nin:\n%s\n' "$1" "$2"; exit 1; }; }
verify() { echo "{\"session_id\":\"$1\"}" | doneguard verify; }
later() { sleep 1.1; }  # file times have 1 s resolution on some filesystems

doneguard init claude >/dev/null
git add -A && git -c user.name=ci -c user.email=ci@example.com commit -qm "doneguard conf"

step "doctor: everything this project needs is there"
doneguard doctor

step "a clean change passes: Postgres starts, gets seeded, vitest and Playwright pass, evidence is kept"
out=$(doneguard check src/cart.ts)
expect "✓ postgres setup" "$out"
expect "✓ playwright" "$out"
expect "e2e passed, evidence in .doneguard/evidence/" "$out"
ls .doneguard/evidence/*/results/*/test-finished-1.png >/dev/null

step "a bug fails vitest and Playwright, with the failure screenshot for the agent"
perl -pi -e 's/ \* qty;/;/' src/cart.ts
out=$(doneguard check src/cart.ts) && exit 1 || true
expect "expected 10 to be 20" "$out"
expect "test-failed-1.png" "$out"
git checkout -q src/cart.ts

step "a broken seed is reported as the cause, and the tests after it don't run"
perl -pi -e 's/VALUES/VALUE/' db/seed.ts
out=$(doneguard check db/seed.ts src/cart.ts) && exit 1 || true
expect 'syntax error at or near "VALUE"' "$out"
! grep -qF "vitest" <<<"$out" || { echo "vitest ran after a broken seed"; exit 1; }
git checkout -q db/seed.ts

step "agent session: a cheat and a new error in a file the agent didn't touch are both caught"
echo "{\"session_id\":\"$SID\"}" | doneguard start
for _ in $(seq 60); do [ -f "${TMPDIR:-/tmp}/doneguard/$SID.known" ] && break; sleep 0.5; done
later
perl -pi -e 's/Promise<number>/Promise<string>/; s/return sum;/return `\$\${sum.toFixed(2)}`;/' src/cart.ts
perl -pi -e 's/^test\(/test.skip(/' src/cart.test.ts
out=$(verify "$SID")
expect '"decision": "block"' "$out"
expect "test.skip" "$out"
expect "New errors in files you didn't change" "$out"
expect "server.ts" "$out"

step "after the fix the agent may stop; the skip it kept is reported to the user once"
git checkout -q src/cart.ts
out=$(verify "$SID")
expect "kept after being told" "$out"
git checkout -q src/cart.test.ts

step "no container left behind"
[ -z "$(docker ps -aq --filter label=doneguard)" ]
echo "integration: all good"
