function mdl=powerscope_configure(runtimeRoot,condition)
% Configure only the isolated generated model before a new continuous run.
addpath(runtimeRoot,fullfile(runtimeRoot,'research-copy','sim'));
p=params_npc(); c=p.cond(condition); mdl='powerscope_npc_live';
load_system(fullfile(runtimeRoot,[mdl '.slx']));
assert(strcmp(get_param(mdl,'SimulationStatus'),'stopped'),'Stop the current simulation first');
set_param([mdl '/Vdc'],'Amplitude',num2str(c.Vdc/2,17));
set_param([mdl '/Vdn'],'Amplitude',num2str(c.Vdc/2,17));
set_param([mdl '/Load'],'Resistance',num2str(c.R,17),'Inductance',num2str(c.L,17));
for item={'Im','Vdc','Kp','Ki'}
    name=item{1}; block=name;
    if strcmp(name,'Vdc'), block='VdcC'; end
    set_param([mdl '/' block],'Value',num2str(c.(name),17));
end
set_param([mdl '/Fault_Command'],'Value','[0 0]');
set_param(mdl,'SimulationMode','accelerator','StopTime','602','Dirty','off');
end
