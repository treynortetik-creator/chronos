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


def read_jobs(cfg):
    try:
        return C.load_jobs(cfg)
    except FileNotFoundError:
        return []


