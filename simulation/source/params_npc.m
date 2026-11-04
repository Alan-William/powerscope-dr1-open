function p = params_npc()
%PARAMS_NPC  三电平 NPC 逆变器故障诊断数据生成平台 —— 全部参数集中于此
%
%   p = params_npc()  返回结构体 p，供 build_npc_model.m / run_one.m / run_sweep.m 使用。
%
%   参数来源标注：
%     [论文]  = Xu et al., IEEE TIE 2026, Table II / Table III，直接照抄
%     [补充]  = 论文未给，按工程常规取值（复现报告里必须写明，见 §2 审计表）
%
%   单位：SI（V, A, Ω, H, F, s, Hz）

% ---------------- 1. 论文 Table II：平台参数 ----------------
p.fs      = 10e3;      % [论文] 开关频率 10 kHz
p.f0      = 50;        % [论文] 输出频率 50 Hz
p.C1      = 5600e-6;   % [论文] 直流侧电容 C1
p.C2      = 5600e-6;   % [论文] 直流侧电容 C2
p.Lf      = 2e-3;      % [论文] 滤波电感 2 mH
p.Cf      = 50e-6;     % [论文] 滤波电容 50 uF

% ---------------- 2. 仿真/采样设置 ----------------
p.Ts_sim  = 1e-6;      % [补充] 电力系统离散步长 1 us（10 kHz 开关要求，见建模清单 §4）
p.Ts_ctrl = 50e-6;     % [补充] 控制/ADC 采样周期 50 us = 20 kHz（= 2*fs，双更新 PWM）
p.Ts_adc  = 50e-6;     % [补充] 数据记录周期（= Ts_ctrl）
p.t_end   = 4.0;       % [补充] 单次仿真时长 4 s（约切出 148 个 500 点窗口/工况，够训练+测试）
p.t_fault = 0.30;      % [补充] 故障注入时刻（前 0.3 s 为健康段，用于取"正常"类样本）
p.fault_phase_dither = true;   % [补充] 故障时刻按 2 ms 抖动，使故障发生在不同电流相位

% ---------------- 3. 功率电路补充参数 ----------------
p.Rf      = 0.05;      % [补充] 滤波电感等效串联电阻（影响阻尼与 PI 零点）
p.Ron     = 1e-3;      % [补充] 器件通态电阻（SPS 三电平桥 Ron）
p.Vf      = [0 0];     % [补充] 器件正向压降 [IGBT 二极管]，0 = 理想
p.Csnub   = inf;       % [补充] 吸收电容 inf = 理想
p.Rsnub   = 1e5;       % [补充] 吸收电阻
p.deadtime = 0;        % [补充] 死区 0（论文未提及；>0 时按 duty 误差等效，见建模清单 §7）

% ---------------- 4. 控制环设计 ----------------
p.m_peak  = 0.80;      % [补充] 目标**实际**调制深度（留 20% 裕度，避免过调制削顶）
p.f_bw    = 300;       % [补充] 电流环带宽 300 Hz（fs/33，远低于开关频率、高于 7 次谐波）
p.Kp_v    = 2*pi*p.f_bw*p.Lf;      % 电压量纲比例增益 [V/A]
p.Ki_v    = 2*pi*p.f_bw*p.Rf;      % 电压量纲积分增益 [V/(A·s)]
p.i_limit = 3.0;       % [补充] 电流软限幅倍数（相对额定峰值），防 CS-DF 时发散

% ---------------- 5. 电流传感器故障参数 ----------------
p.of_pu   = 0.20;      % [补充] 偏置故障幅度 = 20% 额定峰值（论文未给）
p.noise_snr = 60;      % [补充] 传感器白噪声 SNR (dB)，inf = 不加噪

% ---------------- 6. 论文 Table III：8 种工况 ----------------
%  id  Udc(V)  R(Ω)   L(H)
C = [ 1  100    10     0
      2  100    20     0
      3  100    10     1e-3
      4  100     5     2e-3
      5   80     2     0
      6   80     2     1e-3
      7  240    50     2e-3
      8  800   200     0 ];

p.cond = struct('id',{},'Vdc',{},'R',{},'L',{},'Im',{},'Kp',{},'Ki',{});
w = 2*pi*p.f0;
for k = 1:size(C,1)
    Vdc = C(k,2); R = C(k,3); L = C(k,4);
    Zc  = 1/(1j*w*p.Cf);              % 滤波电容阻抗
    Zl  = R + 1j*w*L;                 % 负载阻抗
    Zeq = 1/(1/Zc + 1/Zl);            % 电感电流 -> 负载电压
    Ztot = p.Rf + 1j*w*p.Lf + Zeq;    % 桥端电压/电感电流（含 Lf，低阻工况下不可忽略）
    Im  = p.m_peak*(Vdc/2)/abs(Ztot); % 使实际调制深度 ~ m_peak 的电流幅值
    p.cond(k).id  = C(k,1);
    p.cond(k).Vdc = Vdc;
    p.cond(k).R   = R;
    p.cond(k).L   = L;
    p.cond(k).Im  = Im;
    p.cond(k).Kp  = p.Kp_v/(Vdc/2);   % 换算到调制比量纲
    p.cond(k).Ki  = p.Ki_v/(Vdc/2);
end

% ---------------- 7. 输出目录 ----------------
p.proj    = fileparts(fileparts(mfilename('fullpath')));   % .../Xu2026-repro
p.controller_version = 'sampled-zoh-v4';
p.outdir  = fullfile(p.proj,'data','raw_v4'); % old raw/ is historical, never mixed
p.model   = 'npc_diag';
p.n_class = 22;
p.n_cond  = numel(p.cond);

% ---------------- 8. 自检 ----------------
f_res = 1/(2*pi*sqrt(p.Lf*p.Cf));
assert(p.f0 < p.f_bw && p.f_bw < p.fs/10, '电流环带宽应满足 f0 << f_bw << fs/10');
if p.Ts_ctrl > 1/(2*p.fs), error('控制采样率必须 >= 2*fs'); end
fprintf('[params] %d 工况, LC 谐振 %.1f Hz, 电流环 %.0f Hz (Kp_v=%.3g, Ki_v=%.3g)\n', ...
        p.n_cond, f_res, p.f_bw, p.Kp_v, p.Ki_v);
for k=1:p.n_cond
    fprintf('   工况%d: Vdc=%4.0f V, R=%4.0f Ω, L=%5.1f mH -> Im=%5.2f A\n', ...
            p.cond(k).id, p.cond(k).Vdc, p.cond(k).R, p.cond(k).L*1e3, p.cond(k).Im);
end
end
