function [m_abc, idq] = ctrl_npc(iabc, t, Im, Vdc, Kp, Ki, w, vc1, vc2)
%CTRL_NPC  20 kHz sampled dq PI controller, called by a 1 us plant solver.
% Version: sampled-zoh-v4. All outputs are held between integer 50 us ticks.
% Integrate the previous sampled error over its hold time. Saturated intervals
% freeze the integral but ALWAYS advance the clock. No fictitious startup dt.
%#codegen
Ts = 50e-6;
Lf = 2e-3;
K_NP = 0.004;
M_NP_MAX = 0.12;
persistent xi last_tick last_update last_call old_error allow_integral m_hold dq_hold
if isempty(xi)
    xi = zeros(2,1);
    last_tick = -1;
    last_update = t;
    last_call = t;
    old_error = zeros(2,1);
    allow_integral = false;
    m_hold = zeros(3,1);
    dq_hold = zeros(2,1);
end
if t < last_call - 1e-12
    xi = zeros(2,1);
    last_tick = -1;
    last_update = t;
    old_error = zeros(2,1);
    allow_integral = false;
    m_hold = zeros(3,1);
    dq_hold = zeros(2,1);
end
last_call = t;
% Tiny tolerance only removes floating point boundary error (5 ps).
tick = floor(t / Ts + 1e-7);
if tick == last_tick
    m_abc = m_hold;
    idq = dq_hold;
    return
end
dt = t - last_update;
if last_tick >= 0 && allow_integral
    xi = xi + old_error * dt;
end
last_update = t;                 % advance even while saturated
last_tick = tick;
th = w*t;
c = [cos(th); cos(th-2*pi/3); cos(th+2*pi/3)];
s = [sin(th); sin(th-2*pi/3); sin(th+2*pi/3)];
id = (2/3)*(c'*iabc);
iq = -(2/3)*(s'*iabc);
err = [Im-id; -iq];
md = Kp*err(1) + Ki*xi(1) + (2*w*Lf/Vdc)*iq;
mq = Kp*err(2) + Ki*xi(2) - (2*w*Lf/Vdc)*id;
cm = max(min(-K_NP*(vc1-vc2), M_NP_MAX), -M_NP_MAX);
raw = md*c - mq*s + cm;
allow_integral = abs(md) <= 1 && abs(mq) <= 1 && all(abs(raw) <= 1);
old_error = err;
md = max(min(md,1),-1);
mq = max(min(mq,1),-1);
m_hold = max(min(md*c-mq*s+cm,1),-1);
dq_hold = [id;iq];
m_abc = m_hold;
idq = dq_hold;
end
