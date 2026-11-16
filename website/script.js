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
function closeSettings() { $("setup").hidden = true; document.body.style.overflow = ""; $("open-settings").focus(); }
$("open-settings").addEventListener("click", openSettings);
$("cancel").addEventListener("click", closeSettings);
$("setup").addEventListener("click", e => { if (e.target === $("setup")) closeSettings(); });
document.addEventListener("keydown", e => {
  if ($("setup").hidden) return;
  if (e.key === "Escape") closeSettings();
  if (e.key === "Tab") {
    const fields = [...$("setup").querySelectorAll("input,button")].filter(el => !el.disabled);
    const first = fields[0], last = fields[fields.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});
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
$("url").addEventListener("keydown", e => {
  if (e.key === "Enter" && !button.disabled) button.click();
});
if (!settings().endpoint || !settings().secret) show("Add your connection settings to get started.");
else show("Ready. Paste a video link to begin.");


// Play the original clip bytes. No re-encoding or lower-resolution variants.
(() => {
  const video = $("knight-video");
  const scene = video?.closest(".scene-art");
  if (!video || !scene) return;
  const clips = Array.from({ length: 5 }, (_, i) => "media/clip-" + String(i + 1).padStart(2, "0") + ".mp4.b64");
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  let lastClip = -1, activeUrl = "", timer = 0, loading = false;
  let userPaused = motion.matches;
  video.muted = true;

  function updatePause() {
    $("scene-pause").setAttribute("aria-pressed", String(userPaused));
    $("scene-pause").setAttribute("aria-label", userPaused ? "Play background video" : "Pause background video");
    $("scene-pause-icon").textContent = userPaused ? "▷" : "Ⅱ";
  }
  function schedule() {
    window.clearTimeout(timer);
    if (!userPaused && !document.hidden) timer = window.setTimeout(changeClip, 35000 + Math.floor(Math.random() * 15000));
  }
  function portraitViewport() {
    return window.innerWidth <= 720 || window.innerHeight > window.innerWidth;
  }
  function chooseClip() {
    // Start with the closest native aspect ratio, then keep every scene in rotation.
    if (lastClip < 0) return portraitViewport() ? 4 : 1;
    const weights = portraitViewport() ? [1.5, 1, 1.25, 1.25, 6] : [2, 5, 2.5, 2.5, 0.65];
    const choices = weights.map((weight, index) => ({ weight: index === lastClip ? 0 : weight, index }));
    let pick = Math.random() * choices.reduce((total, choice) => total + choice.weight, 0);
    for (const choice of choices) {
      pick -= choice.weight;
      if (pick < 0) return choice.index;
    }
    return choices.find(choice => choice.weight > 0).index;
  }
  function positionClip(index) {
    video.style.objectPosition = index === 4 ? "50% 42%" : portraitViewport() ? "57% center" : "50% 48%";
  }
  window.addEventListener("resize", () => positionClip(lastClip), { passive: true });
  function decodeVideo(encoded) {
    const binary = atob(encoded.trim());
    const chunks = [];
    for (let offset = 0; offset < binary.length; offset += 32768) {
      const end = Math.min(offset + 32768, binary.length);
      const bytes = new Uint8Array(end - offset);
      for (let i = offset; i < end; i++) bytes[i - offset] = binary.charCodeAt(i);
      chunks.push(bytes);
    }
    return new Blob(chunks, { type: "video/mp4" });
  }
  async function changeClip() {
    if (loading) return;
    loading = true;
    window.clearTimeout(timer);
    $("scene-next").disabled = true;
    const next = chooseClip();
    positionClip(next);
    let nextUrl = "";
    try {
      const response = await fetch(clips[next], { cache: "force-cache" });
      if (!response.ok) throw Error("Clip unavailable");
      nextUrl = URL.createObjectURL(decodeVideo(await response.text()));
      const ready = new Promise((resolve, reject) => {
        let timeout;
        const cleanup = () => { window.clearTimeout(timeout); video.removeEventListener("loadeddata", loaded); video.removeEventListener("error", failed); };
        const loaded = () => { cleanup(); resolve(); };
        const failed = () => { cleanup(); reject(Error("Clip unavailable")); };
        video.addEventListener("loadeddata", loaded);
        video.addEventListener("error", failed);
        timeout = window.setTimeout(failed, 20000);
      });
      video.src = nextUrl;
      video.load();
      await ready;
      if (!userPaused && !document.hidden) {
        try { await video.play(); }
        catch { userPaused = true; updatePause(); }
      }
      const previousUrl = activeUrl;
      activeUrl = nextUrl;
      nextUrl = "";
      lastClip = next;
      video.classList.add("is-visible");
      scene.classList.add("has-video");
      $("scene-number").textContent = "SCENE " + String(next + 1).padStart(2, "0") + " / 05";
      if (previousUrl) URL.revokeObjectURL(previousUrl);
    } catch {
      if (nextUrl) URL.revokeObjectURL(nextUrl);
      if (!activeUrl) {
        video.classList.remove("is-visible");
        scene.classList.remove("has-video");
      }
    } finally {
      loading = false;
      $("scene-next").disabled = false;
      schedule();
    }
  }
  $("scene-pause").addEventListener("click", async () => {
    userPaused = !userPaused;
    updatePause();
    if (userPaused) { video.pause(); window.clearTimeout(timer); }
    else {
      if (activeUrl) {
        try { await video.play(); }
        catch { userPaused = true; updatePause(); }
        schedule();
      } else changeClip();
    }
  });
  $("scene-next").addEventListener("click", changeClip);
  document.addEventListener("visibilitychange", async () => {
    if (document.hidden) { video.pause(); window.clearTimeout(timer); }
    else if (!userPaused) {
      if (activeUrl) {
        try { await video.play(); }
        catch { userPaused = true; updatePause(); }
        schedule();
      } else changeClip();
    }
  });
  window.addEventListener("pagehide", () => { window.clearTimeout(timer); video.pause(); });
  updatePause();
  changeClip();
})();
