#!/usr/bin/env python3
"""doneguard: your coding agent can't say "done" until the checks pass.

Usage:
  doneguard init [agents]  detect the stack, write .doneguard.conf and hook into your agents
                           (default: the agents found here; or name them, or "all")
  doneguard check [files]  run the checks on the given files (default: git changes); exit 1 on failure
  doneguard stats          how many "done" claims were blocked, and how many the agents fixed
  doneguard start          hook, session start: mark the time
  doneguard verify         hook, agent stop: check the files changed since the mark; on failure the
                           errors go back to the agent so it keeps working (gives up after 3 tries)

Agents: claude, cursor, codex, gemini, copilot, antigravity, devin, kiro, opencode, aider.
The hooks read the event JSON on stdin and answer in each agent's format.

.doneguard.conf, one check per line:  <globs> | <dir> | <mode> | <command>
  globs    comma-separated, matched against the path relative to the root (* also crosses folders)
  dir      where the command runs, relative to the root; @<file> = nearest folder holding <file>
           (@package.json runs each package of a monorepo in its own folder)
  mode     files    run `command <changed files>`; fail on non-zero exit
           filter   run `command`; fail only on output lines that cite a changed file
           tests    replace {tests} with the tests related to the changed files; skip if none
           command  run `command`; fail on non-zero exit
A missing tool or a timeout skips the check: doneguard never bricks your agent.
"""
import fnmatch
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

CONF = ".doneguard.conf"
MAX_BLOCKS = 3
TIMEOUT = 150
STATE = Path(tempfile.gettempdir()) / "doneguard"
LOG = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "doneguard" / "log.jsonl"
SKIP_DIRS = {"node_modules", "dist", "build", "target", "coverage", "out", "vendor", "__pycache__", "venv"}
MISSING = ("command not found", "could not determine executable to run", "npx canceled due to missing packages", "Failed to spawn")
TEST_NAME = re.compile(r"^test_|_test\.|_spec\.|\.(test|spec)\.|Tests?\.\w+$")
HEADER = "# doneguard checks, one per line: <globs> | <dir> | <mode> | <command>\n# Modes: files, filter, tests, command. Details: doneguard --help\n"
START, VERIFY = "doneguard start", "doneguard verify"


def load_conf(root):
    f = root / CONF
    if not f.exists():
        return []
    rows = [[p.strip() for p in line.split(" | ", 3)] for line in f.read_text().splitlines() if not line.lstrip().startswith("#")]
    return [r for r in rows if len(r) == 4]


def find_root(path):
    return next((d for d in [path, *path.parents] if (d / CONF).exists()), path)


def sh(cmd, cwd):
    """Run a shell command. Returns (exit code, output); code None means skipped (tool missing, timeout)."""
    env = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"}
    env.update(CI="1", NO_COLOR="1")
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, env=env, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return None, ""
    out = r.stdout + r.stderr
    if r.returncode == 127 or any(m in out for m in MISSING):
        return None, out
    return r.returncode, out


def nearest(path, root, marker):
    """Closest folder from `path` up to `root` that holds `marker`."""
    d = Path(path).parent
    while True:
        if (d / marker).exists():
            return d
        if d == root or root not in d.parents:
            return None
        d = d.parent


def related_tests(rel, base):
    p = Path(rel)
    if TEST_NAME.search(p.name):
        return [rel]
    names = [f"{pre}{p.stem}{post}{p.suffix}" for pre, post in (("test_", ""), ("", "_test"), ("", ".test"), ("", ".spec"), ("", "_spec"), ("", "Test"), ("", "Tests"))]
    found = [t for d in (p.parent, p.parent / "__tests__") for n in names for t in (base / d).glob(n)]
    found += [t for d in ("tests", "test", "Tests", "spec") if (base / d).is_dir() for n in names for t in (base / d).rglob(n)]
    unique = {}  # tests/ and Tests/ are the same folder on case-insensitive disks
    for t in found:
        unique.setdefault(t.stat().st_ino, t)
    return [str(t.relative_to(base)) for t in unique.values()]


def cites(line, rel, path):
    return path in line or re.search(r"(?:^|[\s'\"(]|\./)" + re.escape(rel) + r"\b", line) is not None


def tool(cmd):
    """Short name of the tool a check runs: `npx --no -- vitest related` is vitest."""
    words = [w for w in cmd.split() if not w.startswith("-") and w not in ("npx", "uv", "uvx", "run", "bundle", "exec")]
    return Path(words[0]).name if words else cmd


def run_checks(root, files):
    """Returns ([(command, failure report)], number of checks that ran)."""
    fails, ran = [], 0
    for globs, folder, mode, cmd in load_conf(root):
        groups = {}
        for f in files:
            rel = os.path.relpath(f, root)
            if rel.startswith("..") or not os.path.isfile(f) or not any(fnmatch.fnmatch(rel, g.strip()) for g in globs.split(",")):
                continue
            base = nearest(f, root, folder[1:]) if folder.startswith("@") else (root / folder).resolve()
            if base:
                groups.setdefault(base, []).append(f)
        for base, hits in groups.items():
            rels = [os.path.relpath(h, base) for h in hits]
            full = cmd
            if mode == "files":
                full = f"{cmd} {' '.join(map(shlex.quote, rels))}"
            elif mode == "tests":
                tests = sorted({t for r in rels for t in related_tests(r, base)})
                if not tests:
                    continue
                full = cmd.replace("{tests}", " ".join(map(shlex.quote, tests)))
            code, out = sh(full, base)
            if code is None:
                continue
            ran += 1
            where = os.path.relpath(base, root)
            if mode == "filter":
                bad = list(dict.fromkeys(ln for ln in out.splitlines() if any(cites(ln, r, h) for r, h in zip(rels, hits))))
                if bad:
                    fails.append((cmd, f"[{where}] {cmd}\n" + "\n".join(bad[:20])))
            elif code != 0:
                fails.append((cmd, f"[{where}] {full}\n" + "\n".join(out.splitlines()[-30:])))
    return fails, ran


def message(fails):
    return "doneguard: not done yet. These checks fail on the files you changed. Fix them before finishing:\n\n" + "\n\n".join(t for _, t in fails)


def changed_since(root, since):
    """Files modified after `since`, whoever touched them (edit tools, shell commands, codegen)."""
    try:
        r = subprocess.run(["git", "ls-files", "-co", "--exclude-standard", "-z"], cwd=root, capture_output=True, text=True, check=False)
        paths = [root / p for p in r.stdout.split("\0") if p] if r.returncode == 0 else None
    except OSError:
        paths = None
    if paths is None:
        paths = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
            paths += [Path(d, n) for n in names]
    out = []
    for p in paths:
        try:
            if p.stat().st_mtime > since:
                out.append(str(p))
        except OSError:
            pass
    return out


def git_changed(root):
    out = set()
    for cmd in (["git", "diff", "--name-only", "--relative", "HEAD"], ["git", "ls-files", "-o", "--exclude-standard"]):
        r = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
        if r.returncode == 0:
            out |= {str(root / ln) for ln in r.stdout.splitlines() if ln}
    return sorted(out)


def agent_of(data):
    """Which agent sent the event, told apart by its payload (or the env it sets)."""
    if "conversationId" in data:
        return "antigravity"
    if "cursor_version" in data or "workspace_roots" in data:
        return "cursor"
    if "turn_id" in data:
        return "codex"
    if "sessionId" in data:
        return "copilot"
    for env, name in (("GEMINI_PROJECT_DIR", "gemini"), ("DEVIN_PROJECT_DIR", "devin"), ("CLAUDE_PROJECT_DIR", "claude")):
        if os.environ.get(env):
            return name
    return str(data.get("agent") or "other")


def reply(agent, error=None, gave_up=False):
    """Each agent's way to say "keep working". Most take Claude Code's decision/reason JSON."""
    if agent == "antigravity":
        out = {"decision": "continue", "reason": error} if error else {}
    elif agent == "cursor":
        out = {"followup_message": error} if error else {}
    elif error:
        out = {"decision": "block", "reason": error}
    elif gave_up and agent == "claude":
        out = {"systemMessage": f"doneguard: checks still failing after {MAX_BLOCKS} fix attempts; letting the agent stop."}
    else:
        return 0
    print(json.dumps(out))
    return 0


def log(agent, sid, result, fails):
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a") as f:
            f.write(json.dumps({"t": int(time.time()), "agent": agent, "session": sid, "result": result, "checks": sorted({tool(c) for c, _ in fails})}) + "\n")
    except OSError:
        pass


def hook(event, data):
    agent = agent_of(data)
    sid = data.get("conversationId") or data.get("conversation_id") or data.get("session_id") or data.get("sessionId") or "default"
    sid = re.sub(r"[^\w-]", "_", str(sid))
    STATE.mkdir(parents=True, exist_ok=True)
    since_f, blocks_f = STATE / f"{sid}.since", STATE / f"{sid}.blocks"
    if event == "start" or not since_f.exists():
        since_f.write_text(str(time.time()))
        return reply(agent)
    if data.get("status") in ("aborted", "error") or data.get("terminationReason", "NO_TOOL_CALL") != "NO_TOOL_CALL":
        return reply(agent)
    since = float(since_f.read_text())
    env_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("GEMINI_PROJECT_DIR") or os.environ.get("DEVIN_PROJECT_DIR")
    roots = data.get("workspacePaths") or data.get("workspace_roots") or [env_root or data.get("cwd") or os.getcwd()]
    fails, ran = [], 0
    for root in {find_root(Path(p).resolve()) for p in roots}:
        if (root / CONF).exists():
            f, n = run_checks(root, changed_since(root, since))
            fails, ran = fails + f, ran + n
    blocks = int(blocks_f.read_text()) if blocks_f.exists() else 0
    blocked = bool(fails) and blocks < MAX_BLOCKS
    if ran:
        log(agent, sid, "blocked" if blocked else "gave_up" if fails else "passed", fails)
    if blocked:
        blocks_f.write_text(str(blocks + 1))
        return reply(agent, message(fails))
    since_f.write_text(str(time.time()))
    blocks_f.unlink(missing_ok=True)
    return reply(agent, gave_up=bool(fails))


def common(counter):
    return ", ".join(f"{k} ({v})" for k, v in counter.most_common(4)) or "-"


def stats():
    try:
        rows = [json.loads(line) for line in LOG.read_text().splitlines() if line.strip()]
    except (OSError, ValueError):
        rows = []
    if not rows:
        print("doneguard: no stats yet. They start with your agent's first checked stop.")
        return 0
    last, fixed, gave_up = {}, 0, 0
    for r in rows:
        fixed += last.get(r["session"]) == "blocked" and r["result"] == "passed"
        gave_up += r["result"] == "gave_up"
        last[r["session"]] = r["result"]
    blocked = sum(r["result"] == "blocked" for r in rows)
    ended = fixed + gave_up
    rate = f" ({fixed * 100 // ended}%)" if ended else ""
    print(f"doneguard stats since {time.strftime('%Y-%m-%d', time.localtime(rows[0]['t']))}")
    print(f"  stops checked        {len(rows)} in {len(last)} session(s)")
    print(f'  "done" blocked       {blocked}')
    print(f"  fixed by the agent   {fixed} of {ended}{rate}")
    print(f"  most failing         {common(Counter(t for r in rows if r['result'] == 'blocked' for t in r['checks']))}")
    print(f"  agents               {common(Counter(r['agent'] for r in rows))}")
    if blocked:
        print(f"\nCaught {blocked} broken \"done\" claim(s)" + (f"; the agents fixed {rate.strip(' ()')} on their own." if ended else "."))
    return 0


def detect(root):
    """Checks for the stacks found in the root or one folder down."""
    dirs = [root] + [d for d in sorted(root.iterdir()) if d.is_dir() and d.name not in SKIP_DIRS and not d.name.startswith(".")]

    def found(name):
        return [d for d in dirs if (d / name).exists()]

    def at(name):
        return "." if (root / name).exists() else "@" + name

    checks = []
    pkgs = found("package.json")
    if pkgs:
        deps = set()
        for d in pkgs:
            try:
                meta = json.loads((d / "package.json").read_text())
            except ValueError:
                continue
            deps |= set(meta.get("dependencies", {})) | set(meta.get("devDependencies", {}))
        js = "*.js,*.jsx,*.ts,*.tsx"
        if found("tsconfig.json"):
            checks.append(("*.ts,*.tsx", "@package.json", "filter", "npx --no -- tsc --noEmit --pretty false -p ."))
        if "eslint" in deps:
            checks.append((js, "@package.json", "files", "npx --no -- eslint --quiet"))
        if "vitest" in deps:
            checks.append((js, "@package.json", "files", "npx --no -- vitest related --run --passWithNoTests"))
        elif "jest" in deps:
            checks.append((js, "@package.json", "files", "npx --no -- jest --findRelatedTests --passWithNoTests"))
    marker = next((n for n in ("pyproject.toml", "setup.py", "requirements.txt") if found(n)), None)
    if marker:
        py = found(marker)[0]
        run = "uv run " if (py / "uv.lock").exists() else ".venv/bin/" if (py / ".venv").is_dir() else ""
        checks.append(("*.py", at(marker), "files", f"{run}ruff check --select E9,F63,F7,F82 --output-format concise"))
        checks.append(("*.py", at(marker), "tests", f"{run}pytest -x -q --tb=short {{tests}}"))
    if found("Cargo.toml"):
        checks.append(("*.rs", at("Cargo.toml"), "filter", "cargo check --quiet --message-format short 2>&1 | grep ': error'"))
    if found("go.mod"):
        checks.append(("*.go", at("go.mod"), "filter", "go vet ./..."))
    if found("pom.xml"):
        checks.append(("*.java", at("pom.xml"), "filter", "mvn -q compile 2>&1 | grep ERROR"))
    if found("composer.json"):
        checks.append(("*.php", at("composer.json"), "files", "php -l"))
    if found("Package.swift"):
        checks.append(("*.swift", at("Package.swift"), "filter", "swift build 2>&1 | grep 'error:'"))
    return checks


def group(cmd, matcher=None, **extra):
    g = {"hooks": [{"type": "command", "command": cmd, **extra}]}
    return g if matcher is None else {"matcher": matcher, **g}


# Agents whose hooks are JSON events: config file, keys down to the events, {event: our entry}.
HOOKS = {
    "claude": (".claude/settings.local.json", ["hooks"], {"SessionStart": group(START), "Stop": group(VERIFY, timeout=600)}),
    "codex": (".codex/hooks.json", ["hooks"], {"SessionStart": group(START, "startup|resume"), "Stop": group(VERIFY, timeout=600)}),
    "gemini": (".gemini/settings.json", ["hooks"], {"SessionStart": group(START, "*"), "AfterAgent": group(VERIFY, "*", timeout=600000)}),
    "devin": (".devin/hooks.v1.json", [], {"SessionStart": group(START, ""), "Stop": group(VERIFY, "", timeout=600)}),
    "cursor": (".cursor/hooks.json", ["hooks"], {"sessionStart": {"command": START}, "stop": {"command": VERIFY, "timeout": 600, "loop_limit": MAX_BLOCKS}}),
    "copilot": (".github/hooks/doneguard.json", ["hooks"], {"sessionStart": {"type": "command", "command": START, "timeoutSec": 30}, "agentStop": {"type": "command", "command": VERIFY, "timeoutSec": 600}}),
    "antigravity": (".agents/hooks.json", ["doneguard"], {"SessionStart": {"type": "command", "command": START}, "Stop": {"type": "command", "command": VERIFY, "timeout": 600}}),
}
# Agent: (binaries, project path) that tell init the agent is used here.
AGENTS = {
    "claude": (("claude",), ".claude"),
    "cursor": (("cursor-agent", "cursor"), ".cursor"),
    "codex": (("codex",), ".codex"),
    "gemini": (("gemini",), ".gemini"),
    "copilot": (("copilot",), ".github/hooks"),
    "antigravity": (("agy", "antigravity"), ".agents"),
    "devin": (("devin",), ".devin"),
    "kiro": (("kiro-cli", "kiro"), ".kiro"),
    "opencode": (("opencode",), ".opencode"),
    "aider": (("aider",), ".aider.conf.yml"),
}
IMPORTS_CLAUDE = {"cursor", "copilot", "devin"}  # they also run the hooks in .claude/settings*.json
KIRO = {"version": "v1", "hooks": [
    {"name": "doneguard-start", "trigger": "SessionStart", "action": {"type": "command", "command": START}},
    {"name": "doneguard-verify", "trigger": "Stop", "action": {"type": "command", "command": VERIFY}, "timeout": 600},
]}
OPENCODE = """// doneguard for OpenCode: when the agent goes idle, run the checks and send failures back as a prompt.
export const Doneguard = async ({ client, directory }) => {
  const run = async (event, id) => {
    try {
      const p = Bun.spawn(["doneguard", event], { cwd: directory, stdout: "pipe", stdin: new Blob([JSON.stringify({ agent: "opencode", session_id: id, cwd: directory })]) })
      const out = (await new Response(p.stdout).text()).trim()
      return out ? JSON.parse(out) : {}
    } catch {
      return {}
    }
  }
  return {
    event: async ({ event }) => {
      if (event.type === "session.created") await run("start", event.properties.info?.id)
      if (event.type !== "session.idle") return
      const r = await run("verify", event.properties.sessionID)
      if (r.decision === "block") await client.session.prompt({ path: { id: event.properties.sessionID }, body: { parts: [{ type: "text", text: r.reason }] } })
    },
  }
}
"""


def edit_json(path, change):
    data = json.loads(path.read_text()) if path.exists() else {}
    change(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


def install(root, agent):
    """Register the hooks in the agent's own config. Returns (file, note)."""
    if agent in HOOKS:
        file, keys, events = HOOKS[agent]

        def change(data):
            if agent in ("cursor", "copilot"):
                data.setdefault("version", 1)
            node = data
            for k in keys:
                node = node.setdefault(k, {})
            for event, entry in events.items():
                entries = node.setdefault(event, [])
                if "doneguard" not in json.dumps(entries):
                    entries.append(entry)

        edit_json(root / file, change)
        return file, "approve it once with /hooks in Codex" if agent == "codex" else ""
    if agent == "kiro":
        edit_json(root / ".kiro/hooks/doneguard.json", lambda data: data.update(KIRO))
        return ".kiro/hooks/doneguard.json", ""
    if agent == "opencode":
        (root / ".opencode/plugins").mkdir(parents=True, exist_ok=True)
        (root / ".opencode/plugins/doneguard.js").write_text(OPENCODE)
        return ".opencode/plugins/doneguard.js", "experimental"
    f = root / ".aider.conf.yml"
    text = f.read_text() if f.exists() else ""
    if "lint-cmd" in text or "auto-lint" in text:
        return ".aider.conf.yml", "it already sets lint-cmd/auto-lint: set `lint-cmd: doneguard check` yourself"
    f.write_text(text + ("\n" if text and not text.endswith("\n") else "") + "lint-cmd: doneguard check\nauto-lint: true\n")
    return ".aider.conf.yml", ""


def init(root, names=()):
    names = list(AGENTS) if list(names) == ["all"] else list(names)
    names = names or [a for a, (bins, hint) in AGENTS.items() if any(shutil.which(b) for b in bins) or (root / hint).exists()] or ["claude"]
    unknown = sorted(set(names) - set(AGENTS))
    if unknown:
        print(f"error: unknown agent(s): {', '.join(unknown)}. Known: {', '.join(AGENTS)}, all.")
        return 2
    if load_conf(root):
        print(f"{CONF} already has checks, leaving it as is.")
    else:
        checks = detect(root)
        (root / CONF).write_text(HEADER + "".join(" | ".join(c) + "\n" for c in checks))
        print(f"wrote {CONF} with {len(checks)} check(s)" + (":" if checks else " (no stack found yet: run init again once the project has one)."))
        for c in checks:
            print("  " + " | ".join(c))
    claude_file = root / HOOKS["claude"][0]
    claude_hooked = "claude" in names or (claude_file.exists() and "doneguard" in claude_file.read_text())
    print("hooked into:")
    for agent in names:
        if agent in IMPORTS_CLAUDE and claude_hooked:
            print(f"  {agent:<12} runs the Claude Code hooks too, nothing to add")
            continue
        try:
            file, note = install(root, agent)
        except ValueError:
            print(f"  {agent:<12} skipped: its config is not valid JSON, fix it and run init again")
            continue
        print(f"  {agent:<12} {file}" + (f" ({note})" if note else ""))
    if not shutil.which("doneguard"):
        print("warning: `doneguard` is not on your PATH, so the hooks can't run. Install it: uv tool install doneguard")
    return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    if cmd in ("start", "verify"):
        try:
            data = json.load(sys.stdin)
        except ValueError:
            data = {}
        return hook(cmd, data if isinstance(data, dict) else {})
    if cmd == "init":
        return init(Path.cwd().resolve(), argv[1:])
    if cmd == "stats":
        return stats()
    if cmd == "check":
        root = find_root(Path.cwd().resolve())
        files = [str(Path(a).resolve()) for a in argv[1:]] or git_changed(root)
        fails, _ = run_checks(root, files)
        print(message(fails) if fails else f"doneguard: all checks pass ({len(files)} changed file(s)).")
        return 1 if fails else 0
    print(__doc__.strip())
    return 0 if cmd in ("", "-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main())
