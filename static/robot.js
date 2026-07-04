const chatLog = document.getElementById("chat-log");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const resetBtn = document.getElementById("reset-btn");
const statusEl = document.getElementById("status");
const robotMetaEl = document.getElementById("robot-meta");

function appendMessage(role, text, meta = "") {
  const div = document.createElement("div");
  div.className = `chat-msg ${role}`;
  const label = role === "user" ? "You" : "Robot";
  div.innerHTML = `<span class="role">${label}</span><p>${escapeHtml(text)}</p>`;
  if (meta) {
    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = meta;
    div.appendChild(hint);
  }
  chatLog.appendChild(div);
  chatLog.scrollTop = chatLog.scrollHeight;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    throw new Error(await response.text() || response.statusText);
  }
  return response.json();
}

chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = chatInput.value.trim();
  if (!message) return;

  appendMessage("user", message);
  chatInput.value = "";
  chatInput.disabled = true;
  statusEl.textContent = "Thinking…";

  try {
    const result = await api("/api/robot/chat", {
      method: "POST",
      body: JSON.stringify({ message }),
    });
    const meta = result.steps
      ? `Tools used: ${result.steps} · speed ${result.speed_kmh} km/h`
      : "";
    appendMessage("assistant", result.reply || "Done.", meta);
    statusEl.textContent = "Ready";
  } catch (error) {
    appendMessage("assistant", error.message);
    statusEl.textContent = "Error";
  } finally {
    chatInput.disabled = false;
    chatInput.focus();
  }
});

resetBtn.addEventListener("click", async () => {
  await api("/api/robot/reset", { method: "POST" });
  chatLog.innerHTML = "";
  appendMessage(
    "assistant",
    "Chat cleared. Try: \"Go straight for 50 metres\" or \"Stop\"."
  );
});

initRobotPicker(
  document.getElementById("robot-select"),
  document.getElementById("robot-meta")
);
chatInput.focus();
