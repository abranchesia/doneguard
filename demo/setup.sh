# Throwaway demo project for the VHS tapes in this folder.
# Usage, from the repo root: source demo/setup.sh [shop|empty]
REPO="$(pwd)"
DEMO=/tmp/doneguard-demo
rm -rf /tmp/doneguard-demo
mkdir -p "$DEMO/bin"
printf '#!/bin/sh\nexec python3 "%s/doneguard.py" "$@"\n' "$REPO" > "$DEMO/bin/doneguard"
chmod +x "$DEMO/bin/doneguard"
export PATH="$DEMO/bin:$PATH" XDG_STATE_HOME="$DEMO/state"

cat > "$DEMO/cart.ok.ts" <<'EOF'
export type Item = { price: number; qty: number };

export function total(items: Item[]): number {
  return items.reduce((sum, i) => sum + i.price * i.qty, 0);
}
EOF
cat > "$DEMO/cart.bug.ts" <<'EOF'
export type Item = { price: number; qty: number };

export function total(items: Item[], coupon = ""): number {
  return items.reduce((sum, i) => sum + i.price * i.qty, coupon);
}
EOF

if [ "${1:-shop}" = shop ]; then
  mkdir -p "$DEMO/shop/src" && cd "$DEMO/shop" || return
  printf '{\n  "name": "shop",\n  "private": true,\n  "type": "module",\n  "devDependencies": { "typescript": "5.9.3", "vitest": "3.2.4" }\n}\n' > package.json
  printf '{ "compilerOptions": { "strict": true, "noEmit": true, "target": "ES2022", "module": "ESNext", "moduleResolution": "Bundler", "skipLibCheck": true }, "include": ["src"] }\n' > tsconfig.json
  printf 'node_modules/\n' > .gitignore
  cp "$DEMO/cart.ok.ts" src/cart.ts
  cat > src/cart.test.ts <<'EOF'
import { expect, test } from "vitest";
import { total } from "./cart";

test("total", () => {
  expect(total([{ price: 10, qty: 2 }])).toBe(20);
});
EOF
  npm install --silent --no-audit --no-fund
  git init -q && git add -A && git -c user.name=demo -c user.email=demo@example.com commit -qm init
else
  cd "$DEMO" || return
fi
