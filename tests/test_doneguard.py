import json
import os
import subprocess
import time

import pytest

import doneguard


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(doneguard, "STATE", tmp_path / "state")
    monkeypatch.setattr(doneguard, "LOG", tmp_path / "log.jsonl")
    for env in ("CLAUDE_PROJECT_DIR", "GEMINI_PROJECT_DIR", "DEVIN_PROJECT_DIR"):
        monkeypatch.delenv(env, raising=False)


def project(root, conf, files=()):
    root.mkdir(exist_ok=True)
    (root / doneguard.CONF).write_text(conf)
    for name in files:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text("")
    return root


def touch_later(path):
    later = time.time() + 5
    path.write_text("")
    os.utime(path, (later, later))


def test_files_mode_fails_on_nonzero_exit(tmp_path):
    root = project(tmp_path / "p", "*.py | . | files | false\n", ["a.py"])
    fails, ran, _ = doneguard.run_checks(root, [str(root / "a.py")])
    assert fails and ran == 1


def test_unmatched_files_are_ignored(tmp_path):
    root = project(tmp_path / "p", "*.py | . | files | false\n", ["a.ts"])
    assert doneguard.run_checks(root, [str(root / "a.ts")]) == ([], 0, [])


def test_missing_tool_is_skipped(tmp_path):
    root = project(tmp_path / "p", "*.py | . | command | no-such-tool-xyz\n", ["a.py"])
    assert doneguard.run_checks(root, [str(root / "a.py")]) == ([], 0, [])


def test_filter_mode_only_reports_changed_files(tmp_path):
    cmd = "printf 'src/a.ts(1,1): error TS1\\nsrc/b.ts(2,2): error TS2\\nsrc/b.ts(2,2): error TS2\\n'"
    root = project(tmp_path / "p", f"*.ts | . | filter | {cmd}\n", ["src/a.ts", "src/b.ts"])
    ((_, report),), _, _ = doneguard.run_checks(root, [str(root / "src/b.ts")])
    assert report.splitlines()[1:] == ["src/b.ts(2,2): error TS2"]


def test_cites_common_formats_without_prefix_collisions():
    assert doneguard.cites("src/a.ts(3,1): error TS2322", "src/a.ts", "/x/src/a.ts")
    assert doneguard.cites("vet: ./pkg/a.go:3:2: undefined: x", "pkg/a.go", "/x/pkg/a.go")
    assert doneguard.cites("[ERROR] /x/src/A.java:[5,9] cannot find symbol", "src/A.java", "/x/src/A.java")
    assert not doneguard.cites("src/a.tsx(3,1): error", "src/a.ts", "/x/src/a.ts")
    assert not doneguard.cites("src/data.ts(3,1): error", "a.ts", "/x/a.ts")


def test_related_tests_finds_conventional_names(tmp_path):
    for name in ("app/cart.py", "tests/unit/test_cart.py", "web/cart.ts", "web/cart.test.ts", "src/Cart.php", "tests/CartTest.php"):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text("")
    assert doneguard.related_tests("app/cart.py", tmp_path) == ["tests/unit/test_cart.py"]
    assert doneguard.related_tests("web/cart.ts", tmp_path) == ["web/cart.test.ts"]
    assert doneguard.related_tests("src/Cart.php", tmp_path) == ["tests/CartTest.php"]
    assert doneguard.related_tests("tests/unit/test_cart.py", tmp_path) == ["tests/unit/test_cart.py"]


def test_tool_names():
    assert doneguard.tool("npx --no -- vitest related --run") == "vitest"
    assert doneguard.tool(".venv/bin/ruff check --select E9") == "ruff"
    assert doneguard.tool("uv run pytest -x {tests}") == "pytest"


def test_claude_stop_hook_blocks_then_gives_up(tmp_path, monkeypatch, capsys):
    root = project(tmp_path / "proj", "*.py | . | files | false\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    event = {"session_id": "s1"}
    assert doneguard.hook("start", event) == 0
    assert doneguard.hook("verify", event) == 0
    assert capsys.readouterr().out == ""
    touch_later(root / "a.py")
    for _ in range(doneguard.MAX_BLOCKS):
        assert doneguard.hook("verify", event) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["decision"] == "block" and "not done yet" in out["reason"]
    doneguard.hook("verify", event)
    assert "systemMessage" in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("event, expected", [
    ({"conversationId": "c1"}, {"decision": "continue"}),
    ({"conversation_id": "c2", "cursor_version": "2.0"}, {"followup_message": None}),
    ({"session_id": "c3", "turn_id": "t1"}, {"decision": "block"}),
    ({"sessionId": "c4"}, {"decision": "block"}),
    ({"session_id": "c5", "agent": "opencode"}, {"decision": "block"}),
])
def test_each_agent_gets_its_own_continue_format(tmp_path, capsys, event, expected):
    root = project(tmp_path / "proj", "*.py | . | files | false\n")
    event = {**event, "workspacePaths": [str(root)], "workspace_roots": [str(root)], "cwd": str(root)}
    if "cursor_version" not in event:
        event.pop("workspace_roots")
    if "conversationId" not in event:
        event.pop("workspacePaths")
    doneguard.hook("start", event)
    capsys.readouterr()
    touch_later(root / "a.py")
    doneguard.hook("verify", event)
    out = json.loads(capsys.readouterr().out)
    assert set(out) == set(expected) | ({"reason"} if "decision" in expected else set())
    assert all(out[k] == v for k, v in expected.items() if v)


def test_aborted_stops_are_not_checked(tmp_path, capsys):
    root = project(tmp_path / "proj", "*.py | . | files | false\n")
    event = {"conversation_id": "c1", "workspace_roots": [str(root)], "status": "aborted"}
    doneguard.hook("start", event)
    touch_later(root / "a.py")
    doneguard.hook("verify", event)
    assert json.loads(capsys.readouterr().out.splitlines()[-1]) == {}


def test_stats_counts_blocks_and_fixes(tmp_path, monkeypatch, capsys):
    root = project(tmp_path / "proj", "*.py | . | command | test -f ok\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    event = {"session_id": "s1"}
    doneguard.hook("start", event)
    touch_later(root / "a.py")
    doneguard.hook("verify", event)
    (root / "ok").write_text("")
    doneguard.hook("verify", event)
    capsys.readouterr()
    doneguard.stats()
    out = capsys.readouterr().out
    assert '"done" blocked       1' in out and "fixed by the agent   1 of 1 (100%)" in out and "claude (2)" in out


def test_detect_monorepo_and_more_stacks(tmp_path):
    (tmp_path / "web").mkdir()
    (tmp_path / "web/package.json").write_text(json.dumps({"devDependencies": {"vitest": "1", "typescript": "5"}}))
    (tmp_path / "web/tsconfig.json").write_text("{}")
    for marker in ("pyproject.toml", "pom.xml", "composer.json", "Package.swift"):
        (tmp_path / marker).write_text("")
    checks = doneguard.detect(tmp_path)
    assert ("*.ts,*.tsx", "@package.json", "filter", "npx --no -- tsc --noEmit --pretty false -p .") in checks
    assert any("vitest related" in c[3] for c in checks)
    assert any(c[:3] == ("*.py", ".", "tests") for c in checks)
    assert {c[0] for c in checks} >= {"*.java", "*.php", "*.swift"}


def test_init_detects_again_once_an_empty_project_gets_a_stack(tmp_path):
    doneguard.init(tmp_path, ["claude"])
    assert doneguard.load_conf(tmp_path) == []
    (tmp_path / "go.mod").write_text("module demo\n")
    doneguard.init(tmp_path, ["claude"])
    assert doneguard.load_conf(tmp_path)[1:] == [["*.go", ".", "filter", "go vet ./..."], ["*.go", ".", "tests", "go test {test_dirs}"]]
    assert doneguard.load_conf(tmp_path)[0][2] == "cheats"


def test_init_is_idempotent_and_keeps_existing_settings(tmp_path):
    settings = tmp_path / ".claude/settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
    doneguard.init(tmp_path, ["claude"])
    doneguard.init(tmp_path, ["claude"])
    data = json.loads(settings.read_text())
    assert data["permissions"] == {"allow": ["Bash(ls)"]}
    assert len(data["hooks"]["Stop"]) == 1 and len(data["hooks"]["SessionStart"]) == 1


def test_init_writes_each_agent_config(tmp_path):
    (tmp_path / ".aider.conf.yml").write_text("model: x")
    assert doneguard.init(tmp_path, ["cursor", "codex", "gemini", "copilot", "antigravity", "kiro", "opencode", "aider"]) == 0
    def read(f):
        return json.loads((tmp_path / f).read_text())

    assert read(".cursor/hooks.json")["hooks"]["stop"] == [{"command": "doneguard verify", "timeout": 600, "loop_limit": 3}]
    assert read(".codex/hooks.json")["hooks"]["Stop"][0]["hooks"][0]["command"] == "doneguard verify"
    assert read(".gemini/settings.json")["hooks"]["AfterAgent"][0]["hooks"][0]["timeout"] == 600000
    assert read(".github/hooks/doneguard.json")["hooks"]["agentStop"][0]["command"] == "doneguard verify"
    assert read(".agents/hooks.json")["doneguard"]["Stop"][0]["command"] == "doneguard verify"
    assert read(".kiro/hooks/doneguard.json")["hooks"][1]["trigger"] == "Stop"
    assert "session.idle" in (tmp_path / ".opencode/plugins/doneguard.js").read_text()
    assert (tmp_path / ".aider.conf.yml").read_text() == "model: x\nlint-cmd: doneguard check\nauto-lint: true\n"


def test_agents_that_import_claude_hooks_are_not_hooked_twice(tmp_path, capsys):
    doneguard.init(tmp_path, ["claude", "cursor", "copilot"])
    assert not (tmp_path / ".cursor").exists() and not (tmp_path / ".github").exists()
    assert "runs the Claude Code hooks too" in capsys.readouterr().out


def test_init_rejects_unknown_agents(tmp_path):
    assert doneguard.init(tmp_path, ["vim"]) == 2


FAKE_DOCKER = """#!/bin/sh
echo "$@" >> "$DOCKER_LOG"
case "$1" in
  run) [ -n "$DOCKER_DOWN" ] && { echo "Cannot connect to the Docker daemon at unix:///var/run/docker.sock" >&2; exit 1; }; echo cid42 ;;
  port) echo 127.0.0.1:55432 ;;
esac
"""


@pytest.fixture
def fake_docker(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "docker").chmod(0o755)
    log = tmp_path / "docker.log"
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("DOCKER_LOG", str(log))
    return log


def test_database_starts_for_the_checks_below_it_and_is_removed(tmp_path, fake_docker):
    url = "postgresql://postgres:doneguard@127.0.0.1:55432/test"
    conf = (
        "*.ts | . | postgres=pgvector/pgvector:pg17 | test \"$DATABASE_URL\" = " + url + " && touch seeded\n"
        f"*.ts | . | command | test -f seeded && test \"$POSTGRES_PRISMA_URL\" = {url} && test \"$PGPORT\" = 55432\n"
    )
    root = project(tmp_path / "p", conf, ["a.ts"])
    (root / ".env").write_text('POSTGRES_PRISMA_URL="postgres://prod.example.com/app"\nAPI_URL=https://example.com\n')
    assert doneguard.run_checks(root, [str(root / "a.ts")]) == ([], 2, [])
    calls = fake_docker.read_text()
    assert "pgvector/pgvector:pg17" in calls and "-p 127.0.0.1::5432" in calls and "rm -fv cid42" in calls


def test_database_is_not_started_when_no_check_below_runs(tmp_path, fake_docker):
    root = project(tmp_path / "p", "*.py | . | postgres | -\n*.py | . | tests | false {tests}\n", ["a.py"])
    assert doneguard.run_checks(root, [str(root / "a.py")]) == ([], 0, [])
    assert not fake_docker.exists()


def test_without_docker_the_checks_below_the_database_are_skipped(tmp_path, fake_docker, monkeypatch):
    monkeypatch.setenv("DOCKER_DOWN", "1")
    root = project(tmp_path / "p", "*.py | . | files | true\n*.py | . | postgres | -\n*.py | . | files | false\n", ["a.py"])
    fails, ran, notes = doneguard.run_checks(root, [str(root / "a.py")])
    assert (fails, ran) == ([], 1) and "postgres skipped (Cannot connect to the Docker daemon" in notes[0]


def test_a_broken_seed_is_reported_instead_of_the_tests_after_it(tmp_path, fake_docker):
    root = project(tmp_path / "p", "*.py | . | mysql | echo 'seed.sql: syntax error'; exit 1\n*.py | . | files | false\n", ["a.py"])
    ((cmd, report),), ran, _ = doneguard.run_checks(root, [str(root / "a.py")])
    assert ran == 1 and "mysql setup" in report and "syntax error" in report
    assert "rm -fv cid42" in fake_docker.read_text()


def test_e2e_keeps_evidence_and_points_the_agent_at_it(tmp_path):
    cmd = "mkdir -p {evidence}/results/cart && touch {evidence}/results/cart/test-failed-1.png && test -z \"$CI\" && exit 1"
    root = project(tmp_path / "p", f"*.tsx | . | e2e | {cmd}\n", ["App.tsx"])
    ((_, report),), ran, _ = doneguard.run_checks(root, [str(root / "App.tsx")])
    runs = list((root / ".doneguard/evidence").iterdir())
    assert ran == 1 and len(runs) == 1
    assert f"open: .doneguard/evidence/{runs[0].name}/results/cart/test-failed-1.png" in report
    assert (root / ".doneguard/.gitignore").read_text() == "*\n"


def test_e2e_keeps_only_the_last_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(doneguard, "KEEP_EVIDENCE", 2)
    root = project(tmp_path / "p", "*.css | . | e2e | true\n", ["a.css"])
    for old in ("2020-01-01_00-00-00", "2020-01-02_00-00-00"):
        (root / ".doneguard/evidence" / old).mkdir(parents=True)
    _, _, notes = doneguard.run_checks(root, [str(root / "a.css")])
    runs = sorted(p.name for p in (root / ".doneguard/evidence").iterdir())
    assert len(runs) == 2 and runs[0] == "2020-01-02_00-00-00" and "e2e passed" in notes[0]


def test_playwright_runs_with_a_config_that_records_evidence(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Fake playwright: checks the wrapper config it was given, then fails.
    (bin_dir / "playwright").write_text('#!/bin/sh\n[ "$3" = .doneguard.playwright.config.ts ] && grep -q \'import base from "./playwright.config.ts"\' "$3" && grep -q \'screenshot: "on"\' "$3" && echo wrapped\nexit 1\n')
    (bin_dir / "playwright").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    root = project(tmp_path / "p", "*.tsx | . | e2e | playwright test\n", ["App.tsx", "playwright.config.ts"])
    ((_, report),), _, _ = doneguard.run_checks(root, [str(root / "App.tsx")])
    assert report.splitlines()[:2] == ["[.] playwright test", "wrapped"]
    assert not (root / ".doneguard.playwright.config.ts").exists()


def test_test_dirs_placeholder_for_package_based_runners(tmp_path):
    root = project(tmp_path / "p", "*.go | . | tests | echo {test_dirs}; exit 1\n", ["cart/cart.go", "cart/cart_test.go", "main.go", "main_test.go"])
    ((_, report),), _, _ = doneguard.run_checks(root, [str(root / "cart/cart.go"), str(root / "main.go")])
    assert report.splitlines()[1] == ". ./cart"


def test_detect_databases_e2e_and_seed(tmp_path):
    (tmp_path / "docker-compose.yml").write_text("services:\n  db:\n    image: postgres:16-alpine\n  cache:\n    image: redis:7\n")
    (tmp_path / "package.json").write_text(json.dumps({
        "scripts": {"db:seed": "tsx prisma/seed.ts"},
        "dependencies": {"@prisma/client": "6", "ioredis": "5"},
        "devDependencies": {"prisma": "6", "vitest": "3", "@playwright/test": "1"},
    }))
    (tmp_path / "prisma/migrations").mkdir(parents=True)
    (tmp_path / "prisma/schema.prisma").write_text('datasource db {\n  provider = "postgresql"\n  url = env("DATABASE_URL")\n}\n')
    (tmp_path / "playwright.config.ts").write_text('export default { webServer: { command: "npm run dev" } };\n')
    checks = doneguard.detect(tmp_path)
    modes = [c[2] for c in checks]
    assert modes == ["cheats", "postgres=postgres:16-alpine", "redis=redis:7", "files", "e2e"]
    assert checks[1][3] == "npx --no -- prisma migrate deploy && npm run db:seed" and checks[2][3] == "-"
    assert checks[4][1:] == (".", "e2e", "npx --no -- playwright test") and "*.css" in checks[4][0]


def test_detect_python_ruby_go_and_friends(tmp_path):
    (tmp_path / "requirements.txt").write_text("django\npsycopg[binary]>=3\npytest-playwright\n")
    (tmp_path / "tests/e2e").mkdir(parents=True)
    (tmp_path / "Gemfile").write_text('gem "rails"\ngem "pg"\ngem "rubocop", require: false\n')
    (tmp_path / "go.mod").write_text("module x\n")
    (tmp_path / "composer.json").write_text('{"require-dev": {"pestphp/pest": "^3"}}')
    (tmp_path / "App.sln").write_text("")
    checks = doneguard.detect(tmp_path)
    assert ("*", ".", "postgres", "-") in checks
    assert any(c[2] == "e2e" and "tests/e2e --screenshot on" in c[3] for c in checks)
    assert ("*", ".", "postgres", "RAILS_ENV=test bin/rails db:prepare") in checks
    assert ("*.rb", ".", "tests", "bin/rails test {tests}") in checks
    assert ("*.go", ".", "tests", "go test {test_dirs}") in checks
    assert ("*.php", ".", "tests", "vendor/bin/pest {tests}") in checks
    assert any(c[3].startswith("dotnet build App.sln") for c in checks)


def test_init_lists_new_detections_without_touching_the_conf(tmp_path, capsys):
    (tmp_path / "go.mod").write_text("module x\n")
    project(tmp_path, "*.go | . | filter | go vet ./...\n")
    doneguard.init(tmp_path, ["claude"])
    out = capsys.readouterr().out
    assert "Detected here but not in it" in out and "go test {test_dirs}" in out
    assert (tmp_path / doneguard.CONF).read_text() == "*.go | . | filter | go vet ./...\n"


def test_claude_hears_about_skipped_databases(tmp_path, fake_docker, monkeypatch, capsys):
    monkeypatch.setenv("DOCKER_DOWN", "1")
    root = project(tmp_path / "proj", "*.py | . | postgres | -\n*.py | . | files | false\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    doneguard.hook("start", {"session_id": "s9"})
    touch_later(root / "a.py")
    doneguard.hook("verify", {"session_id": "s9"})
    assert "postgres skipped" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_a_missing_setup_tool_skips_the_tests_instead_of_running_them_on_an_empty_database(tmp_path, fake_docker):
    root = project(tmp_path / "p", "*.py | . | postgres | no-such-migrator up\n*.py | . | files | false\n", ["a.py"])
    fails, ran, notes = doneguard.run_checks(root, [str(root / "a.py")])
    assert (fails, ran) == ([], 0) and "postgres setup skipped" in notes[0]


def test_the_orm_config_and_the_shell_say_which_variables_to_redirect(tmp_path, fake_docker, monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://prod.example.com/app")
    url = "postgresql://postgres:doneguard@127.0.0.1:55432/test"
    root = project(tmp_path / "p", f"*.ts | . | postgres | -\n*.ts | . | command | test \"$SUPABASE_DB_URL\" = {url} && test \"$SHADOW_URL\" = {url}\n", ["a.ts"])
    (root / "prisma").mkdir()
    (root / "prisma/schema.prisma").write_text('datasource db {\n  url = env("SUPABASE_DB_URL")\n  shadowDatabaseUrl = env("SHADOW_URL")\n}\n')
    assert doneguard.run_checks(root, [str(root / "a.ts")]) == ([], 1, [])


def test_a_wrapper_left_by_a_killed_run_is_removed_and_not_checked(tmp_path):
    root = project(tmp_path / "p", "*.ts | . | files | false\n", ["a.ts"])
    (root / doneguard.WRAPPER).write_text("")
    assert doneguard.run_checks(root, [str(root / doneguard.WRAPPER)]) == ([], 0, [])
    assert not (root / doneguard.WRAPPER).exists()


def test_detect_writes_no_setup_it_cannot_redirect(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"pg": "8", "drizzle-orm": "1"}, "devDependencies": {"drizzle-kit": "1", "vitest": "3"}, "scripts": {"db:seed": "tsx seed.ts"}}))
    (tmp_path / "drizzle.config.ts").write_text("export default { dbCredentials: { host: process.env.DB_HOST } };\n")
    notes = []
    assert ("*", ".", "postgres", "-") in doneguard.detect(tmp_path, notes)
    assert "couldn't tell" in notes[0]
    (tmp_path / "drizzle.config.ts").write_text("export default { dbCredentials: { url: process.env.DATABASE_URL! } };\n")
    assert ("*", ".", "postgres", "npx --no -- drizzle-kit push && npm run db:seed") in doneguard.detect(tmp_path)


def test_detect_ignores_comments_odd_images_and_keeps_databases_last(tmp_path):
    (tmp_path / "Gemfile").write_text('gem "rails"\ngem "rspec-rails"\n# gem "redis"\n')
    (tmp_path / "pyproject.toml").write_text('[project]\nkeywords = ["redis", "postgres"]\ndependencies = ["asyncpg"]\n')
    (tmp_path / "compose.yaml").write_text("services:\n  db:\n    image: postgres:${PG_VERSION:-16}\n  cache:\n    image: bitnami/redis:7\n")
    (tmp_path / "go.mod").write_text("module x\n")
    checks = doneguard.detect(tmp_path)
    modes = [c[2] for c in checks]
    assert "redis" not in modes and modes.count("postgres") == 1
    assert modes.index("postgres") > max(i for i, c in enumerate(checks) if c[3].startswith("go "))
    assert ("*.rb", ".", "tests", "bundle exec rspec {tests}") in checks


def test_version_never_fails(capsys):
    assert doneguard.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("doneguard ")


def repo(root, files):
    """A git repo with these files committed; returns the commit."""
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        subprocess.run(g + args, cwd=root, check=True)
    return doneguard.snapshot(root)


def test_cheats_are_flagged_once_then_only_reported(tmp_path):
    root = project(tmp_path / "p", "* | . | cheats | -\n")
    ref = repo(root, {"src/cart.ts": "export const a = 1;\n", "src/cart.test.ts": "test('a', () => {});\n", "src/old.test.ts": "test('b', () => {});\n"})
    (root / "src/cart.ts").write_text("export const a = 1;\n// @ts-ignore\nexport const b = (x as any) + skip();\n")
    (root / "src/cart.test.ts").write_text("test('a', () => {});\ntest.skip('c', () => {});\n")
    (root / "src/old.test.ts").unlink()
    files = [str(root / "src/cart.ts"), str(root / "src/cart.test.ts")]
    flagged = set()
    ((_, report),), ran, _ = doneguard.run_checks(root, files, ref=ref, flagged=flagged)
    assert ran == 1
    assert "src/cart.ts:2  @ts-ignore" in report and "src/cart.ts:3  as any" in report
    assert "src/cart.test.ts:2  test.skip" in report and "src/old.test.ts  deleted" in report
    assert "skip()" not in report  # not a test file
    fails, _, notes = doneguard.run_checks(root, files, ref=ref, flagged=flagged)
    assert fails == [] and notes[0].startswith("kept after being told")
    fails, _, notes = doneguard.run_checks(root, files, ref=ref)  # doneguard check: report, don't fail
    assert fails == [] and notes[0].startswith("cheats: ")


def test_cheat_groups_can_be_turned_off(tmp_path):
    root = project(tmp_path / "p", "* | . | cheats | skips\n")
    ref = repo(root, {"a.ts": ""})
    (root / "a.ts").write_text("// eslint-disable-next-line\nconst x = 1 as any;\n")
    assert doneguard.run_checks(root, [str(root / "a.ts")], ref=ref, flagged=set())[0] == []


def test_new_errors_in_untouched_files_count_old_ones_dont(tmp_path):
    out = tmp_path / "out.txt"
    out.write_text("src/old.ts(1,1): error TS1: old\n")
    root = project(tmp_path / "p", f"*.ts | . | filter | cat {out}\n", ["src/a.ts", "src/old.ts", "src/caller.ts"])
    known = doneguard.known_errors(root)
    out.write_text("src/old.ts(9,9): error TS1: old\nsrc/caller.ts(4,2): error TS2345: wrong type\n")
    ((_, report),), _, _ = doneguard.run_checks(root, [str(root / "src/a.ts")], known=known)
    assert report.endswith("New errors in files you didn't change (your change caused them):\nsrc/caller.ts(4,2): error TS2345: wrong type")
    assert "TS1: old" not in report


def test_a_session_remembers_its_snapshot_and_its_flags(tmp_path, monkeypatch, capsys):
    root = project(tmp_path / "p", "*.py | . | cheats | -\n")
    repo(root, {"tests/test_a.py": "def test_a(): pass\n"})
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    doneguard.hook("start", {"session_id": "s7"})
    touch_later(root / "tests/test_a.py")
    (root / "tests/test_a.py").write_text("import pytest\n@pytest.mark.skip\ndef test_a(): pass\n")
    os.utime(root / "tests/test_a.py", (time.time() + 5, time.time() + 5))
    doneguard.hook("verify", {"session_id": "s7"})
    assert "@pytest.mark.skip" in json.loads(capsys.readouterr().out)["reason"]
    doneguard.hook("verify", {"session_id": "s7"})
    assert "kept after being told" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_doctor_names_what_is_missing(tmp_path, capsys):
    project(tmp_path, "*.py | . | files | no-such-linter-xyz\n*.py | . | weird | x\n")
    assert doneguard.doctor(tmp_path) == 1
    out = capsys.readouterr().out
    assert "✗ no-such-linter-xyz: not found" in out and "✗ mode `weird`" in out and "hooked into no agent" in out
