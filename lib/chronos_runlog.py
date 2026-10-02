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
import sys
import tempfile
import time

import chronoslib as C


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def parse_stream(path):
    """-> dict(result, text, init_model, rate, lines, parsed). Tolerates junk lines and truncated output."""
    out = {"result": None, "text": "", "init_model": None, "rate": None, "lines": 0, "parsed": 0, "last_assistant": ""}
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
                    texts = [c.get("text", "") for c in (d.get("message") or {}).get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
                    if any(texts):
                        out["last_assistant"] = "\n".join(x for x in texts if x)
                except Exception:
                    pass
    return out


def empty_stream():
    return {"result": None, "text": "", "init_model": None, "rate": None, "lines": 0, "parsed": 0, "last_assistant": ""}


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


def write_log(a, ps, raw_text):
    res = ps["result"]
    if res is not None:
        body = str(res.get("result") or "")
        if res.get("is_error"):
            body = "[claude reported an error: %s]\n%s" % (res.get("subtype") or "error", body)
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
    if a.kind == "event":
        ok = (a.rc == 0 and not a.timed_out and ps["result"] is not None and not ps["result"].get("is_error"))
        if ps["result"] is None and ps["parsed"] == 0 and a.rc == 0 and not a.timed_out:
            ok = True            # claude printed plain text (older CLI, or a stub): a clean exit is still a success
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
