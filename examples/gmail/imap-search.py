#!/usr/bin/env python3
"""
Chronos Gmail adapter (example): search Gmail over IMAP and print the JSON the Gmail trigger expects.
Python 3.9+, standard library only. Read-only: it opens the mailbox with readonly=True and fetches with BODY.PEEK,
so it never marks anything read, moves or deletes mail.

    imap-search.py "<gmail search>" <max> [--body]

Set it up (full steps in docs/triggers.md):
  1. In your Google account turn on 2-step verification, then create an App Password (Security > App passwords).
  2. Make sure IMAP is enabled (Gmail settings > Forwarding and POP/IMAP).
  3. Put the credentials in ~/.config/chronos/gmail.env (chmod 600), NOT in the repo:
         GMAIL_ADDRESS=you@gmail.com
         GMAIL_APP_PASSWORD=xxxxxxxxxxxxxxxx
  4. In ~/.config/chronos/config.json set   "gmail_command": "/path/to/chronos/examples/gmail/imap-search.py"
     (the same file works under CHRONOS_GMAIL_ENV=/other/path.env)

Output contract (what chronos_triggers.py reads):
    {"emails": [{"id": "...", "from": "...", "to": "...", "subject": "...", "date": "...",
                 "hasAttachments": false, "attachmentNames": [], "body": "..."}]}
`id` is Gmail's stable message id (X-GM-MSGID). Newest messages come first. `body` is present only with --body and is a
best-effort plain-text excerpt (raw MIME text for multipart mail), capped at 4,000 characters.

Any other tool works too: the adapter is just a command that prints that JSON.
"""
import email
import email.header
import imaplib
import json
import os
import re
import sys

HOST = "imap.gmail.com"
FOLDER = "[Gmail]/All Mail"


def _decode(value):
    try:
        return str(email.header.make_header(email.header.decode_header(value or "")))
    except Exception:
        return value or ""


def _load_env():
    path = os.path.expanduser(os.environ.get("CHRONOS_GMAIL_ENV") or "~/.config/chronos/gmail.env")
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip().strip("'\"")
    except OSError:
        pass
    return out


def search(imap, query, limit=10, want_body=False):
    """Run the search on an open IMAP connection and return the list of email dicts, newest first."""
    typ, _ = imap.select('"%s"' % FOLDER, readonly=True)
    if typ != "OK":
        raise RuntimeError("could not open %s" % FOLDER)
    quoted = '"%s"' % query.replace("\\", "\\\\").replace('"', '\\"')
    typ, data = imap.uid("SEARCH", None, "X-GM-RAW", quoted)
    if typ != "OK":
        raise RuntimeError("search failed")
    uids = (data[0] or b"").split()[-limit:]
    emails = []
    for uid in reversed(uids):
        typ, d = imap.uid("FETCH", uid, "(X-GM-MSGID BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE CONTENT-TYPE)])")
        if typ != "OK" or not d or not isinstance(d[0], tuple):
            continue
        m = re.search(rb"X-GM-MSGID (\d+)", d[0][0])
        if not m:
            continue
        msg = email.message_from_bytes(d[0][1])
        item = {"id": m.group(1).decode(), "from": _decode(msg.get("From")), "to": _decode(msg.get("To")),
                "subject": _decode(msg.get("Subject")), "date": msg.get("Date") or "",
                "hasAttachments": "multipart/mixed" in (msg.get("Content-Type") or "").lower(), "attachmentNames": []}
        if want_body:
            typ, b = imap.uid("FETCH", uid, "(BODY.PEEK[TEXT]<0.20000>)")
            if typ == "OK" and b and isinstance(b[0], tuple):
                item["body"] = b[0][1].decode("utf-8", "replace")[:4000]
        emails.append(item)
    return emails


def main(argv):
    args = [a for a in argv if a != "--body"]
    if len(args) < 1:
        sys.stderr.write(__doc__)
        return 2
    query = args[0]
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
    env = _load_env()
    user = os.environ.get("GMAIL_ADDRESS") or env.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD") or env.get("GMAIL_APP_PASSWORD")
    if not user or not pw:
        sys.stderr.write("set GMAIL_ADDRESS and GMAIL_APP_PASSWORD (see the header of this file)\n")
        return 1
    try:
        imap = imaplib.IMAP4_SSL(HOST, timeout=30) if sys.version_info >= (3, 9) else imaplib.IMAP4_SSL(HOST)
        imap.login(user, pw)
        try:
            emails = search(imap, query, limit, "--body" in argv)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except Exception as e:
        sys.stderr.write("gmail adapter error: %s\n" % (str(e)[:200]))
        return 1
    print(json.dumps({"emails": emails}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
