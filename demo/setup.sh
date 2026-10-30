# Throwaway demo project for the VHS tapes in this folder.
# Usage, from the repo root: source demo/setup.sh [shop|store|empty]
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

# store: the same cart, priced from Postgres, with a page and a Playwright test. Needs Docker running.
write_store() {
  mkdir -p "$DEMO/store/src" "$DEMO/store/db" "$DEMO/store/e2e" && cd "$DEMO/store" || return
  cat > package.json <<'JSON'
{
  "name": "store",
  "private": true,
  "type": "module",
  "scripts": { "dev": "node server.ts", "db:migrate": "node db/migrate.ts", "db:seed": "node db/seed.ts" },
  "dependencies": { "pg": "8.16.3" },
  "devDependencies": { "@playwright/test": "1.64.1", "@types/node": "24.10.1", "@types/pg": "8.15.6", "typescript": "5.9.3", "vitest": "3.2.4" }
}
JSON
  printf 'services:\n  db:\n    image: postgres:17-alpine\n    environment:\n      POSTGRES_PASSWORD: dev\n' > docker-compose.yml
  printf 'DATABASE_URL=postgres://postgres:dev@localhost:5432/store\n' > .env
  printf '{ "compilerOptions": { "strict": true, "noEmit": true, "target": "ES2023", "module": "NodeNext", "moduleResolution": "NodeNext", "allowImportingTsExtensions": true, "skipLibCheck": true, "types": ["node"] }, "include": ["src", "db", "e2e", "*.ts"] }\n' > tsconfig.json
  printf 'node_modules/\n' > .gitignore
  printf 'import pg from "pg";\n\nexport const db = new pg.Pool({ connectionString: process.env.DATABASE_URL });\n' > src/db.ts
  cat > db/migrate.ts <<'TS'
import { db } from "../src/db.ts";

await db.query("CREATE TABLE IF NOT EXISTS products (sku text PRIMARY KEY, name text NOT NULL, price numeric NOT NULL)");
await db.end();
TS
  cat > db/seed.ts <<'TS'
import { db } from "../src/db.ts";

await db.query("INSERT INTO products VALUES ('mug', 'Mug', 10), ('tee', 'T-shirt', 25) ON CONFLICT DO NOTHING");
await db.end();
TS
  cat > src/cart.ts <<'TS'
import { db } from "./db.ts";

export type Item = { sku: string; qty: number };

export async function total(items: Item[]): Promise<number> {
  let sum = 0;
  for (const { sku, qty } of items) {
    const { rows } = await db.query("SELECT price FROM products WHERE sku = $1", [sku]);
    sum += Number(rows[0].price) * qty;
  }
  return sum;
}
TS
  cat > src/cart.test.ts <<'TS'
import { afterAll, expect, test } from "vitest";
import { total } from "./cart.ts";
import { db } from "./db.ts";

afterAll(() => db.end());

test("total uses the prices in the database", async () => {
  expect(await total([{ sku: "mug", qty: 2 }])).toBe(20);
});
TS
  cat > server.ts <<'TS'
import { createServer } from "node:http";
import { total } from "./src/cart.ts";

const page = (qty: number, sum: string) => `<!doctype html><title>Store</title>
<body style="font: 20px system-ui; max-width: 420px; margin: 48px auto; color: #111">
<h1>☕ Mug <small style="color: #888">$10</small></h1>
<a href="?qty=${qty + 1}" style="display: inline-block; padding: 12px 20px; background: #16a34a; color: #fff; border-radius: 8px; text-decoration: none">Add to cart</a>
<p>Items: ${qty}</p><p><b>Total: ${sum}</b></p></body>`;

createServer(async (req, res) => {
  const qty = Number(new URL(req.url ?? "/", "http://x").searchParams.get("qty") ?? 0);
  const sum = await total([{ sku: "mug", qty }]);
  res.setHeader("content-type", "text/html; charset=utf-8");
  res.end(page(qty, `$${sum.toFixed(2)}`));
}).listen(4321);
TS
  cat > playwright.config.ts <<'TS'
import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "e2e",
  use: { baseURL: "http://localhost:4321" },
  webServer: { command: "node server.ts", url: "http://localhost:4321", reuseExistingServer: !process.env.CI },
});
TS
  cat > e2e/cart.spec.ts <<'TS'
import { expect, test } from "@playwright/test";

test("two mugs cost $20.00", async ({ page }) => {
  await page.goto("/");
  await page.getByText("Add to cart").click();
  await page.getByText("Add to cart").click();
  await expect(page.getByText("Total: $20.00")).toBeVisible();
});
TS
  npm install --silent --no-audit --no-fund
  npx playwright install chromium >/dev/null
  docker pull -q postgres:17-alpine >/dev/null
  git init -q && git add -A && git -c user.name=demo -c user.email=demo@example.com commit -qm init
}

if [ "${1:-shop}" = store ]; then
  write_store
elif [ "${1:-shop}" = shop ]; then
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
