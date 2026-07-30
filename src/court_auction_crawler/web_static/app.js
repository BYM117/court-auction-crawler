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

function fmtMoney(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n === 0) return "-";
  if (n >= 100000000) {
    const eok = Math.floor(n / 100000000);
    const man = Math.round((n % 100000000) / 10000);
    return man > 0 ? `${eok}억 ${man.toLocaleString("ko-KR")}만` : `${eok}억`;
  }
  if (n >= 10000) return `${Math.round(n / 10000).toLocaleString("ko-KR")}만`;
  return n.toLocaleString("ko-KR");
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
  els.detailEmpty.hidden = true;
  els.detailContent.hidden = false;
  els.detailContent.innerHTML = '<div class="reportLoading">불러오는 중…</div>';
  try {
    const item = await fetchJson(`/api/v1/auctions/${encodeURIComponent(itemKey)}`);
    els.detailContent.innerHTML = renderReport(item);
    els.detailContent.scrollTop = 0;
  } catch (error) {
    els.detailContent.innerHTML = `<div class="reportLoading">상세를 불러오지 못했습니다: ${escapeHtml(error.message)}</div>`;
  }
}

function renderReport(item) {
  const auction = item.auction || {};
  const property = item.property || {};
  const priceBlock = item.price || {};
  const caseBlock = item.case || {};
  const detailUrl = safeExternalUrl(item.detail_url);
  const badge = auction.is_active === false
    ? '<span class="badge badge-off">종결</span>'
    : `<span class="badge badge-on">${escapeHtml(item.source || "진행")}</span>`;
  const statusText = item.status ? `<span class="badge badge-status">${escapeHtml(item.status)}</span>` : "";

  return [
    `<header class="reportHead">
      <div class="reportHeadTop">
        <div class="reportBadges">${badge}${statusText}</div>
        ${detailUrl ? `<a class="reportLink" href="${detailUrl}" target="_blank" rel="noreferrer">법원 원문 ↗</a>` : ""}
      </div>
      <h2>${fmt(caseBlock.display_case_no || item.case_no)}${item.item_no ? ` · 물건 ${escapeHtml(item.item_no)}` : ""}</h2>
      <p class="reportSub">${fmt([item.court, property.type_guess || item.category].filter(Boolean).join(" · "))}</p>
      <p class="reportAddr">${fmt(item.address)}</p>
    </header>`,
    renderMetrics(item, auction, priceBlock),
    renderGallery(item.assets),
    renderScreening(item.screening),
    renderOfficialPrice(priceBlock.official),
    renderScheduleTables(item.detail),
    renderDetailTables(item.detail),
    renderSections(item.detail && item.detail.sections),
    renderDocuments(item.documents),
    renderMap(item),
    renderEvents(item.events),
  ].join("");
}

function metricCell(label, value, sub) {
  return `<div class="metricCell"><span class="metricLabel">${escapeHtml(label)}</span>`
    + `<b>${value}</b>${sub ? `<em>${sub}</em>` : ""}</div>`;
}

function renderMetrics(item, auction, priceBlock) {
  const rate = priceBlock.minimum_bid_percent != null ? `${priceBlock.minimum_bid_percent}%` : "";
  const cells = [
    metricCell("감정가", fmtMoney(priceBlock.appraisal), priceBlock.appraisal ? "원" : ""),
    metricCell("최저가", fmtMoney(priceBlock.minimum_bid), rate ? `감정가 대비 ${rate}` : ""),
    metricCell("유찰", auction.fail_count ? `${auction.fail_count}회` : "0회", ""),
    metricCell("매각기일", fmt(auction.sale_date || item.sale_date), ""),
  ];
  return `<section class="reportMetrics">${cells.join("")}</section>`;
}

function renderGallery(assets) {
  const photos = (assets || []).filter((a) => a.kind === "photo" && a.url);
  if (!photos.length) return "";
  const thumbs = photos.map((p) => (
    `<a href="${escapeHtml(p.url)}" target="_blank" rel="noreferrer" class="galleryItem" title="${escapeHtml(p.label || "")}">`
    + `<img src="${escapeHtml(p.url)}" alt="${escapeHtml(p.label || "전경도")}" loading="lazy"></a>`
  )).join("");
  return `<section class="reportSection"><h3>사진 <span class="count">${photos.length}</span></h3>`
    + `<div class="gallery">${thumbs}</div></section>`;
}

function renderScreening(screening) {
  if (!screening) return "";
  const flags = (screening.flags || []).map((f) => `<span class="flag">${escapeHtml(f)}</span>`).join("");
  const risk = screening.risk_level || "-";
  const riskClass = risk === "높음" ? "risk-high" : risk === "보통" ? "risk-mid" : "risk-low";
  return `<section class="reportSection"><h3>선별 <span class="riskBadge ${riskClass}">위험도 ${escapeHtml(risk)}</span>`
    + `<span class="count">점수 ${escapeHtml(String(screening.score ?? "-"))}</span></h3>`
    + `<div class="flags">${flags}</div></section>`;
}

function renderOfficialPrice(official) {
  if (!official || !official.value) return "";
  return `<section class="reportSection"><h3>공시기준가</h3>`
    + `<div class="officialPrice"><b>${fmtMoney(official.value)}원</b>`
    + `<span>${escapeHtml(official.type || "")}${official.year ? ` · ${escapeHtml(official.year)}` : ""}</span></div></section>`;
}

function tableHtml(table) {
  const rows = (table.rows || [])
    .filter((r) => r && r.length)
    .filter((r) => !(r.length === 1 && /^(이전으로|다음으로|이전으로 다음으로)$/.test(String(r[0]).trim())));
  if (!rows.length) return "";
  const body = rows.map((row) => (
    `<tr>${row.map((cell) => `<td>${fmt(cell)}</td>`).join("")}</tr>`
  )).join("");
  const capText = (table.caption || "")
    .replace(/\s*displayed in the table.*/i, "")
    .split(/[(（]/)[0]
    .trim();
  const cap = capText ? `<caption>${escapeHtml(capText.slice(0, 40))}</caption>` : "";
  return `<div class="tableScroll"><table class="dataTable">${cap}<tbody>${body}</tbody></table></div>`;
}

function renderScheduleTables(detail) {
  const scheduleTables = (detail && detail.case && detail.case.schedule_tables) || [];
  const target = scheduleTables.find((t) => (t.caption || "").includes("기일")) || scheduleTables[0];
  const fromDetail = (detail && detail.tables || []).find((t) => (t.caption || "").includes("기일내역"));
  const table = fromDetail || target;
  if (!table) return "";
  return `<section class="reportSection"><h3>기일 내역</h3>${tableHtml(table)}</section>`;
}

function renderDetailTables(detail) {
  const tables = (detail && detail.tables) || [];
  const shown = tables.filter((t) => {
    const cap = t.caption || "";
    if (cap.includes("검색조건") || cap.includes("기일내역")) return false;
    return (t.rows || []).some((r) => r && r.length);
  });
  if (!shown.length) return "";
  const blocks = shown.map((t) => tableHtml(t)).join("");
  return `<section class="reportSection"><h3>물건·감정 상세</h3>${blocks}</section>`;
}

function renderSections(sections) {
  const items = (sections || []).filter((s) => s.text && s.text.length > 10 && !/HOME 경매물건/.test(s.text));
  if (!items.length) return "";
  const body = items.map((s) => (
    `<div class="sectionItem"><b>${escapeHtml(s.title || "")}</b><p>${escapeHtml(s.text.slice(0, 1200))}</p></div>`
  )).join("");
  return `<section class="reportSection"><h3>현황·감정 요항</h3>${body}</section>`;
}

function renderDocuments(documents) {
  const docs = documents || [];
  if (!docs.length) return "";
  const blocks = docs.map((d) => {
    const label = escapeHtml(d.document_type || d.title || "문서");
    const meta = d.metadata || {};
    const iframe = meta.iframe || {};
    const tables = [...(meta.tables || []), ...(iframe.tables || [])];
    const text = (meta.text || iframe.text || "").trim();
    const tableBlocks = tables.map(tableHtml).filter(Boolean).join("");
    const textBlock = text.length > 20 ? `<p class="docText">${escapeHtml(text.slice(0, 3000))}</p>` : "";
    const hasContent = tableBlocks || textBlock;
    const fileLink = d.url
      ? `<a class="docChip docChip-ok" href="${escapeHtml(d.url)}" target="_blank" rel="noreferrer">원본 파일 ↓</a>`
      : "";
    const srcLink = safeExternalUrl(d.source_url)
      ? `<a class="docChip" href="${escapeHtml(d.source_url)}" target="_blank" rel="noreferrer">법원에서 보기 ↗</a>`
      : "";
    const state = hasContent ? "" : `<span class="docState">${d.status === "pending" ? "수집 대기" : "내용 없음"}</span>`;
    const inner = `<div class="docBody">${tableBlocks}${textBlock}<div class="docChips">${fileLink}${srcLink}</div></div>`;
    return `<details class="docItem"${hasContent ? "" : ""}>`
      + `<summary><span class="docName">${label}</span>${state}</summary>${inner}</details>`;
  }).join("");
  return `<section class="reportSection"><h3>법원 문서 <span class="count">${docs.length}</span></h3>${blocks}</section>`;
}

function renderMap(item) {
  const map = item.map || {};
  if (!map.lat || !map.lng) return "";
  const q = encodeURIComponent(item.address || "");
  const kakao = `https://map.kakao.com/?q=${q}`;
  return `<section class="reportSection"><h3>위치</h3>`
    + `<div class="mapBox"><span>좌표 ${Number(map.lat).toFixed(5)}, ${Number(map.lng).toFixed(5)}`
    + `${map.pnu ? ` · PNU ${escapeHtml(map.pnu)}` : ""}</span>`
    + `<a class="reportLink" href="${kakao}" target="_blank" rel="noreferrer">지도에서 보기 ↗</a></div></section>`;
}

function renderEvents(events) {
  const list = (events || []).slice(0, 10);
  if (!list.length) return "";
  const body = list.map((e) => (
    `<li><span>${fmtTime(e.created_at)}</span> ${escapeHtml(e.event_type)}</li>`
  )).join("");
  return `<section class="reportSection"><h3>변경 이력</h3><ol class="eventList">${body}</ol></section>`;
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
