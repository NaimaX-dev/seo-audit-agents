/* static/js/leads.js - Lead Discovery page (Agent 5).
   - select all / selected count / enable "Run SEO Audit"
   - spinner while Google Maps is being searched
   - live audit status in the table while audits are running             */
(function () {
  "use strict";

  const auditForm = document.getElementById("audit-leads-form");
  const selectAll = document.getElementById("select-all");
  const runBtn = document.getElementById("run-audit");
  const countEl = document.getElementById("lead-count");
  const boxes = () => Array.from(document.querySelectorAll(".lead-check:not(:disabled)"));

  function refreshSelection() {
    const all = boxes();
    const n = all.filter((b) => b.checked).length;
    if (countEl) countEl.textContent = n + " selected";
    if (runBtn) runBtn.disabled = n === 0;
    if (selectAll) {
      selectAll.checked = all.length > 0 && n === all.length;
      selectAll.indeterminate = n > 0 && n < all.length;
    }
  }
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      boxes().forEach((b) => { b.checked = selectAll.checked; });
      refreshSelection();
    });
  }
  document.querySelectorAll(".lead-check").forEach((b) => b.addEventListener("change", refreshSelection));
  refreshSelection();

  if (auditForm && runBtn) {
    auditForm.addEventListener("submit", () => {
      runBtn.disabled = true;
      runBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Starting...';
    });
  }

  const discoverForm = document.getElementById("discover-form");
  const discoverBtn = document.getElementById("discover-submit");
  if (discoverForm && discoverBtn) {
    discoverForm.addEventListener("submit", () => {
      discoverBtn.disabled = true;
      discoverBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" aria-hidden="true"></span>Browsing Google Maps - this can take a minute or two...';
    });
  }

  /* ---- live status ------------------------------------------------- */
  const panel = document.getElementById("leads-panel");
  if (!panel || panel.dataset.poll !== "true") return;

  const CLASSES = { "Not audited": "pending", Queued: "queued", Auditing: "running", Audited: "completed", Failed: "failed" };
  const ACTIVE = new Set(["Queued", "Auditing"]);
  const progressUrl = (id) => "/audit/" + id;
  const resultsUrl = (id) => "/audit/" + id + "/results";

  function pill(status) {
    const span = document.createElement("span");
    span.className = "status-pill status-" + (CLASSES[status] || "pending");
    const dot = document.createElement("span");
    dot.className = "dot";
    span.append(dot, document.createTextNode(status));
    return span;
  }

  function paint(cell, info) {
    cell.dataset.status = info.status;
    cell.textContent = "";
    const p = pill(info.status);
    if (info.job_id && info.status !== "Not audited") {
      const a = document.createElement("a");
      a.href = info.status === "Audited" || info.status === "Failed" ? resultsUrl(info.job_id) : progressUrl(info.job_id);
      a.appendChild(p);
      cell.appendChild(a);
    } else {
      cell.appendChild(p);
    }
  }

  async function poll() {
    try {
      const res = await fetch(panel.dataset.statusUrl, { headers: { Accept: "application/json" } });
      if (!res.ok) throw new Error(res.status);
      const data = await res.json();
      let stillActive = false;
      document.querySelectorAll("tr[data-lead-id]").forEach((row) => {
        const info = data[row.dataset.leadId];
        const cell = row.querySelector(".lead-status-cell");
        if (!info || !cell) return;
        if (cell.dataset.status !== info.status) {
          paint(cell, info);
          const box = row.querySelector(".lead-check");
          if (box) box.disabled = ACTIVE.has(info.status);
        }
        if (ACTIVE.has(info.status)) stillActive = true;
      });
      refreshSelection();
      if (stillActive) setTimeout(poll, 4000);
    } catch (err) {
      setTimeout(poll, 8000);
    }
  }
  setTimeout(poll, 3000);
})();
