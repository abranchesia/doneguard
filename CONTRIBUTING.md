# Contributing

Thanks for helping. doneguard is small on purpose: one file (`doneguard.py`), Python 3.9+, standard library only. Changes that keep it that way are the easiest to merge.

## Set up

```bash
git clone https://github.com/abranchesia/doneguard && cd doneguard
uvx pytest -q                         # the tests, no other setup
python3 doneguard.py --help           # run it from the checkout
```

The tests use a fake `docker` on the `PATH`, so they don't need Docker. To try the real thing, `source demo/setup.sh store` builds a demo project with Postgres and Playwright in `/tmp/doneguard-demo/store`. It needs npm and Docker.

## What helps most

- **Reports from agents marked 🧪** (Cursor, Codex, Gemini CLI, Copilot, Antigravity, Devin, Kiro, OpenCode, Aider). Open an [agent report](https://github.com/abranchesia/doneguard/issues/new?template=agent-report.yml), whether it worked or not.
- **New stack detections.** Add the rule to `detect()` and a case to `tests/test_doneguard.py`. A check must only fail for problems in the changed files: use `filter` mode for whole-project tools, and a `tests` placeholder for test runners.
- **Agent adapters.** A new agent needs an entry in `HOOKS` (or its own branch in `install()`), a line in `AGENTS`, and its "keep working" format in `reply()`.

## Rules of thumb

- A missing tool, Docker that is off or a timeout skips a check. It never fails one, so doneguard never blocks an agent for something the agent can't fix.
- Nothing leaves the machine. No network calls of doneguard's own, no telemetry.
- Code reads like the code around it: short functions, a docstring when the "why" isn't obvious.

## GIFs

The GIFs in `demo/` are recorded with [VHS](https://github.com/charmbracelet/vhs): `vhs demo/check.tape`. `claude.tape` records a real Claude Code session and needs Claude Code logged in.

## Releasing

1. Move the `[Unreleased]` notes in `CHANGELOG.md` under the new version.
2. Bump `version` in `pyproject.toml` and commit.
3. Run `git tag vX.Y.Z && git push origin vX.Y.Z`. The release workflow runs the tests and publishes to PyPI.
4. Run `gh release create vX.Y.Z --notes-from-tag` (or paste the changelog section).
