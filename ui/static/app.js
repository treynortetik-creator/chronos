/* Chronos UI front end. Vanilla JS, no build. All dynamic text goes through textContent (never innerHTML). */
(function () {
  "use strict";
  var TOKEN = (document.querySelector('meta[name="chronos-token"]') || {}).content || "";
  var app = document.getElementById("app");
  var timers = [];          // intervals owned by the current view
  var state = { jobs: [], status: null };
  var DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  var DAYS_LONG = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  // ------------------------------------------------------------ helpers
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else if (k === "value") el.value = v;
      else if (k === "checked") el.checked = !!v;
      else el.setAttribute(k, v === true ? "" : v);
    });
    for (var i = 2; i < arguments.length; i++) add(el, arguments[i]);
    return el;
  }
  function add(el, c) {
    if (c === null || c === undefined || c === false) return;
    if (Array.isArray(c)) c.forEach(function (x) { add(el, x); });
    else if (c.nodeType) el.appendChild(c);
    else el.appendChild(document.createTextNode(String(c)));
  }
  function ic(name, extra) {
    var s = document.createElement("span");
    s.innerHTML = '<svg class="ic ' + (extra || "") + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + ((window.ICONS || {})[name] || "") + "</svg>";
    return s.firstChild;
  }
  function btn(label, icon, cls, onclick, attrs) {
    var b = h("button", Object.assign({ type: "button", class: "btn " + (cls || ""), onclick: onclick }, attrs || {}));
    if (icon) b.appendChild(ic(icon));
    b.appendChild(document.createTextNode(label));
    return b;
  }
  function parseLocal(s) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?/.exec(s || "");
    return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +(m[6] || 0)) : null;
  }
  function fmtWhen(d) {
    return d.toLocaleString("en-US", { weekday: "short", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  }
  function fmtCount(sec) {
    if (sec < 0) sec = 0;
    var d = Math.floor(sec / 86400), hh = Math.floor((sec % 86400) / 3600), mm = Math.floor((sec % 3600) / 60), ss = sec % 60;
    if (d > 0) return d + "d " + hh + "h " + mm + "m";
    if (hh > 0) return hh + "h " + mm + "m " + ("0" + ss).slice(-2) + "s";
    return mm + "m " + ("0" + ss).slice(-2) + "s";
  }
  function fmtBytes(n) { return n < 1024 ? n + " B" : n < 1048576 ? (n / 1024).toFixed(1) + " KB" : (n / 1048576).toFixed(1) + " MB"; }
  function fmtAge(s) { if (s === null || s === undefined) return "never"; if (s < 90) return s + "s ago"; if (s < 5400) return Math.round(s / 60) + "m ago"; return Math.round(s / 3600) + "h ago"; }

  function toast(msg, kind) {
    var t = h("div", { class: "toast " + (kind || ""), text: msg });
    document.getElementById("toasts").appendChild(t);
    setTimeout(function () { t.remove(); }, kind === "bad" ? 9000 : 4500);
  }
  function api(method, url, body) {
    var opt = { method: method, headers: { "X-Chronos-Token": TOKEN } };
    if (method !== "GET") opt.headers["Content-Type"] = "application/json";
    if (body !== undefined) opt.body = JSON.stringify(body);
    return fetch(url, opt).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok) { var e = new Error(j.error || ("HTTP " + r.status)); e.status = r.status; e.data = j; throw e; }
        return j;
      });
    });
  }
  function fail(e) { toast(e.message || String(e), "bad"); }

  function confirmDlg(title, text, okLabel, danger) {
    return new Promise(function (resolve) {
      var dlg = document.getElementById("dlg");
      dlg.className = "";
      dlg.textContent = "";
      var done = function (v) { dlg.close(); resolve(v); };
      dlg.appendChild(h("h2", { text: title }));
      dlg.appendChild(h("p", { text: text }));
      dlg.appendChild(h("div", { class: "btns" },
        btn("Cancel", null, "", function () { done(false); }),
        btn(okLabel || "OK", null, danger ? "danger" : "primary", function () { done(true); })));
      dlg.oncancel = function () { resolve(false); };
      dlg.showModal();
    });
  }
  function textDlg(title, text) {
    var dlg = document.getElementById("dlg");
    dlg.className = "wide";
    dlg.textContent = "";
    dlg.appendChild(h("h2", { text: title }));
    dlg.appendChild(h("pre", { class: "pre", text: text }));
    dlg.appendChild(h("div", { class: "btns", style: "margin-top:14px" }, btn("Close", "x", "", function () { dlg.close(); })));
    dlg.showModal();
  }

  function clearTimers() { timers.forEach(clearInterval); timers = []; }
  function every(ms, fn) { timers.push(setInterval(fn, ms)); }
  function setNav(k) { document.querySelectorAll(".topnav a").forEach(function (a) { a.classList.toggle("on", a.dataset.nav === k); }); }

  // ------------------------------------------------------------ chrome (masthead)
  function drawTicks(id, r, n, bigEvery, len, bigLen) {
    var g = document.getElementById(id), out = "";
    for (var i = 0; i < n; i++) {
      var a = (i / n) * Math.PI * 2, big = i % bigEvery === 0, l = big ? bigLen : len;
      out += '<line class="tk' + (big ? " big" : "") + '" x1="' + (Math.cos(a) * r).toFixed(2) + '" y1="' + (Math.sin(a) * r).toFixed(2) + '" x2="' + (Math.cos(a) * (r - l)).toFixed(2) + '" y2="' + (Math.sin(a) * (r - l)).toFixed(2) + '"/>';
    }
    g.innerHTML = out;
  }
  function drawStars() {
    var c = document.getElementById("stars"), dpr = Math.min(window.devicePixelRatio || 1, 2);
    var w = window.innerWidth, hgt = window.innerHeight;
    c.width = w * dpr; c.height = hgt * dpr;
    var x = c.getContext("2d"); x.scale(dpr, dpr); x.clearRect(0, 0, w, hgt);
    var seed = 7; function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
    var n = Math.floor((w * hgt) / 5200);
    for (var i = 0; i < n; i++) {
      var px = rnd() * w, py = rnd() * hgt, r = rnd() < 0.06 ? 1.3 : rnd() * 0.7 + 0.25, a = rnd() * 0.55 + 0.12;
      x.beginPath(); x.arc(px, py, r, 0, 6.2832);
      x.fillStyle = rnd() < 0.22 ? "rgba(241,213,143," + a + ")" : "rgba(236,232,220," + a + ")";
      x.fill();
    }
  }
  function renderChrome() {
    var s = state.status; if (!s) return;
    var pa = document.getElementById("pause-all");
    pa.textContent = ""; pa.classList.toggle("pressed", !!s.paused_all);
    pa.setAttribute("aria-pressed", s.paused_all ? "true" : "false");
    pa.appendChild(ic(s.paused_all ? "player-play" : "player-pause"));
    pa.appendChild(document.createTextNode(s.paused_all ? "Resume all" : "Pause all"));
    var beat = document.getElementById("beat");
    beat.textContent = fmtAge(s.beat_age_s);
    document.getElementById("beat-box").classList.toggle("warn", s.beat_age_s === null || s.beat_age_s > 780);
  }
  function tickClock() {
    var d = new Date();
    document.getElementById("now-clock").textContent = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  }
  document.getElementById("pause-all").addEventListener("click", function () {
    var want = !(state.status && state.status.paused_all);
    var go = want ? confirmDlg("Pause everything?", "Chronos will stop starting any job until you resume. A job already running finishes. In-session timers in an interactive Claude Code session are not affected.", "Pause all", true) : Promise.resolve(true);
    go.then(function (ok) {
      if (!ok) return;
      api("POST", "/api/pause-all", { paused: want }).then(function () { toast(want ? "Chronos paused." : "Chronos resumed.", "ok"); refreshStatus(); }).catch(fail);
    });
  });
  function refreshStatus() {
    return api("GET", "/api/status").then(function (s) { state.status = s; renderChrome(); }).catch(function () {});
  }

  // ------------------------------------------------------------ dashboard
  var STATUS_LABEL = { ran: "Ran", failed: "Failed", missed: "Missed", "not-due": "Not due", pending: "Upcoming", running: "Running" };
  function dotsFor(job) {
    var wrap = h("div", { class: "strip", role: "img", "aria-label": "Last 14 days: " + job.strip.map(function (s) { return s.date + " " + (STATUS_LABEL[s.status] || s.status); }).join(", ") });
    job.strip.forEach(function (s) { wrap.appendChild(h("span", { class: "dot " + s.status, title: s.date + ": " + (STATUS_LABEL[s.status] || s.status) })); });
    return wrap;
  }
  function legend() {
    var items = [["ran", "Ran"], ["failed", "Failed"], ["missed", "Missed"], ["running", "Running"], ["pending", "Upcoming today"], ["not-due", "Not due"]];
    return h("div", { class: "legend", "aria-label": "Legend" }, items.map(function (i) { return h("span", null, h("span", { class: "dot " + i[0] }), i[1]); }));
  }
  function stateChips(j) {
    var c = [];
    if (j.running) c.push(h("span", { class: "chip run", text: "Running" }));
    else if (j.today === "ran") c.push(h("span", { class: "chip ok", text: "Done today" }));
    else if (j.today === "failed") c.push(h("span", { class: "chip bad", text: "Failed today" }));
    else if (j.today === "missed") c.push(h("span", { class: "chip warn", text: "Missed today" }));
    if (!j.enabled) c.push(h("span", { class: "chip dim", text: "Disabled" }));
    if (j.paused) c.push(h("span", { class: "chip warn", text: "Paused" }));
    if (j.once) c.push(h("span", { class: "chip", text: "One-shot" }));
    return c;
  }
  function jobCard(j) {
    var cd = h("span", { class: "count", "data-next": j.next_fire || "" });
    var nextTxt = j.next_fire ? fmtWhen(parseLocal(j.next_fire)) : (j.enabled ? "no future run" : "disabled");
    var logs = [];
    if (j.latest_report) logs.push(h("a", { href: "#/view/report/" + j.latest_report.name }, ic("file-text"), "Latest report"));
    if (j.latest_log) logs.push(h("a", { href: "#/view/log/" + j.latest_log.name }, ic("terminal-2"), "Latest log"));
    if (!logs.length) logs.push(h("span", { class: "muted", text: "No runs recorded yet" }));
    var card = h("article", { class: "card" + (j.enabled ? "" : " off"), "data-id": j.id },
      h("div", { class: "card-top" },
        h("h2", null, h("a", { href: "#/job/" + j.id, text: j.name })),
        h("div", { class: "chips" }, stateChips(j))),
      h("p", { class: "desc", text: j.description || "" }),
      h("div", { class: "sched" },
        h("div", { class: "when" }, ic("clock"), j.schedule_text),
        h("div", { class: "nxt" }, h("span", null, j.clock === false ? "No clock run; starts on events" : "Next: " + nextTxt), j.next_fire ? cd : null),
        (j.triggers && j.triggers.length) ? h("div", { class: "nxt" }, h("span", null, ic("broadcast"), " " + j.triggers.map(function (t) { return TRIG_LABEL[t.type] || t.type; }).join(", ") + (j.fires_last_hour ? " (" + j.fires_last_hour + " fired in the last hour)" : ""))) : null,
        h("div", { class: "chips trustrow" }, trustChips(j))),
      h("div", { class: "strip-wrap" },
        h("div", { class: "cap" }, h("span", { text: "Last 14 days" }), h("span", { text: "today at right" })),
        dotsFor(j)),
      h("div", { class: "usageline" }, ic("chart-bar"), usageLine(j.usage)),
      h("div", { class: "card-foot" },
        h("div", { class: "left links" }, logs),
        h("div", { class: "right" },
          btn(j.paused ? "Resume" : "Pause", j.paused ? "player-play" : "player-pause", "sm", function () { togglePause(j); }),
          btn(j.running ? "Running" : "Run now", "bolt", "sm primary", function () { runNow(j); }, j.running ? { disabled: "" } : {}))));
    return card;
  }
  function togglePause(j) {
    api("POST", "/api/jobs/" + j.id + "/pause", { paused: !j.paused })
      .then(function () { toast((j.paused ? "Resumed " : "Paused ") + j.name + ".", "ok"); route(true); }).catch(fail);
  }
  function runNow(j) {
    var go = function (again) {
      return api("POST", "/api/jobs/" + j.id + "/run", { again: again }).then(function () {
        toast("Started " + j.name + ". It takes a few minutes; this page will show it running.", "ok"); route(true);
      });
    };
    confirmDlg("Run " + j.name + " now?", "This starts a real headless Claude run in the background. It uses your Claude Code login, so it counts against your usage.", "Run now").then(function (ok) {
      if (!ok) return;
      go(false).catch(function (e) {
        if (e.status === 409 && e.data && e.data.need_confirm) {
          confirmDlg("Already ran today", "Run it again? Today's finished marker for this job is removed first, so the job does its work a second time.", "Run again", true).then(function (ok2) { if (ok2) go(true).catch(fail); });
        } else fail(e);
      });
    });
  }

  function viewDashboard() {
    setNav("dash");
    app.textContent = "";
    var grid = h("div", { class: "grid" });
    var top = h("div", { class: "pagehead" },
      h("div", null, h("h1", { text: "The schedule" }), h("p", { class: "lede", text: "Every job Chronos keeps, and whether time kept faith with it." })),
      h("div", null, btn("New job", "plus", "primary", function () { location.hash = "#/new"; })));
    var banner = h("div");
    app.appendChild(top); app.appendChild(banner); app.appendChild(legend()); app.appendChild(grid);

    function paint(d) {
      state.jobs = d.jobs; state.status = d.status; renderChrome();
      banner.textContent = "";
      if (d.status.paused_all) banner.appendChild(h("div", { class: "banner" }, ic("alert-triangle"), h("span", { text: "Chronos is paused. Nothing will start on its own until you press Resume all." })));
      else if (d.status.beat_age_s === null || d.status.beat_age_s > 780) banner.appendChild(h("div", { class: "banner" }, ic("alert-triangle"), h("span", { text: "The launchd tick has not run for a while (" + fmtAge(d.status.beat_age_s) + "). Jobs may not start on their own." })));
      grid.textContent = "";
      if (!d.jobs.length) grid.appendChild(h("div", { class: "empty", text: "No jobs yet." }));
      d.jobs.forEach(function (j) { grid.appendChild(jobCard(j)); });
      tickCountdowns();
    }
    function load() { return api("GET", "/api/jobs").then(paint).catch(function (e) { grid.textContent = ""; grid.appendChild(h("div", { class: "empty", text: "Could not load jobs: " + e.message })); }); }
    function tickCountdowns() {
      document.querySelectorAll(".count[data-next]").forEach(function (el) {
        var d = parseLocal(el.dataset.next); if (!d) return;
        var s = Math.round((d.getTime() - Date.now()) / 1000);
        el.textContent = s > 0 ? "in " + fmtCount(s) : "due now";
      });
    }
    load();
    every(1000, tickCountdowns);
    every(10000, function () { load(); });
  }

  function notifySelect(v) {
    var sel = h("select", { "aria-label": "When to send a notification" },
      [["failure", "On failure only"], ["always", "On success and failure"], ["never", "Never"]].map(function (o) { return h("option", { value: o[0], text: o[1] }); }));
    sel.value = v || "failure";
    return sel;
  }

  // ------------------------------------------------------------ schedule widget (shared by detail + new)
  function scheduleWidget(init, opts) {
    opts = opts || {};
    var once = !!opts.once;
    var kind = "daily", dow = {}, dom = "1,15";
    var d = init.days || "daily";
    if (d === "daily" || d === "weekdays") kind = d;
    else if (d.indexOf("dom:") === 0) { kind = "dom"; dom = d.slice(4); }
    else { kind = "dow"; d.split(",").forEach(function (x) { dow[x.trim()] = true; }); }

    var timeIn = h("input", { type: "time", value: init.time || "08:00", required: "", "aria-label": "Time of day" });
    var onceIn = h("input", { type: "datetime-local", value: init.once || "", "aria-label": "One-shot date and time" });
    var catchIn = h("input", { type: "number", min: "15", max: "1440", step: "5", value: String(init.catchup_min || (once ? 120 : 180)), "aria-label": "Catch-up window in minutes" });
    var kindSel = h("select", { "aria-label": "Which days" },
      [["daily", "Every day"], ["weekdays", "Weekdays (Mon to Fri)"], ["dow", "Specific weekdays"], ["dom", "Days of the month"]].map(function (o) { return h("option", { value: o[0], text: o[1] }); }));
    kindSel.value = kind;
    var domIn = h("input", { type: "text", value: dom, placeholder: "1,15", "aria-label": "Days of the month, comma separated" });
    var dowBox = h("div", { class: "checks", role: "group", "aria-label": "Weekdays" }, DAYS.map(function (n, i) {
      var cb = h("input", { type: "checkbox", value: n, checked: !!dow[n] });
      cb.addEventListener("change", changed);
      return h("label", null, cb, DAYS_LONG[i]);
    }));
    var preview = h("div", { class: "preview", "aria-live": "polite" }, h("span", { class: "muted", text: "Working out the next fire times..." }));
    var daysRow = h("label", { class: "f span2" }, "Which days", kindSel, dowBox, domIn);
    function vis() { dowBox.style.display = kindSel.value === "dow" ? "" : "none"; domIn.style.display = kindSel.value === "dom" ? "" : "none"; }
    function value() {
      if (once) return { once: onceIn.value, catchup_min: +catchIn.value };
      var days = kindSel.value;
      if (days === "dow") days = Array.prototype.filter.call(dowBox.querySelectorAll("input"), function (c) { return c.checked; }).map(function (c) { return c.value; }).join(",");
      if (days === "dom") days = "dom:" + domIn.value.replace(/\s+/g, "");
      return { time: timeIn.value, days: days, once: null, catchup_min: +catchIn.value };
    }
    var tmr = null;
    function changed() {
      vis(); clearTimeout(tmr);
      tmr = setTimeout(function () {
        var v = value(), q = new URLSearchParams({ time: v.time || "", days: v.days || "", once: v.once || "", catchup_min: String(v.catchup_min || "") });
        api("GET", "/api/preview?" + q.toString()).then(function (r) {
          preview.textContent = "";
          if (r.errors && r.errors.length) { preview.appendChild(h("div", { class: "err", text: r.errors.join(" ") })); return; }
          preview.appendChild(h("div", { class: "eng", text: r.text }));
          preview.appendChild(h("div", { class: "muted", text: "Next fire times" }));
          preview.appendChild(h("ol", null, r.next.map(function (t) { return h("li", { text: fmtWhen(parseLocal(t)) }); })));
        }).catch(function () {});
        if (opts.onchange) opts.onchange();
      }, 250);
    }
    [timeIn, onceIn, catchIn, kindSel, domIn].forEach(function (e) { e.addEventListener("input", changed); e.addEventListener("change", changed); });
    var catchRow = h("label", { class: "f" }, "Catch-up window (minutes)", catchIn, h("span", { class: "hintline", text: "If the machine was asleep at fire time, Chronos still runs the job up to this long after." }));
    var grid = once
      ? h("div", { class: "formgrid" }, h("label", { class: "f" }, "Date and time", onceIn), catchRow)
      : h("div", { class: "formgrid" }, h("label", { class: "f" }, "Time of day", timeIn), catchRow, daysRow);
    var root = h("div", null, grid, h("div", { style: "margin-top:14px" }, preview));
    vis(); changed();
    return { el: root, value: value };
  }

  // ------------------------------------------------------------ job detail
  function viewJob(jid, tab) {
    setNav("dash");
    app.textContent = "";
    app.appendChild(h("div", { class: "loading", text: "Reading the scrolls..." }));
    api("GET", "/api/jobs/" + jid).then(function (j) { renderJob(j, tab || "work"); }).catch(function (e) {
      app.textContent = ""; app.appendChild(h("div", { class: "empty", text: "Could not open that job: " + e.message }));
    });
  }
  var dirtyGuard = null;
  function renderJob(j, tab) {
    clearTimers(); dirtyGuard = null;
    app.textContent = "";
    var head = h("div", { class: "pagehead" },
      h("div", null,
        h("a", { class: "back", href: "#/" }, ic("arrow-left"), "All jobs"),
        h("h1", { text: j.name }),
        h("p", { class: "lede" }, j.schedule_text, " ", h("span", { class: "chips", style: "display:inline-flex;margin-left:8px" }, stateChips(j))),
        h("div", { class: "chips trustrow" }, trustChips(j))),
      h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" },
        btn(j.paused ? "Resume" : "Pause", j.paused ? "player-play" : "player-pause", "", function () { togglePauseDetail(j); }),
        btn(j.running ? "Running" : "Run now", "bolt", "primary", function () { runNow(j); }, j.running ? { disabled: "" } : {})));
    var tabs = [["work", "Prompt", "edit"], ["schedule", "Schedule", "calendar-time"], ["triggers", "Triggers", "broadcast"], ["model", "Model", "cpu"], ["guard", "Guardrails", "lock"], ["history", "History", "history"], ["runs", "Runs", "terminal-2"]];
    var isCmd = j.kind === "command";
    if (isCmd) {          // a command job has no prompt, triggers, model or guard file: only the command, the schedule and the runs
      tabs = [["work", "Command", "terminal-2"], ["schedule", "Schedule", "calendar-time"], ["runs", "Runs", "terminal-2"]];
      if (!/^(work|schedule|runs)$/.test(tab)) tab = "work";
    }
    var body = h("div");
    var bar = h("div", { class: "tabs", role: "tablist" }, tabs.map(function (t) {
      return h("button", { type: "button", role: "tab", class: t[0] === tab ? "on" : "", onclick: function () { go(t[0]); } }, ic(t[2]), t[1]);
    }));
    function go(t) {
      if (dirtyGuard && dirtyGuard() && !window.confirm("You have unsaved changes. Leave this tab and discard them?")) return;
      history.replaceState(null, "", "#/job/" + j.id + "/" + t);
      renderJob(j, t);
    }
    app.appendChild(head); app.appendChild(strip14(j)); app.appendChild(bar); app.appendChild(body);
    ({ work: tabWork, schedule: tabSchedule, triggers: tabTriggers, model: tabModel, guard: tabGuard, history: tabHistory, runs: tabRuns }[tab] || tabWork)(j, body);
    if (j.running) every(4000, function () { api("GET", "/api/jobs/" + j.id).then(function (n) { if (!n.running) { toast(j.name + " finished.", "ok"); viewJob(j.id, tab); } }).catch(function () {}); });
  }
  function togglePauseDetail(j) {
    api("POST", "/api/jobs/" + j.id + "/pause", { paused: !j.paused }).then(function () { toast(j.paused ? "Resumed." : "Paused.", "ok"); viewJob(j.id, currentTab()); }).catch(fail);
  }
  function currentTab() { var m = /^#\/job\/[^/]+\/([a-z]+)/.exec(location.hash); return m ? m[1] : "work"; }
  function strip14(j) { return h("div", { class: "panel" }, h("div", { class: "strip-wrap" }, h("div", { class: "cap" }, h("span", { text: "Last 14 days" }), h("span", { text: "today at right" })), dotsFor(j), legend())); }

  function tabCommand(j, body) {
    body.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { text: "The command" })),
      h("p", { class: "hint", text: "This job runs a plain shell command on its schedule. It does not use Claude and spends no tokens. The command lives in jobs.json and is not editable here on purpose: change it in that file. Output goes to the run log; exit 0 counts as done." }),
      h("pre", { class: "mono", style: "white-space:pre-wrap;word-break:break-all;margin:0", text: j.command || "" }),
      h("div", { class: "savebar" }, h("span", { class: "grow" }), btn("Delete job", "x", "danger sm", function () { deleteJob(j); }))));
  }
  function tabWork(j, body) {
    if (j.kind === "command") return tabCommand(j, body);
    var ta = h("textarea", { spellcheck: "false", "aria-label": "prompt.md", value: j.prompt_md });
    var base = j.prompt_md, baseSha = j.prompt_sha;
    var dirtyEl = h("span", { class: "dirty" });
    var save = btn("Save prompt", "device-floppy", "primary", doSave, { disabled: "" });
    function upd() { var d = ta.value !== base; save.disabled = !d; dirtyEl.textContent = d ? "Unsaved changes" : ""; count.textContent = ta.value.length.toLocaleString() + " characters"; }
    var count = h("span", { class: "muted mono" });
    ta.addEventListener("input", upd);
    ta.addEventListener("keydown", function (e) { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); if (!save.disabled) doSave(); } });
    dirtyGuard = function () { return ta.value !== base; };
    function doSave() {
      save.disabled = true;
      api("POST", "/api/jobs/" + j.id + "/prompt", { content: ta.value, base_sha: baseSha }).then(function (r) {
        base = ta.value; baseSha = r.sha; upd(); toast(r.saved ? "Saved. The previous version is in History." : "No changes to save.", "ok");
      }).catch(function (e) { fail(e); upd(); });
    }
    body.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { text: "The prompt" }), count),
      h("p", { class: "hint", text: "This is prompt.md, the instructions Claude receives each run (a clock run, or an event run that reads it as trusted instructions). Changes apply from the next run. Cmd+S saves. Every save keeps the old version." }),
      ta,
      h("div", { class: "savebar" }, save, dirtyEl, h("span", { class: "grow" }), btn("Delete job", "x", "danger sm", function () { deleteJob(j); }))));
    upd();
  }
  function deleteJob(j) {
    confirmDlg("Delete " + j.name + "?", "The job folder and its schedule entry are removed. A copy of prompt.md (and guard.md, if any) is archived under history/deleted/ in the Chronos home folder.", "Delete", true).then(function (ok) {
      if (ok) api("DELETE", "/api/jobs/" + j.id).then(function () { toast("Deleted.", "ok"); location.hash = "#/"; }).catch(fail);
    });
  }

  function tabSchedule(j, body) {
    var nameIn = h("input", { type: "text", value: j.name, maxlength: "60", "aria-label": "Name" });
    var descIn = h("input", { type: "text", value: j.description || "", maxlength: "300", "aria-label": "Description" });
    var en = h("input", { type: "checkbox", checked: !!j.enabled });
    var ins = h("input", { type: "checkbox", checked: !!j.in_session, disabled: (j.once || j.clock === false || j.kind === "command") ? "" : null });
    var notifySel = notifySelect(j.notify);
    var eventOnly = j.clock === false;
    var w = eventOnly ? null : scheduleWidget(j, { once: !!j.once, onchange: function () { save.disabled = false; } });
    var save = btn("Save schedule", "device-floppy", "primary", doSave, { disabled: "" });
    [nameIn, descIn, en, ins, notifySel].forEach(function (e) { e.addEventListener("input", function () { save.disabled = false; }); e.addEventListener("change", function () { save.disabled = false; }); });
    dirtyGuard = function () { return !save.disabled; };
    function doSave() {
      var v = w ? w.value() : {};
      var payload = Object.assign({ name: nameIn.value, description: descIn.value, enabled: en.checked, in_session: ins.checked, notify: notifySel.value }, v);
      api("POST", "/api/jobs/" + j.id + "/schedule", payload).then(function (r) {
        toast(r.changed ? "Schedule saved." + (r.warning ? " Note: " + r.warning : "") : "Nothing changed.", r.warning ? "bad" : "ok");
        dirtyGuard = null; viewJob(j.id, "schedule");
      }).catch(fail);
    }
    body.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { text: "When it runs" })),
      h("p", { class: "hint", text: "Chronos reads jobs.json every five minutes, so a saved change takes effect at the next tick. Times are this machine's local time (" + ((state.status && state.status.tz) || "system timezone") + ")." }),
      h("div", { class: "formgrid", style: "margin-bottom:16px" },
        h("label", { class: "f" }, "Name", nameIn),
        h("label", { class: "f" }, "Description", descIn)),
      eventOnly ? h("p", { class: "hint", text: "This job has no clock schedule: it starts only on its triggers. Turn the clock back on in the Triggers tab to schedule it." }) : w.el,
      h("div", { style: "display:flex;gap:28px;flex-wrap:wrap;margin-top:18px;align-items:center" },
        h("label", { class: "toggle" }, en, "Enabled"),
        h("label", { class: "toggle", title: "Needs the optional SessionStart hook. When on, an interactive Claude Code session also arms its own timer for this job at startup. Chronos stays the backstop." }, ins, "Also arm in my interactive session"),
        h("label", { class: "f", style: "flex-direction:row;align-items:center;gap:10px" }, "Notify", notifySel)),
      h("div", { class: "savebar" }, save, h("span", { class: "muted", text: "Saving also keeps the old schedule in History." }))));
  }

  function tabGuard(j, body) {
    body.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("span", { class: "locked" }, ic("lock"), "Locked guardrails"), h("span", { class: "muted mono", text: "read only" })),
      h("p", { class: "hint", text: "What Chronos puts in front of your prompt on every run: the done-marker contract and, if the job has a guard.md file, its standing rules. Not editable here on purpose." }),
      h("pre", { class: "pre", text: j.guard_md })));
  }

  function tabHistory(j, body) {
    var hs = j.history;
    function restoreBtn(kind, f) {
      return btn("Restore", "restore", "sm", function () {
        confirmDlg("Restore this version?", "The current version is saved to History first, so you can undo this.", "Restore").then(function (ok) {
          if (!ok) return;
          api("POST", "/api/jobs/" + j.id + "/restore", { kind: kind, file: f }).then(function () { toast("Restored.", "ok"); viewJob(j.id, "history"); }).catch(fail);
        });
      });
    }
    function viewBtn(f) { return btn("View", "eye", "sm", function () { fetch("/api/history?job=" + j.id + "&file=" + encodeURIComponent(f)).then(function (r) { return r.text(); }).then(function (t) { textDlg(f, t); }); }); }
    var runRows = hs.prompt.map(function (r) { return h("tr", null, h("td", { class: "mono", text: r.ts.replace("T", " ") }), h("td", { class: "hide-sm", text: fmtBytes(r.size) }), h("td", null, h("div", { class: "actions" }, viewBtn(r.file), restoreBtn("prompt", r.file)))); });
    var schRows = hs.jobs.map(function (r) { return h("tr", null, h("td", { class: "mono", text: r.ts.replace("T", " ") }), h("td", { text: r.summary }), h("td", null, h("div", { class: "actions" }, restoreBtn("schedule", r.file)))); });
    body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Prompt versions" })),
      runRows.length ? h("div", { class: "scroll" }, h("table", { class: "list" }, h("thead", null, h("tr", null, h("th", { text: "Saved over at" }), h("th", { class: "hide-sm", text: "Size" }), h("th"))), h("tbody", null, runRows))) : h("div", { class: "empty", text: "No earlier versions yet. The first save creates one." })));
    body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Schedule versions" })),
      schRows.length ? h("div", { class: "scroll" }, h("table", { class: "list" }, h("thead", null, h("tr", null, h("th", { text: "Snapshot taken" }), h("th", { text: "Schedule at that time" }), h("th"))), h("tbody", null, schRows))) : h("div", { class: "empty", text: "No earlier schedules yet." })));
  }

  function tabRuns(j, body) {
    var rows = j.runs.map(function (r) {
      return h("tr", null, h("td", { class: "mono", text: r.date + (r.time ? " " + r.time : "") + (r.event ? " event" : "") }), h("td", { text: r.kind === "log" ? "Log" : "Report" }), h("td", { class: "hide-sm mono", text: fmtBytes(r.size) }),
        h("td", null, h("div", { class: "actions" }, h("a", { class: "btn sm", href: "#/view/" + r.kind + "/" + r.name }, ic("eye"), "Open"))));
    });
    body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Runs of this job" })),
      rows.length ? h("div", { class: "scroll" }, h("table", { class: "list" }, h("thead", null, h("tr", null, h("th", { text: "When" }), h("th", { text: "Kind" }), h("th", { class: "hide-sm", text: "Size" }), h("th"))), h("tbody", null, rows))) : h("div", { class: "empty", text: "No logs or reports yet." })));
  }

  // ------------------------------------------------------------ v4: trust chips, triggers, model
  function trustChips(j) {
    var c = [];
    if (j.kind === "command") return [h("span", { class: "chip", title: "Runs a plain shell command: no Claude, no tokens.", text: "Plain command" })];
    if (j.restricted) c.push(h("span", { class: "chip ok", title: "Scheduled runs get only this job's allowed tools (--permission-mode=default); they never skip permissions.", text: "Restricted: schedule" }));
    else if (j.trust && j.trust.schedule) c.push(h("span", { class: "chip ok", title: "Clock-schedule runs follow your own instructions and run with full permissions.", text: "Trusted: schedule" }));
    if (j.trust && j.trust.event) c.push(h("span", { class: "chip warn", title: "Event runs read text from outside (email, pull requests, files, webhooks). They get a narrow tool list and never skip permissions.", text: "Untrusted: event" }));
    if (j.model) c.push(h("span", { class: "chip", title: "Per-job model override", text: "Model: " + j.model }));
    return c;
  }
  var TRIG_LABEL = { file: "File", gmail: "Gmail", github: "GitHub", webhook: "Webhook" };
  function trigSummary(t) {
    if (t.type === "file") return t.path + "   matching " + (t.glob || "*");
    if (t.type === "gmail") return t.query + (t.body ? "   (with message body)" : "");
    if (t.type === "github") return t.repo + "   new " + t.event + "s";
    return "POST /hook/<job id>, header X-Chronos-Secret";
  }
  function tabTriggers(j, body) {
    var list = (j.triggers || []).map(function (t) { return Object.assign({}, t); });
    var clock = j.clock !== false;
    var defText = (j.default_allowed_tools || []).join("\n");
    var allowedIn = h("textarea", { spellcheck: "false", "aria-label": "Allowed tools for event runs", style: "min-height:120px", value: (j.allowed_tools || j.default_allowed_tools || []).join("\n") });
    var clockCb = h("input", { type: "checkbox", checked: clock });
    var save = btn("Save triggers", "device-floppy", "primary", doSave, { disabled: "" });
    var listEl = h("div", { class: "trows" });
    var dirty = false;
    function touch() { dirty = true; save.disabled = false; }
    dirtyGuard = function () { return dirty; };
    clockCb.addEventListener("change", function () { clock = clockCb.checked; touch(); });
    allowedIn.addEventListener("input", touch);

    function paintList() {
      listEl.textContent = "";
      if (!list.length) listEl.appendChild(h("div", { class: "empty", text: "No triggers. This job runs only on its clock schedule." }));
      list.forEach(function (t, i) {
        var st = (!dirty && j.trigger_status && j.trigger_status[i]) || null;
        var stat = "";
        if (st) stat = st.last_error ? "Last check failed: " + st.last_error : (st.last_poll ? "Checked " + fmtAge(Math.round(Date.now() / 1000 - st.last_poll)) + (st.count !== null && st.count !== undefined ? ", " + st.count + " match now" : "") : (t.type === "webhook" ? "Waiting for the first delivery" : "Waiting for the first check (it only records a baseline)"));
        else stat = "Not saved yet";
        var res = h("div", { class: "tres" });
        var row = h("div", { class: "trow" },
          h("div", { class: "trow-main" },
            h("span", { class: "chip", text: TRIG_LABEL[t.type] || t.type }),
            h("span", { class: "tsum mono", text: trigSummary(t) })),
          h("div", { class: "trow-stat" + (st && st.last_error ? " bad" : ""), text: stat }),
          h("div", { class: "actions" },
            btn("Test", "eye", "sm", function () { testTrigger(t, res); }),
            btn("Remove", "x", "sm danger", function () { list.splice(i, 1); touch(); paintList(); })),
          res);
        if (t.type === "webhook") row.appendChild(webhookBox());
        listEl.appendChild(row);
      });
    }
    function testTrigger(t, res) {
      res.textContent = ""; res.appendChild(h("div", { class: "muted", text: "Checking once, read only..." }));
      api("POST", "/api/jobs/" + j.id + "/trigger-test", { trigger: t }).then(function (r) {
        res.textContent = "";
        if (!r.ok) { res.appendChild(h("div", { class: "err", text: "Would fail: " + r.error })); return; }
        res.appendChild(h("div", { class: "okline", text: (r.would_fire.length ? "Would fire " + r.would_fire.length + " run(s) now." : "Would fire nothing right now.") + " Rate budget left this hour: " + r.budget_left + " of " + (r.budget_total || 6) + "." }));
        if (r.note) res.appendChild(h("div", { class: "muted", text: r.note }));
        r.would_fire.forEach(function (w) { res.appendChild(h("div", { class: "wf" }, h("div", { class: "mono", text: w.summary }), h("pre", { class: "pre small", text: w.payload_preview }))); });
        res.appendChild(h("div", { class: "muted", text: "Test never runs the job and never changes what Chronos has seen." }));
      }).catch(function (e) { res.textContent = ""; res.appendChild(h("div", { class: "err", text: e.message })); });
    }
    function webhookBox() {
      var box = h("div", { class: "hookbox" });
      var url = "http://127.0.0.1:" + (state.status && state.status.port || 4747) + "/hook/" + j.id;
      box.appendChild(h("div", { class: "muted" }, "Local address: ", h("span", { class: "mono", text: url }), "  (reachable only from this Mac)"));
      var out = h("div");
      var saved = (j.triggers || []).some(function (t) { return t.type === "webhook"; });
      var show = btn("Show secret", "key", "sm", function () {
        api("POST", "/api/jobs/" + j.id + "/hook-secret", {}).then(function (r) { reveal(r.secret); }).catch(fail);
      }, saved ? {} : { disabled: "" });
      var rot = btn("Rotate", "refresh", "sm", function () {
        confirmDlg("Rotate the secret?", "The old secret stops working at once. Anything that calls this hook needs the new one.", "Rotate", true).then(function (ok) {
          if (ok) api("POST", "/api/jobs/" + j.id + "/hook-secret", { rotate: true }).then(function (r) { reveal(r.secret); toast("New secret created.", "ok"); }).catch(fail);
        });
      }, saved ? {} : { disabled: "" });
      function reveal(sec) {
        out.textContent = "";
        var cmd = "curl -X POST " + url + " -H \"X-Chronos-Secret: " + sec + "\" -d '{\"hello\":1}'";
        out.appendChild(h("pre", { class: "pre small", text: cmd }));
        out.appendChild(btn("Copy command", "copy", "sm", function () { if (navigator.clipboard) navigator.clipboard.writeText(cmd).then(function () { toast("Copied.", "ok"); }); }));
        out.appendChild(h("span", { class: "muted", style: "margin-left:10px", text: "Shown only on request. Stored with mode 0600 in the hook-secrets folder of the Chronos home." }));
      }
      box.appendChild(h("div", { style: "display:flex;gap:8px;margin-top:8px;flex-wrap:wrap" }, show, rot, h("span", { class: "muted", text: saved ? "Queued deliveries: " + (j.hook_queue || 0) : "Save the trigger first to create its secret." })));
      box.appendChild(out);
      return box;
    }

    // add form
    var typeSel = h("select", { "aria-label": "Trigger type" }, [["file", "File in a folder"], ["gmail", "Gmail search (work account)"], ["github", "GitHub repository"], ["webhook", "Webhook (local only)"]].map(function (o) { return h("option", { value: o[0], text: o[1] }); }));
    var fields = h("div", { class: "formgrid", style: "margin-top:12px" });
    var inp = {};
    function mkFields() {
      fields.textContent = "";
      inp = {};
      var ty = typeSel.value;
      if (ty === "file") {
        inp.path = h("input", { type: "text", placeholder: "/Users/you/Downloads", "aria-label": "Folder" });
        inp.glob = h("input", { type: "text", placeholder: "*.pdf", value: "*.pdf", "aria-label": "File pattern" });
        fields.appendChild(h("label", { class: "f" }, "Folder (absolute path)", inp.path));
        fields.appendChild(h("label", { class: "f" }, "File pattern", inp.glob, h("span", { class: "hintline", text: "Fires for a new or changed file that has stopped changing for 30 seconds. Only the file name, size and time reach the run, never its contents." })));
      } else if (ty === "gmail") {
        inp.query = h("input", { type: "text", placeholder: "from:someone@example.com subject:invoice", "aria-label": "Gmail search" });
        inp.body = h("input", { type: "checkbox" });
        fields.appendChild(h("label", { class: "f span2" }, "Gmail search", inp.query, h("span", { class: "hintline", text: "Needs a Gmail adapter (gmail_command in config.json, see docs/triggers.md). Polled every 10 minutes with newer_than:1d added. Fires on message ids it has not seen. Mail whose subject contains [Chronos] never fires." })));
        fields.appendChild(h("label", { class: "toggle span2" }, inp.body, "Include the message body (more outside text reaches the run)"));
      } else if (ty === "github") {
        inp.repo = h("input", { type: "text", placeholder: "owner/name", "aria-label": "Repository" });
        inp.event = h("select", { "aria-label": "Event" }, [["pr", "New pull request"], ["issue", "New issue"], ["release", "New release"]].map(function (o) { return h("option", { value: o[0], text: o[1] }); }));
        fields.appendChild(h("label", { class: "f" }, "Repository", inp.repo));
        fields.appendChild(h("label", { class: "f" }, "Event", inp.event, h("span", { class: "hintline", text: "Needs the gh command line tool, logged in (gh auth login). Polled with gh api every 10 minutes under that login." })));
      } else {
        fields.appendChild(h("p", { class: "hint span2", text: "Creates an endpoint on this control panel, POST /hook/<job id>, protected by a per-job secret. It is reachable only on 127.0.0.1. See the notes below for exposing it later." }));
      }
    }
    typeSel.addEventListener("change", mkFields);
    var addBtn = btn("Add trigger", "plus", "", function () {
      var ty = typeSel.value, t = { type: ty };
      if (ty === "file") { t.path = inp.path.value.trim(); t.glob = inp.glob.value.trim() || "*"; if (!t.path) return toast("Give a folder path.", "bad"); }
      if (ty === "gmail") { t.query = inp.query.value.trim(); t.body = inp.body.checked; if (!t.query) return toast("Give a Gmail search.", "bad"); }
      if (ty === "github") { t.repo = inp.repo.value.trim(); t.event = inp.event.value; if (!t.repo) return toast("Give owner/name.", "bad"); }
      if (ty === "webhook" && list.some(function (x) { return x.type === "webhook"; })) return toast("One webhook trigger per job is enough.", "bad");
      list.push(t); touch(); paintList(); mkFields();
    });
    function doSave() {
      var lines = allowedIn.value.split("\n").map(function (x) { return x.trim(); }).filter(Boolean);
      var payload = { triggers: list, clock: clockCb.checked, allowed_tools: lines.join("\n") === defText ? null : lines };
      api("POST", "/api/jobs/" + j.id + "/triggers", payload).then(function () {
        toast("Triggers saved. Chronos reads them at its next tick; each new trigger records a baseline first and fires nothing.", "ok"); dirty = false; dirtyGuard = null; viewJob(j.id, "triggers");
      }).catch(fail);
    }

    body.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { text: "How this job starts" }), h("span", { class: "chips" }, trustChips(j))),
      h("div", { class: "twocol" },
        h("div", null, h("div", { class: "chip ok", text: "Trusted: schedule" }), h("p", { class: "hint", text: "A clock run follows instructions you wrote. It runs with full permissions, as today." })),
        h("div", null, h("div", { class: "chip warn", text: "Untrusted: event" }), h("p", { class: "hint", text: "An event run reads outside text (an email, a pull request, a webhook body). It never skips permissions and only gets the tool list below." }))),
      h("div", { style: "display:flex;gap:28px;flex-wrap:wrap;align-items:center;margin-top:6px" },
        h("label", { class: "toggle" }, clockCb, "Also run on the clock schedule (" + (j.once ? "one-shot" : "see Schedule tab") + ")"),
        h("span", { class: "muted", text: "Rate limit: " + (j.rate_per_hour || 6) + " trigger runs per job per hour. Used in the last hour: " + (j.fires_last_hour || 0) + "." }))));
    body.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { text: "Triggers" })),
      h("p", { class: "hint", text: "Chronos checks every 5 minutes. Gmail and GitHub are polled every 10. Nothing here is pushed from the internet unless you publish the webhook path yourself: the panel listens on 127.0.0.1 only." }),
      listEl,
      h("div", { class: "addbox" }, h("div", { class: "panel-head", style: "margin-bottom:0" }, h("h3", { text: "Add a trigger" }), h("div", { style: "min-width:230px" }, typeSel)), fields, h("div", { style: "margin-top:12px" }, addBtn)),
      h("div", { class: "savebar" }, save, h("span", { class: "muted", text: "Test shows what would fire without running the job." }))));
    body.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { text: "What an event run may do" }), btn("Reset to default", "restore", "sm", function () { allowedIn.value = defText; touch(); })),
      h("p", { class: "hint", text: "One tool per line, claude's --allowedTools syntax. Read, Grep and Glob are read only. If a notify command is configured, a Bash line lets the run call chronos-notify and nothing else. Anything not listed is denied, and event runs load no MCP servers. A bare Bash line is refused. Adding Write, Edit or Bash patterns widens what outside text can make this job do." }),
      allowedIn));
    body.appendChild(h("details", { class: "panel docs" },
      h("summary", { text: "Exposing the webhook to the internet later (NOT done, nothing is exposed today)" }),
      h("p", { class: "hint", text: "The control panel listens on 127.0.0.1 only and rejects any other Host header. To let a service outside this Mac call a hook, publish ONLY the /hook/ path, never the whole panel, and add the public hostname to hook_hosts in config.json (it is accepted on /hook/ only)." }),
      h("h4", { text: "Option 1: Tailscale Funnel" }),
      h("pre", { class: "pre small", text: "tailscale funnel --bg --set-path /hook http://127.0.0.1:" + ((state.status && state.status.port) || 4747) + "/hook\ntailscale funnel status        # confirm only /hook is public\n# then add the machine's public name (example) to the panel's launchd job and restart it:\n#   hook_hosts: [\"macbook.tail1234.ts.net\"] in config.json (or CHRONOS_HOOK_HOSTS=...)\n#   launchctl kickstart -k gui/$(id -u)/io.github.chronos.ui" }),
      h("h4", { text: "Option 2: cloudflared named tunnel" }),
      h("pre", { class: "pre small", text: "# ~/.cloudflared/config.yml\ningress:\n  - hostname: hooks.example.com\n    path: ^/hook/[a-z0-9-]+$\n    service: http://127.0.0.1:4747\n  - service: http_status:404       # everything else is refused\n# hook_hosts: [\"hooks.example.com\"] in config.json  (same restart step as above)" }),
      h("p", { class: "hint", text: "Do not use a bare quick tunnel (cloudflared tunnel --url ...): it would publish the whole panel. Every call still needs the per-job secret in X-Chronos-Secret, bodies are capped at 64 KB, the queue holds 20, wrong secrets are throttled, and event runs stay in the narrow tool list. Rotate the secret from this tab after any exposure." })));
    paintList(); mkFields();
  }

  var MODELS = [
    ["", "Default (no override)", "Whatever the claude command picks on this Mac (or the global model in config.json, if you set one). The right pick when you have no reason to choose."],
    ["opus", "Opus (latest)", "The strongest tier: synthesis, multi-source research, anything where a wrong call costs you. Slowest and the most expensive."],
    ["sonnet", "Sonnet (latest)", "Faster and cheaper than the top tier. Right for checklists, file shuffling, housekeeping sweeps, summaries of data you hand it, and anything with a clear recipe."],
    ["custom", "A specific model id", "Pin an exact version such as claude-sonnet-5-5 so a model upgrade never changes this job's behaviour. An install can forbid some models with model_denylist in config.json."]
  ];
  function tabModel(j, body) {
    var cur = j.model || "";
    var isStd = cur === "" || cur === "opus" || cur === "sonnet";
    var sel = h("select", { "aria-label": "Model" }, MODELS.map(function (m) { return h("option", { value: m[0], text: m[1] }); }));
    sel.value = isStd ? cur : "custom";
    var idIn = h("input", { type: "text", placeholder: "claude-sonnet-5-5", value: isStd ? "" : cur, "aria-label": "Full model id" });
    var save = btn("Save model", "device-floppy", "primary", doSave, { disabled: "" });
    var meaning = h("dl", { class: "mdl" });
    function paint() {
      meaning.textContent = "";
      MODELS.forEach(function (m) {
        meaning.appendChild(h("dt", { class: m[0] === sel.value ? "on" : "", text: m[1] }));
        meaning.appendChild(h("dd", { class: m[0] === sel.value ? "on" : "", text: m[2] }));
      });
      idIn.style.display = sel.value === "custom" ? "" : "none";
    }
    sel.addEventListener("change", function () { paint(); save.disabled = false; });
    idIn.addEventListener("input", function () { save.disabled = false; });
    function doSave() {
      var v = sel.value === "custom" ? idIn.value.trim() : sel.value;
      api("POST", "/api/jobs/" + j.id + "/model", { model: v }).then(function (r) { toast(r.changed ? "Model saved. It applies from the next run." : "No change.", "ok"); viewJob(j.id, "model"); }).catch(fail);
    }
    body.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { text: "Model for this job" })),
      h("p", { class: "hint", text: "Chronos passes --model to every run of this job, clock or event. Leave it on Default to keep the current behaviour." }),
      h("div", { class: "formgrid" }, h("label", { class: "f" }, "Model", sel, idIn)),
      h("div", { style: "margin-top:16px" }, meaning),
      h("p", { class: "hint", text: "The Usage tab shows what each model actually cost per run (list-price equivalents). Compare a job on two models before you commit to the cheaper one." }),
      h("div", { class: "savebar" }, save, h("span", { class: "muted", text: "Currently: " + (j.model || "Default") }))));
    paint();
    if (typeof usagePanel === "function") usagePanel(j, body);
  }

  // ------------------------------------------------------------ v4: control room
  function fmtAgeLong(s) { if (s === null || s === undefined) return "never"; if (s < 90) return s + "s ago"; if (s < 5400) return Math.round(s / 60) + "m ago"; if (s < 172800) return Math.round(s / 3600) + "h ago"; return Math.round(s / 86400) + "d ago"; }
  function tile(label, value, sub, cls, extra) {
    return h("div", { class: "tile " + (cls || "") }, h("div", { class: "tlabel", text: label }), h("div", { class: "tval", text: value }), sub ? h("div", { class: "tsub", text: sub }) : null, extra || null);
  }
  function simpleTable(cols, rows, empty) {
    if (!rows.length) return h("div", { class: "empty", text: empty || "Nothing here." });
    return h("div", { class: "scroll" }, h("table", { class: "list" },
      h("thead", null, h("tr", null, cols.map(function (c) { return h("th", { class: c.hide ? "hide-sm" : "", text: c.label }); }))),
      h("tbody", null, rows.map(function (r) { return h("tr", null, cols.map(function (c) { var v = c.get(r); return h("td", { class: (c.cls || "") + (c.hide ? " hide-sm" : "") }, v); })); }))));
  }
  function filterBox(rowsFn, paint) {
    var q = h("input", { type: "text", placeholder: "Filter", "aria-label": "Filter" });
    q.addEventListener("input", function () { paint(q.value.toLowerCase()); });
    return q;
  }
  function viewControl() {
    setNav("control"); clearTimers(); app.textContent = "";
    var body = h("div");
    app.appendChild(h("div", { class: "pagehead" }, h("div", null, h("h1", { text: "Control room" }), h("p", { class: "lede", text: "The agents, skills, hooks and plugins your Claude Code setup is made of, and whether your workspace is healthy. Read only, except the hook switches and the agent files you can edit below." })),
      h("div", null, btn("Refresh", "refresh", "", function () { viewControl(); }))));
    app.appendChild(body);
    body.appendChild(h("div", { class: "loading", text: "Counting the agents..." }));
    api("GET", "/api/control").then(function (d) {
      body.textContent = "";
      // ---- workspace health (tiles are computed by the server from health_checks in config.json)
      var tiles = h("div", { class: "tiles" });
      d.health.forEach(function (t) {
        tiles.appendChild(tile(t.label, t.value, t.sub, t.cls, t.pct !== null && t.pct !== undefined ? h("div", { class: "bar", role: "img", "aria-label": t.pct + " percent" }, h("span", { style: "width:" + t.pct + "%" })) : null));
      });
      if (!d.health.length) tiles.appendChild(h("div", { class: "empty", text: "No health checks configured. Add a health_checks list to config.json to watch file sizes, freshness and daily files here (see docs/control-room.md)." }));
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Workspace health" }), h("span", { class: "muted mono sm", text: d.workspace })), tiles));
      body.appendChild(agentFilesPanel());

      // ---- kill switches + hooks
      var ks = {};
      d.hooks.forEach(function (hk) { if (hk.kill) { var k = ks[hk.kill.name] = ks[hk.kill.name] || { kill: hk.kill, events: [] }; var lab = hk.event + " " + hk.command.split(" ")[0]; if (k.events.indexOf(lab) < 0) k.events.push(lab); } });
      var ksRows = Object.keys(ks).sort().map(function (name) {
        var k = ks[name], cb = h("input", { type: "checkbox", checked: !k.kill.off, "aria-label": name + " hook is on" });
        cb.addEventListener("change", function () {
          var turnOff = !cb.checked;
          var go = turnOff ? confirmDlg("Switch off " + name + "?", "This creates " + k.kill.file + ". The guard stops acting on its next run, in every session on this Mac, until you switch it back on.", "Switch off", true) : Promise.resolve(true);
          go.then(function (ok) {
            if (!ok) { cb.checked = !cb.checked; return; }
            api("POST", "/api/hooks/" + name + "/kill", { off: turnOff }).then(function () { toast(name + (turnOff ? " switched off." : " switched on."), turnOff ? "bad" : "ok"); viewControl(); }).catch(function (e) { cb.checked = !cb.checked; fail(e); });
          });
        });
        return { name: name, toggle: h("label", { class: "toggle" }, cb, h("span", { class: k.kill.off ? "offtxt" : "ontxt", text: k.kill.off ? "OFF" : "On" })), file: k.kill.file, events: k.events.join(", ") };
      });
      body.appendChild(h("div", { class: "panel" },
        h("div", { class: "panel-head" }, h("h3", { text: "Hook kill switches" }), h("span", { class: "muted", text: "Needs the page token; every change is confirmed" })),
        h("p", { class: "hint", text: d.notes.kill }),
        simpleTable([{ label: "Hook", get: function (r) { return h("span", { class: "mono", text: r.name }); } }, { label: "State", get: function (r) { return r.toggle; } },
          { label: "Switch file", hide: true, cls: "mono", get: function (r) { return r.file; } }, { label: "Guards", hide: true, get: function (r) { return r.events; } }], ksRows, "No hook in the registered set reads a kill-switch file.")));
      var hookRows = d.hooks;
      var hookBody = h("div");
      function paintHooks(q) {
        hookBody.textContent = "";
        hookBody.appendChild(simpleTable([{ label: "Event", get: function (r) { return r.event; } }, { label: "Matcher", hide: true, cls: "mono sm", get: function (r) { return r.matcher; } },
          { label: "Command", cls: "mono nw", get: function (r) { return r.command; } }, { label: "Kill switch", get: function (r) { return r.kill ? h("span", { class: "chip " + (r.kill.off ? "bad" : "ok"), text: r.kill.name + (r.kill.off ? " off" : "") }) : h("span", { class: "muted", text: "none" }); } },
          { label: "From", hide: true, get: function (r) { return r.scope; } }],
          hookRows.filter(function (r) { return !q || (r.event + " " + r.matcher + " " + r.command).toLowerCase().indexOf(q) >= 0; }), "No hooks match."));
      }
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Hooks (" + hookRows.length + ")" }), filterBox(null, paintHooks)),
        h("p", { class: "hint", text: "From the workspace's .claude/settings.json (and settings.local.json) and ~/.claude/settings.json. Hooks without a kill switch can only be disabled by editing settings." }), hookBody));
      paintHooks("");

      // ---- agents
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Agents (" + d.agents.length + ")" })),
        h("p", { class: "hint", text: "From .claude/agents/ in the workspace and ~/.claude/agents/. Model shows \"not declared\" when the agent file does not name one, so it inherits the session's." }),
        simpleTable([{ label: "Name", cls: "mono nw", get: function (r) { return r.name; } }, { label: "Model", get: function (r) { return h("span", { class: "chip" + (r.model === "not declared" ? " dim" : ""), text: r.model }); } },
          { label: "Description", get: function (r) { return r.description; } }, { label: "File", hide: true, cls: "mono sm", get: function (r) { return r.source; } }], d.agents, "No agents found.")));

      // ---- skills
      var skBody = h("div");
      function paintSkills(q) {
        skBody.textContent = "";
        skBody.appendChild(simpleTable([{ label: "Name", cls: "mono nw", get: function (r) { return r.name + (r.duplicate ? " (duplicate name)" : ""); } }, { label: "Description", get: function (r) { return r.description; } },
          { label: "Folder", hide: true, cls: "mono sm", get: function (r) { return r.source; } }],
          d.skills.filter(function (r) { return !q || (r.name + " " + r.description).toLowerCase().indexOf(q) >= 0; }), "No skills match."));
      }
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Skills (" + d.skills.length + ")" }), filterBox(null, paintSkills)),
        h("p", { class: "hint", text: "From .claude/skills/ in the workspace and ~/.claude/skills/. Folders starting with an underscore are skipped." }), skBody));
      paintSkills("");

      // ---- plugins + MCP
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Plugins (" + d.plugins.length + ")" })),
        simpleTable([{ label: "Plugin", cls: "mono", get: function (r) { return r.name; } }, { label: "Marketplace", hide: true, get: function (r) { return r.marketplace; } },
          { label: "State", get: function (r) { return h("span", { class: "chip " + (r.enabled ? "ok" : "dim"), text: r.enabled ? "enabled" : "disabled" }); } }, { label: "Set in", hide: true, get: function (r) { return r.source; } }], d.plugins, "No plugins configured."),
        h("p", { class: "hint", style: "margin-top:12px", text: (d.disabled_in_runs.length ? "Chronos runs always start with these plugins disabled: " + d.disabled_in_runs.join(", ") + ". " : "") + "A headless run that loaded a Telegram or iMessage channel plugin would start a second poller and knock your live channel offline (HTTP 409)." })));
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "MCP servers (" + d.mcp.length + ")" })),
        simpleTable([{ label: "Server", cls: "mono", get: function (r) { return r.name; } }, { label: "Transport", get: function (r) { return r.transport; } }, { label: "Target", cls: "mono", get: function (r) { return r.target; } },
          { label: "Set in", hide: true, get: function (r) { return r.source; } }], d.mcp, "No MCP servers in the config files."),
        h("p", { class: "hint", style: "margin-top:12px", text: d.notes.mcp })));
    }).catch(function (e) { body.textContent = ""; body.appendChild(h("div", { class: "empty", text: "Could not load the control room: " + e.message })); });
  }

  // ------------------------------------------------------------ v4: agent files (view / edit)
  function afAge(iso) {
    var d = parseLocal(iso); if (!d) return "";
    var s = Math.round((Date.now() - d.getTime()) / 1000);
    return s < 0 ? "just now" : fmtAgeLong(s);
  }
  function agentFilesPanel() {
    var wrap = h("div", { class: "panel", id: "agent-files" });
    var listEl = h("div", { class: "afcats" });
    var q = h("input", { type: "text", placeholder: "Filter files", "aria-label": "Filter agent files" });
    var data = null;
    wrap.appendChild(h("div", { class: "panel-head" }, h("h3", { id: "af-title", text: "Agent files" }), q));
    wrap.appendChild(h("p", { class: "hint", text: "The markdown files that steer your agents: CLAUDE.md, agents, skills, notes and auto-memory. Open one to read or edit it. Saves need the page token, keep the previous version in History, and are refused if the file changed on disk since you opened it. Excluded folders, .env files and settings are never listed." }));
    wrap.appendChild(listEl);
    listEl.appendChild(h("div", { class: "loading", text: "Listing the files..." }));
    var openCats = {};
    try { openCats = JSON.parse(sessionStorage.getItem("af-open") || "{}"); } catch (e) { openCats = {}; }
    function paint() {
      var f = q.value.toLowerCase();
      listEl.textContent = "";
      var total = 0;
      data.categories.forEach(function (c, ci) {
        var rows = c.files.filter(function (r) { return !f || (r.name + " " + r.path + " " + r.description).toLowerCase().indexOf(f) >= 0; });
        total += rows.length;
        if (f && !rows.length) return;
        var det = h("details", { class: "afcat" });
        if (f || openCats[c.id] === true || (openCats[c.id] === undefined && ci < 2)) det.open = true;
        det.addEventListener("toggle", function () { openCats[c.id] = det.open; try { sessionStorage.setItem("af-open", JSON.stringify(openCats)); } catch (e) { /* private window */ } });
        det.appendChild(h("summary", null, h("span", { class: "afcat-name", text: c.label }), h("span", { class: "chip dim", text: String(rows.length) })));
        det.appendChild(rows.length ? h("div", { class: "scroll" }, h("table", { class: "list afl" },
          h("colgroup", null, h("col", { class: "c-file" }), h("col", { class: "c-path hide-sm" }), h("col", { class: "c-size hide-sm" }), h("col", { class: "c-mod" }), h("col", { class: "c-desc hide-md" })),
          h("thead", null, h("tr", null, h("th", { text: "File" }), h("th", { class: "hide-sm", text: "Path" }), h("th", { class: "hide-sm", text: "Size" }), h("th", { text: "Modified" }), h("th", { class: "hide-md", text: "What it is" }))),
          h("tbody", null, rows.map(function (r) {
            return h("tr", null,
              h("td", { class: "nw" }, h("a", { class: "afname", href: "#/control/file/" + r.id, text: r.name }), r.agent_edited ? h("span", { class: "chip warn afchip", title: "An agent or hook also edits this automatically", text: "live" }) : null),
              h("td", { class: "mono sm hide-sm", text: r.path }),
              h("td", { class: "mono sm nw hide-sm", text: fmtBytes(r.size) }),
              h("td", { class: "sm nw", title: r.mtime.replace("T", " "), text: afAge(r.mtime) }),
              h("td", { class: "sm hide-md", text: r.description }));
          })))) : h("div", { class: "empty", text: "Nothing in this group." }));
        listEl.appendChild(det);
      });
      if (!total) listEl.appendChild(h("div", { class: "empty", text: "No files match." }));
      document.getElementById("af-title").textContent = "Agent files (" + data.count + ")";
    }
    q.addEventListener("input", function () { if (data) paint(); });
    api("GET", "/api/agent-files").then(function (d) { data = d; paint(); }).catch(function (e) { listEl.textContent = ""; listEl.appendChild(h("div", { class: "empty", text: "Could not list the files: " + e.message })); });
    return wrap;
  }

  function viewAgentFile(fid, res) {
    setNav("control"); clearTimers(); app.textContent = "";
    var holder = h("div");
    app.appendChild(holder);
    holder.appendChild(h("div", { class: "loading", text: "Opening the file..." }));
    api("GET", "/api/agent-files/" + fid).then(function (f) { holder.textContent = ""; paintAgentFile(holder, f, res); })
      .catch(function (e) { holder.textContent = ""; holder.appendChild(h("a", { class: "back", href: "#/control" }, ic("arrow-left"), "Control room")); holder.appendChild(h("div", { class: "empty", text: "Could not open it: " + e.message })); });
  }
  function cpLen(s) { var n = 0; for (var i = 0; i < s.length; i++) { var c = s.charCodeAt(i); if (c >= 0xD800 && c <= 0xDBFF && i + 1 < s.length) i++; n++; } return n; }  // = Python len()

  function paintAgentFile(root, f, firstResult) {
    var base = f.content, baseSha = f.sha, stale = false;
    var ta = h("textarea", { spellcheck: "false", "aria-label": f.name, value: f.content, class: "aftext" });
    if (!f.editable) ta.readOnly = true;
    var dirtyEl = h("span", { class: "dirty" });
    var save = btn("Save", "device-floppy", "primary", doSave, { disabled: "" });
    var meter = h("div", { class: "afmeter" });
    var result = h("div", { class: "afresult" });
    var conflict = h("div");
    var histBox = h("div", { class: "panel" });
    var meta = h("span", { class: "muted mono sm" });

    var back = h("a", { class: "back", href: "#/control", onclick: function (e) { if (dirty() && !window.confirm("You have unsaved changes. Leave and discard them?")) e.preventDefault(); } }, ic("arrow-left"), "Control room");
    root.appendChild(h("div", { class: "pagehead" }, h("div", null, back, h("h1", { text: f.name }), h("p", { class: "lede mono-lede", text: f.path })),
      h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" }, btn("Reload from disk", "refresh", "", reload))));
    if (f.warn) root.appendChild(h("div", { class: "banner note" }, ic("alert-triangle"), h("span", { text: f.warn })));
    if (!f.editable) root.appendChild(h("div", { class: "banner" }, ic("lock"), h("span", { text: "Read only: this file " + f.reason + "." })));
    if (f.also && f.also.length) root.appendChild(h("p", { class: "hint", text: "Same file also reachable as: " + f.also.join(", ") }));
    root.appendChild(conflict);
    root.appendChild(h("div", { class: "panel" },
      h("div", { class: "panel-head" }, h("h3", { text: f.editable ? "Edit" : "View" }), meta),
      meter, result, ta,
      h("div", { class: "savebar" }, f.editable ? save : null, dirtyEl, h("span", { class: "grow" }), f.editable ? h("span", { class: "muted sm", text: "Cmd+S saves" }) : null)));
    root.appendChild(histBox);

    function dirty() { return f.editable && ta.value !== base; }
    dirtyGuard = dirty;
    function upd() {
      var d = dirty(); save.disabled = !d || stale; dirtyEl.textContent = d ? "Unsaved changes" : "";
      var n = cpLen(ta.value), bytes = new Blob([ta.value]).size;
      meta.textContent = n.toLocaleString() + " characters, " + fmtBytes(bytes) + ", opened version " + baseSha.slice(0, 8) + (f.crlf ? ", CRLF line endings kept" : "");
      meter.textContent = "";
      if (f.guard === "ceiling" && f.ceiling) {
        var pct = Math.round(n / f.ceiling * 1000) / 10, over = n > f.ceiling;
        meter.appendChild(h("div", { class: "tile " + (over ? "bad" : (pct > 95 ? "warn" : "ok")) },
          h("div", { class: "tlabel", text: f.name + " size, counted in characters" }),
          h("div", { class: "tval", text: n.toLocaleString() + " / " + f.ceiling.toLocaleString() }),
          h("div", { class: "tsub", text: over ? "OVER the ceiling by " + (n - f.ceiling).toLocaleString() + ". Trim it before adding. The save still goes through." : pct + "% of the ceiling. " + (f.ceiling - n).toLocaleString() + " left." }),
          h("div", { class: "bar", role: "img", "aria-label": pct + " percent" }, h("span", { style: "width:" + Math.min(100, pct) + "%" }))));
      }
      if (f.guard === "claude-md") meter.appendChild(h("p", { class: "hint", style: "margin:0 0 10px", text: "Saving runs the agent_files_lint command from config.json and shows its output. A failed lint warns but keeps the save; History can revert it." }));
    }
    ta.addEventListener("input", upd);
    ta.addEventListener("keydown", function (e) { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); if (!save.disabled) doSave(); } });

    function showResult(r) {
      result.textContent = "";
      var g = r.guard || {};
      if (g.lint) {
        var l = g.lint;
        result.appendChild(h("div", { class: "banner " + (l.ok ? "okb" : ""), role: "alert" }, ic(l.ok ? "check" : "alert-triangle"),
          h("span", { text: l.ok ? "Lint passed." : (l.ran ? "Lint FAILED (exit " + l.exit + "). The save was kept; restore the previous version from History if this is wrong." : "Lint did not run: " + l.output) })));
        if (l.ran || l.output) result.appendChild(h("pre", { class: "pre lintout", text: l.output || "(no output)" }));
      }
      if (g.ceiling && g.ceiling.over) result.appendChild(h("div", { class: "banner", role: "alert" }, ic("alert-triangle"), h("span", { text: "This file is " + g.ceiling.chars.toLocaleString() + " characters, over the " + g.ceiling.ceiling.toLocaleString() + " ceiling. Trim it before adding more." })));
    }
    function doSave() {
      save.disabled = true; conflict.textContent = ""; result.textContent = "";
      api("POST", "/api/agent-files/" + f.id, { content: ta.value, base_sha: baseSha }).then(function (r) {
        if (r.saved) { base = ta.value; baseSha = r.sha; toast("Saved. The previous version is in History.", "ok"); }
        else { base = ta.value; baseSha = r.sha; toast("No changes to save.", "ok"); }
        showResult(r); upd(); loadHist();
      }).catch(function (e) {
        if (e.status === 409) { stale = true; showConflict(e); } else fail(e);
        upd();
      });
    }
    function showConflict(e) {
      conflict.textContent = "";
      conflict.appendChild(h("div", { class: "banner", role: "alert" }, ic("alert-triangle"),
        h("div", { class: "grow" }, h("strong", { text: "This file changed since you opened it. Reload before saving." }),
          h("div", { class: "sm", text: "Nothing was written. An agent or hook edited it on disk" + (e.data && e.data.current_mtime ? " (now " + e.data.current_mtime.replace("T", " ") + ")" : "") + ". Your text is still in the box; copy it first if you want to keep it, then reload and re-apply." })),
        h("div", { style: "display:flex;gap:8px;flex-wrap:wrap" },
          btn("Copy my text", "copy", "sm", function () { copyText(ta.value); }),
          btn("Reload from disk", "refresh", "sm", function () { reload(true); }))));
    }
    function copyText(t) {
      var done = function () { toast("Copied your text to the clipboard.", "ok"); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(done, function () { ta.select(); toast("Select all and copy by hand.", "bad"); });
      else { ta.select(); toast("Select all and copy by hand.", "bad"); }
    }
    function reload(force) {
      if (!force && dirty() && !window.confirm("Reload discards your unsaved edits. Continue?")) return;
      dirtyGuard = null;
      viewAgentFile(f.id);
    }

    function loadHist() {
      api("GET", "/api/agent-files/" + f.id).then(function (n) { paintHist(n.history); }).catch(function () {});
    }
    function paintHist(rows) {
      histBox.textContent = "";
      histBox.appendChild(h("div", { class: "panel-head" }, h("h3", { text: "History" }), h("span", { class: "muted sm", text: rows.length ? rows.length + " saved version(s)" : "" })));
      histBox.appendChild(h("p", { class: "hint", text: "Each save keeps the version it replaced. Restoring keeps the current version too, so a restore can be undone." }));
      if (!rows.length) { histBox.appendChild(h("div", { class: "empty", text: "No saved versions yet." })); return; }
      histBox.appendChild(h("div", { class: "scroll" }, h("table", { class: "list" },
        h("thead", null, h("tr", null, h("th", { text: "Saved over" }), h("th", { class: "hide-sm", text: "Size" }), h("th", null))),
        h("tbody", null, rows.map(function (r) {
          return h("tr", null, h("td", { class: "mono sm", text: r.ts.replace("T", " ") }), h("td", { class: "mono sm hide-sm", text: fmtBytes(r.size) }),
            h("td", null, h("div", { class: "actions" },
              btn("View", "eye", "sm", function () { fetch("/api/agent-files/" + f.id + "/history/" + encodeURIComponent(r.file), { headers: { "X-Chronos-Token": TOKEN } }).then(function (x) { return x.text(); }).then(function (t) { textDlg(f.name + " as of " + r.ts.replace("T", " "), t); }); }),
              f.editable ? btn("Restore", "restore", "sm", function () {
                confirmDlg("Restore this version?", "The file on disk is replaced with the version from " + r.ts.replace("T", " ") + ". The current version is saved to History first, so you can undo this." + (dirty() ? " Your unsaved edits in the box will be dropped." : ""), "Restore").then(function (ok) {
                  if (!ok) return;
                  api("POST", "/api/agent-files/" + f.id + "/restore", { file: r.file }).then(function (res) { dirtyGuard = null; toast("Restored. The replaced version is in History.", "ok"); viewAgentFile(f.id, res); }).catch(fail);
                });
              }) : null)));
        })))));
    }
    paintHist(f.history);
    upd();
    if (firstResult) showResult(firstResult);
  }

  // ------------------------------------------------------------ v4: usage meter
  var JOB_COLORS = ["#d8b25e", "#86c08f", "#8cb8f0", "#e8806d", "#c9a0e8", "#e6b04c", "#7fd1c7", "#efeadc"];
  function fmtTok(n) { n = n || 0; return n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e3 ? (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k" : String(n); }
  function fmtUsd(x) { return x === null || x === undefined ? "n/a" : "$" + (x >= 10 ? x.toFixed(1) : x.toFixed(2)); }
  function fmtDur(s) { if (s === null || s === undefined) return "?"; var m = Math.floor(s / 60); return m ? m + "m " + ("0" + (s % 60)).slice(-2) + "s" : s + "s"; }
  function tokTotal(o) { return (o.input || 0) + (o.output || 0) + (o.cache_write || 0); }  // fresh tokens, cache reads excluded
  function runTok(r) { return (r.input_tokens || 0) + (r.output_tokens || 0) + (r.cache_creation_tokens || 0); }
  function usageLine(u) {
    if (!u || !u.last) return h("span", { class: "muted", text: "No usage recorded yet (starts with the next run)" });
    var l = u.last, w = u.week;
    return h("span", null, "Last run " + fmtDur(l.duration_s) + ", " + fmtUsd(l.cost_usd) + ", " + fmtTok(runTok(l)) + " tok" + (w ? "  |  7 days: " + w.runs + " run" + (w.runs === 1 ? "" : "s") + ", " + fmtUsd(w.cost) : ""));
  }
  function svgEl(tag, attrs, text) {
    var e = document.createElementNS("http://www.w3.org/2000/svg", tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function stackChart(d, metric) {
    var narrow = window.innerWidth < 700, W = narrow ? 400 : 900, H = narrow ? 300 : 330, L = narrow ? 44 : 54, R = 8, T = 14, B = 44, pw = W - L - R, ph = H - T - B;
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, class: "chart", role: "img", "aria-label": "Daily " + (metric === "cost" ? "cost" : "tokens") + " by job, last 14 days" });
    var val = function (o) { return metric === "cost" ? (o.cost || 0) : tokTotal(o); };
    var totals = d.days.map(function (day) { return d.order.reduce(function (a, j) { return a + (day.jobs[j] ? val(day.jobs[j]) : 0); }, 0); });
    var max = Math.max.apply(null, totals.concat([metric === "cost" ? 0.5 : 1000]));
    var step = Math.pow(10, Math.floor(Math.log10(max / 4))); var nice = [1, 2, 2.5, 5, 10].map(function (m) { return m * step; }).filter(function (v) { return max / v <= 5; })[0] || step * 10;
    var top = Math.ceil(max / nice) * nice, ticks = Math.round(top / nice);
    for (var i = 0; i <= ticks; i++) {
      var y = T + ph - (i / ticks) * ph, v = (i / ticks) * top;
      svg.appendChild(svgEl("line", { x1: L, x2: W - R, y1: y, y2: y, class: i === 0 ? "axis" : "grid" }));
      svg.appendChild(svgEl("text", { x: L - 8, y: y + 4, class: "tick", "text-anchor": "end" }, metric === "cost" ? fmtUsd(v) : fmtTok(v)));
    }
    var n = d.days.length, slot = pw / n, bw = Math.min(40, slot * (narrow ? 0.72 : 0.66));
    d.days.forEach(function (day, k) {
      var x = L + k * slot + (slot - bw) / 2, yb = T + ph, tot = totals[k];
      var g = svgEl("g", { class: "bar" });
      g.appendChild(svgEl("title", {}, day.date + ": " + (metric === "cost" ? fmtUsd(tot) : fmtTok(tot) + " tokens") + (day.total.runs ? " in " + day.total.runs + " run(s)" : " (no runs)")));
      d.order.forEach(function (j, ji) {
        var v = day.jobs[j] ? val(day.jobs[j]) : 0; if (!v) return;
        var hh = (v / top) * ph; yb -= hh;
        var r = svgEl("rect", { x: x, y: yb, width: bw, height: Math.max(hh, 1), fill: JOB_COLORS[ji % JOB_COLORS.length], rx: 2 });
        r.appendChild(svgEl("title", {}, (d.names[j] || j) + " " + day.date + ": " + (metric === "cost" ? fmtUsd(v) : fmtTok(v) + " tokens")));
        g.appendChild(r);
      });
      svg.appendChild(g);
      if (tot > 0 && !narrow) svg.appendChild(svgEl("text", { x: x + bw / 2, y: yb - 5, class: "val", "text-anchor": "middle" }, metric === "cost" ? fmtUsd(tot) : fmtTok(tot)));
      var parts = day.date.split("-");
      if (n <= 14 && (!narrow || k % 2 === (n - 1) % 2)) svg.appendChild(svgEl("text", { x: x + bw / 2, y: H - 22, class: "tick", "text-anchor": "middle" }, parts[1] + "/" + parts[2]));
    });
    return svg;
  }
  function rateTile(label, w, age) {
    if (!w || w.utilization === null || w.utilization === undefined) return tile(label, "no reading", "the last run did not report one", "warn");
    var pct = Math.round(w.utilization * 1000) / 10, cls = pct >= 90 ? "bad" : pct >= 75 ? "warn" : "ok";
    var resets = w.resetsAt ? new Date(w.resetsAt * 1000).toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit" }) : "?";
    return tile(label, pct + "%", "resets " + resets + "; read " + fmtAgeLong(age) + " by a background run", cls,
      h("div", { class: "bar", role: "img", "aria-label": pct + " percent used" }, h("span", { style: "width:" + Math.min(100, pct) + "%" })));
  }
  function viewUsage() {
    setNav("usage"); clearTimers(); app.textContent = "";
    var body = h("div"), metric = "cost";
    app.appendChild(h("div", { class: "pagehead" }, h("div", null, h("h1", { text: "Usage" }), h("p", { class: "lede", text: "What Chronos runs cost, job by job, and how near the weekly limit sits." }))));
    app.appendChild(body);
    body.appendChild(h("div", { class: "loading", text: "Adding it up..." }));
    api("GET", "/api/usage").then(function (d) {
      body.textContent = "";
      var tiles = h("div", { class: "tiles" });
      if (d.rate) {
        tiles.appendChild(rateTile("Weekly limit (7-day)", d.rate.seven_day, d.rate.age_s));
        tiles.appendChild(rateTile("5-hour window", d.rate.five_hour, d.rate.age_s));
      } else {
        tiles.appendChild(tile("Weekly limit", "no reading yet", "Chronos saves the limit that claude reports during each run; the first run after this update fills this in.", "warn"));
      }
      var wk = d.days.slice(-7), wcost = 0, wruns = 0, today = d.days[d.days.length - 1];
      wk.forEach(function (x) { wcost += x.total.cost || 0; wruns += x.total.runs || 0; });
      tiles.appendChild(tile("Last 7 days", fmtUsd(wcost), wruns + " run" + (wruns === 1 ? "" : "s") + " (list-price equivalent)"));
      tiles.appendChild(tile("Today", fmtUsd(today.total.cost || 0), (today.total.runs || 0) + " run(s)"));
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "At a glance" })), tiles,
        h("p", { class: "hint", style: "margin-top:12px", text: d.rate ? "The weekly figure is the one claude reports to a headless run, so it is only as fresh as the last run (your live session's status line is the real-time one). It covers all use on the account, not just Chronos." : "" })));

      var chartBox = h("div", { class: "chartbox" }), legendBox = h("div", { class: "legend" });
      var seg = h("div", { class: "seg", role: "group", "aria-label": "Metric" });
      function paint() {
        chartBox.textContent = ""; legendBox.textContent = "";
        if (!d.order.length) { chartBox.appendChild(h("div", { class: "empty", text: "No runs recorded in the last 14 days. Rows appear from the next Chronos run." })); }
        else chartBox.appendChild(stackChart(d, metric));
        d.order.forEach(function (j, i) { legendBox.appendChild(h("span", null, h("span", { class: "swatch", style: "background:" + JOB_COLORS[i % JOB_COLORS.length] }), d.names[j] || j)); });
        Array.prototype.forEach.call(seg.children, function (b) { b.classList.toggle("on", b.dataset.m === metric); });
      }
      [["cost", "Cost (USD)"], ["tokens", "Tokens"]].forEach(function (m) { seg.appendChild(h("button", { type: "button", "data-m": m[0], onclick: function () { metric = m[0]; paint(); } }, m[1])); });
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Last 14 days" }), seg), chartBox, legendBox,
        h("p", { class: "hint", style: "margin-top:12px", text: "Cost is the list-price equivalent claude reports for each run, not what a subscription plan bills. Tokens exclude cache reads (they dwarf everything else and cost a tenth)." })));

      var rows = Object.keys(d.jobs).sort(function (a, b) { return ((d.jobs[b].week || {}).cost || 0) - ((d.jobs[a].week || {}).cost || 0); }).map(function (jid) {
        var e = d.jobs[jid], l = e.last, w = e.week || {};
        return { jid: jid, name: d.names[jid] || jid, l: l, w: w };
      });
      body.appendChild(h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "By job" })),
        simpleTable([{ label: "Job", get: function (r) { return h("a", { href: "#/job/" + r.jid + "/model", text: r.name }); } },
          { label: "Last run", cls: "mono nw", get: function (r) { return r.l ? r.l.start.replace("T", " ").slice(5, 16) : "never"; } },
          { label: "Model", hide: true, cls: "mono sm", get: function (r) { return r.l ? r.l.model : ""; } },
          { label: "Time", hide: true, get: function (r) { return r.l ? fmtDur(r.l.duration_s) : ""; } },
          { label: "Last cost", get: function (r) { return r.l ? fmtUsd(r.l.cost_usd) : ""; } },
          { label: "7d runs", get: function (r) { return r.w.runs || 0; } },
          { label: "7d cost", get: function (r) { return fmtUsd(r.w.cost || 0); } },
          { label: "7d tokens", hide: true, cls: "mono", get: function (r) { return fmtTok(tokTotal(r.w)); } }], rows, "No usage recorded yet.")));
      paint();
    }).catch(function (e) { body.textContent = ""; body.appendChild(h("div", { class: "empty", text: "Could not load usage: " + e.message })); });
  }
  function usagePanel(j, body) {
    var box = h("div", { class: "panel" }, h("div", { class: "panel-head" }, h("h3", { text: "Usage of this job" })), h("div", { class: "loading", text: "Reading runs.jsonl..." }));
    body.appendChild(box);
    api("GET", "/api/usage?job=" + j.id).then(function (d) {
      box.textContent = "";
      box.appendChild(h("div", { class: "panel-head" }, h("h3", { text: "Usage of this job" }), h("a", { href: "#/usage", text: "All jobs" })));
      var w = (d.jobs[j.id] || {}).week;
      box.appendChild(h("p", { class: "hint", text: w ? "Last 7 days: " + w.runs + " run(s), " + fmtUsd(w.cost) + ", " + fmtTok(tokTotal(w)) + " tokens (cache reads excluded), " + fmtDur(w.secs) + " of run time." : "No runs recorded in the last 7 days." }));
      box.appendChild(simpleTable([{ label: "Started", cls: "mono nw", get: function (r) { return r.start.replace("T", " ").slice(5, 16) + (r.trigger ? " event" : ""); } },
        { label: "Model", hide: true, cls: "mono sm", get: function (r) { return r.model || ""; } }, { label: "Time", get: function (r) { return fmtDur(r.duration_s); } },
        { label: "In / out", hide: true, cls: "mono", get: function (r) { return fmtTok(r.input_tokens) + " / " + fmtTok(r.output_tokens); } },
        { label: "Cache w / r", hide: true, cls: "mono", get: function (r) { return fmtTok(r.cache_creation_tokens) + " / " + fmtTok(r.cache_read_tokens); } },
        { label: "Turns", hide: true, get: function (r) { return r.num_turns === null || r.num_turns === undefined ? "" : String(r.num_turns); } },
        { label: "Cost", get: function (r) { return fmtUsd(r.cost_usd); } },
        { label: "Result", get: function (r) { return h("span", { class: "chip " + (r.ok ? "ok" : "bad"), text: r.ok ? "ok" : "failed" }); } }], d.recent, "No runs recorded yet. Usage is captured from the next run."));
    }).catch(function (e) { box.appendChild(h("div", { class: "empty", text: e.message })); });
  }

  // ------------------------------------------------------------ new job
  function viewNew() {
    setNav("new"); clearTimers(); app.textContent = "";
    var mode = "recurring";
    var nameIn = h("input", { type: "text", maxlength: "60", placeholder: "Weekly review", "aria-label": "Name" });
    var idIn = h("input", { type: "text", maxlength: "40", placeholder: "weekly-review", "aria-label": "Id slug" });
    var idTouched = false;
    nameIn.addEventListener("input", function () { if (!idTouched) idIn.value = nameIn.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40); });
    idIn.addEventListener("input", function () { idTouched = true; });
    var descIn = h("input", { type: "text", maxlength: "300", placeholder: "One line on what this job does", "aria-label": "Description" });
    var notifyIn = notifySelect("failure");
    var promptIn = h("textarea", { spellcheck: "false", placeholder: "Write the job as instructions to Claude, as if briefing a new teammate who cannot ask questions. Chronos adds the done-marker instructions for you.", "aria-label": "Prompt", style: "min-height:30vh" });
    var ins = h("input", { type: "checkbox" });
    var schedHolder = h("div");
    var insLabel = h("label", { class: "toggle" }, ins, "Also arm in my interactive session (needs the optional hook)");
    var w = null;
    function mount() {
      schedHolder.textContent = "";
      w = scheduleWidget(mode === "once" ? { once: nextHour(), catchup_min: 120 } : { time: "09:00", days: "weekdays", catchup_min: 180 }, { once: mode === "once" });
      schedHolder.appendChild(w.el);
      insLabel.style.display = mode === "once" ? "none" : "";
    }
    function nextHour() { var d = new Date(Date.now() + 3600e3); d.setMinutes(0, 0, 0); var p = function (n) { return ("0" + n).slice(-2); }; return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + "T" + p(d.getHours()) + ":" + p(d.getMinutes()); }
    var segRec = h("button", { type: "button", class: "on", onclick: function () { setMode("recurring"); } }, "Recurring job");
    var segOnce = h("button", { type: "button", onclick: function () { setMode("once"); } }, "One-shot reminder");
    function setMode(m) { mode = m; segRec.classList.toggle("on", m === "recurring"); segOnce.classList.toggle("on", m === "once"); mount(); }
    var create = btn("Create job", "plus", "primary", function () {
      create.disabled = true;
      var payload = Object.assign({ mode: mode, name: nameIn.value, id: idIn.value, description: descIn.value, notify: notifyIn.value, prompt: promptIn.value, in_session: ins.checked }, w.value());
      api("POST", "/api/jobs", payload).then(function (r) { toast("Created " + r.job.name + ".", "ok"); location.hash = "#/job/" + r.job.id; }).catch(function (e) { create.disabled = false; fail(e); });
    });
    app.appendChild(h("div", { class: "pagehead" }, h("div", null, h("h1", { text: "A new job" }), h("p", { class: "lede", text: "Name it, set the hour, tell Claude what to do." })), h("div", { class: "seg", role: "group", "aria-label": "Job type" }, segRec, segOnce)));
    app.appendChild(h("div", { class: "panel" },
      h("div", { class: "formgrid" },
        h("label", { class: "f" }, "Name", nameIn),
        h("label", { class: "f" }, "Id slug", idIn, h("span", { class: "hintline", text: "Lowercase letters, numbers and dashes. Also names the job folder and the done-marker." })),
        h("label", { class: "f span2" }, "Description", descIn),
        h("label", { class: "f span2" }, "Notify", notifyIn, h("span", { class: "hintline", text: "Uses the notify command from your Chronos config. Nothing is sent if none is set." }))),
      h("div", { style: "margin-top:20px" }, h("h3", { text: "When", style: "margin-bottom:10px" }), schedHolder),
      h("div", { style: "margin-top:20px" }, h("label", { class: "f" }, "Prompt", promptIn)),
      h("div", { style: "margin-top:14px" }, insLabel),
      h("div", { class: "savebar" }, create, h("span", { class: "muted", text: "Creates the job folder with prompt.md and a schedule entry." }))));
    mount();
  }

  // ------------------------------------------------------------ logs
  function viewLogs() {
    setNav("logs"); clearTimers(); app.textContent = "";
    var body = h("div");
    app.appendChild(h("div", { class: "pagehead" }, h("div", null, h("h1", { text: "Logs and reports" }), h("p", { class: "lede", text: "What each run said, read only." }))));
    app.appendChild(body);
    api("GET", "/api/runs").then(function (d) {
      var rows = d.runs.map(function (r) {
        return h("tr", null, h("td", { class: "mono", text: r.date ? r.date + (r.time ? " " + r.time : "") + (r.event ? " event" : "") : "always" }), h("td", { text: r.job }), h("td", { text: r.kind === "log" ? "Log" : "Report" }), h("td", { class: "hide-sm mono", text: fmtBytes(r.size) }), h("td", null, h("div", { class: "actions" }, h("a", { class: "btn sm", href: "#/view/" + r.kind + "/" + r.name }, ic("eye"), "Open"))));
      });
      body.appendChild(h("div", { class: "panel" }, rows.length ? h("div", { class: "scroll" }, h("table", { class: "list" }, h("thead", null, h("tr", null, h("th", { text: "When" }), h("th", { text: "Job" }), h("th", { text: "Kind" }), h("th", { class: "hide-sm", text: "Size" }), h("th"))), h("tbody", null, rows))) : h("div", { class: "empty", text: "Nothing here yet." })));
    }).catch(function (e) { body.appendChild(h("div", { class: "empty", text: e.message })); });
  }
  function viewFile(kind, name) {
    setNav("logs"); clearTimers(); app.textContent = "";
    var pre = h("pre", { class: "pre", text: "Loading..." });
    app.appendChild(h("div", { class: "pagehead" }, h("div", null, h("a", { class: "back", href: "#/logs", onclick: function (e) { e.preventDefault(); history.back(); } }, ic("arrow-left"), "Back"), h("h1", { text: name }), h("p", { class: "lede", text: kind === "log" ? "Raw log, read only" : "Report, read only" }))));
    app.appendChild(h("div", { class: "panel" }, pre));
    fetch("/api/file?kind=" + encodeURIComponent(kind) + "&name=" + encodeURIComponent(name)).then(function (r) { return r.text().then(function (t) { pre.textContent = r.ok ? t : "Could not read it: " + t; }); }).catch(function (e) { pre.textContent = String(e); });
  }

  // ------------------------------------------------------------ router
  function route(soft) {
    var hash = location.hash || "#/";
    var m;
    clearTimers();
    if ((m = /^#\/job\/([a-z0-9-]+)(?:\/([a-z]+))?$/.exec(hash))) return viewJob(m[1], m[2]);
    if (hash === "#/new") return viewNew();
    if (hash === "#/logs") return viewLogs();
    if (hash === "#/control") return viewControl();
    if ((m = /^#\/control\/file\/([a-z0-9-]{1,101})$/.exec(hash))) return viewAgentFile(m[1]);
    if (hash === "#/usage") return viewUsage();
    if ((m = /^#\/view\/(log|report)\/([A-Za-z0-9._-]+)$/.exec(hash))) return viewFile(m[1], m[2]);
    return viewDashboard();
  }
  window.addEventListener("hashchange", function () { route(false); window.scrollTo(0, 0); });
  window.addEventListener("beforeunload", function (e) { if (dirtyGuard && dirtyGuard()) { e.preventDefault(); e.returnValue = ""; } });

  drawTicks("ticks1", 176, 120, 10, 6, 14);
  drawTicks("ticks2", 128, 72, 6, 5, 11);
  drawStars(); window.addEventListener("resize", drawStars);
  tickClock(); setInterval(tickClock, 15000);
  refreshStatus(); setInterval(refreshStatus, 30000);
  route(false);
})();
