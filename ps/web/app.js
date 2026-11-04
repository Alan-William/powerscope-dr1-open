const $=id=>document.getElementById(id);
let state=null,online=false,datasetSignature='',seenRun=null,labelSignature='';
const eventNames={initial:'监测建立',fault_detected:'发现故障',fault_changed:'故障类别变化',recovered:'恢复正常'};
const statuses={idle:'待开始',starting:'正在加载模型',running:'正在回放',completed:'回放已完成',stopped:'已停止',finalizing:'正在保存结果',error:'运行中断'};
const ms=n=>(n/1000).toFixed(2);
function draw(waves){
 const c=$('waveform'),ratio=window.devicePixelRatio||1,w=c.clientWidth,h=c.clientHeight;c.width=w*ratio;c.height=h*ratio;
 const g=c.getContext('2d');g.scale(ratio,ratio);g.clearRect(0,0,w,h);const l=32,r=10,t=12,b=22;
 g.font='10px Segoe UI';g.lineWidth=1;
 for(const v of [-1,-.5,0,.5,1]){const y=t+(1-v)/2*(h-t-b);g.strokeStyle=v===0?'#b9c9bf':'#e8eee7';g.beginPath();g.moveTo(l,y);g.lineTo(w-r,y);g.stroke();g.fillStyle='#83968b';g.fillText(v.toFixed(1),0,y+3)}
 for(let i=0;i<=5;i++){const x=l+(w-l-r)*i/5;g.fillText(i*5+' ms',x-9,h-2)}
 if(!waves){g.fillStyle='#83968b';g.fillText('等待回放波形',Math.max(35,w/2-40),h/2-10);return}
 ['#d77b36','#197f68','#6988b2'].forEach((color,k)=>{g.strokeStyle=color;g.lineWidth=1.6;g.beginPath();waves[k].forEach((v,i)=>{const x=l+i/waves[k].length*(w-l-r),y=t+(1-v)/2*(h-t-b);i?g.lineTo(x,y):g.moveTo(x,y)});g.stroke()});
}
function render(s){
 state=s;const active=['running','starting','finalizing'].includes(s.status),last=s.latest;
 const signature=JSON.stringify(s.datasets),picker=$('dataset');
 if(signature!==datasetSignature){const selected=picker.value;picker.replaceChildren(...Object.entries(s.datasets).map(([value,d])=>Object.assign(document.createElement('option'),{value,textContent:d.title})));if(s.datasets[selected])picker.value=selected;datasetSignature=signature}
 if((active||(s.run_id&&seenRun!==s.run_id))&&s.datasets[s.dataset]){picker.value=s.dataset;const period=$('period');if(![...period.options].some(o=>o.value===String(s.period_ms)))period.add(new Option(`${s.period_ms} ms · 当前目标`,String(s.period_ms)));period.value=String(s.period_ms);$('repeat').value=s.scenario?.enabled?1:s.total/s.datasets[s.dataset].count;$('mode').value=s.scenario?.enabled?'monitor':'validation'}seenRun=s.run_id;
 $('connection').textContent='板端已连接';$('status').textContent=statuses[s.status]||s.status;$('status-dot').className=active?'running':'';
 $('backend').textContent=s.worker_ready?'Alnpu 已加载':'NPU 待加载';
 $('model-name').textContent='EDGE DIAGNOSTICS / '+s.model;
 $('progress').textContent=s.run_id?(s.scenario?.enabled?`已监测 ${s.processed} 窗 · 事件 ${s.scenario.event_count} 条`:`${s.processed} / ${s.total} 窗 · ${s.late_frames} 帧超过设定间隔`):'启动监测，在运行中注入故障';
 if(active&&s.scenario?.enabled)$('status').textContent='持续监测中';
 $('processed').textContent=s.processed;$('count').textContent=` / ${s.total} 窗`;
 $('accuracy').textContent=s.external_input?.enabled?'网口波形输入 · 板端不接收参考类别':s.processed?(s.sensor_rules?.enabled?`纯 NPU ${(100*(s.npu_correct??s.correct)/s.processed).toFixed(2)}% · 最终 ${(100*s.correct/s.processed).toFixed(2)}%`:`纯 NPU 准确率 ${(100*s.correct/s.processed).toFixed(2)}%`):'尚无参考标签比对';
 $('rule-summary').textContent=s.sensor_rules?.enabled?`规则命中 ${s.rule_matches||0} 窗 · 修正 ${s.rule_changes||0} 窗 · 改对 ${s.rule_improvements||0} / 改错 ${s.rule_regressions||0}`:'PS 规则未启用';
 $('rule-mode').textContent=s.sensor_rules?.enabled?'PS 规则：启用 · NPU 统计输入：关闭':'PS 规则：关闭 · NPU 统计输入：关闭';
 $('start').disabled=active;$('stop').disabled=!['starting','running'].includes(s.status);['dataset','period','repeat','mode'].forEach(id=>$(id).disabled=active);
 const labels=JSON.stringify(s.labels);if(labels!==labelSignature){$('fault-label').replaceChildren(...s.labels.slice(1).map((name,i)=>new Option(name,String(i+1))));labelSignature=labels}
 const scenario=s.scenario||{enabled:false},monitor=$('mode').value==='monitor';
 $('scenario-panel').hidden=!monitor;$('repeat-control').hidden=monitor;$('dataset-control').hidden=monitor;
 $('start').replaceChildren(document.createTextNode(monitor?'开始监测':'开始回放'));
 $('inject').disabled=$('recover').disabled=!(scenario.enabled&&s.status==='running');
 if(scenario.enabled){
  $('source-state').textContent='场景输入：'+s.labels[scenario.requested_label];
  const prediction=scenario.predicted_label,request=scenario.requests.at(-1),fault=prediction!==null&&prediction!==0;
  $('alarm').className='alarm'+(fault?' fault':'');
  $('alarm-title').textContent=prediction===null?'等待首窗诊断':(fault?'故障告警':'正常监测');
  $('alarm-label').textContent=prediction===null?'NPU 正在分析':s.labels[prediction];
  $('response-time').textContent=request?.first_match_ms!==null?`指令至结果 ${request.command_to_result_ms.toFixed(2)} ms · 首窗进入至结果 ${request.input_to_result_ms.toFixed(2)} ms`:'新运行状态已切换，等待 NPU 结果';
  $('events').replaceChildren(...scenario.events.slice(-6).reverse().map(e=>{const p=document.createElement('p');p.textContent=`${(e.published_ms/1000).toFixed(3)} s  ${eventNames[e.kind]} · ${s.labels[e.label]} · 序号 ${e.sequence}`;return p}));
 }
 $('error').hidden=!s.error;$('error').textContent=s.error||'';
 $('prediction').textContent=last?last.label_name:'—';$('confidence').textContent=last?((last.npu_score??last.score)*100).toFixed(1)+'%':'—';
 $('npu-original').textContent=last?`NPU 原判：${last.npu_label_name||last.label_name}`:'NPU 原判：—';
 $('diagnosis-source').textContent=last?(last.diagnosis_source==='ps_rule'?(last.rule.changed?'来源：PS 规则修正':'来源：PS 规则与 NPU 一致'):'来源：NPU'):'来源：—';
 $('result-state').textContent=last?(active?'最新一帧':'已保存末帧'):'尚无结果';
 $('inference').replaceChildren(document.createTextNode(last?ms(last.inference_us):'—'),Object.assign(document.createElement('small'),{textContent:' ms'}));
 $('application').replaceChildren(document.createTextNode(last?ms(last.ps_application_us):'—'),Object.assign(document.createElement('small'),{textContent:' ms'}));
 const samples=s.sample_transport||{mode:'direct-f32',physical:false};
 $('sample-link').textContent=samples.mode==='pl-q15'?(samples.physical?`PL 返回已校验 · ${ms(samples.transfer_us)} ms`:'PL 通道 · 等待回放'):'PS 文件直接输入';
 $('axi').textContent=s.axi.physical?'已 ACK · '+s.axi.ack_token:(s.axi.mode==='uio'?'UIO 已配置 · 等待提交':'未连接 · 仅预览');
 $('rs485').textContent=s.axi.snapshot?`有效 ${s.axi.snapshot.rx_valid} · CRC 错误 ${s.axi.snapshot.rx_crc_errors}`:'等待 PL 查询快照';
 $('valid').textContent=s.mailbox.flags&1?'有效结果':'无有效发布';
 $('registers').textContent=s.mailbox.registers.map(x=>x.toString(16).padStart(4,'0')).join(' ');
 $('rtu').textContent=s.mailbox.rtu_expected_response_hex.match(/../g).join(' ');
 $('frame').textContent=last?`帧 ${last.sequence} · 数据索引 ${last.dataset_index}`:'帧 —';
 $('expected').textContent=last&&last.expected!==null?`参考类别 ${s.labels[last.expected]}`:'参考类别 —';draw(last&&last.waveform);
 const body=$('rows');body.replaceChildren();if(!s.recent.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=8;td.className='empty';td.textContent='开始回放后，结果会出现在这里。';tr.append(td);body.append(tr)}
 [...s.recent].reverse().forEach(r=>{const tr=document.createElement('tr');[r.sequence,r.npu_label_name||r.label_name,r.label_name,r.diagnosis_source==='ps_rule'?(r.rule_changed?'PS 规则修正':'规则与 NPU 一致'):'NPU',r.expected_name||'—',r.correct===null?'—':r.correct?'一致':'不一致',ms(r.inference_us),ms(r.ps_application_us)].forEach((v,i)=>{const td=document.createElement('td');td.textContent=v;if(i===5&&r.correct!==null)td.className=r.correct?'ok':'bad';tr.append(td)});body.append(tr)});
 if(s.external_input?.enabled){$('progress').textContent=`电脑网口输入 · 已处理 ${s.processed} 窗`;$('frame').textContent=last?`板端帧 ${last.sequence} · 电脑窗口 ${last.network_input?.input_id}`:'等待电脑窗口';$('status').textContent=s.external_input.active?'接收电脑波形':statuses[s.status]||s.status;$('scenario-panel').hidden=true;}
 $('run-id').textContent=s.run_id?'RUN / '+s.run_id:'尚无运行';$('storage').textContent='板端存储：'+s.storage+(s.storage_io?` · 已写 ${s.storage_io.written} / ${s.processed} · 已同步 ${s.storage_io.durable}`:'');
 [['csv','results.csv'],['manifest','manifest.json']].forEach(([id,file])=>{$(id).hidden=!s.run_id;$(id).href=s.run_id?`/runs/${s.run_id}/${file}`:'#'});
}
async function action(path,data){
 if(!state)return;try{const res=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-PowerScope-Token':state.csrf},body:JSON.stringify(data)});const value=await res.json();if(!res.ok)throw Error(value.error);await poll()}catch(e){$('error').hidden=false;$('error').textContent=e.message}
}
$('mode').onchange=()=>{const monitor=$('mode').value==='monitor';$('scenario-panel').hidden=!monitor;$('repeat-control').hidden=monitor;$('dataset-control').hidden=monitor;$('start').textContent=monitor?'开始监测':'开始回放';if(monitor&&Number($('period').value)<75)$('period').value='75'};
$('start').onclick=()=>{const monitor=$('mode').value==='monitor';action('/api/start',{dataset:monitor?'validation':$('dataset').value,repeat:monitor?1:Number($('repeat').value),period_ms:Number($('period').value),monitor})};
$('inject').onclick=()=>action('/api/inject',{label:Number($('fault-label').value)});
$('recover').onclick=()=>action('/api/inject',{label:0});
$('stop').onclick=()=>action('/api/stop',{});
async function poll(){try{const res=await fetch('/api/state');if(!res.ok)throw Error('连接失败');render(await res.json());online=true}catch(e){online=false;$('connection').textContent='板端连接中断';['start','stop','inject','recover'].forEach(id=>$(id).disabled=true)}}
window.addEventListener('resize',()=>draw(state&&state.latest&&state.latest.waveform));draw(null);
(async function loop(){await poll();setTimeout(loop,300)})();
