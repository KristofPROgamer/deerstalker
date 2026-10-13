(() => {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const jobId = params.get("job_id");
  const demoMode = params.get("demo") === "1";
  const pollMs = 1000;
  let timer = null;
  let demoStep = 0;

  const elements = {
    caseNumber: document.querySelector("#case-number"),
    description: document.querySelector("#status-description"),
    word: document.querySelector("#stage-title"),
    detail: document.querySelector("#stage-detail"),
    chip: document.querySelector("#status-chip"),
    track: document.querySelector(".progress-track"),
    fill: document.querySelector("#progress-fill"),
    note: document.querySelector("#progress-note"),
    scanned: document.querySelector("#scanned-count"),
    matched: document.querySelector("#matched-count"),
    errored: document.querySelector("#error-count"),
    currentFile: document.querySelector("#current-file"),
    cancel: document.querySelector("#cancel-button"),
    terminal: document.querySelector("#terminal-actions"),
    toast: document.querySelector("#toast"),
  };

  const statusCopy = {
    pending: ["Opening the case", "Preparing the investigation ledger."],
    scanning: ["Mapping the territory", "Listing the selected locations and opening any archives found along the way."],
    fast_pass: ["Reading the first clues", "Every reachable file is receiving a quick, careful first look."],
    detailed_pass: ["Inspecting the leads", "Promising findings are receiving a fuller second examination."],
    done: ["Case file complete", "The findings and written report are ready to review."],
    cancelled: ["Investigation paused", "The case was cancelled. Files already examined remain in the local case record."],
    error: ["The case needs attention", "The investigation could not continue. Review the case settings and try again."],
  };

  const demoStates = [
    { status: "scanning", counts: { total: 18, scanned: 2, matched: 0, errored: 0 }, current_file: "Documents/old correspondence" },
    { status: "fast_pass", counts: { total: 18, scanned: 8, matched: 2, errored: 0 }, current_file: "Pictures/summer picnic.jpg" },
    { status: "detailed_pass", counts: { total: 18, scanned: 18, matched: 4, errored: 0 }, current_file: "Archive/2018/budget.xlsx" },
    { status: "done", counts: { total: 18, scanned: 18, matched: 4, errored: 0 }, current_file: null },
  ];

  if (!jobId) {
    renderError("No case number was supplied. Return to the investigation desk and begin a new case.");
  } else {
    elements.caseNumber.textContent = `Case ${shortId(jobId)}`;
    elements.cancel.addEventListener("click", cancelInvestigation);
    poll();
  }

  async function poll() {
    try {
      const progress = demoMode ? nextDemoState() : await fetchProgress();
      render(progress);
      if (progress.status === "done") {
        stopPolling();
        window.setTimeout(() => {
          window.location.assign(`results.html?job_id=${encodeURIComponent(jobId)}${demoMode ? "&demo=1" : ""}`);
        }, demoMode ? 950 : 500);
      } else if (progress.status === "cancelled" || progress.status === "error") {
        stopPolling();
      } else {
        timer = window.setTimeout(poll, pollMs);
      }
    } catch (error) {
      renderError(error instanceof Error ? error.message : "Could not read this case's progress.");
    }
  }

  async function fetchProgress() {
    const response = await fetch(`/api/scan/${encodeURIComponent(jobId)}/progress`, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(await responseMessage(response));
    return response.json();
  }

  function nextDemoState() {
    const state = demoStates[Math.min(demoStep, demoStates.length - 1)];
    demoStep += 1;
    return { job_id: jobId, ...state };
  }

  function render(progress) {
    const status = progress.status || "pending";
    const counts = { total: 0, scanned: 0, matched: 0, errored: 0, ...(progress.counts || {}) };
    const [word, detail] = statusCopy[status] || statusCopy.pending;
    const percent = counts.total > 0 ? Math.min(100, Math.round((counts.scanned / counts.total) * 100)) : (status === "scanning" ? 4 : 0);

    elements.word.textContent = word;
    elements.detail.textContent = detail;
    elements.description.textContent = detail;
    elements.chip.textContent = status.replace("_", " ");
    elements.chip.dataset.status = status;
    elements.fill.style.width = `${percent}%`;
    elements.track.setAttribute("aria-valuenow", String(percent));
    elements.note.textContent = `${counts.scanned} of ${counts.total} files examined`;
    elements.scanned.textContent = String(counts.scanned);
    elements.matched.textContent = String(counts.matched);
    elements.errored.textContent = String(counts.errored);
    elements.currentFile.textContent = progress.current_file || (status === "done" ? "All records examined." : "Awaiting the next record…");

    const terminal = status === "done" || status === "cancelled" || status === "error";
    elements.cancel.hidden = terminal;
    if (terminal && status !== "done") {
      elements.terminal.hidden = false;
      elements.terminal.textContent = status === "cancelled"
        ? "This investigation was cancelled. You may return to the desk and begin another case."
        : "This investigation ended with an error. Check that the selected location and local model are available, then try again.";
    } else {
      elements.terminal.hidden = true;
    }
  }

  async function cancelInvestigation() {
    if (demoMode) {
      render({ status: "cancelled", counts: demoStates[2].counts, current_file: null });
      stopPolling();
      return;
    }
    elements.cancel.disabled = true;
    elements.cancel.textContent = "Cancellation requested…";
    try {
      const response = await fetch(`/api/scan/${encodeURIComponent(jobId)}/cancel`, { method: "POST", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(await responseMessage(response));
      showToast("Cancellation requested. The current file will finish before the case closes.");
      window.clearTimeout(timer);
      timer = window.setTimeout(poll, 250);
    } catch (error) {
      elements.cancel.disabled = false;
      elements.cancel.textContent = "Cancel this investigation";
      showToast(error instanceof Error ? error.message : "Could not cancel this investigation.", "error");
    }
  }

  function renderError(message) {
    render({ status: "error", counts: { total: 0, scanned: 0, matched: 0, errored: 1 }, current_file: null });
    elements.terminal.hidden = false;
    elements.terminal.textContent = message;
    stopPolling();
  }

  async function responseMessage(response) {
    try {
      const body = await response.json();
      return body.detail || `Request failed (${response.status})`;
    } catch {
      return `Request failed (${response.status})`;
    }
  }

  function shortId(value) { return value.length > 12 ? `${value.slice(0, 8)}…` : value; }
  function stopPolling() { if (timer) window.clearTimeout(timer); timer = null; }
  function showToast(message, kind = "info") {
    elements.toast.textContent = message;
    elements.toast.dataset.kind = kind;
    elements.toast.hidden = false;
    window.setTimeout(() => { elements.toast.hidden = true; }, 5000);
  }

  window.addEventListener("beforeunload", stopPolling);
})();
