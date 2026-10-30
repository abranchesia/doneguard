import json
import os
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
    fails, ran = doneguard.run_checks(root, [str(root / "a.py")])
    assert fails and ran == 1


def test_unmatched_files_are_ignored(tmp_path):
    root = project(tmp_path / "p", "*.py | . | files | false\n", ["a.ts"])
    assert doneguard.run_checks(root, [str(root / "a.ts")]) == ([], 0)


def test_missing_tool_is_skipped(tmp_path):
    root = project(tmp_path / "p", "*.py | . | command | no-such-tool-xyz\n", ["a.py"])
    assert doneguard.run_checks(root, [str(root / "a.py")]) == ([], 0)


def test_filter_mode_only_reports_changed_files(tmp_path):
    cmd = "printf 'src/a.ts(1,1): error TS1\\nsrc/b.ts(2,2): error TS2\\nsrc/b.ts(2,2): error TS2\\n'"
    root = project(tmp_path / "p", f"*.ts | . | filter | {cmd}\n", ["src/a.ts", "src/b.ts"])
    ((_, report),), _ = doneguard.run_checks(root, [str(root / "src/b.ts")])
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
    assert doneguard.load_conf(tmp_path) == [["*.go", ".", "filter", "go vet ./..."]]


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
