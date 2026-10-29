"""
chronos_runlog: the Chronos usage meter. Python 3.9+, standard library only.

bin/chronos-run.sh launches claude with `--output-format=stream-json --verbose` and then calls

    bin/chronos record --raw RAW --log LOG --err ERR --id JOB --kind schedule|event \\
        --start EPOCH --end EPOCH --rc N --timed-out 0|1 [--ttype T --thash H] --marker-path P

which (1) turns the JSONL stream into the plain-text LOG that the UI and humans read (final text, then stderr),
(2) appends one row to <home_dir>/runs.jsonl, (3) saves the newest `rate_limit_event` to <home_dir>/rate-limits.json,
and (4) prints  ok  or  fail  on stdout. It never raises into the caller: the worst case is a raw log.

Why stream-json and not plain json: only the stream carries `rate_limit_event` (the 7-day and 5-hour utilisation),
and a run killed by the watchdog still leaves a readable partial log. The field names below were checked against
Claude Code 2.1.x. If a future version changes the shape the parser degrades to "usage unknown" and keeps the
raw output as the log; it can never change the outcome of a run.

The same module reads the file back for the UI (read_rows / summarize).
"""
import argparse
import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time

import chronoslib as C


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def parse_stream(path):
    """-> dict(result, text, init_model, rate, lines, parsed). Tolerates junk lines and truncated output."""
    out = {"result": None, "text": "", "init_model": None, "rate": None, "lines": 0, "parsed": 0, "last_assistant": "", "tool_uses": 0}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return out
    with fh:
        for line in fh:
            out["lines"] += 1
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            out["parsed"] += 1
            t = d.get("type")
            if t == "result":
                out["result"] = d
            elif t == "system" and d.get("subtype") == "init":
                out["init_model"] = d.get("model")
            elif t == "rate_limit_event" and isinstance(d.get("rate_limit_info"), dict):
                out["rate"] = d["rate_limit_info"]
            elif t == "assistant":
                try:
                    out["tool_uses"] += sum(1 for c in (d.get("message") or {}).get("content") or [] if isinstance(c, dict) and c.get("type") == "tool_use")
                except Exception:
                    pass
                try:
                    texts = [c.get("text", "") for c in (d.get("message") or {}).get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
                    if any(texts):
                        out["last_assistant"] = "\n".join(x for x in texts if x)
                except Exception:
                    pass
    return out


def empty_stream():
    return {"result": None, "text": "", "init_model": None, "rate": None, "lines": 0, "parsed": 0, "last_assistant": "", "tool_uses": 0}


def build_row(a, ps):
    if getattr(a, "command", 0):
        # a command job: no Claude, so no tokens or cost; the row exists so the run history stays complete
        return {
            "job": a.id, "kind": a.kind, "command": True,
            "start": _iso(a.start), "end": _iso(a.end), "start_ts": a.start, "duration_s": max(0, a.end - a.start),
            "exit": a.rc, "timed_out": bool(a.timed_out), "model": "(command)", "model_flag": None,
            "input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0,
            "cost_usd": 0, "num_turns": None, "is_error": None, "usage_missing": False, "denials": 0,
            "trigger": None, "session_id": None, "log": os.path.basename(a.log),
        }
    res = ps["result"] or {}
    usage = res.get("usage") or {}
    mu = res.get("modelUsage") or {}
    model = "+".join(mu.keys()) if mu else (ps["init_model"] or None)
    if not model:
        model = a.model_flag or "default"
    return {
        "job": a.id, "kind": a.kind,
        "start": _iso(a.start), "end": _iso(a.end), "start_ts": a.start, "duration_s": max(0, a.end - a.start),
        "exit": a.rc, "timed_out": bool(a.timed_out), "model": model, "model_flag": a.model_flag or None,
        "input_tokens": int(usage.get("input_tokens") or 0), "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cost_usd": res.get("total_cost_usd"), "num_turns": res.get("num_turns"), "is_error": bool(res.get("is_error")) if res else None,
        "usage_missing": not bool(res), "denials": len(res.get("permission_denials") or []),
        "trigger": ({"type": a.ttype, "hash": a.thash} if a.thash else None),
        "session_id": res.get("session_id"), "log": os.path.basename(a.log),
    }


def denial_count(ps):
    return len((ps["result"] or {}).get("permission_denials") or [])


def all_denied(ps):
    """True when the run tried tools and the permission system refused every attempt: a restricted job that could do none of
    its work. Counted from the stream (tool_use blocks) against the result's permission_denials."""
    d = denial_count(ps)
    return d > 0 and ps["tool_uses"] > 0 and d >= ps["tool_uses"]


def write_log(a, ps, raw_text):
    res = ps["result"]
    if res is not None:
        body = str(res.get("result") or "")
        if res.get("is_error"):
            body = "[claude reported an error: %s]\n%s" % (res.get("subtype") or "error", body)
        if (a.kind == "event" or getattr(a, "restricted", 0)) and denial_count(ps):
            body = body.rstrip("\n") + "\n\n[chronos: the permission system refused %d tool call(s) in this run%s]" % (
                denial_count(ps), "; EVERY tool call was refused, so the job did none of its work" if all_denied(ps) else "")
    elif ps["parsed"]:
        body = (ps["last_assistant"] + "\n" if ps["last_assistant"] else "") + "[no final result: the run ended early (killed or crashed); partial output above]"
    else:
        body = raw_text  # not a stream at all (claude printed plain text or an error): keep it verbatim
    err = ""
    try:
        with open(a.err, encoding="utf-8", errors="replace") as fh:
            err = fh.read().strip()
    except OSError:
        pass
    # claude prints this harmless notice when stdin is empty; drop it so logs stay clean
    err = "\n".join(l for l in err.split("\n") if "no stdin data received" not in l).strip()
    text = body.rstrip("\n") + "\n"
    if err:
        text += "\n--- stderr ---\n" + err + "\n"
    tmp = a.log + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, a.log)


def save_rate(cfg, a, rate):
    if not rate:
        return
    uw = rate.get("unifiedWindows") or {}
    rec = {"captured": _iso(a.end), "captured_ts": a.end, "job": a.id, "status": rate.get("status"),
           "seven_day": uw.get("seven_day") or ({"utilization": rate.get("utilization"), "resetsAt": rate.get("resetsAt")} if rate.get("rateLimitType") == "seven_day" else None),
           "five_hour": uw.get("five_hour") or ({"utilization": rate.get("utilization"), "resetsAt": rate.get("resetsAt")} if rate.get("rateLimitType") == "five_hour" else None),
           "is_using_overage": rate.get("isUsingOverage")}
    os.makedirs(cfg["home_dir"], exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".rl.", dir=cfg["home_dir"])
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.chmod(tmp, 0o600)
    os.replace(tmp, cfg["rate_file"])


def record(cfg, a):
    try:
        raw_text = open(a.raw, encoding="utf-8", errors="replace").read()
    except OSError:
        raw_text = ""
    ps = empty_stream() if getattr(a, "command", 0) else parse_stream(a.raw)   # a command's output is never a Claude stream
    row = build_row(a, ps)
    if a.kind == "event" or getattr(a, "restricted", 0):
        ok = (a.rc == 0 and not a.timed_out and ps["result"] is not None and not ps["result"].get("is_error"))
        if ps["result"] is None and ps["parsed"] == 0 and a.rc == 0 and not a.timed_out:
            ok = True            # claude printed plain text (older CLI, or a stub): a clean exit is still a success
        if ok and all_denied(ps):
            ok = False           # every tool call was refused: the run "finished" but did nothing; a failure, not a done job
    else:
        ok = bool(a.marker_path) and os.path.exists(a.marker_path)
    row["ok"] = bool(ok)
    try:
        write_log(a, ps, raw_text)
    except Exception as e:
        sys.stderr.write("runlog: could not write log: %r\n" % e)
    try:
        os.makedirs(cfg["home_dir"], exist_ok=True)
        with open(cfg["runs_file"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        sys.stderr.write("runlog: could not append runs.jsonl: %r\n" % e)
    try:
        save_rate(cfg, a, ps["rate"])
    except Exception as e:
        sys.stderr.write("runlog: could not save rate limits: %r\n" % e)
    print("ok" if ok else "fail")


# --------------------------------------------------------------------------- reading (UI)
def read_rows(cfg, since_ts=None):
    rows = []
    try:
        with open(cfg["runs_file"], encoding="utf-8") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("job") and (since_ts is None or float(r.get("start_ts") or 0) >= since_ts):
                    rows.append(r)
    except OSError:
        pass
    return rows


def _tok(r):
    return {"input": r.get("input_tokens") or 0, "output": r.get("output_tokens") or 0, "cache_write": r.get("cache_creation_tokens") or 0,
            "cache_read": r.get("cache_read_tokens") or 0}


def _add(acc, r):
    t = _tok(r)
    for k, v in t.items():
        acc[k] = acc.get(k, 0) + v
    acc["cost"] = round(acc.get("cost", 0.0) + float(r.get("cost_usd") or 0.0), 6)
    acc["runs"] = acc.get("runs", 0) + 1
    acc["secs"] = acc.get("secs", 0) + int(r.get("duration_s") or 0)


def summarize(cfg, rows, now=None, days=14):
    """-> {days:[{date, jobs:{job:{cost,input,...}}, total:{...}}], jobs:{job:{last, week}}, order:[job...], rate:{...}|None}"""
    now = now or time.time()
    today = datetime.date.fromtimestamp(now)
    dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    by_day = {d: {"date": d, "jobs": {}, "total": {}} for d in dates}
    jobs = {}
    week_start = now - 7 * 86400
    for r in rows:
        ts = float(r.get("start_ts") or 0)
        d = datetime.date.fromtimestamp(ts).isoformat()
        j = r["job"]
        if d in by_day:
            _add(by_day[d]["jobs"].setdefault(j, {}), r)
            _add(by_day[d]["total"], r)
        e = jobs.setdefault(j, {"last": None, "week": {}})
        if ts >= week_start:
            _add(e["week"], r)
        if e["last"] is None or ts >= float(e["last"].get("start_ts") or 0):
            e["last"] = r
    order = sorted({j for d in by_day.values() for j in d["jobs"]}, key=lambda j: -sum(by_day[d]["jobs"].get(j, {}).get("cost", 0) for d in dates))
    rate = None
    try:
        with open(cfg["rate_file"], encoding="utf-8") as fh:
            rate = json.load(fh)
    except (OSError, ValueError):
        pass
    return {"days": [by_day[d] for d in dates], "jobs": jobs, "order": order, "rate": rate}


# =========================================================================== v0.3.0: runs (claim, finish, orphans, start, session-prompt, health, probe)
# Everything below is the Chronos side of "who is running what": run ids, the claim's sibling owner file, per-run reports, the
# launch-stall block, and the signals `health` reports. The api verbs in chronos_api.py are thin wrappers over these functions.
RUN_ID_RE = re.compile(r"^r_[0-9A-HJKMNP-TV-Z]{26}$")
DS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FINISH_STATUSES = ("ok", "failed", "timed_out", "session_ended")
MAX_REPORT_BYTES = 256 * 1024
PROTECTED_DIRS = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents", "Library/CloudStorage")
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


class RunError(Exception):
    """A request that is not valid: code is a chronos_write.WriteError code (bad_request, not_found, schedule_invalid, held)."""

    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code, self.message = code, message


def new_run_id():
    """r_<ULID>: 48 bits of milliseconds (the scheduler's clock) then 80 random bits, Crockford base32."""
    ms = int(C.localize(C.now()).timestamp() * 1000) if os.environ.get("CHRONOS_NOW") else int(time.time() * 1000)
    n = (ms << 80) | secrets.randbits(80)
    out = ""
    for _ in range(26):
        out = _B32[n & 31] + out
        n >>= 5
    return "r_" + out


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_json(path, obj, mode=0o600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def run_report_path(cfg, jid, ds, run_id):
    """reports/<id>-<ds>-<run_id>.md. Both ids are validated by their regexes before any path is built; the run id is otherwise only a key."""
    return os.path.join(cfg["reports_dir"], "%s-%s-%s.md" % (jid, ds, run_id))


def _job(cfg, jid):
    if not C.ID_RE.match(jid or ""):
        raise RunError("bad_request", "bad job id")
    try:
        jobs = C.load_jobs(cfg)
    except Exception as e:
        raise RunError("registry_unreadable", "jobs file unreadable: %s" % e)
    job = next((j for j in jobs if isinstance(j, dict) and j.get("id") == jid), None)
    if job is None:
        raise RunError("not_found", "no such job")
    return job


def _aware_to_local(s):
    t = datetime.datetime.fromisoformat(s)
    return t.astimezone(C.zone_info()).replace(tzinfo=None) if t.tzinfo is not None else t


def read_owner(cfg, jid, ds):
    return _read_json(C.owner_path(cfg, jid, ds))


def owners(cfg, jid=None):
    """[(id, ds, owner dict)] for every claim owner file in the state dir."""
    out = []
    try:
        names = sorted(os.listdir(cfg["state_dir"]))
    except OSError:
        return out
    for n in names:
        m = re.match(r"^claim-([a-z0-9-]{2,40})-(\d{4}-\d{2}-\d{2})\.owner\.json$", n)
        if not m or (jid and m.group(1) != jid):
            continue
        o = _read_json(os.path.join(cfg["state_dir"], n))
        if o is not None:
            out.append((m.group(1), m.group(2), o))
    return out


def write_owner(cfg, jid, ds, run_id, runner, at, manual=False, fire_at=None, bridge_instance="", trigger=None, extra=None):
    o = {"run_id": run_id, "id": jid, "ds": ds, "runner": runner, "claimed_at": C.aware_iso(at), "fire_at": fire_at,
         "manual": bool(manual), "bridge_instance": bridge_instance or ""}
    if trigger:
        o["trigger"] = trigger
    if extra:
        o.update(extra)
    _write_json(C.owner_path(cfg, jid, ds), o)


def _check_run_id(rid):
    if not isinstance(rid, str) or not RUN_ID_RE.match(rid):
        raise RunError("bad_request", "run_id must look like r_ followed by 26 characters of a ULID")


def _check_ds(ds):
    if not isinstance(ds, str) or not DS_RE.match(ds):
        raise RunError("bad_request", "ds must be YYYY-MM-DD")
    try:
        return datetime.date.fromisoformat(ds)
    except ValueError:
        raise RunError("bad_request", "ds is not a date")


# --------------------------------------------------------------------------- claim
def claim_run(cfg, jid, req, at=None):
    """-> {"ok": true, "run_id", "ds"} or {"ok": false, "reason": already_done | running_elsewhere | not_due | window_passed | held | paused |
    failed_today}. The claim directory is the atomic part (mkdir); the owner file is written in the same call, tmp + rename."""
    at = at or C.now()
    if not isinstance(req, dict):
        raise RunError("bad_request", "claim takes a JSON object")
    job = _job(cfg, jid)
    rid = req.get("run_id")
    _check_run_id(rid)
    manual, again = req.get("manual") is True, req.get("again") is True
    runner = req.get("runner") or "session"
    if runner not in ("session", "headless"):
        raise RunError("bad_request", "runner must be session or headless")
    inst = req.get("bridge_instance") or ""
    if not isinstance(inst, str) or len(inst) > 80 or any(ord(c) < 32 for c in inst):
        raise RunError("bad_request", "bridge_instance must be a short plain string")
    fire_at = req.get("fire_at")
    if fire_at is not None and (not isinstance(fire_at, str) or len(fire_at) > 40):
        raise RunError("bad_request", "fire_at must be an RFC 3339 instant or null")
    if runner == "session" and (C.is_restricted(job) or C.is_command(job)):
        raise RunError("schedule_invalid", "a restricted or command job never runs in a live session")
    ds = req.get("ds")
    d = _check_ds(ds) if ds is not None else None
    if ds is None:
        ds = C.date_key(job, at)         # Run now: the job's own date rule (a one-shot keeps its `once` date)
        d = datetime.date.fromisoformat(ds)
    if C.is_held(cfg, job):
        return {"ok": False, "reason": "held"}
    if not manual:
        if job.get("enabled") is not True or not C.clock_on(job):
            return {"ok": False, "reason": "not_due"}
        if job.get("once"):
            if C.parse_once(job["once"]).date() != d:
                return {"ok": False, "reason": "not_due"}
        elif not C.is_due_day(job, d):
            return {"ok": False, "reason": "not_due"}
        f = C.fire_dt(job, d)
        if at < f:
            return {"ok": False, "reason": "not_due"}
        if at >= f + datetime.timedelta(minutes=C.window_minutes(job)):
            return {"ok": False, "reason": "window_passed"}
        if os.path.exists(cfg["pause_all"]) or C.is_paused(cfg, jid):
            return {"ok": False, "reason": "paused"}
    ran = os.path.exists(C.ran_path(cfg, jid, ds))
    if ran and not (manual and again):
        return {"ok": False, "reason": "already_done"}
    if not manual and (os.path.exists(C.failed_path(cfg, jid, ds)) or os.path.exists(C.stalled_path(cfg, jid, ds))):
        return {"ok": False, "reason": "failed_today"}
    os.makedirs(cfg["state_dir"], exist_ok=True)
    cp = C.claim_path(cfg, jid, ds)
    C.sweep_stale_claim(cfg, cp, at)
    try:
        os.mkdir(cp)
    except FileExistsError:
        return {"ok": False, "reason": "running_elsewhere"}
    if manual:                                   # under the claim: Run now clears the day's failure, stall and (with `again`) done marker
        for pth in (C.failed_path(cfg, jid, ds), C.stalled_path(cfg, jid, ds)) + ((C.ran_path(cfg, jid, ds),) if again else ()):
            try:
                os.remove(pth)
            except FileNotFoundError:
                pass
    try:
        write_owner(cfg, jid, ds, rid, runner, at, manual=manual, fire_at=fire_at, bridge_instance=inst, trigger="run_now" if manual else "clock")
    except Exception:
        C.remove_claim(cp)
        raise
    return {"ok": True, "run_id": rid, "ds": ds}


# --------------------------------------------------------------------------- runs.jsonl rows
def append_row(cfg, row):
    os.makedirs(cfg["home_dir"], exist_ok=True)
    with open(cfg["runs_file"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def run_rows(cfg, jid=None, run_id=None):
    out = []
    for r in read_rows(cfg):
        if r.get("run_id") and (jid is None or r.get("job") == jid) and (run_id is None or r.get("run_id") == run_id):
            out.append(r)
    return out


def closed_run(cfg, run_id):
    """The runs.jsonl row that closed this run id (a terminal status), or None."""
    for r in run_rows(cfg, run_id=run_id):
        if r.get("status") in FINISH_STATUSES + ("launch_stalled",):
            return r
    return None


def schedule_run(cfg, row):
    """A runs.jsonl row -> the ScheduleRun object the app shows."""
    jid, ds, rid = row.get("job"), row.get("ds"), row.get("run_id")
    has_report = os.path.isfile(run_report_path(cfg, jid, ds, rid)) or os.path.isfile(C.report_path(cfg, jid, ds))
    out = {"run_id": rid, "ds": ds, "started_at": row.get("started_at"), "runner": row.get("runner") or "headless",
           "trigger": row.get("trigger_kind") or "clock", "status": row.get("status") or ("ok" if row.get("ok") else "failed")}
    if row.get("duration_s") is not None:
        out["duration_s"] = int(row["duration_s"])
    if row.get("runner") == "headless" and isinstance(row.get("cost_usd"), (int, float)):
        out["cost_usd"] = row["cost_usd"]
    out["has_report"] = bool(has_report)
    note = late_note(cfg, jid, ds, rid)
    if note:
        out["late_note"] = note[:280]
    return out


def late_note(cfg, jid, ds, rid):
    try:
        with open(os.path.join(cfg["reports_dir"], "%s-%s-%s.late.md" % (jid, ds, rid)), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


# --------------------------------------------------------------------------- finish
def finish_run(cfg, jid, req, at=None):
    """-> {"ok": true} | {"ok": false, "reason": "not_owner" | "late"}. Owner check against the claim's sibling file, keyed on that claim's ds."""
    at = at or C.now()
    if not isinstance(req, dict):
        raise RunError("bad_request", "finish takes a JSON object")
    _job(cfg, jid)
    rid, status, report = req.get("run_id"), req.get("status"), req.get("report", "")
    _check_run_id(rid)
    if status not in FINISH_STATUSES:
        raise RunError("bad_request", "status must be one of %s" % ", ".join(FINISH_STATUSES))
    if not isinstance(report, str) or len(report.encode("utf-8")) > MAX_REPORT_BYTES:
        raise RunError("bad_request", "report must be text of at most %d bytes" % MAX_REPORT_BYTES)
    closed = closed_run(cfg, rid)
    if closed is not None:                                   # an already-closed run: keep what was decided, save the words as a note
        if closed.get("job") == jid and DS_RE.match(str(closed.get("ds"))) and report:
            _write_text(os.path.join(cfg["reports_dir"], "%s-%s-%s.late.md" % (jid, closed["ds"], rid)), report)
        return {"ok": False, "reason": "late"}
    mine = next(((i, ds, o) for i, ds, o in owners(cfg, jid) if o.get("run_id") == rid), None)
    if mine is None:
        return {"ok": False, "reason": "not_owner"}
    _, ds, own = mine
    if report:
        _write_text(run_report_path(cfg, jid, ds, rid), report)
        _write_text(C.report_path(cfg, jid, ds), report)     # the old per-day path stays a copy of the latest
    ok = status == "ok"
    os.makedirs(cfg["state_dir"], exist_ok=True)
    open(C.ran_path(cfg, jid, ds) if ok else C.failed_path(cfg, jid, ds), "a").close()
    started = _aware_to_local(own["claimed_at"]) if own.get("claimed_at") else at
    start_ts = int(C.localize(started).timestamp())
    end_ts = int(C.localize(at).timestamp())
    append_row(cfg, {"job": jid, "kind": "schedule", "start": _iso(start_ts), "end": _iso(end_ts), "start_ts": start_ts,
                     "duration_s": max(0, end_ts - start_ts), "exit": 0 if ok else 1, "timed_out": status == "timed_out", "model": "(session)",
                     "model_flag": None, "input_tokens": 0, "output_tokens": 0, "cache_creation_tokens": 0, "cache_read_tokens": 0,
                     "cost_usd": None, "num_turns": None, "is_error": None, "usage_missing": True, "denials": 0, "trigger": None,
                     "session_id": None, "log": "", "ok": ok, "run_id": rid, "ds": ds, "runner": own.get("runner") or "session",
                     "started_at": own.get("claimed_at"), "status": status,
                     "trigger_kind": own.get("trigger") or "clock"})
    C.remove_claim(C.claim_path(cfg, jid, ds))
    if ok:
        C.disable_once(cfg, jid)                              # a finished one-shot switches itself off
    return {"ok": True}


def orphans(cfg, instance):
    """Session claims whose owner file names a bridge instance other than `instance` (N11)."""
    if not isinstance(instance, str) or not instance or len(instance) > 80:
        raise RunError("bad_request", "orphans needs --bridge-instance <id>")
    return {"orphans": [{"id": i, "ds": ds, "run_id": o.get("run_id")} for i, ds, o in owners(cfg)
                        if o.get("runner") == "session" and (o.get("bridge_instance") or "") != instance and os.path.isdir(C.claim_path(cfg, i, ds))]}


def add_record_arguments(sub):
    r = sub.add_parser("record")
    r.add_argument("--raw", required=True)
    r.add_argument("--log", required=True)
    r.add_argument("--err", default="")
    r.add_argument("--id", required=True)
    r.add_argument("--kind", choices=("schedule", "event"), default="schedule")
    r.add_argument("--start", type=int, required=True)
    r.add_argument("--end", type=int, required=True)
    r.add_argument("--rc", type=int, default=0)
    r.add_argument("--timed-out", dest="timed_out", type=int, default=0)
    r.add_argument("--model-flag", dest="model_flag", default="")
    r.add_argument("--ttype", default="")
    r.add_argument("--thash", default="")
    r.add_argument("--marker-path", dest="marker_path", default="")
    r.add_argument("--command", type=int, default=0)         # 1 = a command job (plain output, no usage)
    r.add_argument("--restricted", type=int, default=0)      # 1 = a restricted clock run: judged like an event run (clean exit, no error result)
    return r


def main(argv, cfg=None):
    cfg = cfg or C.load_config()
    ap = argparse.ArgumentParser(prog="chronos record")
    sub = ap.add_subparsers(dest="cmd")
    add_record_arguments(sub)
    a = ap.parse_args(argv)
    if a.cmd == "record":
        record(cfg, a)
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
