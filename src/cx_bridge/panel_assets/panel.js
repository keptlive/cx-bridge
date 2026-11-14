"use strict";

const byId = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="cx-panel"]').content;
const labels = {
  codex: "Codex", claude: "Claude Code", agy: "Antigravity",
  sign_in: "Sign-in needed", approval: "Approval needed", missing: "Setup needed",
  unavailable: "Unavailable", unverified: "Unverified", connected: "Connected",
  disabled: "Disabled", running: "Running", succeeded: "Succeeded", failed: "Failed",
  timed_out: "Timed out", interrupted: "Interrupted", pending: "Pending",
  delivered: "Delivered", acknowledged: "Acknowledged",
};
const state = {
  view: "activity", job: null, activityRequest: 0, previousActivity: "",
  scanRequest: 0, scanned: false, scanning: false, installing: false,
  package: null, report: null, fileRequest: 0,
};

function node(tag, text = "", className = "") {
  const result = document.createElement(tag);
  result.textContent = text;
  result.className = className;
  return result;
}

function status(value) {
  const badge = node("span", labels[value] || value || "Unknown", "status");
  badge.dataset.state = value;
  return badge;
}

function notice(id, message = "", error = false) {
  byId(id).textContent = message;
  byId(id).classList.toggle("error", error);
}

async function request(route, payload) {
  const options = { headers: { "X-CX-Panel": token } };
  if (payload !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  }
  const response = await fetch(route, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Panel request failed.");
  return result;
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "" : date.toLocaleString();
}

function items(parent, entries, render, fallback) {
  parent.replaceChildren(...(entries.length ? entries.map(render) : [node("p", fallback, "empty muted")]));
}

function list(values, render) {
  const ul = node("ul");
  for (const value of values) ul.append(node("li", render(value)));
  return ul;
}

function jobEntry(task, opened) {
  const row = node("article", "", "entry");
  const heading = node("div", "", "entry-head");
  heading.append(node("h3", task.name || task.id), status(task.status));
  const meta = node("p", "", "meta");
  for (const item of [task.target, task.model, task.group, formatTime(task.started_at)].filter(Boolean)) {
    meta.append(node("span", item));
  }
  if (!state.job) {
    const inspect = node("button", "Focus job", "button small");
    inspect.type = "button";
    inspect.addEventListener("click", () => {
      state.job = task.id;
      refreshActivity();
    });
    heading.append(inspect);
  }
  row.append(heading, meta);
  const brief = task.summary;
  if (brief) row.append(node("p", brief, "body"));
  const details = node("details");
  details.dataset.job = task.id;
  details.open = Boolean(state.job || opened.has(task.id));
  const readCount = task.files_read.length;
  details.append(node("summary", "Job details · " + readCount + (readCount === 1 ? " file read" : " file reads")));
  details.append(node("p", task.id, "meta path"));
  if (task.session) details.append(node("p", "Session " + task.session, "meta path"));
  if (task.cwd) details.append(node("p", task.cwd, "meta path"));
  if (task.error) details.append(node("p", task.error.replaceAll("_", " "), "meta"));
  if (task.active_tools?.length) {
    details.append(node("h3", "In progress"), list(task.active_tools, (tool) => tool));
  }
  if (task.plan.length) {
    details.append(node("h3", "Plan"), list(task.plan, (step) => (step.completed ? "Done · " : "Open · ") + step.text));
  }
  if (readCount) {
    details.append(node("h3", "Observed file reads"), list(task.files_read, (read) =>
      read.path + " · " + read.tool + (read.evidence === "inferred_from_command" ? " (inferred)" : " (reported)")));
  }
  if (task.event_log && task.event_log !== "available") {
    details.append(node("p", "Event log " + task.event_log.replaceAll("_", " "), "meta"));
  }
  row.append(details);
  return row;
}

function messageEntry(message) {
  const row = node("article", "", "entry");
  const heading = node("div", "", "entry-head");
  heading.append(node("h3", (message.sender || "Unknown sender") + " → " + message.target), status(message.status));
  const meta = node("p", [formatTime(message.created_at), message.thread, message.group].filter(Boolean).join(" · "), "meta");
  row.append(heading, meta);
  if (message.task_file) {
    row.append(node("p", message.task_file, "meta path"),
      node("p", message.receipt_ready ? "Receipt available" : (message.status === "failed" ? "Send failed" : "Awaiting receipt"), "meta"));
  }
  if (message.body !== undefined) row.append(node("p", message.body, "body"));
  return row;
}

async function refreshActivity() {
  const ticket = ++state.activityRequest;
  const params = new URLSearchParams();
  const filters = {
    job: state.job, target: byId("activity-target").value, group: byId("activity-group").value.trim(),
    briefs: byId("show-tasks").checked ? "1" : "", messages: byId("show-messages").checked ? "1" : "",
  };
  for (const [key, value] of Object.entries(filters)) if (value) params.set(key, value);
  try {
    const result = await request("/api/activity?" + params);
    if (ticket !== state.activityRequest) return;
    const fingerprint = JSON.stringify([state.job, result]);
    if (state.previousActivity !== fingerprint) {
      const opened = new Set(Array.from(byId("jobs").querySelectorAll("details[open]"), (details) => details.dataset.job));
      items(byId("jobs"), result.tasks, (task) => jobEntry(task, opened), "No matching jobs.");
      items(byId("messages"), result.messages, messageEntry, "No matching peer messages.");
      const running = result.tasks.filter((task) => task.status === "running").length;
      byId("running-count").textContent = running;
      byId("recent-count").textContent = result.tasks.length - running;
      byId("message-count").textContent = result.messages.length;
      byId("clear-job").hidden = !state.job;
      state.previousActivity = fingerprint;
    }
    notice("activity-status");
  } catch (error) {
    if (ticket === state.activityRequest) notice("activity-status", error.message, true);
  }
}

function connectorRow(row) {
  const entry = node("article", "", "entry connection");
  const heading = node("div", "", "entry-head");
  heading.append(node("h3", row.label), status(row.status));
  entry.append(heading, node("p", row.detail, "meta"));
  if (row.command) {
    const command = node("div", "", "command");
    const code = node("code", row.command);
    const copy = node("button", "Copy", "button small");
    copy.type = "button";
    copy.setAttribute("aria-label", "Copy " + row.label + " setup command");
    copy.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(row.command);
        copy.textContent = "Copied";
        setTimeout(() => { copy.textContent = "Copy"; }, 1500);
      } catch {
        const selection = window.getSelection();
        const range = document.createRange();
        range.selectNodeContents(code);
        selection.removeAllRanges();
        selection.addRange(range);
        notice("connector-status", "Command selected. Copy it to your clipboard.");
      }
    });
    command.append(code, copy);
    entry.append(command);
  }
  return entry;
}

function renderConnectors() {
  const report = state.report;
  const preview = byId("agent-preview");
  preview.hidden = !report?.agent;
  if (!report) {
    byId("connection-profiles").replaceChildren();
    byId("connection-count").textContent = "";
    byId("install").disabled = true;
    return;
  }
  if (report.agent) {
    const instructions = node("details");
    instructions.append(node("summary", "Review agent instructions"), node("p", report.agent.instructions, "body"));
    preview.replaceChildren(node("h3", report.agent.name), node("p", report.agent.description, "muted"),
      node("p", report.agent.connectors.length ? "Requires " + report.agent.connectors.join(", ") : "No connectors required.", "meta"),
      instructions);
  }
  const models = report.profiles.map((profile) => {
    const block = node("section", "", "profile");
    const title = labels[profile.provider] + (profile.target === profile.provider ? "" : " · " + profile.target);
    block.append(node("h3", title));
    if (profile.incomplete) block.append(node("p", "Scan incomplete for this profile.", "empty muted"));
    const rows = report.steps.filter((row) => row.target === profile.target);
    rows.sort((a, b) => Number(b.status === "connected") - Number(a.status === "connected") || a.label.localeCompare(b.label));
    const group = node("div");
    items(group, rows, connectorRow, "No connector setup required.");
    block.append(group);
    return block;
  });
  byId("connection-profiles").replaceChildren(...models);
  const count = report.steps.filter((row) => row.status === "connected").length;
  byId("connection-count").textContent = count ? count + " connected" : "";
  updateInstall();
}

function updateInstall() {
  const destination = byId("destination").value;
  const report = state.report;
  const eligible = Boolean(report?.agent && destination && report.profiles.some((profile) => profile.target === destination));
  const needsSetup = eligible && report.steps.some((row) => row.target === destination && row.status !== "connected");
  byId("install").disabled = !eligible || needsSetup || state.scanning || state.installing;
  if (!state.installing && !byId("install-status").dataset.final) {
    notice("install-status", needsSetup ? "Complete the connector steps for this client, then scan again." : "");
  }
}

function resetInstallStatus() {
  delete byId("install-status").dataset.final;
  notice("install-status");
}

async function scanConnectors() {
  const ticket = ++state.scanRequest;
  state.scanned = true;
  state.scanning = true;
  state.report = null;
  for (const id of ["scan", "workspace", "agent-file"]) byId(id).disabled = true;
  renderConnectors();
  notice("connector-status", "Checking Codex and Claude Code…");
  const payload = { workspace: byId("workspace").value };
  if (state.package !== null) payload.package = state.package;
  try {
    const result = await request("/api/connectors", payload);
    if (ticket !== state.scanRequest) return;
    state.report = result;
    notice("connector-status", result.profiles.length ? "" : "Add a Codex or Claude Code target to your CX Bridge config.");
    if (state.package && !byId("destination").value) {
      const profile = result.profiles.find((profile) =>
        result.steps.every((row) => row.target !== profile.target || row.status === "connected"));
      if (profile) byId("destination").value = profile.target;
    }
  } catch (error) {
    if (ticket === state.scanRequest) notice("connector-status", error.message, true);
  } finally {
    if (ticket === state.scanRequest) {
      state.scanning = false;
      for (const id of ["scan", "workspace", "agent-file"]) byId(id).disabled = false;
      renderConnectors();
    }
  }
}

async function loadPackage() {
  const ticket = ++state.fileRequest;
  const [file] = byId("agent-file").files;
  if (file?.size > 96 * 1024) {
    state.package = null;
    state.report = null;
    renderConnectors();
    notice("install-status", "Choose a shared agent JSON file under 96 KiB.", true);
    return;
  }
  resetInstallStatus();
  try {
    const contents = file ? await file.text() : null;
    if (ticket !== state.fileRequest) return;
    state.package = contents;
    await scanConnectors();
  } catch {
    if (ticket === state.fileRequest) notice("install-status", "Could not read the shared agent file.", true);
  }
}

async function install() {
  state.installing = true;
  for (const id of ["workspace", "agent-file", "destination", "scan"]) byId(id).disabled = true;
  updateInstall();
  notice("install-status", "Rechecking connector access and installing…");
  try {
    const result = await request("/api/agents", {
      workspace: byId("workspace").value, target: byId("destination").value, package: state.package,
    });
    notice("install-status", "Installed " + result.path + ". Start a new session in " + labels[result.provider] + " for this workspace.");
  } catch (error) {
    notice("install-status", error.message, true);
  } finally {
    byId("install-status").dataset.final = "1";
    state.installing = false;
    for (const id of ["workspace", "agent-file", "destination", "scan"]) byId(id).disabled = false;
    updateInstall();
  }
}

function showView(view) {
  state.view = view;
  for (const button of document.querySelectorAll("nav button")) {
    const active = button.dataset.view === view;
    byId(button.dataset.view).hidden = !active;
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  if (view === "activity") refreshActivity();
  else if (!state.scanned) scanConnectors();
}

async function start() {
  document.querySelectorAll("nav button").forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  for (const id of ["activity-target", "activity-group", "show-tasks", "show-messages"]) {
    byId(id).addEventListener("change", () => {
      state.job = null;
      refreshActivity();
    });
  }
  byId("refresh-activity").addEventListener("click", refreshActivity);
  byId("clear-job").addEventListener("click", () => {
    state.job = null;
    refreshActivity();
  });
  byId("scan").addEventListener("click", scanConnectors);
  byId("workspace").addEventListener("change", () => {
    resetInstallStatus();
    byId("workspace-path").hidden = Boolean(byId("workspace").value);
    scanConnectors();
  });
  byId("agent-file").addEventListener("change", loadPackage);
  byId("destination").addEventListener("change", () => {
    resetInstallStatus();
    updateInstall();
  });
  byId("install").addEventListener("click", install);
  setInterval(() => {
    if (state.view === "activity" && byId("follow").checked && !document.hidden) refreshActivity();
  }, 3000);
  try {
    const settings = await request("/api/settings");
    byId("workspace-path").textContent = settings.cwd;
    for (const workspace of settings.workspaces) byId("workspace").add(new Option(workspace, workspace));
    for (const target of settings.targets) {
      byId("activity-target").add(new Option(target.name, target.name));
      if (target.kind === "codex" || target.kind === "claude") {
        byId("destination").add(new Option(labels[target.kind] + (target.name === target.kind ? "" : " · " + target.name), target.name));
      }
    }
    updateInstall();
  } catch (error) {
    notice("activity-status", error.message, true);
  }
  refreshActivity();
}

start();
