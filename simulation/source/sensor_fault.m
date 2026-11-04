function imeas = sensor_fault(iact, t, mode, ph, amp, tf)
%SENSOR_FAULT  三相电流传感器故障模型
%
%   说明：传感器是代数模型，不涉及积分，因此不需要时间门控（块按 1 µs 执行不影响
%   正确性，只是把同一采样值重复算了几十次）。保留最简单的单出口结构，便于代码生成。
%
%   输入：iact(3x1) 真实电流, t 时间, mode 0=N/1=DF/2=SF/3=OF,
%         ph 故障相, amp OF 偏置幅度, tf 故障注入时刻
%   输出：imeas(3x1) 传感器输出（送控制器 + 作为诊断输入记录）

%#codegen
persistent held
if isempty(held)
    held = zeros(3,1);
end

imeas = iact;
if t < tf
    held = iact;          % 故障前持续跟踪真实值
else
    for k = 1:3
        if k == ph
            if mode == 1
                imeas(k) = 0;              % 断线
            elseif mode == 2
                imeas(k) = held(k);        % 卡死
            elseif mode == 3
                imeas(k) = iact(k) + amp;  % 偏置
            end
        end
    end
end
end
