"""
chronos_write: the ONE writer for jobs.json, job folders and the approved record. Python 3.9+, standard library only.

Everything that changes a job (`chronos api apply`, the web UI's create, schedule, model, triggers, prompt and delete
paths, `disable`, `adopt`) goes through here, under one cross-process flock (`<jobs_file>.lock`, the same one
`disable_once` takes). A save through this module is an APPROVAL: it writes `<state_dir>/approved/<id>.json`
({content_hash, prompt, guard, approved_at, via}) in the same locked step, so a change and its approval cannot come
apart. Nothing else writes that record. With `approval_required` on, a job whose content differs from its record is held.
"""
import datetime
import fcntl
import json
import os
import re
import secrets
import shutil
import threading

import chronoslib as C

MAX_API_PROMPT = 16 * 1024            # bytes; what the API accepts. A longer prompt on disk is read-only for it.
MAX_UI_PROMPT = 200_000               # the Chronos web UI has always allowed this much
_THREAD = threading.RLock()
_local = threading.local()
C0 = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
WEEKDAYS = C.DAY_NAMES


class WriteError(Exception):
    """code: schedule_invalid | schedule_conflict | not_found | both_jobs_remain | bad_request. fields: [{path, message}]."""

    def __init__(self, code, message, fields=None):
        Exception.__init__(self, message)
        self.code, self.message, self.fields = code, message, list(fields or [])

    def as_json(self):
        return {"error": {"code": self.code, "message": self.message, "fields": self.fields}}


def invalid(path, message):
    return WriteError("schedule_invalid", message, [{"path": path, "message": message}])


# --------------------------------------------------------------------------- lock and atomic files
class jobs_lock(object):
    """Cross-process (flock) and cross-thread lock around every jobs.json / prompt / approved-record write. Re-entrant in a thread."""

    def __init__(self, cfg):
        self.path = cfg["jobs_file"] + ".lock"

    def __enter__(self):
        depth = getattr(_local, "depth", 0)
        if depth == 0:
            _THREAD.acquire()
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            self.fh = open(self.path, "a")
            fcntl.flock(self.fh, fcntl.LOCK_EX)
            _local.fh = self.fh
        _local.depth = depth + 1
        return self

    def __exit__(self, *a):
        _local.depth -= 1
        if _local.depth == 0:
            try:
                fcntl.flock(_local.fh, fcntl.LOCK_UN)
                _local.fh.close()
            finally:
                _THREAD.release()


def atomic_write(path, data, mode=None):
    """Write bytes (or str) to path through a same-directory temp file and os.replace. Keeps the old file's mode."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    if isinstance(data, str):
        data = data.encode("utf-8")
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError:
            mode = 0o644
    tmp = os.path.join(d, ".%s.tmp.%d.%s" % (os.path.basename(path), os.getpid(), secrets.token_hex(3)))
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _stamp():
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")


def _unique(d, base, suffix):
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, base + suffix)
    n = 1
    while os.path.exists(p):
        n += 1
        p = os.path.join(d, "%s-%d%s" % (base, n, suffix))
    return p


def snapshot_prompt(cfg, jid):
    """Keep the prompt that is about to be replaced under history/<id>/ (the layout the web UI's History tab reads)."""
    old = C._read_bytes(C.prompt_file(cfg, jid))
    if os.path.isfile(C.prompt_file(cfg, jid)):
        p = _unique(os.path.join(cfg["history_dir"], jid), _stamp() + "-prompt", ".md")
        with open(p, "wb") as fh:
            fh.write(old)
        return p
    return None


def read_jobs(cfg):
    try:
        return C.load_jobs(cfg)
    except FileNotFoundError:
        return []


def write_jobs(cfg, jobs):
    """Validate every record, snapshot the current file into history/jobs/, then replace jobs.json atomically. Caller holds the lock."""
    for j in jobs:
        try:
            C.validate_job_record(j)
        except ValueError as e:
            raise WriteError("schedule_invalid", str(e), [{"path": str(j.get("id") if isinstance(j, dict) else ""), "message": str(e)}])
    try:
        cur = open(cfg["jobs_file"], encoding="utf-8", errors="replace").read()
    except OSError:
        cur = ""
    if cur.strip():
        p = _unique(os.path.join(cfg["history_dir"], "jobs"), _stamp(), ".json")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(cur)
    atomic_write(cfg["jobs_file"], json.dumps(jobs, indent=2, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- the approved record
def approve(cfg, job, via):
    """Write <state_dir>/approved/<id>.json for the job as it is on disk right now. Caller holds the lock."""
    jid = job["id"]
    pb, gb = C._read_bytes(C.prompt_file(cfg, jid)), C._read_bytes(C.guard_file(cfg, jid))
    rec = {"content_hash": C.record_content_hash(job, pb, gb), "prompt": pb.decode("utf-8", "replace"),
           "guard": gb.decode("utf-8", "replace") if os.path.isfile(C.guard_file(cfg, jid)) else None,
           "approved_at": C.aware_iso(C.now()), "via": via}
    os.makedirs(cfg["approved_dir"], mode=0o700, exist_ok=True)
    atomic_write(C.approved_path(cfg, jid), json.dumps(rec, indent=2, ensure_ascii=False) + "\n", 0o600)
    return rec


def approve_on_disk(cfg, jid, via, content_hash=None):
    """'Approve as it is on disk' (UI, adopt, migrate): approve a job as it now stands. With `content_hash` the approval is bound
    to what the owner looked at: if the files moved since, it is refused (schedule_conflict) instead of approving unseen text."""
    with jobs_lock(cfg):
        job = next((j for j in read_jobs(cfg) if isinstance(j, dict) and j.get("id") == jid), None)
        if job is None:
            raise WriteError("not_found", "no such job")
        if content_hash is not None and content_hash != C.job_content_hash(cfg, job):
            raise WriteError("schedule_conflict", "the job changed since you looked at it; review it again", [{"path": "content_hash", "message": "stale"}])
        rec = approve(cfg, job, via)
        return {"content_hash": rec["content_hash"], "etag": C.job_etag(cfg, job)}


# --------------------------------------------------------------------------- validating and normalizing a change
DRAFT_KEYS = ("id", "name", "description", "when", "catchup_min", "enabled", "runner", "permissions", "allowed_tools", "model", "notify", "prompt")
_WHEN_KEYS = {"daily": {"type", "time"}, "weekdays": {"type", "time"}, "days": {"type", "days", "time"},
              "monthdays": {"type", "days", "time"}, "once": {"type", "at"}}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def check_model(cfg, m):
    m = m.strip() if isinstance(m, str) else m
    if m in (None, ""):
        return None
    if not isinstance(m, str) or not C.MODEL_RE.match(m):
        raise ValueError("model must be opus, sonnet, haiku or a full model id like claude-sonnet-5-5")
    for bad in cfg["model_denylist"]:
        if bad and bad.lower() in m.lower():
            raise ValueError("the model %r is on this install's model_denylist" % m)
    return m


def check_prompt(text, ui=False):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("the prompt is empty")
    n = len(text.encode("utf-8"))
    if "\x00" in text:
        raise ValueError("binary content refused")
    if ui:
        if n > MAX_UI_PROMPT:
            raise ValueError("prompt.md is too large (200 KB max)")
    else:
        if n > MAX_API_PROMPT:
            raise ValueError("the prompt is longer than 16 KiB")
        if C0.search(text):
            raise ValueError("the prompt has control characters (only newline and tab are allowed)")
    return text


def when_to_fields(when):
    """The Talos `when` object -> Chronos {time, days, once}. Raises ValueError."""
    if not isinstance(when, dict) or when.get("type") not in _WHEN_KEYS:
        raise ValueError("when.type must be one of: daily, weekdays, days, monthdays, once")
    t = when["type"]
    extra = set(when) - _WHEN_KEYS[t]
    if extra or not _WHEN_KEYS[t] <= set(when):
        raise ValueError("when for %r takes exactly: %s" % (t, ", ".join(sorted(_WHEN_KEYS[t]))))
    if t == "once":
        return {"once": when["at"], "time": None, "days": None}
    f = {"time": when["time"], "once": None}
    if t in ("daily", "weekdays"):
        f["days"] = t
    elif t == "days":
        if not isinstance(when["days"], list) or not all(isinstance(x, str) for x in when["days"]):
            raise ValueError("when.days must be a list of mon..sun")
        f["days"] = ",".join(when["days"])
    else:
        if not isinstance(when["days"], list) or not all(_is_int(x) for x in when["days"]):
            raise ValueError("when.days must be a list of day-of-month numbers")
        f["days"] = "dom:" + ",".join(str(x) for x in when["days"])
    return f


def _check_fields(cfg, d, ui=False):
    """Validate + normalize a dict of draft fields (all or some). Returns the normalized dict; raises WriteError(schedule_invalid)."""
    out, errs = {}, []

    def bad(path, msg):
        errs.append({"path": path, "message": msg})

    for k in d:
        if k not in DRAFT_KEYS:
            bad(k, "unknown field %r%s" % (k, " (thread_id and other Talos-only fields must be stripped first)" if k == "thread_id" else ""))
    if "id" in d:
        if isinstance(d["id"], str) and C.ID_RE.match(d["id"]):
            out["id"] = d["id"]
        else:
            bad("id", "id must be 2-40 characters of a-z, 0-9 and dashes")
    if "name" in d:
        v = d["name"].strip().replace("\n", " ") if isinstance(d["name"], str) else None
        if v and len(v) <= 60:
            out["name"] = v
        else:
            bad("name", "name must be 1-60 characters")
    if "description" in d:
        v = d["description"].strip().replace("\n", " ") if isinstance(d["description"], str) else None
        if v is not None and len(v) <= 300:
            out["description"] = v
        else:
            bad("description", "description is limited to 300 characters")
    if "when" in d:
        try:
            f = when_to_fields(d["when"])
            clean, e2 = C.validate_schedule({"once": f["once"], "time": f["time"], "days": f["days"], "catchup_min": 180})
            if e2:
                raise ValueError("; ".join(e2))
            probe = {"enabled": True, "catchup_min": 180, "time": clean["time"], "days": clean["days"], "once": clean.get("once")}
            out["when"] = C.when_obj(probe)
            out["_sched"] = clean
        except (ValueError, KeyError, TypeError) as e:
            bad("when", str(e))
    if "catchup_min" in d:
        if _is_int(d["catchup_min"]) and 15 <= d["catchup_min"] <= 1440:
            out["catchup_min"] = d["catchup_min"]
        else:
            bad("catchup_min", "catch-up window must be a whole number of minutes between 15 and 1440")
    if "enabled" in d:
        if isinstance(d["enabled"], bool):
            out["enabled"] = d["enabled"]
        else:
            bad("enabled", "enabled must be true or false")
    if "runner" in d:
        if d["runner"] in ("session", "headless"):
            out["runner"] = d["runner"]
        else:
            bad("runner", "runner must be session or headless")
    if "permissions" in d:
        if d["permissions"] in ("full", "restricted"):
            out["permissions"] = d["permissions"]
        else:
            bad("permissions", "permissions must be full or restricted")
    if "allowed_tools" in d:
        try:
            if not isinstance(d["allowed_tools"], list) or len(d["allowed_tools"]) > 40 or not all(isinstance(x, str) and len(x) <= 200 for x in d["allowed_tools"]):
                raise ValueError("allowed_tools must be a list of at most 40 short strings")
            out["allowed_tools"] = C.validate_allowed_tools(d["allowed_tools"]) or []
        except ValueError as e:
            bad("allowed_tools", str(e))
    if "model" in d:
        try:
            out["model"] = check_model(cfg, d["model"])
        except ValueError as e:
            bad("model", str(e))
    if "notify" in d:
        if d["notify"] in C.NOTIFY_MODES:
            out["notify"] = d["notify"]
        else:
            bad("notify", "notify must be one of: " + ", ".join(C.NOTIFY_MODES))
    if "prompt" in d:
        try:
            out["prompt"] = check_prompt(d["prompt"], ui)
        except ValueError as e:
            bad("prompt", str(e))
    if errs:
        raise WriteError("schedule_invalid", "; ".join("%s: %s" % (x["path"], x["message"]) for x in errs), errs)
    return out


def _runner_fields(runner):
    return {"in_session": runner == "session", "headless": runner != "session"}


def _build_record(cfg, n, now_s):
    """A normalized draft -> a new Chronos job record."""
    sc = n["_sched"]
    rec = {"id": n["id"], "name": n["name"], "description": n["description"], "time": sc["time"], "days": sc["days"],
           "catchup_min": n["catchup_min"], "enabled": n["enabled"]}
    rec.update(_runner_fields(n["runner"]))
    rec.update({"notify": n["notify"], "once": sc.get("once")})
    if n["model"]:
        rec["model"] = n["model"]
    if n["permissions"] == "restricted":
        rec["restricted"] = True
    if n["allowed_tools"]:
        rec["allowed_tools"] = n["allowed_tools"]
    rec["created"] = rec["updated"] = now_s
    return rec


def _apply_set(job, n, now_s):
    """Apply the normalized `set` of an update onto a copy of the job record."""
    new = dict(job)
    for k in ("name", "description", "catchup_min", "enabled", "notify"):
        if k in n:
            new[k] = n[k]
    if "when" in n:
        sc = n["_sched"]
        new.update({"time": sc["time"], "days": sc["days"], "once": sc.get("once")})
    if "runner" in n:
        new.update(_runner_fields(n["runner"]))
    if "permissions" in n:
        if n["permissions"] == "restricted":
            new["restricted"] = True
        else:
            new.pop("restricted", None)
    if "model" in n:
        if n["model"]:
            new["model"] = n["model"]
        else:
            new.pop("model", None)
    if "allowed_tools" in n:
        if n["allowed_tools"]:
            new["allowed_tools"] = n["allowed_tools"]
        else:
            new.pop("allowed_tools", None)
    new["updated"] = now_s
    return new


def _final_checks(job):
    """Rules every stored record must satisfy, whoever wrote it."""
    try:
        C.validate_job_record(job)
    except ValueError as e:
        raise invalid("permissions" if "restricted" in str(e) else "runner", str(e))
    if C.job_runner(job) is None:
        raise invalid("runner", "a job that is neither in_session nor headless can never run")
    if job.get("restricted") is True and job.get("in_session") is True:
        raise invalid("runner", "a restricted job runs headless; it cannot run in the live session")


def plan_change(cfg, change, jobs, ui=False):
    """Validate a change against the current jobs. Never writes. Returns (normalized_change, plan).
    plan = {op, id, old, new (record or None), prompt (bytes or None = unchanged), etag (the etag the write produces, None for delete)}."""
    if not isinstance(change, dict) or change.get("op") not in ("create", "update", "delete", "adopt"):
        raise WriteError("schedule_invalid", "op must be create, update, delete or adopt", [{"path": "op", "message": "op must be create, update, delete or adopt"}])
    op = change["op"]
    now_s = C.iso(C.now())
    by_id = {j.get("id"): j for j in jobs if isinstance(j, dict)}
    if op == "create":
        if set(change) != {"op", "draft"} or not isinstance(change.get("draft"), dict):
            raise invalid("draft", "create takes exactly {op, draft}")
        d = change["draft"]
        missing = [k for k in DRAFT_KEYS if k not in d]
        if missing:
            raise WriteError("schedule_invalid", "missing: " + ", ".join(missing), [{"path": "draft." + k, "message": "required"} for k in missing])
        try:
            n = _check_fields(cfg, d, ui)
        except WriteError as e:
            for f in e.fields:
                f["path"] = "draft." + f["path"]
            raise
        if n["id"] in by_id or os.path.exists(os.path.join(cfg["jobs_dir"], n["id"])):
            raise WriteError("schedule_conflict", "a job with that id already exists", [{"path": "draft.id", "message": "already exists"}])
        rec = _build_record(cfg, n, now_s)
        _final_checks(rec)
        prompt = n["prompt"].encode("utf-8")
        norm = {"op": "create", "draft": dict((k, n[k]) for k in DRAFT_KEYS)}
        return norm, {"op": op, "id": n["id"], "old": None, "new": rec, "prompt": prompt, "etag": C.record_etag(rec, prompt, b"")}
    jid = change.get("id")
    if not isinstance(jid, str) or not C.ID_RE.match(jid):
        raise invalid("id", "id must be 2-40 characters of a-z, 0-9 and dashes")
    old = by_id.get(jid)
    if old is None:
        raise WriteError("not_found", "no such job", [{"path": "id", "message": "no such job"}])
    if op == "adopt":
        if set(change) != {"op", "id", "content_hash"}:
            raise invalid("", "adopt takes exactly {op, id, content_hash}")
        cur = C.job_content_hash(cfg, old)
        if change["content_hash"] != cur:
            raise WriteError("schedule_conflict", "the job changed since the hash was read", [{"path": "content_hash", "message": "stale"}])
        return dict(change), {"op": op, "id": jid, "old": old, "new": old, "prompt": None, "etag": C.job_etag(cfg, old)}
    base = change.get("base_etag")
    if not isinstance(base, str) or not re.match(r"^sha256:[0-9a-f]{64}$", base):
        raise invalid("base_etag", "base_etag is required")
    if base != C.job_etag(cfg, old):
        raise WriteError("schedule_conflict", "the job changed since it was read (stale base_etag)", [{"path": "base_etag", "message": "stale"}])
    if op == "delete":
        if set(change) != {"op", "id", "base_etag"}:
            raise invalid("", "delete takes exactly {op, id, base_etag}")
        return dict(change), {"op": op, "id": jid, "old": old, "new": None, "prompt": None, "etag": None}
    # update
    if C.is_held(cfg, old):
        raise WriteError("held", "this job is held: its files changed outside Chronos. Review them and send an adopt change "
                         "(with the content_hash you reviewed) first; an update must not approve what nobody saw",
                         [{"path": "id", "message": "held"}])
    if set(change) != {"op", "id", "base_etag", "set"} or not isinstance(change.get("set"), dict) or not change["set"]:
        raise invalid("set", "update takes {op, id, base_etag, set} with at least one field in set")
    if C.is_command(old) or not C.clock_on(old):
        raise invalid("id", "command jobs and event-only jobs are edited at the Mac, not through the API")
    if "id" in change["set"]:
        raise invalid("set.id", "a job's id cannot change")
    cur_runner = C.job_runner(old)
    if cur_runner == "both" and "runner" not in change["set"]:
        raise invalid("runner", "this job runs in the session with a background backup; choose a runner (session or headless) to edit it")
    try:
        n = _check_fields(cfg, change["set"], ui)
    except WriteError as e:
        for f in e.fields:
            f["path"] = "set." + f["path"]
        raise
    new = _apply_set(old, n, now_s)
    _final_checks(new)
    norm_set = dict((k, n[k]) for k in DRAFT_KEYS if k in n)
    prompt = n["prompt"].encode("utf-8") if "prompt" in n else None
    pb = prompt if prompt is not None else C._read_bytes(C.prompt_file(cfg, jid))
    return ({"op": "update", "id": jid, "base_etag": base, "set": norm_set},
            {"op": op, "id": jid, "old": old, "new": new, "prompt": prompt, "etag": C.record_etag(new, pb, C._read_bytes(C.guard_file(cfg, jid))),
             "was_clean": not C.changed_outside(cfg, old)})


def preview(cfg, plan):
    rec = plan["new"] or plan["old"]
    nf = []
    try:
        if rec is not None and C.clock_on(rec):
            nf = [C.aware_iso(x) for x in C.next_fires(rec, C.now(), 3)]
    except Exception:
        nf = []
    return {"when_text": C.english(rec) if rec is not None else "", "next_runs": nf, "tz": C.zone_name()}


def validate_change(cfg, change):
    """The dry run: {ok, normalized, preview, expected_etag}. Never writes."""
    normalized, plan = plan_change(cfg, change, read_jobs(cfg))
    return {"ok": True, "normalized": normalized, "preview": preview(cfg, plan), "expected_etag": plan["etag"]}


# --------------------------------------------------------------------------- the single writer
def apply_change(cfg, change, expected_etag, via="api_apply", ui=False, normalized_only=True):
    """Apply one change under the jobs flock: re-validate, refuse if the change is not already normalized or if the etag it
    would produce is not `expected_etag`, then write the prompt (byte-exact), jobs.json and the approved record together.
    Returns {"ok": True, "etag": <new etag or None for delete>}."""
    with jobs_lock(cfg):
        jobs = read_jobs(cfg)
        normalized, plan = plan_change(cfg, change, jobs, ui)
        if normalized_only and normalized != change:
            raise WriteError("schedule_invalid", "the change is not normalized; send the `normalized` value validate returned",
                             [{"path": "", "message": "not normalized"}])
        if expected_etag != plan["etag"]:
            raise WriteError("schedule_conflict", "the result would not have the expected etag", [{"path": "expected_etag", "message": "mismatch"}])
        execute(cfg, plan, jobs, via)
        job = plan["new"]
        return {"ok": True, "id": plan["id"], "etag": C.job_etag(cfg, job) if job is not None else None}


def execute(cfg, plan, jobs, via):
    """Do what a plan says. Caller holds the lock and has validated."""
    op, jid = plan["op"], plan["id"]
    if op == "create":
        tdir = os.path.join(cfg["jobs_dir"], jid)
        os.makedirs(cfg["jobs_dir"], exist_ok=True)
        try:
            os.mkdir(tdir)
        except FileExistsError:
            raise WriteError("schedule_conflict", "a job with that id already exists", [{"path": "draft.id", "message": "already exists"}])
        try:
            atomic_write(C.prompt_file(cfg, jid), plan["prompt"], 0o644)
            write_jobs(cfg, jobs + [plan["new"]])
            approve(cfg, plan["new"], via)
        except Exception:
            shutil.rmtree(tdir, ignore_errors=True)
            raise
    elif op == "update":
        if plan["prompt"] is not None:
            snapshot_prompt(cfg, jid)
            atomic_write(C.prompt_file(cfg, jid), plan["prompt"], 0o644)
        out = [plan["new"] if j.get("id") == jid else j for j in jobs]
        write_jobs(cfg, out)
        # re-approve only a job that matched its record before this update: otherwise the update would approve outside
        # edits (a hand-changed prompt, model or tool list) that the caller never submitted
        if plan.get("was_clean", True):
            approve(cfg, plan["new"], via)
    elif op == "adopt":
        approve(cfg, plan["new"], via)
    elif op == "delete":
        delete_files(cfg, jid, jobs)


def delete_files(cfg, jid, jobs):
    """Archive the job folder under history/deleted/, drop the record, its approval, its pause flag, its webhook secret and queue."""
    import chronos_triggers as TR
    tdir = os.path.join(cfg["jobs_dir"], jid)
    arch = os.path.join(cfg["history_dir"], "deleted", "%s-%s" % (jid, _stamp()))
    if os.path.isdir(tdir):
        os.makedirs(arch, exist_ok=True)
        for nme in ("prompt.md", "guard.md"):
            if os.path.isfile(os.path.join(tdir, nme)):
                shutil.copy2(os.path.join(tdir, nme), os.path.join(arch, nme))
        shutil.rmtree(tdir)
    write_jobs(cfg, [j for j in jobs if j.get("id") != jid])
    for p in (os.path.join(cfg["paused_dir"], jid), C.approved_path(cfg, jid)):
        try:
            os.remove(p)
        except OSError:
            pass
    TR.delete_secret(cfg, jid)
    TR.clear_queue(cfg, jid)
    return arch


def disable(cfg, jid):
    """Switch a job off (enabled:false) under the lock. It is not an approval: a held job stays held."""
    with jobs_lock(cfg):
        jobs = read_jobs(cfg)
        job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == jid), None)
        if job is None:
            raise WriteError("not_found", "no such job")
        if job.get("enabled") is not False:
            job["enabled"] = False
            job["updated"] = C.iso(C.now())
            write_jobs(cfg, jobs)
        return {"ok": True, "id": jid, "etag": C.job_etag(cfg, job)}


# --------------------------------------------------------------------------- config
CONFIG_KEYS = {"session_runner": ("hook", "talos"), "approval_required": (True, False), "session_wait_notify_min": None}


def both_jobs(cfg):
    return [j["id"] for j in read_jobs(cfg) if isinstance(j, dict) and C.job_runner(j) == "both"]


def set_config(cfg, values):
    """Write config keys together, in one atomic write. Refuses session_runner=talos while any `both` job exists."""
    clean = {}
    for k, v in values.items():
        if k not in CONFIG_KEYS:
            raise WriteError("bad_request", "cannot set %r (allowed: %s)" % (k, ", ".join(sorted(CONFIG_KEYS))))
        if k == "session_wait_notify_min":
            if not _is_int(v) or v < 0:
                raise WriteError("schedule_invalid", "session_wait_notify_min must be a whole number, 0 or more", [{"path": k, "message": "invalid"}])
        elif v not in CONFIG_KEYS[k] or (k == "approval_required" and not isinstance(v, bool)):
            raise WriteError("schedule_invalid", "%s must be one of %s" % (k, ", ".join(str(x).lower() for x in CONFIG_KEYS[k])), [{"path": k, "message": "invalid"}])
        clean[k] = v
    if not clean:
        raise WriteError("bad_request", "nothing to set")
    path = cfg["config_path"]
    with jobs_lock(cfg):
        if clean.get("session_runner") == "talos":
            blockers = both_jobs(cfg)
            if blockers:
                raise WriteError("both_jobs_remain", "these jobs run in the session with a background backup; make each one session or headless first: " + ", ".join(blockers),
                                 [{"path": b, "message": "runner is both"} for b in blockers])
        try:
            cur = json.load(open(path, encoding="utf-8"))
            if not isinstance(cur, dict):
                cur = {}
        except (OSError, ValueError):
            cur = {}
        cur.update(clean)
        atomic_write(path, json.dumps(cur, indent=2, ensure_ascii=False) + "\n")
    return clean
