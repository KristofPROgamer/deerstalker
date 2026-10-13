(() => {
  "use strict";

  const rootsList = document.querySelector("#roots-list");
  const intentGrid = document.querySelector("#intent-grid");
  const form = document.querySelector("#scan-form");
  const customQuery = document.querySelector("#custom-query");
  const beginButton = document.querySelector("#begin-button");
  const chooseRootButton = document.querySelector("#choose-root-button");
  const choosePortableStageButton = document.querySelector("#choose-portable-stage-button");
  const portableStageDirectory = document.querySelector("#portable-stage-directory");
  const portableStageNote = document.querySelector("#portable-stage-note");
  const alertBox = document.querySelector("#form-alert");
  const toast = document.querySelector("#toast");
  const counts = {
    roots: document.querySelector("#root-count"),
    intents: document.querySelector("#intent-count"),
    custom: document.querySelector("#custom-status"),
  };

  let apiAvailable = true;
  let roots = [];
  let intents = [];
  let portableStageSettings = null;
  const selectedRootPaths = new Set();
  const selectedIntentIds = new Set();

  const mockRoots = [
    { path: "C:\\Users\\You\\Documents", label: "Documents", kind: "folder" },
    { path: "C:\\Users\\You\\Pictures", label: "Pictures", kind: "folder" },
    { path: "C:\\", label: "System drive", kind: "drive" },
  ];

  const mockIntents = [
    { id: "sensitive_docs", label: "Sensitive documents", description: "IDs, medical records, and personal paperwork.", icon: "◫" },
    { id: "financial", label: "Financial records", description: "Statements, receipts, budgets, and wallet information.", icon: "$" },
    { id: "credentials", label: "Credentials", description: "Passwords, API keys, tokens, and secrets.", icon: "⌘" },
    { id: "people_photos", label: "Photos of people", description: "Portraits, groups, selfies, and ID-style images.", icon: "◉" },
    { id: "legal", label: "Legal & contracts", description: "Agreements, filings, and legal correspondence.", icon: "§" },
    { id: "work_docs", label: "Work & professional", description: "Resumes, plans, presentations, and reports.", icon: "▤" },
    { id: "memories", label: "Media & memories", description: "Events, screenshots, creative work, and personal media.", icon: "✦" },
    { id: "cleanup", label: "Cleanup leads", description: "Large, temporary, or reclaimable files.", icon: "⌫" },
    { id: "forgotten", label: "Old & forgotten", description: "Stale files and traces of retired software.", icon: "◌" },
    { id: "interesting", label: "Interesting Things", description: "Anomalies, mismatches, and anything worth a closer look.", icon: "⌕" },
  ];

  async function fetchJson(url) {
    const response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) throw new Error(await responseText(response));
    return response.json();
  }

  async function responseText(response) {
    try {
      const body = await response.json();
      return body.detail || `Request failed (${response.status})`;
    } catch {
      return `Request failed (${response.status})`;
    }
  }

  async function loadChoices() {
    try {
      const [rootResponse, intentResponse, stageResponse] = await Promise.all([
        fetchJson("/api/roots"),
        fetchJson("/api/intents"),
        fetchJson("/api/portable-stage-settings"),
      ]);
      roots = Array.isArray(rootResponse.roots) ? rootResponse.roots : [];
      intents = Array.isArray(intentResponse.intents) ? intentResponse.intents : [];
      portableStageSettings = stageResponse;
      if (!roots.length || !intents.length) throw new Error("The investigation desk returned no choices.");
    } catch {
      apiAvailable = false;
      roots = mockRoots;
      intents = mockIntents;
      showToast("Showing a demonstration case. Start the app server to inspect your own files.");
    }
    if (roots[0] && roots[0].path) selectedRootPaths.add(roots[0].path);
    renderRoots();
    renderIntents();
    renderPortableStageSettings();
    updateSummary();
  }

  function renderRoots() {
    rootsList.replaceChildren();
    roots.forEach((root) => {
      const rootMark = root.kind === "drive" ? "DRV"
        : root.kind === "removable" ? "USB"
          : root.kind === "portable" ? "MTP"
            : "DIR";
      const option = document.createElement("label");
      option.className = "root-option";
      option.innerHTML = `
        <input type="checkbox" name="roots" value="${escapeAttribute(root.path)}" ${selectedRootPaths.has(root.path) ? "checked" : ""}>
        <span class="root-icon" aria-hidden="true">${rootMark}</span>
        <span class="root-copy">
          <span class="root-label">${escapeHtml(root.label || root.path)}</span>
          <span class="root-path">${escapeHtml(root.path)}</span>
        </span>`;
      rootsList.append(option);
    });
  }

  function renderIntents() {
    intentGrid.replaceChildren();
    intents.forEach((intent) => {
      const card = document.createElement("button");
      const isInteresting = intent.id === "interesting";
      card.type = "button";
      card.className = `intent-card${isInteresting ? " intent-card--interesting" : ""}`;
      card.dataset.intentId = intent.id;
      card.setAttribute("aria-pressed", "false");
      card.innerHTML = `
        <span class="intent-icon" aria-hidden="true">${escapeHtml(intent.icon || "•")}</span>
        <span class="selection-mark" aria-hidden="true">✓</span>
        <span class="intent-title">${escapeHtml(intent.label)}</span>
        <span class="intent-description">${escapeHtml(intent.description)}</span>`;
      card.addEventListener("click", () => toggleIntent(intent.id, card));
      intentGrid.append(card);
    });
  }

  function toggleIntent(intentId, card) {
    if (selectedIntentIds.has(intentId)) {
      selectedIntentIds.delete(intentId);
      card.setAttribute("aria-pressed", "false");
    } else {
      selectedIntentIds.add(intentId);
      card.setAttribute("aria-pressed", "true");
    }
    updateSummary();
  }

  function updateSummary() {
    counts.roots.textContent = String(selectedRoots().length);
    counts.intents.textContent = String(selectedIntentIds.size);
    counts.custom.textContent = customQuery.value.trim() ? "Yes" : "No";
  }

  function selectedRoots() {
    return [...document.querySelectorAll('input[name="roots"]:checked')].map((input) => input.value);
  }

  rootsList.addEventListener("change", (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.name !== "roots") return;
    if (input.checked) selectedRootPaths.add(input.value);
    else selectedRootPaths.delete(input.value);
    updateSummary();
  });

  chooseRootButton.addEventListener("click", chooseRoot);
  choosePortableStageButton.addEventListener("click", choosePortableStageFolder);

  async function chooseRoot() {
    hideAlert();
    chooseRootButton.disabled = true;
    chooseRootButton.textContent = "Opening folder picker...";
    try {
      const response = await fetch("/api/choose-folder", {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(await responseText(response));
      const payload = await response.json();
      if (!payload.selected) {
        showToast("No location was added.");
        return;
      }
      const root = payload.root;
      if (!root || typeof root.path !== "string" || !root.path.trim()) {
        throw new Error("The folder picker did not return a usable location.");
      }
      const existing = roots.find((candidate) => samePath(candidate.path, root.path));
      if (!existing) roots.unshift(root);
      selectedRootPaths.add(existing ? existing.path : root.path);
      renderRoots();
      updateSummary();
      showToast(`${root.label || root.path} was added to this case.`);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Could not open the folder picker.";
      showAlert(`${message} Choose a suggested location, or start the local app server and try again.`);
    } finally {
      chooseRootButton.disabled = false;
      chooseRootButton.innerHTML = '<span aria-hidden="true">+</span> Add a folder or drive';
    }
  }

  function renderPortableStageSettings() {
    if (!portableStageSettings) {
      portableStageDirectory.textContent = "Start the local app to choose the phone staging folder.";
      portableStageNote.textContent = "Connected-phone scans are unavailable in demonstration mode.";
      choosePortableStageButton.disabled = true;
      return;
    }
    const directory = portableStageSettings.directory || "the default local staging folder";
    const batchGiB = Number(portableStageSettings.batch_gib) || 5;
    portableStageDirectory.textContent = directory;
    portableStageNote.textContent = `Phone files are copied, examined, and cleared in ${batchGiB} GiB batches${portableStageSettings.uses_default ? " (default location)" : ""}.`;
    choosePortableStageButton.disabled = false;
  }

  async function choosePortableStageFolder() {
    hideAlert();
    choosePortableStageButton.disabled = true;
    choosePortableStageButton.textContent = "Opening folder picker...";
    try {
      const response = await fetch("/api/choose-portable-stage-folder", {
        method: "POST",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(await responseText(response));
      const payload = await response.json();
      if (!payload.selected) {
        showToast("The phone staging folder was not changed.");
        return;
      }
      portableStageSettings = payload;
      renderPortableStageSettings();
      showToast(payload.persisted ? "Phone staging folder saved." : "Phone staging folder set for this session.");
    } catch (error) {
      showAlert(error instanceof Error ? error.message : "Could not choose the phone staging folder.");
    } finally {
      choosePortableStageButton.textContent = "Choose staging folder";
      if (portableStageSettings) choosePortableStageButton.disabled = false;
    }
  }

  function samePath(left, right) {
    return String(left).replace(/[\\/]+$/, "").toLocaleLowerCase() === String(right).replace(/[\\/]+$/, "").toLocaleLowerCase();
  }

  form.addEventListener("input", updateSummary);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    hideAlert();
    const rootsToScan = selectedRoots();
    const query = customQuery.value.trim();
    if (!rootsToScan.length) return showAlert("Mark at least one location before opening the case.");
    if (!selectedIntentIds.size && !query) return showAlert("Mark an investigation lead or write your own description.");

    beginButton.disabled = true;
    beginButton.textContent = "Opening the case…";
    try {
      let jobId = "demo-case";
      if (apiAvailable) {
        const response = await fetch("/api/scan", {
          method: "POST",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify({ roots: rootsToScan, intent_ids: [...selectedIntentIds], custom_query: query || null }),
        });
        if (!response.ok) throw new Error(await responseText(response));
        const payload = await response.json();
        if (!payload.job_id) throw new Error("The investigation did not return a case number.");
        jobId = payload.job_id;
      }
      const demoFlag = apiAvailable ? "" : "&demo=1";
      window.location.assign(`progress.html?job_id=${encodeURIComponent(jobId)}${demoFlag}`);
    } catch (error) {
      showAlert(error instanceof Error ? error.message : "Could not open the case.");
      beginButton.disabled = false;
      beginButton.innerHTML = '<span aria-hidden="true">⌕</span> Begin the investigation';
    }
  });

  function showAlert(message) {
    alertBox.textContent = message;
    alertBox.hidden = false;
  }

  function hideAlert() {
    alertBox.hidden = true;
    alertBox.textContent = "";
  }

  function showToast(message) {
    toast.textContent = message;
    toast.hidden = false;
    window.setTimeout(() => { toast.hidden = true; }, 5500);
  }

  function escapeHtml(value) {
    const element = document.createElement("span");
    element.textContent = String(value ?? "");
    return element.innerHTML;
  }

  function escapeAttribute(value) {
    return escapeHtml(value).replaceAll('"', "&quot;");
  }

  loadChoices();
})();
