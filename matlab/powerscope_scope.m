function powerscope_scope(mdl)
% Display-only configuration, applied to the isolated model in memory.
cfg=get_param([mdl '/Current_Scope'],'ScopeConfiguration');
colors=[.78 .50 .20;.08 .50 .54;.43 .38 .65];
graphics=struct('FigureColor','[.95 .97 .985]', ...
    'AxesColor','[1 1 1]','AxesTickColor','[.33 .42 .50]', ...
    'LineColors',mat2str([colors;colors]),'LineStyles','-|-|-|-|-|-', ...
    'LineWidths','[1.4 1.4 1.4 1.4 1.4 1.4]', ...
    'MarkerStyles','none|none|none|none|none|none');
% R2023b applies FigureColor only before the Scope is first instantiated.
cfg.ScopeGraphics=graphics;
cfg.LayoutDimensions=[2 1];
cfg.TimeSpan='.04'; cfg.TimeUnits='Seconds';
cfg.Position=[390 100 1000 720];
cfg.ReduceUpdates=true;
cfg.Decimation='4';
cfg.AxesScaling='Auto';
cfg.PreserveColorsForCopyToClipboard=true;
titles={'传感器测量电流  /  A 相 · B 相 · C 相', ...
        '电路真实电流  /  A 相 · B 相 · C 相'};
for k=1:2
    cfg.ActiveDisplay=num2str(k);
    cfg.Title=titles{k}; cfg.YLabel='电流 / A';
    cfg.ShowGrid=true; cfg.ShowLegend=false;
end
% R2023b exposes its native Scope style through the compatibility structure.
% Colors match the MATLAB control panel; no signal samples are altered.
cfg.ScopeGraphics=graphics;
end
