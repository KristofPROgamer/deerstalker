(() => {
  "use strict";

  const params = new URLSearchParams(window.location.search);
  const jobId = params.get("job_id");
  const demoMode = params.get("demo") === "1";
  const matchThreshold = 0.5;
  let caseData = null;
  let findings = [];

  const elements = {
    caseNumber: document.querySelector("#case-number"),
    description: document.querySelector("#findings-description"),
    stamp: document.querySelector("#summary-stamp"),
    interestingPanel: document.querySelector("#interesting-panel"),
    interestingList: document.querySelector("#interesting-list"),
    total: document.querySelector("#total-count"),
    finding: document.querySelector("#finding-count"),
    tag: document.querySelector("#tag-count"),
    search: document.querySelector("#search-findings"),
    intent: document.querySelector("#intent-filter"),
    sort: document.querySelector("#sort-findings"),
    grid: document.querySelector("#results-grid"),
    report: document.querySelector("#report-content"),
    openFolder: document.querySelector("#open-folder-button"),
    toast: document.querySelector("#toast"),
  };

  if (!jobId) {
    showFatal("No case number was supplied. Return to the investigation desk to open a case.");
  } else {
    elements.caseNumber.textContent = `Case ${shortId(jobId)}`;
    elements.search.addEventListener("input", renderFindings);
    elements.intent.addEventListener("change", renderFindings);
    elements.sort.addEventListener("change", renderFindings);
    elements.openFolder.addEventListener("click", openCaseFolder);
    loadCase();
  }

  async function loadCase() {
    try {
      const resultPayload = demoMode ? demoResults() : await fetchJson(`/api/scan/${encodeURIComponent(jobId)}/results`);
      const reportText = demoMode ? demoReport() : await fetchReport();
      caseData = resultPayload;
      findings = consolidateFindings(resultPayload.results || [], resultPayload.files || {});
      renderSummary();
      populateIntentFilter();
      renderInteresting();
      renderFindings();
      renderReport(reportText);
    } catch (error) {
      showFatal(error instanceof Error ? error.message : "Could not open this case file.");
    }
  }

  async function fetchJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(await responseMessage(response));
    return response.json();
  }

  async function fetchReport() {
    const response = await fetch(`/api/scan/${encodeURIComponent(jobId)}/report`, { headers: { Accept: "text/markdown" } });
    if (!response.ok) throw new Error(await responseMessage(response));
    return response.text();
  }

  function consolidateFindings(results, files) {
    const preferred = new Map();
    for (const result of results) {
      const key = `${result.file_id}::${result.intent_id ?? "custom"}`;
      const previous = preferred.get(key);
      if (!previous || rank(result) > rank(previous)) preferred.set(key, result);
    }
    return [...preferred.values()]
      .filter((result) => Number(result.relevance_score) >= matchThreshold || result.flagged_interesting)
      .map((analysis) => ({ analysis, file: files[analysis.file_id] || null }));
  }

  function rank(result) {
    return (result.pass_type === "detailed" ? 10_000_000_000 : 0) + Number(result.created_at || 0);
  }

  function failedSources() {
    return Object.values((caseData && caseData.files) || {})
      .filter((file) => file && file.status === "error");
  }

  function renderSummary() {
    const counts = { total: 0, scanned: 0, matched: 0, errored: 0, ...((caseData.job && caseData.job.counts) || {}) };
    const sourceErrors = failedSources();
    const examined = Number(counts.scanned) || 0;
    const topTag = topTags()[0] || "—";
    elements.total.textContent = String(examined);
    elements.finding.textContent = String(findings.length);
    elements.tag.textContent = topTag;
    elements.tag.title = topTag === "—" ? "No tags" : `Most common tag: ${topTag}`;
    elements.stamp.innerHTML = `<b>${escapeHtml(examined)}</b> files examined<br><b>${escapeHtml(findings.length)}</b> findings marked<br><b>${escapeHtml(counts.errored)}</b> errors recorded`;
    elements.description.textContent = !examined && sourceErrors.length
      ? `No files could be examined. ${sourceErrors[0].error_message || "The selected location could not be read."}`
      : findings.length
      ? `${findings.length} finding${findings.length === 1 ? "" : "s"} filed from this local investigation. Filter the evidence or read the written account.`
      : "The investigation completed without any findings that met the case threshold.";
  }

  function topTags() {
    const tally = new Map();
    findings.forEach(({ analysis }) => (analysis.tags || []).forEach((tag) => tally.set(tag, (tally.get(tag) || 0) + 1)));
    return [...tally.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).map(([tag]) => tag);
  }

  function populateIntentFilter() {
    const labels = new Map();
    findings.forEach(({ analysis }) => labels.set(intentLabel(analysis.intent_id), analysis.intent_id ?? "custom"));
    for (const [label, id] of [...labels.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = label;
      elements.intent.append(option);
    }
  }

  function renderInteresting() {
    const interesting = findings.filter(({ analysis }) => analysis.flagged_interesting || analysis.intent_id === "interesting");
    if (!interesting.length) {
      elements.interestingPanel.hidden = true;
      return;
    }
    elements.interestingPanel.hidden = false;
    elements.interestingList.replaceChildren();
    interesting.slice(0, 5).forEach(({ analysis, file }) => {
      const item = document.createElement("div");
      item.className = "interesting-item";
      item.innerHTML = `<b>${escapeHtml(displayPath(file, analysis))}</b><br>${escapeHtml(analysis.reasoning || analysis.summary || "Flagged for a closer look.")}`;
      elements.interestingList.append(item);
    });
  }

  function renderFindings() {
    const query = elements.search.value.trim().toLowerCase();
    const filter = elements.intent.value;
    const sorted = findings
      .filter(({ analysis, file }) => {
        if (filter !== "all" && (analysis.intent_id ?? "custom") !== filter) return false;
        if (!query) return true;
        const haystack = [displayPath(file, analysis), analysis.summary, analysis.reasoning, ...(analysis.tags || [])].join(" ").toLowerCase();
        return haystack.includes(query);
      })
      .sort((left, right) => compareFindings(left, right, elements.sort.value));

    elements.grid.replaceChildren();
    if (!sorted.length) {
      const sourceErrors = failedSources();
      const scanned = Number((caseData && caseData.job && caseData.job.counts && caseData.job.counts.scanned) || 0);
      if (!scanned && sourceErrors.length) {
        const message = sourceErrors[0].error_message || "The selected location could not be read.";
        elements.grid.innerHTML = `<div class="empty-state"><strong>No files were scanned.</strong><span>${escapeHtml(message)} Keep the phone unlocked and connected, then start a new case.</span></div>`;
      } else {
        elements.grid.innerHTML = '<div class="empty-state">No filed evidence matches those filters. Try another phrase or broaden the investigation lead.</div>';
      }
      return;
    }
    sorted.forEach((finding) => elements.grid.append(renderCard(finding)));
  }

  function compareFindings(left, right, sort) {
    if (sort === "path") return displayPath(left.file, left.analysis).localeCompare(displayPath(right.file, right.analysis));
    if (sort === "intent") return intentLabel(left.analysis.intent_id).localeCompare(intentLabel(right.analysis.intent_id));
    return Number(right.analysis.relevance_score) - Number(left.analysis.relevance_score) || displayPath(left.file, left.analysis).localeCompare(displayPath(right.file, right.analysis));
  }

  function renderCard({ analysis, file }) {
    const image = Boolean(file && (file.mime_type || "").startsWith("image/"));
    const card = document.createElement("article");
    card.className = `result-card${analysis.flagged_interesting || analysis.intent_id === "interesting" ? " result-card--interesting" : ""}`;
    const tags = (analysis.tags || []).slice(0, 5).map((tag) => `<span class="tag">${escapeHtml(tag)}</span>`).join("");
    card.innerHTML = `
      <div class="file-glyph${image ? " file-glyph--image" : ""}" aria-hidden="true">${image ? "IMG" : escapeHtml(extension(file))}</div>
      <div>
        <p class="result-path">${escapeHtml(displayPath(file, analysis))}</p>
        <p class="result-reasoning">${escapeHtml(analysis.reasoning || analysis.summary || "No short rationale was recorded.")}</p>
        <div class="result-meta"><span class="score">${Math.round(Number(analysis.relevance_score || 0) * 100)}% relevance</span><span class="tag tag--brass">${escapeHtml(intentLabel(analysis.intent_id))}</span></div>
        ${tags ? `<div class="tag-list">${tags}</div>` : ""}
      </div>`;
    return card;
  }

  function renderReport(markdown) {
    elements.report.innerHTML = markdownToSafeHtml(markdown);
  }

  async function openCaseFolder() {
    const path = caseData && caseData.job && caseData.job.output_folder;
    if (!path) return showToast("This case does not include an output folder.", "error");
    if (demoMode) return showToast("In the desktop app, this opens the local case folder.");
    elements.openFolder.disabled = true;
    try {
      const response = await fetch("/api/open-folder", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ path }),
      });
      const payload = await response.json();
      if (!response.ok || !payload.opened) throw new Error(payload.error || payload.detail || "Could not open the case folder.");
      showToast("Case folder opened.");
    } catch (error) {
      showToast(error instanceof Error ? error.message : "Could not open the case folder.", "error");
    } finally {
      elements.openFolder.disabled = false;
    }
  }

  function showFatal(message) {
    elements.description.textContent = message;
    elements.grid.innerHTML = `<div class="empty-state">${escapeHtml(message)}</div>`;
    elements.report.textContent = "The written report is unavailable.";
  }

  function intentLabel(intentId) {
    if (intentId === null || intentId === undefined) return "Custom search";
    const known = {
      sensitive_docs: "Sensitive documents", financial: "Financial records", credentials: "Credentials",
      people_photos: "Photos of people", legal: "Legal & contracts", work_docs: "Work & professional",
      memories: "Media & memories", cleanup: "Cleanup leads", forgotten: "Old & forgotten", interesting: "Interesting Things",
    };
    return known[intentId] || intentId;
  }

  function displayPath(file, analysis) { return (file && (file.display_path || file.path)) || analysis.file_id || "Unknown file"; }
  function extension(file) { return file && file.ext ? file.ext.replace(".", "").slice(0, 4).toUpperCase() : "FILE"; }
  function shortId(value) { return value.length > 12 ? `${value.slice(0, 8)}…` : value; }

  function markdownToSafeHtml(markdown) {
    const escape = (value) => escapeHtml(value);
    return String(markdown || "No written report was produced.").split("\n").map((line) => {
      if (line.startsWith("### ")) return `<h3>${escape(line.slice(4))}</h3>`;
      if (line.startsWith("## ")) return `<h2>${escape(line.slice(3))}</h2>`;
      if (line.startsWith("# ")) return `<h1>${escape(line.slice(2))}</h1>`;
      if (line.startsWith("- ")) return `<div>• ${escape(line.slice(2))}</div>`;
      return line ? `<div>${escape(line)}</div>` : "<br>";
    }).join("");
  }

  async function responseMessage(response) {
    try { const body = await response.json(); return body.detail || `Request failed (${response.status})`; }
    catch { return `Request failed (${response.status})`; }
  }

  function showToast(message, kind = "info") {
    elements.toast.textContent = message;
    elements.toast.dataset.kind = kind;
    elements.toast.hidden = false;
    window.setTimeout(() => { elements.toast.hidden = true; }, 5000);
  }

  function escapeHtml(value) {
    const element = document.createElement("span");
    element.textContent = String(value ?? "");
    return element.innerHTML;
  }

  function demoResults() {
    const files = {
      "file-passport": { id: "file-passport", display_path: "Documents/Travel/passport_scan.pdf", path: "Documents/Travel/passport_scan.pdf", ext: ".pdf", mime_type: "application/pdf" },
      "file-photo": { id: "file-photo", display_path: "Pictures/2019/summer picnic.jpg", path: "Pictures/2019/summer picnic.jpg", ext: ".jpg", mime_type: "image/jpeg" },
      "file-sheet": { id: "file-sheet", display_path: "Archive/2018/budget.xlsx", path: "Archive/2018/budget.xlsx", ext: ".xlsx", mime_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" },
    };
    return {
      job: { id: jobId, status: "done", output_folder: "C:\\Deerstalker\\output\\demo-case", counts: { total: 18, scanned: 18, matched: 4, errored: 0 } },
      files,
      results: [
        { id: "fast-1", file_id: "file-passport", intent_id: "sensitive_docs", pass_type: "fast", relevance_score: 0.78, tags: ["passport", "identity"], summary: "Scanned passport page.", reasoning: "Passport layout and identity details are visible.", flagged_interesting: false, created_at: 1 },
        { id: "detail-1", file_id: "file-passport", intent_id: "sensitive_docs", pass_type: "detailed", relevance_score: 0.92, tags: ["passport", "identity", "travel"], summary: "Scanned passport identity page.", reasoning: "Contains a passport-style identity page with visible personal details.", flagged_interesting: false, created_at: 2 },
        { id: "detail-2", file_id: "file-photo", intent_id: "people_photos", pass_type: "detailed", relevance_score: 0.84, tags: ["people", "outdoors"], summary: "Outdoor group photograph.", reasoning: "Several people appear in a casual outdoor gathering.", flagged_interesting: false, created_at: 2 },
        { id: "detail-3", file_id: "file-sheet", intent_id: "financial", pass_type: "detailed", relevance_score: 0.73, tags: ["budget", "2018"], summary: "Old budget workbook.", reasoning: "Workbook contains dated budget categories and financial totals.", flagged_interesting: true, created_at: 2 },
      ],
    };
  }

  function demoReport() {
    return `# Deerstalker Investigation Report\n\n## Summary\n\n- Files examined: 18\n- Matched files: 3\n- File errors: 0\n\n## Interesting Things\n\n- Archive/2018/budget.xlsx\n  - Why: A dated budget workbook was found in an archive folder.\n\n## Sensitive Documents\n\n- Documents/Travel/passport_scan.pdf\n  - Why: Passport-style identity page with visible personal details.`;
  }
})();
