#!/usr/bin/env python3
"""Deployer — a local macOS control panel for committing, pushing and deploying projects to your servers.

Run:       python3 app.py   (or via Deployer.app)
Serves the UI on http://127.0.0.1:47615 (or a free port). No third-party packages required.
Settings:  ~/Library/Application Support/Deployer/projects.json
Secrets:   macOS Keychain (service "Deployer")
"""
import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST, PORT = "127.0.0.1", int(os.environ.get("DEPLOYER_PORT", "47615"))
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DEPLOYER_DATA") or os.path.expanduser("~/Library/Application Support/Deployer")
CONFIG = os.path.join(DATA, "projects.json")
HISTORY = os.path.join(DATA, "history.log")
REMOTE_SCRIPT = os.path.join(BASE, "remote_deploy.sh")
INDEX = os.path.join(BASE, "index.html")
KC_SERVICE = os.environ.get("DEPLOYER_KEYCHAIN_SERVICE", "Deployer")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

LOCALES = os.path.join(BASE, "locales")
LANGS = ("en", "uk", "ru")

# ---------------- i18n ----------------
# English strings are the keys; locales/<lang>.json hold the translations (shared with the UI).
_i18n = {"lang": None, "mtime": None, "dict": {}, "patterns": []}


def _current_lang():
    try:
        mt = os.path.getmtime(CONFIG)
    except OSError:
        return "en"
    if mt != _i18n["mtime"]:
        _i18n["mtime"] = mt
        try:
            with open(CONFIG, encoding="utf-8") as f:
                lang = json.load(f).get("lang") or "en"
        except Exception:
            lang = "en"
        _load_dict(lang if lang in LANGS else "en")
    return _i18n["lang"]


def _load_dict(lang):
    if lang == _i18n["lang"]:
        return
    d = {}
    if lang != "en":
        try:
            with open(os.path.join(LOCALES, lang + ".json"), encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            d = {}
    pats = []
    for k, v in d.items():
        if "{0}" in k:  # templated messages → regex, to translate streamed log lines
            rx = re.escape(k)
            for i in range(10):
                rx = rx.replace(re.escape("{%d}" % i), "(?P<a%d>.+?)" % i)
            try:
                pats.append((re.compile("^" + rx + "$"), v))
            except re.error:
                pass
    _i18n.update(lang=lang, dict=d, patterns=pats)


def tr(key, *args):
    _current_lang()
    s = _i18n["dict"].get(key) or key
    for i, a in enumerate(args):
        s = s.replace("{%d}" % i, str(a))
    return s


_PREFIX = re.compile(r"^(\s*(?:[✓✅❌⚠️→═•]+\s*)*)(.*?)(\s*)$", re.S)


def tr_line(line):
    """Translate one line of log output (from this app or the server script)."""
    if _current_lang() == "en":
        return line
    m = _PREFIX.match(line)
    pre, body, post = m.group(1), m.group(2), m.group(3)
    if not body:
        return line
    d = _i18n["dict"]
    if body in d:
        return pre + d[body] + post
    for rx, v in _i18n["patterns"]:
        mm = rx.match(body)
        if mm:
            out = v
            for name, val in mm.groupdict().items():
                out = out.replace("{%s}" % name[1:], val)
            return pre + out + post
    return line


_running = set()
_lock = threading.Lock()
_server = None


# ---------------- Keychain ----------------

def kc_account(pid, eid):
    return "db:%s-%s" % (pid, eid)


def kc_get(acct):
    r = subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-a", acct, "-w"],
                       capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else ""


def kc_has(acct):
    r = subprocess.run(["security", "find-generic-password", "-s", KC_SERVICE, "-a", acct],
                       capture_output=True, text=True)
    return r.returncode == 0


def kc_set(acct, pw):
    r = subprocess.run(["security", "add-generic-password", "-U", "-s", KC_SERVICE, "-a", acct,
                        "-l", "Deployer " + acct, "-w", pw], capture_output=True, text=True)
    if r.returncode:
        raise ValueError(tr("Could not save the password to Keychain: {0}", r.stderr.strip()))


def kc_del(acct):
    subprocess.run(["security", "delete-generic-password", "-s", KC_SERVICE, "-a", acct],
                   capture_output=True, text=True)


# ---------------- configuration ----------------

def load_config():
    if not os.path.exists(CONFIG):
        return {"servers": [], "projects": []}
    with open(CONFIG, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.setdefault("servers", [])
    cfg.setdefault("projects", [])
    return cfg


def ssh_account(sid):
    return "ssh:%s" % sid


def config_for_ui():
    cfg = load_config()
    cfg["git_services"] = git_services(cfg)
    for s in cfg["servers"]:
        s.setdefault("auth", "key")
        s["has_secret"] = kc_has(ssh_account(s["id"]))
    for p in cfg["projects"]:
        for e in p.get("environments", []):
            db = e.setdefault("db", {})
            db["has_password"] = bool(db.get("name")) and kc_has(kc_account(p["id"], e["id"]))
    return cfg


def _req(obj, key, where):
    v = obj.get(key)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(tr("{0}: field “{1}” is required", where, key))
    obj[key] = v.strip()


def _check_id(v, where):
    if not ID_RE.match(v):
        raise ValueError(tr("{0}: id may contain only lowercase Latin letters, digits, - and _", where))


def validate(cfg):
    if not isinstance(cfg, dict):
        raise ValueError(tr("Invalid settings"))
    if cfg.get("lang") not in LANGS:
        cfg["lang"] = "en"
    servers = cfg.get("servers") or []
    projects = cfg.get("projects") or []
    svc_ids = set()
    for sv in cfg.get("git_services") or []:
        where = tr("Git service “{0}”", sv.get("name") or sv.get("host") or "?")
        for k in ("id", "name", "host"):
            _req(sv, k, where)
        sv["host"] = re.sub(r"^[a-z]+://", "", sv["host"].lower()).split("/")[0]
        if not re.match(r"^[a-z0-9.-]+$", sv["host"]):
            raise ValueError(tr("{0}: host must look like bitbucket.org or git.example.com", where))
        if sv.get("type") not in ("bitbucket", "github", "gitlab", "other"):
            sv["type"] = "other"
        if sv["id"] in svc_ids:
            raise ValueError(tr("{0}: duplicate", where))
        svc_ids.add(sv["id"])
    sids = set()
    for s in servers:
        where = tr("Server “{0}”", s.get("name") or s.get("host") or "?")
        s["auth"] = s.get("auth") or "key"
        if s["auth"] not in ("key", "password"):
            raise ValueError(tr("{0}: unknown sign-in method", where))
        for k in ("id", "name", "host", "user") + (("key",) if s["auth"] == "key" else ()):
            _req(s, k, where)
        if s["id"] in sids:
            raise ValueError(tr("{0}: duplicate", where))
        sids.add(s["id"])
        try:
            s["port"] = int(s.get("port") or 22)
        except (TypeError, ValueError):
            raise ValueError(tr("{0}: port must be a number", where))
        if not 0 < s["port"] < 65536:
            raise ValueError(tr("{0}: invalid port", where))
    pids = set()
    for p in projects:
        where = tr("Project “{0}”", p.get("name") or p.get("id") or "?")
        for k in ("id", "name", "path"):
            _req(p, k, where)
        _check_id(p["id"], where)
        if p["id"] in pids:
            raise ValueError(tr("{0}: duplicate", where))
        pids.add(p["id"])
        envs = p.get("environments") or []
        if not envs:
            raise ValueError(tr("{0}: add at least one environment", where))
        eids = set()
        for e in envs:
            ew = "%s / %s" % (where, e.get("name") or e.get("id") or "?")
            for k in ("id", "name", "server", "branch", "target"):
                _req(e, k, ew)
            _check_id(e["id"], ew)
            if e["id"] in eids:
                raise ValueError(tr("{0}: duplicate", ew))
            eids.add(e["id"])
            if e["server"] not in sids:
                raise ValueError(tr("{0}: choose a server", ew))
            ex = e.get("excludes") or []
            if not isinstance(ex, list) or not all(isinstance(x, str) for x in ex):
                raise ValueError(tr("{0}: excludes must be a list", ew))
            e["excludes"] = [x.strip() for x in ex if x.strip()]


def save_config(cfg):
    validate(cfg)
    # passwords go to Keychain and never into the file
    for s in cfg["servers"]:
        acct = ssh_account(s["id"])
        secret = s.pop("secret", "")
        if s.pop("clear_secret", False):
            kc_del(acct)
        elif secret:
            kc_set(acct, secret)
        s.pop("has_secret", None)
    for p in cfg["projects"]:
        for e in p["environments"]:
            db = e.get("db") or {}
            acct = kc_account(p["id"], e["id"])
            pw = db.pop("password", "")
            if db.pop("clear_password", False) or not db.get("name"):
                kc_del(acct)
            elif pw:
                kc_set(acct, pw)
            db.pop("has_password", None)
            e["db"] = db
    os.makedirs(DATA, exist_ok=True)
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG)


def cfg_now():
    return load_config()


def find_project(cfg, pid, eid=None):
    p = next((x for x in cfg["projects"] if x["id"] == pid), None)
    if p is None:
        raise ValueError(tr("Project not found: {0}", pid))
    if eid is None:
        return p, None
    e = next((x for x in p["environments"] if x["id"] == eid), None)
    if e is None:
        raise ValueError(tr("Environment not found: {0}", eid))
    return p, e


def find_server(cfg, sid):
    s = next((x for x in cfg["servers"] if x["id"] == sid), None)
    if s is None:
        raise ValueError(tr("Server not found (check the environment settings)"))
    return s


# ---------------- local git ----------------

def git(path, *args, timeout=30):
    try:
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0",
                   GIT_SSH_COMMAND=os.environ.get("GIT_SSH_COMMAND") or "ssh -o BatchMode=yes")
        r = subprocess.run(["git", "-C", path] + list(args), capture_output=True,
                           text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except subprocess.TimeoutExpired:
        return 1, "", "git %s timed out" % args[0]


def project_path(p):
    return os.path.expanduser(p["path"])


def _norm_url(u):
    u = re.sub(r"([^:])/{2,}", r"\1/", (u or "").strip().lower()).rstrip("/")
    return u[:-4] if u.endswith(".git") else u


# ---- Git services (Bitbucket, GitHub, GitLab, self-hosted…) ----
DEFAULT_SERVICES = [
    {"id": "bitbucket", "name": "Bitbucket", "type": "bitbucket", "host": "bitbucket.org"},
    {"id": "github", "name": "GitHub", "type": "github", "host": "github.com"},
]


def git_services(cfg):
    return cfg.get("git_services") or DEFAULT_SERVICES


def url_host(url):
    u = (url or "").strip()
    m = re.match(r"^[a-z+]+://(?:[^@/]+@)?([^/:]+)", u)  # ssh://git@host:22/..., https://host/...
    if m:
        return m.group(1).lower()
    m = re.match(r"^(?:[^@]+@)?([^:/]+):", u)  # git@host:path
    return m.group(1).lower() if m else ""


def service_for_url(cfg, url):
    h = url_host(url)
    if not h:
        return None
    for sv in git_services(cfg):
        host = (sv.get("host") or "").lower()
        if host and (h == host or h.endswith("." + host)):
            return sv
    return None


def list_remotes(path):
    """Local git remotes: [{name, url, push:[...]}]"""
    _, out, _ = git(path, "remote", "-v")
    res = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        d = res.setdefault(parts[0], {"name": parts[0], "url": "", "push": []})
        if parts[2] == "(fetch)":
            d["url"] = parts[1]
        elif parts[1] not in d["push"]:
            d["push"].append(parts[1])
    return list(res.values())


def git_remote(p):
    """(remote name, url) the SERVER pulls this project's code from."""
    path = project_path(p)
    remotes = {r["name"]: r["url"] for r in list_remotes(path)}
    if not remotes:
        return None, None
    if p.get("deploy_remote") in remotes:  # 1) explicitly chosen in settings
        return p["deploy_remote"], remotes[p["deploy_remote"]]
    if p.get("repo"):  # 2) remote with the URL from settings
        for n, u in remotes.items():
            if _norm_url(u) == _norm_url(p["repo"]):
                return n, u
    prov = p.get("provider")  # 3) remote of the chosen service (legacy settings)
    if prov in ("github", "bitbucket"):
        host = "github.com" if prov == "github" else "bitbucket.org"
        for n, u in remotes.items():
            if host in u:
                return n, u
    if "origin" in remotes:  # 4) origin
        return "origin", remotes["origin"]
    _, up, _ = git(path, "rev-parse", "--abbrev-ref", "@{u}")  # 5) upstream of the current branch
    if "/" in up and up.split("/")[0] in remotes:
        n = up.split("/")[0]
        return n, remotes[n]
    n = sorted(remotes)[0]
    return n, remotes[n]


def push_remotes(p):
    """Where to push (remote names)."""
    names = {r["name"] for r in list_remotes(project_path(p))}
    chosen = [n for n in (p.get("push_remotes") or []) if n in names]
    if chosen:
        return chosen
    dr, _ = git_remote(p)
    return [dr] if dr else []


def repo_url(p):
    _, url = git_remote(p)
    if p.get("deploy_remote") and url:
        return url
    if p.get("repo"):
        return p["repo"]
    if not url:
        raise ValueError(tr("Could not determine the repository URL: add a repository in the project settings"))
    return url


def git_provider(p, url=None, cfg=None):
    try:
        url = url or repo_url(p)
    except ValueError:
        url = ""
    sv = service_for_url(cfg or load_config(), url)
    if sv:
        return sv.get("type") or "other"
    if p.get("provider") in ("github", "bitbucket"):
        return p["provider"]
    return "github" if "github.com" in url else ("bitbucket" if "bitbucket" in url else "other")


def _ahead_behind(path, ref_remote, ref_local):
    c, out, _ = git(path, "rev-list", "--left-right", "--count", "%s...%s" % (ref_remote, ref_local))
    if c == 0 and out:
        behind, ahead = out.split()
        return int(ahead), int(behind)
    return None


def project_status(p):
    cfg = load_config()
    path = project_path(p)
    if not os.path.isdir(path):
        return {"ok": False, "error": tr("Folder does not exist: {0}", path)}
    code, _, _ = git(path, "rev-parse", "--git-dir")
    if code:
        return {"ok": False, "error": tr("Folder is not a git repository: {0}", path)}
    _, branch, _ = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    _, porcelain, _ = git(path, "status", "--porcelain")
    dirty = [l for l in porcelain.splitlines() if l.strip()]
    remote, _ = git_remote(p)
    if not remote:
        return {"ok": False, "error": tr("The local repository has no remotes")}
    pushes = push_remotes(p)
    ferrs = []
    for r in dict.fromkeys([remote] + pushes):
        fcode, _, ferr = git(path, "fetch", "-q", "--prune", r, timeout=40)
        if fcode:
            ferrs.append("%s: %s" % (r, ferr))
    _, br, _ = git(path, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    branches = [b for b in br.splitlines() if b]
    envs = {}
    for e in p["environments"]:
        b = e["branch"]
        info = {"branch": b, "local": b in branches}
        if info["local"]:
            ab = _ahead_behind(path, "%s/%s" % (remote, b), b)
            if ab:
                info.update(ahead=ab[0], behind=ab[1])
            else:
                info["no_remote"] = True
        envs[e["id"]] = info
    rlist = []
    for r in list_remotes(path):
        sv = service_for_url(cfg, r["url"])
        item = {"name": r["name"], "url": r["url"], "service": sv["name"] if sv else url_host(r["url"]),
                "push": r["name"] in pushes, "deploy": r["name"] == remote,
                "also": [u for u in r["push"] if _norm_url(u) != _norm_url(r["url"])]}
        if branch and branch != "HEAD":
            ab = _ahead_behind(path, "%s/%s" % (r["name"], branch), branch)
            if ab:
                item.update(ahead=ab[0], behind=ab[1])
            else:
                item["no_branch"] = True
        rlist.append(item)
    return {"ok": True, "provider": git_provider(p, cfg=cfg), "remote": remote, "remotes": rlist,
            "branch": branch, "dirty": dirty[:50], "dirty_count": len(dirty),
            "branches": branches, "envs": envs, "fetch_error": "\n".join(ferrs)}


# ---------------- SSH / running jobs ----------------

# ---- password / passphrase prompts while connecting (SSH_ASKPASS) ----
# ssh runs askpass.py, which calls back here; the secret comes from Keychain on the first
# attempt, otherwise the user is asked in the Deployer window.
ASKPASS = os.path.join(BASE, "askpass.py")
_port = 0
_askpass_ctx = {}   # token -> {"sid", "label", "attempts"}
_prompts = {}       # id -> {"id","prompt","server","retry","kind","event","answer","remember","sid"}


def askpass_session(server, label=None):
    token = secrets.token_urlsafe(24)
    _askpass_ctx[token] = {"sid": server.get("id") or "", "attempts": 0,
                           "preset": server.get("secret") or "",
                           "label": label or "%s (%s@%s)" % (server.get("name") or server["host"],
                                                             server["user"], server["host"])}
    return token


def run_local_git(path, args, write, label):
    """Run git on this Mac (push etc.); a key passphrase, if needed, is asked in the Deployer window."""
    token = askpass_session({}, label=label)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", SSH_ASKPASS=ASKPASS, SSH_ASKPASS_REQUIRE="force",
               DISPLAY=os.environ.get("DISPLAY", ":0"),
               DEPLOYER_ASKPASS_URL="http://127.0.0.1:%d/askpass" % _port, DEPLOYER_ASKPASS_TOKEN=token)
    try:
        return stream_cmd(["git", "-C", path] + list(args), write, env=env)
    finally:
        _askpass_ctx.pop(token, None)


def service_check(host, write):
    """Does the Git service accept this Mac's key (ssh -T git@host)?"""
    write(("→ ssh -T git@%s (%s)\n" % (host, tr("using this Mac's keys"))).encode())
    token = askpass_session({}, label="ssh → " + host)
    env = dict(os.environ, SSH_ASKPASS=ASKPASS, SSH_ASKPASS_REQUIRE="force", DISPLAY=os.environ.get("DISPLAY", ":0"),
               DEPLOYER_ASKPASS_URL="http://127.0.0.1:%d/askpass" % _port, DEPLOYER_ASKPASS_TOKEN=token)
    out = []
    try:
        stream_cmd(["ssh", "-T", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=10",
                    "git@" + host], lambda b: (out.append(b), write(b)), env=env)
    finally:
        _askpass_ctx.pop(token, None)
    text = b"".join(out).decode("utf-8", "replace").lower()
    if any(k in text for k in ("authenticated", "successfully", "welcome to gitlab", "welcome to gitea", "hi ")):
        write(("✅ %s\n" % tr("This Mac has access")).encode())
        return 0
    write(("❌ %s\n" % tr("No access: add this Mac's public key to your account in this service (SSH keys)")).encode())
    return 1


def current_branch(path):
    _, b, _ = git(path, "rev-parse", "--abbrev-ref", "HEAD")
    return b if b and b != "HEAD" else ""


def push_job(p, remotes, branch, write):
    path = project_path(p)
    if not branch:
        write(("❌ %s\n" % tr("No branch selected (the repository may be in detached HEAD state)")).encode())
        return 1
    if not remotes:
        write(("❌ %s\n" % tr("No push target: choose repositories in the project settings")).encode())
        return 1
    rc_all = 0
    for r in remotes:
        write(("→ git push %s %s\n" % (r, branch)).encode())
        rc = run_local_git(path, ["push", r, branch], write, "git push → %s" % r)
        if rc:
            write(("❌ %s\n" % tr("Push to {0} failed", r)).encode())
            rc_all = rc
        else:
            write(("✓ %s\n" % tr("{0}: pushed", r)).encode())
    return rc_all


def commit_job(p, message, write):
    path = project_path(p)
    message = (message or "").strip()
    if not message:
        write(("❌ %s\n" % tr("Describe the changes (commit message)")).encode())
        return 1
    _, porcelain, _ = git(path, "status", "--porcelain")
    if not porcelain.strip():
        write(("%s\n" % tr("Nothing to commit")).encode())
        return 0
    write("→ git add -A\n".encode())
    rc = stream_cmd(["git", "-C", path, "add", "-A"], write)
    if rc:
        return rc
    write(("→ git commit -m %s\n" % shlex.quote(message)).encode())
    rc = stream_cmd(["git", "-C", path, "commit", "-m", message], write)
    if rc == 0:
        write(("✅ %s\n" % tr("Changes committed. You can now push or deploy them.")).encode())
    return rc


def askpass_request(token, prompt):
    ctx = _askpass_ctx.get(token)
    if not ctx:
        return None
    ctx["attempts"] += 1
    sid = ctx["sid"]
    if ctx["attempts"] == 1:
        saved = ctx["preset"] or (kc_get(ssh_account(sid)) if sid else "")
        if saved:
            return saved
    low = prompt.lower()
    item = {"id": secrets.token_hex(8), "prompt": prompt.strip(), "server": ctx["label"],
            "retry": ctx["attempts"] > 1, "kind": "passphrase" if "passphrase" in low else "password",
            "event": threading.Event(), "answer": None, "remember": False}
    _prompts[item["id"]] = item
    try:
        if not item["event"].wait(300):
            return None
    finally:
        _prompts.pop(item["id"], None)
    if item["answer"] is not None and item["remember"] and sid:
        kc_set(ssh_account(sid), item["answer"])
    return item["answer"]


def remote_cmd(server, token=None):
    """(command, env) to run a script from stdin on the server."""
    if server["host"] == "local":  # for tests: run on this computer
        return ["bash", "-s"], None
    cmd = ["ssh", "-p", str(server.get("port") or 22),
           "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
    if (server.get("auth") or "key") == "password":
        cmd += ["-o", "PubkeyAuthentication=no",
                "-o", "PreferredAuthentications=password,keyboard-interactive",
                "-o", "NumberOfPasswordPrompts=2"]
    else:
        cmd += ["-i", os.path.expanduser(server["key"]), "-o", "IdentitiesOnly=yes",
                "-o", "PreferredAuthentications=publickey", "-o", "NumberOfPasswordPrompts=2"]
    cmd += ["%s@%s" % (server["user"], server["host"]), "bash -s"]
    env = dict(os.environ)
    if token:
        env.update(SSH_ASKPASS=ASKPASS, SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY", ":0"),
                   DEPLOYER_ASKPASS_URL="http://127.0.0.1:%d/askpass" % _port,
                   DEPLOYER_ASKPASS_TOKEN=token)
    return cmd, env


def run_remote(server, write, stdin_bytes):
    token = askpass_session(server)
    try:
        cmd, env = remote_cmd(server, token)
        return stream_cmd(cmd, write, stdin_bytes, env=env)
    finally:
        _askpass_ctx.pop(token, None)


def stream_cmd(cmd, write, stdin_bytes=None, cwd=None, env=None):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=cwd, env=env,
                            start_new_session=True,  # no controlling terminal: ssh asks via askpass
                            stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL)
    if stdin_bytes is not None:
        try:
            proc.stdin.write(stdin_bytes)
            proc.stdin.close()
        except BrokenPipeError:
            pass
    for line in iter(proc.stdout.readline, b""):
        write(line)
    rc = proc.wait()
    if rc == 255 and cmd[0] == "ssh":
        write(("❌ %s\n" % tr("SSH connection failed. Check the IP, port, user, key or password.")).encode())
    return rc


def with_vars(script, env_vars):
    # Variables (including the DB password) are sent via stdin, not on the command line,
    # so they never show up in the server's process list.
    head = "".join("export %s=%s\n" % (k, shlex.quote(v)) for k, v in env_vars.items())
    return (head + script).encode()


DEFAULT_GIT_KEY = "~/.ssh/deployer_git"


def shared_git_key(cfg):
    """Contents of the shared Git key on this Mac (sent to the server only while a job runs)."""
    path = os.path.expanduser(cfg.get("git_key") or DEFAULT_GIT_KEY)
    if not os.path.exists(path):
        raise ValueError(tr("The shared Git key does not exist yet: Git → Shared Git key → Show / create"))
    if _key_encrypted(path):
        raise ValueError(tr("The shared Git key has a passphrase. Deploys need a key without a passphrase."))
    with open(path, encoding="utf-8") as f:
        return f.read().strip()


def run_job(cfg, p, e, mode, ref, push, write):
    server = find_server(cfg, e["server"])
    path = project_path(p)
    ref = (ref or e["branch"]).strip()

    if mode == "deploy" and push:
        code, _, _ = git(path, "show-ref", "--verify", "-q", "refs/heads/" + ref)
        if code == 0:
            rc = push_job(p, push_remotes(p), ref, write)
            if rc:
                write(("❌ %s\n" % tr("Deploy stopped: push failed")).encode())
                return rc

    db = e.get("db") or {}
    env_vars = {
        "MODE": mode,
        "KEY": "%s-%s" % (p["id"], e["id"]),
        "REPO": repo_url(p),
        "GIT_PROVIDER": git_provider(p),
        "GIT_KEY": "~/.ssh/deployer_%s" % p["id"],
        "GIT_KEY_DATA": shared_git_key(cfg) if p.get("git_key_mode") == "shared" else "",
        "REF": ref,
        "TARGET": e["target"],
        "PHP": e.get("php") or "php",
        "POST_DEPLOY": e.get("post_deploy") or "",
        "EXCLUDES": "\n".join(e.get("excludes") or []),
        "ONLY_FROM": e.get("only_from") or "",
        "DB_NAME": db.get("name") or "",
        "DB_USER": db.get("user") or "",
        "DB_HOST": db.get("host") or "localhost",
        "DB_PASS": kc_get(kc_account(p["id"], e["id"])) if db.get("name") else "",
    }
    if os.environ.get("DEPLOY_HOME"):
        env_vars["DEPLOY_HOME"] = os.environ["DEPLOY_HOME"]
    with open(REMOTE_SCRIPT, encoding="utf-8") as f:
        script = f.read()
    write(("→ %s@%s\n" % (server["user"], server["host"])).encode())
    return run_remote(server, write, with_vars(script, env_vars))


SERVER_CHECK = r'''
echo "✓ Connection works: $(whoami)@$(hostname)"
for c in git rsync mysqldump; do
  if command -v $c >/dev/null; then echo "✓ $c"; else echo "⚠️  $c is not installed"; fi
done
TK=""
if [ -n "${GIT_KEY_DATA:-}" ]; then
  TK=$(mktemp); chmod 600 "$TK"; printf '%s\n' "$GIT_KEY_DATA" > "$TK"; unset GIT_KEY_DATA
  trap 'rm -f "$TK"' EXIT
fi
O="-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
for h in bitbucket.org github.com; do
  if [ -n "$TK" ]; then
    echo "Access to $h with the shared Git key:"
    ssh -T $O -i "$TK" -o IdentitiesOnly=yes git@$h </dev/null 2>&1 | grep -v '^$' | head -2 | sed 's/^/   /'
  fi
  echo "Access to $h with the server user's default key:"
  ssh -T $O git@$h </dev/null 2>&1 | grep -v '^$' | head -2 | sed 's/^/   /'
done
'''

SERVER_KEY = r'''
set -e
K="$HOME/.ssh/id_ed25519"
if [ ! -f "$K" ]; then
  mkdir -p "$HOME/.ssh"; chmod 700 "$HOME/.ssh"
  ssh-keygen -q -t ed25519 -N "" -C "$(whoami)@$(hostname)-deploy" -f "$K" </dev/null
  echo "(server key has just been created)"
fi
echo "The server's default key. You can add it to:"
echo "  • Bitbucket → repository → Repository settings → Access keys (one key can be used in many repositories)"
echo "  • GitHub → repository → Settings → Deploy keys (GitHub allows a key in ONE repository only)"
echo
cat "$K.pub"
'''


def local_key(path, generate):
    path = os.path.expanduser(path or "~/.ssh/id_ed25519")
    pub = path + ".pub"
    if not os.path.exists(path):
        if not generate:
            return {"exists": False, "path": path}
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        r = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "deployer-mac",
                            "-f", path], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if r.returncode:
            raise ValueError("ssh-keygen: " + r.stderr.strip())
    if not os.path.exists(pub):
        r = subprocess.run(["ssh-keygen", "-y", "-f", path], capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        if r.returncode:
            raise ValueError(tr("Could not read the public key (does the key have a passphrase?)"))
        return {"exists": True, "path": path, "pub": r.stdout.strip()}
    with open(pub, encoding="utf-8") as f:
        return {"exists": True, "path": path, "pub": f.read().strip()}


def choose_folder():
    r = subprocess.run(["osascript", "-e", "activate", "-e",
                        'POSIX path of (choose folder with prompt "%s")' % tr("Choose the project folder")],
                       capture_output=True, text=True)
    if r.returncode:
        return {"cancel": True}
    return {"path": r.stdout.strip().rstrip("/")}


def _key_encrypted(path):
    r = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", path], capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    return r.returncode != 0


def ssh_keys():
    """Private keys in ~/.ssh."""
    d = os.path.expanduser("~/.ssh")
    out = []
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return out
    for n in names:
        p = os.path.join(d, n)
        if n.endswith(".pub") or not os.path.isfile(p):
            continue
        try:
            with open(p, "rb") as f:
                head = f.read(80)
        except OSError:
            continue
        if b"PRIVATE KEY" not in head:
            continue
        out.append({"path": "~/.ssh/" + n, "encrypted": _key_encrypted(p)})
    return out


def choose_key():
    script = ('POSIX path of (choose file with prompt "%s" '
              'default location (POSIX file "%s") with invisibles)' % (tr("Choose a private SSH key"),
                                                                       os.path.expanduser("~/.ssh")))
    r = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, text=True)
    if r.returncode:
        return {"cancel": True}
    p = r.stdout.strip()
    home = os.path.expanduser("~")
    if p.startswith(home + "/"):
        p = "~" + p[len(home):]
    return {"path": p, "encrypted": _key_encrypted(os.path.expanduser(p))}


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif re.match(r"^/locales/(en|uk|ru)\.json$", self.path):
            fp = os.path.join(LOCALES, self.path.rsplit("/", 1)[1])
            body = b"{}"
            if os.path.exists(fp):
                with open(fp, "rb") as f:
                    body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/ping":
            self._json({"app": "deployer", "base": BASE, "pid": os.getpid()})
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/askpass":  # called by askpass.py (from ssh); has its own token
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            ans = askpass_request(self.headers.get("X-Askpass-Token") or "", str(body.get("prompt") or ""))
            return self._json({"answer": ans})
        # The custom header keeps other websites in the browser from calling the API (CORS)
        if self.headers.get("X-Deployer") != "1":
            return self.send_error(403)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            route = self.path
            if route == "/api/config/get":
                return self._json(config_for_ui())
            if route == "/api/config/save":
                save_config(body.get("config"))
                return self._json(config_for_ui())
            if route == "/api/localkey":
                return self._json(local_key(body.get("path"), bool(body.get("generate"))))
            if route == "/api/choose-folder":
                return self._json(choose_folder())
            if route == "/api/choose-key":
                return self._json(choose_key())
            if route == "/api/git/remotes":
                path = os.path.expanduser(body.get("path") or "")
                if not os.path.isdir(path) or git(path, "rev-parse", "--git-dir")[0]:
                    return self._json({"ok": False, "remotes": []})
                rl = list_remotes(path)
                for r in rl:
                    sv = service_for_url(cfg_now(), r["url"])
                    r["service"] = sv["name"] if sv else url_host(r["url"])
                return self._json({"ok": True, "remotes": rl})
            if route == "/api/git/remote-add":
                path = os.path.expanduser(body.get("path") or "")
                name, url = (body.get("name") or "").strip(), (body.get("url") or "").strip()
                if not re.match(r"^[A-Za-z0-9._-]+$", name) or not url:
                    raise ValueError(tr("Enter a remote name (Latin letters) and the repository URL"))
                c, _, err = git(path, "remote", "add", name, url)
                if c:
                    raise ValueError("git remote add: " + err)
                return self._json({"ok": True})
            if route == "/api/git/remote-update":
                path = os.path.expanduser(body.get("path") or "")
                old = (body.get("name") or "").strip()
                new = (body.get("new_name") or old).strip()
                url = (body.get("url") or "").strip()
                if old not in {r["name"] for r in list_remotes(path)}:
                    raise ValueError(tr("Remote not found: {0}", old))
                if not re.match(r"^[A-Za-z0-9._-]+$", new) or not url:
                    raise ValueError(tr("Enter a remote name (Latin letters) and the repository URL"))
                if new != old:
                    c, _, err = git(path, "remote", "rename", old, new)
                    if c:
                        raise ValueError("git remote rename: " + err)
                c, _, err = git(path, "remote", "set-url", new, url)
                if c:
                    raise ValueError("git remote set-url: " + err)
                return self._json({"ok": True})
            if route == "/api/git/remote-remove":
                path = os.path.expanduser(body.get("path") or "")
                name = (body.get("name") or "").strip()
                if name not in {r["name"] for r in list_remotes(path)}:
                    raise ValueError(tr("Remote not found: {0}", name))
                c, _, err = git(path, "remote", "remove", name)
                if c:
                    raise ValueError("git remote remove: " + err)
                return self._json({"ok": True})
            if route == "/api/git/pushurl-add":
                path = os.path.expanduser(body.get("path") or "")
                name, url = (body.get("name") or "").strip(), (body.get("url") or "").strip()
                rem = {r["name"]: r for r in list_remotes(path)}
                if name not in rem:
                    raise ValueError(tr("Remote not found: {0}", name))
                if not url:
                    raise ValueError(tr("Enter the repository URL"))
                # The first explicit pushurl replaces the implicit one (the fetch URL),
                # so keep pushing to the original repository too.
                _, explicit, _ = git(path, "config", "--get-all", "remote.%s.pushurl" % name)
                if not explicit.strip():
                    c, _, err = git(path, "remote", "set-url", "--add", "--push", name, rem[name]["url"])
                    if c:
                        raise ValueError("git remote set-url --add --push: " + err)
                if _norm_url(url) not in {_norm_url(u) for u in rem[name]["push"]} | {_norm_url(rem[name]["url"])}:
                    c, _, err = git(path, "remote", "set-url", "--add", "--push", name, url)
                    if c:
                        raise ValueError("git remote set-url --add --push: " + err)
                return self._json({"ok": True})
            if route == "/api/git/pushurl-remove":
                path = os.path.expanduser(body.get("path") or "")
                name, url = (body.get("name") or "").strip(), (body.get("url") or "").strip()
                c, _, err = git(path, "remote", "set-url", "--delete", "--push", name, re.escape(url))
                if c:
                    raise ValueError("git remote set-url --delete: " + err)
                return self._json({"ok": True})
            if route == "/api/sshkeys":
                return self._json({"keys": ssh_keys()})
            if route == "/api/askpass/pending":
                return self._json({"prompts": [{k: v for k, v in it.items() if k in
                                               ("id", "prompt", "server", "retry", "kind")}
                                              for it in list(_prompts.values())]})
            if route == "/api/askpass/answer":
                it = _prompts.get(body.get("id"))
                if it:
                    a = body.get("answer")
                    it["answer"] = a if isinstance(a, str) else None
                    it["remember"] = bool(body.get("remember"))
                    it["event"].set()
                return self._json({"ok": True})
            if route == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=_server.shutdown, daemon=True).start()
                return
            cfg = load_config()
            if route == "/api/status":
                p, _ = find_project(cfg, body.get("project"))
                return self._json(project_status(p))
            if route == "/api/run":
                return self._run(cfg, body)
            if route == "/api/server":
                return self._server_action(cfg, body)
            if route == "/api/git/commit":
                p, _ = find_project(cfg, body.get("project"))
                return self._stream("git-" + p["id"], lambda w: commit_job(p, body.get("message"), w))
            if route == "/api/git/push":
                p, _ = find_project(cfg, body.get("project"))
                names = {r["name"] for r in list_remotes(project_path(p))}
                remotes = [r for r in (body.get("remotes") or []) if r in names] or push_remotes(p)
                branch = (body.get("branch") or "").strip() or current_branch(project_path(p))
                return self._stream("git-" + p["id"], lambda w: push_job(p, remotes, branch, w))
            if route == "/api/git/service-check":
                host = (body.get("host") or "").strip()
                if not re.match(r"^[A-Za-z0-9.-]+$", host):
                    raise ValueError(tr("Enter the service host, e.g. bitbucket.org"))
                return self._stream("svc-" + host, lambda w: service_check(host, w))
            self.send_error(404)
        except ValueError as ex:
            self._json({"error": tr_line(str(ex))}, 400)
        except Exception as ex:
            self._json({"error": "%s: %s" % (type(ex).__name__, ex)}, 500)

    def _stream(self, key, job):
        with _lock:
            if key in _running:
                raise ValueError(tr("Something is already running for {0}", key))
            _running.add(key)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            alive = [True]

            def write(b):
                if not alive[0]:
                    return
                try:
                    b = tr_line(b.decode("utf-8", "replace")).encode("utf-8")
                    self.wfile.write(b)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    alive[0] = False  # the window was closed, but let the process finish

            try:
                rc = job(write)
            except Exception as ex:
                write(("❌ %s\n" % ex).encode())
                rc = 1
            write(("\n[exit %d]\n" % rc).encode())
            return rc
        finally:
            with _lock:
                _running.discard(key)

    def _run(self, cfg, body):
        mode = body.get("mode")
        if mode not in ("check", "preview", "deploy", "repokey"):
            raise ValueError(tr("Unknown action"))
        p, e = find_project(cfg, body.get("project"), body.get("env"))
        find_server(cfg, e["server"])
        key = "%s-%s" % (p["id"], e["id"])
        ref = (body.get("ref") or e["branch"]).strip()
        rc = self._stream(key, lambda w: run_job(cfg, p, e, mode, ref, bool(body.get("push")), w))
        if mode == "deploy":
            with open(HISTORY, "a", encoding="utf-8") as f:
                f.write("%s  %s  %s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key, ref,
                                              "OK" if rc == 0 else "FAIL(%s)" % rc))

    def _server_action(self, cfg, body):
        s = dict(body.get("server") or {})  # an unsaved server can be tested too
        need = ("host", "user") + (("key",) if (s.get("auth") or "key") == "key" else ())
        for k in need:
            if not str(s.get(k) or "").strip():
                raise ValueError(tr("Fill in the IP, user and key") if "key" in need else tr("Fill in the IP and user"))
        script = {"check": SERVER_CHECK, "serverkey": SERVER_KEY}.get(body.get("action"))
        if not script:
            raise ValueError(tr("Unknown action"))
        if body.get("action") == "check":
            try:
                script = with_vars(script, {"GIT_KEY_DATA": shared_git_key(cfg)}).decode()
            except ValueError:
                pass  # no shared key: test only the server's keys
        self._stream("server-" + s["host"], lambda w: (
            w(("→ %s@%s\n" % (s["user"], s["host"])).encode()),
            run_remote(s, w, script.encode()))[1])


def main():
    global _server
    os.makedirs(DATA, exist_ok=True)
    try:
        _server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:
        if "DEPLOYER_PORT" in os.environ:
            raise
        # the preferred port is taken by another program: use any free one
        _server = ThreadingHTTPServer((HOST, 0), Handler)
    global _port
    port = _port = _server.server_address[1]
    port_file = os.path.join(DATA, "port")
    with open(port_file, "w") as f:
        f.write(str(port))
    url = "http://%s:%d" % (HOST, port)
    print("Deployer is running: %s" % url, flush=True)
    # SIGTERM (app closed) → graceful stop that removes the port file
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=_server.shutdown, daemon=True).start())
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            with open(port_file) as f:
                if f.read().strip() == str(port):
                    os.remove(port_file)
        except OSError:
            pass


if __name__ == "__main__":
    main()
