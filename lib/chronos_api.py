"""
chronos_api: `chronos api <verb>`, the JSON interface other programs use to read and drive Chronos.
Python 3.9+, standard library only.

  Input   JSON on stdin (some verbs also take an id as an argument).
  Output  one JSON document on stdout. Exit 0 on success, 1 on error. Errors are
          {"error": {"code": "...", "message": "...", "fields": [{"path": "...", "message": "..."}]}}.
  Times   every instant is RFC 3339 with an offset (2026-07-15T09:00:00-04:00); `tz` is an IANA zone name.

Verbs (0.3.0): version, snapshot, detail <id>, validate, apply, pause, disable <id>, config set, due --for session,
session-beat, approved <id>, migrate. The run verbs (claim, finish, start, session-prompt, health, probe, orphans) arrive in
the next release candidate.
"""
import json
import os
import sys

import chronoslib as C
import chronos_write as W

PREVIEW_CHARS = 400
DETAIL_PROMPT_MAX = 64 * 1024
DETAIL_GUARD_MAX = 16 * 1024
EDIT_PROMPT_MAX = W.MAX_API_PROMPT
SYNC_FOLDERS = ("Library/Mobile Documents", "Library/CloudStorage", "Dropbox")


def _strip_controls(s):
    return "".join(ch for ch in s if ch in "\n\t" or ord(ch) >= 32 and ord(ch) != 127)


def _trim_bytes(b, limit):
    if len(b) <= limit:
        return b.decode("utf-8", "replace"), False
    cut = b[:limit]
    return cut.decode("utf-8", "ignore"), True


def _last_run(cfg, jid):
    """The latest finished day (from its ran-/failed-/stalled- marker). Run ids and timings arrive with the run verbs."""
    best = None
    try:
        names = os.listdir(cfg["state_dir"])
    except OSError:
        return None
    for n in names:
        for prefix, status in (("ran-", "ok"), ("failed-", "failed"), ("stalled-", "launch_stalled")):
            if n.startswith(prefix + jid + "-"):
                ds = n[len(prefix) + len(jid) + 1:]
                if len(ds) == 10 and ds[4] == "-" and ds[7] == "-" and (best is None or ds >= best[0]):
                    best = (ds, status)
    if best is None:
        return None
    ds, status = best
    return {"ds": ds, "status": status, "has_report": os.path.isfile(C.report_path(cfg, jid, ds))}


def _editable(cfg, job, runner, prompt_bytes):
    if C.is_command(job):
        return False, "Command jobs are edited at the Mac"
    if not C.clock_on(job):
        return False, "Runs on events only; edit it at the Mac"
    if runner is None:
        return False, "Has no way to run"
    if runner == "both":
        return False, "Session, with a background backup (set outside Talos)"
    if prompt_bytes > EDIT_PROMPT_MAX:
        return False, "Prompt is longer than the app can edit. Edit it at the Mac"
    return True, None


def describe(cfg, job, at, beat_age):
    jid = job["id"]
    pb, gb = C._read_bytes(C.prompt_file(cfg, jid)), C._read_bytes(C.guard_file(cfg, jid))
    runner = C.job_runner(job)
    d, state = C.current_day(cfg, job, at)
    paused = C.is_paused(cfg, jid)
    held = state == "held"
    enabled = job.get("enabled") is True
    nf = C.next_fires(job, at, 1) if enabled else []
    fire_end = None
    if state in ("waiting", "running") and C.clock_on(job):
        try:
            fire_end = C.aware_iso(C.fire_dt(job, d) + C.datetime.timedelta(minutes=C.window_minutes(job)))
        except Exception:
            fire_end = None
    editable, why = _editable(cfg, job, runner, len(pb))
    try:
        when, when_text = C.when_obj(job), C.english(job)
    except Exception:
        when, when_text = {"type": "events"}, "Schedule unreadable"
        editable, why = False, "Schedule unreadable"
    etag, chash = C.record_etag(job, pb, gb), C.record_content_hash(job, pb, gb)
    rec = C.read_approved(cfg, jid)
    beat_bucket = "stale" if beat_age is None or beat_age > 180 else "fresh"
    state_rev = C.sha256_ref(C.canon([C.day_str(d), state, "", "", paused, held, beat_bucket]))
    prompt_text = pb.decode("utf-8", "replace")
    return {
        "id": jid, "name": job.get("name") or jid, "description": job.get("description") or "",
        "kind": "command" if C.is_command(job) else "claude",
        "when": when, "when_text": when_text, "catchup_min": C.window_minutes(job),
        "enabled": enabled, "paused": paused,
        "runner": runner or "headless", "permissions": C.job_permissions(job),
        "allowed_tools": list(job.get("allowed_tools") or []), "model": job.get("model") or None,
        "notify": C.job_notify_mode(job), "triggers_count": len(job.get("triggers") or []),
        "next_run": C.aware_iso(nf[0]) if nf else None, "today": state, "ds": C.day_str(d), "window_ends": fire_end,
        "last_run": _last_run(cfg, jid),
        "prompt_preview": _strip_controls(prompt_text)[:PREVIEW_CHARS] if not C.is_command(job) else "",
        "prompt_bytes": len(pb), "editable": editable, "read_only_reason": why,
        "changed_outside": rec is None or rec["content_hash"] != chash,
        "etag": etag, "content_hash": chash, "state_rev": state_rev,
    }


def _beat_age(path, at):
    try:
        import datetime
        txt = C._read_bytes(path).decode("utf-8").strip()
        t = datetime.datetime.fromisoformat(txt)
        return max(0, int((at - t).total_seconds()))
    except (ValueError, OSError):
        try:
            return max(0, int(C.time.time() - os.path.getmtime(path)))
        except OSError:
            return None


def _under(path, rels):
    home = os.path.expanduser("~")
    return any(os.path.realpath(path).startswith(os.path.join(home, r)) for r in rels)


def snapshot(cfg):
    at = C.now()
    try:
        jobs = C.load_jobs(cfg)
    except FileNotFoundError:
        jobs = []
    except Exception as e:
        raise W.WriteError("registry_unreadable", "jobs file unreadable (%s): %s" % (cfg["jobs_file"], e))
    beat = C.session_beat_age_s(cfg, at)
    out = []
    for j in jobs:
        if isinstance(j, dict) and C.ID_RE.match(str(j.get("id", ""))):
            try:
                out.append(describe(cfg, j, at, beat))
            except Exception as e:  # one broken record must not hide the others
                out.append({"id": j["id"], "error": str(e)})
    return {
        "api": C.API_VERSION, "chronos": C.CHRONOS_API_VERSION, "as_of": C.aware_iso(at), "tz": C.zone_name(),
        "session_runner": cfg["session_runner"], "approval_required": cfg["approval_required"],
        "paused_all": os.path.exists(cfg["pause_all"]),
        "session_beat_age_s": beat, "tick_age_s": _beat_age(cfg["beat_file"], at),
        "jobs_file": cfg["jobs_file"], "state_dir": cfg["state_dir"],
        "registry_synced_folder": _under(cfg["jobs_file"], SYNC_FOLDERS) or _under(cfg["state_dir"], SYNC_FOLDERS),
        "schedules": out,
    }


def detail(cfg, jid):
    job = next((j for j in W.read_jobs(cfg) if isinstance(j, dict) and j.get("id") == jid), None)
    if job is None:
        raise W.WriteError("not_found", "no such job")
    at = C.now()
    s = describe(cfg, job, at, C.session_beat_age_s(cfg, at))
    pb, gb = C._read_bytes(C.prompt_file(cfg, jid)), C._read_bytes(C.guard_file(cfg, jid))
    prompt, truncated = _trim_bytes(pb, DETAIL_PROMPT_MAX)
    guard = _trim_bytes(gb, DETAIL_GUARD_MAX)[0] if os.path.isfile(C.guard_file(cfg, jid)) else None
    nf = [C.aware_iso(x) for x in C.next_fires(job, at, 3)] if job.get("enabled") is True else []
    return {"schedule": s, "prompt": prompt, "prompt_truncated": truncated, "guard": guard, "next_runs": nf, "runs": []}


def approved(cfg, jid):
    if not C.ID_RE.match(jid or ""):
        raise W.WriteError("bad_request", "bad job id")
    return C.read_approved(cfg, jid)


def _pause(cfg, req):
    scope = req.get("scope")
    paused = req.get("paused")
    if not isinstance(paused, bool):
        raise W.WriteError("bad_request", "paused must be true or false")
    if scope == "all":
        os.makedirs(cfg["home_dir"], exist_ok=True)
        if paused:
            open(cfg["pause_all"], "a").close()
        else:
            try:
                os.remove(cfg["pause_all"])
            except FileNotFoundError:
                pass
        return {"ok": True, "scope": "all", "paused": os.path.exists(cfg["pause_all"]), "paused_all": os.path.exists(cfg["pause_all"])}
    jid = scope.get("id") if isinstance(scope, dict) else None
    if not isinstance(jid, str) or not C.ID_RE.match(jid):
        raise W.WriteError("bad_request", 'scope must be "all" or {"id": "<job id>"}')
    if not any(isinstance(j, dict) and j.get("id") == jid for j in W.read_jobs(cfg)):
        raise W.WriteError("not_found", "no such job")
    os.makedirs(cfg["paused_dir"], exist_ok=True)
    p = os.path.join(cfg["paused_dir"], jid)
    if paused:
        open(p, "a").close()
    else:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    return {"ok": True, "scope": jid, "paused": C.is_paused(cfg, jid), "paused_all": os.path.exists(cfg["pause_all"])}


def _parse_config_args(args, req):
    vals = {}
    if args and args[0] == "set":
        args = args[1:]
    if args:
        if len(args) % 2:
            raise W.WriteError("bad_request", "config set takes key value pairs")
        for k, v in zip(args[::2], args[1::2]):
            vals[k] = {"true": True, "false": False}.get(v, int(v) if v.isdigit() else v)
    vals.update(req or {})
    return vals


# --------------------------------------------------------------------------- migrate (interactive baseline for a 0.2 install)
def migrate(cfg, args, stdin, stderr):
    """The same steps `talos-app schedules adopt` runs: list, approve each unapproved job (y/N), offer to turn each `both` job
    into session-only (y/N), then switch to session_runner talos + approval_required only if no `both` job remains.
    Prompts go to stderr, answers come from stdin (end of input = N), the summary is JSON on stdout."""
    def ask(q):
        stderr.write(q + " [y/N] ")
        stderr.flush()
        line = stdin.readline()
        stderr.write("\n" if not line.endswith("\n") else "")
        return line.strip().lower() in ("y", "yes")

    jobs = W.read_jobs(cfg)
    summary = {"approved": [], "held": [], "converted": [], "switched": False, "blocking": []}
    stderr.write("Chronos jobs on this Mac:\n")
    for j in jobs:
        first = [l for l in C._read_bytes(C.prompt_file(cfg, j["id"])).decode("utf-8", "replace").splitlines()[:6]]
        guard = C._read_bytes(C.guard_file(cfg, j["id"])).decode("utf-8", "replace").splitlines()[:6]
        stderr.write("- %s (%s): runner=%s permissions=%s model=%s tools=%s %s\n    %s\n" % (
            j["id"], j.get("name") or j["id"], C.job_runner(j), C.job_permissions(j), j.get("model") or "default",
            ",".join(j.get("allowed_tools") or []) or "default", C.english(j) if C.clock_on(j) else "events", "\n    ".join(first)))
        if guard:
            stderr.write("    guard.md:\n    %s\n" % "\n    ".join(guard))
    for j in jobs:
        if not C.changed_outside(cfg, j):
            continue
        if ask('Approve "%s" as it is on disk now?' % (j.get("name") or j["id"])):
            W.approve_on_disk(cfg, j["id"], "migrate")
            summary["approved"].append(j["id"])
        else:
            summary["held"].append(j["id"])
    for j in W.read_jobs(cfg):
        if C.job_runner(j) != "both":
            continue
        if ask('Run "%s" in your live session only (no background backup)?' % (j.get("name") or j["id"])):
            old = j
            change = {"op": "update", "id": j["id"], "base_etag": C.job_etag(cfg, old), "set": {"runner": "session"}}
            was_approved = not C.changed_outside(cfg, old)
            with W.jobs_lock(cfg):
                jobs_now = W.read_jobs(cfg)
                normalized, plan = W.plan_change(cfg, change, jobs_now)
                W.execute(cfg, plan, jobs_now, "migrate") if was_approved else _convert_unapproved(cfg, plan, jobs_now)
            summary["converted"].append(j["id"])
    summary["blocking"] = W.both_jobs(cfg)
    if not summary["blocking"] and "--no-switch" not in args:
        try:
            W.set_config(cfg, {"session_runner": "talos", "approval_required": True})
            summary["switched"] = True
        except W.WriteError as e:
            summary["blocking"] = [f["path"] for f in e.fields]
    return summary


def _convert_unapproved(cfg, plan, jobs):
    """Convert a runner on a job the owner did NOT approve: write the change but leave it unapproved (so it stays held)."""
    W.write_jobs(cfg, [plan["new"] if j.get("id") == plan["id"] else j for j in jobs])


# --------------------------------------------------------------------------- dispatch
def _read_stdin(stdin):
    raw = stdin.read()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        raise W.WriteError("bad_request", "stdin is not valid JSON")
    return data


def run(argv, cfg, stdin=None, stdout=None, stderr=None):
    stdin, stdout, stderr = stdin or sys.stdin, stdout or sys.stdout, stderr or sys.stderr
    try:
        out = dispatch(argv, cfg, stdin, stderr)
    except W.WriteError as e:
        stdout.write(json.dumps(e.as_json(), indent=2, ensure_ascii=False) + "\n")
        return 1
    except Exception as e:  # never a stack trace on stdout
        stdout.write(json.dumps({"error": {"code": "internal", "message": str(e), "fields": []}}) + "\n")
        return 1
    stdout.write(json.dumps(out, indent=2, ensure_ascii=False) + "\n")
    return 0


def dispatch(argv, cfg, stdin, stderr):
    verb = argv[0] if argv else ""
    args = argv[1:]
    if verb == "version":
        return {"api": C.API_VERSION, "chronos": C.CHRONOS_API_VERSION, "session_runner": cfg["session_runner"], "approval_required": cfg["approval_required"]}
    if verb == "snapshot":
        return snapshot(cfg)
    if verb == "detail":
        jid = args[0] if args else (_read_stdin(stdin).get("id"))
        return detail(cfg, jid)
    if verb == "validate":
        return W.validate_change(cfg, _read_stdin(stdin))
    if verb == "apply":
        req = _read_stdin(stdin)
        if not isinstance(req, dict) or "change" not in req:
            raise W.WriteError("bad_request", 'apply takes {"change": {...}, "expected_etag": "sha256:..."|null}')
        via = "api_apply"
        return W.apply_change(cfg, req["change"], req.get("expected_etag"), via)
    if verb == "pause":
        return _pause(cfg, _read_stdin(stdin))
    if verb == "disable":
        jid = args[0] if args else _read_stdin(stdin).get("id")
        if not isinstance(jid, str) or not C.ID_RE.match(jid):
            raise W.WriteError("bad_request", "disable takes a job id")
        return W.disable(cfg, jid)
    if verb == "config":
        if not args or args[0] != "set":
            raise W.WriteError("bad_request", "usage: chronos api config set <key> <value> [<key> <value> ...]")
        req = _read_stdin(stdin) if not args[1:] else {}
        return {"ok": True, "set": W.set_config(cfg, _parse_config_args(args, req))}
    if verb == "due":
        if args[:2] != ["--for", "session"]:
            raise W.WriteError("bad_request", "usage: chronos api due --for session")
        beat = C.write_session_beat(cfg)
        return {"due": C.due_for_session(cfg), "beat": beat}
    if verb == "session-beat":
        return {"ok": True, "beat": C.write_session_beat(cfg)}
    if verb == "approved":
        jid = args[0] if args else _read_stdin(stdin).get("id")
        return approved(cfg, jid)
    if verb == "migrate":
        return migrate(cfg, args, stdin, stderr)
    raise W.WriteError("unknown_verb", "unknown api verb %r" % verb)
