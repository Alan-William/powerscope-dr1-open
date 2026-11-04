function result = powerscope_build(projectRoot, runtimeRoot, sourceRoot)
% Prepare a derived live model. Research sources are copied and hash-bound.
if ~exist(runtimeRoot,'dir'), mkdir(runtimeRoot); end
cd(runtimeRoot);
addpath(fullfile(projectRoot,'matlab'), runtimeRoot);
Simulink.fileGenControl('set','CacheFolder',fullfile(runtimeRoot,'cache'), ...
    'CodeGenFolder',fullfile(runtimeRoot,'codegen'),'createDir',true);
inputs={'npc_diag.slx','ctrl_npc.m','params_npc.m','sensor_fault.m'};
snapshot=fullfile(runtimeRoot,'research-copy','sim');
if ~exist(snapshot,'dir'), mkdir(snapshot); end
sourceHashes=struct();
for k=1:numel(inputs)
    src=fullfile(sourceRoot,inputs{k});
    copyfile(src,fullfile(snapshot,inputs{k}));
    sourceHashes.(matlab.lang.makeValidName(inputs{k}))=file_hash(src);
end
addpath(snapshot);
copyfile(fullfile(projectRoot,'matlab','powerscope_bridge.c'),runtimeRoot);
clear powerscope_bridge
mex('-v','-R2018a','-outdir',runtimeRoot,fullfile(runtimeRoot,'powerscope_bridge.c'), ...
    fullfile(matlabroot,'sys','lcc64','lcc64','lib64','ws2_32.lib'));
original='npc_diag'; mdl='powerscope_npc_live';
if bdIsLoaded(mdl), close_system(mdl,0); end
load_system(fullfile(snapshot,[original '.slx']));
rt=sfroot; ctrl=rt.find('-isa','Stateflow.EMChart','Path',[original '/Ctrl']);
assert(numel(ctrl)==1 && strcmp(strrep(ctrl.Script,char(13),''), ...
    strrep(fileread(fullfile(snapshot,'ctrl_npc.m')),char(13),'')), 'Controller/source mismatch');
save_system(original,fullfile(runtimeRoot,[mdl '.slx']));
if bdIsLoaded(original), close_system(original,0); end
load_system(fullfile(runtimeRoot,[mdl '.slx']));

% Replace the fixed event with a held live command. Plant and dq PI stay intact.
remove={'FaultStep','FaultBits','MxGain','Ones12','SumMask','SensMode','SensPh','SensAmp','FaultT'};
for k=1:numel(remove)
    block=[mdl '/' remove{k}]; handles=get_param(block,'LineHandles');
    fields=fieldnames(handles);
    for j=1:numel(fields)
        for line=reshape(handles.(fields{j}),1,[])
            if line~=-1, delete_line(line); end
        end
    end
    delete_block(block);
end
fprintf('BUILD: original event blocks removed\n');
bridge=[mdl '/Web_Link'];
add_block('simulink/User-Defined Functions/S-Function',bridge, ...
    'FunctionName','powerscope_bridge','Position',[40 700 210 870]);
add_line(mdl,'Web_Link/1','MaskGate/2','autorouting','on');
for k=2:5, add_line(mdl,sprintf('Web_Link/%d',k),sprintf('Sensor/%d',k+1),'autorouting','on'); end
sensor=rt.find('-isa','Stateflow.EMChart','Path',[mdl '/Sensor']);
sensor.Script=fileread(fullfile(projectRoot,'matlab','powerscope_sensor.m'));
add_line(mdl,'Sensor/1','Web_Link/1','autorouting','on');
add_line(mdl,'VI1/2','Web_Link/2','autorouting','on');
add_block('simulink/Sources/Constant',[mdl '/Fault_Command'], ...
    'Value','[0 0]','Position',[30 930 150 965]);
add_line(mdl,'Fault_Command/1','Web_Link/5','autorouting','on');
fprintf('BUILD: live command block wired\n');

% Ideal V-I probe between the LC node and the load. Power uses load current.
load_system('spsThreePhaseVIMeasurementLib');
blocks=find_system('spsThreePhaseVIMeasurementLib','SearchDepth',1,'LookUnderMasks','all');
probe=[mdl '/Load_VI']; add_block(blocks{2},probe,'Position',[820 70 860 190]);
set_param(probe,'VoltageMeasurement','phase-to-ground','CurrentMeasurement','yes');
lf=get_param([mdl '/Lf'],'PortHandles'); ld=get_param([mdl '/Load'],'PortHandles');
cf=get_param([mdl '/Cf'],'PortHandles');
vi=get_param(probe,'PortHandles');
for k=1:3
    % Rebuild this named three-node physical branch using line handles.
    % The signal-port overload of delete_line does not accept SPS connectors.
    for port=[lf.RConn(k),cf.LConn(k),ld.LConn(k)]
        line=get_param(port,'Line');
        if line~=-1, delete_line(line); end
    end
    add_line(mdl,lf.RConn(k),cf.LConn(k),'autorouting','on');
    add_line(mdl,lf.RConn(k),vi.LConn(k),'autorouting','on');
    add_line(mdl,vi.RConn(k),ld.LConn(k),'autorouting','on');
end
add_block('simulink/Math Operations/Gain',[mdl '/Load_Phase_Voltage'], ...
    'Gain','eye(3)-ones(3)/3','Multiplication','Matrix(K*u)', ...
    'Position',[920 80 990 125]);
add_line(mdl,'Load_VI/1','Load_Phase_Voltage/1','autorouting','on');
add_line(mdl,'Load_Phase_Voltage/1','Web_Link/3','autorouting','on');
add_line(mdl,'Load_VI/2','Web_Link/4','autorouting','on');
fprintf('BUILD: load voltage/current probe wired\n');

% A bounded Scope is available next to the physical model for recording.
scope=[mdl '/Current_Scope']; add_block('simulink/Sinks/Scope',scope, ...
    'Position',[480 760 540 820],'NumInputPorts','2');
% Scope must run at ADC rate, not the inherited 1 MHz electrical solver rate.
for k=1:2
    zoh=sprintf('Scope_ADC%d',k);
    add_block('simulink/Discrete/Zero-Order Hold',[mdl '/' zoh], ...
        'SampleTime','50e-6','Position',[350 740+50*k 410 770+50*k]);
    source={'Sensor/1','VI1/2'};
    add_line(mdl,source{k},[zoh '/1'],'autorouting','on');
    add_line(mdl,[zoh '/1'],sprintf('Current_Scope/%d',k),'autorouting','on');
end
cfg=get_param(scope,'ScopeConfiguration');
cfg.TimeSpan='.1'; cfg.LimitDataPoints=true; cfg.MaxDataPoints='6000';
cfg.Decimation='10';
fprintf('BUILD: Scope configured\n');
% Bounded research logging retains the model's inspectable internal signals.
logs=find_system(mdl,'SearchDepth',1,'BlockType','ToWorkspace');
for k=1:numel(logs), set_param(logs{k},'MaxDataPoints','4000'); end
set_param(mdl,'SimulationMode','accelerator','StopTime','602', ...
    'DefaultParameterBehavior','Tunable', ...
    'Description','PowerScope: original NPC Simscape plant and dq controller; live loopback window interface.', ...
    'ZoomFactor','FitSystem');
try, Simulink.BlockDiagram.arrangeSystem(mdl); catch, end
save_system(mdl,fullfile(runtimeRoot,[mdl '.slx']));
result=struct('model',mdl,'matlab',version,'source',sourceHashes, ...
    'model_sha256',file_hash(fullfile(runtimeRoot,[mdl '.slx'])), ...
    'bridge_sha256',file_hash(fullfile(runtimeRoot,['powerscope_bridge.' mexext])), ...
    'created_at',char(datetime('now','TimeZone','UTC','Format','yyyy-MM-dd''T''HH:mm:ssXXX')));
result.implementation=struct();
for name={'powerscope_bridge.c','powerscope_sensor.m','powerscope_build.m'}
    result.implementation.(matlab.lang.makeValidName(name{1}))=file_hash(fullfile(projectRoot,'matlab',name{1}));
end
f=fopen(fullfile(runtimeRoot,'build.json'),'w','n','UTF-8');
fprintf(f,'%s',jsonencode(result)); fclose(f);
end

function value=file_hash(path)
f=fopen(path,'rb'); bytes=fread(f,Inf,'*uint8'); fclose(f);
md=java.security.MessageDigest.getInstance('SHA-256'); md.update(bytes);
value=lower(reshape(dec2hex(typecast(md.digest(),'uint8'),2).',1,[]));
end
