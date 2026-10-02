"""
Chronos UI "Control Room": a read-mostly view of the agents, skills, hooks, workspace health, plugins and MCP
servers that make up your Claude Code setup. Python 3.9+ stdlib only.

Read-only except ONE write: a hook's kill-switch file (<kill_switch_dir>/<name>.off, default ~/.chronos/switches).
A toggle is only offered for a hook whose SOURCE reads such a file (checked here, per request, from the script on disk),
so the UI never promises a switch that does nothing. The convention is described in docs/control-room.md.

It NEVER runs `claude mcp list`: that command's health check starts every configured MCP server, and a channel
plugin among them starts a second Telegram poller (HTTP 409) that kills your live channel. Plugins and MCP servers are
read from settings and config files only, and only names plus transport are surfaced (never args, env or headers).

Everything is rooted at the configured workspace (`workspace`) and Claude home (`claude_home`); nothing here is
specific to one person's folder layout.
"""
import datetime
import glob
import json
import os
import re
import shlex
import time

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
SCRIPT_RE = re.compile(r"""[^\s"';|&]+\.(?:py|sh)\b""")
OFF_RE = re.compile(r"""["'][^"'\n]*?([A-Za-z0-9][A-Za-z0-9_-]{1,40})\.off["']""")
MAX_CHECKS = 16


# --------------------------------------------------------------------------- tiny front-matter reader
def read_text(path, limit=400_000):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def front_matter(text):
    """Parse the leading ---/--- block: scalars, quoted scalars and > / | block scalars. Anything fancier is skipped."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    lines = text[3:end].strip("\n").split("\n")
    out, i = {}, 0
    while i < len(lines):
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", lines[i])
        if not m:
            i += 1
            continue
        key, val = m.group(1), m.group(2).strip()
        if val in (">", "|", ">-", "|-", ">+", "|+"):
            buf = []
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or not lines[i].strip()):
                buf.append(lines[i].strip())
                i += 1
            out[key] = " ".join(x for x in buf if x)
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key] = val
        i += 1
    return out


def clip(s, n):
    s = re.sub(r"[*_`]{1,3}", "", str(s or ""))  # prose often carries markdown emphasis
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "..."


# --------------------------------------------------------------------------- agents + skills
def list_agents(root, claude_home):
    rows = []
    for scope, base in (("project", os.path.join(root, ".claude", "agents")), ("user", os.path.join(claude_home, "agents"))):
        for p in sorted(glob.glob(os.path.join(base, "*.md"))):
            fm = front_matter(read_text(p))
            where = (".claude/agents/" if scope == "project" else "~/.claude/agents/") + os.path.basename(p)
            rows.append({"name": fm.get("name") or os.path.basename(p)[:-3], "description": clip(fm.get("description"), 260),
                         "model": fm.get("model") or "not declared", "source": where, "scope": scope})
    return rows


def list_skills(root, claude_home):
    rows, seen = [], set()
    for scope, base, label in (("project", os.path.join(root, ".claude", "skills"), ".claude/skills"),
                               ("user", os.path.join(claude_home, "skills"), "~/.claude/skills")):
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for n in names:
            f = os.path.join(base, n, "SKILL.md")
            if n.startswith(("_", ".")) or not os.path.isfile(f):
                continue
            fm = front_matter(read_text(f, 20_000))
            nm = fm.get("name") or n
            rows.append({"name": nm, "description": clip(fm.get("description"), 260), "source": label + "/" + n, "scope": scope,
                         "duplicate": nm in seen})
            seen.add(nm)
    return rows


# --------------------------------------------------------------------------- hooks
def _load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def script_paths(command, root):
    out = []
    for tok in SCRIPT_RE.findall(command):
        p = tok.replace("$CLAUDE_PROJECT_DIR", root).replace("${CLAUDE_PROJECT_DIR}", root).replace("$HOME", os.path.expanduser("~"))
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            p = os.path.join(root, p)
        if p not in out:
            out.append(p)
    return out


def honored_kill_switches(paths):
    """Kill-switch names whose .off file the script (or its same-stem sibling) really reads. Comment-only mentions do not count."""
    cands = []
    for p in paths:
        cands.append(p)
        stem = os.path.splitext(p)[0]
        for ext in (".py", ".sh"):
            q = stem + ext
            if q not in cands and os.path.isfile(q):
                cands.append(q)
    found = []
    for p in cands:
        for line in read_text(p, 300_000).split("\n"):
            # a string literal like "claim-gate.off" in CODE (comment lines and trailing comments are skipped)
            code = "" if line.lstrip().startswith("#") else line.split("  #", 1)[0]
            for m in OFF_RE.finditer(code):
                if m.group(1) not in found:
                    found.append(m.group(1))
    return found


def _home_label(p):
    h = os.path.expanduser("~")
    return "~" + p[len(h):] if p == h or p.startswith(h.rstrip(os.sep) + os.sep) else p


def list_hooks(root, kill_dir, claude_home):
    rows = []
    sources = (("project", os.path.join(root, ".claude", "settings.json")),
               ("project-local", os.path.join(root, ".claude", "settings.local.json")),
               ("user", os.path.join(claude_home, "settings.json")))
    for label, path in sources:
        d = _load_json(path)
        if not isinstance(d, dict) or not isinstance(d.get("hooks"), dict):
            continue
        for event, groups in d["hooks"].items():
            if not isinstance(groups, list):
                continue
            for g in groups:
                if not isinstance(g, dict):
                    continue
                for hk in g.get("hooks") or []:
                    cmd = str((hk or {}).get("command") or "")
                    paths = script_paths(cmd, root)
                    base, extra = "", ""
                    if paths:
                        base = os.path.basename(paths[-1])
                        m = re.match(r"^\s*(?:(?:bash|python3?|sh)\s+)?[^\s=;]+\.(?:py|sh)\b\s*([^;|&]*)$", cmd)
                        extra = clip(m.group(1), 40) if (m and len(paths) == 1) else ""
                    else:
                        try:
                            base = os.path.basename(shlex.split(cmd)[0]) if cmd.strip() else "(empty)"
                        except ValueError:
                            base = clip(cmd, 40)
                    kills = honored_kill_switches(paths) if paths else []
                    ks = None
                    if kills:
                        name = kills[0]
                        ks = {"name": name, "file": _home_label(os.path.join(kill_dir, name + ".off")),
                              "off": os.path.exists(os.path.join(kill_dir, name + ".off"))}
                    rows.append({"event": event, "matcher": clip(g.get("matcher") or "(all)", 90), "command": base + ((" " + extra) if extra else ""),
                                 "scope": label, "kill": ks})
    return rows


def kill_switch_names(root, kill_dir, claude_home):
    return {h["kill"]["name"] for h in list_hooks(root, kill_dir, claude_home) if h.get("kill")}


def set_kill(name, off, root, kill_dir, claude_home):
    """Create or remove <kill_dir>/<name>.off. Only for a name some registered hook's source really reads."""
    if not NAME_RE.match(str(name or "")) or name not in kill_switch_names(root, kill_dir, claude_home):
        return None
    p = os.path.join(kill_dir, name + ".off")
    os.makedirs(kill_dir, exist_ok=True)
    if off:
        open(p, "a").close()
    else:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return {"name": name, "off": os.path.exists(p)}


# --------------------------------------------------------------------------- workspace health (configurable)
def _resolve(root, p):
    p = os.path.expanduser(str(p or ""))
    return p if os.path.isabs(p) else os.path.join(root, p)


def _tile(label, value, sub, cls, pct=None, path=""):
    return {"label": label, "value": value, "sub": sub, "cls": cls, "pct": pct, "path": path}


def _ago(s):
    if s < 90:
        return "%ds ago" % s
    if s < 5400:
        return "%dm ago" % round(s / 60)
    if s < 172800:
        return "%dh ago" % round(s / 3600)
    return "%dd ago" % round(s / 86400)


def health_checks(root, checks, now=None):
    """Turn the `health_checks` config list into display tiles. A bad entry becomes a visible warning, never an error.

      {"label": "STATE size", "type": "chars", "path": "notes/STATE.md", "ceiling": 60000}   characters in a file vs a ceiling
      {"label": "Last lint",  "type": "age",   "path": "notes/_lint-*.md", "max_age_days": 9}   newest matching file's age
      {"label": "Daily log",  "type": "today", "path": "notes/%Y-%m-%d.md"}                     does today's file exist?
    """
    now = now or time.time()
    out = []
    for c in (checks or [])[:MAX_CHECKS]:
        if not isinstance(c, dict):
            continue
        label = clip(c.get("label") or c.get("path") or "check", 40)
        ty, path = c.get("type"), str(c.get("path") or "")
        try:
            if ty == "chars":
                ceiling = int(c.get("ceiling") or 0)
                p = _resolve(root, path)
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        n = len(fh.read())
                except OSError:
                    out.append(_tile(label, "missing", path + " not found", "bad", None, path))
                    continue
                if ceiling > 0:
                    pct = round(100.0 * n / ceiling, 1)
                    out.append(_tile(label, "%s / %s" % (format(n, ","), format(ceiling, ",")), "%s%% of the ceiling, counted in characters" % pct,
                                     "bad" if n > ceiling else ("warn" if pct > 95 else "ok"), min(100.0, pct), path))
                else:
                    out.append(_tile(label, format(n, ","), "characters", "ok", None, path))
            elif ty == "age":
                files = [f for f in glob.glob(_resolve(root, path)) if os.path.isfile(f)]
                if not files:
                    out.append(_tile(label, "none found", "nothing matches " + path, "warn", None, path))
                    continue
                newest = max(files, key=os.path.getmtime)
                age = int(now - os.path.getmtime(newest))
                days = age // 86400
                limit = c.get("max_age_days")
                cls = "warn" if (limit is not None and days > float(limit)) else "ok"
                out.append(_tile(label, _ago(age), "%s, modified %s" % (os.path.basename(newest),
                                 datetime.datetime.fromtimestamp(os.path.getmtime(newest)).strftime("%Y-%m-%d %H:%M")), cls, None, path))
            elif ty == "today":
                p = _resolve(root, time.strftime(path, time.localtime(now)))
                if os.path.isfile(p):
                    out.append(_tile(label, "present", "%s, %d bytes" % (os.path.basename(p), os.path.getsize(p)), "ok", None, path))
                else:
                    out.append(_tile(label, "not yet", os.path.basename(p) + " does not exist yet", "warn", None, path))
            else:
                out.append(_tile(label, "unknown type", "type must be chars, age or today", "warn", None, path))
        except (ValueError, TypeError) as e:
            out.append(_tile(label, "bad check", clip(str(e), 80), "warn", None, path))
    return out


# --------------------------------------------------------------------------- plugins + MCP (files only, names only)
def plugins_and_mcp(root, claude_home):
    seen = {}
    for label, path in (("user", os.path.join(claude_home, "settings.json")), ("project", os.path.join(root, ".claude", "settings.json")),
                        ("project-local", os.path.join(root, ".claude", "settings.local.json"))):
        d = _load_json(path)
        if not isinstance(d, dict) or not isinstance(d.get("enabledPlugins"), dict):
            continue
        for k, v in d["enabledPlugins"].items():
            name, _, mk = str(k).partition("@")
            seen[k] = {"name": name, "marketplace": mk, "enabled": bool(v), "source": label}
    plugins = sorted(seen.values(), key=lambda r: (not r["enabled"], r["name"]))
    mcp = []

    def add(name, cfg, source):
        if not isinstance(cfg, dict):
            return
        t = cfg.get("type") or ("http" if cfg.get("url") else "stdio")
        where = ""
        if cfg.get("url"):
            m = re.match(r"^\w+://([^/:?#]+)", str(cfg["url"]))
            where = m.group(1) if m else ""
        elif cfg.get("command"):
            where = os.path.basename(str(cfg["command"]))
        mcp.append({"name": str(name), "transport": str(t), "target": where, "source": source})

    for label, path in (("~/.claude/settings.json", os.path.join(claude_home, "settings.json")),
                        (".claude/settings.json", os.path.join(root, ".claude", "settings.json")),
                        (".mcp.json", os.path.join(root, ".mcp.json"))):
        d = _load_json(path)
        if isinstance(d, dict) and isinstance(d.get("mcpServers"), dict):
            for k, v in d["mcpServers"].items():
                add(k, v, label)
    # ~/.claude.json lives next to (not inside) the Claude home directory
    d = _load_json(os.path.join(os.path.dirname(claude_home.rstrip(os.sep)), ".claude.json"))
    if isinstance(d, dict):
        if isinstance(d.get("mcpServers"), dict):
            for k, v in d["mcpServers"].items():
                add(k, v, "~/.claude.json (user)")
        proj = (d.get("projects") or {}).get(root) if isinstance(d.get("projects"), dict) else None
        if isinstance(proj, dict) and isinstance(proj.get("mcpServers"), dict):
            for k, v in proj["mcpServers"].items():
                add(k, v, "~/.claude.json (this project)")
    return plugins, mcp


def collect(cfg):
    root, claude_home, kill_dir = cfg["workspace"], cfg["claude_home"], cfg["kill_switch_dir"]
    plugins, mcp = plugins_and_mcp(root, claude_home)
    return {"agents": list_agents(root, claude_home), "skills": list_skills(root, claude_home), "hooks": list_hooks(root, kill_dir, claude_home),
            "health": health_checks(root, cfg["health_checks"]), "plugins": plugins, "mcp": mcp,
            "disabled_in_runs": list(cfg["disable_plugins"]), "workspace": _home_label(root),
            "notes": {"mcp": "Read from settings and config files only. `claude mcp list` is never run: its health check starts every server, and a channel plugin "
                             "among them starts a second Telegram poller that knocks your live channel offline. Account-level connectors live on the account, "
                             "not in files, so they are not listed here.",
                      "kill": "A toggle appears only for hooks whose source reads a kill-switch file named <name>.off in %s. Flipping one takes effect on the hook's next run."
                              % _home_label(kill_dir)}}
