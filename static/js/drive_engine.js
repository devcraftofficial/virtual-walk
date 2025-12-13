/* ==========================================================
   DRIVE ENGINE v3.0 — Optimized & Synced With Enhanced UI
   Author: ChatGPT (for Elvin)
   ----------------------------------------------------------
   SYSTEMS:
   - State Manager
   - Video Engine
   - Speed + Gear + Fuel HUD
   - Joystick Input
   - Keyboard Input
   - Full Map System
   - Settings Engine
   - Modals + UI System
   - Physics Loop (60FPS)
========================================================== */

/* -----------------------
   CONFIG + DATA
----------------------- */
const CONFIG = JSON.parse(document.getElementById("app-config").textContent || "{}");
const STREETS = JSON.parse(document.getElementById("streets-data").textContent || "[]");
const SELECTED_STREET = JSON.parse(document.getElementById("selected-street-data").textContent || "null");

/* -----------------------
   DOM ELEMENTS
----------------------- */
const EL = {
  loading: document.getElementById("loadingScreen"),
  video: document.getElementById("streetVideo"),
  videoSource: document.getElementById("videoSource"),
  videoLoading: document.getElementById("videoLoading"),

  // Map
  miniMap: document.getElementById("miniMapStatic"),
  mapOverlay: document.getElementById("fullMapOverlay"),
  mapContainer: document.getElementById("fullMapContainer"),
  mapClose: document.getElementById("closeMapBtn"),

  // HUD
  streetName: document.getElementById("hudStreetName"),
  streetMeta: document.getElementById("hudStreetMeta"),
  speedArc: document.getElementById("speedArc"),
  speedValue: document.getElementById("speedValue"),
  gear: document.getElementById("gearBadge"),
  fuelBar: document.getElementById("fuelBar"),
  toast: document.getElementById("toastNotification"),

  // Controls
  joystickBase: document.getElementById("joystickBase"),
  joystickKnob: document.getElementById("joystickKnob"),
  pauseBtn: document.getElementById("btnPause"),

  // Settings
  settingsBtn: document.getElementById("btnSettings"),
  settingsPanel: document.getElementById("settingsPanel"),
  settingsClose: document.getElementById("btnCloseSettings"),
  resetBtn: document.getElementById("resetDefaults"),
  saveBtn: document.getElementById("saveSettings"),

  // Toggles
  tAuto: document.getElementById("toggleAuto"),
  tNight: document.getElementById("toggleNight"),
  tShake: document.getElementById("toggleShake"),
  tSfx: document.getElementById("toggleSfx"),
  tVideoAudio: document.getElementById("toggleVideoAudio"),
  tFullscreen: document.getElementById("toggleFullscreen"),
  volumeSlider: document.getElementById("volumeSlider"),
  volumeValue: document.getElementById("volumeValue"),

  // Help modal
  helpBtn: document.getElementById("helpBtn"),
  helpModal: document.getElementById("helpModal"),
  helpClose: document.getElementById("closeHelp"),
  helpOk: document.getElementById("helpGotIt")
};

/* -----------------------
   ENGINE STATE
----------------------- */
const STATE = {
  index: -1,
  videos: [],
  videoIndex: 0,
  speed: 0,
  targetSpeed: 0,
  dir: 0,          // -1, 0, 1
  keyDir: 0,
  joyDir: 0,
  fuel: 100,

  // Effects
  shake: false,
  headBob: 0,
  bobDir: 1,

  // Flags
  paused: false,
  settingsOpen: false,

  // Map
  map: null,
  mapReady: false,

  // Perf
  last: 0,
  raf: null
};

/* ==========================================================
   🔧 UTILITY HELPERS
========================================================== */
const U = {
  clamp(v, min, max) { return Math.max(min, Math.min(max, v)); },
  lerp(a, b, t) { return a + (b - a) * t; },

  toast(msg, type = "info") {
    EL.toast.textContent = msg;
    EL.toast.className = `toast ${type} show`;
    setTimeout(() => EL.toast.classList.remove("show"), 2500);
  },

  play() { EL.video.play().catch(() => {}); },
  pause() { if (!EL.video.paused) EL.video.pause(); },

  saveSettings() {
    const s = {
      auto: EL.tAuto.checked,
      night: EL.tNight.checked,
      shake: EL.tShake.checked,
      sfx: EL.tSfx.checked,
      audio: EL.tVideoAudio.checked,
      vol: EL.volumeSlider.value
    };
    localStorage.setItem("driveSettings", JSON.stringify(s));
  },

  loadSettings() {
    const s = JSON.parse(localStorage.getItem("driveSettings") || "{}");
    EL.tAuto.checked = s.auto ?? false;
    EL.tNight.checked = s.night ?? false;
    EL.tShake.checked = s.shake ?? false;
    EL.tSfx.checked = s.sfx ?? true;
    EL.tVideoAudio.checked = s.audio ?? false;

    EL.volumeSlider.value = s.vol ?? 50;
    EL.volumeValue.textContent = `${EL.volumeSlider.value}%`;
    EL.video.volume = (s.vol ?? 50) / 100;
  }
};

/* ==========================================================
   🎬 LOAD STREET
========================================================== */
function loadStreet(street) {
  EL.streetName.textContent = street.name || "Street";
  EL.streetMeta.textContent = `${street.city || ""}${street.city && street.country ? ", " : ""}${street.country || ""}`;

  STATE.videos = street.videos ? street.videos.map(v => v.url) : [];
  STATE.videoIndex = 0;

  EL.videoSource.src = STATE.videos[0] || "";
  EL.video.load();
  EL.video.currentTime = 0;
  U.pause();

  STATE.speed = 0;
  STATE.targetSpeed = 0;
  STATE.dir = 0;
  STATE.fuel = 100;

  updateHUD(0);
  setGear("N");
}

/* ==========================================================
   🎚 GEAR & SPEED HUD
========================================================== */
function setGear(symbol) {
  EL.gear.className = "gear-badge";
  EL.gear.textContent = symbol;

  if (symbol === "R") EL.gear.classList.add("gear-r");
  else if (symbol === "N") EL.gear.classList.add("gear-n");
  else EL.gear.classList.add("gear-d");
}

function updateHUD(dt) {
  STATE.speed = U.lerp(STATE.speed, STATE.targetSpeed, 0.15);
  STATE.fuel = Math.max(0, STATE.fuel - STATE.speed * 0.001 * dt);

  const spd = U.clamp(STATE.speed, 0, 100);
  EL.speedValue.textContent = Math.round(spd);

  const arcMax = 251.2;
  EL.speedArc.style.strokeDashoffset = arcMax - (arcMax * spd / 100);

  if (STATE.dir === -1) setGear("R");
  else if (spd < 2) setGear("N");
  else if (spd < 30) setGear("1");
  else if (spd < 55) setGear("2");
  else if (spd < 80) setGear("3");
  else setGear("4");

  EL.fuelBar.style.height = `${(STATE.fuel / 100) * 70}px`;
}

/* ==========================================================
   🗺 FULL MAP
========================================================== */
function initMap() {
  if (STATE.mapReady) return;

  STATE.map = new maplibregl.Map({
    container: EL.mapContainer,
    style: "https://demotiles.maplibre.org/style.json",
    zoom: 2,
    center: [0, 20]
  });

  STATE.map.addControl(new maplibregl.NavigationControl());

  STREETS.forEach((s, i) => {
    if (!s.lat || !s.lng) return;

    const el = document.createElement("div");
    el.className = "street-marker";
    el.innerHTML = `<div class='marker-dot'></div>`;

    new maplibregl.Marker({ element: el })
      .setLngLat([s.lng, s.lat])
      .addTo(STATE.map)
      .getElement()
      .addEventListener("click", () => {
        loadStreetByIndex(i);
        hideMap();
      });
  });

  STATE.mapReady = true;
}

function showMap() {
  EL.mapOverlay.classList.add("active");
  initMap();

  const s = STREETS[STATE.index];
  if (s?.lat && s?.lng) {
    STATE.map.easeTo({ center: [s.lng, s.lat], zoom: 14 });
  }
}

function hideMap() {
  EL.mapOverlay.classList.remove("active");
}

/* ==========================================================
   🎮 MOVEMENT ENGINE
========================================================== */
function applyDir() {
  const dir = STATE.keyDir || STATE.joyDir || (EL.tAuto.checked ? 1 : 0);
  STATE.dir = dir;

  if (dir === 1) {
    STATE.targetSpeed = 75;
    U.play();
  } else if (dir === -1) {
    STATE.targetSpeed = 25;
    reverseVideo();
  } else {
    STATE.targetSpeed = 0;
    U.pause();
  }
}

function reverseVideo() {
  function step() {
    if (STATE.dir !== -1) return;
    EL.video.currentTime =
      EL.video.currentTime <= 0.02
        ? EL.video.duration
        : EL.video.currentTime - 0.02;
    requestAnimationFrame(step);
  }
  step();
}

/* ==========================================================
   🎮 JOYSTICK SYSTEM
========================================================== */
function initJoystick() {
  let active = false;

  EL.joystickBase.addEventListener("pointerdown", e => {
    active = true;
    EL.joystickBase.setPointerCapture(e.pointerId);
    move(e);
  });

  EL.joystickBase.addEventListener("pointermove", e => {
    if (active) move(e);
  });

  EL.joystickBase.addEventListener("pointerup", reset);
  EL.joystickBase.addEventListener("pointercancel", reset);

  function move(e) {
    const r = EL.joystickBase.getBoundingClientRect();
    const cx = r.left + r.width / 2;
    const cy = r.top + r.height / 2;

    const dx = e.clientX - cx;
    const dy = e.clientY - cy;
    const dist = Math.min(1, Math.hypot(dx, dy) / (r.width / 2));
    const angle = Math.atan2(dy, dx);

    EL.joystickKnob.style.transform =
      `translate(${Math.cos(angle) * 35}px, ${Math.sin(angle) * 35}px)`;

    const moveY = -Math.sin(angle) * dist;
    STATE.joyDir = Math.abs(moveY) > 0.2 ? (moveY > 0 ? 1 : -1) : 0;

    applyDir();
  }

  function reset() {
    STATE.joyDir = 0;
    EL.joystickKnob.style.transform = "translate(0,0)";
    applyDir();
  }
}

/* ==========================================================
   🎹 KEYBOARD INPUT
========================================================== */
function initKeyboard() {
  document.addEventListener("keydown", e => {
    if (["ArrowUp", "w", "W"].includes(e.key)) {
      STATE.keyDir = 1;
      applyDir();
    }
    if (["ArrowDown", "s", "S"].includes(e.key)) {
      STATE.keyDir = -1;
      applyDir();
    }
    if (e.key === " ") STATE.targetSpeed = 0;
    if (e.key === "m" || e.key === "M") showMap();
  });

  document.addEventListener("keyup", e => {
    if (["ArrowUp", "w", "W", "ArrowDown", "s", "S"].includes(e.key)) {
      STATE.keyDir = 0;
      applyDir();
    }
  });
}

/* ==========================================================
   🧰 SETTINGS PANEL
========================================================== */
function initSettings() {
  EL.settingsBtn.addEventListener("click", () => {
    STATE.settingsOpen = !STATE.settingsOpen;
    EL.settingsPanel.classList.toggle("active", STATE.settingsOpen);
  });

  EL.settingsClose.addEventListener("click", () => {
    STATE.settingsOpen = false;
    EL.settingsPanel.classList.remove("active");
  });

  EL.volumeSlider.addEventListener("input", () => {
    const v = EL.volumeSlider.value;
    EL.volumeValue.textContent = `${v}%`;
    EL.video.volume = v / 100;
  });

  EL.resetBtn.addEventListener("click", () => {
    localStorage.removeItem("driveSettings");
    U.loadSettings();
    U.toast("Settings reset", "success");
  });

  EL.saveBtn.addEventListener("click", () => {
    U.saveSettings();
    U.toast("Saved ✔", "success");
  });

  EL.tFullscreen.addEventListener("change", () => {
    if (EL.tFullscreen.checked) document.documentElement.requestFullscreen();
    else document.exitFullscreen();
  });
}

/* ==========================================================
   🎥 VIDEO EVENTS
========================================================== */
function initVideo() {
  EL.video.addEventListener("loadeddata", () => {
    EL.videoLoading.style.display = "none";
  });

  EL.video.addEventListener("ended", () => {
    if (STATE.videoIndex < STATE.videos.length - 1) {
      STATE.videoIndex++;
      EL.videoSource.src = STATE.videos[STATE.videoIndex];
      EL.video.load();
      U.play();
    } else {
      STATE.targetSpeed = 0;
      U.pause();
    }
  });
}

/* ==========================================================
   ❓ HELP MODAL
========================================================== */
function initHelp() {
  EL.helpBtn.addEventListener("click", () => EL.helpModal.showModal());
  EL.helpClose.addEventListener("click", () => EL.helpModal.close());
  EL.helpOk.addEventListener("click", () => EL.helpModal.close());
}

/* ==========================================================
   🔁 GAME LOOP (60 FPS)
========================================================== */
function loop(t) {
  const dt = t - STATE.last;
  STATE.last = t;

  // Shake & head bob
  if (!STATE.paused && EL.tShake.checked && STATE.dir !== 0) {
    STATE.headBob += STATE.bobDir * 0.4 * (STATE.speed / 60);
    if (STATE.headBob > 3 || STATE.headBob < -3) STATE.bobDir *= -1;
  } else {
    STATE.headBob *= 0.9;
  }

  EL.video.style.transform = `translateY(${STATE.headBob}px)`;

  updateHUD(dt);

  STATE.raf = requestAnimationFrame(loop);
}

/* ==========================================================
   🚀 INITIALIZE ENGINE
========================================================== */
function init() {
  U.loadSettings();
  initJoystick();
  initKeyboard();
  initSettings();
  initHelp();
  initVideo();

  // Mini map
  EL.miniMap.addEventListener("click", showMap);
  EL.mapClose.addEventListener("click", hideMap);

  // Load initial street
  if (SELECTED_STREET) {
    const idx = STREETS.findIndex(s => s._id === SELECTED_STREET._id);
    loadStreetByIndex(idx !== -1 ? idx : 0);
  } else {
    loadStreetByIndex(0);
  }

  // Hide loader
  setTimeout(() => EL.loading.classList.add("hidden"), 600);

  STATE.last = performance.now();
  loop(STATE.last);
}

function loadStreetByIndex(i) {
  STATE.index = i;
  document.body.classList.add("fade");
  setTimeout(() => {
    loadStreet(STREETS[i]);
    document.body.classList.remove("fade");
  }, 250);
}

/* ========================================================== */
document.readyState === "loading"
  ? document.addEventListener("DOMContentLoaded", init)
  : init();
