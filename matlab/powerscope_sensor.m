function imeas = powerscope_sensor(iact, t, mode, ph, amp, tf)
% Original sensor equations with capture on each newly applied stuck fault.
% Repeated live fault/recovery commands retain the continuous plant state.
%#codegen
persistent held oldMode oldPh oldTf
if isempty(held)
    held=zeros(3,1); oldMode=0; oldPh=1; oldTf=1e9;
end
imeas=iact;
if t<tf || mode==0
    held=iact;
else
    if mode==2 && (oldMode~=2 || oldPh~=ph || oldTf~=tf)
        held=iact;
    end
    for k=1:3
        if k==ph
            if mode==1, imeas(k)=0;
            elseif mode==2, imeas(k)=held(k);
            elseif mode==3, imeas(k)=iact(k)+amp;
            end
        end
    end
end
oldMode=mode; oldPh=ph; oldTf=tf;
end
