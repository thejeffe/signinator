let WLED_DITHER;   // WLED's temporal dithering status (dither firmware), from /api/state
// Signinator page. Same look and feel as Treeinator: a dark LIVE stage showing what the sign shows now
// (drawn as LEDs), a Preview of your edits before you send them, control boxes, a status chip and ⚙ Settings.
const $ = (id) => document.getElementById(id);
function say(el, m, err) { el.textContent = m || ""; el.classList.toggle("err", !!err); }
const status = (m, err) => say($("status"), m, err);

async function api(path, opts) {
  const r = await fetch(path, Object.assign({ cache: "no-store" }, opts));
  let j = null;
  try { j = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) { const e = new Error((j && j.error) || ("error " + r.status)); e.body = j; throw e; }
  return j;
}
const postJSON = (path, body) =>
  api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
const pressed = (b, on) => b.setAttribute("aria-pressed", on ? "true" : "false");
const isOn = (b) => b.getAttribute("aria-pressed") === "true";
function toggles(...ids) { ids.forEach(id => $(id).addEventListener("click", () => pressed($(id), !isOn($(id))))); }
function seg(containerId, attr, onPick) {
  const bs = [...$(containerId).querySelectorAll("button")];
  bs.forEach(b => b.addEventListener("click", () => { bs.forEach(o => pressed(o, o === b)); onPick && onPick(b.dataset[attr]); }));
  return { get: () => (bs.find(isOn) || bs[0]).dataset[attr], set: (v) => bs.forEach(o => pressed(o, o.dataset[attr] === v)) };
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

let S = null;              // last /api/state
let W = 96, H = 8;         // sign size
let BRI = 128;             // real brightness 0..255
let VIEW = "text";

// ---------- views ----------
function setView(v) {
  VIEW = v; $("app").dataset.view = v;
  document.querySelectorAll("#viewSeg button[data-v]").forEach(o => pressed(o, o.dataset.v === v));
  document.querySelectorAll(".view").forEach(c => c.hidden = c.dataset.for !== v);
  try { localStorage.setItem("signView", v); } catch (e) {}
  schedulePreview();
}
document.querySelectorAll("#viewSeg button[data-v]").forEach(b => b.addEventListener("click", () => setView(b.dataset.v)));

// ---------- LED monitors: the live stage and the preview ----------
class Monitor {
  constructor(canvas) { this.cv = canvas; this.ctx = canvas.getContext("2d"); this.off = document.createElement("canvas"); this.octx = this.off.getContext("2d"); this.cell = 4; this.frame = null; this.k = 1; }
  size(cell) {
    this.cell = cell; const dpr = window.devicePixelRatio || 1;
    this.cv.width = W * cell * dpr; this.cv.height = H * cell * dpr;
    this.cv.style.width = W * cell + "px"; this.cv.style.height = H * cell + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.off.width = W; this.off.height = H; this.draw();
  }
  draw() {
    const { ctx, cell } = this;
    ctx.fillStyle = "#020305"; ctx.fillRect(0, 0, W * cell, H * cell);
    const f = this.frame;
    if (!f || f.w !== W || f.h !== H) return;
    const k = this.k, px = f.px, img = this.octx.createImageData(W, H);
    for (let i = 0, j = 0; i < W * H; i++, j += 3) {
      img.data[i * 4] = px[j] * k; img.data[i * 4 + 1] = px[j + 1] * k; img.data[i * 4 + 2] = px[j + 2] * k; img.data[i * 4 + 3] = 255;
    }
    this.octx.putImageData(img, 0, 0);
    if ("filter" in ctx && cell >= 4) {   // glow: a blurred, scaled copy behind the dots
      ctx.save(); ctx.filter = `blur(${cell * 0.55}px)`; ctx.globalAlpha = 0.85; ctx.imageSmoothingEnabled = true;
      ctx.drawImage(this.off, 0, 0, W * cell, H * cell); ctx.restore();
    }
    const r = Math.max(0.6, cell * 0.36);
    for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
      const d = (y * W + x) * 4, R = img.data[d], G = img.data[d + 1], B = img.data[d + 2];
      ctx.fillStyle = (R + G + B) < 12 ? "#141b25" : `rgb(${R},${G},${B})`;
      ctx.beginPath(); ctx.arc(x * cell + cell / 2, y * cell + cell / 2, r, 0, 6.2832); ctx.fill();
    }
  }
}
const live = new Monitor($("view")), prev = new Monitor($("pv"));
// dim LEDs look much brighter to the eye than linear; same curve as Treeinator's preview
const eyeK = () => Math.pow(BRI / 255, 0.4);
function setLiveK() { const m = S ? S.mode : "on", on = m === "on" || (m === "auto" && S && S.power); live.k = on ? eyeK() : m === "wled" ? 0.25 * eyeK() : 0; prev.k = Math.max(0.35, eyeK()); }

const phone = window.matchMedia("(max-width:759px)");
function sizeCanvas() {
  // Wide strip signs go on top (layout "strip"); square-ish signs sit left of the controls.
  const app = $("app"), wrap = $("cvwrap");
  app.classList.toggle("strip", W / H >= 2.5);
  const stacked = phone.matches || app.classList.contains("strip");
  wrap.style.height = "";
  const aw = wrap.clientWidth;
  const ah = stacked ? window.innerHeight * (phone.matches ? 0.32 : 0.28) : wrap.clientHeight;
  const cell = Math.max(2, Math.min(40, Math.floor(Math.min(aw / W, ah / H))));
  if (stacked) wrap.style.height = H * cell + "px";
  live.size(cell);
  // preview: smaller, capped in height
  const pw = $("pvwrap").clientWidth - 16, ph = phone.matches ? 130 : Math.max(90, Math.min(170, window.innerHeight * 0.17));
  prev.size(Math.max(1, Math.min(24, Math.floor(Math.min(pw / W, ph / H)))));
  setLiveK();
}
let rsz = 0;
window.addEventListener("resize", () => { cancelAnimationFrame(rsz); rsz = requestAnimationFrame(sizeCanvas); });

async function fetchFrame(url) {
  const r = await fetch(url, { cache: "no-store" });
  const b = new Uint8Array(await r.arrayBuffer());
  const w = (b[0] << 8) | b[1], h = (b[2] << 8) | b[3];
  if (!w || !h || b.length < 4 + w * h * 3) return null;
  if (w !== W || h !== H) { W = w; H = h; sizeCanvas(); showSize(); }
  return { w, h, px: b.subarray(4) };
}
let polling = false;
async function pollFrames() {
  if (polling) return; polling = true;
  let n = 0;
  while (!document.hidden) {
    const t0 = performance.now();
    try {
      const f = await fetchFrame("/api/frame.bin"); if (f) { live.frame = f; live.draw(); }
      if (n++ % 2 === 0) { const p = await fetchFrame("/api/preview.bin"); if (p) { prev.frame = p; prev.draw(); } }
    } catch (e) { await sleep(1500); }
    await sleep(Math.max(0, 66 - (performance.now() - t0)));
  }
  polling = false;
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) { pollFrames(); loadState(); } });

// ---------- status ----------
function showSize() {
  $("gifInfo").textContent = `sign is ${W}×${H}`;
}
function describeLook(c) {
  if (!c) return "nothing";
  if (c.kind === "text") return `text “${(c.text || "").replace(/\n/g, " / ")}”`;
  if (c.kind === "effect") return `effect ${FX_NAMES[c.fx] || c.fx}`;
  if (c.kind === "gif") return `GIF ${(c.name || "").replace(/[-_]/g, " ")}${c.beat ? " (dancing to the beat)" : ""}`;
  if (c.kind === "image") return c.name || "picture";
  if (c.kind === "test") return `test: ${c.pattern}`;
  return c.kind === "idle" ? "blank" : c.kind;
}
function renderStatus() {
  if (!S) return;
  const w = S.wled || {}, st = w.status || {}, badge = $("modeBadge"), mode = S.mode || (S.power ? "on" : "off");
  let cls = "", name = "", info = "";
  if (mode === "wled") { cls = "off"; name = "WLED"; info = "Signinator is off · WLED is running its own effects"; }
  else if (mode === "auto" && !S.power) { cls = "off"; name = "AUTO"; info = "Idle · turns on when music plays"; }
  else if (!w.enabled || !w.host) { name = "PREVIEW"; info = "Not sending to WLED · open ⚙ to connect"; }
  else if (st.online === false) { cls = "offline"; name = "OFFLINE"; info = `${w.host} isn't answering` + (st.error ? ` · ${st.error}` : ""); }
  else if (mode === "off") { cls = "off"; name = "SIGN OFF"; info = `${w.host} · the sign is dark`; }
  else { cls = st.online ? "live" : ""; name = st.online ? "LIVE" : "SENDING"; info = (st.name ? st.name + " · " : "") + w.host + (st.version ? " · WLED " + st.version : ""); }
  badge.className = "mode " + cls; $("modeName").textContent = name; $("modeInfo").textContent = info;
  badge.title = info + " · open settings";
  const stg = $("stage");
  stg.classList.toggle("live", mode === "on" && cls === "live");
  stg.classList.toggle("off-mode", mode === "off"); stg.classList.toggle("wled-mode", mode === "wled");
  stg.classList.toggle("auto-mode", mode === "auto");
  stg.classList.toggle("live", (mode === "on" || (mode === "auto" && S.power)) && cls === "live");
  $("stageTag").textContent = mode === "off" ? "LIVE · OFF" : mode === "wled" ? "LIVE · WLED" : mode === "auto" ? (S.power ? "LIVE · AUTO" : "AUTO · IDLE") : cls === "live" ? "LIVE · ON AIR" : "LIVE";
  const dark = mode === "off" || mode === "wled" || (mode === "auto" && !S.power);
  $("stMsg").hidden = !dark;
  $("stMsg").textContent = mode === "off" ? "Sign is off" : mode === "auto" ? "Waiting for music (Auto)" : "WLED is running its own effects";
  const c = S.content || {};
  const what = describeLook(c);
  const np = S.nowplaying || {};
  const npOn = !!np.active && (mode === "on" || (mode === "auto" && S.power));
  stg.classList.toggle("np-mode", npOn);
  if (npOn) $("stageTag").textContent = "LIVE · NOW PLAYING";
  $("srcText").textContent = npOn ? `♪ ${np.title || "untitled"}${np.artist ? " — " + np.artist : ""}` + (np.state !== "playing" ? ` (${np.state})` : "")
                                  : `${W}×${H} · ${what}`;
  $("npX").hidden = !npOn;
  const rl = S.relay || {};
  if (rl.state === "playing" && rl.target) $("srcText").textContent += `  → 🔊 ${rl.target}`;
  document.querySelectorAll("#pwSeg button").forEach(b => pressed(b, b.dataset.p === mode));
  setLiveK(); live.draw();
  const upd = S.build && S.build !== document.body.dataset.build;
  $("reloadBtn").classList.toggle("upd", !!upd);
  $("reloadBtn").title = upd ? "A newer version is on the server: tap to reload" : "Reload the app";
  markRest();
  showAlbumColors();
  $("gifBpm").textContent = S.audio && S.audio.bpm ? `${S.audio.bpm} BPM` : (S.music_playing ? "finding the beat…" : "no music");
  showDirty();
}

let first = true;
async function loadState() {
  try { S = await api("/api/state"); } catch (e) { $("modeBadge").className = "mode offline"; $("modeName").textContent = "NO SERVER"; $("modeInfo").textContent = "Can't reach Signinator"; return; }
  if (S.width !== W || S.height !== H) { W = S.width; H = S.height; sizeCanvas(); }
  showSize(); syncStyle();
  if (!dimDragging) setDim(S.brightness);
  if (first) {
    first = false; fillContent(S.content);
    const k = S.content && S.content.kind;
    if (!VIEW_SET) setView(k === "effect" ? "fx" : k === "gif" ? "gif" : "text"); else schedulePreview();
  }
  renderStatus();
}
function fillContent(c) {
  if (!c) return;
  if (c.kind === "effect") { fillEffect(c); return; }
  if (c.kind === "gif") { GIF_SEL = c.name; gifMode.set(c.mode || "fit"); $("gifRate").value = c.rate || 1; $("gifRateV").textContent = (+c.rate || 1) + "×";
    gifBeat.set(c.beat ? "1" : "0"); $("gifBpl").value = String(c.bpl || "auto"); syncGifBeat(); renderGifs(); return; }
  if (c.kind !== "text") return;
  $("text").value = c.text || "";
  if (c.color) $("color").value = c.color;
  if (c.color2) $("color2").value = c.color2;
  if (c.bg) $("bg").value = c.bg;
  if (c.effect) effects.set(c.effect);
  const [sz, hh] = normSize(c);
  tsize.set(sz); if (sz === "px") $("theight").value = hh;
  FONT_WANT = c.font || "pixel"; $("font").value = FONT_WANT;
  pressed($("smooth"), !!c.smooth); talign.set(c.align || "center");
  if (c.speed) { $("speed").value = c.speed; $("speedval").textContent = c.speed; }
  pressed($("scroll"), c.scroll !== false);
  pressed($("emoji"), !!c.emoji);
  pressed($("continuous"), c.gap_mode === "space");
  syncEffect(); syncStyle();
}
setInterval(() => { if (!document.hidden) loadState(); }, 3000);

$("reloadBtn").addEventListener("click", async () => {
  try { if (window.caches) for (const k of await caches.keys()) await caches.delete(k); } catch (e) {}
  location.replace("/?t=" + Date.now());
});

// ---------- preview: what you're editing, before it's sent ----------
// "same as sign" when the editor matches what's on the sign, "not sent yet" otherwise.
function normSize(p) { return p.size === "normal" || !p.size ? ["px", 8] : [p.size, p.size === "px" ? +p.height : 0]; }
const textSig = (p) => JSON.stringify([p.text, p.color, p.bg, +p.speed, !!p.scroll, !!p.emoji, p.effect, p.effect === "two_color_words" ? p.color2 : "", p.gap_mode || "screen",
  normSize(p), p.font || "pixel", (p.font || "pixel") !== "pixel" && !!p.smooth, p.scroll ? "" : (p.align || "center")]);
const fxSig = (p) => JSON.stringify([p.fx, +p.speed, +p.intensity, p.palette, (p.c1 || "").toLowerCase(), (p.c2 || "").toLowerCase(), (p.c3 || "").toLowerCase(), !!p.album]);
const gifSig = (p) => JSON.stringify([p.name, p.mode || "fit", +p.rate || 1, !!p.beat, p.beat ? String(p.bpl || "auto") : ""]);
function viewLook() {   // what the current view would send, as a "look"
  if (VIEW === "fx") return Object.assign({ kind: "effect" }, fxParams());
  if (VIEW === "gif") return GIF_SEL ? Object.assign({ kind: "gif" }, gifParams()) : null;
  return Object.assign({ kind: "text" }, textParams());
}
function sameLook(a, b) {
  if (!a || !b || a.kind !== b.kind) return false;
  if (a.kind === "text") return textSig(a) === textSig(b);
  if (a.kind === "effect") return fxSig(a) === fxSig(b);
  if (a.kind === "gif") return gifSig(a) === gifSig(b);
  return false;
}
function isDirty() {
  const look = viewLook();
  return !!look && !sameLook(S && S.content, look);
}
function showDirty() {
  const d = isDirty(), t = $("pvTag");
  t.classList.toggle("dirty", d);
  const art = S && S.nowplaying && S.nowplaying.active;
  t.textContent = (VIEW === "gif" && !GIF_SEL) ? "PICK A GIF" : d ? "NOT SENT YET" : art ? "SENT · ART SHOWING NOW" : "SAME AS SIGN";
}
let pvTimer = 0;
function schedulePreview() { clearTimeout(pvTimer); pvTimer = setTimeout(sendPreview, 120); }
async function sendPreview() {
  try {
    if (VIEW === "fx") {
      await postJSON("/api/preview/effect", fxParams());
    } else if (VIEW === "gif") {
      if (!GIF_SEL) { showDirty(); return; }
      await postJSON("/api/preview/gif", gifParams());
    } else {
      await postJSON("/api/preview/text", textParams());
    }
  } catch (e) { status("Preview: " + e.message, true); }
  showDirty();
}
document.querySelectorAll(".view").forEach(v => ["input", "change", "click"].forEach(ev => v.addEventListener(ev, (e) => {
  if (e.target.closest(".send, .restbtn")) return;
  setTimeout(schedulePreview, 0);   // after the control's own handler has run
})));
// Sending also turns Signinator on, so a message never goes to a dark sign.
async function ensureOn() {
  if (!S) return;
  if (S.mode === "auto") { if (!S.power) await postJSON("/api/power", { mode: "auto", wake: true }); return; }   // stay in auto
  if (S.mode !== "on") await postJSON("/api/power", { mode: "on" });
}

// ---------- text ----------
const EFF_NAMES = { solid: "Solid color", two_color_words: "Two colors, word by word", rainbow_fade: "Rainbow fade", rainbow_chars: "Rainbow letters", fade: "Breathe", blink: "Blink" };
const effects = seg("effects", "e", () => syncEffect());
const tsize = seg("tsize", "s", () => syncStyle());
const talign = seg("talign", "a");
toggles("smooth");
$("font").onchange = () => syncStyle();
$("theight").oninput = () => $("theightval").textContent = $("theight").value;
// Pixel font sizes come in whole steps of 8 (crisp blocks); other fonts take any height.
function syncStyle() {
  const pixel = $("font").value === "pixel", hs = $("theight");
  hs.min = pixel ? 8 : 4; hs.step = pixel ? 8 : 1; hs.max = Math.max(+hs.min, H);
  if (pixel) hs.value = Math.max(8, Math.floor(+hs.value / 8) * 8);
  $("theightval").textContent = hs.value;
  $("hline").hidden = tsize.get() !== "px";
  $("smooth").disabled = pixel;
  const scrolling = isOn($("scroll"));
  $("talign").classList.toggle("dim", scrolling);
  $("styleNote").textContent = scrolling ? "align: turn Scroll off" : "still text · Enter for more lines";
}
$("scroll").addEventListener("click", () => syncStyle());
(async () => {
  try {
    const j = await api("/api/fonts"), sel = $("font"), cur = sel.value;
    sel.innerHTML = "";
    j.fonts.forEach(f => { const o = document.createElement("option"); o.value = f.id; o.textContent = f.name; sel.appendChild(o); });
    sel.value = FONT_WANT || cur; syncStyle();
  } catch (e) {}
})();
let FONT_WANT = null;
function syncEffect() {
  const e = effects.get();
  $("color2-wrap").hidden = e !== "two_color_words";
  $("effName").textContent = EFF_NAMES[e] || e;
}
toggles("scroll", "continuous", "emoji");
$("speed").oninput = () => $("speedval").textContent = $("speed").value;
function textParams() {
  return {
    text: $("text").value, color: $("color").value, bg: $("bg").value,
    speed: +$("speed").value, scroll: isOn($("scroll")), emoji: isOn($("emoji")),
    effect: effects.get(), color2: $("color2").value,
    gap_mode: isOn($("continuous")) ? "space" : "screen", size: tsize.get(),
    font: $("font").value || "pixel", height: tsize.get() === "px" ? +$("theight").value : 0,
    smooth: isOn($("smooth")) && $("font").value !== "pixel", align: talign.get(),
  };
}
async function sendText() {
  try {
    await postJSON("/api/text", textParams());
    await ensureOn();
    status("Sent: " + $("text").value);
    await loadState();
  } catch (e) { status("Couldn't send: " + e.message, true); }
}
$("send-text").onclick = sendText;
$("text").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); sendText(); } });

// ---------- emoji ----------
const EMOJIS = ["😀","😂","😍","😎","🤔","😭","😡","👍","👎","👏","🙌","🙏",
  "💪","🔥","✨","⭐","🌟","💯","❤️","🧡","💛","💚","💙","💜","🖤","🤍",
  "💥","⚡","🌈","☀️","🌙","⛄","☂️","🎉","🎊","🎂","🎁","🍕","🌮","🍔",
  "🍟","🍺","🍻","☕","🎃","🎄","💀","👻","🤖","👽","🚀","🏆","⚽","🏀",
  "🎸","🎮","💰","📈","✅","❌","⁉️","💤","👀","🐶","🐱","🦖","🦄","🦃","🍂","❄️","🎅"];
const picker = $("emoji-picker");
EMOJIS.forEach(e => {
  const b = document.createElement("button");
  b.type = "button"; b.textContent = e; b.onclick = () => insertEmoji(e);
  picker.appendChild(b);
});
let CUSTOM_GLYPHS = {};   // name -> 'builtin' | 'user'
async function refreshCustomEmoji() {
  try { CUSTOM_GLYPHS = (await api("/api/emoji")).emoji || {}; } catch (e) { CUSTOM_GLYPHS = {}; }
  picker.querySelectorAll(".glyph-btn").forEach(b => b.remove());
  const list = $("ce-list"); list.innerHTML = "";
  Object.keys(CUSTOM_GLYPHS).sort().forEach(name => {
    const src = `/api/emoji/${name}.png?scale=4&t=${Date.now()}`;
    const b = document.createElement("button");
    b.type = "button"; b.className = "glyph-btn"; b.title = `:${name}: (${CUSTOM_GLYPHS[name]})`;
    const img = document.createElement("img"); img.src = src; img.alt = name; b.appendChild(img);
    b.onclick = () => insertEmoji(`:${name}:`);
    picker.appendChild(b);
    const row = document.createElement("div"); row.className = "ce-row";
    const im2 = document.createElement("img"); im2.src = src;
    const tag = document.createElement("code"); tag.textContent = `:${name}:`;
    row.append(im2, tag);
    if (CUSTOM_GLYPHS[name] === "user") {
      const del = document.createElement("button");
      del.type = "button"; del.className = "ce-del"; del.textContent = "×"; del.title = "Delete";
      del.onclick = () => deleteCustomEmoji(name);
      row.appendChild(del);
    } else {
      const t = document.createElement("span"); t.className = "ce-builtin"; t.textContent = "built-in"; row.appendChild(t);
    }
    list.appendChild(row);
  });
}
$("ce-file").onchange = () => { const f = $("ce-file").files[0]; $("ce-file-lbl").firstChild.textContent = f ? f.name : "Choose image"; $("ce-file-lbl").classList.toggle("has", !!f); };
$("ce-add").onclick = async () => {
  const f = $("ce-file").files[0];
  const name = $("ce-name").value.trim().toLowerCase();
  if (!f) { status("Pick an image first", true); return; }
  if (!/^[a-z0-9_]{1,32}$/.test(name)) { status("Name: a-z, 0-9 and _ (1 to 32 characters)", true); return; }
  const fd = new FormData(); fd.append("file", f); fd.append("name", name);
  try {
    const j = await api("/api/emoji", { method: "POST", body: fd });
    status("Added :" + j.name + ":");
    $("ce-file").value = ""; $("ce-name").value = ""; $("ce-file").onchange();
    refreshCustomEmoji();
  } catch (e) { status("Couldn't add it: " + e.message, true); }
};
async function deleteCustomEmoji(name) {
  try { await api(`/api/emoji/${encodeURIComponent(name)}`, { method: "DELETE" }); status("Deleted :" + name + ":"); refreshCustomEmoji(); }
  catch (e) { status("Couldn't delete it", true); }
}
$("emoji-toggle").onclick = () => {
  const show = picker.hidden;
  picker.hidden = !show; $("emoji-manage").hidden = !show; pressed($("emoji-toggle"), show);
  if (show) refreshCustomEmoji();
};
refreshCustomEmoji();
function insertEmoji(e) {
  const inp = $("text");
  const s = inp.selectionStart ?? inp.value.length, en = inp.selectionEnd ?? inp.value.length;
  inp.value = inp.value.slice(0, s) + e + inp.value.slice(en);
  const pos = s + e.length;
  inp.focus(); inp.setSelectionRange(pos, pos);
  pressed($("emoji"), true);  // emoji rendering needed
}

// ---------- effects ----------
let FX_NAMES = {}, FX_HINTS = {}, FX_PAL = {}, FX_MUSIC = {}, FX_SEL = "soap";
function fxParams() {
  return { fx: FX_SEL, speed: +$("fxSpeed").value, intensity: +$("fxInt").value, palette: $("fxPal").value,
           c1: $("fxC1").value, c2: $("fxC2").value, c3: $("fxC3").value, album: isOn($("fxAlbum")) };
}
function pickFx(id, keepPalette) {
  FX_SEL = id;
  $("fxList").querySelectorAll("button").forEach(b => pressed(b, b.dataset.fx === id));
  $("fxName").textContent = FX_NAMES[id] || id;
  $("fxHint").textContent = (FX_HINTS[id] || "") + (FX_MUSIC[id] && S && S.audio && !S.audio.live ? " · No music is playing through Signinator right now, so it idles." : "");
  if (!keepPalette && FX_PAL[id]) $("fxPal").value = FX_PAL[id];
  // music effects start in album colors, unless you switched that off for this effect
  if (!keepPalette) pressed($("fxAlbum"), !!FX_MUSIC[id] && !albumOff().includes(id));
}
function fillEffect(c) {
  ["Speed", "Int"].forEach((k, i) => { const v = i ? c.intensity : c.speed; if (v != null) { $("fx" + k).value = v; $("fx" + k + "V").textContent = v; } });
  if (c.palette) $("fxPal").value = c.palette;
  ["c1", "c2", "c3"].forEach((k, i) => { if (c[k]) $("fxC" + (i + 1)).value = c[k]; });
  pressed($("fxAlbum"), !!c.album);
  FX_WANT = c.fx; if (FX_NAMES[c.fx]) pickFx(c.fx, true);
}
let FX_WANT = null;
(async () => {
  try {
    const j = await api("/api/effects");
    const list = $("fxList");
    const add = (e) => {
      FX_NAMES[e.id] = e.name; FX_HINTS[e.id] = e.hint; FX_PAL[e.id] = e.palette; FX_MUSIC[e.id] = !!e.music;
      const b = document.createElement("button"); b.type = "button"; b.dataset.fx = e.id; b.textContent = (e.music ? "♪ " : "") + e.name; b.title = e.hint || e.name;
      b.addEventListener("click", () => pickFx(e.id));
      list.appendChild(b);
    };
    j.effects.filter(e => !e.music).forEach(add);
    const hd = document.createElement("div"); hd.className = "fxhead"; hd.textContent = "♪ Reacts to the music playing through Signinator";
    list.appendChild(hd);
    j.effects.filter(e => e.music).forEach(add);
    j.palettes.forEach(p => { const o = document.createElement("option"); o.value = p.id; o.textContent = p.name; $("fxPal").appendChild(o); });
    const want = FX_WANT || FX_SEL;
    pickFx(want, !!FX_WANT);
    if (S) renderStatus();
  } catch (e) { $("fxHint").textContent = "Couldn't load the effects."; }
})();
toggles("fxAlbum");
function albumOff() { try { return JSON.parse(localStorage.getItem("signAlbumOff") || "[]"); } catch (e) { return []; } }
$("fxAlbum").addEventListener("click", () => {   // remember per effect
  if (!FX_MUSIC[FX_SEL]) return;
  const off = albumOff().filter(x => x !== FX_SEL);
  if (!isOn($("fxAlbum"))) off.push(FX_SEL);
  try { localStorage.setItem("signAlbumOff", JSON.stringify(off)); } catch (e) {}
});
function showAlbumColors() {
  const cols = (S && S.nowplaying && S.nowplaying.colors) || [];
  const sw = $("fxAlbumSw"); sw.innerHTML = "";
  cols.forEach(c => { const i = document.createElement("i"); i.style.background = c; sw.appendChild(i); });
  $("fxAlbumNote").textContent = cols.length ? "" : "no album art yet";
}
[["fxSpeed", "fxSpeedV"], ["fxInt", "fxIntV"]].forEach(([a, b]) => $(a).addEventListener("input", () => $(b).textContent = $(a).value));
$("send-fx").onclick = async () => {
  try { await postJSON("/api/effect", fxParams()); await ensureOn(); status("Running: " + (FX_NAMES[FX_SEL] || FX_SEL)); await loadState(); }
  catch (e) { status("Couldn't start the effect: " + e.message, true); }
};

// ---------- GIF library ----------
let GIFS = [], GIF_SEL = null;
const gifMode = seg("gifMode", "m");
const gifBeat = seg("gifBeat", "b", () => syncGifBeat());
function syncGifBeat() { $("gifBplLine").hidden = gifBeat.get() !== "1"; }
function gifParams() { return { name: GIF_SEL, mode: gifMode.get(), rate: +$("gifRate").value, beat: gifBeat.get() === "1", bpl: $("gifBpl").value }; }
$("gifRate").addEventListener("input", () => $("gifRateV").textContent = $("gifRate").value + "×");
function renderGifs() {
  const box = $("gifList"); box.innerHTML = "";
  GIFS.forEach(g => {
    const b = document.createElement("button"); b.type = "button"; b.title = `${g.label} · ${g.frames} frame${g.frames === 1 ? "" : "s"} · ${g.width}×${g.height}`;
    pressed(b, g.name === GIF_SEL);
    const im = document.createElement("img"); im.loading = "lazy"; im.alt = ""; im.src = `/api/gifs/${encodeURIComponent(g.name)}?t=${g.mtime}`;
    const sp = document.createElement("span"); sp.textContent = g.label;
    b.append(im, sp);
    b.addEventListener("click", () => { GIF_SEL = g.name; renderGifs(); schedulePreview(); });
    box.appendChild(b);
  });
  $("gifCount").textContent = `${GIFS.length} in the library`;
  const sel = GIFS.find(g => g.name === GIF_SEL);
  $("gifDel").disabled = !sel;
}
async function loadGifs() {
  try { GIFS = (await api("/api/gifs")).gifs; } catch (e) { GIFS = []; }
  if (GIF_SEL && !GIFS.some(g => g.name === GIF_SEL)) GIF_SEL = null;
  renderGifs();
}
loadGifs();
$("gifUp").onchange = async () => {
  const f = $("gifUp").files[0]; if (!f) return;
  const fd = new FormData(); fd.append("file", f); fd.append("name", f.name);
  status("Adding " + f.name + "…");
  try { const j = await api("/api/gifs", { method: "POST", body: fd }); GIF_SEL = j.name; await loadGifs(); schedulePreview(); status("Added " + j.name); }
  catch (e) { status("Couldn't add it: " + e.message, true); }
  $("gifUp").value = "";
};
$("gifDel").onclick = async () => {
  if (!GIF_SEL || !confirm(`Delete “${GIF_SEL.replace(/[-_]/g, " ")}” from the library?`)) return;
  try { await api(`/api/gifs/${encodeURIComponent(GIF_SEL)}`, { method: "DELETE" }); status("Deleted " + GIF_SEL); GIF_SEL = null; await loadGifs(); }
  catch (e) { status("Couldn't delete it", true); }
};
$("gifRestore").onclick = async () => {
  try { const j = await postJSON("/api/gifs/restore", {}); await loadGifs(); status(j.restored ? `Brought back ${j.restored} built-in GIF(s)` : "All built-in GIFs are already there"); }
  catch (e) { status("Couldn't restore them", true); }
};
$("send-gif").onclick = async () => {
  if (!GIF_SEL) { status("Pick a GIF first", true); return; }
  try { await postJSON("/api/gif", gifParams()); await ensureOn(); status("Playing: " + GIF_SEL.replace(/[-_]/g, " ")); await loadState(); }
  catch (e) { status("Couldn't play it: " + e.message, true); }
};

// ---------- resting state ----------
function markRest() {
  const look = S && S.rest && S.rest.look, cur = viewLook();
  document.querySelectorAll(".restbtn").forEach(b => b.classList.toggle("is-rest", !!look && sameLook(look, cur)));
}
document.querySelectorAll(".restbtn").forEach(b => b.addEventListener("click", async () => {
  const look = viewLook();
  if (!look) { status("Pick something first", true); return; }
  try { await postJSON("/api/rest", { look }); status("Resting look saved: " + describeLook(look) + ". The sign shows it when the music stops."); await loadState(); }
  catch (e) { status("Couldn't save it: " + e.message, true); }
}));

$("npX").addEventListener("click", async () => {
  try { await postJSON("/api/nowplaying/dismiss", {}); status("Album art hidden until the next song"); await loadState(); }
  catch (e) { status("Couldn't hide the art: " + e.message, true); }
});

// ---------- on / off / WLED + brightness ----------
const MODE_MSG = { on: "Signinator is on", off: "Sign is off (dark)", wled: "Signinator is off. WLED is running its own effects.", auto: "Auto: the sign turns on when music plays and off when it stops" };
document.querySelectorAll("#pwSeg button").forEach(b => b.addEventListener("click", async () => {
  try { await postJSON("/api/power", { mode: b.dataset.p }); status(MODE_MSG[b.dataset.p]); await loadState(); }
  catch (e) { status("Couldn't switch: " + e.message, true); }
}));
// Perceptual slider: position 0..1000 maps to brightness 0..255 on a square curve, so the dim end gets most of the travel.
const DIM_POS = 1000, DIM_GAMMA = 2.0;
const posToBri = (p) => Math.round(255 * Math.pow(Math.max(0, Math.min(1, +p / DIM_POS)), DIM_GAMMA));
const briToPos = (b) => Math.round(DIM_POS * Math.pow(Math.max(0, Math.min(1, +b / 255)), 1 / DIM_GAMMA));
let dimDragging = false;
function setDim(v) { BRI = Math.max(0, Math.min(255, Math.round(+v))); $("dim").value = briToPos(BRI); $("dimval").textContent = BRI; }
$("dim").oninput = () => { dimDragging = true; BRI = posToBri($("dim").value); $("dimval").textContent = BRI; setLiveK(); live.draw(); prev.draw(); };
$("dim").onchange = async () => {
  try { await postJSON("/api/brightness", { value: BRI }); status("Brightness " + BRI); }
  catch (e) { status("Couldn't set brightness", true); }
  dimDragging = false;
};

// ---------- settings dialog ----------
(function () {
  const dlg = $("setDlg"), out = $("wledOut"), sMsg = $("sMsg");
  const f = { host: $("sHost"), port: $("sPort"), en: $("sEnabled"), auto: $("sAuto"), map: $("sMap"), w: $("sW"), h: $("sH"),
    tw: $("sTW"), th: $("sTH"), tx: $("sTX"), ty: $("sTY"), serp: $("sSerp"), chain: $("sChain"), fh: $("sFlipH"), fv: $("sFlipV"),
    gamma: $("sGamma"), cc: $("sCC"), r: $("sR"), g: $("sG"), b: $("sB") };
  let lastWled = null;
  function showNote() {
    const panels = f.map.value === "panels";
    dlg.querySelectorAll("[data-m]").forEach(el => el.hidden = el.dataset.m !== f.map.value);
    const lock = f.auto.checked && !panels && lastWled && lastWled.geometry;
    f.w.disabled = f.h.disabled = !!lock;
    const w = panels ? +f.tw.value * +f.tx.value : +f.w.value, h = panels ? +f.th.value * +f.ty.value : +f.h.value;
    $("sNote").textContent = (panels
      ? `Sign is ${w} × ${h}. Signinator sends pixels in wiring order, so WLED should be a plain strip of ${w * h} LEDs (no 2D matrix, or “respect LED maps” off).`
      : `Sign is ${w} × ${h}. Signinator sends row by row from the top left and WLED's 2D matrix (with “respect LED maps” on) does the wiring.`)
      + (lock ? " Width and height follow WLED while “Match” is on." : "");
  }
  dlg.addEventListener("input", showNote); dlg.addEventListener("change", showNote);
  f.cc.addEventListener("change", () => { if (f.cc.value !== "custom") { const [r, g, b] = f.cc.value.split(",").map(Number); f.r.value = r; f.g.value = g; f.b.value = b; } });
  [f.r, f.g, f.b].forEach(el => el.addEventListener("input", () => { const v = [f.r.value, f.g.value, f.b.value].join(","); f.cc.value = [...f.cc.options].some(o => o.value === v) ? v : "custom"; }));
  function fill(s) {
    const w = s.wled || {}, m = s.mapping || {}, cc = s.color_correction || {};
    f.host.value = w.host || ""; f.port.value = w.port || 4048; f.en.checked = !!w.enabled; f.auto.checked = w.auto_geometry !== false;
    f.map.value = m.mode === "panels" ? "panels" : "matrix";
    f.w.value = s.width; f.h.value = s.height;
    f.tw.value = m.tile_w || s.width; f.th.value = m.tile_h || s.height; f.tx.value = m.tiles_x || 1; f.ty.value = m.tiles_y || 1;
    f.serp.checked = !!m.serpentine; f.chain.checked = !!m.tile_chain_serpentine; f.fh.checked = !!m.flip_h; f.fv.checked = !!m.flip_v;
    f.gamma.value = s.gamma == null ? 2.2 : s.gamma;
    $("sFps").value = s.fps || 40; $("sFpsAuto").checked = s.fps_auto !== false;
    $("sFpsNote").textContent = `Sending ${s.send_fps || "?"} fps now` + (s.wled_fps != null ? `; WLED shows ${s.wled_fps}.` : ".") +
      (s.fps_auto !== false ? " Automatic: follows what WLED can show, up to the maximum." : " Set it to about what WLED's Info page shows.");
    { const dz = s.dither || {}; $("sDitherMode").value = dz.mode || "spatial"; $("sDitherDark").checked = !!dz.darkest;
      $("sDitherHz").value = String(dz.min_hz || 0); $("sDitherFit").checked = dz.fit !== false; $("sDitherFps").value = String(dz.wled_fps ?? "auto"); if (!$("sDitherFps").value) $("sDitherFps").value = "auto"; WLED_DITHER = ((s.wled || {}).status || {}).dither; syncDither(); }
    const np = s.nowplaying || {}, ns = np.settings || {};
    $("sNpOn").checked = ns.enabled !== false; $("sNpMode").value = ns.mode || "fill"; $("sNpHold").value = ns.hold ?? 60;
    $("sNpEnh").checked = ns.enhance !== false; $("sNpLift").checked = ns.lift !== false;
    $("sNpShow").value = ns.show || "song"; $("sNpBrief").value = ns.brief_s || 10;
    { const ad = ns.art_delay ?? "auto"; $("sNpDelayMode").value = ad === "auto" ? "auto" : "ms"; $("sNpDelay").value = ad === "auto" ? 0 : ad; $("sNpDelay").disabled = ad === "auto"; }
    $("sNpTok").value = ""; $("sNpTok").placeholder = np.token_set ? "a token is set · type a new one, or none to remove it" : "leave empty for none";
    const ap = np.airplay || {}, base = location.protocol + "//" + location.host;
    $("npOut").textContent = [
      np.title ? `Last: ${np.artist ? np.artist + " — " : ""}${np.title} · ${np.state} · from ${np.source}` : "Nothing received yet.",
      `AirPlay speaker: ${ap.connected ? "connected to shairport-sync" : "shairport-sync not running"} (${ap.pipe || "no pipe"})`,
      `ESP32 / other senders: POST ${base}/api/nowplaying (JSON state/title/artist)`,
      `and POST ${base}/api/nowplaying/art (image/jpeg body)`].join("\n");
    f.r.value = cc.r ?? 255; f.g.value = cc.g ?? 255; f.b.value = cc.b ?? 255; f.r.dispatchEvent(new Event("input"));
    showNote();
    { const ro = s.routing || {}, sh = ro.showing || {}, word = { art: "album art", content: "the animation", dark: "nothing (dark)" };
      $("sRouteSign").value = ro.sign || "all"; $("sRouteScreen").value = ro.screen || "all";
      $("sRouteNote").textContent = `Right now: the LED sign shows ${word[sh.sign] || "?"}, the screen shows ${word[sh.screen] || "?"}.`; }
    { const mr = s.mirror || {}; $("sMirror").value = (mr.hosts || []).join(", ");
      $("sMirrorLook").value = mr.look || "led"; $("sMirrorSize").value = String(mr.size || 120); $("sMirrorFps").value = mr.fps || 30; $("sMirrorFpsAuto").checked = mr.fps_auto !== false; $("sMirrorPhotos").checked = mr.photos !== false; $("sMirrorOn").checked = mr.enabled !== false;
      $("sMirrorNote").textContent = (mr.status || []).map(m => `${m.host}: ` + (m.online ? `${m.name || "screen"} ${m.width}×${m.height}` + ` · sending ${m.sent_fps || 0} fps (${mr.fps_auto !== false ? "auto, up to " + m.fps_target : "fixed"}), screen draws ${m.fps ?? 0}` + (m.photo ? " · showing a photo" : mr.look === "hires" ? ` · full resolution ${mr.size}×${mr.size}` : (m.width && (m.width !== s.width || m.height !== s.height) ? ` (scaled from ${s.width}×${s.height})` : "")) : m.online === false ? "offline" + (m.error ? " (" + m.error + ")" : "") : "checking…")).join(" · ") || "Each screen gets the same picture as the sign; its backlight follows the sign's brightness."; }
    { const st = s.stereo || {}, ss = st.status || {};
      $("sStOn").checked = !!st.enabled; $("sStUrl").value = st.url || ""; $("sStOwn").checked = st.only_own !== false;
      $("sStTok").value = ""; $("sStTok").placeholder = st.token_set ? "saved · type to change" : "";
      $("sStOff").value = String(st.off_after_s ?? 300);
      const sel = $("sStEnt"); if (st.entity && ![...sel.options].some(o => o.value === st.entity)) { const o = document.createElement("option"); o.value = st.entity; o.textContent = st.entity; sel.appendChild(o); }
      sel.value = st.entity || "";
      $("sStNote").textContent = (ss.error ? "Problem: " + ss.error + ". " : "") + (ss.last ? `Last: turned ${ss.last} at ${new Date(ss.at * 1000).toLocaleTimeString()}. ` : "") + (st.enabled ? (ss.music ? "Music is playing." : "No music right now.") : ""); }
    { const mq = s.mqtt || {}, ms = mq.status || {};
      $("sMqOn").checked = !!mq.enabled; $("sMqHost").value = mq.host || ""; $("sMqPort").value = mq.port || 1883;
      $("sMqUser").value = mq.username || ""; $("sMqBase").value = mq.base || "signinator";
      $("sMqPass").value = ""; $("sMqPass").placeholder = mq.password_set ? "saved · type to change" : "";
      $("sMqNote").textContent = { off: "Not connected (turned off).", connecting: "Connecting…",
        connected: "Connected. Find it in Home Assistant under Settings → Devices & services → MQTT.",
        error: "Couldn't connect: " + (ms.error || "unknown error") }[ms.state] || ""; }
  }
  async function loadSpeakers(fresh) {
    const sel = $("sRelay"), keep = sel.value;
    $("sRelayNote").textContent = fresh ? "Looking for AirPlay speakers…" : $("sRelayNote").textContent;
    try {
      const j = await api("/api/relay/speakers" + (fresh ? "?fresh=1" : ""));
      const want = keep || (j.settings && j.settings.target) || "";
      sel.innerHTML = '<option value="">Off (sign only, no sound)</option>';
      const names = j.speakers.map(s => s.name);
      j.speakers.forEach(s => { const o = document.createElement("option"); o.value = s.name; o.textContent = `${s.name} (${s.address})`; sel.appendChild(o); });
      if (want && !names.includes(want)) { const o = document.createElement("option"); o.value = want; o.textContent = `${want} (not found right now)`; sel.appendChild(o); }
      sel.value = want;
      const st = j.status || {};
      $("sRelayNote").textContent = !st.installed ? "The relay sender isn't installed yet: run sudo bash /opt/signinator/deploy/install-airplay.sh"
        : st.state === "playing" ? `Playing on ${st.target} (${st.address}).`
        : st.state === "error" ? `Problem: ${st.error}`
        : `${j.speakers.length} speaker${j.speakers.length === 1 ? "" : "s"} found.` + (st.target ? ` Starts on ${st.target} when music plays.` : "");
      if (st.airplay_name) $("sRelayNote").textContent += ` On your phone, pick “${st.airplay_name}”.`;
      if (st.phone_db != null) $("sRelayNote").textContent += ` Phone volume ${st.phone_db <= -30 ? "muted" : st.phone_db.toFixed(1) + " dB"}` + (st.volume_forward === "ok" ? ", passed to the speaker." : st.volume_forward ? ` (${st.volume_forward}).` : ".");
    } catch (e) { $("sRelayNote").textContent = "Couldn't look for speakers."; }
  }
  $("sRelayFind").addEventListener("click", () => loadSpeakers(true));
  async function openDlg() {
    say(sMsg, ""); out.hidden = true;
    try { const st = await api("/api/state"); fill(st); renderWledLinks(); fillRest(st.rest); syncRefresh(); loadArtHistory(); { const au = st.audio || {};
      $("sAudioAuto").checked = au.auto !== false; $("sAudioDelay").value = au.delay_ms ?? 1500; $("sAudioOff").value = au.offset_ms ?? 0;
      $("sAudioNote").textContent = `Music effects run ${au.current_ms ?? 0} ms behind the audio Signinator hears` +
        (au.latency_ms != null ? ` (speaker latency ${au.latency_ms} ms + ${au.backlog_ms || 0} ms queued for the relay).` : (au.auto !== false ? ". The AirPlay delay is measured while the relay is playing." : ".")); } $("sRelayVol").value = (st.relay && st.relay.settings && st.relay.settings.volume) ?? 60; $("sRelayCodec").value = (st.relay && st.relay.settings && st.relay.settings.codec) || "alac"; $("sRelayVolMode").value = (st.relay && st.relay.settings && st.relay.settings.volume_mode) || "audio"; loadSpeakers(false); $("sSave").disabled = false; }
    catch (e) { $("sSave").disabled = true; say(sMsg, "Can't reach Signinator.", true); }
    dlg.showModal();
  }
  $("setBtn").addEventListener("click", openDlg);
  $("modeBadge").addEventListener("click", openDlg);
  $("sCancel").addEventListener("click", () => dlg.close());
  // links into WLED's own setup pages, for whatever host is typed in
  function renderWledLinks() {
    const box = $("sWledLinks"), host = f.host.value.trim();
    box.innerHTML = "";
    if (!/^[A-Za-z0-9.-]+(:\d+)?$/.test(host)) return;
    [["WLED main page ↗", "/"], ["WLED setup ↗", "/settings"]].forEach(([t, p]) => {
      const a = document.createElement("a"); a.href = "http://" + host + p; a.target = "_blank"; a.rel = "noopener"; a.textContent = t;
      a.title = "Opens " + a.href + " in a new tab"; box.appendChild(a);
    });
  }
  f.host.addEventListener("input", renderWledLinks);
  function syncDither() {
    const m = $("sDitherMode").value;
    $("sDitherDark").disabled = !(m === "spatial" || m === "temporal" || m === "wled");
    $("sDitherNote").textContent = {
      spatial: "Neighboring LEDs share the in-between levels in a fixed 4×4 pattern, so each small area shows the exact color. Nothing flickers. Up close a dim flat color can show a faint texture.",
      hue: "Each LED holds one steady color, picked so the hue stays right instead of a weak channel dropping out. No pattern, no flicker; less exact at the very lowest brightness.",
      temporal: "Each LED flickers between neighboring levels over time. Exact, but it needs WLED to refresh quickly (split long strands across outputs).",
      wled: "WLED dithers on its own at every LED refresh, with 16-bit gamma, white balance and brightness (Signinator sends the plain picture and the brightness). " +
        (WLED_DITHER === undefined ? "" : WLED_DITHER ? `WLED has it (${WLED_DITHER.fw}${WLED_DITHER.on ? ", on, " + WLED_DITHER.rate + " refreshes/s" + (WLED_DITHER.ledMax ? ", the LEDs allow up to " + WLED_DITHER.ledMax : "") + (WLED_DITHER.levels != null && WLED_DITHER.levels < 256 ? ", flicker limit allows " + (WLED_DITHER.levels > 1 ? "1/" + WLED_DITHER.levels + " steps" : "no in-between steps (rounding)") : "") : ", off"}).` + (WLED_DITHER.fw === "dither1" ? " Update WLED from Lickety Split for the automatic refresh rate." : "") : "This WLED doesn't have it yet: install the WLED (ESP32) build from Lickety Split."),
      off: "WLED does the dimming: steady, but colors drift when the sign is dim.",
    }[m] + (m === "off" || m === "wled" ? "" : " Signinator does the dimming and WLED's own brightness stays at 100%.");
    $("sDitherFpsL").hidden = m !== "wled"; $("sDitherHzL").hidden = m !== "wled"; $("sDitherFitL").hidden = m !== "wled";
  }
  $("sDitherMode").addEventListener("change", syncDither);
  $("sNpDelayMode").addEventListener("change", () => { $("sNpDelay").disabled = $("sNpDelayMode").value === "auto"; });
  // ---- recent album art: hide placeholders ----
  async function loadArtHistory() {
    const box = $("artHist");
    try {
      const h = (await api("/api/nowplaying/history")).history;
      box.innerHTML = "";
      if (!h.length) { box.textContent = "No album art received yet."; return; }
      h.forEach(a => {
        const fig = document.createElement("figure"); fig.className = a.blocked ? "hidden" : "";
        const im = document.createElement("img"); im.src = `/api/nowplaying/history/${a.sha}`; im.alt = "";
        const cap = document.createElement("figcaption");
        cap.textContent = a.title || "(no title)"; cap.title = `${a.artist ? a.artist + " — " : ""}${a.title || ""} · ${new Date(a.time * 1000).toLocaleString()} · ${a.bytes} bytes`;
        const b = document.createElement("button"); b.type = "button";
        b.textContent = a.blocked ? "Show again" : "Hide";
        b.addEventListener("click", async () => {
          try { await postJSON("/api/nowplaying/block", { sha: a.sha, blocked: !a.blocked }); say(sMsg, a.blocked ? "That image will show again" : "That image won't be shown anymore"); loadArtHistory(); loadState(); }
          catch (e) { say(sMsg, e.message, true); }
        });
        fig.append(im, cap, b); box.appendChild(fig);
      });
    } catch (e) { box.textContent = "Couldn't load recent album art."; }
  }

  // ---- sync test ----
  let syncTimer = 0;
  async function syncRefresh() {
    try {
      const st = await api("/api/state"), au = st.audio || {}, sy = st.sync || {};
      $("sAudioOff").value = au.offset_ms ?? 0;
      $("syncNote").textContent = (sy.active ? `Test running (${sy.remaining} s left). ` : "") +
        `Lights run ${au.current_ms ?? 0} ms behind the audio` +
        (au.latency_ms != null ? ` = speaker ${au.latency_ms} + queued ${au.backlog_ms || 0} + fine-tune ${au.offset_ms || 0}.` : `, fine-tune ${au.offset_ms || 0} ms.`);
      $("syncStop").disabled = !sy.active;
      clearTimeout(syncTimer);
      if (dlg.open && sy.active) syncTimer = setTimeout(syncRefresh, 1000);
    } catch (e) {}
  }
  $("syncStart").addEventListener("click", async () => {
    try { await postJSON("/api/sync/start", { seconds: 60 }); say(sMsg, "Sync test running: watch the sign and listen"); syncRefresh(); }
    catch (e) { say(sMsg, e.message, true); }
  });
  $("syncStop").addEventListener("click", async () => { try { await postJSON("/api/sync/stop", {}); } catch (e) {} setTimeout(syncRefresh, 400); });
  $("syncNudge").querySelectorAll("button").forEach(b => b.addEventListener("click", async () => {
    const v = Math.max(-2000, Math.min(2000, (+$("sAudioOff").value || 0) + (+b.dataset.n)));
    $("sAudioOff").value = v;
    try { await postJSON("/api/config", { audio: { offset_ms: v } }); syncRefresh(); } catch (e) { say(sMsg, e.message, true); }
  }));
  dlg.addEventListener("close", () => clearTimeout(syncTimer));
  function setTab(t) {
    dlg.querySelectorAll("#sTabs button").forEach(b => pressed(b, b.dataset.t === t));
    dlg.querySelectorAll("fieldset[data-tab]").forEach(f => f.hidden = f.dataset.tab !== t);
    try { localStorage.setItem("signSetTab", t); } catch (e) {}
  }
  dlg.querySelectorAll("#sTabs button").forEach(b => b.addEventListener("click", () => setTab(b.dataset.t)));
  let tab0 = "wled"; try { tab0 = localStorage.getItem("signSetTab") || "wled"; } catch (e) {}
  const ht = /^#settings-(\w+)$/.exec(location.hash); if (ht) tab0 = ht[1];
  setTab(tab0);
  function fillRest(r) {
    $("sRestAfter").value = (r && r.after_music) || "rest";
    $("sRestNow").textContent = r && r.look ? `Resting look: ${describeLook(r.look)}.` : "No resting look saved yet. Use ☾ Make resting next to Send.";
    $("sRestShow").disabled = $("sRestClear").disabled = !(r && r.look);
  }
  $("sRestShow").addEventListener("click", async () => {
    try { await postJSON("/api/rest/show", {}); say(sMsg, "Showing the resting look"); loadState(); } catch (e) { say(sMsg, e.message, true); }
  });
  $("sRestClear").addEventListener("click", async () => {
    try { const j = await postJSON("/api/rest", { look: null }); fillRest(j.rest); say(sMsg, "Resting look cleared"); loadState(); } catch (e) { say(sMsg, e.message, true); }
  });
  if (location.hash.startsWith("#settings")) openDlg();

  function describe(w) {
    if (!w.ok) return `Couldn't reach ${w.host}: ${w.error || "no answer"}`;
    const L = [`${w.name || "WLED"} ${w.version || ""} at ${w.ip || w.host}`,
      `${w.count} LEDs` + (w.realtime ? "" : " · UDP realtime is OFF (turn it on in WLED → Sync)") + (w.live ? " · receiving live data" + (w.liveSource ? " from " + w.liveSource : "") : "")];
    (w.outputs || []).forEach((o, i) => L.push(`LED${i + 1} · GPIO ${o.pin} · start ${o.start} · length ${o.len}`));
    if (w.geometry) {
      L.push(`2D matrix ${w.geometry.width} wide × ${w.geometry.height} tall` + ((w.panels || []).length > 1 ? ` in ${w.panels.length} panels` : ""));
      L.push(w.ledMaps ? "Realtime uses the 2D layout (“respect LED maps” on) → mapping: 2D matrix" : "“Respect LED maps” is OFF in WLED → realtime ignores the 2D layout. Turn it on (WLED → Sync → Realtime), or use Panels mapping here.");
    } else L.push("No 2D matrix in WLED → use Panels mapping here, or set up WLED → Config → 2D Configuration");
    L.push("WLED realtime gamma is " + (w.realtimeGamma ? "ON → set Gamma here to 1" : "OFF → keep Gamma here at about 2.2"));
    return L.join("\n");
  }
  $("sCheck").addEventListener("click", async () => {
    const h = f.host.value.trim(); out.hidden = false; out.textContent = `Asking ${h}…`;
    try {
      const w = await api("/api/wled?host=" + encodeURIComponent(h)).catch(e => e.body || { ok: false, host: h, error: e.message });
      lastWled = w.ok ? w : null; out.textContent = describe(w);
      if (w.ok && w.geometry && f.map.value === "matrix") { f.w.value = w.geometry.width; f.h.value = w.geometry.height; }
      showNote();
    } catch (e) { out.textContent = "Couldn't ask Signinator to check WLED."; }
  });
  $("sGeom").addEventListener("click", async () => {
    const h = f.host.value.trim(); out.hidden = false; out.textContent = `Reading the 2D setup from ${h}…`;
    try {
      const j = await postJSON("/api/wled/geometry", { host: h });
      lastWled = j.wled; f.map.value = "matrix"; f.w.value = j.geometry.width; f.h.value = j.geometry.height;
      out.textContent = describe(j.wled) + `\n\nThe sign is now ${j.geometry.width} × ${j.geometry.height}.`;
      showNote(); loadState();
    } catch (e) { out.textContent = e.message + (e.body && e.body.wled ? "\n\n" + describe(e.body.wled) : ""); }
  });
  document.querySelectorAll(".pat").forEach(b => b.addEventListener("click", async () => {
    try { await postJSON("/api/testpattern", { pattern: b.dataset.pat }); say(sMsg, "Showing test: " + b.textContent); loadState(); }
    catch (e) { say(sMsg, "Couldn't show the test", true); }
  }));
  $("setForm").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const host = f.host.value.trim();
    if (f.en.checked && !host) { say(sMsg, "Enter WLED's host name, or turn off Send to WLED.", true); f.host.focus(); return; }
    const panels = f.map.value === "panels";
    const mapping = panels
      ? { mode: "panels", tile_w: +f.tw.value, tile_h: +f.th.value, tiles_x: +f.tx.value, tiles_y: +f.ty.value }
      : { mode: "matrix", tile_w: +f.w.value, tile_h: +f.h.value, tiles_x: 1, tiles_y: 1 };
    if (Object.values(mapping).some(v => typeof v === "number" && !(v >= 1))) { say(sMsg, "Sizes must be 1 or more.", true); return; }
    Object.assign(mapping, { serpentine: f.serp.checked, tile_chain_serpentine: f.chain.checked, flip_h: f.fh.checked, flip_v: f.fv.checked });
    $("sSave").disabled = true; say(sMsg, "Saving…");
    try {
      await postJSON("/api/config", {
        wled: { host, port: +f.port.value || 4048, enabled: f.en.checked, auto_geometry: f.auto.checked },
        mapping, gamma: Math.max(1, Math.min(3, +f.gamma.value || 1)),
        color_correction: { r: +f.r.value, g: +f.g.value, b: +f.b.value },
        dither: { mode: $("sDitherMode").value, darkest: $("sDitherDark").checked, wled_fps: $("sDitherFps").value === "auto" ? "auto" : +$("sDitherFps").value, min_hz: +$("sDitherHz").value, fit: $("sDitherFit").checked },
        routing: { sign: $("sRouteSign").value, screen: $("sRouteScreen").value },
        mirror: { hosts: $("sMirror").value.split(/[\s,]+/).map(h => h.trim()).filter(Boolean), look: $("sMirrorLook").value,
                  size: +$("sMirrorSize").value, fps: Math.max(5, Math.min(60, +$("sMirrorFps").value || 30)), fps_auto: $("sMirrorFpsAuto").checked, photos: $("sMirrorPhotos").checked, enabled: $("sMirrorOn").checked },
        stereo: Object.assign({ enabled: $("sStOn").checked, url: $("sStUrl").value.trim(), entity: $("sStEnt").value,
                                off_after_s: +$("sStOff").value, only_own: $("sStOwn").checked },
                              $("sStTok").value.trim() ? { token: $("sStTok").value.trim() } : {}),
        mqtt: Object.assign({ enabled: $("sMqOn").checked, host: $("sMqHost").value.trim(), port: +$("sMqPort").value || 1883,
                              username: $("sMqUser").value.trim(), base: $("sMqBase").value.trim().toLowerCase() || "signinator" },
                            $("sMqPass").value ? { password: $("sMqPass").value } : {}),
        fps: Math.max(5, Math.min(240, +$("sFps").value || 40)), fps_auto: $("sFpsAuto").checked,
        audio: { auto: $("sAudioAuto").checked, delay_ms: Math.max(0, Math.min(5000, +$("sAudioDelay").value || 0)),
                 offset_ms: Math.max(-2000, Math.min(2000, +$("sAudioOff").value || 0)) },
        relay: { target: $("sRelay").value, volume: Math.max(0, Math.min(100, +$("sRelayVol").value || 0)), codec: $("sRelayCodec").value, volume_mode: $("sRelayVolMode").value },
        nowplaying: Object.assign({ enabled: $("sNpOn").checked, mode: $("sNpMode").value, hold: +$("sNpHold").value || 0,
                                    enhance: $("sNpEnh").checked, lift: $("sNpLift").checked,
                                    show: $("sNpShow").value, brief_s: Math.max(1, Math.min(600, +$("sNpBrief").value || 10)),
                                    art_delay: $("sNpDelayMode").value === "auto" ? "auto" : Math.max(0, Math.min(10000, +$("sNpDelay").value || 0)) },
                                  $("sNpTok").value.trim() ? { token: $("sNpTok").value.trim().toLowerCase() === "none" ? "" : $("sNpTok").value.trim() } : {}),
      });
      await postJSON("/api/rest", { after_music: $("sRestAfter").value });
      dlg.close(); status(f.en.checked ? "Settings saved. Sending to " + host + "." : "Settings saved. Preview only.");
      loadState();
    } catch (e) { say(sMsg, e.message, true); }
    $("sSave").disabled = false;
  });
})();

let VIEW_SET = false;
if (["#text", "#fx", "#gif"].includes(location.hash)) { setView(location.hash.slice(1)); VIEW_SET = true; }
else try { let v = localStorage.getItem("signView"); if (v === "image") v = "gif"; if (v === "text" || v === "fx" || v === "gif") { setView(v); VIEW_SET = true; } } catch (e) {}
sizeCanvas();
loadState().then(pollFrames);

// ---- stereo power: pick the entity, test it (saves the address/token first so Find works before Save) ----
async function stereoSaveConn() {
  const body = { url: $("sStUrl").value.trim() };
  if ($("sStTok").value.trim()) body.token = $("sStTok").value.trim();
  if ($("sStEnt").value) body.entity = $("sStEnt").value;
  await postJSON("/api/config", { stereo: body });
}
$("sStFind").addEventListener("click", async () => {
  const note = $("sStNote"); note.textContent = "Asking Home Assistant…";
  try {
    await stereoSaveConn();
    const r = await fetch("/api/stereo/entities"); const j = await r.json();
    if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    const sel = $("sStEnt"), want = sel.value;
    sel.innerHTML = '<option value="">(pick the stereo)</option>' + j.entities.map(e => `<option value="${e.id}">${e.name.replace(/</g, "&lt;")} · ${e.id} (${e.state})</option>`).join("");
    if (want) sel.value = want;
    note.textContent = `Found ${j.entities.length} on/off devices. Pick the stereo, then Save.`;
  } catch (e) { note.textContent = e.message; }
});
for (const [id, on] of [["sStTestOn", true], ["sStTestOff", false]]) $(id).addEventListener("click", async () => {
  const note = $("sStNote"); note.textContent = "Sending…";
  try {
    await stereoSaveConn();
    const r = await fetch("/api/stereo/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ on }) });
    const j = await r.json(); if (!r.ok) throw new Error(j.error || "HTTP " + r.status);
    note.textContent = `The stereo is ${j.state}.`;
  } catch (e) { note.textContent = e.message; }
});
