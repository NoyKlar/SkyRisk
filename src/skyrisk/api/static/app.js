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
      list.appendChild(li);
    }
    group.appendChild(list);
    groups.appendChild(group);
  }
  document.getElementById("hubs-panel").hidden = false;

  const line = document.getElementById("hubs-line");
  line.textContent = "Hubs: " + hubs.map((h) => h.city).join(" · ");
  line.hidden = false;
}

fetch("/api/hubs")
  .then((res) => (res.ok ? res.json() : Promise.reject(res.status)))
  .then(renderHubs)
  .catch(() => { /* the panel is a convenience; chat works without it */ });

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
