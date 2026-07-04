const statusEl = document.getElementById("status");
const destinationEl = document.getElementById("destination");
const stepEl = document.getElementById("step");
const routeInfoEl = document.getElementById("route-info");
const instructionEl = document.getElementById("instruction");
const lastSpeechEl = document.getElementById("last-speech");
const lastHttpEl = document.getElementById("last-http");
const messageEl = document.getElementById("message");
const connectionEl = document.getElementById("connection");
const navForm = document.getElementById("nav-form");
const destInput = document.getElementById("dest-input");
const stopBtn = document.getElementById("stop-btn");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const listenBtn = document.getElementById("listen-btn");
const wakeBtn = document.getElementById("wake-btn");
const wakeStatusEl = document.getElementById("wake-status");
const wakeLogEl = document.getElementById("wake-log");
const agentReplyEl = document.getElementById("agent-reply");
const manualForm = document.getElementById("manual-form");
const manualNameInput = document.getElementById("manual-name");
const manualRouteInput = document.getElementById("manual-route");
const manualPreviewEl = document.getElementById("manual-preview");
const manualRunBtn = document.getElementById("manual-run-btn");
const manualStopBtn = document.getElementById("manual-stop-btn");
const manualSpeakInput = document.getElementById("manual-speak");

async function api(path, options = {}) {
  const response = await authFetch(path, options);
  if (!response.ok) {
    const text = await response.text();
    try {
      const data = JSON.parse(text);
      throw new Error(data.detail || text || response.statusText);
    } catch (err) {
      if (err instanceof Error && err.message !== text) throw err;
      throw new Error(text || response.statusText);
    }
  }
  return response.json();
}

function applyUpdate(data) {
  statusEl.textContent = data.status || "idle";
  destinationEl.textContent = data.place_name || data.destination || "—";
  if (data.step_total) {
    stepEl.textContent = `${data.step_index || 0} / ${data.step_total}`;
  }
  if (data.total_distance) {
    const provider = data.route_provider === "manual" ? "manual · " : "";
    routeInfoEl.textContent = `${provider}${data.total_distance}, ${data.total_duration || ""}`;
  }
  if (data.current_instruction) instructionEl.textContent = data.current_instruction;
  if (data.last_http) lastHttpEl.textContent = `Motion: ${data.last_http}`;
  if (data.last_speech) lastSpeechEl.textContent = `Spoken: ${data.last_speech}`;
  else if (data.route_provider === "manual") lastSpeechEl.textContent = "";
  if (data.message) messageEl.textContent = data.message;
}

if (navForm) {
  navForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const destination = destInput.value.trim();
    if (!destination) return;
    await api("/api/navigation/start", {
      method: "POST",
      body: JSON.stringify({ destination }),
    });
  });
}

if (stopBtn) {
  stopBtn.addEventListener("click", async () => {
    await api("/api/navigation/stop", { method: "POST" });
  });
}

if (manualForm && manualRunBtn) {
  manualForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const routeText = manualRouteInput.value.trim();
    if (!routeText) return;
    manualPreviewEl.textContent = "Starting route (motion only)…";
    manualRunBtn.disabled = true;
    try {
      const result = await api("/api/robot/route", {
        method: "POST",
        body: JSON.stringify({
          name: manualNameInput.value.trim() || "Manual route",
          route_text: routeText,
          speak: manualSpeakInput ? manualSpeakInput.checked : true,
        }),
      });
      const via = result.parser === "llm" ? " (via LLM)" : "";
      manualPreviewEl.textContent = result.summary
        ? `${result.message} Plan${via}: ${result.summary}`
        : result.message || "Route started.";
    } catch (error) {
      manualPreviewEl.textContent = `Error: ${error.message}`;
    } finally {
      manualRunBtn.disabled = false;
    }
  });
}

if (manualStopBtn) {
  manualStopBtn.addEventListener("click", async () => {
    await api("/api/robot/route/stop", { method: "POST" });
    manualPreviewEl.textContent = "Stopped.";
  });
}

if (chatForm) {
  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = chatInput.value.trim();
    if (!message) return;
    const result = await api("/api/agent/chat", {
      method: "POST",
      body: JSON.stringify({ message, ...getForceRouteOptions() }),
    });
    agentReplyEl.textContent = result.reply || result.heard || "No reply";
    chatInput.value = "";
  });
}

if (listenBtn) {
  listenBtn.addEventListener("click", async () => {
    listenBtn.disabled = true;
    agentReplyEl.textContent = 'Listening… say "Hi Loomo, …"';
    try {
      const result = await api("/api/agent/listen", {
        method: "POST",
        body: JSON.stringify({ duration: 5, require_wake_word: true, ...getForceRouteOptions() }),
      });
      if (result.wake_triggered === false) {
        appendWakeLog(
          `Listen once: wake failed — "${result.heard}" (${result.wake_reason || "no match"})`,
          "warn",
        );
        agentReplyEl.textContent = result.reply || `Heard "${result.heard}" — wake phrase not detected.`;
      } else {
        appendWakeLog(`Listen once: wake OK → "${result.command}"`, "ok");
        agentReplyEl.textContent = result.command
          ? `Command: "${result.command}" — ${result.reply}`
          : result.reply || "No speech detected";
      }
    } catch (error) {
      agentReplyEl.textContent = error.message;
    } finally {
      listenBtn.disabled = false;
    }
  });
}

const WAKE_LOG_MAX = 50;

function appendWakeLog(line, level = "info") {
  if (!wakeLogEl) return;
  const ts = new Date().toLocaleTimeString();
  const entry = document.createElement("div");
  entry.className = `wake-log-line wake-log-${level}`;
  entry.textContent = `[${ts}] ${line}`;
  wakeLogEl.prepend(entry);
  while (wakeLogEl.children.length > WAKE_LOG_MAX) {
    wakeLogEl.lastChild.remove();
  }
}

function formatWakeEvent(data) {
  const src = data.source === "listen_once"
    ? " (listen once)"
    : data.source === "local_mic"
      ? " (local mic test)"
      : "";
  switch (data.status) {
    case "started":
      return { line: "Always-on listener started", level: "ok" };
    case "stopped":
      return { line: "Always-on listener stopped", level: "warn" };
    case "simulated":
      return { line: data.message || "Simulated robot — mic unavailable", level: "warn" };
    case "capturing":
      return {
        line: `Capturing mic${data.duration ? ` (${data.duration}s)` : ""}${src}…`,
        level: "info",
      };
    case "transcribing":
      return {
        line: `Transcribing${data.audio_bytes ? ` ${data.audio_bytes} bytes` : ""}${src}…`,
        level: "info",
      };
    case "no_audio":
      return { line: `No audio from mic${src}`, level: "warn" };
    case "silent":
      return {
        line: `Whisper returned empty${data.audio_bytes ? ` (${data.audio_bytes} bytes captured)` : ""}${src}`,
        level: "warn",
      };
    case "heard":
      return { line: `Heard: "${data.heard}"${src}`, level: "info" };
    case "ignored":
      return {
        line: `No wake phrase — "${data.heard}" (${data.reason || "no match"})${src}`,
        level: "warn",
      };
    case "triggered":
      return {
        line: `Wake OK → command: "${data.command}" (from "${data.heard}")${src}`,
        level: "ok",
      };
    case "thinking":
      return { line: `Sending to LLM: "${data.command}"`, level: "info" };
    case "replied":
      return {
        line: `LLM reply: ${data.reply || "(empty)"}${src}`,
        level: "ok",
      };
    default:
      return data.message
        ? { line: `${data.status}: ${data.message}`, level: "info" }
        : null;
  }
}

function handleWakeEvent(data) {
  const formatted = formatWakeEvent(data);
  if (formatted) appendWakeLog(formatted.line, formatted.level);

  if (data.status === "triggered" && data.command) {
    agentReplyEl.textContent = `Heard: "${data.heard}" → "${data.command}"`;
  }
  if (data.status === "replied" && data.reply) {
    agentReplyEl.textContent = `Command: "${data.command}" — ${data.reply}`;
  }
  if (wakeStatusEl) {
    const base = wakeBtn && wakeBtn.getAttribute("aria-pressed") === "true"
      ? 'Listening for "Hi Loomo"'
      : "";
    const detail = data.heard && data.status === "heard"
      ? `last: "${data.heard}"`
      : data.status;
    wakeStatusEl.textContent = base && detail ? `${base} · ${detail}` : base || detail || "";
  }
}

function updateWakeUi(running, statusText = "") {
  if (!wakeBtn) return;
  wakeBtn.setAttribute("aria-pressed", running ? "true" : "false");
  wakeBtn.textContent = running ? "Stop Hi Loomo listener" : "Always listen: Hi Loomo";
  if (wakeStatusEl) {
    wakeStatusEl.textContent = statusText;
  }
}

async function refreshWakeStatus() {
  if (!wakeBtn) return;
  try {
    const status = await api("/api/agent/wake");
    const detail = status.last_status ? ` · ${status.last_status}` : "";
    updateWakeUi(status.running, status.running ? `Listening for "Hi Loomo"${detail}` : "");
  } catch {
    updateWakeUi(false, "");
  }
}

if (wakeBtn) {
  wakeBtn.addEventListener("click", async () => {
    wakeBtn.disabled = true;
    try {
      const status = await api("/api/agent/wake");
      const next = !status.running;
      await api("/api/agent/wake", {
        method: "POST",
        body: JSON.stringify({ enabled: next }),
      });
      updateWakeUi(next, next ? 'Listening for "Hi Loomo"…' : "Wake listener stopped.");
      appendWakeLog(next ? "Always-on listener enabled" : "Always-on listener disabled", next ? "ok" : "warn");
    } catch (error) {
      agentReplyEl.textContent = error.message;
    } finally {
      wakeBtn.disabled = false;
    }
  });
  refreshWakeStatus();
}

function connect() {
  const protocol = window.location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${protocol}://${window.location.host}/ws`);
  ws.onopen = () => { connectionEl.textContent = "Live"; };
  ws.onclose = () => {
    connectionEl.textContent = "Disconnected — reconnecting…";
    setTimeout(connect, 1500);
  };
  ws.onmessage = (event) => {
    const data = JSON.parse(event.data);
    if (data.type === "navigation_update") applyUpdate(data);
    if (data.type === "wake_listener") {
      handleWakeEvent(data);
    }
  };
}

connect();
initRobotPicker(
  document.getElementById("robot-select"),
  document.getElementById("robot-meta")
);
