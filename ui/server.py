#!/usr/bin/env python3
"""
Chronos UI server. A local control panel for the Chronos scheduler.

    python3 ui/server.py          # http://127.0.0.1:4747/   (port: config "ui_port", or CHRONOS_UI_PORT)

Python 3.9+, standard library only. Bound to 127.0.0.1 ONLY. Normally run under launchd as io.github.chronos.ui.

Source of truth is jobs.json (the tick, the optional SessionStart hook and this UI all read it). Each job's
instructions live in <jobs_dir>/<id>/prompt.md (editable here). An optional <jobs_dir>/<id>/guard.md holds
locked standing rules: shown read-only here, never written by this server.

Security model
  * 127.0.0.1 bind + Host header must be 127.0.0.1:PORT or localhost:PORT (DNS-rebinding guard)
  * every mutating request needs header X-Chronos-Token == <home_dir>/ui-token (0600); the token is
    injected into the page server-side; an Origin header, when present, must be our own origin
  * job ids ^[a-z0-9-]{2,40}$; file names are matched against strict regexes; no path is ever built
    from user text without resolving it back under its directory
  * file contents are only ever served as text/plain and rendered with textContent
  * (v0.2.0) POST /hook/<id> is the one route without the UI token: it needs the job's own secret in X-Chronos-Secret,
    is still 127.0.0.1-only, takes bodies up to 64 KB, queues at most 20, and throttles wrong secrets. A tunnel's public
    hostname is accepted on /hook/ ONLY, and only when listed in config `hook_hosts` (or CHRONOS_HOOK_HOSTS).
  * (v0.2.0) agent files: opaque ids only, an allowlist rebuilt per request, a sha256 conflict guard, and the page token
    even to READ file contents (see agent_files.py)
"""
import datetime
import fcntl
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "lib"))
import chronoslib as C  # noqa: E402
import chronos_runlog as RL  # noqa: E402  (usage meter: runs.jsonl, rate-limits.json)
import chronos_triggers as TR  # noqa: E402  (event triggers, webhook secrets and queue)
import control_room as CR  # noqa: E402  (agents, skills, hooks, health, plugins and MCP)
import agent_files as AF  # noqa: E402  (view/edit the markdown that steers your agents)

CFG = C.load_config()
PORT = CFG["ui_port"]
JOBS_FILE = CFG["jobs_file"]
JOBS_DIR = CFG["jobs_dir"]
STATE_DIR = CFG["state_dir"]
HISTORY_DIR = CFG["history_dir"]
TOKEN_FILE = CFG["token_file"]
LOGS_DIR = CFG["logs_dir"]
REPORTS_DIR = CFG["reports_dir"]
PAUSED_DIR = CFG["paused_dir"]
PAUSE_ALL = CFG["pause_all"]
BEAT_FILE = CFG["beat_file"]
STATIC_DIR = os.path.join(HERE, "static")
KILL_DIR = CFG["kill_switch_dir"]
CLAUDE_HOME = CFG["claude_home"]
WORKSPACE = CFG["workspace"]
AF_CFG = AF.Cfg.from_config(CFG)
HOOK_HOSTS = {h.strip().lower() for h in (list(CFG["hook_hosts"]) + os.environ.get("CHRONOS_HOOK_HOSTS", "").split(",")) if str(h).strip()}
MAX_TRIGGERS = 8

ID_RE = C.ID_RE
MAX_PROMPT = 200_000
MAX_VIEW_BYTES = 600_000
GRACE_MIN = CFG["grace_min"]
STRIP_DAYS = 14

LOCK = threading.RLock()


class ApiError(Exception):
    def __init__(self, status, msg, **extra):
        Exception.__init__(self, msg)
        self.status, self.msg, self.extra = status, msg, extra


# --------------------------------------------------------------------------- small file helpers
def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return default


def atomic_write(path, text, mode=None):
    """Write text to path via a same-directory temp file + os.replace. Keeps the old file's mode."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o644
    tmp = os.path.join(d, ".%s.tmp.%d.%s" % (os.path.basename(path), os.getpid(), secrets.token_hex(3)))
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def unique_path(d, base, suffix):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, base + suffix)
    n = 1
    while os.path.exists(p):
        n += 1
        p = os.path.join(d, "%s-%d%s" % (base, n, suffix))
    return p


def now():
    return C.now()


def iso(dt):
    return C.iso(dt)


def check_id(jid):
    if not isinstance(jid, str) or not ID_RE.match(jid):
        raise ApiError(400, "bad job id")
    return jid


# --------------------------------------------------------------------------- jobs.json
class JobsLock(object):
    """Cross-process lock (the run script's one-shot disabler takes the same flock)."""

    def __enter__(self):
        LOCK.acquire()
        self.fh = open(JOBS_FILE + ".lock", "a")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()
        finally:
            LOCK.release()


def load_jobs():
    return C.load_jobs(CFG)


def save_jobs(jobs):
    """Snapshot the current file into history/jobs/, then replace atomically. Caller holds JobsLock."""
    for j in jobs:
        try:
            C.validate_job_record(j)
        except ValueError as e:
            raise ApiError(400, str(e))
    cur = read_text(JOBS_FILE, "")
    if cur.strip():
        os.makedirs(os.path.join(HISTORY_DIR, "jobs"), exist_ok=True)
        p = unique_path(os.path.join(HISTORY_DIR, "jobs"), stamp(), ".json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(cur)
    atomic_write(JOBS_FILE, json.dumps(jobs, indent=2, ensure_ascii=False) + "\n")


def find_job(jobs, jid):
    for j in jobs:
        if j.get("id") == jid:
            return j
    raise ApiError(404, "no such job")


# --------------------------------------------------------------------------- state / status
def day_status(job, d, today_dt):
    ds = C.day_str(d)
    today = today_dt.date()
    jid = job["id"]
    if os.path.exists(C.ran_path(CFG, jid, ds)):
        return "ran"
    if not C.clock_on(job) and C.event_ran(CFG, jid, ds):
        return "ran"
    if d == today and (os.path.isdir(C.claim_path(CFG, jid, ds)) or C.event_claims(CFG, jid, ds)):
        return "running"
    if os.path.exists(C.failed_path(CFG, jid, ds)):
        return "failed"
    if not C.is_due_day(job, d):
        return "not-due"
    created = parse_created(job)
    if created and d < created:
        return "not-due"
    if d > today:
        return "not-due"
    f = C.fire_dt(job, d)
    if d == today and today_dt < f + datetime.timedelta(minutes=GRACE_MIN):
        return "pending"
    if d == today and job.get("enabled") is False:
        return "not-due"
    return "missed"


def parse_created(job):
    try:
        return datetime.datetime.fromisoformat(job.get("created")).date()
    except Exception:
        return None


def is_paused(jid):
    return C.is_paused(CFG, jid)


def list_files(d):
    try:
        return sorted(os.listdir(d))
    except OSError:
        return []


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def runs_for(jid, limit=60):
    """Logs and reports belonging to a job, newest first."""
    log_re = re.compile(r"^%s-(\d{4}-\d{2}-\d{2})-(\d{4})(?:-([a-f0-9]{10}))?\.log$" % re.escape(jid))
    rep_re = re.compile(r"^%s-(\d{4}-\d{2}-\d{2})(?:-([a-f0-9]{10}))?\.md$" % re.escape(jid))
    out = []
    for n in list_files(LOGS_DIR):
        m = log_re.match(n)
        if m:
            out.append({"kind": "log", "name": n, "date": m.group(1), "time": m.group(2)[:2] + ":" + m.group(2)[2:], "event": m.group(3),
                        "size": _size(os.path.join(LOGS_DIR, n))})
    for n in list_files(REPORTS_DIR):
        m = rep_re.match(n)
        if m:
            out.append({"kind": "report", "name": n, "date": m.group(1), "time": "", "event": m.group(2),
                        "size": _size(os.path.join(REPORTS_DIR, n))})
    out.sort(key=lambda r: (r["date"], r["time"], r["kind"]), reverse=True)
    return out[:limit]


_USAGE_CACHE = {"key": None, "val": None}


def usage_summary():
    """runs.jsonl summarised for the last 14 days; cached on (file mtime, minute)."""
    try:
        mt = os.path.getmtime(CFG["runs_file"])
    except OSError:
        mt = 0
    try:
        rt = os.path.getmtime(CFG["rate_file"])
    except OSError:
        rt = 0
    key = (mt, rt, int(time.time() // 60))
    if _USAGE_CACHE["key"] != key:
        _USAGE_CACHE["val"] = RL.summarize(CFG, RL.read_rows(CFG, since_ts=time.time() - 16 * 86400))
        _USAGE_CACHE["key"] = key
    return _USAGE_CACHE["val"]


def slim_run(r):
    if not r:
        return None
    keys = ("start", "end", "duration_s", "exit", "ok", "kind", "model", "input_tokens", "output_tokens", "cache_creation_tokens",
            "cache_read_tokens", "cost_usd", "num_turns", "trigger", "denials", "usage_missing", "timed_out", "log")
    return {k: r.get(k) for k in keys}


def usage_for_job(jid):
    u = usage_summary()["jobs"].get(jid)
    if not u:
        return {"last": None, "week": None}
    return {"last": slim_run(u["last"]), "week": u["week"] or None}


def describe_job(job, detail=False):
    n = now()
    jid = job["id"]
    out = {k: job.get(k) for k in ("id", "name", "description", "time", "days", "catchup_min",
                                   "enabled", "in_session", "once", "created", "updated")}
    out["clock"] = C.clock_on(job)
    out["kind"] = "command" if C.is_command(job) else "claude"
    out["command"] = job.get("command") if C.is_command(job) else None
    out["notify"] = C.job_notify_mode(job)
    try:
        out["schedule_text"] = C.english(job)
        out["schedule_ok"] = True
    except Exception:
        out["schedule_text"] = "Schedule unreadable"
        out["schedule_ok"] = False
    try:
        nf = C.next_fires(job, n, 3) if job.get("enabled") else []
    except Exception:
        nf = []
    out["next_fire"] = iso(nf[0]) if nf else None
    out["next_fires"] = [iso(x) for x in nf]
    out["next_in_s"] = int((nf[0] - n).total_seconds()) if nf else None
    out["paused"] = is_paused(jid)
    today = n.date()
    tds = C.day_str(today)
    ev_claims = C.event_claims(CFG, jid, tds)
    out["running"] = (os.path.isdir(C.claim_path(CFG, jid, tds)) and not os.path.exists(C.ran_path(CFG, jid, tds))) or bool(ev_claims)
    try:
        cps = [C.claim_path(CFG, jid, tds)] + [os.path.join(STATE_DIR, c) for c in ev_claims]
        ages = [int(time.time() - os.path.getmtime(c)) for c in cps if os.path.isdir(c)]
        out["claim_age_s"] = min(ages) if (out["running"] and ages) else None
    except OSError:
        out["claim_age_s"] = None
    # v0.2.0: triggers, trust, model, usage
    out["triggers"] = job.get("triggers") or []
    out["trust"] = {"schedule": C.clock_on(job), "event": bool(out["triggers"])}
    out["model"] = job.get("model") or None
    out["allowed_tools"] = job.get("allowed_tools") or None
    out["default_allowed_tools"] = C.default_allowed_tools(CFG)
    out["rate_per_hour"] = CFG["event_rate_per_hour"]
    try:
        stt = TR.load_state(CFG, jid)
        out["fires_last_hour"] = len(TR.fires_last_hour(stt, time.time()))
        out["trigger_status"] = []
        for t in out["triggers"]:
            try:
                ts = stt["triggers"].get(TR.trigger_key(TR.validate_trigger(t)), {})
            except ValueError:
                ts = {}
            out["trigger_status"].append({"last_poll": ts.get("last_poll"), "last_ok": ts.get("last_ok"), "last_error": ts.get("last_error"),
                                          "baselined": ("files" in ts or "seen" in ts or t.get("type") == "webhook"), "count": ts.get("last_count")})
    except Exception:
        out["fires_last_hour"] = 0
        out["trigger_status"] = []
    out["usage"] = usage_for_job(jid)
    out["hook_secret_set"] = bool(TR.get_secret(CFG, jid))
    out["hook_queue"] = TR.queue_len(CFG, jid)
    strip = []
    for i in range(STRIP_DAYS - 1, -1, -1):
        d = today - datetime.timedelta(days=i)
        try:
            st = day_status(job, d, n)
        except Exception:
            st = "not-due"
        strip.append({"date": C.day_str(d), "status": st})
    out["strip"] = strip
    out["today"] = strip[-1]["status"]
    runs = runs_for(jid, 12 if not detail else 60)
    out["latest_log"] = next((r for r in runs if r["kind"] == "log"), None)
    out["latest_report"] = next((r for r in runs if r["kind"] == "report"), None)
    out["has_prompt"] = os.path.isfile(C.prompt_file(CFG, jid)) or C.is_command(job)
    out["has_guard"] = os.path.isfile(C.guard_file(CFG, jid))
    if detail:
        out["runs"] = runs
    return out


def global_status():
    beat_age = None
    try:
        beat_age = int(time.time() - os.path.getmtime(BEAT_FILE))
    except OSError:
        pass
    return {"now": iso(now()), "paused_all": os.path.exists(PAUSE_ALL), "beat_age_s": beat_age,
            "tz": C.tz_name(), "port": PORT, "version": C.VERSION, "notify_configured": bool(CFG["notify"])}


# --------------------------------------------------------------------------- history
def snapshot_text(jid, text, label):
    """Store a copy of text as history/<id>/<ts>-<label>."""
    d = os.path.join(HISTORY_DIR, jid)
    p = unique_path(d, stamp() + "-" + label.rsplit(".", 1)[0], "." + label.rsplit(".", 1)[1])
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(text)
    return p


HIST_RE = re.compile(r"^(\d{8}-\d{6})(-\d+)?-prompt\.md$")
JSNAP_RE = re.compile(r"^(\d{8}-\d{6})(-\d+)?\.json$")
SCHEDULE_FIELDS = ("name", "description", "time", "days", "once", "catchup_min", "enabled", "in_session", "notify")


def ts_pretty(s):
    try:
        return iso(datetime.datetime.strptime(s, "%Y%m%d-%H%M%S"))
    except ValueError:
        return s


def history_for(jid, current=None):
    prompts = []
    d = os.path.join(HISTORY_DIR, jid)
    for n in sorted(list_files(d), reverse=True):
        m = HIST_RE.match(n)
        if m:
            prompts.append({"file": n, "ts": ts_pretty(m.group(1)), "size": _size(os.path.join(d, n))})
    jobs_snaps = []
    sig = lambda e: tuple(e.get(k) for k in SCHEDULE_FIELDS)
    last_sig = sig(current) if current else None  # skip snapshots that would restore nothing new
    jd = os.path.join(HISTORY_DIR, "jobs")
    for n in sorted(list_files(jd), reverse=True)[:80]:
        m = JSNAP_RE.match(n)
        if not m:
            continue
        try:
            data = json.loads(read_text(os.path.join(jd, n), "[]"))
            ent = next((j for j in data if j.get("id") == jid), None)
        except Exception:
            ent = None
        if ent is None or sig(ent) == last_sig:
            continue
        last_sig = sig(ent)
        try:
            summ = C.english(ent) + (" (disabled)" if not ent.get("enabled") else "")
        except Exception:
            summ = "unreadable"
        jobs_snaps.append({"file": n, "ts": ts_pretty(m.group(1)), "summary": summ,
                           "catchup_min": ent.get("catchup_min")})
    return {"prompt": prompts[:60], "jobs": jobs_snaps[:30]}


# --------------------------------------------------------------------------- operations
def guard_text(job):
    """What the locked Guardrails tab shows: the standard run preamble plus guard.md when present."""
    ds = C.date_key(job, now())
    txt = C.preamble(CFG, job, ds, when="<date and time of the run>")
    g = read_text(C.guard_file(CFG, job["id"]), "").strip()
    if g:
        txt += "\n\nSTANDING RULES FOR THIS JOB (locked; not editable from the web UI):\n" + g
    return txt


def read_job_detail(jid):
    check_id(jid)
    job = find_job(load_jobs(), jid)
    d = describe_job(job, detail=True)
    p = read_text(C.prompt_file(CFG, jid), None)
    d["prompt_md"] = p if p is not None else ""
    d["prompt_sha"] = sha(d["prompt_md"])
    d["guard_md"] = guard_text(job)
    d["history"] = history_for(jid, job)
    return d


def write_prompt(jid, content, base_sha):
    check_id(jid)
    if not isinstance(content, str):
        raise ApiError(400, "content must be text")
    if len(content.encode("utf-8")) > MAX_PROMPT:
        raise ApiError(413, "prompt.md is too large (200 KB max)")
    if "\x00" in content:
        raise ApiError(400, "binary content refused")
    with LOCK:
        if C.is_command(find_job(load_jobs(), jid)):
            raise ApiError(409, "this is a command job: it runs a shell command and has no prompt")
        path = C.prompt_file(CFG, jid)
        if not os.path.isdir(os.path.join(JOBS_DIR, jid)):
            raise ApiError(404, "job folder missing")
        cur = read_text(path, None)
        cur_text = cur if cur is not None else ""
        if base_sha is not None and sha(cur_text) != base_sha:
            raise ApiError(409, "prompt.md changed on disk since you opened it. Reload before saving.",
                           current_sha=sha(cur_text))
        if cur is not None and cur == content:
            return {"saved": False, "unchanged": True, "sha": sha(content)}
        snap = snapshot_text(jid, cur, "prompt.md") if cur is not None else None
        atomic_write(path, content)
        return {"saved": True, "sha": sha(content), "snapshot": os.path.basename(snap) if snap else None}


def restore_prompt(jid, fname):
    check_id(jid)
    if not HIST_RE.match(str(fname)):
        raise ApiError(400, "bad history file name")
    p = os.path.join(HISTORY_DIR, jid, fname)
    if os.path.realpath(os.path.dirname(p)) != os.path.realpath(os.path.join(HISTORY_DIR, jid)) or not os.path.isfile(p):
        raise ApiError(404, "no such version")
    return write_prompt(jid, read_text(p), None)


def update_schedule(jid, body):
    check_id(jid)
    with JobsLock():
        jobs = load_jobs()
        job = find_job(jobs, jid)
        if C.clock_on(job):
            merged = {k: job.get(k) for k in ("time", "days", "once", "catchup_min")}
            for k in ("time", "days", "once", "catchup_min"):
                if k in body:
                    merged[k] = body[k]
            clean, errors = C.validate_schedule(merged)
            if errors:
                raise ApiError(422, "; ".join(errors), errors=errors)
        else:                       # an event-only job has no clock schedule to edit; its other settings still save
            clean = {}
        new = dict(job)
        new.update(clean)
        for k in ("name", "description"):
            if k in body:
                v = str(body[k]).strip().replace("\n", " ")
                if k == "name" and not (1 <= len(v) <= 60):
                    raise ApiError(422, "name must be 1-60 characters")
                if k == "description" and len(v) > 300:
                    raise ApiError(422, "description is limited to 300 characters")
                new[k] = v
        for k in ("enabled", "in_session"):
            if k in body:
                if not isinstance(body[k], bool):
                    raise ApiError(422, k + " must be true or false")
                new[k] = body[k]
        if "notify" in body:
            if body["notify"] not in C.NOTIFY_MODES:
                raise ApiError(422, "notify must be one of: " + ", ".join(C.NOTIFY_MODES))
            new["notify"] = body["notify"]
        if new.get("once"):
            new["in_session"] = False
        changed = any(new.get(k) != job.get(k) for k in set(new) | set(job) if k != "updated")
        if not changed:
            return {"changed": False, "job": describe_job(job)}
        new["updated"] = iso(now())
        job.clear()
        job.update(new)
        save_jobs(jobs)
        return {"changed": True, "warning": "", "job": describe_job(job)}


def restore_schedule(jid, fname):
    check_id(jid)
    if not JSNAP_RE.match(str(fname)):
        raise ApiError(400, "bad snapshot name")
    p = os.path.join(HISTORY_DIR, "jobs", fname)
    if not os.path.isfile(p):
        raise ApiError(404, "no such snapshot")
    try:
        ent = next(j for j in json.loads(read_text(p)) if j.get("id") == jid)
    except Exception:
        raise ApiError(404, "that snapshot has no entry for this job")
    body = {k: ent.get(k) for k in SCHEDULE_FIELDS if k in ent}
    return update_schedule(jid, body)


def update_triggers(jid, body):
    """Replace a job's triggers (and its clock switch and allowed-tools list). Everything is validated."""
    check_id(jid)
    raw = body.get("triggers", [])
    if not isinstance(raw, list) or len(raw) > MAX_TRIGGERS:
        raise ApiError(422, "triggers must be a list of at most %d" % MAX_TRIGGERS)
    clean = []
    for t in raw:
        try:
            clean.append(TR.validate_trigger(t))
        except ValueError as e:
            raise ApiError(422, str(e))
    if sum(1 for t in clean if t["type"] == "webhook") > 1:
        raise ApiError(422, "one webhook trigger per job is enough")
    clock = body.get("clock", True)
    if not isinstance(clock, bool):
        raise ApiError(422, "clock must be true or false")
    if not clock and not clean:
        raise ApiError(422, "with the clock off the job needs at least one trigger, or it can never run")
    allowed = body.get("allowed_tools", None)
    try:
        allowed = C.validate_allowed_tools(allowed) if allowed else None
    except ValueError as e:
        raise ApiError(422, str(e))
    with JobsLock():
        jobs = load_jobs()
        job = find_job(jobs, jid)
        if C.is_command(job):
            raise ApiError(409, "a command job runs on its clock schedule only and cannot have triggers")
        if job.get("once") and clean:
            raise ApiError(422, "a one-shot reminder cannot have triggers")
        new = dict(job)
        if clean:
            new["triggers"] = clean
        else:
            new.pop("triggers", None)
        if clock:
            new.pop("clock", None)
        else:
            new["clock"] = False
            new["in_session"] = False
        if allowed:
            new["allowed_tools"] = allowed
        else:
            new.pop("allowed_tools", None)
        changed = any(new.get(k) != job.get(k) for k in set(new) | set(job) if k != "updated")
        if changed:
            new["updated"] = iso(now())
            job.clear()
            job.update(new)
            save_jobs(jobs)
    if any(t["type"] == "webhook" for t in clean):
        TR.ensure_secret(CFG, jid)
    else:
        TR.delete_secret(CFG, jid)
        TR.clear_queue(CFG, jid)
    return {"changed": changed, "job": describe_job(job)}


def test_trigger(jid, body):
    check_id(jid)
    find_job(load_jobs(), jid)
    try:
        return TR.test_trigger(CFG, jid, body.get("trigger"))
    except ValueError as e:
        raise ApiError(422, str(e))


def hook_secret(jid, rotate):
    check_id(jid)
    job = find_job(load_jobs(), jid)
    if not any(isinstance(t, dict) and t.get("type") == "webhook" for t in (job.get("triggers") or [])):
        raise ApiError(409, "this job has no webhook trigger; add one and save first")
    return {"secret": TR.ensure_secret(CFG, jid, rotate=bool(rotate)), "path": "/hook/" + jid, "url": "http://127.0.0.1:%d/hook/%s" % (PORT, jid)}


def check_model(m):
    """-> a clean model string or None. Raises ApiError for a bad or denylisted one."""
    m = str(m).strip() if m not in (None, "") else None
    if not m:
        return None
    if not C.MODEL_RE.match(m):
        raise ApiError(422, "model must be opus, sonnet, haiku or a full model id like claude-sonnet-5-5")
    for bad in CFG["model_denylist"]:
        if bad and bad.lower() in m.lower():
            raise ApiError(422, "the model %r is on this install's model_denylist" % m)
    return m


def update_model(jid, body):
    check_id(jid)
    m = check_model(body.get("model"))
    with JobsLock():
        jobs = load_jobs()
        job = find_job(jobs, jid)
        if C.is_command(job):
            raise ApiError(409, "a command job does not use Claude, so it has no model")
        if (job.get("model") or None) == m:
            return {"changed": False, "job": describe_job(job)}
        if m:
            job["model"] = m
        else:
            job.pop("model", None)
        job["updated"] = iso(now())
        save_jobs(jobs)
    return {"changed": True, "job": describe_job(job)}


def set_paused(jid, paused):
    check_id(jid)
    find_job(load_jobs(), jid)
    os.makedirs(PAUSED_DIR, exist_ok=True)
    p = os.path.join(PAUSED_DIR, jid)
    if paused:
        open(p, "a").close()
    else:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return {"paused": is_paused(jid)}


def set_pause_all(paused):
    os.makedirs(CFG["home_dir"], exist_ok=True)
    if paused:
        open(PAUSE_ALL, "a").close()
    else:
        try:
            os.remove(PAUSE_ALL)
        except FileNotFoundError:
            pass
    return {"paused_all": os.path.exists(PAUSE_ALL)}


def run_now(jid, again):
    check_id(jid)
    with LOCK:
        try:
            job = C.start_run(CFG, jid, again)
        except C.RunError as e:
            raise ApiError(e.status, e.msg, **e.extra)
    return {"started": True, "job": describe_job(job)}


# ---- create / delete
def create_job(body):
    name = str(body.get("name", "")).strip()
    jid = str(body.get("id", "")).strip()
    prompt = str(body.get("prompt", "")).strip()
    desc = str(body.get("description", "")).strip() or name
    mode = body.get("mode", "recurring")
    notify = body.get("notify", "failure")
    if not (1 <= len(name) <= 60):
        raise ApiError(422, "name must be 1-60 characters")
    if not ID_RE.match(jid):
        raise ApiError(422, "id must be 2-40 characters of a-z, 0-9 and dashes")
    if not prompt:
        raise ApiError(422, "the prompt is empty")
    if len(prompt) > 20000 or len(desc) > 300:
        raise ApiError(422, "something is too long (prompt 20k, description 300)")
    if mode not in ("recurring", "once"):
        raise ApiError(422, "bad mode")
    if notify not in C.NOTIFY_MODES:
        raise ApiError(422, "notify must be one of: " + ", ".join(C.NOTIFY_MODES))
    fields = {"time": body.get("time"), "days": body.get("days"),
              "catchup_min": body.get("catchup_min", 180 if mode == "recurring" else 120)}
    if mode == "once":
        fields["once"] = body.get("once")
    clean, errors = C.validate_schedule(fields)
    if errors:
        raise ApiError(422, "; ".join(errors), errors=errors)
    if mode == "once" and C.parse_once(clean["once"]) <= now() - datetime.timedelta(minutes=1):
        raise ApiError(422, "that one-shot time is already in the past")
    with JobsLock():
        try:
            jobs = load_jobs()
        except FileNotFoundError:
            jobs = []
        if any(j.get("id") == jid for j in jobs) or os.path.exists(os.path.join(JOBS_DIR, jid)):
            raise ApiError(409, "a job with that id already exists")
        stamp_now = iso(now())
        job = {"id": jid, "name": name, "description": desc}
        mdl = check_model(body.get("model"))
        if mdl:
            job["model"] = mdl
        job.update({"time": clean["time"], "days": clean["days"], "catchup_min": clean["catchup_min"],
                    "enabled": True, "in_session": bool(body.get("in_session")) and mode == "recurring",
                    "notify": notify, "once": clean.get("once"), "created": stamp_now, "updated": stamp_now})
        tdir = os.path.join(JOBS_DIR, jid)
        os.makedirs(JOBS_DIR, exist_ok=True)
        os.mkdir(tdir)
        try:
            atomic_write(os.path.join(tdir, "prompt.md"), prompt + "\n", 0o644)
            jobs.append(job)
            save_jobs(jobs)
        except Exception:
            shutil.rmtree(tdir, ignore_errors=True)
            raise
    return {"created": True, "job": describe_job(job)}


def delete_job(jid):
    """Delete a job. Its folder is archived under history/deleted/ first."""
    check_id(jid)
    with JobsLock():
        jobs = load_jobs()
        find_job(jobs, jid)
        tdir = os.path.join(JOBS_DIR, jid)
        arch = os.path.join(HISTORY_DIR, "deleted", "%s-%s" % (jid, stamp()))
        if os.path.isdir(tdir):
            os.makedirs(arch, exist_ok=True)
            for n in ("prompt.md", "guard.md"):
                if os.path.isfile(os.path.join(tdir, n)):
                    shutil.copy2(os.path.join(tdir, n), os.path.join(arch, n))
            shutil.rmtree(tdir)
        jobs[:] = [j for j in jobs if j.get("id") != jid]
        save_jobs(jobs)
        try:
            os.remove(os.path.join(PAUSED_DIR, jid))
        except OSError:
            pass
    TR.delete_secret(CFG, jid)
    TR.clear_queue(CFG, jid)
    return {"deleted": True, "archive": arch}


# ---- viewing files
VIEW_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(log|md|txt)$")


def view_file(kind, name):
    base = {"log": LOGS_DIR, "report": REPORTS_DIR}.get(kind)
    if not base or not VIEW_NAME_RE.match(str(name or "")):
        raise ApiError(400, "bad file reference")
    p = os.path.join(base, name)
    rp = os.path.realpath(p)
    if os.path.dirname(rp) != os.path.realpath(base) or not os.path.isfile(rp):
        raise ApiError(404, "no such file")
    size = os.path.getsize(rp)
    with open(rp, "rb") as fh:
        if size > MAX_VIEW_BYTES:
            fh.seek(size - MAX_VIEW_BYTES)
        data = fh.read()
    txt = data.decode("utf-8", errors="replace")
    if size > MAX_VIEW_BYTES:
        txt = "[... earlier %d bytes not shown ...]\n" % (size - MAX_VIEW_BYTES) + txt
    return txt


def all_runs(job_filter=None, limit=80):
    rows = []
    for n in list_files(LOGS_DIR):
        m = re.match(r"^(.+?)-(\d{4}-\d{2}-\d{2})-(\d{4})(?:-([a-f0-9]{10}))?\.log$", n)
        if m:
            rows.append({"kind": "log", "name": n, "job": m.group(1), "date": m.group(2), "event": m.group(4),
                         "time": m.group(3)[:2] + ":" + m.group(3)[2:], "size": _size(os.path.join(LOGS_DIR, n))})
    for n in list_files(REPORTS_DIR):
        m = re.match(r"^(.+?)-(\d{4}-\d{2}-\d{2})(?:-([a-f0-9]{10}))?\.md$", n)
        if m:
            rows.append({"kind": "report", "name": n, "job": m.group(1), "date": m.group(2), "time": "", "event": m.group(3),
                         "size": _size(os.path.join(REPORTS_DIR, n))})
    if job_filter:
        rows = [r for r in rows if r["job"] == job_filter]
    rows.sort(key=lambda r: (r["date"], r["time"], r["kind"]), reverse=True)
    extra = []
    if os.path.isfile(os.path.join(LOGS_DIR, "chronos.log")):
        extra.append({"kind": "log", "name": "chronos.log", "job": "(scheduler)", "date": "", "time": "",
                      "size": _size(os.path.join(LOGS_DIR, "chronos.log"))})
    return extra + rows[:limit]


def preview(q):
    fields = {k: (q.get(k) or [""])[0] for k in ("time", "days", "once", "catchup_min")}
    if not fields["catchup_min"]:
        fields["catchup_min"] = "180"
    clean, errors = C.validate_schedule(fields)
    res = {"ok": not errors, "errors": errors, "text": "", "next": []}
    if not errors:
        job = {"time": clean["time"], "days": clean["days"], "once": clean.get("once")}
        res["text"] = C.english(job)
        res["next"] = [iso(x) for x in C.next_fires(job, now(), 3)]
        if clean.get("once") and not res["next"]:
            res["errors"] = ["that one-shot time has already passed"]
            res["ok"] = False
    return res


# --------------------------------------------------------------------------- HTTP
def load_token():
    try:
        t = read_text(TOKEN_FILE).strip()
        if len(t) >= 32:
            return t
    except Exception:
        pass
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    t = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(t + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return t


TOKEN = None
JOB_PATH = re.compile(r"^/api/jobs/([a-z0-9-]{2,40})(?:/([a-z-]+))?$")
HOOK_PATH = re.compile(r"^/hook/([a-z0-9-]{2,40})$")
HOOK_KILL_PATH = re.compile(r"^/api/hooks/([a-z0-9][a-z0-9-]{1,40})/kill$")
AF_PATH = re.compile(r"^/api/agent-files(?:/([a-z0-9][a-z0-9-]{0,100})(?:/(history|restore)(?:/(\d{8}-\d{6}(?:-\d+)?\.md))?)?)?$")
HOOK_FAILS = {}

CSP = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


class Handler(BaseHTTPRequestHandler):
    server_version = "ChronosUI/1"
    sys_version = ""

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (time.strftime("%F %T"), fmt % args))

    # ---- plumbing
    def _send(self, status, body, ctype, extra=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj), "application/json; charset=utf-8")

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        return host in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)

    def _origin_ok(self):
        o = self.headers.get("Origin")
        return o is None or o in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT)

    def _guard(self, mutating):
        if mutating == "hook":  # webhook: no UI token or Origin; its own per-job secret is checked in _hook
            host = (self.headers.get("Host") or "").strip().lower()
            if host in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT) or host in HOOK_HOSTS:
                return True
            self._json(403, {"error": "bad host header"})
            return False
        if not self._host_ok():
            self._json(403, {"error": "bad host header"})
            return False
        if mutating:
            if not self._origin_ok():
                self._json(403, {"error": "bad origin"})
                return False
            tok = self.headers.get("X-Chronos-Token") or ""
            if not hmac.compare_digest(tok.encode(), TOKEN.encode()):
                self._json(401, {"error": "missing or wrong token"})
                return False
        return True

    def _need_token(self):
        tok = self.headers.get("X-Chronos-Token") or ""
        if not self._origin_ok() or not hmac.compare_digest(tok.encode(), TOKEN.encode()):
            raise ApiError(401, "missing or wrong token")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad content length")
        if n > 400_000:
            raise ApiError(413, "request too large")
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise ApiError(400, "body is not valid JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "body must be a JSON object")
        return data

    def _dispatch(self, fn, mutating):
        if not self._guard(mutating):
            return
        try:
            fn()
        except (ApiError, AF.AFError) as e:
            payload = {"error": e.msg}
            payload.update(e.extra)
            self._json(e.status, payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # never leak a stack trace to the browser
            sys.stderr.write("ERROR %s %s: %r\n" % (self.command, self.path, e))
            self._json(500, {"error": "internal error (see ui.err)"})

    # ---- verbs
    def do_GET(self):
        self._dispatch(self._get, False)

    def do_HEAD(self):
        self._dispatch(self._get, False)

    def do_POST(self):
        if urlparse(self.path).path.startswith("/hook/"):
            return self._dispatch(self._hook, "hook")
        self._dispatch(self._post, True)

    def do_PUT(self):
        self._dispatch(self._post, True)

    def do_DELETE(self):
        self._dispatch(self._delete, True)

    # ---- webhook (POST /hook/<id>)
    def _hook(self):
        m = HOOK_PATH.match(urlparse(self.path).path)
        if not m:
            raise ApiError(404, "not found")
        jid = m.group(1)
        now_t = time.time()
        fails = [t for t in HOOK_FAILS.get(jid, []) if now_t - t < 60]
        if len(fails) >= 10:
            HOOK_FAILS[jid] = fails
            raise ApiError(429, "too many bad secrets; wait a minute")
        secret = TR.get_secret(CFG, jid)
        got = (self.headers.get("X-Chronos-Secret") or "").encode()
        if not secret or not hmac.compare_digest(got, secret.encode()):
            fails.append(now_t)
            HOOK_FAILS[jid] = fails
            raise ApiError(401, "unauthorized")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "bad content length")
        if n > TR.HOOK_BODY_MAX:
            raise ApiError(413, "body too large (64 KB max)")
        raw = self.rfile.read(n) if n else b""
        job = next((j for j in load_jobs() if j.get("id") == jid), None)
        active = (job and job.get("enabled") is True and not C.is_command(job) and not is_paused(jid) and not os.path.exists(PAUSE_ALL)
                  and any(isinstance(t, dict) and t.get("type") == "webhook" for t in (job.get("triggers") or [])))
        if not active:
            raise ApiError(409, "this hook is not active (job disabled, paused, or no webhook trigger)")
        try:
            h = TR.enqueue_webhook(CFG, jid, raw.decode("utf-8", "replace"), self.headers.get("Content-Type") or "", self.client_address[0])
        except TR.TriggerError:
            raise ApiError(429, "queue full; Chronos fires at most %d runs per job per hour" % CFG["event_rate_per_hour"])
        self._json(202, {"queued": True, "event": h, "note": "Chronos picks this up at its next 5 minute tick"})

    # ---- GET
    def _get(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path in ("/", "/index.html"):
            html = read_text(os.path.join(STATIC_DIR, "index.html")).replace("__TOKEN__", TOKEN)
            return self._send(200, html, "text/html; charset=utf-8")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/favicon.ico":
            return self._static("favicon.png")
        if path == "/api/jobs":
            jobs = load_jobs()
            return self._json(200, {"status": global_status(), "jobs": [describe_job(j) for j in jobs]})
        m = JOB_PATH.match(path)
        if m and not m.group(2):
            return self._json(200, read_job_detail(m.group(1)))
        if path == "/api/preview":
            return self._json(200, preview(q))
        if path == "/api/runs":
            jf = (q.get("job") or [""])[0]
            if jf and not (ID_RE.match(jf) or jf == "(scheduler)"):
                raise ApiError(400, "bad job filter")
            return self._json(200, {"runs": all_runs(jf or None)})
        if path == "/api/file":
            kind = (q.get("kind") or [""])[0]
            name = (q.get("name") or [""])[0]
            txt = view_file(kind, name)
            return self._send(200, txt, "text/plain; charset=utf-8")
        if path == "/api/history":
            jid = (q.get("job") or [""])[0]
            fname = (q.get("file") or [""])[0]
            check_id(jid)
            if not HIST_RE.match(fname):
                raise ApiError(400, "bad history file name")
            hp = os.path.join(HISTORY_DIR, jid, fname)
            if os.path.dirname(os.path.realpath(hp)) != os.path.realpath(os.path.join(HISTORY_DIR, jid)) or not os.path.isfile(hp):
                raise ApiError(404, "no such version")
            return self._send(200, read_text(hp), "text/plain; charset=utf-8")
        if path == "/api/status":
            return self._json(200, global_status())
        if path == "/api/usage":
            u = usage_summary()
            rate = dict(u["rate"]) if u["rate"] else None
            if rate and rate.get("captured_ts"):
                rate["age_s"] = int(time.time() - float(rate["captured_ts"]))
            names = {}
            try:
                names = {j["id"]: j.get("name") or j["id"] for j in load_jobs()}
            except Exception:
                pass
            jf = (q.get("job") or [""])[0]
            recent = []
            if jf:
                check_id(jf)
                recent = [slim_run(r) for r in sorted(RL.read_rows(CFG, since_ts=time.time() - 16 * 86400), key=lambda r: r.get("start_ts") or 0, reverse=True)
                          if r.get("job") == jf][:15]
            return self._json(200, {"days": u["days"], "order": u["order"], "jobs": {k: {"last": slim_run(v["last"]), "week": v["week"]} for k, v in u["jobs"].items()},
                                    "names": names, "rate": rate, "recent": recent, "now": iso(now()),
                                    "note": "Cost is the list-price equivalent reported by claude, not what a subscription plan bills."})
        if path == "/api/control":
            return self._json(200, CR.collect(CFG))
        ma = AF_PATH.match(path)
        if ma:
            self._need_token()  # file contents need the page token even to read
            fid, sub, hist = ma.groups()
            if not fid:
                return self._json(200, AF.list_all(AF_CFG))
            if not sub:
                return self._json(200, AF.read_file(AF_CFG, fid))
            if sub == "history" and hist:
                return self._send(200, AF.read_history(AF_CFG, fid, hist), "text/plain; charset=utf-8")
            if sub == "history":
                return self._json(200, {"history": AF.history_list(AF_CFG, AF.find(AF_CFG, fid)["id"])})
            raise ApiError(404, "not found")
        raise ApiError(404, "not found")

    def _static(self, rel):
        if not re.match(r"^[A-Za-z0-9._-]+$", rel or ""):
            raise ApiError(404, "not found")
        p = os.path.realpath(os.path.join(STATIC_DIR, rel))
        if os.path.dirname(p) != os.path.realpath(STATIC_DIR) or not os.path.isfile(p) or rel == "index.html":
            raise ApiError(404, "not found")
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        with open(p, "rb") as fh:
            data = fh.read()
        self._send(200, data, ctype, {"Cache-Control": "no-cache"})

    # ---- POST / PUT
    def _post(self):
        path = urlparse(self.path).path
        body = self._body()
        if path == "/api/jobs":
            return self._json(201, create_job(body))
        if path == "/api/pause-all":
            return self._json(200, set_pause_all(bool(body.get("paused"))))
        ma = AF_PATH.match(path)
        if ma and ma.group(1):
            fid, sub, hist = ma.groups()
            if not sub and not hist:
                return self._json(200, AF.save_file(AF_CFG, fid, body.get("content"), body.get("base_sha")))
            if sub == "restore" and not hist:
                return self._json(200, AF.restore_file(AF_CFG, fid, body.get("file")))
            raise ApiError(404, "not found")
        mk = HOOK_KILL_PATH.match(path)
        if mk:
            r = CR.set_kill(mk.group(1), bool(body.get("off")), WORKSPACE, KILL_DIR, CLAUDE_HOME)
            if r is None:
                raise ApiError(404, "no registered hook reads that kill switch, so there is nothing to toggle")
            return self._json(200, r)
        m = JOB_PATH.match(path)
        if not m or not m.group(2):
            raise ApiError(404, "not found")
        jid, action = m.group(1), m.group(2)
        if action == "prompt":
            return self._json(200, write_prompt(jid, body.get("content"), body.get("base_sha")))
        if action == "schedule":
            return self._json(200, update_schedule(jid, body))
        if action == "triggers":
            return self._json(200, update_triggers(jid, body))
        if action == "trigger-test":
            return self._json(200, test_trigger(jid, body))
        if action == "hook-secret":
            return self._json(200, hook_secret(jid, bool(body.get("rotate"))))
        if action == "model":
            return self._json(200, update_model(jid, body))
        if action == "run":
            return self._json(202, run_now(jid, bool(body.get("again"))))
        if action == "pause":
            return self._json(200, set_paused(jid, bool(body.get("paused"))))
        if action == "restore":
            if body.get("kind") == "prompt":
                return self._json(200, restore_prompt(jid, body.get("file")))
            if body.get("kind") == "schedule":
                return self._json(200, restore_schedule(jid, body.get("file")))
            raise ApiError(400, "bad restore kind")
        raise ApiError(404, "not found")

    def _delete(self):
        m = JOB_PATH.match(urlparse(self.path).path)
        if not m or m.group(2):
            raise ApiError(404, "not found")
        self._json(200, delete_job(m.group(1)))


def main():
    global TOKEN
    TOKEN = load_token()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer.allow_reuse_address = True
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    sys.stderr.write("%s Chronos UI listening on http://127.0.0.1:%d/ (jobs: %s)\n" % (time.strftime("%F %T"), PORT, JOBS_FILE))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
