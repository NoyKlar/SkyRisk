"use strict";

const SESSION_KEY = "skyrisk.session_id";

const thread = document.getElementById("thread");
const empty = document.getElementById("empty");
const form = document.getElementById("composer");
const input = document.getElementById("input");
const send = document.getElementById("send");

let pending = false;

function loadSession() {
  try { return sessionStorage.getItem(SESSION_KEY); } catch { return null; }
}

function saveSession(id) {
  try {
    if (id) sessionStorage.setItem(SESSION_KEY, id);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch { /* storage unavailable: memory lasts until the page reloads */ }
}

let sessionId = loadSession();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function scrollToEnd() {
  thread.scrollTop = thread.scrollHeight;
}

function typingDots() {
  // Decorative only: screen readers announce just "Thinking".
  const dots = el("span", "dots");
  dots.setAttribute("aria-hidden", "true");
  for (let i = 0; i < 3; i++) dots.appendChild(el("span", "dot"));
  return dots;
}

function addUserMessage(text) {
  empty.hidden = true;
  const msg = el("div", "msg user text", text);
  msg.dir = "auto";
  thread.appendChild(msg);
  scrollToEnd();
}

function renderReply(msg, data) {
  msg.className = "msg assistant";
  if (data.status.startsWith("refused")) msg.classList.add("refused");
  if (data.status === "error") msg.classList.add("error");
  msg.replaceChildren();

  const text = el("div", "text", data.answer);
  text.dir = "auto";
  msg.appendChild(text);

  if (data.limitations.length) {
    const box = el("section", "limitations");
    box.appendChild(el("h2", "", "Limitations"));
    const list = el("ul");
    list.dir = "auto";  // on the list, not the items, so bullets move to the right for RTL text
    for (const item of data.limitations) list.appendChild(el("li", "", item));
    box.appendChild(list);
    msg.appendChild(box);
  }

  const meta = [data.served_by || "no model call"];
  if (data.tools_used.length) meta.push("tools: " + data.tools_used.join(", "));
  msg.appendChild(el("div", "meta", meta.join(" · ")));
}

function renderFailure(msg, text) {
  msg.className = "msg assistant error";
  msg.replaceChildren(el("div", "text", text));
}

async function failureText(res) {
  if (res.status === 422) return "That message couldn't be sent. Please shorten it and try again.";
  if (res.status === 429) {
    try {
      const body = await res.json();
      if (body.detail) return body.detail;
    } catch { /* fall through */ }
    return "Too many questions right now. Please try again later.";
  }
  return "Something went wrong on the server. Please try again in a moment.";
}

async function ask(question) {
  if (pending || !question.trim()) return;
  pending = true;
  send.disabled = true;
  addUserMessage(question);

  const msg = el("div", "msg assistant pending", "Thinking");
  msg.appendChild(typingDots());
  thread.appendChild(msg);
  scrollToEnd();

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, session_id: sessionId }),
    });
    if (!res.ok) {
      renderFailure(msg, await failureText(res));
      return;
    }
    const data = await res.json();
    if (data.session_reset) thread.insertBefore(el("p", "note", "Earlier context expired, starting fresh."), msg);
    sessionId = data.session_id;
    saveSession(sessionId);
    renderReply(msg, data);
  } catch {
    renderFailure(msg, "Couldn't reach the server. Check your connection and try again.");
  } finally {
    pending = false;
    send.disabled = false;
    scrollToEnd();
    input.focus();
  }
}

function autoSize() {
  input.style.height = "auto";
  input.style.height = input.scrollHeight + "px";
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const question = input.value;
  if (pending || !question.trim()) return;
  input.value = "";
  autoSize();
  ask(question);
});

input.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    form.requestSubmit();
  }
});

input.addEventListener("input", autoSize);

function insertHub(name) {
  // Insert at the cursor, padded with spaces so it doesn't run into neighbouring words.
  const { selectionStart: start, selectionEnd: end, value } = input;
  const before = start > 0 && !/\s$/.test(value.slice(0, start)) ? " " : "";
  const after = end < value.length && !/^\s/.test(value.slice(end)) ? " " : "";
  input.focus();
  input.setRangeText(before + name + after, start, end, "end");
  autoSize();
}

function renderHubs(hubs) {
  hubs = [...hubs].sort((a, b) => a.city.localeCompare(b.city));
  const byRegion = new Map();
  for (const hub of hubs) {
    if (!byRegion.has(hub.region)) byRegion.set(hub.region, []);
    byRegion.get(hub.region).push(hub);
  }
  const groups = document.getElementById("hubs-groups");
  for (const region of [...byRegion.keys()].sort()) {
    const group = el("section", "hub-group");
    group.appendChild(el("h3", "", region));
    const list = el("ul");
    for (const hub of byRegion.get(region)) {
      const button = el("button", "hub", hub.city);
      button.type = "button";
      button.dir = "auto";
      button.title = `${hub.name}, ${hub.state} (insert into question)`;
      button.addEventListener("click", () => insertHub(hub.city));
      const li = el("li");
      li.appendChild(button);
      const badge = el("span", "level nt-badge", "–");
      badge.dataset.hub = hub.hub_id;
      badge.title = "Near-term forecast loading";
      li.appendChild(badge);
      list.appendChild(li);
    }
    group.appendChild(list);
    groups.appendChild(group);
  }
  document.getElementById("hubs-panel").hidden = false;

  document.getElementById("hub-count").textContent = `${hubs.length} US hubs`;

  const line = document.getElementById("hubs-line");
  line.textContent = "Hubs: " + hubs.map((h) => h.city).join(" · ");
  line.hidden = false;
}

function renderNearTerm(data) {
  for (const h of data.hubs) {
    const badge = document.querySelector(`.nt-badge[data-hub="${CSS.escape(h.hub_id)}"]`);
    if (!badge) continue;
    if (h.level) {
      badge.className = `level nt-badge ${h.level}`;
      badge.textContent = h.level;
      badge.title = `Near-term ${Math.round(h.score)} (${h.level}), ${h.forecast_start} – ${h.forecast_end}`;
    } else {
      badge.title = "Forecast unavailable";
    }
  }
}

function markNearTermUnavailable() {
  for (const badge of document.querySelectorAll(".nt-badge")) badge.title = "Forecast unavailable";
}

fetch("/api/hubs")
  .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
  .then((hubs) => {
    renderHubs(hubs);
    // The badges fill in later: a cold forecast cache takes a few seconds.
    fetch("/api/near-term")
      .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
      .then(renderNearTerm)
      .catch(markNearTermUnavailable);
  })
  .catch(() => { /* the panel is a convenience; chat works without it */ });

function timeAgo(iso) {
  const minutes = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (!Number.isFinite(minutes) || minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} days ago`;
}

function alertChange(a) {
  return `${Math.round(a.prev_score)} → ${Math.round(a.new_score)}`;
}

function renderAlerts(data) {
  const list = document.getElementById("alerts-list");
  list.replaceChildren();
  for (const a of data.alerts) {
    const item = el("li", "alert");
    item.title = `${a.reason}; ${a.detail}`;
    const head = el("div", "alert-head");
    head.appendChild(el("span", "", a.city));
    head.appendChild(el("span", `level ${a.new_level}`, a.new_level));
    if (a.demo) head.appendChild(el("span", "demo-badge", "demo"));
    item.appendChild(head);
    item.appendChild(el("div", "alert-change", `${alertChange(a)} · ${timeAgo(a.created_at)}`));
    item.appendChild(el("div", "", a.detail));
    list.appendChild(item);
  }
  document.getElementById("alerts-empty").hidden = data.alerts.length > 0;
  document.getElementById("alerts-panel").hidden = false;
}

fetch("/api/alerts?limit=10")
  .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
  .then(renderAlerts)
  .catch(() => { /* alerts are informational; chat works without them */ });

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => ask(chip.querySelector("span").textContent));
}

document.getElementById("new-chat").addEventListener("click", async () => {
  if (pending) return;
  const old = sessionId;
  sessionId = null;
  saveSession(null);
  for (const node of [...thread.children]) if (node !== empty) node.remove();
  empty.hidden = false;
  input.focus();
  if (old) {
    try { await fetch("/api/sessions/" + encodeURIComponent(old), { method: "DELETE" }); } catch { /* best effort */ }
  }
});
