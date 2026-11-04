"use strict";
const $ = id => document.getElementById(id);
let token = "", session = null, cursor = 0, rows = [], selected = null, filter = "all";
let lastState = null, busy = false, ended = false, omitted = 0, noticeSource = "";
const registers = ["ABI_VERSION", "FLAGS", "LABEL", "SCORE", "SEQ_HI", "SEQ_LO", "NPU_US_HI", "NPU_US_LO", "ERROR_HI", "ERROR_LO"];
const number = value => Number(value || 0).toLocaleString("zh-CN");
const ms = value => Number(value).toFixed(2);
const set = (id, value) => { $(id).textContent = value; };

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {cache: "no-store"} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-PowerScope-Token": token}, body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function notice(message = "", source = "command") {
  noticeSource = message ? source : "";
  set("notice", message); $("notice").hidden = !message;
}
function localTime(iso) {
  if (!iso) return "—";
  const date = new Date(iso);
  return date.toLocaleTimeString("zh-CN", {hour12: false}) + "." + String(date.getMilliseconds()).padStart(3, "0");
}
async function refreshPorts() {
  const previous = $("port").value;
  const result = await api("/api/ports");
  $("port").replaceChildren();
  for (const port of result.ports) {
    const option = document.createElement("option");
    option.value = port.device; option.textContent = `${port.device} · ${port.description}`;
    $("port").append(option);
  }
  if (!result.ports.length) $("port").add(new Option("未检测到串口", ""));
  const preferred = lastState?.config?.port || previous || "COM22";
  if (result.ports.some(p => p.device === preferred)) $("port").value = preferred;
  else if (!previous) {
    const placeholder = new Option("请选择 USB-RS485 适配器", "", true, true);
    $("port").prepend(placeholder);
  }
  updateControls();
}
function updateControls() {
  const connected = lastState?.link === "connected";
  for (const id of ["port", "period", "refresh"]) $(id).disabled = connected || busy || ended;
  $("connect").disabled = connected || busy || ended || !token || !$("port").value;
  $("disconnect").disabled = !connected || busy || ended;
}
function summary(state) {
  const connected = state.link === "connected", record = state.latest;
  const stale = connected && record && record.result_valid && state.last_good_age_ms > Math.max(750, (state.config?.period_ms || 100) * 3);
  $("connection").className = `status-pill ${state.link}`;
  $("connection").lastElementChild.textContent = connected ? "串口已连接" : state.link === "error" ? "连接中断" : "串口未连接";
  set("session-hint", connected ? `${state.config.port} · 每 ${state.config.period_ms} ms 查询` : state.ended_at ? "会话已保存，串口已释放" : "选择适配器，开始接收");
  $("traffic-dot").classList.toggle("on", connected);
  set("diagnosis-caption", connected ? "串口接收的诊断结果" : record ? "上次接收的诊断结果" : "串口接收的诊断结果");
  set("diagnosis", stale ? "等待新应答" : record?.abi_ok ? record.label_name : record ? "等待有效报文" : "尚未接收");
  const active = record?.result_valid && !stale;
  $("diagnosis-card").classList.toggle("alarm", Boolean(active && record.label !== 0));
  set("result-kind", !connected && record ? "已断开" : stale ? "应答延迟" : active ? record.label === 0 ? "正常" : "故障" : record?.abi_ok ? record.application_error ? "应用状态" : "板端待机" : record ? "检查报文" : "等待报文");
  $("result-kind").className = "badge" + (connected && active ? record.label === 0 ? " good" : " bad" : "");
  set("result-code", record?.abi_ok ? record.label_code : "—");
  set("result-sequence", `序号 ${record?.abi_ok ? number(record.sequence) : "—"}`);
  set("result-score", `模型分数 ${active ? (record.confidence_q / 100).toFixed(2) + "%" : "—"}`);
  set("valid-count", number(state.counts.valid_packets)); set("query-count", number(state.counts.queries));
  set("update-count", `${number(state.counts.updates)} 次诊断结果更新`);
  set("roundtrip", record?.byte_count ? ms(record.roundtrip_ms) : "—");
  set("npu-time", `报文中的 NPU 耗时 ${active ? ms(record.inference_us / 1000) + " ms" : "—"}`);
  set("crc-count", number(state.counts.crc_errors)); set("timeout-count", number(state.counts.timeouts));
  $("crc-count").classList.toggle("bad-number", state.counts.crc_errors > 0);
  $("timeout-count").classList.toggle("bad-number", state.counts.timeouts > 0);
  set("protocol-count", `协议异常 ${number(state.counts.protocol_errors)} · 遗留字节 ${number(state.counts.stale_bytes)}`);
  const values = rows.slice(-40).filter(r => r.byte_count).map(r => r.roundtrip_ms);
  const low = Math.min(...values), span = Math.max(...values) - low || 1;
  $("rtt-line").setAttribute("points", values.map((v, i) => `${i * 100 / Math.max(1, values.length - 1)},${27 - (v - low) / span * 22}`).join(" "));
  for (const fmt of ["csv", "jsonl"]) {
    const link = $(`export-${fmt}`), href = state.downloads[fmt];
    link.setAttribute("aria-disabled", String(!href));
    if (href) link.href = href; else link.removeAttribute("href");
  }
  set("saved-session", state.session ? `会话 ${state.session}` : "尚未开始会话");
  if (state.error) notice(`串口或记录中断：${state.error}。检查 USB 连接及串口占用后，可重新开始接收。`, "state");
  else if (noticeSource === "state") notice();
  updateControls();
}

function packetNode(record, direction) {
  const node = document.createElement("button");
  const bad = direction === "STALE" || direction === "RX" && (!record.abi_ok || record.application_error);
  node.className = "packet-row" + (bad ? " bad" : "") + (selected?.row.id === record.id && selected.direction === direction ? " selected" : "");
  node.dataset.id = record.id; node.dataset.direction = direction;
  const raw = direction === "TX" ? record.request_hex : direction === "STALE" ? record.stale_hex : record.response_hex;
  const time = document.createElement("span"); time.className = "timestamp";
  time.textContent = localTime(direction === "RX" ? record.received_at : record.timestamp);
  const label = document.createElement("span"); label.className = `direction ${direction.toLowerCase()}`; label.textContent = direction;
  const bytes = document.createElement("span"); bytes.className = "packet-hex";
  bytes.textContent = raw || (record.status === "timeout" ? "超时 · 未收到应答字节" : "无字节");
  const crc = document.createElement("span"); crc.className = `packet-crc${bad ? " bad" : ""}${direction !== "RX" ? " muted" : ""}`;
  crc.textContent = direction !== "RX" ? `${raw.split(" ").length} B` : record.crc_ok === true ? "OK" : record.crc_ok === false ? "ERR" : "—";
  node.append(time, label, bytes, crc); return node;
}
function renderLog() {
  const viewport = $("packet-log"), previousScroll = viewport.scrollTop;
  const visible = rows.filter(r => filter !== "errors" || !["ok", "idle"].includes(r.status) || r.stale_hex || r.application_error).slice(-100);
  const fragment = document.createDocumentFragment();
  for (const row of visible) {
    if (row.stale_hex) fragment.append(packetNode(row, "STALE"));
    if (filter !== "rx") fragment.append(packetNode(row, "TX"));
    fragment.append(packetNode(row, "RX"));
  }
  if (!visible.length) {
    const empty = document.createElement("div"); empty.className = "empty-state";
    const title = document.createElement("h4"); title.textContent = filter === "errors" ? "当前没有异常报文" : cursor ? "等待下一条报文" : "等待第一帧报文";
    const note = document.createElement("p"); note.textContent = cursor ? "完整会话记录持续保存在电脑本地。" : "选择 USB-RS485 适配器，点击“开始接收”。";
    empty.append(title, note); fragment.append(empty);
  }
  viewport.replaceChildren(fragment);
  if ($("follow").checked) viewport.scrollTop = viewport.scrollHeight; else viewport.scrollTop = previousScroll;
  set("log-count", `显示最近 ${visible.length} 条事务${omitted ? " · 完整记录可导出" : " · 原始记录已保存"}`);
}
function resetInspector() {
  for (const id of ["detail-sequence", "detail-label", "detail-score", "detail-inference"]) set(id, "—");
  for (const [id, label] of [["flag-valid", "VALID"], ["flag-npu", "NPU"], ["flag-sim", "SIM"]]) {
    set(id, label + " —"); $(id).className = "";
  }
  renderRegisters(null);
}
function renderRegisters(values) {
  $("registers").replaceChildren(...registers.map((name, i) => {
    const row = document.createElement("tr");
    for (const text of [String(i).padStart(2, "0"), name, values ? values[i].toString(16).toUpperCase().padStart(4, "0") : "—", values ? String(values[i]) : "—"]) {
      const cell = document.createElement("td"); cell.textContent = text; row.append(cell);
    }
    return row;
  }));
}
function inspect() {
  resetInspector();
  if (!selected) {
    set("detail-subtitle", "选中一条 RX 应答"); set("detail-crc", "—"); $("detail-crc").className = "badge";
    set("frame-bytes", "收到应答后，在这里逐字段查看原始字节。");
    set("detail-note", "CRC 低字节在前，寄存器按大端序解析。"); $("detail-note").className = "detail-note";
    return;
  }
  const {row, direction} = selected, rx = direction === "RX";
  set("detail-subtitle", `${direction} · ${localTime(rx ? row.received_at : row.timestamp)} · #${row.id}`);
  set("detail-crc", rx ? row.crc_ok === true ? "CRC 正确" : row.crc_ok === false ? "CRC 错误" : "无完整帧" : direction === "TX" ? "读取请求" : "遗留字节");
  $("detail-crc").className = "badge" + (rx ? row.crc_ok === true ? " good" : " bad" : "");
  const raw = direction === "TX" ? row.request_hex : direction === "STALE" ? row.stale_hex : row.response_hex;
  const bytes = raw ? raw.split(" ") : [];
  $("frame-bytes").replaceChildren(...bytes.map((hex, i) => {
    const byte = document.createElement("span"); byte.textContent = hex;
    byte.className = "byte " + (i === 0 ? "addr" : i === 1 ? "func" : i >= bytes.length - 2 ? "crc" : i === 2 && rx ? "length" : "payload");
    byte.title = `字节 ${i} · 0x${hex}`; return byte;
  }));
  if (!bytes.length) set("frame-bytes", "未收到字节");
  $("detail-note").className = "detail-note" + (rx && !row.abi_ok ? " error" : "");
  set("detail-note", rx ? row.error || `CRC 0x${row.crc_received} · ${row.byte_count} 字节 · ${ms(row.roundtrip_ms)} ms` : direction === "TX" ? "从站 01 · 功能 03 · 起始 0000 · 数量 000A" : "查询发出前已存在的字节，单独保存。");
  if (!rx) return;
  if (row.registers) renderRegisters(row.registers);
  if (!row.abi_ok) return;
  set("detail-sequence", number(row.sequence));
  set("detail-label", row.label_name);
  set("detail-score", row.result_valid ? (row.confidence_q / 100).toFixed(2) + "%" : "—");
  set("detail-inference", row.result_valid ? ms(row.inference_us / 1000) + " ms" : "—");
  for (const [id, name, enabled] of [["flag-valid", "VALID", row.result_valid], ["flag-npu", "NPU", row.npu_executed], ["flag-sim", "SIM", row.simulated]]) {
    set(id, `${name} ${enabled ? 1 : 0}`); $(id).classList.toggle("on", Boolean(enabled));
  }
  if (row.application_error) set("detail-note", `应用错误 ${row.application_error} · ${row.application_error_name}`);
}
async function poll() {
  if (ended) return;
  try {
    const state = await api(`/api/state?after=${cursor}&session=${encodeURIComponent(session || "")}`);
    token = state.token; document.body.classList.remove("offline");
    if (state.session !== session) {
      session = state.session; rows = []; cursor = 0; selected = null; omitted = 0;
      renderLog(); inspect();
      if (state.config) { $("port").value = state.config.port; $("period").value = String(state.config.period_ms); }
    }
    lastState = state; omitted += state.omitted_rows;
    if (state.rows.length) {
      rows.push(...state.rows); rows = rows.slice(-600); cursor = state.rows.at(-1).id;
      if ($("follow").checked || !selected) selected = {row: state.rows.at(-1), direction: "RX"};
      renderLog(); inspect();
    }
    summary(state);
    window.dispatchEvent(new CustomEvent("powerscope-state", {detail: state}));
  } catch (_error) {
    document.body.classList.add("offline");
    $("connection").lastElementChild.textContent = "本机服务未连接";
    set("session-hint", "请重新双击启动快捷方式");
    set("diagnosis-caption", "历史接收记录 · 服务连接中断");
    token = ""; updateControls();
    window.dispatchEvent(new CustomEvent("powerscope-unavailable"));
  }
  if (!ended) setTimeout(poll, 250);
}
async function command(path, data) {
  if (busy) return;
  busy = true; notice(); updateControls();
  try { await api(path, data); }
  catch (error) { notice(error.message); }
  finally { busy = false; updateControls(); }
}
$("refresh").addEventListener("click", () => refreshPorts().catch(error => notice(error.message)));
$("port").addEventListener("change", updateControls);
$("connect").addEventListener("click", () => command("/api/connect", {port: $("port").value, period_ms: Number($("period").value)}));
$("disconnect").addEventListener("click", () => command("/api/disconnect", {}));
$("packet-log").addEventListener("click", event => {
  const item = event.target.closest(".packet-row"); if (!item) return;
  const row = rows.find(r => r.id === Number(item.dataset.id)); if (!row) return;
  selected = {row, direction: item.dataset.direction}; $("follow").checked = false; renderLog(); inspect();
});
$("follow").addEventListener("change", () => {
  if ($("follow").checked && rows.length) { selected = {row: rows.at(-1), direction: "RX"}; renderLog(); inspect(); }
});
document.querySelectorAll("[data-filter]").forEach(button => button.addEventListener("click", () => {
  filter = button.dataset.filter;
  document.querySelectorAll("[data-filter]").forEach(b => b.classList.toggle("chosen", b === button));
  renderLog();
}));
$("clear").addEventListener("click", () => { rows = []; selected = null; renderLog(); inspect(); });
$("shutdown").addEventListener("click", async () => {
  try {
    await api("/api/shutdown", {}); ended = true;
    $("connection").lastElementChild.textContent = "程序已退出";
    $("connection").className = "status-pill"; set("session-hint", "串口已释放，记录已保存");
    set("diagnosis-caption", "历史接收记录 · 程序已退出"); $("traffic-dot").classList.remove("on");
    for (const fmt of ["csv", "jsonl"]) { $(`export-${fmt}`).removeAttribute("href"); $(`export-${fmt}`).setAttribute("aria-disabled", "true"); }
    $("shutdown").disabled = true; updateControls();
    window.dispatchEvent(new CustomEvent("powerscope-unavailable"));
  } catch (error) { notice(error.message); }
});
setInterval(() => set("clock", new Date().toLocaleString("zh-CN", {hour12: false})), 1000);
renderRegisters(null);
refreshPorts().catch(error => notice(error.message));
poll();
