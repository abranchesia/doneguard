#!/usr/bin/env python3
"""doneguard: your coding agent can't say "done" until the checks pass.

Usage:
  doneguard init [agents]  detect the stack, write .doneguard.conf and hook into your agents
                           (default: the agents found here; or name them, or "all")
  doneguard check [files]  run the checks on the given files (default: git changes); exit 1 on failure
  doneguard stats          how many "done" claims were blocked, and how many the agents fixed
  doneguard --version      the installed version
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
           tests    replace {tests} (or {test_dirs}) with the tests related to the changed files;
                    skip if none
           command  run `command`; fail on non-zero exit
           e2e      run `command` and keep screenshots, videos and traces in .doneguard/evidence/
                    ({evidence} is that run's folder); fail on non-zero exit
           postgres, mysql, redis, mongo (optionally =<image>)
                    start a throwaway database in Docker right before the next check runs, point
                    DATABASE_URL (and the like) at it for every check below, then run `command` to
                    migrate and seed it (- for nothing). Removed when the checks end. If it can't
                    start, or its command fails, the checks below it are skipped.
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
E2E_TIMEOUT = 420
EVIDENCE = Path(".doneguard") / "evidence"
KEEP_EVIDENCE = 10
STATE = Path(tempfile.gettempdir()) / "doneguard"
LOG = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "doneguard" / "log.jsonl"
SKIP_DIRS = {"node_modules", "dist", "build", "target", "coverage", "out", "vendor", "__pycache__", "venv"}
MISSING = (
    "command not found", "could not determine executable to run", "npx canceled due to missing packages", "Failed to spawn",
    "Cannot connect to the Docker daemon", "failed to connect to the docker API",  # Docker is off
    "npx playwright install", "Executable doesn't exist at", "Cypress binary is missing",  # browsers not installed
    "is already used, make sure that nothing is running on the port",  # your dev server holds the port
)
# Throwaway databases: default image, port, container env, server command, readiness probe, URL, env vars it fills.
# Every variable whose value uses the same scheme (in the project's .env files or your shell), and every URL-like
# variable the ORM config reads (Prisma env("…"), process.env.…), is pointed at it too.
SERVICES = {
    "postgres": ("postgres:17-alpine", 5432, "POSTGRES_PASSWORD=doneguard POSTGRES_DB=test", "postgres -c fsync=off -c synchronous_commit=off",
                 "pg_isready -h 127.0.0.1 -U postgres -d test", "postgresql://postgres:doneguard@127.0.0.1:{port}/test", ("DATABASE_URL",)),
    "mysql": ("mysql:8.4", 3306, "MYSQL_ROOT_PASSWORD=doneguard MYSQL_DATABASE=test", "mysqld",
              "mysqladmin ping -h 127.0.0.1 -uroot -pdoneguard --silent", "mysql://root:doneguard@127.0.0.1:{port}/test", ("DATABASE_URL",)),
    "redis": ("redis:7-alpine", 6379, "", "redis-server", "redis-cli ping", "redis://127.0.0.1:{port}", ("REDIS_URL",)),
    "mongo": ("mongo:7", 27017, "", "mongod", "mongosh --quiet --eval 'db.runCommand({ping:1})'", "mongodb://127.0.0.1:{port}/test", ("MONGODB_URI", "MONGO_URL")),
}
LIFETIME = 900  # a database outlives a killed run by 15 minutes at most
PLAYWRIGHT = """// Written by doneguard for one run, then deleted: your config, plus evidence of every test.
{base}
const use = {{ screenshot: "on", video: "on", trace: "retain-on-failure" }};
export default {{
  ...base,
  outputDir: {results},
  reporter: [["line"], ["html", {{ outputFolder: {report}, open: "never" }}]],
  use: {{ ...base.use, ...use }},
  ...(base.projects && {{ projects: base.projects.map((p) => ({{ ...p, use: {{ ...p.use, ...use }} }})) }}),
}};
"""
TEST_NAME = re.compile(r"^test_|_test\.|_spec\.|\.(test|spec)\.|Tests?\.\w+$")
HEADER = "# doneguard checks, one per line: <globs> | <dir> | <mode> | <command>\n# Modes: files, filter, tests, command, e2e, postgres, mysql, redis, mongo. Details: doneguard --help\n"
START, VERIFY = "doneguard start", "doneguard verify"
WRAPPER = ".doneguard.playwright.config.ts"


def load_conf(root):
    f = root / CONF
    if not f.exists():
        return []
    rows = [[p.strip() for p in line.split(" | ", 3)] for line in f.read_text().splitlines() if not line.lstrip().startswith("#")]
    return [r for r in rows if len(r) == 4]


def find_root(path):
    return next((d for d in [path, *path.parents] if (d / CONF).exists()), path)


def sh(cmd, cwd, extra=None, timeout=TIMEOUT):
    """Run a shell command. Returns (exit code, output); code None means skipped (tool missing, timeout).
    `extra` adds environment variables; a None value removes one."""
    env = {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"}
    env.update(CI="1", NO_COLOR="1")
    for k, v in (extra or {}).items():
        if v is None:
            env.pop(k, None)
        else:
            env[k] = v
    try:
        r = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env, check=False)
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


def docker(*args):
    """Run docker. Returns (exit code, stdout on success or the error)."""
    try:
        r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except subprocess.TimeoutExpired:
        return 1, f"docker {args[0]} took over {TIMEOUT} s; pulling the image? `docker pull` it once"
    except OSError:
        return 1, "Docker is not installed"
    return r.returncode, (r.stdout if r.returncode == 0 else r.stderr).strip()


ORM_CONFIGS = ("prisma/**/*.prisma", "schema.prisma", "prisma.config.*", "drizzle.config.*", "knexfile.*")


def url_vars(d):
    """URL-like variables an ORM config reads: env("DATABASE_URL"), process.env.DIRECT_URL…"""
    text = read(*(p for g in ORM_CONFIGS for p in d.glob(g)))
    found = re.findall(r"""env\(\s*["'](\w+)["']|process\.env\.(\w+)|process\.env\[["'](\w+)["']\]""", text)
    return {n for m in found for n in m if n and re.search(r"URL|URI|DSN", n, re.I)}


def env_aliases(dirs, scheme):
    """Variables that hold a `scheme`… URL in the project's .env files or your shell, plus those the ORM config reads."""
    names = {k for k, v in os.environ.items() if v.startswith(scheme) and "://" in v}
    for d in dirs:
        names |= url_vars(d)
        for f in d.glob(".env*"):
            names |= {m[1] for m in re.finditer(r"^\s*(?:export\s+)?(\w+)\s*=\s*['\"]?([\w+]+)://", read(f), re.M) if m[2].startswith(scheme)}
    return names


def start_service(mode, dirs):
    """Start a throwaway database in Docker. Returns (container id, env for the checks) or (None, why not)."""
    kind, _, image = mode.partition("=")
    default, port, cenv, server, ready, url, names = SERVICES[kind]
    flags = [x for e in cenv.split() for x in ("-e", e)]
    code, out = docker("run", "-d", "--rm", "--label", "doneguard", "-p", f"127.0.0.1::{port}", *flags,
                       "--entrypoint", "timeout", image or default, str(LIFETIME), "docker-entrypoint.sh", *server.split())
    if code != 0:
        return None, (out.splitlines() or ["docker run failed"])[-1]
    cid = out.splitlines()[-1]
    deadline = time.time() + 90
    while True:
        code, out = docker("exec", cid, *shlex.split(ready))
        if code == 0:
            break
        if time.time() > deadline or "is not running" in out or "No such container" in out:
            docker("rm", "-fv", cid)
            return None, f"{kind} did not get ready ({(out.splitlines() or ['timed out'])[-1][:80]})"
        time.sleep(0.3)
    code, out = docker("port", cid, f"{port}/tcp")
    host_port = out.splitlines()[0].rpartition(":")[2] if code == 0 and out else ""
    if not host_port.isdigit():
        docker("rm", "-fv", cid)
        return None, "docker port gave no port"
    u = url.format(port=host_port)
    env = {n: u for n in set(names) | env_aliases(dirs, kind)}
    if kind == "postgres":
        env.update(PGHOST="127.0.0.1", PGPORT=host_port, PGUSER="postgres", PGPASSWORD="doneguard", PGDATABASE="test")
    return cid, env


def playwright_config(base, ev):
    """Your Playwright config, wrapped to save a screenshot and video of every test (and traces of failures) in `ev`."""
    cfg = next((base / f"playwright.config.{x}" for x in ("ts", "js", "mts", "mjs", "cts", "cjs") if (base / f"playwright.config.{x}").is_file()), None)
    imp = f'import base from "./{cfg.name}";' if cfg else "const base = {};"
    wrapper = base / WRAPPER  # TypeScript: Playwright loads it, and through it any kind of config
    wrapper.write_text(PLAYWRIGHT.format(base=imp, results=json.dumps(str(ev / "results")), report=json.dumps(str(ev / "report"))))
    return wrapper


def e2e(cmd, base, root, env):
    """Run an end-to-end suite, keeping its evidence. Returns (exit code, output, evidence folder)."""
    runs = root / EVIDENCE
    ev = runs / time.strftime("%Y-%m-%d_%H-%M-%S")
    if base != root:
        ev = ev / os.path.relpath(base, root).replace(os.sep, "-")
    ev.mkdir(parents=True, exist_ok=True)
    (runs.parent / ".gitignore").write_text("*\n")
    for old in sorted(p for p in runs.iterdir() if p.is_dir())[:-KEEP_EVIDENCE]:
        shutil.rmtree(old, ignore_errors=True)
    wrapper = None
    if re.search(r"\bplaywright test\b", cmd) and not re.search(r"(^|\s)(-c|--config)\b", cmd):
        wrapper = playwright_config(base, ev)
        cmd += f" -c {wrapper.name}"
    # No test database: CI unset, like a run in your terminal, so Playwright reuses the dev server you may have
    # running. With one: CI set, so it starts its own server pointed at it (skipped if your dev server holds the port).
    extra = {**env, "NO_COLOR": None, "PLAYWRIGHT_HTML_OPEN": "never", **({} if env else {"CI": None})}
    try:
        code, out = sh(cmd.replace("{evidence}", shlex.quote(str(ev))), base, extra, E2E_TIMEOUT)
    finally:
        if wrapper:
            wrapper.unlink(missing_ok=True)
    if code is None:
        shutil.rmtree(ev, ignore_errors=True)
        if ev.parent != runs:
            try:
                ev.parent.rmdir()  # only if empty
            except OSError:
                pass
    return code, out, ev


def evidence_note(root, ev):
    """Where the evidence is, and the files worth opening first (failure screenshots, page snapshots)."""
    look = sorted(p for p in ev.rglob("*.png") if "fail" in p.name) + sorted(ev.rglob("error-context.md"))
    lines = [f"Evidence (screenshots, videos, traces): {os.path.relpath(ev, root)}/"]
    lines += [f"  open: {os.path.relpath(p, root)}" for p in look[:6]]
    if (ev / "report").is_dir():
        lines.append(f"  report: npx playwright show-report {os.path.relpath(ev / 'report', root)}")
    return "\n".join(lines)


def run_checks(root, files, say=None):
    """Returns ([(command, failure report)], number of checks that ran, notes for the user).
    `say` gets a progress line per check."""
    fails, ran, notes = [], 0, []
    env, pending, started, containers, down = {}, [], set(), [], False

    def progress(ok, name, t0):
        if say:
            say(f"  {'✓' if ok else '✗'} {name:<16} {time.time() - t0:5.1f}s")

    def bring_up():
        """Start the databases listed above the check about to run, then migrate and seed them. False if one failed."""
        nonlocal ran
        while pending:
            smode, scmd, sbase = pending.pop(0)
            kind, t0 = smode.partition("=")[0], time.time()
            if smode not in started:
                cid, got = start_service(smode, {root, sbase})
                if cid is None:
                    notes.append(f"{kind} skipped ({got}); the checks below it in {CONF} were skipped too")
                    return False
                started.add(smode)
                containers.append(cid)
                env.update(got)
                progress(True, kind, t0)
            if scmd.strip() in ("", "-"):
                continue
            t0 = time.time()
            code, out = sh(scmd, sbase, {"NODE_ENV": "test", **env})
            if code is None:  # tests on an empty database would only fail for nothing
                notes.append(f"{kind} setup skipped (tool missing or too slow); the checks below it were skipped too")
                return False
            ran += 1
            progress(code == 0, f"{kind} setup", t0)
            if code:  # a broken migration or seed: report the cause, not the tests failing after it
                fails.append((scmd, f"[{os.path.relpath(sbase, root)}] {kind} setup: {scmd}\n{tail(out)}"))
                return False
        return True

    for leftover in [*root.glob(WRAPPER), *root.glob(f"*/{WRAPPER}")]:  # from a run that was killed
        leftover.unlink(missing_ok=True)
    files = [f for f in files if not f.endswith(WRAPPER)]
    try:
        for globs, folder, mode, cmd in load_conf(root):
            groups = {}
            for f in files:
                rel = os.path.relpath(f, root)
                if rel.startswith("..") or not os.path.isfile(f) or not any(fnmatch.fnmatch(rel, g.strip()) for g in globs.split(",")):
                    continue
                base = nearest(f, root, folder[1:]) if folder.startswith("@") else (root / folder).resolve()
                if base:
                    groups.setdefault(base, []).append(f)
            if mode.partition("=")[0] in SERVICES:
                pending += [(mode, cmd, base) for base in groups]  # started only if a check below runs
                continue
            for base, hits in groups.items():
                rels = [os.path.relpath(h, base) for h in hits]
                full = cmd
                if mode == "files":
                    full = f"{cmd} {' '.join(map(shlex.quote, rels))}"
                elif mode == "tests":
                    tests = sorted({t for r in rels for t in related_tests(r, base)})
                    if not tests:
                        continue
                    dirs = sorted({"./" + os.path.dirname(t) if os.path.dirname(t) else "." for t in tests})
                    full = cmd.replace("{tests}", " ".join(map(shlex.quote, tests))).replace("{test_dirs}", " ".join(map(shlex.quote, dirs)))
                if pending and not down:
                    down = not bring_up()
                if down:
                    continue
                t0, ev = time.time(), None
                if mode == "e2e":
                    code, out, ev = e2e(full, base, root, env)
                else:
                    code, out = sh(full, base, env)
                if code is None:
                    why = next((ln.strip(" ║│") for ln in out.splitlines() if any(m in ln for m in MISSING)), "tool not found" if out else "timed out")
                    if say:
                        say(f"  – {tool(cmd):<16} skipped: {why[:70]}")
                    if mode == "e2e":
                        notes.append(f"e2e skipped ({why.removeprefix('Error: ')[:120]})")
                    continue
                ran += 1
                where = os.path.relpath(base, root)
                if mode == "filter":
                    bad = list(dict.fromkeys(ln for ln in out.splitlines() if any(cites(ln, r, h) for r, h in zip(rels, hits))))
                    progress(not bad, tool(cmd), t0)
                    if bad:
                        fails.append((cmd, f"[{where}] {cmd}\n" + "\n".join(bad[:20])))
                    continue
                progress(code == 0, tool(cmd), t0)
                if code != 0 and ev:  # the evidence paths are listed once, below
                    out = "\n".join(ln for ln in out.splitlines() if not re.search(r"attachment #|──────|Usage:|show-trace|To open last HTML report|\.doneguard/evidence", ln))
                    fails.append((cmd, f"[{where}] {full}\n{tail(out, 40)}\n{evidence_note(root, ev)}"))
                elif code != 0:
                    fails.append((cmd, f"[{where}] {full}\n{tail(out)}"))
                elif ev:
                    notes.append(f"e2e passed, evidence in {os.path.relpath(ev, root)}")
    finally:
        for cid in containers:
            docker("rm", "-fv", cid)
    return fails, ran, notes


def tail(out, n=30):
    """The end of a long output, plus its first error line if that scrolled out of view."""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    if len(lines) <= n:
        return "\n".join(lines)
    first = next((ln for ln in lines[:-n] if re.search(r"error", ln, re.I)), None)
    return "\n".join([first, "..."][: 2 if first else 0] + lines[-n:])


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


def reply(agent, error=None, gave_up=False, notes=()):
    """Each agent's way to say "keep working". Most take Claude Code's decision/reason JSON.
    Claude Code also shows the user `notes` (skipped databases, where the e2e evidence went)."""
    notes = [f"checks still failing after {MAX_BLOCKS} fix attempts; letting the agent stop"] * gave_up + list(notes)
    if agent == "antigravity":
        out = {"decision": "continue", "reason": error} if error else {}
    elif agent == "cursor":
        out = {"followup_message": error} if error else {}
    elif error:
        out = {"decision": "block", "reason": error}
    elif notes and agent == "claude":
        out = {"systemMessage": "doneguard: " + "; ".join(notes) + "."}
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
    fails, ran, notes = [], 0, []
    for root in {find_root(Path(p).resolve()) for p in roots}:
        if (root / CONF).exists():
            f, n, msgs = run_checks(root, changed_since(root, since))
            fails, ran, notes = fails + f, ran + n, notes + msgs
    blocks = int(blocks_f.read_text()) if blocks_f.exists() else 0
    blocked = bool(fails) and blocks < MAX_BLOCKS
    if ran:
        log(agent, sid, "blocked" if blocked else "gave_up" if fails else "passed", fails)
    if blocked:
        blocks_f.write_text(str(blocks + 1))
        return reply(agent, message(fails))
    since_f.write_text(str(time.time()))
    blocks_f.unlink(missing_ok=True)
    return reply(agent, gave_up=bool(fails), notes=notes)


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


# Dependencies (npm, PyPI, gems) that mean the tests talk to a database, and the images that run one.
DB_DEPS = {
    "postgres": {"pg", "postgres", "pg-promise", "psycopg", "psycopg2", "psycopg2-binary", "asyncpg"},
    "mysql": {"mysql", "mysql2", "mysqlclient", "pymysql", "aiomysql"},
    "redis": {"redis", "ioredis", "bullmq"},
    "mongo": {"mongodb", "mongoose", "pymongo", "motor", "mongoid"},
}
DB_IMAGES = {"postgres": {"postgres", "postgis", "pgvector", "timescaledb"}, "mysql": {"mysql"}, "redis": {"redis"}, "mongo": {"mongo"}}
COMPOSE = ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml")
JS = "*.js,*.jsx,*.ts,*.tsx"
UI = JS + ",*.vue,*.svelte,*.css,*.html"


def read(*paths):
    """The text of the files that exist, joined."""
    out = []
    for p in paths:
        try:
            out.append(p.read_text(errors="ignore"))
        except OSError:  # missing, a folder, unreadable
            pass
    return "".join(out)


def words(text):
    """Package names in a dependency file, comments and keywords left out."""
    text = re.sub(r"#.*|^\s*(keywords|description|classifiers)\s*=.*", "", text, flags=re.M)
    return set(re.findall(r"[a-z0-9_.-]+", text.lower()))


def js_setup(d, deps, meta):
    """Migrate and seed a fresh database: the ORM's own deploy command, then the project's seed script.
    Only when the connection visibly comes from a URL variable doneguard redirects: the ORM config reads one, or
    (for plain scripts) the .env files hold one. Otherwise "-", and you write the command yourself."""
    scripts = meta.get("scripts", {})
    migrate = next((n for n in ("db:migrate", "migrate") if n in scripts), None)
    seed = next((n for n in ("db:seed", "seed") if n in scripts), None)
    orm = bool(deps & {"prisma", "@prisma/client", "drizzle-kit", "knex"})
    if not (url_vars(d) if orm else re.search(r"=\s*['\"]?(postgres|mysql)\w*://", read(*d.glob(".env*")))):
        return "-"
    steps = []
    if deps & {"prisma", "@prisma/client"}:
        steps.append("npx --no -- prisma migrate deploy" if any(d.glob("prisma/**/migrations")) else "npx --no -- prisma db push")
    elif "drizzle-kit" in deps:
        steps.append("npx --no -- drizzle-kit push")
    elif "knex" in deps:
        steps.append("npx --no -- knex migrate:latest")
    elif migrate:
        steps.append(f"npm run {migrate}")
    if seed:
        steps.append(f"npm run {seed}")
    elif deps & {"prisma", "@prisma/client"} and ("seed" in meta.get("prisma", {}) or "seed" in read(*d.glob("prisma.config.*"))):
        steps.append("npx --no -- prisma db seed")
    return " && ".join(steps) or "-"


def detect(root, notes=None):
    """Checks for the stacks found in the root or one folder down. Checks that need a database go last, so
    Docker being off never skips the others. `notes` gets what init should tell you."""
    notes = [] if notes is None else notes
    dirs = [root] + [d for d in sorted(root.iterdir()) if d.is_dir() and d.name not in SKIP_DIRS and not d.name.startswith(".")]

    def found(name):
        return [d for d in dirs if (d / name).exists()]

    def at(name):
        return "." if (root / name).exists() else "@" + name

    def rel(d):
        return os.path.relpath(d, root)

    images = {}  # the database images docker compose already uses: same version, same extensions
    for image in re.findall(r"^\s*image:\s*['\"]?([^\s'\"#]+)", read(*(d / n for d in dirs for n in COMPOSE)), re.M):
        *ns, repo = image.split("@")[0].split(":")[0].split("/")
        if "$" in image or (ns and ns[-1] not in ("library", "pgvector", "postgis", "timescale")):
            continue  # ${VARS}, and images like bitnami's that start differently
        for kind, repos in DB_IMAGES.items():
            if repo in repos:
                images.setdefault(kind, f"{kind}={image}")

    def services(deps, folder, setup):
        """`*`: a database starts only when a check below it runs, whatever changed. One SQL database per stack."""
        sql = next((k for k in ("postgres", "mysql") if k in deps), None) or next((k for k in ("postgres", "mysql") if deps & DB_DEPS[k]), None)
        kinds = [k for k in SERVICES if k == sql or (k in ("redis", "mongo") and deps & DB_DEPS[k])]
        if sql and setup == "-":
            notes.append(f"{sql}: write its migrate and seed command in {CONF} (doneguard couldn't tell that it connects through a URL variable)")
        return [("*", folder, images.get(k, k), setup if k == sql else "-") for k in kinds]

    checks, late = [], []
    pkgs = found("package.json")
    if pkgs:
        deps, metas = set(), {}
        for d in pkgs:
            try:
                meta = json.loads((d / "package.json").read_text())
            except ValueError:
                continue
            metas[d] = (set(meta.get("dependencies", {})) | set(meta.get("devDependencies", {})), meta)
            deps |= metas[d][0]
        if found("tsconfig.json"):
            checks.append(("*.ts,*.tsx", "@package.json", "filter", "npx --no -- tsc --noEmit --pretty false -p ."))
        if "eslint" in deps:
            checks.append((JS, "@package.json", "files", "npx --no -- eslint --quiet"))
        elif "@biomejs/biome" in deps:
            checks.append((JS, "@package.json", "files", "npx --no -- biome lint --diagnostic-level=error --no-errors-on-unmatched"))
        tests = e2e = None
        if "vitest" in deps:
            tests = (JS, "@package.json", "files", "npx --no -- vitest related --run --passWithNoTests")
        elif "jest" in deps:
            tests = (JS, "@package.json", "files", "npx --no -- jest --findRelatedTests --passWithNoTests")
        pw = next((d for d in dirs if any(d.glob("playwright.config.*"))), None)
        cy = next((d for d in dirs if any(d.glob("cypress.config.*"))), None)
        if pw and "@playwright/test" in deps and "webServer" in read(*pw.glob("playwright.config.*")):
            e2e = (UI, rel(pw), "e2e", "npx --no -- playwright test")
        elif pw and "@playwright/test" in deps:
            notes.append("Playwright: its config has no webServer, so the tests need an app you start by hand; add an e2e line once it starts the app itself")
        elif cy and "cypress" in deps:
            notes.append("Cypress: add an e2e line that starts your app, e.g. npx --no -- start-server-and-test dev http://localhost:3000 'cypress run --config screenshotsFolder={evidence}/screenshots,videosFolder={evidence}/videos,video=true'")
        if tests or e2e:
            db = next((d for d, (mine, _) in metas.items() if mine & (set().union(*DB_DEPS.values()) | {"prisma", "@prisma/client"})), pkgs[0])
            mine, meta = metas.get(db, (set(), {}))
            schema = read(*db.glob("prisma/**/*.prisma"), db / "schema.prisma")
            kinds = {k for k, p in (("postgres", "postgresql"), ("mysql", "mysql")) if re.search(rf'provider\s*=\s*"{p}"', schema)}
            needs = services(mine | kinds, rel(db), js_setup(db, mine, meta))
            (late if needs else checks).extend([*needs, *(c for c in (tests, e2e) if c)])
    marker = next((n for n in ("pyproject.toml", "setup.py", "requirements.txt") if found(n)), None)
    if marker:
        py = found(marker)[0]
        run = "uv run " if (py / "uv.lock").exists() else ".venv/bin/" if (py / ".venv").is_dir() else ""
        pydeps = words(read(py / "pyproject.toml", py / "setup.py", py / "Pipfile", *py.glob("requirements*.txt")))
        checks.append(("*.py", at(marker), "files", f"{run}ruff check --select E9,F63,F7,F82 --output-format concise"))
        alembic = (py / "alembic.ini").exists() and re.search(r"environ|getenv|settings", read(*py.glob("*/env.py")))
        needs = services(pydeps, rel(py), f"{run}alembic upgrade head" if alembic else "-")
        tests = [("*.py", at(marker), "tests", f"{run}pytest -x -q --tb=short {{tests}}")]
        e2e_dir = next((p for p in ("tests/e2e", "e2e") if (py / p).is_dir()), None)
        if "pytest-playwright" in pydeps and e2e_dir:
            tests.append(("*.py,*.html,*.css,*.js", at(marker), "e2e", f"{run}pytest -q --tb=short {e2e_dir} --screenshot on --video on --tracing retain-on-failure --output {{evidence}}"))
        (late if needs else checks).extend([*needs, *tests])
    gemfile = next(iter(found("Gemfile")), None)
    gems = set(re.findall(r"""^\s*gem\s+["']([\w-]+)""", read(gemfile / "Gemfile"), re.M)) if gemfile else set()
    if gems:
        if "rubocop" in gems:
            checks.append(("*.rb", at("Gemfile"), "files", "bundle exec rubocop --format emacs --fail-level error --force-exclusion"))
        runner = "bundle exec rspec {tests}" if gems & {"rspec", "rspec-rails"} else "bin/rails test {tests}" if "rails" in gems else None
        needs = services(gems, rel(gemfile), "RAILS_ENV=test bin/rails db:prepare" if "rails" in gems else "-") if runner else []
        (late if needs else checks).extend([*needs, *([("*.rb", at("Gemfile"), "tests", runner)] if runner else [])])
    if found("Cargo.toml"):
        checks.append(("*.rs", at("Cargo.toml"), "filter", "cargo check --quiet --message-format short 2>&1 | grep ': error'"))
    if found("go.mod"):
        checks.append(("*.go", at("go.mod"), "filter", "go vet ./..."))
        checks.append(("*.go", at("go.mod"), "tests", "go test {test_dirs}"))
    if found("pom.xml"):
        checks.append(("*.java", at("pom.xml"), "filter", "mvn -q compile 2>&1 | grep ERROR"))
    gradle = next((d for d in dirs if (d / "gradlew").exists() and any(d.glob("build.gradle*"))), None)
    if gradle:
        checks.append(("*.java,*.kt", rel(gradle), "filter", "./gradlew -q --console=plain classes 2>&1"))
    dotnet = next((p for d in dirs for p in sorted(d.glob("*.sln")) + sorted(d.glob("*.csproj"))), None)
    if dotnet:
        checks.append(("*.cs", rel(dotnet.parent), "filter", f"dotnet build {shlex.quote(dotnet.name)} --nologo -v q -clp:ErrorsOnly 2>&1"))
    if found("composer.json"):
        checks.append(("*.php", at("composer.json"), "files", "php -l"))
        composer = read(*(d / "composer.json" for d in found("composer.json")))
        runner = "pest" if '"pestphp/pest"' in composer else "phpunit" if '"phpunit/phpunit"' in composer else None
        if runner:
            checks.append(("*.php", at("composer.json"), "tests", f"vendor/bin/{runner} {{tests}}"))
    deno = next((n for n in ("deno.json", "deno.jsonc") if found(n)), None)
    if deno:
        checks.append((JS, at(deno), "files", "deno check"))
        checks.append((JS, at(deno), "tests", "deno test -A {tests}"))
    if found("Package.swift"):
        checks.append(("*.swift", at("Package.swift"), "filter", "swift build 2>&1 | grep 'error:'"))
    return checks + late


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


def table(checks):
    """Checks as .doneguard.conf lines, columns aligned."""
    widths = [max((len(c[i]) for c in checks), default=0) for i in range(3)]
    return [" | ".join([*(c[i].ljust(widths[i]) for i in range(3)), c[3]]) for c in checks]


def init(root, names=()):
    names = list(AGENTS) if list(names) == ["all"] else list(names)
    names = names or [a for a, (bins, hint) in AGENTS.items() if any(shutil.which(b) for b in bins) or (root / hint).exists()] or ["claude"]
    unknown = sorted(set(names) - set(AGENTS))
    if unknown:
        print(f"error: unknown agent(s): {', '.join(unknown)}. Known: {', '.join(AGENTS)}, all.")
        return 2
    notes = []
    checks, conf = detect(root, notes), load_conf(root)
    if conf:
        have = {(r[2], r[3]) for r in conf}
        shown = [c for c in checks if (c[2], c[3]) not in have]
        print(f"{CONF} already has checks, leaving it as is." + (" Detected here but not in it (add the ones you want):" if shown else ""))
    else:
        shown = checks
        (root / CONF).write_text(HEADER + "".join(ln + "\n" for ln in table(checks)))
        print(f"wrote {CONF} with {len(checks)} check(s)" + (":" if checks else " (no stack found yet: run init again once the project has one)."))
    for c in shown:
        print("  " + " | ".join(c))
    for n in notes:
        print(f"note: {n}")
    kinds = [c[2].partition("=")[0] for c in checks if c[2].partition("=")[0] in SERVICES]
    if kinds and docker("info")[0] != 0:
        print(f"note: {', '.join(kinds)} for the tests runs in Docker, which isn't running. Until it is, the checks below it are skipped.")
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "ms-playwright"
    if any("playwright test" in c[3] for c in checks) and not any(Path(cache).glob("chromium*")):
        print("note: Playwright has no browsers yet, the e2e check is skipped until you run: npx playwright install chromium")
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
    if cmd in ("--version", "-V", "version"):
        try:
            from importlib.metadata import version
            print(f"doneguard {version('doneguard')}")
        except ImportError:  # PackageNotFoundError too: run from a checkout, not installed
            print("doneguard (not installed: run from source)")
        return 0
    if cmd == "check":
        root = find_root(Path.cwd().resolve())
        files = [str(Path(a).resolve()) for a in argv[1:]] or git_changed(root)
        fails, ran, notes = run_checks(root, files, say=print)
        for n in notes:
            print(f"doneguard: {n}.")
        print(("\n" if ran else "") + (message(fails) if fails else f"doneguard: all checks pass ({len(files)} changed file(s)).").rstrip())
        return 1 if fails else 0
    print(__doc__.strip())
    return 0 if cmd in ("", "-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main())
