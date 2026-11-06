#!/usr/bin/env python3
"""doneguard: your coding agent can't say "done" until the checks pass.

Usage:
  doneguard init [--update] [agents]
                           detect the stack, write .doneguard.conf and hook into your agents
                           (default: the agents found here; or name them, or "all"). With a
                           .doneguard.conf already there, --update adds the checks it doesn't have
  doneguard init --global [agents]
                           hook into your agents for every project, once per machine: each git repo
                           gets a .doneguard.conf, kept out of git, on its first session
  doneguard check [--base REF] [files]
                           run the checks on the given files (default: the git changes, or everything
                           changed since the branch left REF); exit 1 on failure
  doneguard report [--base REF] [files]
                           the same checks, written as markdown for a pull request or a CI summary
  doneguard doctor         what the checks here need, and what's missing
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
           filter   run `command`; fail on output lines that cite a changed file, and on errors in
                    other files that weren't there when the session started
           tests    replace {tests} (or {test_dirs}) with the tests related to the changed files;
                    skip if none
           command  run `command`; fail on non-zero exit
           cheats   fail on lines added this session that skip tests, silence checkers, use any or
                    hardcode secrets, and on deleted tests; `command` lists the groups: skips,
                    silencers, any, secrets, deleted-tests (- for all). Each one is flagged once;
                    `check` only reports them.
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
TEST_DIRS = {"test", "tests", "spec", "__tests__", "e2e", "Tests"}
# Shortcuts an agent takes to make checks pass without fixing anything, by group (mode `cheats`).
CHEATS = {
    "skips": re.compile(  # only looked for in test files
        r"\b(?:it|test|describe|context|suite)\.(?:skip|only|todo|fixme)\b|\b(?:xit|xdescribe|xcontext|xtest|fit|fdescribe)\s*\("
        r"|@pytest\.mark\.(?:skip|skipif|xfail)\b|\bpytest\.skip\(|@unittest\.skip|\.skipTest\(|\bt\.Skip(?:Now|f)?\("
        r"|#\[ignore\]|@Disabled\b|@Ignore\b|markTest(?:Skipped|Incomplete)\(|^\s*(?:skip|pending)\b"),
    "silencers": re.compile(
        r"@ts-(?:ignore|nocheck|expect-error)|eslint-disable|biome-ignore|#\s*type:\s*ignore|#\s*noqa|pylint:\s*disable"
        r"|pyright:\s*ignore|//\s*nolint|#!?\[allow\(|@SuppressWarnings|rubocop:disable|@phpstan-ignore|@psalm-suppress|swiftlint:disable"),
    "any": re.compile(r"\bas\s+any\b|:\s*any\b(?!\s*[A-Za-z_])|<any>"),  # only in TypeScript files
    "secrets": re.compile(  # keys that look real, in files git would commit (.gitignore'd ones aren't checked)
        r"\bAKIA[0-9A-Z]{16}\b|\bsk-(?:ant-|proj-)?[\w-]{32,}|\b[rs]k_live_[0-9A-Za-z]{20,}|\bgh[pousr]_[0-9A-Za-z]{36}\b"
        r"|\bgithub_pat_\w{60,}|\bxox[abpr]-[0-9A-Za-z-]{10,}|\bAIza[\w-]{35}|-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
}
WHY = {
    "skips": "skips or focuses a test: that hides the failure instead of fixing it",
    "silencers": "silences the checker: fix what it reports instead",
    "any": "types it as any: give it its real type",
    "secrets": "hardcodes a secret: read it from an environment variable",
    "deleted-tests": "deletes a test file: a failing test gets fixed, not removed",
}
CITES_FILE = re.compile(r"[\w./@-]+\.\w+(?:\(\d+|:\[?\d+)")  # a file position: a.ts(3,1), ./a.go:3:2, A.java:[5,9]
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
    """The nearest folder with a .doneguard.conf, else the git repo's, else `path`."""
    up = [path, *path.parents]
    return next((d for d in up if (d / CONF).exists()), None) or next((d for d in up if (d / ".git").exists()), path)


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
    words = [w for w in cmd.split() if not w.startswith("-") and "=" not in w and w not in ("npx", "uv", "uvx", "run", "bundle", "exec")]
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


def git(root, *args):
    """stdout of a git command run in `root`, or None if it fails (no git, not a repo, unknown ref)."""
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return None
    return r.stdout if r.returncode == 0 else None


def snapshot(root):
    """A commit with the working tree as it is now (tracked files), to compare with later. None without git."""
    ref = (git(root, "stash", "create") or "").strip()  # writes nothing to the stash, the index or the files
    return ref or (git(root, "rev-parse", "--verify", "-q", "HEAD") or "").strip() or None


def is_test(path):
    p = Path(path)
    return bool(TEST_NAME.search(p.name)) or bool(TEST_DIRS & set(p.parts[:-1]))


def added_lines(root, ref, rels):
    """{path: [(line number, text)]} for the lines added since commit `ref` (new files: all their lines)."""
    tracked = set((git(root, "ls-tree", "-r", "--name-only", ref, "--", *rels) or "").splitlines()) if rels else set()
    diff = git(root, "diff", "--no-color", "--no-ext-diff", "--unified=0", "--relative", ref, "--", *sorted(tracked)) or "" if tracked else ""
    out, path, n = {}, None, 0
    for ln in diff.splitlines():
        if ln.startswith("+++ "):
            path = ln[6:] if ln.startswith("+++ b/") else None
        elif ln.startswith("@@"):
            n = int(re.search(r"\+(\d+)", ln)[1])
        elif ln.startswith("+") and path:
            out.setdefault(path, []).append((n, ln[1:]))
            n += 1
    for r in rels:
        f = root / r
        if r not in tracked and f.is_file() and f.stat().st_size < 1_000_000 and b"\0" not in f.read_bytes()[:8000]:
            out[r] = list(enumerate(read(f).splitlines(), 1))
    return out


def find_cheats(root, ref, files, groups):
    """[(path, line, group, what)]: added lines that skip tests, silence checkers, use any or hardcode secrets, and
    deleted tests. A secret is cut short, so it doesn't spread to the agent's transcript or a PR comment."""
    found = []
    for path, lines in added_lines(root, ref, [os.path.relpath(f, root) for f in files]).items():
        for n, text in lines:
            for g, rx in CHEATS.items():
                m = rx.search(text) if g in groups and (g != "skips" or is_test(path)) and (g != "any" or path.endswith((".ts", ".tsx", ".mts", ".cts"))) else None
                if m:
                    found.append((path, n, g, m.group(0)[:10] + "…" if g == "secrets" else m.group(0).strip()))
    if "deleted-tests" in groups:
        gone = (git(root, "diff", "--name-only", "--relative", "--diff-filter=D", ref) or "").splitlines()
        found += [(p, 0, "deleted-tests", "deleted") for p in gone if is_test(p)]
    return found


def errors_in(lines):
    """Output lines that cite a file position, positions removed: moving code around doesn't make an error new."""
    return [re.sub(r"\(\d+,\d+\)|:\[\d+,\d+\]|:\d+(?::\d+)?", "", ln).strip() for ln in lines if CITES_FILE.search(ln)]


def bases(root, folder):
    """Every folder a check can run in: `folder`, or for @<file> each folder (two levels deep) holding <file>."""
    if not folder.startswith("@"):
        return [(root / folder).resolve()]
    dirs = [root] + [d for d in root.glob("*") if d.is_dir()] + [d for d in root.glob("*/*") if d.is_dir()]
    return [d for d in dirs if (d / folder[1:]).exists() and not (SKIP_DIRS | {".git"}) & set(d.relative_to(root).parts)]


def known_errors(root):
    """The errors whole-project checks (mode filter) report right now, per command and folder."""
    known = {}
    for _, folder, mode, cmd in load_conf(root):
        for base in bases(root, folder) if mode == "filter" else []:
            code, out = sh(cmd, base)
            if code is not None:
                known[f"{cmd}\0{base}"] = errors_in(out.splitlines())
    return known


def run_checks(root, files, say=None, ref=None, known=None, flagged=None):
    """Returns ([(command, failure report)], number of checks that ran, notes for the user).
    `say(ok, name, seconds, why="")` hears about each check as it ends (ok None: skipped, `why` says why). `ref`: the commit cheats are looked for since. `known`: errors
    whole-project checks reported at the start (from known_errors), so new ones anywhere count. `flagged`:
    cheats the agent was already told about (a set, updated here); None outside an agent session, where
    cheats are only reported."""
    fails, ran, notes = [], 0, []
    env, pending, started, containers, down = {}, [], set(), [], False

    def progress(ok, name, t0):
        if say:
            say(ok, name, time.time() - t0)

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
            # cheats also see files deleted since `ref`: the hook's mtime scan can't, and a deleted test may be the only change
            gone = (git(root, "diff", "--name-only", "--relative", "--diff-filter=D", ref) or "").splitlines() if mode == "cheats" and ref else []
            for f in dict.fromkeys(files + [str(root / p) for p in gone]):
                rel = os.path.relpath(f, root)
                if rel.startswith("..") or not (os.path.isfile(f) or mode == "cheats") or not any(fnmatch.fnmatch(rel, g.strip()) for g in globs.split(",")):
                    continue
                base = nearest(f, root, folder[1:]) if folder.startswith("@") else (root / folder).resolve()
                if base:
                    groups.setdefault(base, []).append(f)
            if mode.partition("=")[0] in SERVICES:
                pending += [(mode, cmd, base) for base in groups]  # started only if a check below runs
                continue
            if mode == "cheats":
                for base, hits in groups.items() if ref else []:
                    t0, wanted = time.time(), set(re.split(r"[\s,]+", cmd.strip())) - {"", "-"} or set(WHY)
                    found = find_cheats(base, ref, hits, wanted)
                    ran += 1
                    new = [c for c in found if f"{c[0]}:{c[3]}" not in flagged] if flagged is not None else []
                    progress(not new, "cheats", t0)
                    lines = [f"{p}{f':{n}' if n else ''}  {what}  {WHY[g]}" for p, n, g, what in found]
                    if new:
                        flagged.update(f"{c[0]}:{c[3]}" for c in found)
                        fails.append(("cheats", f"[{os.path.relpath(base, root)}] cheats: changes that hide failures or leak secrets\n" + "\n".join(lines[:20])
                                      + "\nRemove them and fix the cause. If one really has to stay, keep it and tell the user why in your final answer: doneguard flags each one only once."))
                    elif found:
                        notes.append(("kept after being told: " if flagged is not None else "cheats: ") + "; ".join(lines[:5]))
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
                        say(None, tool(cmd), 0, why)
                    if mode == "e2e":
                        notes.append(f"e2e skipped ({why.removeprefix('Error: ')[:120]})")
                    continue
                ran += 1
                where = os.path.relpath(base, root)
                if mode == "filter":
                    bad = list(dict.fromkeys(ln for ln in out.splitlines() if any(cites(ln, r, h) for r, h in zip(rels, hits))))
                    left, new = Counter((known or {}).get(f"{cmd}\0{base}", [])), []
                    for ln in (out.splitlines() if known and f"{cmd}\0{base}" in known else []):
                        k = (errors_in([ln]) or [None])[0]
                        if k is None or ln in bad:
                            continue
                        if left[k]:
                            left[k] -= 1
                        elif ln not in new:
                            new.append(ln)
                    progress(not (bad or new), tool(cmd), t0)
                    if bad or new:
                        also = ["New errors in files you didn't change (your change caused them):", *new[:20]] if new else []
                        fails.append((cmd, f"[{where}] {cmd}\n" + "\n".join(bad[:20] + also)))
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


def git_changed(root, ref="HEAD"):
    """Files changed since `ref`, plus untracked ones."""
    out = set()
    for args in (["diff", "--name-only", "--relative", ref], ["ls-files", "-o", "--exclude-standard"]):
        out |= {str(root / ln) for ln in (git(root, *args) or "").splitlines() if ln}
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


def load(f, default):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return default


def mark(sid, roots):
    """Start of a session, or of the next task after a stop: note the time and a git snapshot per project, and
    record (in the background, the agent isn't kept waiting) the errors whole-project checks already report."""
    (STATE / f"{sid}.since").write_text(str(time.time()))
    (STATE / f"{sid}.refs").write_text(json.dumps({str(r): snapshot(r) for r in roots}))
    if any(row[2] == "filter" for r in roots for row in load_conf(r)):
        try:
            subprocess.Popen([sys.executable, os.path.abspath(__file__), "_known", str(STATE), sid, *map(str, roots)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass


def hook(event, data, everywhere=False):
    """`everywhere`: called from your user config (init --global), which leaves alone the projects hooked on their own."""
    agent = agent_of(data)
    sid = data.get("conversationId") or data.get("conversation_id") or data.get("session_id") or data.get("sessionId") or "default"
    sid = re.sub(r"[^\w-]", "_", str(sid))
    env_root = os.environ.get("CLAUDE_PROJECT_DIR") or os.environ.get("GEMINI_PROJECT_DIR") or os.environ.get("DEVIN_PROJECT_DIR")
    paths = data.get("workspacePaths") or data.get("workspace_roots") or [env_root or data.get("cwd") or os.getcwd()]
    roots = sorted({find_root(Path(p).resolve()) for p in paths})
    if everywhere:
        roots = [r for r in roots if not hooked_here(r, agent)]
        for r in roots:
            if not (r / CONF).exists() and (r / ".git").exists():
                auto_conf(r)
    roots = [r for r in roots if (r / CONF).exists()]
    if everywhere and not roots:  # not a project, or its own hook runs: touch nothing, it may be running right now
        return reply(agent)
    STATE.mkdir(parents=True, exist_ok=True)
    since_f, blocks_f, flagged_f = STATE / f"{sid}.since", STATE / f"{sid}.blocks", STATE / f"{sid}.flagged"
    if event == "start" or not since_f.exists():
        mark(sid, roots)
        return reply(agent)
    if data.get("status") in ("aborted", "error") or data.get("terminationReason", "NO_TOOL_CALL") != "NO_TOOL_CALL":
        return reply(agent)
    since = float(since_f.read_text())
    refs, known, flagged = load(STATE / f"{sid}.refs", {}), load(STATE / f"{sid}.known", {}), set(load(flagged_f, []))
    fails, ran, notes, events, t0 = [], 0, [], [], time.time()
    for root in roots:
        f, n, msgs = run_checks(root, changed_since(root, since), say=lambda *e: events.append(e), ref=refs.get(str(root)), known=known.get(str(root)), flagged=flagged)
        fails, ran, notes = fails + f, ran + n, notes + msgs
    flagged_f.write_text(json.dumps(sorted(flagged)))
    blocks = int(blocks_f.read_text()) if blocks_f.exists() else 0
    blocked = bool(fails) and blocks < MAX_BLOCKS
    if ran:
        log(agent, sid, "blocked" if blocked else "gave_up" if fails else "passed", fails)
    if blocked:
        blocks_f.write_text(str(blocks + 1))
        return reply(agent, message(fails))
    mark(sid, roots)
    blocks_f.unlink(missing_ok=True)
    return reply(agent, gave_up=bool(fails), notes=[receipt(events, time.time() - t0)] * (bool(ran) and not fails) + notes)


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
    # Agents that "pass" by skipping a test or silencing the checker get caught too, whatever the stack.
    return [("*", ".", "cheats", "skips,silencers,any,secrets,deleted-tests")] * bool(checks or late) + checks + late


def installed_version():
    try:
        from importlib.metadata import version
        return version("doneguard")
    except ImportError:  # PackageNotFoundError too: run from a checkout, not installed
        return None


def latest_version():
    """The newest doneguard on PyPI, or None when offline. Only `doctor` asks, and only when you run it."""
    try:
        from urllib.request import urlopen
        with urlopen("https://pypi.org/pypi/doneguard/json", timeout=3) as r:
            return json.load(r)["info"]["version"]
    except (OSError, ValueError, KeyError):
        return None


def newer(a, b):
    """Whether version `a` comes after version `b` (plain X.Y.Z)."""
    return tuple(map(int, re.findall(r"\d+", a)[:3])) > tuple(map(int, re.findall(r"\d+", b)[:3]))


def runnable(cmd, dirs):
    """Whether the program a command starts can be found: on PATH, in node_modules/.bin, .venv/bin, or as a path."""
    words = [w for w in cmd.split() if "=" not in w and not w.startswith("-") and w not in ("npx", "uv", "uvx", "run", "bundle", "exec")]
    prog = words[0] if words else ""
    if "/" in prog:
        return any((d / prog).exists() for d in dirs)
    homes = {p for d in dirs for p in (d, *d.parents)}
    return bool(shutil.which(prog)) or any((h / sub / prog).exists() for h in homes for sub in ("node_modules/.bin", ".venv/bin", "vendor/bin"))


def doctor(root):
    """What the checks in this project need, and whether it's there. Returns 1 when a check would be skipped."""
    bad = []

    def say(ok, what, fix=""):
        print(f"  {'✓' if ok else '✗'} {what}" + ("" if ok or not fix else f": {fix}"))
        if not ok:
            bad.append(what)

    ver = installed_version()
    print(f"doneguard {ver or 'from source'} · Python {sys.version.split()[0]} · {root}")
    latest = latest_version() if ver else None
    if latest and newer(latest, ver):
        print(f"  ↑ doneguard {latest} is out: uv tool upgrade doneguard (or pipx upgrade doneguard), then doneguard init --update")
    say(bool(shutil.which("doneguard")), "doneguard on your PATH", "the hooks call it by name: uv tool install doneguard")
    say(git(root, "rev-parse", "--git-dir") is not None, "git repository", "without it, no cheat checks and no .gitignore")
    lines = [ln for ln in read(root / CONF).splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    rows = load_conf(root)
    say(bool(rows), f"{CONF} with {len(rows)} check(s)", "run doneguard init")
    for ln in lines:
        if len(ln.split(" | ", 3)) != 4:
            say(False, f"line `{ln.strip()[:60]}`", "it needs 4 fields: <globs> | <dir> | <mode> | <command>")
    docker_up = None
    for _, folder, mode, cmd in rows:
        kind, _, image = mode.partition("=")
        dirs = bases(root, folder) or [root]
        if kind in SERVICES:
            if docker_up is None:
                docker_up = docker("info")[0] == 0
            image = image or SERVICES[kind][0]
            say(docker_up, f"{kind}: Docker", "not running: this database and the checks below it are skipped")
            if docker_up and docker("image", "inspect", image)[0] != 0:
                print(f"  · {image} isn't pulled yet: the first run pulls it (or now: docker pull {image})")
            if cmd.strip() not in ("", "-"):
                say(runnable(cmd, dirs), f"{kind} setup: {tool(cmd)}", "not found")
        elif mode == "cheats":
            say(True, "cheats")
        elif mode not in ("files", "filter", "tests", "command", "e2e"):
            say(False, f"mode `{mode}`", "unknown: files, filter, tests, command, e2e, cheats, postgres, mysql, redis or mongo")
        else:
            say(runnable(cmd, dirs), tool(cmd), "not found, so this check is skipped")
            if mode == "e2e" and "playwright" in cmd:
                out = sh("npx --no -- playwright install --dry-run chromium", dirs[0])[1]
                missing = [p for p in re.findall(r"Install location:\s+(\S+)", out) if not Path(p).exists()]
                say(not missing, "Playwright browsers", "missing, run: npx playwright install chromium")
    hooked = [a for a, (f, _, _) in HOOKS.items() if "doneguard" in read(root / f)]
    hooked += [a for a, f in (("kiro", ".kiro/hooks/doneguard.json"), ("opencode", ".opencode/plugins/doneguard.js")) if (root / f).exists()]
    hooked += ["aider"] * ("doneguard" in read(root / ".aider.conf.yml"))
    hooked += [f"{a} (every project)" for a in AGENTS if a not in hooked and hooked_everywhere(a)]
    say(bool(hooked), "hooked into " + (", ".join(hooked) or "no agent"), "run doneguard init")
    print(f"\n{len(bad)} problem(s)." if bad else "\nAll set.")
    return 1 if bad else 0


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
IMPORTS_CLAUDE_GLOBAL = {"cursor", "devin"}  # ...and in ~/.claude/settings.json (Copilot doesn't)


def global_hooks(agent):
    """(the agent's folder in your home, the file in it that holds hooks for every project), or None."""
    home = Path.home()
    return {
        "claude": (home / ".claude", "settings.json"),
        "codex": (Path(os.environ.get("CODEX_HOME") or home / ".codex"), "hooks.json"),
        "gemini": (home / ".gemini", "settings.json"),
        "cursor": (home / ".cursor", "hooks.json"),
        "copilot": (Path(os.environ.get("COPILOT_HOME") or home / ".copilot"), "hooks/doneguard.json"),
        "antigravity": (home / ".gemini" / "config", "hooks.json"),
        "devin": (home / ".config" / "devin", "config.json"),
        "kiro": (home / ".kiro", "hooks/doneguard.json"),
    }.get(agent)


def hooked_here(root, agent):
    """Whether the project has its own doneguard hook for this agent: then the copy in your user config steps aside."""
    files = [HOOKS[agent][0]] if agent in HOOKS else [".kiro/hooks/doneguard.json"] if agent == "kiro" else []
    files += [".claude/settings.local.json", ".claude/settings.json"] * (agent == "claude" or agent in IMPORTS_CLAUDE)
    return any("doneguard" in read(root / f) for f in files)
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


def install(root, agent, everywhere=False):
    """Register the hooks in the agent's own config: the project's or, with `everywhere`, your user config, for every
    project. Returns (file, note)."""
    if agent in HOOKS or agent == "kiro":
        file, keys, events = HOOKS.get(agent, (".kiro/hooks/doneguard.json", None, KIRO))
        path = root / file
        if everywhere:  # `--global` lets the project's own hook, if it has one, do the work
            folder, name = global_hooks(agent)
            path, keys = folder / name, ["hooks"] if agent == "devin" else keys
            events = json.loads(json.dumps(events).replace(START, START + " --global").replace(VERIFY, VERIFY + " --global"))
            file = str(path).replace(str(Path.home()), "~", 1)

        def change(data):
            if keys is None:  # Kiro: the file is ours
                data.update(events)
                return
            if agent in ("cursor", "copilot"):
                data.setdefault("version", 1)
            node = data
            for k in keys:
                node = node.setdefault(k, {})
            for event, entry in events.items():
                entries = node.setdefault(event, [])
                if "doneguard" not in json.dumps(entries):
                    entries.append(entry)

        edit_json(path, change)
        return file, {"codex": "approve it once with /hooks in Codex", "kiro": "Kiro CLI V3: kiro-cli --v3" if everywhere else ""}.get(agent, "")
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


def add_checks(root, detected, new):
    """Write newly detected checks into an existing .doneguard.conf, each where it works: cheats on top, a
    database right above the first check of yours that needs it, other checks above the databases."""
    lines = (root / CONF).read_text().splitlines()
    rows = {}  # line number -> (mode, command)
    for i, ln in enumerate(lines):
        parts = [p.strip() for p in ln.split(" | ", 3)]
        if len(parts) == 4 and not ln.lstrip().startswith("#"):
            rows[i] = (parts[2], parts[3])
    top = next((i for i, ln in enumerate(lines) if ln.strip() and not ln.lstrip().startswith("#")), len(lines))
    first_db = next((i for i, (m, _) in rows.items() if m.partition("=")[0] in SERVICES), len(lines))
    late = next((k for k, c in enumerate(detected) if c[2].partition("=")[0] in SERVICES), len(detected))
    at = {}  # line number to insert before -> checks
    for c in new:
        k = detected.index(c)
        if c[2] == "cheats":
            i = top
        elif k < late:
            i = first_db
        elif c[2].partition("=")[0] in SERVICES:
            needs = {(d[2], d[3]) for d in detected[k + 1:]}  # its stack's tests and e2e
            i = next((i for i, key in rows.items() if key in needs), len(lines))
        else:
            i = len(lines)
        at.setdefault(i, []).append(" | ".join(c))
    out = [ln for i in range(len(lines) + 1) for ln in at.get(i, []) + lines[i:i + 1]]
    (root / CONF).write_text("\n".join(out) + "\n")


def hooked_everywhere(agent):
    """The user config file where doneguard is hooked for every project, or None."""
    where = global_hooks(agent)
    return where and "doneguard" in read(where[0] / where[1]) and str(where[0] / where[1]).replace(str(Path.home()), "~", 1)


def auto_conf(root):
    """For a git repo nobody set up, hooked with init --global: write the .doneguard.conf init would, kept out of git
    (.git/info/exclude), with the quick checks on and the databases, the tests that need them and e2e commented out."""
    checks = detect(root)
    if not checks:
        return  # no stack yet: the next session looks again
    quick = next((i for i, c in enumerate(checks) if c[2].partition("=")[0] in SERVICES), len(checks))
    off = [c for i, c in enumerate(checks) if i >= quick or c[2] == "e2e"]
    (root / CONF).write_text(HEADER + "# Written for this repo by doneguard init --global, kept out of git. Off until you remove their #:\n"
                             "# throwaway databases, the tests that need them, and e2e.\n"
                             + "".join("# " * (c in off) + ln + "\n" for c, ln in zip(checks, table(checks))))
    exclude = root / (git(root, "rev-parse", "--git-path", "info/exclude") or ".git/info/exclude").strip()
    have = read(exclude)
    if CONF not in have.split():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        exclude.write_text(have + ("\n" if have and not have.endswith("\n") else "") + CONF + "\n")


def init_global(names):
    """Hook the agents for every project, in your user config: after this, nothing to run per project."""
    names = [a for a in AGENTS if global_hooks(a)] if names == ["all"] else names
    names = names or [a for a in AGENTS if global_hooks(a) and (any(shutil.which(b) for b in AGENTS[a][0]) or global_hooks(a)[0].exists())] or ["claude"]
    other = sorted(set(names) - {a for a in AGENTS if global_hooks(a)})
    if other:
        print(f"error: {', '.join(other)} can't be hooked for every project; run doneguard init in each project for them.")
        return 2
    claude = "claude" in names or hooked_everywhere("claude")
    print("hooked for every project:")
    for agent in names:
        if agent in IMPORTS_CLAUDE_GLOBAL and claude:
            print(f"  {agent:<12} runs the Claude Code hooks in ~/.claude/settings.json too, nothing to add")
            continue
        try:
            file, note = install(Path.home(), agent, everywhere=True)
        except ValueError:
            print(f"  {agent:<12} skipped: its config is not valid JSON, fix it and run init --global again")
            continue
        print(f"  {agent:<12} {file}" + (f" ({note})" if note else ""))
    print(f"Nothing to run per project now. The first session in a git repo writes its {CONF}, kept out of git, with the\n"
          "quick checks on (types, lint, tests that need no database, no shortcuts) and the databases and e2e off until\n"
          "you remove their #. doneguard init in a project writes one to commit instead.")
    if not shutil.which("doneguard"):
        print("warning: `doneguard` is not on your PATH, so the hooks can't run. Install it: uv tool install doneguard")
    return 0


def init(root, names=()):
    update = "--update" in names
    if "--global" in names:
        return init_global([n for n in names if n != "--global"])
    names = [n for n in names if n != "--update"]
    names = list(AGENTS) if names == ["all"] else names
    names = names or [a for a, (bins, hint) in AGENTS.items() if any(shutil.which(b) for b in bins) or (root / hint).exists()] or ["claude"]
    unknown = sorted(set(names) - set(AGENTS))
    if unknown:
        print(f"error: unknown agent(s): {', '.join(unknown)}. Known: {', '.join(AGENTS)}, all.")
        return 2
    notes = []
    checks, conf = detect(root, notes), load_conf(root)
    if conf:
        have = {(r[2], "" if r[2] == "cheats" else r[3]) for r in conf}  # one cheats line, whatever groups it lists
        shown = [c for c in checks if (c[2], "" if c[2] == "cheats" else c[3]) not in have]
        if update and shown:
            add_checks(root, checks, shown)
            print(f"added {len(shown)} check(s) to {CONF}, each in its place:")
        else:
            print(f"{CONF} already has checks, leaving it as is."
                  + (" Detected here but not in it (add them with doneguard init --update, or by hand):" if shown else ""))
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
        if hooked_everywhere(agent) or (agent in IMPORTS_CLAUDE_GLOBAL and hooked_everywhere("claude")):
            print(f"  {agent:<12} hooked for every project ({hooked_everywhere(agent) or hooked_everywhere('claude')}), nothing to add")
            continue
        try:
            file, note = install(root, agent)
        except ValueError:
            print(f"  {agent:<12} skipped: its config is not valid JSON, fix it and run init again")
            continue
        print(f"  {agent:<12} {file}" + (f" ({note})" if note else ""))
    if checks and not conf:
        print("Show that your agent is checked, with a README badge: https://github.com/abranchesia/doneguard#show-it")
    if not shutil.which("doneguard"):
        print("warning: `doneguard` is not on your PATH, so the hooks can't run. Install it: uv tool install doneguard")
    return 0


def show(ok, name, secs, why=""):
    """A progress line of `doneguard check`."""
    print(f"  {'✓' if ok else '–' if ok is None else '✗'} {name:<16} " + (f"skipped: {why[:70]}" if ok is None else f"{secs:5.1f}s"))


def receipt(events, secs):
    """One line for the user when everything passed: which checks ran, and how long it took."""
    names = dict.fromkeys(name for ok, name, *_ in events if ok and not name.endswith(" setup"))
    return f"✓ {' · '.join(names)} ({secs:.1f} s)"


def report(root, files, ref):
    """`doneguard report`: run the checks and write the result as markdown, for a PR or a CI summary."""
    events, t0 = [], time.time()
    fails, _, notes = run_checks(root, files, say=lambda *e: events.append(e), ref=ref)
    out = ["### " + ("❌ doneguard: checks failing" if fails else "✅ Checked by doneguard"), ""]
    if events:
        out += ["| Check | Result | Time |", "|---|---|---|"]
        out += [f"| {name} | {'✓ passed' if ok else f'– skipped: {why[:80]}' if ok is None else '✗ failed'} | {'' if ok is None else f'{secs:.1f} s'} |"
                for ok, name, secs, *rest in events for why in [(rest or [''])[0].replace("|", "\\|")]]
    else:
        out.append(f"No check applies to the {len(files)} changed file(s).")
    out += [""] + [f"- {n}" for n in notes] + [""] * bool(notes)
    shots = sorted(p for p in (root / EVIDENCE).glob("*/**/*.png") if p.stat().st_mtime >= t0 and "report" not in p.parts)
    if shots:
        out += ["<details><summary>Screenshots of the end-to-end tests</summary>", ""]
        out += [f"- `{os.path.relpath(p, root)}`" for p in shots[:20]] + ["", "</details>", ""]
    if fails:
        out += ["<details><summary>What failed</summary>", "", "```text", "\n\n".join(t for _, t in fails), "```", "", "</details>", ""]
    out.append(f"<sub>{len(files)} changed file(s) · {time.time() - t0:.1f} s · [doneguard](https://github.com/abranchesia/doneguard): your coding agent can't say \"done\" until the checks pass.</sub>")
    print("\n".join(out))
    return 1 if fails else 0


def targets(root, args):
    """The files and the git ref `check` and `report` work on: [--base REF] [files…]."""
    ref = "HEAD"
    if args[:1] == ["--base"] and len(args) > 1:  # in CI: everything the branch changed since it left <ref>
        ref = (git(root, "merge-base", args[1], "HEAD") or args[1]).strip()
        args = args[2:]
    files = [str(Path(a).resolve()) for a in args] or git_changed(root, ref)
    return files, ref if git(root, "rev-parse", "--verify", "-q", ref) else None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else ""
    if cmd in ("start", "verify"):
        try:
            data = json.load(sys.stdin)
        except ValueError:
            data = {}
        return hook(cmd, data if isinstance(data, dict) else {}, "--global" in argv[1:])
    if cmd == "init":
        return init(Path.cwd().resolve(), argv[1:])
    if cmd == "stats":
        return stats()
    if cmd == "doctor":
        return doctor(find_root(Path.cwd().resolve()))
    if cmd in ("--version", "-V", "version"):
        print(f"doneguard {installed_version() or '(not installed: run from source)'}")
        return 0
    if cmd == "_known":  # background job started by `mark`: _known <state dir> <session> <roots…>
        state, sid, roots = Path(argv[1]), argv[2], argv[3:]
        known = {r: known_errors(Path(r)) for r in roots}
        tmp = state / f"{sid}.known.tmp"
        tmp.write_text(json.dumps(known))
        os.replace(tmp, state / f"{sid}.known")
        return 0
    if cmd == "report":
        root = find_root(Path.cwd().resolve())
        return report(root, *targets(root, argv[1:]))
    if cmd == "check":
        root = find_root(Path.cwd().resolve())
        files, ref = targets(root, argv[1:])
        fails, ran, notes = run_checks(root, files, say=show, ref=ref)
        for n in notes:
            print(f"doneguard: {n}.")
        print(("\n" if ran else "") + (message(fails) if fails else f"doneguard: all checks pass ({len(files)} changed file(s)).").rstrip())
        return 1 if fails else 0
    print(__doc__.strip())
    return 0 if cmd in ("", "-h", "--help", "help") else 2


if __name__ == "__main__":
    sys.exit(main())
