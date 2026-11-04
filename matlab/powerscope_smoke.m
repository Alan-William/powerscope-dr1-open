function report=powerscope_smoke(projectRoot,runtimeRoot,port,duration)
% Exercise the actual generated model and live receiver, without hardware.
if nargin<4, duration=21; end
addpath(fullfile(projectRoot,'matlab'),runtimeRoot); cd(runtimeRoot);
Simulink.fileGenControl('set','CacheFolder',fullfile(runtimeRoot,'cache'), ...
    'CodeGenFolder',fullfile(runtimeRoot,'codegen'),'createDir',true);
base=sprintf('http://127.0.0.1:%d',port);
s=webread([base '/api/simulation/state']);
opts=weboptions('MediaType','application/json','ContentType','json','Timeout',10, ...
    'HeaderFields',{'X-PowerScope-Token',s.token});
mdl=powerscope_configure(runtimeRoot,1);
armed=webwrite([base '/api/simulink/arm'],struct('condition',1,'automatic',true,'duration_s',duration),opts);
report=struct('session',armed.session,'matlab',version,'duration_s',duration);
try
    setenv('POWERSCOPE_SIMULINK_CONFIG',armed.config_path);
    out=sim(mdl,'StopTime',num2str(2+duration+.1,17),'ReturnWorkspaceOutputs','on');
    report.timing=out.SimulationMetadata.TimingInfo;
    for attempt=1:30
        final=webread([base '/api/simulation/state']);
        if strcmp(final.status,'stopped'), break; end
        pause(.1);
    end
    assert(strcmp(final.status,'stopped') && isempty(final.error),'Receiver did not finish cleanly');
    assert(final.latest.window_id==round(duration/.025),'Missing model output windows');
    assert(final.dropped_windows==0 && final.invalid_packets==0,'Invalid or lost loopback packets');
    records=fullfile(projectRoot,'artifacts','simulink-live-20261103','sessions');
    f=fopen(fullfile(records,[armed.session '.windows.bin']),'rb','ieee-le');
    fseek(f,-18096,'eof'); header=fread(f,96,'*uint8'); raw=fread(f,4500,'*single'); fclose(f);
    assert(isequal(char(header(1:4).'),'PSL1'),'Invalid exported packet');
    measured=reshape(raw(1:1500),500,3);
    ts=out.get('i_meas'); logdata=double(ts.Data);
    if ndims(logdata)==3, logdata=squeeze(logdata).'; end
    indices=find(ts.Time>armed.warmup_s+duration-.025+1e-8 & ts.Time<=armed.warmup_s+duration+1e-8);
    assert(numel(indices)==500,'Unexpected Simulink logging sample grid');
    report.measured_export_max_abs=max(abs(double(measured)-double(single(logdata(indices,:)))),[],'all');
    assert(report.measured_export_max_abs<1e-6,'Recorded windows differ from actual model logging');
    truth=reshape(raw(1501:3000),500,3);
    report.last_window_kcl_max_A=max(abs(sum(double(truth),2)));
    assert(report.last_window_kcl_max_A<1e-4,'Floating-star KCL violation');
    report.windows=final.latest.window_id;
    report.elapsed_wall_s=final.latest.wall_elapsed_s;
    report.success=true;
    trace=struct('t',ts.Time,'i_meas',logdata,'i_true',out.get('i_true').Data);
    save(fullfile(projectRoot,'artifacts','simulink-live-20261103',[armed.session '.mat']),'-struct','trace','-v7');
catch err
    report.success=false;report.error=getReport(err,'extended','hyperlinks','off');
    webwrite([base '/api/simulink/error'],struct('session',armed.session,'error',err.message),opts);
end
file=fullfile(projectRoot,'artifacts','simulink-live-20261103',[armed.session '.matlab.json']);
f=fopen(file,'w','n','UTF-8');fprintf(f,'%s',jsonencode(report));fclose(f);
disp(jsonencode(report));
if ~report.success, error('PowerScope:SmokeFailed','%s',report.error); end
close_system(mdl,0);
end
