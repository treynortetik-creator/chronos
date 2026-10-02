"""
Chronos UI "Agent files": view and edit the markdown files that steer your agents (CLAUDE.md, agents, skills, notes,
auto-memory) from the Control Room. Python 3.9+ stdlib only.

THE SECURITY MODEL IS AN ALLOWLIST, REBUILT ON EVERY REQUEST. The client only ever sends an opaque id (a slug); the
server looks the id up in the list it just enumerated and uses the path it found there. No path, file name or directory
is ever taken from the client. Every enumerated file must also pass `allowed()`:
  * its REAL path (symlinks resolved) is inside the workspace or the Claude home (~/.claude)
  * not matched by the `agent_files_exclude` globs (for example a private notes folder), and no path component starts with ".env"
  * not a settings*.json (hooks and permissions stay view-only in the Control Room); only .md files
  * a regular file, not a directory, not a device
Writes go to the resolved real path (so a symlinked skill folder is edited in place and the link is never replaced).

Saves: sha256 of the bytes on disk at load time must still match (409 otherwise, nothing written), the previous bytes are
snapshotted to <home_dir>/history/agent-files/<id>/<timestamp>.md, then an atomic same-directory temp file + os.replace.
The sha is checked a second time just before the replace to shrink the window against your own agents and hooks that
edit the same files.
"""
import datetime
import glob
import hashlib
import os
import re
import secrets
import shlex
import subprocess
import threading

import control_room as CR

MAX_EDIT = 300_000          # bytes; bigger files are view-only (the request body cap is 400 KB)
MAX_VIEW = 1_500_000
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,100}$")
HIST_RE = re.compile(r"^(\d{8}-\d{6})(-\d+)?\.md$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
AGENT_WARN = "An agent, hook or scheduled job may also edit this file automatically. Keep edits small and save quickly."

BUILTIN_CATS = [("core", "Core instructions"), ("agents", "Agents"), ("skills", "Skills")]
LAST_CAT = ("automem", "Auto-memory (index and notes)")

_LOCK = threading.Lock()


class AFError(Exception):
    def __init__(self, status, msg, **extra):
        Exception.__init__(self, msg)
        self.status, self.msg, self.extra = status, msg, extra


class Cfg(object):
    def __init__(self, root, claude_home, history_dir, extra=None, exclude=None, lint=None, automem_dir=None):
        self.root = os.path.realpath(root)
        self.claude_home = os.path.realpath(claude_home)
        self.hist = os.path.join(history_dir, "agent-files")
        slug = re.sub(r"[^A-Za-z0-9]", "-", self.root)
        self.automem = automem_dir or os.path.join(self.claude_home, "projects", slug, "memory")
        self.extra = [e for e in (extra or []) if isinstance(e, dict) and e.get("glob")][:12]
        self.exclude = [str(x) for x in (exclude or []) if str(x).strip()][:40]
        self.lint = (lint or "").strip()

    @classmethod
    def from_config(cls, c):
        return cls(c["workspace"], c["claude_home"], c["history_dir"], c["agent_files_extra"], c["agent_files_exclude"], c["agent_files_lint"])


# --------------------------------------------------------------------------- allowlist
def _inside(p, root):
    return p == root or p.startswith(root.rstrip(os.sep) + os.sep)


def _glob_re(pat):
    """Translate a small glob (** crosses folders, * and ? do not) into an anchored regex."""
    out, i = "", 0
    while i < len(pat):
        c = pat[i]
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pat.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else ("[^/]" if c == "?" else re.escape(c))
        i += 1
    return re.compile("^" + out + "$")


def excluded(cfg, rp):
    """True when a configured exclude glob matches the real path (relative to the workspace, or absolute)."""
    for pat in cfg.exclude:
        pat = os.path.expanduser(pat).rstrip("/")
        targets = [rp] if os.path.isabs(pat) else ([os.path.relpath(rp, cfg.root)] if _inside(rp, cfg.root) else [])
        for t in targets:
            if _glob_re(pat).match(t) or _glob_re(pat + "/**").match(t):
                return True
    return False


def allowed(cfg, path):
    """-> real path if `path` may be exposed, else None."""
    try:
        rp = os.path.realpath(path)
        if not (_inside(rp, cfg.root) or _inside(rp, cfg.claude_home)):
            return None
        if excluded(cfg, rp):
            return None
        for part in rp.split(os.sep):
            if part.startswith(".env"):
                return None
        base = os.path.basename(rp).lower()
        if not base.endswith(".md") or (base.startswith("settings") and base.endswith(".json")):
            return None
        if not os.path.isfile(rp):
            return None
        return rp
    except (OSError, ValueError):
        return None


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:90] or "file"


def _home_label(p):
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if _inside(p, h) else p


def categories(cfg):
    cats = list(BUILTIN_CATS)
    for e in cfg.extra:
        cid, label = "x-" + _slug(str(e.get("category") or "notes")), str(e.get("category") or "Notes")[:40]
        if cid not in [c for c, _ in cats]:
            cats.append((cid, label))
    cats.append(LAST_CAT)
    return cats


def enumerate_files(cfg):
    """Every exposed file, in display order. Each: {id, cat, name, label, path (real), also, guard, agent_edited, ceiling}."""
    out, seen_real, seen_id = [], {}, set()

    def add(cat, p, label, name, slug_src, extra=None):
        rp = allowed(cfg, p)
        if not rp:
            return
        if rp in seen_real:
            seen_real[rp].setdefault("also", []).append(label)
            return
        i = _slug(slug_src)
        n = 1
        while i in seen_id:
            n += 1
            i = "%s-%d" % (_slug(slug_src), n)
        seen_id.add(i)
        e = {"id": i, "cat": cat, "name": name, "label": label, "path": rp}
        if extra:
            e.update(extra)
        seen_real[rp] = e
        out.append(e)

    R, H = cfg.root, cfg.claude_home
    add("core", os.path.join(R, "CLAUDE.md"), "CLAUDE.md", "CLAUDE.md (workspace)", "workspace-claude-md", {"guard": "claude-md" if cfg.lint else None})
    add("core", os.path.join(H, "CLAUDE.md"), "~/.claude/CLAUDE.md", "CLAUDE.md (global)", "home-claude-md")
    for scope, base, label in (("workspace", os.path.join(R, ".claude", "agents"), ".claude/agents/"), ("home", os.path.join(H, "agents"), "~/.claude/agents/")):
        for p in sorted(glob.glob(os.path.join(base, "*.md"))):
            b = os.path.basename(p)
            add("agents", p, label + b, b[:-3], "%s-agents-%s" % (scope, b))
    for scope, base, label in (("workspace", os.path.join(R, ".claude", "skills"), ".claude/skills/"), ("home", os.path.join(H, "skills"), "~/.claude/skills/")):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            names = []
        for n in names:
            if n.startswith(("_", ".")):
                continue
            add("skills", os.path.join(base, n, "SKILL.md"), "%s%s/SKILL.md" % (label, n), n, "%s-skill-%s" % (scope, n))
    for e in cfg.extra:
        cat = "x-" + _slug(str(e.get("category") or "notes"))
        pat = os.path.expanduser(str(e["glob"]))
        pat = pat if os.path.isabs(pat) else os.path.join(R, pat)
        try:
            ceiling = int(e["ceiling"]) if e.get("ceiling") else None
        except (TypeError, ValueError):
            ceiling = None
        for p in sorted(glob.glob(pat)):
            rel = os.path.relpath(os.path.realpath(p), cfg.root) if _inside(os.path.realpath(p), cfg.root) else _home_label(p)
            add(cat, p, rel, os.path.basename(p), "x-" + rel, {"agent_edited": bool(e.get("agent_edited")), "ceiling": ceiling,
                                                               "guard": "ceiling" if ceiling else None})
    add_am = lambda p, label, name, slug, extra=None: add("automem", p, label, name, slug, extra)
    add_am(os.path.join(cfg.automem, "MEMORY.md"), _home_label(os.path.join(cfg.automem, "MEMORY.md")), "MEMORY.md (index)", "automem-MEMORY-md", {"agent_edited": True})
    try:
        names = sorted(os.listdir(cfg.automem))
    except OSError:
        names = []
    for n in names:
        if n == "MEMORY.md" or n.startswith(".") or not n.endswith(".md"):
            continue
        add_am(os.path.join(cfg.automem, n), "auto-memory/" + n, n[:-3], "automem-" + n)
    return out


def find(cfg, fid):
    if not isinstance(fid, str) or not ID_RE.match(fid):
        raise AFError(400, "bad file id")
    for e in enumerate_files(cfg):
        if e["id"] == fid:
            rp = allowed(cfg, e["path"])  # re-check at use time (a symlink may have been swapped since enumeration)
            if not rp:
                raise AFError(403, "that file is not allowed")
            e["path"] = rp
            return e
    raise AFError(404, "no such agent file")


# --------------------------------------------------------------------------- reading
def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def describe(text, name=""):
    fm = CR.front_matter(text)
    d = fm.get("description")
    if not d:
        body = text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            body = text[end + 4:] if end > 0 else text
        head, prose = None, None
        for line in body.splitlines():
            s = line.strip().lstrip(">").strip()
            if not s or s.startswith(("<!--", "```", "---", "|", "- [", "===")):
                continue
            if s.startswith("#"):
                if head is None:
                    head = s.lstrip("#").strip()
                continue
            prose = s
            break
        stem = re.sub(r"\.md$", "", name or "", flags=re.I).lower()
        h = (head or "").lower()
        if head and not (h == stem or h == (name or "").lower() or h.endswith(".md")):
            d = head
        else:
            d = prose or head
    return CR.clip(d or "", 170)


def _head(path, n=6000):
    try:
        with open(path, "rb") as fh:
            return fh.read(n).decode("utf-8", errors="replace")
    except OSError:
        return ""


def list_all(cfg):
    cats = categories(cfg)
    groups = {c: [] for c, _ in cats}
    for e in enumerate_files(cfg):
        try:
            st = os.stat(e["path"])
        except OSError:
            continue
        groups.setdefault(e["cat"], []).append({"id": e["id"], "name": e["name"], "path": e["label"], "size": st.st_size, "mtime": _iso(st.st_mtime),
                                                "description": describe(_head(e["path"]), os.path.basename(e["path"])),
                                                "agent_edited": bool(e.get("agent_edited")), "also": e.get("also") or []})
    return {"categories": [{"id": c, "label": lab, "files": groups[c]} for c, lab in cats],
            "count": sum(len(v) for v in groups.values())}


def _classify(raw):
    """-> (text, crlf, reason) ; reason is None when the file is safely editable as text."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace"), False, "not valid UTF-8, so it is view-only"
    if "\x00" in text:
        return text, False, "contains binary bytes, so it is view-only"
    crlf = "\r\n" in text
    if "\r" in text.replace("\r\n", ""):
        return text, crlf, "has mixed or old-Mac line endings, so it is view-only (saving would rewrite them)"
    if crlf and text.count("\r\n") != text.count("\n"):
        return text, crlf, "has mixed line endings, so it is view-only (saving would rewrite them)"
    if len(raw) > MAX_EDIT:
        return text, crlf, "is over %d KB, so it is view-only" % (MAX_EDIT // 1000)
    return text, crlf, None


def history_list(cfg, fid):
    d = os.path.join(cfg.hist, fid)
    rows = []
    try:
        names = sorted(os.listdir(d), reverse=True)
    except OSError:
        names = []
    for n in names[:80]:
        m = HIST_RE.match(n)
        if m:
            try:
                size = os.path.getsize(os.path.join(d, n))
            except OSError:
                size = 0
            try:
                ts = datetime.datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").isoformat(timespec="seconds")
            except ValueError:
                ts = m.group(1)
            rows.append({"file": n, "ts": ts, "size": size})
    return rows


def read_file(cfg, fid):
    e = find(cfg, fid)
    with open(e["path"], "rb") as fh:
        raw = fh.read(MAX_VIEW + 1)
    if len(raw) > MAX_VIEW:
        raise AFError(413, "file is too large to open here")
    st = os.stat(e["path"])
    text, crlf, reason = _classify(raw)
    if crlf:
        text = text.replace("\r\n", "\n")
    return {"id": e["id"], "cat": e["cat"], "name": e["name"], "path": e["label"], "also": e.get("also") or [], "content": text,
            "sha": _sha(raw), "size": st.st_size, "mtime": _iso(st.st_mtime), "chars": len(text), "crlf": crlf,
            "editable": reason is None, "reason": reason, "guard": e.get("guard"),
            "warn": AGENT_WARN if e.get("agent_edited") else None, "ceiling": e.get("ceiling"),
            "history": history_list(cfg, e["id"])}


def read_history(cfg, fid, fname):
    find(cfg, fid)
    if not HIST_RE.match(str(fname or "")):
        raise AFError(400, "bad history file name")
    d = os.path.join(cfg.hist, fid)
    p = os.path.join(d, fname)
    if os.path.dirname(os.path.realpath(p)) != os.path.realpath(d) or not os.path.isfile(p):
        raise AFError(404, "no such version")
    with open(p, "rb") as fh:
        return fh.read().decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- writing
def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _snapshot(cfg, fid, raw):
    d = os.path.join(cfg.hist, fid)
    os.makedirs(d, exist_ok=True)
    base = _stamp()
    p = os.path.join(d, base + ".md")
    n = 1
    while os.path.exists(p):
        n += 1
        p = os.path.join(d, "%s-%d.md" % (base, n))
    with open(p, "wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    return os.path.basename(p)


def _atomic_write(path, raw):
    d = os.path.dirname(path)
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    tmp = os.path.join(d, ".%s.tmp.%d.%s" % (os.path.basename(path), os.getpid(), secrets.token_hex(3)))
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def run_lint(cfg):
    """Run the configured `agent_files_lint` command (argv, no shell) from the workspace. The command comes from config.json."""
    if not cfg.lint:
        return {"ran": False, "ok": None, "output": "no agent_files_lint command is configured", "exit": None}
    try:
        argv = shlex.split(os.path.expanduser(cfg.lint))
        r = subprocess.run(argv, cwd=cfg.root, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        out = r.stdout.decode("utf-8", errors="replace")
        return {"ran": True, "ok": r.returncode == 0, "exit": r.returncode, "output": out[-4000:]}
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "exit": None, "output": "lint timed out after 30 s"}
    except (OSError, ValueError) as ex:
        return {"ran": False, "ok": None, "output": "could not run lint: %s" % ex, "exit": None}


def _guard_result(cfg, e, text):
    g = {}
    if e.get("guard") == "claude-md":
        g["lint"] = run_lint(cfg)
    if e.get("guard") == "ceiling" and e.get("ceiling"):
        g["ceiling"] = {"chars": len(text), "ceiling": e["ceiling"], "over": len(text) > e["ceiling"]}
    return g


def save_file(cfg, fid, content, base_sha, _restore=False):
    e = find(cfg, fid)
    if not isinstance(content, str):
        raise AFError(400, "content must be text")
    if "\x00" in content:
        raise AFError(400, "binary content refused")
    if not _restore and not (isinstance(base_sha, str) and SHA_RE.match(base_sha)):
        raise AFError(400, "base_sha is required: the page must say which version it opened")
    with _LOCK:
        with open(e["path"], "rb") as fh:
            cur = fh.read()
        cur_sha = _sha(cur)
        if not _restore and cur_sha != base_sha:
            raise AFError(409, "%s changed on disk since you opened it. Reload before saving; nothing was written." % e["name"],
                          current_sha=cur_sha, current_mtime=_iso(os.stat(e["path"]).st_mtime), changed=True)
        _, crlf, reason = _classify(cur)
        if reason:
            raise AFError(403, "this file %s" % reason)
        text = content.replace("\r\n", "\n")
        if "\r" in text:
            raise AFError(400, "stray carriage returns refused")
        new = (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
        if len(new) > MAX_EDIT:
            raise AFError(413, "file would be over %d KB" % (MAX_EDIT // 1000))
        if new == cur:
            return {"saved": False, "unchanged": True, "sha": cur_sha, "size": len(cur), "chars": len(text), "guard": _guard_result(cfg, e, text)}
        snap = _snapshot(cfg, fid, cur)
        with open(e["path"], "rb") as fh:  # second check, immediately before the replace
            if _sha(fh.read()) != cur_sha:
                raise AFError(409, "%s changed on disk while saving. Reload and try again; nothing was written." % e["name"], changed=True)
        _atomic_write(e["path"], new)
        st = os.stat(e["path"])
    res = {"saved": True, "sha": _sha(new), "size": st.st_size, "mtime": _iso(st.st_mtime), "chars": len(text), "snapshot": snap}
    res["guard"] = _guard_result(cfg, e, text)
    return res


def restore_file(cfg, fid, fname):
    if not HIST_RE.match(str(fname or "")):
        raise AFError(400, "bad history file name")
    txt = read_history(cfg, fid, fname)
    # the restore replaces whatever is on disk; the current version is snapshotted first, so it is undoable
    return save_file(cfg, fid, txt.replace("\r\n", "\n"), None, _restore=True)
