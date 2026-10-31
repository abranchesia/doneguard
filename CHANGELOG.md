# Changelog

## [Unreleased]

### Added

- **`doneguard init --global`:** install and forget. It hooks doneguard into your agents' user config (Claude Code, Codex, Gemini CLI, Copilot, Antigravity, Kiro CLI V3; Cursor and Devin run Claude Code's), once per machine.
  - The first session in a git repo writes that repo's `.doneguard.conf` and keeps it out of git (`.git/info/exclude`). The quick checks are on; databases, the tests that need them and e2e are commented out.
  - Projects hooked on their own keep their hooks, and the global ones step aside there, so nothing runs twice.
  - `init` in a project no longer adds hooks for an agent that is hooked for every project.

## [0.5.0] - 2026-10-30

### Added

- A `secrets` group in `cheats`: AWS, Anthropic, OpenAI, Stripe, GitHub, Slack and Google keys, and private keys, added in the session to a file git would commit. They're shown cut short, so the key doesn't spread to the agent's transcript or a PR comment. `init` adds the group to new projects. In an existing `.doneguard.conf`, add `secrets` to the `cheats` line (a line with `-` already has it); `init --update` leaves that line as it is.
- README FAQ: why not a hand-written stop hook.

### Changed

- Codex CLI and GitHub Copilot CLI are tested end to end in real sessions (✅), with recordings in the README.

## [0.4.0] - 2026-10-30

### Added

- **Receipt:** when everything passes, Claude Code shows what doneguard checked under the agent's answer: `doneguard: ✓ cheats · tsc · vitest (3.1 s)`.
- `doneguard report [--base REF] [files]`: the checks as markdown, for a PR comment (`| gh pr comment --body-file -`) or a GitHub Actions summary (`>> $GITHUB_STEP_SUMMARY`).
- A "checked by doneguard" README badge; `init` points to it.
- `doneguard init --update`: adds the checks a new version detects to an existing `.doneguard.conf`, each where it works, without touching your lines.
- `doneguard doctor` says when a newer doneguard is out (one request to pypi.org, only when you run it).

## [0.3.0] - 2026-10-30

### Added

- **No shortcuts:** a `cheats` mode, which `init` adds. It sends back lines added in the session that skip or focus tests, silence the type checker or linter, or use `any`, plus test files deleted in the session. It compares against a git snapshot taken at the start, flags each finding once, and `doneguard check` only lists them.
- **New errors in files the agent didn't touch:** when a session starts, doneguard records in the background what `filter` checks (tsc, cargo, go vet…) already report. Any error that wasn't there counts, in any file.
- `doneguard doctor`: what the checks need and what's missing.
- `doneguard check --base <ref>`: everything the branch changed, for CI.
- `doneguard --version`.
- Integration CI: a real Postgres, vitest and Playwright run on the demo project for every push (`demo/integration.sh`).

## [0.2.0] - 2026-10-30

### Added

- **Throwaway databases for tests.** New modes `postgres`, `mysql`, `redis` and `mongo` (optionally `=<image>`). The database starts in Docker right before the first check below it that runs. `DATABASE_URL`, same-kind URLs from `.env` files and the shell, and the URL variables the ORM config reads are pointed at it. Then the migrate and seed command runs with `NODE_ENV=test`, and the container is removed at the end (or after 15 minutes if the run is killed).
- **`e2e` mode with evidence.** Playwright runs with a wrapper config that keeps a screenshot and a video of every test, plus traces of failures, in `.doneguard/evidence/`. The last 10 runs are kept. Failures point the agent at the failure screenshot and the page snapshot. `{evidence}` works with any other runner.
- `init` detects databases from dependencies, the Prisma provider and `docker-compose` images. It writes the migrate and seed command for Prisma, Drizzle, Knex, npm scripts, Alembic and Rails, but only when the connection goes through a variable it redirects. Database rows go last.
- New stacks: Ruby (rubocop, rspec, `rails test`), `go test`, Gradle, .NET, Pest and PHPUnit, Deno, Biome, pytest-playwright.
- `{test_dirs}` placeholder for runners that take folders.
- `doneguard check` prints a progress line per check.
- Skips (Docker off, missing browsers, a dev server holding the port) are shown in Claude Code.
- With a `.doneguard.conf` that already has checks, `init` lists the detections it doesn't have yet.
- Published on PyPI: `uv tool install doneguard`.

### Changed

- `.doneguard.conf` is written with aligned columns.
- A failing check shows the end of its output plus its first error line, with Playwright's attachment noise removed.

## [0.1.0] - 2026-10-30

### Added

- Stop hook for Claude Code, Cursor, Codex, Gemini CLI, Copilot, Antigravity, Devin, Kiro, OpenCode and Aider.
- `doneguard check` for CI and git hooks, and `doneguard stats`.
- Detection for TypeScript, ESLint, Vitest, Jest, Python (ruff, pytest), Rust, Go, Java (Maven), PHP and Swift.

[Unreleased]: https://github.com/abranchesia/doneguard/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/abranchesia/doneguard/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/abranchesia/doneguard/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/abranchesia/doneguard/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/abranchesia/doneguard/releases/tag/v0.2.0
[0.1.0]: https://github.com/abranchesia/doneguard/commit/42ef2b2
