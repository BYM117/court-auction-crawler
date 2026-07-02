const state = {
  items: [],
  selectedKey: "",
  query: "",
  region: "",
  source: "",
  status: "",
  total: 0,
  offset: 0,
  limit: 200,
  loadingItems: false,
  exhausted: false,
  sources: new Set(),
  statuses: new Set(),
  collector: {},
};

const els = {
  syncLine: document.querySelector("#syncLine"),
  collectorStatus: document.querySelector("#collectorStatus"),
  collectorDot: document.querySelector("#collectorDot"),
  collectorState: document.querySelector("#collectorState"),
  collectorCurrent: document.querySelector("#collectorCurrent"),
  collectorResult: document.querySelector("#collectorResult"),
  collectorAge: document.querySelector("#collectorAge"),
  collectorProgress: document.querySelector("#collectorProgress"),
  metrics: document.querySelector("#metrics"),
  rows: document.querySelector("#itemRows"),
  tableWrap: document.querySelector(".tableWrap"),
  listStatus: document.querySelector("#listStatus"),
  search: document.querySelector("#searchInput"),
  region: document.querySelector("#regionSelect"),
  source: document.querySelector("#sourceSelect"),
  status: document.querySelector("#statusSelect"),
  collectorStart: document.querySelector("#collectorStartButton"),
  collectorStop: document.querySelector("#collectorStopButton"),
  detailEmpty: document.querySelector("#detailEmpty"),
  detailContent: document.querySelector("#detailContent"),
  detailTitle: document.querySelector("#detailTitle"),
  detailSub: document.querySelector("#detailSub"),
  detailLink: document.querySelector("#detailLink"),
  detailFields: document.querySelector("#detailFields"),
  eventList: document.querySelector("#eventList"),
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function fmt(value) {
  return value && String(value).trim() ? escapeHtml(value) : "-";
}

function fmtTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", { hour12: false });
}

function safeExternalUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value, window.location.href);
    return ["http:", "https:"].includes(url.protocol) ? url.href : "";
  } catch {
    return "";
  }
}

async function loadStats() {
  const stats = await fetchJson("/api/stats");
  const latest = stats.latest_sync;
  const control = stats.collector_control || {};
  const syncText = latest
    ? `마지막 동기화 ${fmtTime(latest.finished_at || latest.started_at)} · ${latest.status}`
    : "아직 동기화 기록이 없습니다.";
  const controlText = control.enabled
    ? `자동 수집 켜짐 · ${Math.round((control.interval_seconds || 10800) / 3600)}시간 주기`
    : "자동 수집 꺼짐";
  els.syncLine.textContent = `${syncText} · ${controlText}`;
  els.collectorStart.disabled = Boolean(control.enabled);
  els.collectorStop.disabled = !control.enabled;
  state.collector = stats.collector || {};
  renderCollector(state.collector);

  const inserted = latest?.summary?.inserted ?? 0;
  const updated = latest?.summary?.updated ?? 0;
  const collected = latest?.summary?.collected ?? 0;
  const sources = Object.fromEntries((stats.by_source || []).map((row) => [row.name, row.count]));
  const collectorRunning = isCollectorRunning(state.collector);
  const progress = collectorProgressLabel(state.collector);
  els.metrics.innerHTML = [
    metric(collectorRunning ? "현재 저장 물건" : "전체 물건", stats.total),
    metric("활성 물건", stats.active || 0),
    metric("확인 예정", stats.due || 0),
    progress ? metric("수집 진행", progress) : "",
    metric("진행", sources["진행"] || 0),
    metric("예정", sources["예정"] || 0),
    metric("최근 수집", collected),
    metric("신규", inserted),
    metric("변경", updated),
  ].join("");

  updateSourceOptions(stats.by_source || []);
  updateStatusOptions(stats.by_status || []);
}

function renderCollector(collector) {
  const state = collector.state || "unknown";
  const current = collector.current || "";
  const percent = Number.isFinite(collector.progress_percent) ? collector.progress_percent : 0;
  els.collectorStatus.dataset.state = state;
  els.collectorState.textContent = collector.state_label || "상태 확인 중";
  els.collectorCurrent.textContent = current ? current.replace(/^\[(\d+)\/(\d+)\]\s*/, "$1/$2 · ") : "";
  els.collectorResult.textContent = collector.last_result || "아직 반영 로그가 없습니다.";
  els.collectorAge.textContent = collector.seconds_since_log == null
    ? ""
    : `마지막 로그 ${relativeSeconds(collector.seconds_since_log)}`;
  els.collectorProgress.style.width = `${Math.max(0, Math.min(100, percent))}%`;
}

function isCollectorRunning(collector) {
  return ["running", "stale"].includes(collector?.state);
}

function collectorProgressLabel(collector) {
  const current = collector?.progress_current;
  const total = collector?.progress_total;
  if (Number.isFinite(current) && Number.isFinite(total) && total > 0) {
    return `${current.toLocaleString("ko-KR")} / ${total.toLocaleString("ko-KR")}`;
  }
  return "";
}

function relativeSeconds(seconds) {
  if (seconds < 60) return `${seconds}초 전`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}분 전`;
  return `${Math.floor(minutes / 60)}시간 전`;
}

function metric(label, value) {
  return `<div class="metric"><b>${escapeHtml(value)}</b><span>${escapeHtml(label)}</span></div>`;
}

function updateStatusOptions(rows) {
  const current = els.status.value;
  rows.forEach((row) => row.name && state.statuses.add(row.name));
  const options = ['<option value="">전체 상태</option>']
    .concat([...state.statuses].sort().map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`));
  els.status.innerHTML = options.join("");
  els.status.value = current;
}

function updateSourceOptions(rows) {
  const current = els.source.value;
  rows.forEach((row) => row.name && state.sources.add(row.name));
  const options = ['<option value="">전체 구분</option>']
    .concat([...state.sources].sort().map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`));
  els.source.innerHTML = options.join("");
  els.source.value = current;
}

async function loadItems({ append = false } = {}) {
  if (state.loadingItems) return;
  state.loadingItems = true;
  updateListStatus();

  if (!append) {
    state.items = [];
    state.offset = 0;
    state.total = 0;
    state.exhausted = false;
    renderRows();
  }

  const params = new URLSearchParams({
    limit: String(state.limit),
    offset: String(state.offset),
  });
  if (state.query) params.set("query", state.query);
  if (state.region) params.set("region", state.region);
  if (state.source) params.set("source", state.source);
  if (state.status) params.set("status", state.status);
  try {
    const payload = await fetchJson(`/api/items?${params.toString()}`);
    const nextItems = payload.items || [];
    state.total = payload.total || 0;
    state.items = append ? state.items.concat(nextItems) : nextItems;
    state.offset = state.items.length;
    state.exhausted = state.items.length >= state.total || nextItems.length === 0;
    renderRows();
  } finally {
    state.loadingItems = false;
    updateListStatus();
  }
}

function renderRows() {
  if (!state.items.length) {
    els.rows.innerHTML = '<tr><td colspan="8">표시할 물건이 없습니다.</td></tr>';
    return;
  }
  els.rows.innerHTML = state.items.map((item) => `
    <tr data-key="${escapeHtml(item.item_key)}" class="${item.item_key === state.selectedKey ? "active" : ""}">
      <td>${fmt(item.source || "진행")}</td>
      <td>${fmt(item.case_no)}</td>
      <td>${fmt(item.item_no)}</td>
      <td class="address">${fmt(item.address)}</td>
      <td>${fmt(item.category)}</td>
      <td>${fmt(item.minimum_bid)}</td>
      <td>${fmt(item.sale_date)}</td>
      <td><span class="status" data-kind="${escapeHtml(item.status)}">${fmt(item.status)}</span></td>
    </tr>
  `).join("");
}

function updateListStatus() {
  if (state.loadingItems) {
    els.listStatus.textContent = "목록을 불러오는 중";
    return;
  }
  if (!state.total) {
    els.listStatus.textContent = "표시할 물건이 없습니다.";
    return;
  }
  els.listStatus.textContent = state.exhausted
    ? `${isCollectorRunning(state.collector) ? "현재 저장된 " : "전체 "}${state.total.toLocaleString("ko-KR")}개 표시 완료`
    : `${state.items.length.toLocaleString("ko-KR")} / ${state.total.toLocaleString("ko-KR")}개 표시 중`;
}

function resetAndLoadItems() {
  loadItems({ append: false }).catch(showError);
}

async function selectItem(itemKey) {
  state.selectedKey = itemKey;
  renderRows();
  const item = await fetchJson(`/api/items/${encodeURIComponent(itemKey)}`);
  els.detailEmpty.hidden = true;
  els.detailContent.hidden = false;
  els.detailTitle.textContent = item.case_no || item.item_key;
  els.detailSub.textContent = [item.court, item.address].filter(Boolean).join(" · ");

  const detailUrl = safeExternalUrl(item.detail_url);
  if (detailUrl) {
    els.detailLink.href = detailUrl;
    els.detailLink.hidden = false;
  } else {
    els.detailLink.removeAttribute("href");
    els.detailLink.hidden = true;
  }

  const fields = {
    수집구분: item.source || "진행",
    사건번호: item.case_no,
    물건번호: item.item_no,
    법원: item.court,
    소재지: item.address,
    용도: item.category,
    감정평가액: item.appraisal,
    최저매각가격: item.minimum_bid,
    매각기일: item.sale_date,
    진행상태: item.status,
    최초수집: fmtTime(item.first_seen_at),
    최근확인: fmtTime(item.last_seen_at),
    ...item.detail,
    ...item.raw,
  };
  els.detailFields.innerHTML = Object.entries(fields)
    .filter(([, value]) => value)
    .map(([key, value]) => `<dt>${escapeHtml(key)}</dt><dd>${fmt(value)}</dd>`)
    .join("");

  els.eventList.innerHTML = (item.events || []).map((event) => (
    `<li>${fmtTime(event.created_at)} · ${escapeHtml(event.event_type)}</li>`
  )).join("") || "<li>변경 이력이 없습니다.</li>";
}

async function fetchJson(url, options) {
  const headers = new Headers(options?.headers || {});
  const apiKey = window.localStorage?.getItem("AUCTION_API_KEY") || "";
  if (apiKey && !headers.has("X-API-Key")) headers.set("X-API-Key", apiKey);
  const res = await fetch(url, { ...options, headers });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

let searchTimer = 0;
els.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = els.search.value.trim();
    resetAndLoadItems();
  }, 250);
});

els.status.addEventListener("change", () => {
  state.status = els.status.value;
  resetAndLoadItems();
});

els.source.addEventListener("change", () => {
  state.source = els.source.value;
  resetAndLoadItems();
});

els.region.addEventListener("change", () => {
  state.region = els.region.value;
  resetAndLoadItems();
});

els.tableWrap.addEventListener("scroll", () => {
  const remaining = els.tableWrap.scrollHeight - els.tableWrap.scrollTop - els.tableWrap.clientHeight;
  if (remaining < 240 && !state.exhausted) {
    loadItems({ append: true }).catch(showError);
  }
});

els.rows.addEventListener("click", (event) => {
  const row = event.target.closest("tr[data-key]");
  if (row) selectItem(row.dataset.key).catch(showError);
});

els.collectorStart.addEventListener("click", async () => {
  await fetchJson("/api/collector/start", { method: "POST" });
  await loadStats();
});

els.collectorStop.addEventListener("click", async () => {
  await fetchJson("/api/collector/stop", { method: "POST" });
  await loadStats();
  await loadItems({ append: false });
});

function showError(error) {
  els.syncLine.textContent = `오류: ${error.message}`;
}

async function tick() {
  try {
    await loadStats();
  } catch (error) {
    showError(error);
  }
}

async function boot() {
  await loadStats();
  await loadItems({ append: false });
}

boot().catch(showError);
setInterval(tick, 5000);
