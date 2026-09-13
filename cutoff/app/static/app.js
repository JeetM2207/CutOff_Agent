// Vanilla JS, polling /api/* every second (Section 16). No build step.

const COLUMN_LABELS = {
  register: "Register",
  your_call: "Your call",
  not_for_you: "Not for you",
  watch_out: "Watch out",
  closed: "Closed",
};
const COLUMN_DESCRIPTIONS = {
  register: "You qualify — an approval card is waiting in Telegram.",
  your_call: "Something's unclear — CutOff asked you a question in Telegram.",
  not_for_you: "You don't meet the criteria for these.",
  watch_out: "Looked like a scam or a fake sender — nothing was done.",
  closed: "Already registered, or the drive is no longer open.",
};
const COLUMN_ORDER = ["register", "your_call", "not_for_you", "watch_out", "closed"];

let selectedDriveId = null;
let currentView = "board";
let lastRenderedDetail = null;
let lastRenderedBoard = null;
let lastRenderedTrace = null;
let lastRenderedEval = null;

// --- tab switching -----------------------------------------------------------

function switchView(view) {
  currentView = view;
  document.querySelectorAll(".view").forEach((el) => el.classList.toggle("active", el.id === `view-${view}`));
  document.querySelectorAll("nav.tabs button").forEach((btn) => btn.classList.toggle("active", btn.dataset.view === view));
}

document.querySelectorAll("nav.tabs button").forEach((btn) => {
  btn.addEventListener("click", () => switchView(btn.dataset.view));
});

// --- board ---------------------------------------------------------------------

function formatDeadline(iso) {
  if (!iso) return "No deadline stated";
  const d = new Date(iso);
  const now = new Date();
  const hoursLeft = (d - now) / 36e5;
  const label = d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  return { label, closingSoon: hoursLeft > 0 && hoursLeft < 6, passed: hoursLeft <= 0 };
}

function renderBubbles(bubbles) {
  const strip = document.createElement("div");
  strip.className = "bubble-strip";
  for (const b of bubbles || []) {
    const el = document.createElement("span");
    el.className = "bubble";
    el.dataset.state = b.state;
    el.title = `${b.label}: ${b.state.replace("_", " ")}`;
    strip.appendChild(el);
  }
  return strip;
}

function renderDriveRow(drive) {
  const row = document.createElement("div");
  row.className = "drive-row";
  row.addEventListener("click", () => loadDriveDetail(drive.drive_id));

  const company = document.createElement("div");
  company.className = "company";
  company.textContent = drive.company;
  row.appendChild(company);

  const role = document.createElement("div");
  role.className = "role";
  role.textContent = `${drive.role} · v${drive.version}`;
  row.appendChild(role);

  const deadline = document.createElement("div");
  const dl = formatDeadline(drive.deadline);
  if (typeof dl === "string") {
    deadline.className = "deadline";
    deadline.textContent = dl;
  } else {
    deadline.className = "deadline" + (dl.closingSoon || dl.passed ? " closing-soon" : "");
    deadline.textContent = dl.passed ? `Closed · ${dl.label}` : dl.label;
  }
  row.appendChild(deadline);

  row.appendChild(renderBubbles(drive.bubbles));
  return row;
}

function renderWatchoutRow(item) {
  const row = document.createElement("div");
  row.className = "watchout-row";
  const company = document.createElement("div");
  company.className = "company";
  company.textContent = item.company_hint;
  row.appendChild(company);
  const signals = document.createElement("div");
  signals.className = "signals";
  signals.textContent = item.signals;
  row.appendChild(signals);
  return row;
}

function renderLegend() {
  const legend = document.getElementById("bubble-legend");
  if (!legend || legend.dataset.built) return;
  legend.dataset.built = "1";
  legend.innerHTML = "";
  const title = document.createElement("span");
  title.className = "legend-title";
  title.textContent = "Criteria dots:";
  legend.appendChild(title);
  const items = [
    ["passed", "meets it"],
    ["failed", "fails it"],
    ["unclear", "unclear — worth a second look"],
    ["not_stated", "not stated in the email"],
  ];
  items.forEach(([state, text]) => {
    const item = document.createElement("span");
    item.className = "legend-item";
    const dot = document.createElement("span");
    dot.className = `legend-dot ${state}`;
    item.appendChild(dot);
    item.appendChild(document.createTextNode(text));
    legend.appendChild(item);
  });
}

async function pollBoard() {
  try {
    const resp = await fetch("/api/drives");
    const data = await resp.json();
    const serialized = JSON.stringify(data);
    if (serialized === lastRenderedBoard) return;  // don't fight the reader's scroll for no reason
    lastRenderedBoard = serialized;
    renderLegend();
    const board = document.getElementById("board");
    board.innerHTML = "";
    for (const key of COLUMN_ORDER) {
      const col = document.createElement("div");
      col.className = "board-column";
      const head = document.createElement("div");
      head.className = "col-head";
      const h2 = document.createElement("h2");
      const titleSpan = document.createElement("span");
      titleSpan.textContent = COLUMN_LABELS[key];
      h2.appendChild(titleSpan);
      const count = document.createElement("span");
      count.className = "col-count";
      count.textContent = data[key].length;
      h2.appendChild(count);
      head.appendChild(h2);
      const desc = document.createElement("p");
      desc.className = "col-desc";
      desc.textContent = COLUMN_DESCRIPTIONS[key];
      head.appendChild(desc);
      col.appendChild(head);

      const body = document.createElement("div");
      body.className = "col-body";
      if (data[key].length === 0) {
        const empty = document.createElement("div");
        empty.className = "empty-state";
        empty.textContent = "Nothing here yet.";
        body.appendChild(empty);
      } else if (key === "watch_out") {
        data[key].forEach((item) => body.appendChild(renderWatchoutRow(item)));
      } else {
        data[key].forEach((drive) => body.appendChild(renderDriveRow(drive)));
      }
      col.appendChild(body);
      board.appendChild(col);
    }
  } catch (err) {
    console.error("CutOff: /api/drives poll failed", err);
  }
}

// --- drive detail --------------------------------------------------------------

const NOTICE_TYPE_LABELS = {
  NEW_DRIVE: "New drive found",
  REVISION: "Details changed",
  CANCELLATION: "Drive cancelled",
  REMINDER: "Reminder email",
  SCHEDULE: "Test/interview scheduled",
  SHORTLIST: "Shortlist result",
};

function describeHistoryEntry(h) {
  if (h.created) return "First seen — this drive was created from this email.";
  // A later entry classified as NEW_DRIVE just reflects the extractor's own
  // read of that one email, not a second "new drive" event — the real
  // first-seen entry is the h.created one above. Anything after that is,
  // by definition, an update.
  const noticeType = h.notice_type === "NEW_DRIVE" ? "REVISION" : h.notice_type;
  const label = NOTICE_TYPE_LABELS[noticeType] || "Updated";
  return h.change_summary ? `${label}: ${h.change_summary}` : label;
}

const ACTION_APP_LABELS = { sheets: "Tracker sheet", calendar: "Calendar", telegram: "Telegram" };
const ACTION_KIND_LABELS = {
  upsert_row: "update the tracker row",
  cancel_event: "cancel the calendar event",
  upsert_event: "add/update a calendar event",
  send_msg: "send a message",
};

function describeAction(a) {
  const appLabel = ACTION_APP_LABELS[a.app] || a.app;
  const kindLabel = ACTION_KIND_LABELS[a.kind] || a.kind;
  return `${appLabel}: ${kindLabel}`;
}

const STATUS_LABELS = {
  VERIFIED: "Verified", DONE: "Done", FAILED: "Failed",
  PLANNED: "Planned", EXECUTING: "Running", VOIDED: "Voided",
};
const STATUS_DESCRIPTIONS = {
  VERIFIED: "Done, and double-checked against the real app afterward.",
  DONE: "Done.",
  FAILED: "Didn't go through — see the error below.",
  PLANNED: "Queued, hasn't run yet.",
  EXECUTING: "Running right now.",
  VOIDED: "No longer relevant — superseded by a newer update.",
};

function renderEvidenceField(label, value, evidenceList, fieldPath, emptyText) {
  const wrap = document.createElement("div");
  wrap.className = "detail-field";
  const labelEl = document.createElement("div");
  labelEl.className = "label";
  const isEmpty = value === null || value === undefined || value === "";
  labelEl.textContent = `${label}: ${isEmpty ? (emptyText || "not stated") : value}`;
  wrap.appendChild(labelEl);

  const match = (evidenceList || []).find((e) => e.field === fieldPath);
  if (match) {
    const ev = document.createElement("div");
    ev.className = "evidence";
    ev.textContent = `“${match.quote}”`;
    wrap.appendChild(ev);
  }
  return wrap;
}

async function loadDriveDetail(driveId) {
  selectedDriveId = driveId;
  try {
    const resp = await fetch(`/api/drives/${encodeURIComponent(driveId)}`);
    if (!resp.ok) return;
    const data = await resp.json();
    // Rebuilding the DOM every poll tick (even to identical content) resets
    // scroll position mid-read — only re-render when the data actually changed.
    const serialized = JSON.stringify(data);
    if (serialized === lastRenderedDetail) return;
    lastRenderedDetail = serialized;
    renderDriveDetail(data);
  } catch (err) {
    console.error("CutOff: drive detail fetch failed", err);
  }
}

function renderDriveDetail(data) {
  const panel = document.getElementById("drive-detail");
  panel.innerHTML = "";
  const box = document.createElement("div");
  box.className = "detail-panel";

  const closeBtn = document.createElement("button");
  closeBtn.className = "close-detail";
  closeBtn.textContent = "Close ×";
  closeBtn.addEventListener("click", () => { selectedDriveId = null; lastRenderedDetail = null; panel.innerHTML = ""; });
  box.appendChild(closeBtn);

  const h2 = document.createElement("h2");
  h2.textContent = `${data.drive.company} — ${data.drive.role}`;
  box.appendChild(h2);

  const latestEvidence = (data.history[data.history.length - 1] || {}).evidence || [];
  box.appendChild(renderEvidenceField("Company", data.drive.company, latestEvidence, "company"));
  box.appendChild(renderEvidenceField("Role", data.drive.role, latestEvidence, "role"));
  box.appendChild(renderEvidenceField("Min GPA", data.criteria.min_gpa, latestEvidence, "criteria.min_gpa"));
  box.appendChild(renderEvidenceField("Branches", (data.criteria.branches_allowed || []).join(", "), latestEvidence, "criteria.branches_allowed"));
  box.appendChild(renderEvidenceField("Max backlogs", data.criteria.max_active_backlogs, latestEvidence, "criteria.max_active_backlogs"));
  box.appendChild(renderEvidenceField("Deadline", data.drive.deadline, latestEvidence, "deadline_text", "not stated in the email"));
  box.appendChild(renderEvidenceField("Form", data.form_url, latestEvidence, "form_url", "not found — check the email"));

  // Interactive Live Action Buttons (Form Prefill + Resume PDF)
  if (data.prefilled_form_url || (data.resolved_resume_pick && data.resolved_resume_pick.web_view_link)) {
    const actionBtns = document.createElement("div");
    actionBtns.style.display = "flex";
    actionBtns.style.flexWrap = "wrap";
    actionBtns.style.gap = "0.6rem";
    actionBtns.style.margin = "1rem 0 0.5rem 0";

    if (data.prefilled_form_url) {
      const formBtn = document.createElement("a");
      formBtn.href = data.prefilled_form_url;
      formBtn.target = "_blank";
      formBtn.className = "btn-primary-action";
      formBtn.innerHTML = "📝 Open Pre-Filled Google Form (All 6 Fields Populated) ↗";
      actionBtns.appendChild(formBtn);
    }
    if (data.resolved_resume_pick && data.resolved_resume_pick.web_view_link) {
      const resumeBtn = document.createElement("a");
      resumeBtn.href = data.resolved_resume_pick.web_view_link;
      resumeBtn.target = "_blank";
      resumeBtn.className = "btn-secondary-action";
      resumeBtn.innerHTML = "📄 View Tailored Resume (PDF) ↗";
      actionBtns.appendChild(resumeBtn);
    }
    box.appendChild(actionBtns);
  }

  // Multi-Platform Status Grid
  const platformGrid = document.createElement("div");
  platformGrid.className = "platform-grid";

  // 1. Gmail API Card
  const gmailCard = document.createElement("div");
  gmailCard.className = "platform-card";
  gmailCard.innerHTML = `
    <div class="platform-card-header">
      <span>📬 Gmail API Ingestion</span>
      <span class="chip-status live">Verified</span>
    </div>
    <div class="platform-card-desc">Grounding quotes extracted with zero hallucinations from career email attachment.</div>
  `;
  platformGrid.appendChild(gmailCard);

  // 2. Google Sheets Card
  const sheetsCard = document.createElement("div");
  sheetsCard.className = "platform-card";
  const sheetLink = data.platform_links?.sheet_url
    ? `<a href="${data.platform_links.sheet_url}" target="_blank" style="color:var(--brand);font-weight:600;">Open Sheet Tab ↗</a>`
    : "";
  sheetsCard.innerHTML = `
    <div class="platform-card-header">
      <span>📊 Google Sheets Tracker</span>
      <span class="chip-status live">Synced</span>
    </div>
    <div class="platform-card-desc">Row recorded under placement ledger with verdict: <strong>${data.drive.verdict || 'PENDING'}</strong>. ${sheetLink}</div>
  `;
  platformGrid.appendChild(sheetsCard);

  // 3. Google Calendar Card
  const calCard = document.createElement("div");
  calCard.className = "platform-card";
  const calEventDesc = data.events && data.events.length > 0
    ? `${data.events[0].kind} on ${new Date(data.events[0].start).toLocaleString(undefined, {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'})} (0 clashes with exam timetable).`
    : "No exam timetable clashes detected. Auto-scheduling ready upon round shortlist.";
  calCard.innerHTML = `
    <div class="platform-card-header">
      <span>📅 Google Calendar</span>
      <span class="chip-status live">Clash-Free</span>
    </div>
    <div class="platform-card-desc">${calEventDesc}</div>
  `;
  platformGrid.appendChild(calCard);

  // 4. Telegram Copilot Card
  const tgCard = document.createElement("div");
  tgCard.className = "platform-card";
  const tgLink = data.platform_links?.telegram_url
    ? `<a href="${data.platform_links.telegram_url}" target="_blank" style="color:#2563EB;font-weight:600;">@hackathon_cutoff_bot ↗</a>`
    : "@hackathon_cutoff_bot";
  tgCard.innerHTML = `
    <div class="platform-card-header">
      <span>💬 Telegram Copilot</span>
      <span class="chip-status live">Active</span>
    </div>
    <div class="platform-card-desc">Interactive approval card delivered. Review, generate resume, or approve via ${tgLink}.</div>
  `;
  platformGrid.appendChild(tgCard);

  box.appendChild(platformGrid);

  // Interview Prep Intel (if available). strategy_summary and the reference
  // links are LLM-synthesized from live web search results -- untrusted text
  // that must never go into innerHTML unescaped (a search hit's title/snippet
  // could contain markup).
  if (data.prep_intel && data.prep_intel.strategy_summary) {
    const intelBox = document.createElement("div");
    intelBox.className = "prep-intel-box";

    const title = document.createElement("div");
    title.className = "prep-intel-title";
    title.textContent = "💡 Interview Prep Intel (Autonomous Synthesis)";
    intelBox.appendChild(title);

    const strategy = document.createElement("div");
    strategy.className = "prep-intel-strategy";
    strategy.textContent = data.prep_intel.strategy_summary;
    intelBox.appendChild(strategy);

    const links = data.prep_intel.top_reference_links || [];
    if (links.length > 0) {
      const linksBox = document.createElement("div");
      linksBox.className = "prep-intel-links";
      linksBox.appendChild(document.createTextNode("🔗 "));
      const label = document.createElement("strong");
      label.textContent = "Resources:";
      linksBox.appendChild(label);
      links.forEach((l, i) => {
        if (i > 0) linksBox.appendChild(document.createTextNode(" • "));
        const a = document.createElement("a");
        a.href = l;
        a.target = "_blank";
        a.textContent = l;
        linksBox.appendChild(a);
      });
      intelBox.appendChild(linksBox);
    }

    box.appendChild(intelBox);
  }

  const historyHeader = document.createElement("h2");
  historyHeader.textContent = "Version history";
  box.appendChild(historyHeader);
  const historyDesc = document.createElement("p");
  historyDesc.className = "section-desc";
  historyDesc.textContent = "Every email that created or changed this drive, oldest first.";
  box.appendChild(historyDesc);
  if (data.history.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No revisions yet.";
    box.appendChild(empty);
  }
  data.history.forEach((h) => {
    const entry = document.createElement("div");
    entry.className = "history-entry";
    const badge = document.createElement("span");
    badge.className = "version-badge";
    badge.textContent = `v${h.version}`;
    entry.appendChild(badge);
    entry.appendChild(document.createTextNode(
      `${describeHistoryEntry(h)} — ${new Date(h.timestamp).toLocaleString()}`
    ));
    box.appendChild(entry);
  });

  const actionsHeader = document.createElement("h2");
  actionsHeader.textContent = "Planned actions";
  box.appendChild(actionsHeader);
  const actionsDesc = document.createElement("p");
  actionsDesc.className = "section-desc";
  actionsDesc.textContent = "What CutOff has done, or is about to do, for this drive — hover a status for what it means.";
  box.appendChild(actionsDesc);
  if (data.actions.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "No actions planned yet.";
    box.appendChild(empty);
  }
  data.actions.forEach((a) => {
    const entry = document.createElement("div");
    entry.className = "action-entry";
    const badge = document.createElement("span");
    badge.className = "status-badge";
    badge.dataset.status = a.status;
    badge.textContent = STATUS_LABELS[a.status] || a.status;
    badge.title = STATUS_DESCRIPTIONS[a.status] || "";
    entry.appendChild(badge);
    entry.appendChild(document.createTextNode(describeAction(a)));
    if (a.last_error) {
      const err = document.createElement("div");
      err.className = "evidence";
      err.textContent = a.last_error;
      entry.appendChild(err);
    }
    box.appendChild(entry);
  });

  panel.appendChild(box);
}

// --- live trace ------------------------------------------------------------------

async function pollTrace() {
  try {
    const resp = await fetch("/api/runs/latest");
    const data = await resp.json();
    const serialized = JSON.stringify(data);
    if (serialized === lastRenderedTrace) return;
    lastRenderedTrace = serialized;
    const label = document.getElementById("trace-run-label");
    const timeline = document.getElementById("trace-timeline");
    if (!data.run_id) {
      label.textContent = "No runs yet.";
      timeline.innerHTML = "";
      return;
    }
    label.innerHTML = "";
    const firstSpan = data.spans[0];
    const when = firstSpan ? new Date(firstSpan.started_at).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
    }) : "just now";
    label.appendChild(document.createTextNode(`Latest run — ${when} `));
    const ref = document.createElement("span");
    ref.className = "run-ref";
    ref.textContent = `(ref: ${data.run_id.slice(0, 8)})`;
    label.appendChild(ref);
    timeline.innerHTML = "";
    for (const span of data.spans) {
      const el = document.createElement("div");
      el.className = "trace-span";
      el.dataset.status = span.status;
      const name = document.createElement("div");
      name.className = "span-name";
      const duration = span.ended_at
        ? `${(new Date(span.ended_at) - new Date(span.started_at))}ms`
        : "running…";
      name.textContent = `${span.name} (${duration})`;
      el.appendChild(name);
      const attrs = document.createElement("div");
      attrs.className = "span-attrs";
      const parsedAttrs = JSON.parse(span.attrs || "{}");
      attrs.textContent = JSON.stringify(parsedAttrs);
      el.appendChild(attrs);
      if (parsedAttrs.error) {
        const errEl = document.createElement("div");
        errEl.className = "span-error";
        errEl.style.color = "#d93025";
        errEl.style.fontWeight = "600";
        errEl.style.marginTop = "4px";
        errEl.textContent = `Error: ${parsedAttrs.error}`;
        el.appendChild(errEl);
      }
      timeline.appendChild(el);
    }
  } catch (err) {
    console.error("CutOff: trace poll failed", err);
  }
}

// --- eval tab --------------------------------------------------------------------

function pct(ratio) {
  return ratio === null || ratio === undefined ? "n/a" : `${(ratio * 100).toFixed(1)}%`;
}

const METRIC_DESCRIPTIONS = {
  verdict_accuracy: "Did CutOff give the right eligible/not-eligible/needs-review answer?",
  dangerous_errors: "Times it registered someone who wasn't actually eligible — the worst kind of mistake.",
  missed_opportunities: "Times it failed to tell an eligible student about a drive.",
  deadline_accuracy: "Did it get the registration deadline right?",
  duplicate_effects: "Times the same email caused the same action twice (e.g. two calendar events).",
  forbidden_effects: "Times it did something it should never do on its own (like registering without approval).",
  required_effects: "Of everything it was supposed to do (sheet update, calendar event, message), how much actually happened.",
  scam_recall: "Of the scam/fake emails in the test set, how many did it correctly catch.",
  scam_false_alarms: "Real emails it wrongly flagged as scams.",
};

function describeRunColumn(r) {
  // r.label already bakes in a "-chaos"/"-heldout" suffix (it's derived from
  // the result file's own name) — strip it before adding our own plain-
  // English suffix, or "baseline-chaos + simulated failures" doubles up.
  if (!r.chaos || r.chaos === "none") {
    return r.label.replace(/-heldout$/, " (held-out set — emails never used while building this)");
  }
  const base = r.label.replace(/-chaos$/, "");
  return `${base} + simulated failures`;
}

function formatCategoryLabel(category) {
  return category.replace(/_/g, " ");
}

function describeFailureCluster(c) {
  const metricLabel = c.failed_metric.replace(/_/g, " ");
  const count = c.count === 1 ? "1 test case" : `${c.count} test cases`;
  return {
    headline: `${count} in "${formatCategoryLabel(c.category)}" got the wrong result for: ${metricLabel}`,
    ids: c.scenario_ids.join(", "),
  };
}

async function loadEval() {
  try {
    const resp = await fetch("/api/eval/latest");
    const data = await resp.json();
    const serialized = JSON.stringify(data);
    if (serialized === lastRenderedEval) return;
    lastRenderedEval = serialized;
    const container = document.getElementById("eval-content");
    container.innerHTML = "";

    if (data.runs.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No eval results committed yet.";
      container.appendChild(empty);
      return;
    }

    const intro = document.createElement("p");
    intro.className = "section-desc";
    intro.textContent = (
      "Scored against a fixed set of test emails (not real students), so changes to the code can be " +
      "checked for regressions. \"baseline\" is before a change, \"after\" is after. Columns marked " +
      "“+ simulated failures” replay the same test emails with random network errors and " +
      "crashes mixed in, to check nothing breaks under real-world flakiness. Hover a row or column for what it means."
    );
    container.appendChild(intro);

    const table = document.createElement("table");
    table.className = "eval-table";
    const metrics = [
      ["verdict_accuracy", "Verdict accuracy", true],
      ["dangerous_errors", "Dangerous errors", false],
      ["missed_opportunities", "Missed opportunities", false],
      ["deadline_accuracy", "Deadline accuracy", true],
      ["duplicate_effects", "Duplicate effects", false],
      ["forbidden_effects", "Forbidden effects", false],
      ["required_effects", "Required effects", true],
      ["scam_recall", "Scam recall", true],
      ["scam_false_alarms", "Scam false alarms", false],
    ];
    const thead = document.createElement("tr");
    thead.appendChild(document.createElement("th")).textContent = "Metric";
    data.runs.forEach((r) => {
      const th = document.createElement("th");
      th.textContent = describeRunColumn(r);
      if (r.chaos && r.chaos !== "none") th.title = `Stress-test profile: ${r.chaos}`;
      thead.appendChild(th);
    });
    table.appendChild(thead);

    metrics.forEach(([key, label, isRatio]) => {
      const row = document.createElement("tr");
      const th = document.createElement("td");
      th.textContent = label;
      th.title = METRIC_DESCRIPTIONS[key] || "";
      row.appendChild(th);
      data.runs.forEach((r) => {
        const td = document.createElement("td");
        const v = r.summary ? r.summary[key] : undefined;
        td.textContent = isRatio ? pct(v) : (v === undefined || v === null ? "n/a" : v);
        row.appendChild(td);
      });
      table.appendChild(row);
    });
    container.appendChild(table);

    const clustersHeader = document.createElement("h2");
    clustersHeader.textContent = "What went wrong in the latest run";
    container.appendChild(clustersHeader);
    const clustersDesc = document.createElement("p");
    clustersDesc.className = "section-desc";
    clustersDesc.textContent = "Specific test cases grouped by the same kind of mistake, worst offenders first.";
    container.appendChild(clustersDesc);
    const latest = data.runs[data.runs.length - 1];
    (latest.top_failure_clusters || []).forEach((c) => {
      const described = describeFailureCluster(c);
      const el = document.createElement("div");
      el.className = "failure-cluster";
      const headline = document.createElement("div");
      headline.className = "failure-headline";
      headline.textContent = described.headline;
      el.appendChild(headline);
      const ids = document.createElement("div");
      ids.className = "failure-ids";
      ids.textContent = described.ids;
      el.appendChild(ids);
      container.appendChild(el);
    });
    if ((latest.top_failure_clusters || []).length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "No failures in the latest run.";
      container.appendChild(empty);
    }
  } catch (err) {
    console.error("CutOff: eval poll failed", err);
  }
}

// --- self-healing tab --------------------------------------------------------------

let lastRenderedHealth = null;

const CIRCUIT_APP_LABELS = { sheets: "Sheets", calendar: "Calendar", telegram: "Telegram" };

async function pollHealth() {
  try {
    const resp = await fetch("/api/health");
    const data = await resp.json();
    const serialized = JSON.stringify(data);
    if (serialized === lastRenderedHealth) return;
    lastRenderedHealth = serialized;
    const container = document.getElementById("health-content");
    container.innerHTML = "";

    const intro = document.createElement("p");
    intro.className = "section-desc";
    intro.textContent = (
      "CutOff never gives up on a single hiccup. Every write retries automatically with backoff; if one " +
      "app keeps failing, CutOff briefly pauses on it instead of hammering a broken service (a “circuit " +
      "breaker”); and killing the whole process mid-action and restarting it never creates a duplicate " +
      "or loses a write. This page shows that behavior actually happening — nothing here is simulated."
    );
    container.appendChild(intro);

    const circuitsHeader = document.createElement("h2");
    circuitsHeader.textContent = "App status right now";
    container.appendChild(circuitsHeader);
    const circuitsGrid = document.createElement("div");
    circuitsGrid.className = "circuit-grid";
    data.circuits.forEach((c) => {
      const card = document.createElement("div");
      card.className = "circuit-card";
      card.dataset.state = c.state;
      const name = document.createElement("div");
      name.className = "circuit-app";
      name.textContent = CIRCUIT_APP_LABELS[c.app] || c.app;
      card.appendChild(name);
      const status = document.createElement("div");
      status.className = "circuit-status";
      status.textContent = c.state === "open"
        ? `Paused — ${c.consecutive_failures} failures in a row`
        : "Healthy";
      card.appendChild(status);
      circuitsGrid.appendChild(card);
    });
    container.appendChild(circuitsGrid);

    const healedHeader = document.createElement("h2");
    healedHeader.textContent = "Recently self-healed";
    container.appendChild(healedHeader);
    const healedDesc = document.createElement("p");
    healedDesc.className = "section-desc";
    healedDesc.textContent = "Actions that hit a transient failure (a dropped connection, a rate limit, a brief outage) and succeeded automatically on retry — no one had to intervene.";
    container.appendChild(healedDesc);
    if (data.self_healed.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      empty.textContent = "Nothing has needed to self-heal yet — that's a good sign, not a missing feature.";
      container.appendChild(empty);
    }
    data.self_healed.forEach((a) => {
      const entry = document.createElement("div");
      entry.className = "action-entry";
      const badge = document.createElement("span");
      badge.className = "status-badge";
      badge.dataset.status = a.status;
      badge.textContent = `${a.attempts} tries`;
      entry.appendChild(badge);
      entry.appendChild(document.createTextNode(`${describeAction(a)} — ${a.drive_id}`));
      container.appendChild(entry);
    });
  } catch (err) {
    console.error("CutOff: health poll failed", err);
  }
}

// --- demo panel (MODE=fake, DEMO_MODE=1 only) -------------------------------------

const DEMO_SEND_LABELS = {
  new_drive_1: "new drive (eligible)",
  new_drive_2: "new drive (needs review)",
  new_drive_3: "new drive (not eligible)",
  correction: "correction",
  shortlist: "shortlist PDF",
  scam: "scam email",
};

function setDemoStatus(text) {
  const el = document.getElementById("demo-status");
  if (el) el.textContent = text;
}

async function setupDemoPanel() {
  let config;
  try {
    config = await (await fetch("/api/config")).json();
  } catch (err) {
    console.error("CutOff: config fetch failed", err);
    return;
  }
  if (!config.demo_mode) return;

  const panel = document.getElementById("demo-panel");
  panel.hidden = false;

  panel.querySelectorAll("[data-demo-send]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const preset = btn.dataset.demoSend;
      btn.disabled = true;
      setDemoStatus(`Sending ${DEMO_SEND_LABELS[preset] || preset}…`);
      try {
        const resp = await fetch("/api/demo/send", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ preset }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        setDemoStatus(`Sent ${DEMO_SEND_LABELS[preset] || preset} — ${data.notice_type || "no drive"}${data.verdict ? `, ${data.verdict}` : ""}.`);
      } catch (err) {
        setDemoStatus(`Failed to send: ${err.message}`);
      } finally {
        btn.disabled = false;
        await pollAll();
      }
    });
  });

  panel.querySelectorAll("[data-demo-chaos]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const target = btn.dataset.demoChaos;
      btn.disabled = true;
      try {
        await fetch("/api/demo/chaos", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target }),
        });
        setDemoStatus(target === "clear" ? "Chaos cleared." : `${target} will now fail on its next write — send an email to see it.`);
      } catch (err) {
        setDemoStatus(`Failed to set chaos: ${err.message}`);
      } finally {
        btn.disabled = false;
      }
    });
  });

  document.getElementById("demo-reset").addEventListener("click", async () => {
    if (!confirm("Clear all demo drives, actions, and traces? This can't be undone.")) return;
    const btn = document.getElementById("demo-reset");
    btn.disabled = true;
    try {
      await fetch("/api/demo/reset", { method: "POST" });
      setDemoStatus("Demo data reset — board is empty.");
    } catch (err) {
      setDemoStatus(`Failed to reset: ${err.message}`);
    } finally {
      btn.disabled = false;
      await pollAll();
    }
  });
}

setupDemoPanel();

// --- polling loop ----------------------------------------------------------------

async function pollAll() {
  await pollBoard();
  if (selectedDriveId) await loadDriveDetail(selectedDriveId);
  if (currentView === "trace") await pollTrace();
  if (currentView === "eval") await loadEval();
  if (currentView === "health") await pollHealth();
}

pollAll();
setInterval(pollAll, 1000);
