"""Shared helpers for the Chronos 0.3 API tests (test_api_*.py). Python 3.9+, standard library only."""
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_chronos import Env, job, C, CLI, TICK, HOOK, SERVER, ROOT  # noqa: E402,F401

RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")
SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
NY = "America/New_York"


def api(e, args, payload=None, now="2026-10-05T09:00", tz=NY, expect=0, stdin=None, **extra):
    """Run `chronos api <args>`; return (parsed stdout JSON or None, completed process)."""
    env = e.env(CHRONOS_NOW=now, **extra)
    if tz:
        env["TZ"] = tz
    else:
        env.pop("TZ", None)
    data = stdin if stdin is not None else (json.dumps(payload) if payload is not None else "")
    r = subprocess.run([sys.executable, CLI, "api"] + list(args), env=env, input=data, capture_output=True, text=True, timeout=60)
    assert r.returncode == expect, "exit %s (wanted %s)\nstdout=%s\nstderr=%s" % (r.returncode, expect, r.stdout, r.stderr)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        out = None
    return out, r


def walk(x, path=""):
    """Yield (path, leaf) for every leaf of a JSON tree."""
    if isinstance(x, dict):
        for k, v in x.items():
            for p in walk(v, "%s.%s" % (path, k)):
                yield p
    elif isinstance(x, list):
        for i, v in enumerate(x):
            for p in walk(v, "%s[%d]" % (path, i)):
                yield p
    else:
        yield path, x


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha(b):
    return "sha256:" + hashlib.sha256(b).hexdigest()


def job_files(e, jid):
    cfg = e.cfg
    rec = next(j for j in json.load(open(cfg["jobs_file"])) if j["id"] == jid)
    p = os.path.join(cfg["jobs_dir"], jid, "prompt.md")
    g = os.path.join(cfg["jobs_dir"], jid, "guard.md")
    return rec, (open(p, "rb").read() if os.path.exists(p) else b""), (open(g, "rb").read() if os.path.exists(g) else b"")


def expected_etag(e, jid):
    rec, pb, gb = job_files(e, jid)
    rec = {k: v for k, v in rec.items() if k not in ("created", "updated")}
    return sha(canon(rec) + b"\0" + pb + b"\0" + gb)


def expected_content_hash(e, jid):
    rec, pb, gb = job_files(e, jid)
    sub = {"kind": rec.get("kind") or "claude", "command": rec.get("command"), "restricted": rec.get("restricted") is True,
           "allowed_tools": rec.get("allowed_tools"), "model": rec.get("model") or None,
           "in_session": rec.get("in_session") is True, "headless": rec.get("headless") is not False}
    return sha(canon(sub) + b"\0" + pb + b"\0" + gb)


def draft(jid="new-job", **kw):
    d = {"id": jid, "name": "New Job", "description": "a test job", "when": {"type": "weekdays", "time": "07:30"},
         "catchup_min": 180, "enabled": True, "runner": "session", "permissions": "full", "allowed_tools": [],
         "model": None, "notify": "failure", "prompt": "Do the thing.\n"}
    d.update(kw)
    return d


def create_change(jid="new-job", **kw):
    return {"op": "create", "draft": draft(jid, **kw)}


def validated(e, change, **kw):
    out, _ = api(e, ["validate"], change, **kw)
    return out


def apply_ok(e, change, **kw):
    """validate, then apply the NORMALIZED change with the etag validate promised. Returns apply's JSON."""
    v = validated(e, change, **kw)
    assert v["ok"] is True, v
    out, _ = api(e, ["apply"], {"change": v["normalized"], "expected_etag": v["expected_etag"]}, **kw)
    return out


def err(r):
    return json.loads(r.stdout)["error"]


def snap(e, **kw):
    out, _ = api(e, ["snapshot"], **kw)
    return out


def sched(e, jid, **kw):
    return next(s for s in snap(e, **kw)["schedules"] if s["id"] == jid)
