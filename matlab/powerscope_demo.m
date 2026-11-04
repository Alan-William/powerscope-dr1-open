function fig=powerscope_demo(projectRoot,runtimeRoot,port)
% Native MATLAB controls and live plots for the original Simulink NPC model.
% No browser is used. The local receiver records samples and bridges to DR1.
if nargin<1, projectRoot=fileparts(fileparts(mfilename('fullpath'))); end
if nargin<2, runtimeRoot='E:/fpga_workflow_runs/powerscope-simulink-runtime'; end
if nargin<3, port=18768; end
addpath(fullfile(projectRoot,'matlab'),runtimeRoot);
cd(runtimeRoot);
Simulink.fileGenControl('set','CacheFolder',fullfile(runtimeRoot,'cache'), ...
    'CodeGenFolder',fullfile(runtimeRoot,'codegen'),'createDir',true);
base=sprintf('http://127.0.0.1:%d',port);
readOptions=weboptions('Timeout',2,'ContentType','json');
s=webread([base '/api/simulation/state?display=matlab'],readOptions);
token=s.token;
writeOptions=weboptions('Timeout',4,'MediaType','application/json','ContentType','json', ...
    'HeaderFields',{'X-PowerScope-Token',token});
mdl=powerscope_configure(runtimeRoot,1);
powerscope_scope(mdl);
lastWindow=-1; lastSession=''; currentSession=''; faultSerial=0; closing=false;
labels=cellstr(string(s.labels)); colors=[.78 .50 .20;.08 .50 .54;.43 .38 .65];
fig=uifigure('Name','PowerScope | Simulink 在线故障演示','Position',[70 60 1480 880], ...
    'Color',[.95 .97 .985],'Tag','PowerScopeSimulinkDemo');
outer=uigridlayout(fig,[2 2],'RowHeight',{65,'1x'},'ColumnWidth',{275,'1x'}, ...
    'Padding',[18 12 18 18],'RowSpacing',12,'ColumnSpacing',15);
head=uipanel(outer,'BorderType','none','BackgroundColor',fig.Color); head.Layout.Column=[1 2];
hg=uigridlayout(head,[2 2],'RowHeight',{30,22},'ColumnWidth',{'1x',380},'Padding',[0 0 0 0]);
uilabel(hg,'Text','PowerScope  /  Simulink 在线故障演示','FontSize',23,'FontWeight','bold','FontColor',[.13 .23 .33]);
status=uilabel(hg,'Text','就绪','HorizontalAlignment','right','FontSize',13,'FontColor',[.08 .50 .45]);
uilabel(hg,'Text','原 NPC 功率电路 · 运行中故障注入 · 连续电流与电压波形','FontSize',11,'FontColor',[.44 .52 .58]);
uilabel(hg,'Text','FPGA / NPU / AXI / RS485','HorizontalAlignment','right','FontSize',11,'FontColor',[.44 .52 .58]);
left=uipanel(outer,'Title','运行与故障控制','FontWeight','bold','BackgroundColor',[1 1 1]);
lg=uigridlayout(left,[20 2],'ColumnWidth',{'1x','1x'},'RowHeight', ...
    {20,32,36,36,16,22,34,34,34,16,22,32,34,65,55,22,32,34,'1x',30},'Padding',[15 13 15 10],'RowSpacing',5);
label=uilabel(lg,'Text','电路工况','FontColor',[.4 .5 .57]); label.Layout.Column=[1 2];
condition=uidropdown(lg,'Items',cellstr(string({s.conditions.name})),'ItemsData',1:4); condition.Layout.Column=[1 2];
startButton=uibutton(lg,'Text','启动仿真','ButtonPushedFcn',@(~,~)start_run(false));
autoButton=uibutton(lg,'Text','自动 21 秒','ButtonPushedFcn',@(~,~)start_run(true));
stopButton=uibutton(lg,'Text','停止并保存','Enable','off','ButtonPushedFcn',@(~,~)stop_run()); stopButton.Layout.Column=[1 2];
space=uilabel(lg,'Text',''); space.Layout.Column=[1 2];
label=uilabel(lg,'Text','故障位置与类型','FontColor',[.4 .5 .57]); label.Layout.Column=[1 2];
fault=uidropdown(lg,'Items',labels,'ItemsData',0:21,'Value',2); fault.Layout.Column=[1 2];
inject=uibutton(lg,'Text','注入故障','Enable','off','ButtonPushedFcn',@(~,~)apply_fault(fault.Value));
recover=uibutton(lg,'Text','恢复正常','Enable','off','ButtonPushedFcn',@(~,~)apply_fault(0));
inputLabel=uilabel(lg,'Text','输入状态 · 正常运行','FontSize',12,'FontColor',[.08 .50 .45]); inputLabel.Layout.Column=[1 2];
space=uilabel(lg,'Text',''); space.Layout.Column=[1 2];
label=uilabel(lg,'Text','Simulink 模型与 Scope','FontColor',[.4 .5 .57]); label.Layout.Column=[1 2];
uibutton(lg,'Text','查看电路模型','ButtonPushedFcn',@(~,~)open_system(mdl));
uibutton(lg,'Text','打开实时 Scope','ButtonPushedFcn',@(~,~)open_system([mdl '/Current_Scope']));
showActual=uicheckbox(lg,'Text','叠加真实电流（虚线）','Value',true,'ValueChangedFcn',@(~,~)actual_visibility()); showActual.Layout.Column=[1 2];
notes=uilabel(lg,'Text',sprintf('20 kHz 采样 / 每窗 500 点\n1 μs 电路步长 / 10 kHz PWM\n故障操作改变实际模型输入'),'FontSize',10,'FontColor',[.45 .53 .6]); notes.Layout.Column=[1 2];
engine=uilabel(lg,'Text',sprintf('MATLAB / Simulink Accelerator\n本机连续电路仿真'),'FontSize',11,'FontColor',[.14 .37 .48]); engine.Layout.Column=[1 2];
label=uilabel(lg,'Text','DR1 诊断与 RS485','FontColor',[.4 .5 .57]); label.Layout.Column=[1 2];
serial=uidropdown(lg,'Items',{'选择 USB-RS485 串口'},'ItemsData',{''}); serial.Layout.Column=[1 2];
boardStart=uibutton(lg,'Text','接入 DR1','Enable','off','ButtonPushedFcn',@(~,~)connect_board());
boardStop=uibutton(lg,'Text','停止传输','Enable','off','ButtonPushedFcn',@(~,~)post('/api/demo/stop',struct()));
diagnosis=uilabel(lg,'Text',sprintf('本机仿真\nsfaug-r1 · 纯 NPU 诊断接口'),'WordWrap','on','FontSize',12,'FontColor',[.24 .38 .48]); diagnosis.Layout.Column=[1 2];
saveButton=uibutton(lg,'Text','打开本次记录目录','ButtonPushedFcn',@(~,~)winopen(fullfile(projectRoot,'artifacts','simulink-live-20261103','sessions'))); saveButton.Layout.Column=[1 2];

right=uipanel(outer,'BorderType','none','BackgroundColor',fig.Color);
rg=uigridlayout(right,[7 1],'RowHeight',{54,25,'1x',24,'1x',24,125},'Padding',[0 0 0 0],'RowSpacing',7);
metricsPanel=uipanel(rg,'BorderType','none','BackgroundColor',[1 1 1]);
mg=uigridlayout(metricsPanel,[1 4],'ColumnWidth',{'1x','1.3x','1x','1.3x'},'Padding',[15 5 10 5]);
clockLabel=uilabel(mg,'Text',sprintf('仿真时间\n0.000 s'),'FontSize',13,'FontWeight','bold');
rmsLabel=uilabel(mg,'Text',sprintf('电流有效值 / A\n—'),'FontSize',12);
powerLabel=uilabel(mg,'Text',sprintf('三相负载有功功率\n— W'),'FontSize',12);
paceLabel=uilabel(mg,'Text',sprintf('仿真 / 实际时间\n—'),'FontSize',12);
uilabel(rg,'Text','传感器输出与真实电流 / A','FontSize',13,'FontWeight','bold');
axI=uiaxes(rg); prepare_axes(axI,'时间 / ms','电流 / A');
hold(axI,'on'); current=gobjects(1,3); actual=gobjects(1,3);
for k=1:3
    current(k)=plot(axI,nan,nan,'Color',colors(k,:),'LineWidth',1.5,'DisplayName',['I' char('a'+k-1)]);
    actual(k)=plot(axI,nan,nan,'--','Color',colors(k,:)*.65+.35,'LineWidth',1.1,'HandleVisibility','off');
end
legend(axI,current,{'A 相','B 相','C 相'},'Location','northeast','Orientation','horizontal','Box','off');
uilabel(rg,'Text','LC 滤波后相电压 / V','FontSize',13,'FontWeight','bold');
axV=uiaxes(rg); prepare_axes(axV,'时间 / ms','相电压 / V'); hold(axV,'on'); voltage=gobjects(1,3);
for k=1:3, voltage(k)=plot(axV,nan,nan,'Color',colors(k,:),'LineWidth',1.4); end
eventsLabel=uilabel(rg,'Text','运行、故障注入与恢复时间线','FontSize',11,'FontColor',[.38 .48 .56]);
axTrend=uiaxes(rg); prepare_axes(axTrend,'仿真时间 / s','RMS / A'); hold(axTrend,'on'); trend=gobjects(1,3);
for k=1:3, trend(k)=plot(axTrend,nan,nan,'Color',colors(k,:),'LineWidth',1.3); end
fig.CloseRequestFcn=@(~,~)close_app();
try
    ports=webread([base '/api/ports'],readOptions);
    if ~isempty(ports.ports)
        serial.Items=[{'选择 USB-RS485 串口'},reshape(arrayfun(@(p)[p.device ' · ' p.description],ports.ports,'UniformOutput',false),1,[])];
        serial.ItemsData=[{''},reshape(arrayfun(@(p)p.device,ports.ports,'UniformOutput',false),1,[])];
    end
catch
end
pollTimer=timer('ExecutionMode','fixedSpacing','Period',.35,'BusyMode','drop','TimerFcn',@(~,~)refresh());
fig.UserData=struct('model',mdl,'runtime',runtimeRoot,'server',base,'timer',pollTimer);
start(pollTimer); open_system(mdl); figure(fig);
fprintf('PowerScope MATLAB console ready. Original model: %s\n',fullfile(runtimeRoot,[mdl '.slx']));

    function result=post(path,body)
        result=webwrite([base path],body,writeOptions);
    end
    function start_run(automatic)
        try
            startButton.Enable='off'; autoButton.Enable='off'; status.Text='准备 Simulink 模型'; drawnow;
            powerscope_configure(runtimeRoot,condition.Value);
            armed=post('/api/simulink/arm',struct('condition',condition.Value,'automatic',logical(automatic)));
            currentSession=armed.session; faultSerial=0;
            setenv('POWERSCOPE_SIMULINK_CONFIG',armed.config_path);
            set_param(mdl,'StopTime',num2str(armed.warmup_s+armed.duration_s+.1,17));
            set_param(mdl,'SimulationCommand','start');
        catch err
            if ~isempty(currentSession)
                try, post('/api/simulink/error',struct('session',currentSession,'error',err.message)); catch, end
            end
            status.Text='启动已停止'; uialert(fig,err.message,'Simulink');
        end
    end
    function apply_fault(label)
        try
            faultSerial=faultSerial+1;
            set_param([mdl '/Fault_Command'],'Value',mat2str([faultSerial label]));
        catch err, uialert(fig,err.message,'故障操作'); end
    end
    function stop_run()
        try
            if ~strcmp(get_param(mdl,'SimulationStatus'),'stopped'), set_param(mdl,'SimulationCommand','stop'); end
            post('/api/simulation/stop',struct());
        catch err, if ~closing, uialert(fig,err.message,'停止仿真'); end, end
    end
    function connect_board()
        try, post('/api/demo/start',struct('source','live','port',serial.Value));
        catch err, uialert(fig,err.message,'DR1 接入'); end
    end
    function actual_visibility()
        for k=1:3, actual(k).Visible=matlab.lang.OnOffSwitchState(showActual.Value); end
    end
    function refresh()
        if closing || ~isvalid(fig), return; end
        try
            state=webread([base '/api/simulation/state?display=matlab'],readOptions);
            active=any(strcmp(state.status,{'starting','running','stopping'}));
            isRunning=strcmp(state.status,'running');
            startButton.Enable=on_off(~active); autoButton.Enable=on_off(~active);
            condition.Enable=on_off(~active); stopButton.Enable=on_off(active);
            inject.Enable=on_off(isRunning); recover.Enable=on_off(isRunning);
            status.Text=state.stage;
            if ~isempty(state.error), status.FontColor=[.7 .32 .18]; else, status.FontColor=[.08 .50 .45]; end
            inputLabel.Text=['输入状态 · ' labels{state.label+1}];
            if state.label, inputLabel.FontColor=[.7 .39 .19]; else, inputLabel.FontColor=[.08 .50 .45]; end
            boardActive=isstruct(state.transport) && any(strcmp(state.transport.status,{'starting','running','stopping'}));
            boardStart.Enable=on_off(isRunning && ~boardActive && ~isempty(serial.Value));
            boardStop.Enable=on_off(boardActive); serial.Enable=on_off(~boardActive);
            same=false;
            if isstruct(state.transport) && isstruct(state.transport.latest_input) && isfield(state.transport.latest_input,'simulator_session')
                same=strcmp(state.transport.latest_input.simulator_session,state.session);
            end
            if same && isstruct(state.transport.latest_match)
                match=state.transport.latest_match;
                diagnosis.Text=sprintf('%s\n帧 %d · NPU %.2f ms\n发送至回传 %.1f ms',match.label_name,match.sequence,match.inference_us/1000,match.end_to_end_ms);
            elseif boardActive, diagnosis.Text='正在接入 DR1 / RS485';
            else, diagnosis.Text=sprintf('本机仿真\nsfaug-r1 · 纯 NPU 诊断接口'); end
            if ~isstruct(state.latest), return; end
            batch=state.latest;
            if strcmp(lastSession,state.session) && batch.window_id==lastWindow, return; end
            lastSession=state.session; lastWindow=batch.window_id;
            clockLabel.Text=sprintf('仿真时间\n%.3f s',state.elapsed_s);
            rmsLabel.Text=sprintf('电流有效值 / A\nA %.2f  B %.2f  C %.2f',batch.rms_a);
            powerLabel.Text=sprintf('三相负载有功功率\n%.1f W',batch.power_w);
            paceLabel.Text=sprintf('仿真 / 实际 %.3f×\n窗口执行 %.2f ms',batch.pace_ratio,batch.compute_ms);
            measured=phase_matrix(batch.measured); truth=phase_matrix(batch.actual); volts=phase_matrix(batch.voltage);
            % The compact display takes samples 1,5,...,497 of a 500-point window.
            t=.05+(0:size(measured,2)-1)*(25/size(measured,2));
            for k=1:3
                set(current(k),'XData',t,'YData',measured(k,:)); set(actual(k),'XData',t,'YData',truth(k,:));
                set(voltage(k),'XData',t,'YData',volts(k,:));
            end
            axI.XLim=[0 25]; axV.XLim=[0 25];
            history=state.history;
            if ~isempty(history)
                htime=[history.time_s]; hrms=vertcat(history.rms_a);
                if size(hrms,2)~=3, hrms=reshape(hrms,3,[]).'; end
                for k=1:3, set(trend(k),'XData',htime,'YData',hrms(:,k)); end
                axTrend.XLim=[max(0,state.elapsed_s-30),max(12,state.elapsed_s)];
                axTrend.YLim=[0,max(4,ceil(max(hrms,[],'all')*1.15))];
            end
            if ~isempty(state.events)
                ev=state.events(max(1,end-2):end);
                eventsLabel.Text=strjoin(arrayfun(@(e)sprintf('%.2f s %s',e.time_s,e.name),ev,'UniformOutput',false),'   |   ');
            end
            drawnow limitrate nocallbacks;
        catch err
            status.Text=['连接状态：' err.message];
        end
    end
    function close_app()
        closing=true; stop(pollTimer); delete(pollTimer); stop_run(); delete(fig);
    end
end

function prepare_axes(ax,xname,yname)
ax.Color=[1 1 1]; ax.FontName='Microsoft YaHei'; ax.FontSize=10;
ax.XColor=[.43 .52 .59]; ax.YColor=[.43 .52 .59];
ax.GridColor=[.74 .81 .86]; ax.GridAlpha=.35;
grid(ax,'on'); xlabel(ax,xname); ylabel(ax,yname); ax.Box='off';
end
function value=on_off(enabled)
if enabled, value='on'; else, value='off'; end
end
function values=phase_matrix(values)
if size(values,1)~=3, values=values.'; end
assert(size(values,1)==3,'Expected three phase samples');
end
