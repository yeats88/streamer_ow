const ROLE = { tank: "坦克", dps: "输出", support: "支援", flex: "全能", coach: "教练", manager: "管理" };
const PLATFORM = { twitch: "Twitch", chzzk: "CHZZK", soop: "SOOP", bilibili: "哔哩哔哩", youtube: "YouTube" };
const STATE = { live: "直播中", offline: "未开播", unknown: "查询中" };

const favorites = new Set(JSON.parse(localStorage.getItem("owcs-favorites") || "[]"));
let notifyOn = localStorage.getItem("owcs-notify") === "1";
let previousLive = new Set();
let players = [];
let ready = false;

const $ = (id) => document.getElementById(id);

function saveFavorites() {
  localStorage.setItem("owcs-favorites", JSON.stringify([...favorites]));
}

function selectedKinds() {
  return new Set([...document.querySelectorAll("#kinds input:checked")].map((input) => input.value));
}

function matches(player) {
  const q = $("search").value.trim().toLowerCase();
  const region = $("region").value;
  const role = $("role").value;
  const team = $("team").value;
  const kinds = selectedKinds();
  if ($("fav-only").checked && !favorites.has(player.id)) return false;
  if (kinds.size && !kinds.has(player.kind) && !(player.kind === "player" && kinds.has("player"))) return false;
  if (kinds.size && !kinds.has(player.kind)) return false;
  if (region && player.region !== region) return false;
  if (role && player.role !== role) return false;
  if (team && player.team !== team) return false;
  if (!q) return true;
  const hay = [player.name, player.team, player.aliases, player.heroes, player.nationality].join(" ").toLowerCase();
  return hay.includes(q);
}

function platformLinks(player) {
  return Object.entries(player.links || {}).map(([key, url]) => {
    const live = (player.livePlatforms || []).includes(key) ? " live" : "";
    return `<a class="${live.trim()}" href="${url}" target="_blank" rel="noreferrer">${PLATFORM[key] || key}</a>`;
  }).join("");
}

function elapsed(value) {
  if (!value) return "";
  const date = value.includes("T") ? new Date(value) : new Date(value.replace(" ", "T") + "+09:00");
  if (Number.isNaN(date.getTime())) return "";
  const mins = Math.max(0, Math.round((Date.now() - date.getTime()) / 60000));
  if (mins < 60) return `${mins} 分钟`;
  return `${Math.floor(mins / 60)} 小时 ${mins % 60} 分`;
}

function starButton(id) {
  return `<button class="star ${favorites.has(id) ? "on" : ""}" data-fav="${id}" type="button">${favorites.has(id) ? "★" : "☆"}</button>`;
}

function render() {
  const shown = players.filter(matches);
  const live = shown.filter((player) => player.state === "live");
  $("live-count").textContent = String(players.filter((player) => player.state === "live").length);
  $("roster-count").textContent = String(players.length);
  $("filtered-count").textContent = `${shown.length} 个频道`;
  $("live-empty").hidden = live.length > 0;
  $("live-grid").innerHTML = live.map((player) => `
    <article class="card">
      <a class="thumb" href="${player.watchUrl}" target="_blank" rel="noreferrer" style="${player.thumbnail ? `background-image:url('${String(player.thumbnail).replace(/["'()]/g, "")}')` : ""}">
        <span class="viewers">${player.viewers ? player.viewers.toLocaleString() + " 观众" : "直播中"}</span>
      </a>
      <div class="card-body">
        <h3><span class="who"><span class="badge">LIVE</span><span class="name">${escapeHtml(player.name)}</span></span>${starButton(player.id)}</h3>
        <p class="title">${escapeHtml(player.title || "无标题")}</p>
        <div class="meta">
          <span>${escapeHtml(player.team || "自由选手")}</span>
          <span>${escapeHtml(player.regionLabel || "")}</span>
          <span>${ROLE[player.role] || ""}</span>
          <span>${elapsed(player.startedAt)}</span>
        </div>
        <div class="platforms">${platformLinks(player)}</div>
      </div>
    </article>
  `).join("");

  $("roster-body").innerHTML = shown.map((player) => `
    <tr class="${player.state}">
      <td>${starButton(player.id)}</td>
      <td><a href="${player.watchUrl || "#"}" target="_blank" rel="noreferrer">${escapeHtml(player.name)}</a></td>
      <td>${escapeHtml(player.team || "—")}</td>
      <td>${escapeHtml(player.regionLabel || "—")}</td>
      <td>${ROLE[player.role] || "—"}</td>
      <td class="state-${player.state}">${STATE[player.state] || player.state}${player.viewers ? " · " + player.viewers.toLocaleString() : ""}</td>
      <td class="platforms">${platformLinks(player)} <button class="ghost" data-hide="${player.id}" type="button">隐藏</button></td>
    </tr>
  `).join("");
}

function escapeHtml(value) {
  return String(value || "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

function fillFilters() {
  const regions = new Map();
  const teams = new Map();
  players.forEach((player) => {
    regions.set(player.region || "", player.regionLabel || "未分区");
    if (player.team) teams.set(player.team, (teams.get(player.team) || 0) + 1);
  });
  const regionValue = $("region").value;
  const teamValue = $("team").value;
  $("region").innerHTML = `<option value="">全部赛区</option>` + [...regions.entries()]
    .sort((a, b) => a[1].localeCompare(b[1], "zh"))
    .map(([id, label]) => `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`).join("");
  $("team").innerHTML = `<option value="">全部队伍</option>` + [...teams.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([name]) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  $("region").value = regionValue;
  $("team").value = teamValue;
}

async function refresh() {
  const response = await fetch("/api/players");
  const data = await response.json();
  players = data.players || [];
  $("updated-at").textContent = data.updatedAt ? new Date(data.updatedAt).toLocaleTimeString() : "等待扫描";
  $("check-state").textContent = data.checking ? "正在扫描平台…" : "已更新";
  const errors = $("errors");
  errors.hidden = !data.errors?.length;
  errors.textContent = (data.errors || []).join("；");
  if (!ready) {
    fillFilters();
    ready = true;
  } else if (!$("team").options.length) {
    fillFilters();
  }
  notifyLive();
  render();
  if (data.settings) {
    $("settings-form").intervalSec.value = data.settings.intervalSec;
    $("settings-form").youtubeMode.value = data.settings.youtubeMode;
  }
}

function notifyLive() {
  const liveIds = new Set(players.filter((player) => player.state === "live" && favorites.has(player.id)).map((player) => player.id));
  if (notifyOn && Notification.permission === "granted" && previousLive.size) {
    players.filter((player) => liveIds.has(player.id) && !previousLive.has(player.id)).forEach((player) => {
      new Notification(`${player.name} 开播了`, { body: player.title || player.team || "点击前往直播间" });
    });
  }
  previousLive = liveIds;
}

document.body.addEventListener("click", async (event) => {
  const fav = event.target.closest("[data-fav]");
  if (fav) {
    const id = fav.dataset.fav;
    favorites.has(id) ? favorites.delete(id) : favorites.add(id);
    saveFavorites();
    render();
  }
  const hide = event.target.closest("[data-hide]");
  if (hide) {
    await fetch("/api/hidden", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id: hide.dataset.hide, hidden: true }) });
    players = players.filter((player) => player.id !== hide.dataset.hide);
    render();
  }
});

["search", "region", "role", "team", "fav-only"].forEach((id) => $(id).addEventListener("input", render));
$("kinds").addEventListener("change", render);

$("notify-btn").addEventListener("click", async () => {
  if (!notifyOn) {
    const permission = await Notification.requestPermission();
    if (permission !== "granted") return;
    notifyOn = true;
  } else {
    notifyOn = false;
  }
  localStorage.setItem("owcs-notify", notifyOn ? "1" : "0");
  $("notify-btn").textContent = notifyOn ? "提醒已开" : "开播提醒";
});
$("notify-btn").textContent = notifyOn ? "提醒已开" : "开播提醒";

$("add-btn").addEventListener("click", () => $("add-dialog").showModal());
$("add-cancel").addEventListener("click", () => $("add-dialog").close());
$("add-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = Object.fromEntries(new FormData(event.target).entries());
  const response = await fetch("/api/players", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  $("add-error").textContent = data.ok ? "" : (data.error || "保存失败");
  if (data.ok) {
    event.target.reset();
    $("add-dialog").close();
    ready = false;
    refresh();
  }
});

$("settings-btn").addEventListener("click", () => $("settings-dialog").showModal());
$("settings-cancel").addEventListener("click", () => $("settings-dialog").close());
$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const body = Object.fromEntries(new FormData(event.target).entries());
  const response = await fetch("/api/settings", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  $("settings-error").textContent = data.ok ? "已保存，下一轮扫描生效" : (data.error || "保存失败");
});
$("refresh-roster").addEventListener("click", async () => {
  $("settings-error").textContent = "正在更新名册…";
  const response = await fetch("/api/roster/refresh", { method: "POST" });
  const data = await response.json();
  $("settings-error").textContent = data.ok ? `名册已更新，${data.count} 个有直播间的条目` : (data.error || "更新失败");
  if (data.ok) {
    ready = false;
    refresh();
  }
});

$("copy-live").addEventListener("click", async () => {
  const lines = players.filter(matches).filter((player) => player.state === "live")
    .map((player) => `${player.name} · ${player.team || "自由选手"} · ${player.viewers || 0} · ${player.watchUrl}`);
  await navigator.clipboard.writeText(lines.join("\n") || "当前没有开播");
  $("copy-live").textContent = "已复制";
  setTimeout(() => { $("copy-live").textContent = "复制开播列表"; }, 1200);
});

refresh();
setInterval(refresh, 15000);
