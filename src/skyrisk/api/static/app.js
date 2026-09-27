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

async function ask(question) {
  if (pending || !question.trim()) return;
  pending = true;
  send.disabled = true;
  addUserMessage(question);

  const msg = el("div", "msg assistant pending", "Thinking…");
  thread.appendChild(msg);
  scrollToEnd();

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: question, session_id: sessionId }),
    });
    if (!res.ok) {
      renderFailure(msg, res.status === 422
        ? "That message couldn't be sent. Please shorten it and try again."
        : "Something went wrong on the server. Please try again in a moment.");
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

for (const chip of document.querySelectorAll(".chip")) {
  chip.addEventListener("click", () => ask(chip.textContent));
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
