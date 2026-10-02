"""
chronos_triggers: event triggers for Chronos. Python 3.9+, standard library only.

A job in jobs.json may carry   "triggers": [ {...}, ... ]   next to (or, with "clock": false, instead of) its
clock schedule. bin/chronos-tick.sh runs `bin/chronos triggers` every 5 minutes AFTER it has handled the clock
jobs, so a slow poll can never delay a scheduled job. Each hit prints one line

    id|eventhash|eventfile|type

and the tick claims claim-<id>-<date>-<eventhash> (atomic mkdir) and starts bin/chronos-run.sh in event mode.

Trigger types
  {"type":"file","path":"/abs/dir","glob":"*.pdf"}            new or modified file (settled 30 s) in a folder
  {"type":"gmail","query":"from:x","body":false}              mail matching a Gmail search, through the `gmail_command` adapter
  {"type":"github","repo":"owner/name","event":"pr"}          new pull requests / issues / releases, through `gh api`
  {"type":"webhook"}                                          POST /hook/<id> on the UI server (127.0.0.1 only), header X-Chronos-Secret
  optional on any: "interval_min" (poll spacing for gmail/github, default 10)

State: <home_dir>/trigger-state/<id>.json (seen ids, file mtimes, recent fire times). The FIRST evaluation of a
trigger only records a baseline and fires nothing, so wiring a trigger to a full inbox never replays history.

Degrading cleanly: a Gmail trigger without a `gmail_command`, or a GitHub trigger without the `gh` CLI, does not
crash the tick. The trigger records `last_error` (shown in the UI, "Test" says why) and every other trigger keeps working.

Security: every payload is UNTRUSTED. It is stripped of control and invisible-format characters, capped at 4,000
characters and wrapped in a delimited, nonce-marked DATA block. Event runs never get --dangerously-skip-permissions
(see chronos-run.sh and chronoslib.event_settings). Rate limit: `event_rate_per_hour` fires per job (default 6).
"""
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata

import chronoslib as C

MAX_PAYLOAD = 4000
SETTLE_S = 30
QUEUE_CAP = 20
QUEUE_MAX_AGE_S = 24 * 3600
HOOK_BODY_MAX = 64 * 1024
DEFAULT_INTERVAL_MIN = {"gmail": 10, "github": 10}
TYPES = ("file", "gmail", "github", "webhook")
GITHUB_EVENTS = ("pr", "issue", "release")
ID_RE = C.ID_RE
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
GLOB_RE = re.compile(r"^[\w.*?\[\]!\- ,+@#%()]{1,80}$")
MARKER_WORD = "UNTRUSTED EVENT DATA"
CHRONOS_TAG = "[chronos]"          # mail whose subject carries this tag never fires a trigger (so Chronos-sent mail cannot loop)


class TriggerError(Exception):
    pass


# --------------------------------------------------------------------------- sanitising
def sanitize(text, limit=MAX_PAYLOAD):
    """Strip control + invisible formatting characters (keep \\n and \\t), neutralise our own delimiter
    word, cap at `limit` characters (the cap includes the truncation note)."""
    s = "" if text is None else str(text)
    out = []
    for ch in s:
        if ch in "\n\t":
            out.append(ch)
            continue
        if ch == "\r":
            continue
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Co", "Cs", "Cn") or ch in "  ":
            continue
        out.append(ch)
    s = "".join(out)
    s = re.sub(re.escape(MARKER_WORD), "[marker removed]", s, flags=re.I)
    s = re.sub(r"\n{4,}", "\n\n\n", s)
    if len(s) > limit:
        note = "\n[truncated]"
        s = s[: limit - len(note)] + note
    return s


def one_line(text, n=140):
    return re.sub(r"\s+", " ", sanitize(text, 2000)).strip()[:n]


# --------------------------------------------------------------------------- validation
def validate_trigger(t):
    """Return a clean trigger dict or raise ValueError with a plain message."""
    if not isinstance(t, dict):
        raise ValueError("a trigger must be an object")
    ty = t.get("type")
    if ty not in TYPES:
        raise ValueError("trigger type must be one of: " + ", ".join(TYPES))
    out = {"type": ty}
    if ty == "file":
        p = str(t.get("path") or "").strip()
        if "\x00" in p or not p:
            raise ValueError("a folder path is required")
        p = os.path.normpath(os.path.expanduser(p))
        if not os.path.isabs(p):
            raise ValueError("the folder path must be absolute (start with / or ~)")
        g = str(t.get("glob") or "*").strip()
        if not GLOB_RE.match(g) or "/" in g:
            raise ValueError("the file pattern may only use letters, digits and * ? [ ] . - _ (no slashes)")
        out.update(path=p, glob=g)
    elif ty == "gmail":
        q = str(t.get("query") or "").strip()
        if not (1 <= len(q) <= 200):
            raise ValueError("a Gmail search is required (up to 200 characters)")
        if re.search(r"[\x00-\x1f'\\`$]", q):
            raise ValueError("the Gmail search may not contain single quotes, backslashes, backticks or $")
        out["query"] = q
        out["body"] = bool(t.get("body"))
    elif ty == "github":
        r = str(t.get("repo") or "").strip()
        if not REPO_RE.match(r):
            raise ValueError("repo must look like owner/name")
        ev = str(t.get("event") or "pr").strip()
        if ev not in GITHUB_EVENTS:
            raise ValueError("github event must be pr, issue or release")
        out.update(repo=r, event=ev)
    if "interval_min" in t and t["interval_min"] not in (None, ""):
        try:
            iv = int(t["interval_min"])
        except (TypeError, ValueError):
            raise ValueError("interval_min must be a whole number of minutes")
        if iv < 5 or iv > 1440:
            raise ValueError("interval_min must be between 5 and 1440")
        out["interval_min"] = iv
    return out


def trigger_key(t):
    core = {k: v for k, v in t.items() if k not in ("interval_min",)}
    return "%s-%s" % (t["type"], hashlib.sha1(json.dumps(core, sort_keys=True).encode()).hexdigest()[:8])


def event_hash(ty, eid):
    return hashlib.sha1(("%s|%s" % (ty, eid)).encode("utf-8", "replace")).hexdigest()[:10]


# --------------------------------------------------------------------------- files
def _atomic_json(path, obj, mode=0o600):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(cfg, jid):
    try:
        with open(os.path.join(cfg["trigger_state_dir"], jid + ".json"), encoding="utf-8") as fh:
            st = json.load(fh)
        if isinstance(st, dict):
            st.setdefault("triggers", {})
            st.setdefault("fires", [])
            return st
    except (OSError, ValueError):
        pass
    return {"triggers": {}, "fires": []}


def save_state(cfg, jid, st):
    _atomic_json(os.path.join(cfg["trigger_state_dir"], jid + ".json"), st)


class JobLock(object):
    """Non-blocking per-job flock so two overlapping ticks (or a tick and a UI test) never double-fire."""

    def __init__(self, cfg, jid):
        self.cfg, self.jid, self.fh = cfg, jid, None

    def __enter__(self):
        os.makedirs(self.cfg["trigger_state_dir"], exist_ok=True)
        self.fh = open(os.path.join(self.cfg["trigger_state_dir"], self.jid + ".lock"), "a")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            self.fh = None
        return self.fh is not None

    def __exit__(self, *a):
        if self.fh:
            try:
                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()


def fires_last_hour(st, now):
    return [x for x in st.get("fires", []) if isinstance(x, (int, float)) and now - x < 3600]


# --------------------------------------------------------------------------- webhook secrets and queue
def _ensure_dir(d):
    os.makedirs(d, mode=0o700, exist_ok=True)
    try:
        os.chmod(d, 0o700)
    except OSError:
        pass


def secret_path(cfg, jid):
    return os.path.join(cfg["hook_secrets_dir"], jid)


def get_secret(cfg, jid):
    try:
        with open(secret_path(cfg, jid), encoding="utf-8") as fh:
            s = fh.read().strip()
        return s if len(s) >= 24 else None
    except OSError:
        return None


def ensure_secret(cfg, jid, rotate=False):
    _ensure_dir(cfg["hook_secrets_dir"])
    if rotate:
        delete_secret(cfg, jid)
    s = get_secret(cfg, jid)
    if s:
        return s
    s = secrets.token_urlsafe(32)
    fd = os.open(secret_path(cfg, jid), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(s + "\n")
    os.chmod(secret_path(cfg, jid), 0o600)
    return s


def delete_secret(cfg, jid):
    try:
        os.remove(secret_path(cfg, jid))
    except OSError:
        pass


def queue_len(cfg, jid):
    try:
        return len([n for n in os.listdir(os.path.join(cfg["trigger_queue_dir"], jid)) if n.endswith(".json")])
    except OSError:
        return 0


def clear_queue(cfg, jid):
    shutil.rmtree(os.path.join(cfg["trigger_queue_dir"], jid), ignore_errors=True)


def enqueue_webhook(cfg, jid, body, ctype="", remote=""):
    """Store one webhook delivery. Returns its event hash. Raises TriggerError when the queue is full."""
    d = os.path.join(cfg["trigger_queue_dir"], jid)
    _ensure_dir(cfg["trigger_queue_dir"])
    _ensure_dir(d)
    if queue_len(cfg, jid) >= QUEUE_CAP:
        raise TriggerError("queue full")
    nonce = "%d-%s" % (time.time_ns(), secrets.token_hex(3))
    rec = {"received": time.strftime("%Y-%m-%dT%H:%M:%S"), "received_ts": time.time(), "ctype": one_line(ctype, 80),
           "remote": one_line(remote, 60), "body": body[:HOOK_BODY_MAX]}
    _atomic_json(os.path.join(d, nonce + ".json"), rec)
    return event_hash("webhook", nonce)


# --------------------------------------------------------------------------- evaluators
def _run(cmd, timeout):
    try:
        p = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TriggerError("timed out after %ds" % timeout)
    except OSError as e:
        raise TriggerError("could not start %s: %s" % (os.path.basename(cmd[0]), e.strerror or e))
    return p.returncode, p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")


def _ev(ty, eid, summary, lines, mark):
    payload = sanitize("\n".join("%s: %s" % (k, v) for k, v in lines if v not in (None, "")))
    return {"type": ty, "id": str(eid), "hash": event_hash(ty, eid), "summary": one_line(summary), "payload": payload, "_mark": mark}


def eval_file(cfg, t, ts, now):
    d = t["path"]
    try:
        names = sorted(os.listdir(d))
    except OSError as e:
        raise TriggerError("cannot read the folder: %s" % (e.strerror or e))
    old = ts.get("files")
    cur, settling = {}, 0
    for n in names:
        if n.startswith(".") or not fnmatch.fnmatch(n, t["glob"]):
            continue
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
            continue
        if now - st.st_mtime < SETTLE_S:
            settling += 1
            continue
        cur[p] = [int(st.st_mtime), st.st_size]
    if old is None:
        ts2 = dict(ts)
        ts2["files"] = dict(list(cur.items())[:5000])
        return [], ts2, {"baseline": True, "count": len(cur), "settling": settling}
    events, base = [], {}
    for p, v in cur.items():
        if p not in old or old[p] != v:
            kind = "new file" if p not in old else "modified file"
            ev = _ev("file", "%s|%s|%s" % (p, v[0], v[1]), "%s: %s" % (kind, os.path.basename(p)),
                     [("event", "file " + ("created" if p not in old else "modified")), ("path", p), ("name", os.path.basename(p)),
                      ("size_bytes", v[1]), ("modified", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v[0])))],
                     ("files", p, v))
            events.append(ev)
            if p in old:
                base[p] = old[p]
        else:
            base[p] = v
    ts2 = dict(ts)
    ts2["files"] = dict(list(base.items())[:5000])
    return events, ts2, {"baseline": False, "count": len(cur), "settling": settling}


def gmail_command(cfg):
    """The configured Gmail adapter as an argv prefix, or a TriggerError that says how to set one up."""
    cmd = os.environ.get("CHRONOS_GMAIL_COMMAND") or cfg.get("gmail_command") or ""
    if not cmd.strip():
        raise TriggerError("no Gmail adapter is configured: set gmail_command in config.json (see docs/triggers.md)")
    argv = shlex.split(os.path.expanduser(cmd))
    if not argv or not shutil.which(argv[0]):
        raise TriggerError("the Gmail adapter %r was not found or is not executable" % (argv[0] if argv else cmd))
    return argv


def eval_gmail(cfg, t, ts, now):
    cmd = gmail_command(cfg) + [t["query"] + " newer_than:1d", "10"]
    if t.get("body"):
        cmd.append("--body")
    rc, out, err = _run(cmd, 90)
    try:
        data = json.loads(out)
    except ValueError:
        raise TriggerError("the Gmail adapter did not return JSON (exit %d): %s" % (rc, one_line(err or out, 120)))
    if not isinstance(data, dict) or "emails" not in data:
        raise TriggerError("the Gmail adapter reply had no `emails` key: %s" % one_line(out, 120))
    emails = [e for e in data["emails"] if isinstance(e, dict) and e.get("id")]
    seen = ts.get("seen")
    ts2 = dict(ts)
    keep = {k: v for k, v in (seen or {}).items() if now - float(v) < 3 * 86400}
    if seen is None:
        ts2["seen"] = {str(e["id"]): now for e in emails}
        return [], ts2, {"baseline": True, "count": len(emails)}
    events, skipped = [], 0
    for e in emails:
        mid = str(e["id"])
        if mid in keep:
            continue
        subj = str(e.get("subject") or "")
        if CHRONOS_TAG in subj.lower():
            keep[mid] = now
            skipped += 1
            continue
        lines = [("event", "email received"), ("from", e.get("from")), ("to", e.get("to")), ("date", e.get("date")),
                 ("subject", subj), ("message_id", mid), ("has_attachments", e.get("hasAttachments")),
                 ("attachments", ", ".join(map(str, e.get("attachmentNames") or [])))]
        if t.get("body") and e.get("body"):
            lines.append(("body", "\n" + str(e.get("body"))))
        events.append(_ev("gmail", mid, "email from %s: %s" % (one_line(e.get("from"), 40), subj), lines, ("seen", mid, now)))
    ts2["seen"] = keep
    return events, ts2, {"baseline": False, "count": len(emails), "skipped_chronos": skipped}


def _gh(cfg):
    """The gh CLI: CHRONOS_GH, else config gh_bin (an explicit path is never second-guessed), else PATH and the usual install spots."""
    explicit = os.environ.get("CHRONOS_GH") or cfg.get("gh_bin")
    candidates = [explicit] if explicit else [shutil.which("gh"), "/opt/homebrew/bin/gh", "/usr/local/bin/gh"]
    for c in candidates:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    raise TriggerError("the gh command line tool was not found%s (install it from https://cli.github.com and run `gh auth login`, "
                       "or set gh_bin in config.json)" % (" at %s" % explicit if explicit else ""))


def eval_github(cfg, t, ts, now):
    repo, ev = t["repo"], t["event"]
    path = {"pr": "repos/%s/pulls?state=all&sort=created&direction=desc&per_page=10",
            "issue": "repos/%s/issues?state=all&sort=created&direction=desc&per_page=10",
            "release": "repos/%s/releases?per_page=10"}[ev] % repo
    rc, out, err = _run([_gh(cfg), "api", path], 60)
    if rc != 0:
        raise TriggerError("gh api failed: %s" % one_line(err or out, 160))
    try:
        items = json.loads(out)
    except ValueError:
        raise TriggerError("gh did not return JSON")
    if not isinstance(items, list):
        raise TriggerError("unexpected gh reply")
    rows = []
    for it in items:
        if not isinstance(it, dict):
            continue
        if ev == "issue" and "pull_request" in it:
            continue
        iid = str(it.get("number") if ev != "release" else it.get("id"))
        if iid in ("None", ""):
            continue
        rows.append((ev + "-" + iid, it))
    seen = ts.get("seen")
    ts2 = dict(ts)
    if seen is None:
        ts2["seen"] = {k: now for k, _ in rows}
        return [], ts2, {"baseline": True, "count": len(rows)}
    keep = dict(seen)
    events = []
    for k, it in sorted(rows, key=lambda r: str(r[1].get("created_at") or r[1].get("published_at") or "")):
        if k in keep:
            continue
        user = (it.get("user") or it.get("author") or {}).get("login")
        title = it.get("title") or it.get("name") or it.get("tag_name")
        lines = [("event", {"pr": "pull request", "issue": "issue", "release": "release"}[ev]), ("repo", repo),
                 ("number" if ev != "release" else "tag", it.get("number") or it.get("tag_name")), ("title", title),
                 ("author", user), ("state", it.get("state")), ("url", it.get("html_url")),
                 ("created", it.get("created_at") or it.get("published_at")),
                 ("text", "\n" + str(it.get("body") or "")[:1500])]
        events.append(_ev("github", k, "%s %s: %s" % (repo, ev, title), lines, ("seen", k, now)))
    newest = sorted(keep.items(), key=lambda kv: kv[1], reverse=True)[:200]
    ts2["seen"] = dict(newest)
    return events, ts2, {"baseline": False, "count": len(rows)}


def eval_webhook(cfg, t, ts, now, jid=None):
    d = os.path.join(cfg["trigger_queue_dir"], jid or "")
    events = []
    try:
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except OSError:
        names = []
    for n in names:
        p = os.path.join(d, n)
        try:
            with open(p, encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        if now - float(rec.get("received_ts") or 0) > QUEUE_MAX_AGE_S:
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        eid = n[:-5]
        events.append(_ev("webhook", eid, "webhook delivery %s" % rec.get("received", ""),
                          [("event", "webhook POST"), ("received", rec.get("received")), ("content_type", rec.get("ctype")),
                           ("body", "\n" + str(rec.get("body") or ""))], ("queue_file", p)))
    return events, dict(ts), {"baseline": False, "count": len(events)}


def evaluate(cfg, jid, t, ts, now):
    ty = t["type"]
    if ty == "file":
        return eval_file(cfg, t, ts, now)
    if ty == "gmail":
        return eval_gmail(cfg, t, ts, now)
    if ty == "github":
        return eval_github(cfg, t, ts, now)
    return eval_webhook(cfg, t, ts, now, jid)


def apply_mark(ts, mark, commit=True):
    kind = mark[0]
    if kind == "files":
        ts.setdefault("files", {})[mark[1]] = mark[2]
    elif kind == "seen":
        ts.setdefault("seen", {})[mark[1]] = mark[2]
    elif kind == "queue_file" and commit:
        try:
            os.remove(mark[1])
        except OSError:
            pass


# --------------------------------------------------------------------------- test (UI "Test" button)
def test_trigger(cfg, jid, raw_trigger):
    """Evaluate one trigger once, read-only. Never writes state, never runs the job."""
    t = validate_trigger(raw_trigger)
    now = time.time()
    st = load_state(cfg, jid)
    ts = st["triggers"].get(trigger_key(t), {})
    cap = cfg["event_rate_per_hour"]
    res = {"type": t["type"], "ok": True, "error": None, "baseline": False, "would_fire": [], "count": 0,
           "budget_left": max(0, cap - len(fires_last_hour(st, now))), "budget_total": cap, "note": ""}
    try:
        events, ts2, info = evaluate(cfg, jid, t, ts, now)
    except TriggerError as e:
        res.update(ok=False, error=str(e))
        return res
    res["count"] = info.get("count", 0)
    if info.get("baseline"):
        res["baseline"] = True
        res["note"] = ("First check: Chronos would record the %d item(s) that already match as a baseline and fire nothing. "
                       "Only things that arrive after that fire the job." % info.get("count", 0))
    for e in events[:20]:
        res["would_fire"].append({"summary": e["summary"], "hash": e["hash"], "payload_preview": e["payload"][:400]})
    if not events and not info.get("baseline"):
        res["note"] = "Nothing new right now. %d item(s) currently match and are already seen." % info.get("count", 0)
    if t["type"] == "webhook":
        res["note"] = ("%d delivery(ies) waiting in the queue. The secret is %s." %
                       (len(events), "set" if get_secret(cfg, jid) else "NOT set yet (save the trigger to create it)"))
    if info.get("settling"):
        res["note"] += " %d file(s) are still being written and are skipped until they settle." % info["settling"]
    if info.get("skipped_chronos"):
        res["note"] += " %d message(s) tagged [Chronos] were ignored." % info["skipped_chronos"]
    return res


# --------------------------------------------------------------------------- tick
def event_file_text(jid, ev):
    nonce = secrets.token_hex(4)
    return ("EVENT TYPE: %s\nEVENT ID: %s\nBEGIN-NONCE: %s\n===== %s (begin %s) =====\n%s\n===== %s (end %s) =====\n"
            % (ev["type"], ev["hash"], nonce, MARKER_WORD, nonce, ev["payload"], MARKER_WORD, nonce))


def _log(msg):
    sys.stderr.write("%s %s\n" % (time.strftime("%F %T"), msg))


def prune_events(cfg, now):
    try:
        for n in os.listdir(cfg["trigger_events_dir"]):
            p = os.path.join(cfg["trigger_events_dir"], n)
            if now - os.path.getmtime(p) > 7 * 86400:
                os.remove(p)
    except OSError:
        pass


def tick(cfg, dry=False):
    """Evaluate every enabled job's triggers. Prints one `id|hash|eventfile|type` line per event to fire."""
    try:
        jobs = C.load_jobs(cfg)
    except Exception as e:
        _log("TRIGGERS jobs file unreadable (%s): %s" % (cfg["jobs_file"], e))
        return 3
    if os.path.exists(cfg["pause_all"]):
        return 0
    cap = cfg["event_rate_per_hour"]
    now = time.time()
    if not dry:
        prune_events(cfg, now)
    for j in jobs:
        try:
            if not isinstance(j, dict) or j.get("enabled") is not True or not j.get("triggers"):
                continue
            jid = str(j["id"])
            if not ID_RE.match(jid) or C.is_paused(cfg, jid) or C.is_command(j) or not os.path.isfile(C.prompt_file(cfg, jid)):
                continue
            with JobLock(cfg, jid) as got:
                if not got:
                    continue
                st = load_state(cfg, jid)
                fires = fires_last_hour(st, now)
                dirty = False
                for raw in j["triggers"]:
                    try:
                        t = validate_trigger(raw)
                    except ValueError as e:
                        _log("TRIGGERS %s: skipping invalid trigger: %s" % (jid, e))
                        continue
                    key = trigger_key(t)
                    ts = st["triggers"].setdefault(key, {})
                    iv = t.get("interval_min") or DEFAULT_INTERVAL_MIN.get(t["type"])
                    if iv and now - float(ts.get("last_poll") or 0) < iv * 60 - 45:
                        continue
                    try:
                        events, ts2, info = evaluate(cfg, jid, t, ts, now)
                    except TriggerError as e:
                        ts.update(last_poll=now, last_error=str(e), last_error_at=now)
                        _log("TRIGGERS %s %s: error: %s" % (jid, t["type"], e))
                        dirty = True
                        continue
                    ts2.update(last_poll=now, last_ok=now, last_error=None)
                    ts2["last_count"] = info.get("count", 0)
                    if info.get("baseline"):
                        _log("TRIGGERS %s %s: baseline recorded (%d existing items, nothing fires)" % (jid, t["type"], info.get("count", 0)))
                    st["triggers"][key] = ts2
                    dirty = True
                    for ev in events:
                        if len(fires) >= cap:
                            _log("TRIGGERS %s RATE-LIMITED (%d fires in the last hour); %s waits" % (jid, len(fires), ev["summary"]))
                            break
                        if dry:
                            print("DRYRUN would fire %s on %s event %s: %s" % (jid, ev["type"], ev["hash"], ev["summary"]))
                            fires.append(now)
                            continue
                        os.makedirs(cfg["trigger_events_dir"], exist_ok=True)
                        ef = os.path.join(cfg["trigger_events_dir"], "%s-%s-%s.txt" % (jid, time.strftime("%Y%m%d"), ev["hash"]))
                        with open(ef, "w", encoding="utf-8") as fh:
                            fh.write(event_file_text(jid, ev))
                        os.chmod(ef, 0o600)
                        apply_mark(ts2, ev["_mark"])
                        fires.append(now)
                        print("%s|%s|%s|%s" % (jid, ev["hash"], ef, ev["type"]))
                        sys.stdout.flush()
                st["fires"] = fires
                if dirty and not dry:
                    save_state(cfg, jid, st)
        except Exception as e:  # one bad job never stops the others
            _log("TRIGGERS error on job %r: %r" % (j.get("id") if isinstance(j, dict) else j, e))
    return 0


def run_tick(cfg, dry=False):
    """tick() under a 150 second hard stop, so a hung poll can never wedge the 5-minute tick."""
    def stop(*_a):
        _log("TRIGGERS watchdog: tick evaluation exceeded 150 s, aborting")
        os._exit(0)
    try:
        signal.signal(signal.SIGALRM, stop)
        signal.alarm(150)
    except (ValueError, AttributeError):
        pass
    try:
        return tick(cfg, dry=dry)
    finally:
        try:
            signal.alarm(0)
        except (ValueError, AttributeError):
            pass
