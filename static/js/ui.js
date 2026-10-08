
import { $, esc } from "./api.js";

const PHONE_ON = `<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="#2563eb" stroke-width="1.8" stroke-linecap="round"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><line x1="10.5" y1="19" x2="13.5" y2="19"/></svg>`;
const PHONE_OFF = `<svg viewBox="0 0 24 24" width="34" height="34" fill="none" stroke="#94a3b8" stroke-width="1.8" stroke-linecap="round"><rect x="7" y="2.5" width="10" height="19" rx="2.5"/><line x1="4" y1="4" x2="20" y2="20"/></svg>`;

export function tplDevice(d) {
  if (!d || !d.connected) {
    return `<div class="d-flex align-items-center gap-3">${PHONE_OFF}
      <div><div class="fw-bold">未发现设备</div>
      <small class="text-muted">数据线直连 · 解锁 · 点信任 · 只连一台</small></div></div>
      <div class="mt-2"><span class="badge bg-danger">未连接</span></div>`;
  }
  const model = (d.marketing_name && d.marketing_name !== "?")
    ? d.marketing_name
    : ((d.product_type && d.product_type !== "?") ? d.product_type : "iPhone");
  const subModel = (d.product_type && d.product_type !== "?" && d.product_type !== model)
    ? d.product_type : "";
  const extra = [subModel, d.model_number, d.build].filter(x => x && x !== "?").join(" · ");
  const lockB = d.locked == null
    ? `<span class="badge bg-secondary">解锁未知</span>`
    : d.locked
      ? `<span class="badge bg-warning text-dark">已锁定</span>`
      : `<span class="badge bg-success">已解锁</span>`;
  const iosMajor = parseInt(String(d.ios || "").split(".")[0], 10) || 0;
  const devNA = iosMajor > 0 && iosMajor < 16;
  const devB = devNA
    ? `<span class="badge bg-secondary">iOS ${esc(d.ios)} 免开发者模式</span>`
    : d.devmode == null
      ? ""
      : d.devmode
        ? `<span class="badge bg-success">开发者模式开</span>`
        : `<span class="badge bg-danger">开发者模式关</span>`;
  const st = window._lastChecks || {};
  const dotCls = s => s === "pass" ? "bg-success" : s === "warn" ? "bg-warning" : "bg-danger";
  return `<div class="d-flex align-items-center gap-3">${PHONE_ON}
      <div class="min-w-0"><div class="fw-bold text-truncate">${esc(d.name || "iPhone")}</div>
      <small class="text-muted">${esc(model)}${extra ? " · " + esc(extra) : ""}${d.connection ? " · " + esc(d.connection) : ""}</small></div></div>
    <div class="d-flex gap-1 flex-wrap mt-2">
      <span class="badge bg-primary">${esc(model)}</span>
      <span class="badge bg-light text-dark border">iOS ${esc(d.ios || "?")}</span>${lockB}${devB}
    </div>
    <div class="row g-2 mt-2 text-center">
      <div class="col-4"><div class="border rounded py-2"><span class="dot ${dotCls(st.device)}"></span><div class="small fw-bold mt-1">连接</div><small class="text-muted">${esc(st.deviceDetail || d.connection || "正常")}</small></div></div>
      <div class="col-4"><div class="border rounded py-2"><span class="dot ${dotCls(st.unlock)}"></span><div class="small fw-bold mt-1">解锁</div><small class="text-muted">${d.locked ? "需解锁" : "已解锁"}</small></div></div>
      <div class="col-4"><div class="border rounded py-2"><span class="dot ${dotCls(st.devmode)}"></span><div class="small fw-bold mt-1">开发者</div><small class="text-muted">${devNA ? "无需" : d.devmode ? "已开启" : "未开启"}</small></div></div>
    </div>`;
}

export function tplChecks(list, showAll) {
  const pass = list.filter(c => c.status === "pass").length;
  $("healthChip").textContent = `${pass}/${list.length} 通过`;
  $("healthBar").style.width = Math.round(pass / Math.max(1, list.length) * 100) + "%";
  $("healthTitle").textContent = pass === list.length ? "全部就绪，可以出发" : "还有 " + (list.length - pass) + " 项要注意";
  const rows = (showAll ? list : list.filter(c => c.status !== "pass"));
  if (!rows.length) return `<div class="alert alert-success py-2 small mb-0">全部通过，可出发。</div>`;
  const dotBg = s => s === "pass" ? "bg-success" : s === "warn" ? "bg-warning" : "bg-danger";
  return rows.map(c => `<div class="list-group-item px-0">
    <div class="d-flex gap-2 align-items-start">
      <span class="dot mt-1 ${dotBg(c.status)}"></span>
      <div><strong class="small">${esc(c.name)}</strong> <small class="text-muted">${esc(c.detail || "")}</small>
      ${c.hint ? `<div class="alert alert-warning py-1 px-2 small mb-0 mt-1">-> ${esc(c.hint)}</div>` : ""}
      ${c.id === "deps" && c.status !== "pass" ? `<div class="mt-1"><button class="btn btn-primary btn-sm" onclick="installDeps()">一键安装依赖</button></div>` : ""}</div>
    </div></div>`).join("")
    + (showAll ? "" : `<div class="small text-muted mt-1">另有 ${pass} 项已通过，已收起 · <a href="#" onclick="toggleShowAll(event)">展开</a></div>`);
}

export function tplRoutes(d) {
  $("routes").innerHTML = d.routes.map(r =>
    `<button class="btn btn-sm ${r.active ? "btn-dark" : "btn-outline-secondary"}" onclick="selectRoute('${esc(r.file)}')">${esc(r.file)}</button>`
  ).join("") || `<span class="badge bg-light text-dark border">暂无路线文件</span>`;
  const cur = d.routes.find(r => r.active);
  if (cur) $("routeTitle").textContent = "路线 · " + cur.file;
}

export function tplPreview(d) {
  if (d.error) {
    $("previewStats").innerHTML = `<div class="col-12"><div class="alert alert-danger py-2 small mb-0">试算失败：${esc(d.error)}</div></div>`;
    return;
  }
  const s = d.spacing;
  const fmtT = v => { v = Math.round(v); const m = Math.floor(v / 60); return m + ":" + String(v % 60).padStart(2, "0"); };
  const cell = (t, v, sub) => `<div class="col-6 col-md-3"><div class="border rounded py-2 px-1 h-100"><small class="text-muted d-block">${t}</small><strong>${v}</strong><small class="text-muted d-block">${sub}</small></div></div>`;
  $("previewStats").innerHTML =
    cell("配速 / 速度", esc(d.pace), d.speed + " m/s") +
    cell("环长 / 点数", d.loop_m + " m", `${d.points} 点 · 点距 ${s.mean}±${s.std}m`) +
    cell("单圈用时", fmtT(d.lap_s), "约 " + d.lap_points + " 个定位点") +
    cell("坐标系", "BD-09 → WGS84", "已转换供地图显示");
}
