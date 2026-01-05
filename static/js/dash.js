(function () {
  const $ = (id) => document.getElementById(id);

  let viewsChart = null;
  let map = null;
  let markers = [];

  const fallbackStyle = "https://demotiles.maplibre.org/style.json";
  let ALL_STREETS = [];

  function fmtNum(n) {
    n = Number(n || 0);
    return n.toLocaleString();
  }

  function tag(mode) {
    const m = (mode || "walk").toLowerCase();
    return `tag ${m}`;
  }

  function buildWorldUrl(street) {
    const type = (street.type || "").toLowerCase();
    const mode = (street.mode || "walk").toLowerCase();
    const sid = street._id;

    // 3D -> world_walk
    if (type === "3d") {
      return `${window.ABTO.worldWalk}?street_id=${sid}`;
    }

    if (mode === "drive") return `${window.ABTO.worldDrive}?street_id=${sid}`;
    if (mode === "fly") return `${window.ABTO.worldFly}?street_id=${sid}`;
    if (mode === "sit") return `${window.ABTO.worldSit}?street_id=${sid}`;
    return `${window.ABTO.worldBase}?street_id=${sid}`;
  }

  async function fetchSummary(days) {
    const url = `${window.ABTO.apiSummaryUrl}?days=${encodeURIComponent(days)}`;
    const res = await fetch(url, { headers: { "Accept": "application/json" } });
    if (!res.ok) throw new Error("Failed to load dashboard data");
    return res.json();
  }

  function setUser(u) {
    $("userName").textContent = u?.name || "User";
    $("userEmail").textContent = u?.email || "";
    $("avatarLetter").textContent = (u?.name || u?.email || "U").trim().slice(0, 1).toUpperCase();

    const isAdmin = !!u?.is_admin || (String(u?.role || "").toLowerCase() === "admin");
    const badge = $("adminBadge");
    if (badge) badge.style.display = isAdmin ? "inline-flex" : "none";
  }

  function setStats(t) {
    $("statTotal").textContent = fmtNum(t.total_streets);
    $("statLikes").textContent = fmtNum(t.total_likes);
    $("statWalk").textContent = fmtNum(t.walk_count);
    $("statDrive").textContent = fmtNum(t.drive_count);
    $("statFly").textContent = fmtNum(t.fly_count);
    $("statSit").textContent = fmtNum(t.sit_count);
  }

  function drawViewsChart(series) {
    const ctx = $("viewsChart").getContext("2d");
    if (viewsChart) viewsChart.destroy();

    const labels = (series?.labels || []).map(d => {
      try {
        const parts = d.split("-");
        const dt = new Date(Date.UTC(parts[0], parts[1] - 1, parts[2]));
        return dt.toLocaleDateString(undefined, { month: "short", day: "2-digit" });
      } catch {
        return d;
      }
    });

    const data = series?.data || [];
    const total = data.reduce((a, b) => a + (Number(b) || 0), 0);
    $("viewsMeta").textContent = `${fmtNum(total)} views in range`;

    viewsChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [{
          label: "Views",
          data,
          borderColor: "#38bdf8",
          backgroundColor: "rgba(56,189,248,.12)",
          borderWidth: 3,
          fill: true,
          tension: 0.35,
          pointRadius: 3,
          pointHoverRadius: 5
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: {
            beginAtZero: true,
            grid: { color: "rgba(148,163,184,.12)" },
            ticks: { color: "#9ca3af", font: { size: 11 } }
          },
          x: {
            grid: { display: false },
            ticks: { color: "#9ca3af", font: { size: 11 } }
          }
        }
      }
    });
  }

  function escapeHtml(str) {
    return String(str || "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function renderTopViews(list) {
    const el = $("topViewsList");
    if (!list || !list.length) {
      el.innerHTML = `<div class="small">No view events yet. Once you log "view_street" events, this will show real ranking.</div>`;
      return;
    }

    el.innerHTML = list.map(item => `
      <div class="row clickable" data-id="${item.streetId}">
        <div class="left">
          <div class="t">${escapeHtml(item.name || "Unknown")}</div>
          <div class="s">${escapeHtml((item.city || "") + (item.country ? ", " + item.country : ""))}</div>
        </div>
        <div class="${tag(item.mode)}">${(item.mode || "walk").toUpperCase()}</div>
        <div class="small">${fmtNum(item.views)} views</div>
      </div>
    `).join("");
  }

  function renderTopLikes(list) {
    const el = $("topLikesList");
    if (!list || !list.length) {
      el.innerHTML = `<div class="small">No streets yet.</div>`;
      return;
    }

    el.innerHTML = list.map(s => `
      <div class="row clickable" data-open="${s._id}">
        <div class="left">
          <div class="t">${escapeHtml(s.name || "Untitled")}</div>
          <div class="s">${escapeHtml((s.city || "") + (s.country ? ", " + s.country : ""))}</div>
        </div>
        <div class="${tag(s.mode)}">${(s.mode || "walk").toUpperCase()}</div>
        <div class="small">♥ ${fmtNum(s.likes)}</div>
      </div>
    `).join("");
  }

  function applyStreetFilters() {
    const q = ($("streetSearch")?.value || "").trim().toLowerCase();
    const mode = ($("modeFilter")?.value || "all").toLowerCase();

    let filtered = ALL_STREETS.slice();

    if (mode !== "all") {
      filtered = filtered.filter(s => String(s.mode || "").toLowerCase() === mode);
    }

    if (q) {
      filtered = filtered.filter(s => {
        const hay = [
          s.name || "",
          s.city || "",
          s.country || "",
          s.type || "",
          s.mode || ""
        ].join(" ").toLowerCase();
        return hay.includes(q);
      });
    }

    return filtered;
  }

  function renderStreetsQuick(streets) {
    const el = $("streetsList");
    if (!streets || !streets.length) {
      el.innerHTML = `<div class="small">No streets yet — upload your first one.</div>`;
      $("streetsMeta").textContent = "";
      return;
    }

    $("streetsMeta").textContent = `${fmtNum(streets.length)} streets shown`;
    const top = streets.slice(0, 20);

    el.innerHTML = top.map(s => {
      const mode = (s.mode || "walk").toLowerCase();
      const delAction = `/street/${encodeURIComponent(s._id)}/delete`;

      return `
        <div class="row clickable" data-open="${s._id}">
          <div class="left">
            <div class="t">${escapeHtml(s.name || "Untitled")}</div>
            <div class="s">${escapeHtml((s.city || "Unknown") + ", " + (s.country || "Unknown"))}</div>
          </div>

          <div class="${tag(mode)}">${mode.toUpperCase()}</div>

          <form method="POST" action="${delAction}" style="margin:0;" data-delete-form="1">
            <button type="submit" class="mini danger" title="Delete street">
              <i class="fa-solid fa-trash"></i>
            </button>
          </form>
        </div>
      `;
    }).join("");

    wireDeleteConfirm();
  }

  function wireDeleteConfirm() {
    document.querySelectorAll('[data-delete-form="1"]').forEach(form => {
      form.addEventListener("submit", (e) => {
        e.stopPropagation();
        const ok = confirm("Delete this street? This cannot be undone.");
        if (!ok) e.preventDefault();
      });
    });

    // prevent row click when clicking delete
    document.querySelectorAll('[data-delete-form="1"] button').forEach(btn => {
      btn.addEventListener("click", (e) => e.stopPropagation());
    });
  }

  function renderActivity(recent) {
    const el = $("activityList");
    if (!recent || !recent.length) {
      el.innerHTML = `<div class="small">No activity logs yet.</div>`;
      return;
    }

    el.innerHTML = recent.map(a => {
      const when = a.timestamp ? new Date(a.timestamp).toLocaleString() : "";
      const title = a.streetName || "Activity";
      const place = (a.city || "—") + (a.country ? ", " + a.country : "");
      const mode = (a.mode || "walk").toLowerCase();
      return `
        <div class="row clickable" data-id="${a.streetId || ""}">
          <div class="left">
            <div class="t">${escapeHtml(title)}</div>
            <div class="s">${escapeHtml((a.eventType || "event") + " • " + place + (when ? " • " + when : ""))}</div>
          </div>
          <div class="${tag(mode)}">${mode.toUpperCase()}</div>
          <div class="small"></div>
        </div>
      `;
    }).join("");
  }

  function initMap(streets) {
    const style = (window.ABTO.mapStyle && window.ABTO.mapStyle.trim()) ? window.ABTO.mapStyle : fallbackStyle;

    if (!map) {
      map = new maplibregl.Map({
        container: "map",
        style,
        center: [55.2708, 25.2048],
        zoom: 2
      });
      map.addControl(new maplibregl.NavigationControl({ showCompass: true }), "top-right");
    }

    markers.forEach(m => m.remove());
    markers = [];

    const points = (streets || []).filter(s => s.lat != null && s.lng != null).slice(0, 400);

    points.forEach(s => {
      const m = new maplibregl.Marker({ color: "#38bdf8" })
        .setLngLat([s.lng, s.lat])
        .setPopup(new maplibregl.Popup({ closeButton: true }).setHTML(`
          <div style="font-family:Inter,system-ui; font-size:12px;">
            <b>${escapeHtml(s.name || "Untitled")}</b><br/>
            ${escapeHtml((s.city || "") + (s.country ? ", " + s.country : ""))}<br/>
            <span style="opacity:.8">${escapeHtml((s.type || "").toUpperCase())} ${(s.mode || "").toUpperCase()}</span>
          </div>
        `))
        .addTo(map);

      markers.push(m);
    });

    if (points.length >= 2) {
      const bounds = new maplibregl.LngLatBounds();
      points.forEach(p => bounds.extend([p.lng, p.lat]));
      map.fitBounds(bounds, { padding: 40, maxZoom: 7, duration: 650 });
    }
  }

  function wireClicks(streets) {
    // open world on row click
    document.querySelectorAll("[data-open]").forEach(el => {
      el.addEventListener("click", () => {
        const id = el.getAttribute("data-open");
        const s = (streets || []).find(x => x._id === id);
        if (!s) return;
        window.location.href = buildWorldUrl(s);
      });
    });

    document.querySelectorAll("#topViewsList [data-id], #activityList [data-id]").forEach(el => {
      el.addEventListener("click", () => {
        const id = el.getAttribute("data-id");
        if (!id) return;
        const s = (streets || []).find(x => x._id === id);
        if (!s) return;
        window.location.href = buildWorldUrl(s);
      });
    });
  }

  function attachFilters() {
    const search = $("streetSearch");
    const mode = $("modeFilter");

    const rerender = () => {
      const filtered = applyStreetFilters();
      renderStreetsQuick(filtered);
      wireClicks(filtered);
    };

    if (search) search.addEventListener("input", rerender);
    if (mode) mode.addEventListener("change", rerender);
  }

  async function load(days) {
    const data = await fetchSummary(days);

    setUser(data.user);
    setStats(data.totals);
    drawViewsChart(data.views_chart);

    renderTopViews(data.top_views);
    renderTopLikes(data.top_likes);

    ALL_STREETS = data.streets || [];
    const filtered = applyStreetFilters();
    renderStreetsQuick(filtered);

    renderActivity(data.recent);
    initMap(ALL_STREETS);

    wireClicks(ALL_STREETS);
    attachFilters();
  }

  // init
  const sel = $("daysSelect");
  sel.addEventListener("change", async () => {
    try {
      await load(parseInt(sel.value, 10));
    } catch (e) {
      console.error(e);
      alert("Failed to load dashboard data.");
    }
  });

  load(parseInt(sel.value, 10)).catch(e => {
    console.error(e);
    alert("Failed to load dashboard data.");
  });
})();
