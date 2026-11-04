"use strict";
let demoState = null, demoBusy = false, lastInput = "", demoLabels = false;
const demoActive = () => ["starting", "running", "stopping"].includes(demoState?.status);
function drawInput(waves) {
  const c = $("input-wave"), ratio = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  c.width = w * ratio; c.height = h * ratio;
  const g = c.getContext("2d"); g.scale(ratio, ratio);
  const l = 34, r = 14, t = 14, b = 25;
  g.font = "10px Segoe UI";
  for (const v of [-1, -.5, 0, .5, 1]) {
    const y = t + (1 - v) / 2 * (h - t - b);
    g.strokeStyle = v === 0 ? "#a8c0c8" : "#dfebed"; g.lineWidth = 1;
    g.beginPath(); g.moveTo(l, y); g.lineTo(w - r, y); g.stroke();
    g.fillStyle = "#80959f"; g.fillText(v.toFixed(1), 3, y + 3);
  }
  for (let i = 0; i <= 5; i++) {
    const x = l + (w - l - r) * i / 5;
    g.fillText(`${i * 5} ms`, x - 12, h - 5);
  }
  if (!waves) { g.fillStyle = "#80959f"; g.fillText("开始演示后显示实际发送窗口", Math.max(38, w / 2 - 75), h / 2 - 10); return; }
  ["#d28336", "#138b79", "#6483b6"].forEach((color, k) => {
    g.strokeStyle = color; g.lineWidth = 1.7; g.beginPath();
    waves[k].forEach((v, i) => {
      const x = l + i / waves[k].length * (w - l - r), y = t + (1 - v) / 2 * (h - t - b);
      if (i) g.lineTo(x, y); else g.moveTo(x, y);
    }); g.stroke();
  });
}
function demoControls() {
  const active = demoActive(), ready = demoState?.status === "running";
  $("demo-start").disabled = $("demo-auto").disabled = active || demoBusy || !demoState?.available || !token || ended;
  $("demo-inject").disabled = $("demo-recover").disabled = !ready || demoBusy || !token || ended;
  $("demo-stop").disabled = !active || demoState?.status === "stopping" || demoBusy || !token || ended;
  if (active) $("disconnect").disabled = true;
}
function renderDemo(state) {
  demoState = state;
  if (!state) { set("demo-status", "请重新启动本机程序以启用演示"); demoControls(); return; }
  if (!demoLabels) {
    $("demo-fault").replaceChildren(...state.labels.slice(1).map((name, i) => new Option(name, String(i + 1))));
    $("demo-fault").value = "2"; demoLabels = true;
  }
  const messages = {idle: "网线与 USB-RS485 接好后，点击开始；程序会自动连接所选串口。", starting: "正在连接板端并加载 NPU…", running: state.automatic ? `自动演示中 · ${state.elapsed_s.toFixed(1)} / 21 秒` : "正在发送电流窗口，可随时注入故障或恢复正常。", stopping: "正在停止输入并保存结果…", stopped: "演示已结束，记录已保存。可再次开始。", error: state.error};
  set("demo-status", state.library_error || messages[state.status] || state.status);
  $("demo-status").classList.toggle("error", state.status === "error" || !state.available);
  const source = state.latest_input;
  const signature = source ? `${state.session}/${source.input_id}` : "";
  if (signature !== lastInput) {
    lastInput = signature; drawInput(source?.waveform);
    set("input-label", source?.source_name || "正常运行");
    set("input-count", source ? `窗口 #${source.input_id} · 6000 B` : "等待发送");
    const liveSource = ["npc-switching-r1", "npc-simulink-live-r1"].includes(source?.source_kind);
    set("input-meta", source ? `${liveSource ? `仿真 ${source.sim_time_s.toFixed(3)} s` : `源索引 ${source.dataset_index}`} · 25 ms / 窗 · SHA ${source.sha256.slice(0, 10)}` : "20 kHz · 每窗 25 ms · 归一化电流");
  }
  set("demo-match", `发送 ${state.sent} 窗 / RS485 对应 ${state.matched} 窗`);
  const match = state.latest_match;
  set("demo-latency", match ? `发送至串口响应 ${match.end_to_end_ms.toFixed(1)} ms · 帧 ${match.sequence}` : "发送至串口响应 —");
  $("demo-export").setAttribute("aria-disabled", String(!state.export));
  if (state.export) $("demo-export").href = state.export; else $("demo-export").removeAttribute("href");
  $("demo-steps").replaceChildren(...state.steps.map((step, i) => {
    const item = document.createElement("span"), active = state.automatic && state.status === "running" && state.elapsed_s >= step.at_s && (i === state.steps.length - 1 || state.elapsed_s < state.steps[i + 1].at_s);
    item.className = active ? "current" : "";
    item.textContent = `${String(i + 1).padStart(2, "0")} ${step.name.replace(" 开关开路", " 开路").replace(" 相传感器", " 相")}`;
    return item;
  }));
  $("demo-events").replaceChildren(...state.events.slice(-5).reverse().map(event => {
    const item = document.createElement("span"); item.className = event.label ? "fault-event" : "normal-event";
    item.textContent = `${event.elapsed_s.toFixed(1)} s ${event.kind} · ${event.label_name}`; return item;
  }));
  if (!state.events.length) set("demo-events", "开始后记录正常、故障与恢复的响应。");
  demoControls();
}
async function demoCommand(path, data) {
  if (demoBusy) return;
  demoBusy = true; notice(); demoControls();
  try { await api(path, data); }
  catch (error) { notice(error.message); }
  finally { demoBusy = false; demoControls(); }
}
window.addEventListener("powerscope-state", event => renderDemo(event.detail.demo));
window.addEventListener("powerscope-unavailable", () => {
  demoControls();
  $("demo-export").removeAttribute("href"); $("demo-export").setAttribute("aria-disabled", "true");
  set("demo-status", ended ? "本机程序已退出。双击快捷方式可重新启动。" : "本机服务连接中断，请重新打开启动器检查状态。");
});
window.addEventListener("resize", () => drawInput(demoState?.latest_input?.waveform));
$("demo-start").onclick = () => demoCommand("/api/demo/start", {port: $("port").value, automatic: false});
$("demo-auto").onclick = () => demoCommand("/api/demo/start", {port: $("port").value, automatic: true});
$("demo-inject").onclick = () => demoCommand("/api/demo/select", {label: Number($("demo-fault").value)});
$("demo-recover").onclick = () => demoCommand("/api/demo/select", {label: 0});
$("demo-stop").onclick = () => demoCommand("/api/demo/stop", {});
drawInput(null); demoControls();
