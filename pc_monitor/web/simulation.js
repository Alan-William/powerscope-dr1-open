"use strict";
const $ = id => document.getElementById(id);
const colors = ["#c78135", "#167f8a", "#6960a1"];
let state = null, token = "", busy = false, initialized = false, lastWindow = "", reachable = false;
const set = (id, text) => { $(id).textContent = text; };
const running = () => state?.status === "running";
const active = () => ["starting", "running", "stopping"].includes(state?.status);
const transportActive = () => ["starting", "running", "stopping"].includes(state?.transport?.status);

async function api(path, data) {
  const response = await fetch(path, data === undefined ? {cache: "no-store"} : {
    method: "POST", headers: {"Content-Type": "application/json", "X-PowerScope-Token": token},
    body: JSON.stringify(data)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `HTTP ${response.status}`);
  return result;
}
function notice(message = "") { set("notice", message); $("notice").hidden = !message; }
function controls() {
  const disabled = busy || !reachable || !token;
  $("start").disabled = $("auto").disabled = disabled || active() || !state?.available || transportActive();
  $("condition").disabled = active() || disabled;
  $("stop").disabled = disabled || !active() || state?.status === "stopping";
  $("inject").disabled = $("recover").disabled = !running() || disabled;
  for (const button of document.querySelectorAll(".device")) button.disabled = !running() || disabled;
  $("board-start").disabled = disabled || !running() || transportActive() || !$("port").value;
  $("board-stop").disabled = disabled || !transportActive() || state.transport.status === "stopping";
  $("port").disabled = $("ports").disabled = disabled || transportActive();
}
async function command(path, body) {
  if (busy) return;
  busy = true; notice(); controls();
  try { await api(path, body); }
  catch (error) { notice(error.message); }
  finally { busy = false; controls(); }
}
function buildBridge() {
  for (let phase = 0; phase < 3; phase++) {
    const leg = document.createElement("div"); leg.className = "leg";
    const name = document.createElement("span"); name.className = `leg-title phase-${"abc"[phase]}`;
    name.textContent = `${"ABC"[phase]} 相桥臂`; leg.append(name);
    for (let device = 1; device <= 4; device++) {
      if (device === 3) {
        const center = document.createElement("div"); center.className = "neutral";
        center.textContent = `${"abc"[phase]} 相输出 →`; leg.append(center);
      }
      const button = document.createElement("button"), label = phase * 4 + device;
      button.className = "device"; button.id = `device-${label}`; button.disabled = true;
      button.textContent = `T${"abc"[phase]}${device}`;
      button.title = `注入 T${"abc"[phase]}${device} 开关开路`;
      button.onclick = () => command("/api/simulation/select", {label}); leg.append(button);
    }
    const sensor = document.createElement("div"); sensor.className = "sensor"; sensor.id = `sensor-${phase}`;
    sensor.textContent = `I${"abc"[phase]} 电流传感器`; $("current-sensors").append(sensor);
    $("bridge-legs").append(leg);
  }
}
function canvas(id) {
  const element = $(id), ratio = window.devicePixelRatio || 1;
  const width = element.clientWidth, height = element.clientHeight;
  element.width = width * ratio; element.height = height * ratio;
  const context = element.getContext("2d"); context.scale(ratio, ratio);
  context.font = '9px "Segoe UI", "Microsoft YaHei", sans-serif';
  return {g: context, w: width, h: height, left: 43, right: 12, top: 13, bottom: 24};
}
function axes(c, lower, upper, start, end, suffix) {
  const {g,w,h,left,right,top,bottom} = c;
  for (let i = 0; i <= 4; i++) {
    const value = lower + (upper - lower) * i / 4, y = h - bottom - (h - top - bottom) * i / 4;
    g.beginPath(); g.strokeStyle = i === 2 && lower < 0 ? "#d1dce5" : "#edf1f5"; g.lineWidth = 1;
    g.moveTo(left, y); g.lineTo(w - right, y); g.stroke();
    g.fillStyle = "#8fa1af"; g.textAlign = "right";
    g.fillText(Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1), left - 8, y + 3);
  }
  for (let i = 0; i <= 5; i++) {
    const x = left + (w - left - right) * i / 5;
    g.strokeStyle = "#f1f4f7"; g.beginPath(); g.moveTo(x, top); g.lineTo(x, h - bottom); g.stroke();
    g.fillStyle = "#9aaab7"; g.textAlign = i === 0 ? "left" : i === 5 ? "right" : "center";
    g.fillText(`${(start + (end - start) * i / 5).toFixed(suffix === "s" ? 1 : 0)} ${suffix}`, x, h - 6);
  }
  g.textAlign = "left";
}
function traces(c, waves, lower, upper, dashed = false) {
  const {g,w,h,left,right,top,bottom} = c;
  if (!waves) return;
  g.save(); g.beginPath(); g.rect(left, top, w-left-right, h-top-bottom); g.clip();
  waves.forEach((wave, phase) => {
    g.strokeStyle = colors[phase]; g.lineWidth = dashed ? 1 : 1.7;
    g.globalAlpha = dashed ? .5 : .95; g.setLineDash(dashed ? [4,3] : []); g.beginPath();
    wave.forEach((value, index) => {
      const x = left + index / wave.length * (w-left-right);
      const y = h-bottom - (value-lower)/(upper-lower)*(h-top-bottom);
      if (index) g.lineTo(x,y); else g.moveTo(x,y);
    }); g.stroke();
  }); g.restore();
}
function scope(id, waves, actual) {
  const c = canvas(id), peak = waves ? Math.max(...waves.flat().map(Math.abs), ...(actual || []).flat().map(Math.abs), .1) : 1;
  const limit = Math.ceil(peak * 1.12 * (peak < 10 ? 2 : .1)) / (peak < 10 ? 2 : .1);
  axes(c, -limit, limit, 0, 25, "ms");
  if (actual) traces(c, actual, -limit, limit, true);
  traces(c, waves, -limit, limit);
  if (!waves) {
    c.g.fillStyle = "#a2b1bc"; c.g.textAlign = "center";
    c.g.fillText("启动仿真后显示连续求解的电路波形", c.w/2+10, c.h/2-5);
  }
}
function trend() {
  const c = canvas("rms-trend"), history = state?.history || [];
  const end = Math.max(state?.automatic ? 21 : 12, state?.elapsed_s || 0), start = Math.max(0,end-30);
  const limit = Math.max(1, Math.ceil(Math.max(0, ...history.flatMap(row => row.rms_a)) * 1.2));
  axes(c, 0, limit, start, end, "s");
  const {g,w,h,left,right,top,bottom} = c;
  (state?.events || []).filter(event => event.time_s >= start).forEach(event => {
    const x = left + (event.time_s-start)/(end-start)*(w-left-right);
    g.setLineDash([3,4]); g.strokeStyle = event.label ? "#e4b68f" : "#c2d8d0";
    g.beginPath();g.moveTo(x,top);g.lineTo(x,h-bottom);g.stroke();g.setLineDash([]);
  });
  for (let phase=0; phase<3; phase++) {
    g.strokeStyle=colors[phase];g.lineWidth=1.6;g.beginPath();
    history.forEach((row,index) => {
      const x=left+(row.time_s-start)/(end-start)*(w-left-right), y=h-bottom-row.rms_a[phase]/limit*(h-top-bottom);
      if(index) g.lineTo(x,y); else g.moveTo(x,y);
    });g.stroke();
  }
}
function render(data) {
  state=data;
  if (!initialized) {
    $("condition").replaceChildren(...data.conditions.map(row => new Option(row.name,String(row.id))));
    $("condition").value=String(data.condition.id);
    $("fault").replaceChildren(...data.labels.slice(1).map((label,index) => new Option(label,String(index+1))));
    $("fault").value="2"; initialized=true;
  }
  const labels={idle:"就绪",starting:"电路启动中",running:data.automatic?"自动流程运行中":data.schedule_resyncs?"在线运行":"实时运行 · 1×",stopping:"正在停止",stopped:"运行记录已保存",error:"运行已停止"};
  set("run-status",labels[data.status] || data.status); $("run-status").className=`status ${data.status}`;
  if(data.error || data.runtime_error) notice(data.error || data.runtime_error);
  set("sim-time",data.elapsed_s.toFixed(3));
  const batch=data.latest;
  if(batch) {
    batch.rms_a.forEach((v,k)=>set(`rms-${"abc"[k]}`,v.toFixed(2)));
    set("power",batch.power_w.toFixed(1));set("speed",batch.speed.toFixed(1));
    set("compute",`${batch.compute_ms.toFixed(2)} ms 计算 / 25 ms 电流窗口`);
    set("pacing",`仿真 / 实际 ${batch.pace_ratio.toFixed(3)}× · 偏差 ${batch.lag_ms.toFixed(1)} ms`);
    set("window-info",`窗口 ${batch.window_id} · 3 × 500 点 · 25 ms`);
  } else {
    for(const phase of "abc") set(`rms-${phase}`,"—");
    set("power","—");set("speed","—");set("window-info","三相 × 500 点");
    set("compute","计算时间 / 25 ms 电流窗口");set("pacing","按实际时间推进 · 1×");
  }
  const signature=`${data.session}/${batch?.window_id || 0}/${$("show-actual").checked}`;
  if(signature!==lastWindow) {
    lastWindow=signature;
    scope("current-wave",batch?.measured,$("show-actual").checked?batch?.actual:null);
    scope("voltage-wave",batch?.voltage);trend();
  }
  for(let label=1;label<=12;label++) $( `device-${label}`).classList.toggle("fault",data.label===label);
  for(let phase=0;phase<3;phase++) $( `sensor-${phase}`).classList.toggle("fault",[13+phase,16+phase,19+phase].includes(data.label));
  $("input-state").classList.toggle("fault",data.label!==0);
  $("input-state").lastElementChild.textContent=`输入状态 · ${data.labels[data.label]}`;
  set("circuit-vdc",`${data.condition.vdc} V DC`);
  set("load-label",`R ${data.condition.resistance} Ω${data.condition.inductance?` / ${data.condition.inductance*1000} mH`:""}`);
  $("events").replaceChildren(...data.events.slice(-6).map(event=> {
    const node=document.createElement("span");node.className=event.label?"fault":"normal";
    node.textContent=`${event.time_s.toFixed(2)} s · ${event.kind} · ${event.name}`;return node;
  }));
  if(!data.events.length) set("events","启动后记录运行、故障注入与恢复时刻。");
  $("export").setAttribute("aria-disabled",String(!data.export));
  if(data.export) $("export").href=data.export; else $("export").removeAttribute("href");
  const transport=data.transport;
  const sameRun=transport?.source_kind==="live" && transport.latest_input?.simulator_session===data.session;
  const received=sameRun?transport.latest_match:null;
  set("wire-status",sameRun && transport.status==="running"?"真实 RS485 接收":"本机仿真");
  set("diagnosis",received?received.label_name:"连接板端后接收");
  $("diagnosis").parentElement.classList.toggle("alarm",Boolean(received?.label));
  set("wire-detail",received?`帧 ${received.sequence} · NPU ${(received.inference_us/1000).toFixed(3)} ms · 输入至串口 ${received.end_to_end_ms.toFixed(1)} ms`:"sfaug-r1 · 纯 NPU 分类");
  set("correspondence",sameRun?`发送 ${transport.sent} 窗 / RS485 对应 ${transport.matched} 窗`:"输入与回传逐窗对应");
  if(transport?.source_kind==="live" && transport.status==="error") notice(transport.error);
  set("footer-state",data.session?`运行 ${data.session.slice(0,12)} · CSV 自动保存`:"PowerScope / 国产 FPGA 故障诊断平台");
  controls();
}
async function refreshPorts() {
  try {
    const previous=$("port").value, result=await api("/api/ports");
    $("port").replaceChildren(new Option("选择 USB-RS485 串口",""),...result.ports.map(p=>new Option(`${p.device} · ${p.description}`,p.device)));
    if(result.ports.some(p=>p.device===previous)) $("port").value=previous;
  } catch(error) {notice(error.message);} controls();
}
async function poll() {
  try {const data=await api("/api/simulation/state");token=data.token;reachable=true;render(data);}
  catch(error) {reachable=false;set("run-status","本机服务连接中断");$("run-status").className="status error";controls();}
  finally {setTimeout(poll,83);}
}
$("start").onclick=()=>command("/api/simulation/start",{condition:Number($("condition").value),automatic:false});
$("auto").onclick=()=>command("/api/simulation/start",{condition:Number($("condition").value),automatic:true});
$("stop").onclick=()=>command("/api/simulation/stop",{});
$("inject").onclick=()=>command("/api/simulation/select",{label:Number($("fault").value)});
$("recover").onclick=()=>command("/api/simulation/select",{label:0});
$("ports").onclick=refreshPorts;$("port").onchange=controls;
$("board-start").onclick=()=>command("/api/demo/start",{source:"live",port:$("port").value});
$("board-stop").onclick=()=>command("/api/demo/stop",{});
$("show-actual").onchange=()=>{if(state)render(state);};
window.addEventListener("resize",()=>{lastWindow="";if(state)render(state);else {scope("current-wave");scope("voltage-wave");trend();}});
buildBridge();scope("current-wave");scope("voltage-wave");trend();controls();refreshPorts();poll();
