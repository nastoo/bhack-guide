/** Browser microphone capture for local Hi Loomo / Whisper tests. */

async function recordLocalMic(durationSec) {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("This browser does not support microphone access.");
  }

  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  const preferred = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"];
  const mimeType = preferred.find((type) => MediaRecorder.isTypeSupported(type)) || "";
  const recorder = mimeType
    ? new MediaRecorder(stream, { mimeType })
    : new MediaRecorder(stream);
  const chunks = [];

  recorder.ondataavailable = (event) => {
    if (event.data.size > 0) chunks.push(event.data);
  };

  recorder.start(250);
  await new Promise((resolve) => setTimeout(resolve, durationSec * 1000));
  if (recorder.state !== "inactive") recorder.stop();
  await new Promise((resolve) => {
    recorder.onstop = resolve;
  });
  stream.getTracks().forEach((track) => track.stop());

  const type = recorder.mimeType || mimeType || "audio/webm";
  return new Blob(chunks, { type });
}

function localMicFilename(blob) {
  if (blob.type.includes("webm")) return "local-mic.webm";
  if (blob.type.includes("mp4") || blob.type.includes("m4a")) return "local-mic.m4a";
  return "local-mic.wav";
}

async function uploadLocalMicTest({
  durationSec = 5,
  requireWakeWord = true,
  runAgent = false,
} = {}) {
  const blob = await recordLocalMic(durationSec);
  const form = new FormData();
  form.append("audio", blob, localMicFilename(blob));
  form.append("require_wake_word", requireWakeWord ? "true" : "false");
  form.append("run_agent", runAgent ? "true" : "false");

  const response = await authFetch("/api/test/local-mic", {
    method: "POST",
    body: form,
  });
  const text = await response.text();
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error(text || response.statusText);
  }
  if (!response.ok) {
    throw new Error(data.detail || text || response.statusText);
  }
  return data;
}
