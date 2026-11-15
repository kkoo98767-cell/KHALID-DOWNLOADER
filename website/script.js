const $ = id => document.getElementById(id);
const result = $("result");
const button = $("submit");
const progress = $("progress");
const progressFill = $("progress-fill");
const progressValue = $("progress-value");
const progressLabel = $("progress-label");
const settings = () => ({ endpoint: localStorage.getItem("kd_endpoint") || "", secret: localStorage.getItem("kd_secret") || "" });
function show(message, kind = "") { result.textContent = message; result.className = `result ${kind}`; }
function setProgress(value, label) {
  const safe = Math.max(0, Math.min(100, Math.round(value)));
  progressFill.style.width = safe + "%";
  progressValue.textContent = safe + "%";
  if (label) progressLabel.textContent = label;
}
function openSettings() {
  const s = settings();
  $("endpoint").value = s.endpoint; $("secret").value = s.secret;
  $("setup-error").hidden = true;
  $("setup").hidden = false;
  document.body.style.overflow = "hidden";
  $("endpoint").focus();
}
function closeSettings() { $("setup").hidden = true; document.body.style.overflow = ""; }
$("open-settings").addEventListener("click", openSettings);
$("cancel").addEventListener("click", closeSettings);
$("setup").addEventListener("click", e => { if (e.target === $("setup")) closeSettings(); });
document.addEventListener("keydown", e => { if (e.key === "Escape") closeSettings(); });
$("save").addEventListener("click", () => {
  const endpoint = $("endpoint").value.trim().replace(/\/+$/, "");
  const secret = $("secret").value.trim();
  const err = $("setup-error");
  try { const u = new URL(endpoint); if (u.protocol !== "https:") throw Error(); }
  catch { err.textContent = "Enter a valid https:// Worker URL."; err.hidden = false; return; }
  if (!secret) { err.textContent = "Enter the API secret."; err.hidden = false; return; }
  localStorage.setItem("kd_endpoint", endpoint);
  localStorage.setItem("kd_secret", secret);
  closeSettings();
  show("Settings saved in this browser.", "success");
});
$("paste").addEventListener("click", async () => { try { $("url").value = await navigator.clipboard.readText(); } catch { show("Clipboard blocked. Paste the link manually.", "error"); } });
$("submit").addEventListener("click", async () => {
  const s = settings();
  const url = $("url").value.trim();
  if (!s.endpoint || !s.secret) { openSettings(); show("Add your Worker URL and API secret first."); return; }
  try { const parsed = new URL(url); if (!["http:","https:"].includes(parsed.protocol)) throw Error(); }
  catch { show("Enter a valid HTTP(S) URL.", "error"); return; }

  const originalLabel = "QUEUE DOWNLOAD";
  button.disabled = true;
  button.classList.add("is-loading");
  button.querySelector(".button-icon").textContent = "↻";
  button.querySelector(".button-label").textContent = "SENDING REQUEST…";
  progress.hidden = false;
  setProgress(0, "Sending request to the service…");
  show("Connecting securely…");

  let current = 0;
  const ticker = window.setInterval(() => {
    current = Math.min(90, current + (current < 35 ? 4 : current < 70 ? 2 : 1));
    setProgress(current, "Waiting for the service to accept the request…");
  }, 180);

  try {
    const response = await fetch(s.endpoint, {
      method:"POST",
      headers:{"content-type":"application/json", "authorization":`Bearer ${s.secret}`},
      body:JSON.stringify({url})
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw Error(data.error || `Request failed (${response.status})`);
    window.clearInterval(ticker);
    setProgress(100, "Request accepted by the service");
    button.querySelector(".button-icon").textContent = "✓";
    button.querySelector(".button-label").textContent = "REQUEST SENT";
    show("Request queued. Telegram will receive the result when processing finishes.", "success");
  } catch (e) {
    window.clearInterval(ticker);
    setProgress(0, "Request could not be sent");
    button.querySelector(".button-icon").textContent = "!";
    button.querySelector(".button-label").textContent = "TRY AGAIN";
    show(e.message || "Could not reach the service.", "error");
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.classList.remove("is-loading");
      button.querySelector(".button-label").textContent = originalLabel;
      button.querySelector(".button-icon").textContent = "↓";
    }, 1600);
  }
});
if (!settings().endpoint) show("Deploy the Worker, then add its URL and API secret in Connection settings.");


// Rotate through the five user-provided knight clips without loading them all at once.
(() => {
  const video = document.getElementById("knight-video");
  const scene = video?.closest(".scene-art");
  if (!video || !scene) return;

  const clips = [
    "media/clip-01.mp4.b64",
    "media/clip-02.mp4.b64",
    "media/clip-03.mp4.b64",
    "media/clip-04.mp4.b64",
    "media/clip-05.mp4.b64"
  ];
  let lastClip = -1;
  let activeUrl = "";

  function decodeVideo(encoded) {
    const binary = atob(encoded.trim());
    const chunks = [];
    const chunkSize = 32768;
    for (let offset = 0; offset < binary.length; offset += chunkSize) {
      const end = Math.min(offset + chunkSize, binary.length);
      const bytes = new Uint8Array(end - offset);
      for (let i = offset; i < end; i++) bytes[i - offset] = binary.charCodeAt(i);
      chunks.push(bytes);
    }
    return new Blob(chunks, { type: "video/mp4" });
  }

  async function changeClip() {
    const choices = clips.map((_, index) => index).filter(index => index !== lastClip);
    const next = choices[Math.floor(Math.random() * choices.length)];
    try {
      const response = await fetch(clips[next], { cache: "force-cache" });
      if (!response.ok) throw new Error("Clip could not be loaded");
      const blob = decodeVideo(await response.text());
      const nextUrl = URL.createObjectURL(blob);
      video.classList.remove("is-visible");
      scene.classList.remove("has-video");
      video.pause();
      video.src = nextUrl;
      video.load();
      await new Promise((resolve, reject) => {
        video.addEventListener("loadeddata", resolve, { once: true });
        video.addEventListener("error", reject, { once: true });
      });
      await video.play();
      const previousUrl = activeUrl;
      activeUrl = nextUrl;
      lastClip = next;
      video.classList.add("is-visible");
      scene.classList.add("has-video");
      if (previousUrl) window.setTimeout(() => URL.revokeObjectURL(previousUrl), 1200);
      window.setTimeout(changeClip, 30000 + Math.floor(Math.random() * 16000));
    } catch {
      window.setTimeout(changeClip, 12000);
    }
  }

  window.setTimeout(changeClip, 700);
})();
