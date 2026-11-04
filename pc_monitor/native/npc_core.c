/* PowerScope local switching simulator, schema npc-switching-r1.
 * SI units. Balanced stiff DC link, ideal NPC switching paths and antiparallel
 * diodes, floating-star LC/RL load; 1 us trapezoidal integration, 20 kHz dq PI.
 * Parameter/control provenance: laboratory Xu2026-repro sim/params_npc.m and
 * ctrl_npc.m, sampled-zoh-v4. This is an independent reduced circuit solver,
 * not the Simscape solver or a playback of the classifier's training windows.
 */
#include <math.h>
#include <stdlib.h>
#include <string.h>
#ifdef _WIN32
#define API __declspec(dllexport)
int DllMain(void *module, unsigned long reason, void *reserved) { return 1; }
#else
#define API
#endif
#define PI 3.14159265358979323846
#define DT 0.000001
#define ADC 0.00005

typedef struct {
    double x[3][3], F[3][3], G[3];
    double vdc, resistance, inductance, im, kp, ki;
    double xi[2], error[2], m[3], held[3], poles[3];
    double seconds, window_power;
    long long tick;
    int dim, fault, integrate, started, gates[12];
} Plant;

static double clamp(double v, double lo, double hi) {
    return v < lo ? lo : (v > hi ? hi : v);
}

static void transition(Plant *p) {
    double A[3][3]={{0}}, aug[3][6]={{0}}, B[3][3]={{0}};
    int i,j,k;
    p->dim = p->inductance > 0 ? 3 : 2;
    A[0][0]=-0.05/0.002; A[0][1]=-1/0.002;
    A[1][0]=1/0.00005;
    if (p->dim==2) A[1][1]=-1/(p->resistance*0.00005);
    else {
        A[1][2]=-1/0.00005;
        A[2][1]=1/p->inductance;
        A[2][2]=-p->resistance/p->inductance;
    }
    for(i=0;i<p->dim;i++) for(j=0;j<p->dim;j++) {
        aug[i][j]=(i==j ? 1.0 : 0.0)-DT*0.5*A[i][j];
        B[i][j]=(i==j ? 1.0 : 0.0)+DT*0.5*A[i][j];
        aug[i][j+p->dim]=(i==j ? 1.0 : 0.0);
    }
    for(k=0;k<p->dim;k++) {
        double pivot=aug[k][k];
        for(j=0;j<2*p->dim;j++) aug[k][j]/=pivot;
        for(i=0;i<p->dim;i++) if(i!=k) {
            double factor=aug[i][k];
            for(j=0;j<2*p->dim;j++) aug[i][j]-=factor*aug[k][j];
        }
    }
    for(i=0;i<p->dim;i++) {
        p->G[i]=aug[i][p->dim]*DT/0.002;
        for(j=0;j<p->dim;j++) {
            p->F[i][j]=0;
            for(k=0;k<p->dim;k++) p->F[i][j]+=aug[i][k+p->dim]*B[k][j];
        }
    }
}

API void *npc_create(double vdc, double resistance, double inductance) {
    Plant *p;
    double w=2*PI*50, yr, yi, zden, zr, zi;
    if(!(vdc>=20 && vdc<=800 && resistance>=2 && resistance<=200 &&
         inductance>=0 && inductance<=0.002)) return NULL;
    p=(Plant*)calloc(1,sizeof(Plant));
    if(!p) return NULL;
    p->vdc=vdc; p->resistance=resistance; p->inductance=inductance;
    zden=resistance*resistance+w*w*inductance*inductance;
    yr=resistance/zden; yi=w*0.00005-w*inductance/zden;
    zden=yr*yr+yi*yi;
    zr=0.05+yr/zden; zi=w*0.002-yi/zden;
    p->im=0.8*(vdc/2)/sqrt(zr*zr+zi*zi);
    p->kp=(2*PI*300*0.002)/(vdc/2);
    p->ki=(2*PI*300*0.05)/(vdc/2);
    transition(p);
    return p;
}

API void npc_destroy(void *handle) { free(handle); }

API int npc_fault(void *handle, int label) {
    Plant *p=(Plant*)handle; int k;
    if(!p || label<0 || label>21) return -1;
    if(label!=p->fault) for(k=0;k<3;k++) p->held[k]=p->x[k][0];
    p->fault=label; return 0;
}

static double measure(Plant *p, int k) {
    if(p->fault==13+k) return 0;
    if(p->fault==16+k) return p->held[k];
    if(p->fault==19+k) return p->x[k][0]+0.2*p->im;
    return p->x[k][0];
}

static void controller(Plant *p) {
    double c[3],s[3],id=0,iq=0,md,mq,raw;
    int k;
    if(p->started && p->integrate) for(k=0;k<2;k++) p->xi[k]+=p->error[k]*ADC;
    for(k=0;k<3;k++) {
        double theta=2*PI*50*p->seconds+(k==1 ? -2*PI/3 : k==2 ? 2*PI/3 : 0);
        c[k]=cos(theta); s[k]=sin(theta);
        id+=(2.0/3)*c[k]*measure(p,k); iq-=(2.0/3)*s[k]*measure(p,k);
    }
    p->error[0]=p->im-id; p->error[1]=-iq;
    md=p->kp*p->error[0]+p->ki*p->xi[0]+(2*2*PI*50*0.002/p->vdc)*iq;
    mq=p->kp*p->error[1]+p->ki*p->xi[1]-(2*2*PI*50*0.002/p->vdc)*id;
    p->integrate=fabs(md)<=1 && fabs(mq)<=1;
    for(k=0;k<3;k++) if(fabs(md*c[k]-mq*s[k])>1) p->integrate=0;
    md=clamp(md,-1,1); mq=clamp(mq,-1,1);
    for(k=0;k<3;k++) { raw=md*c[k]-mq*s[k]; p->m[k]=clamp(raw,-1,1); }
    p->started=1;
}

/* Current-directed ideal conduction paths include diodes of a disabled IGBT.
 * Four gate bits are ordered from DC+ to DC-. P=1100, O=0110, N=0011.
 */
API double npc_pole(int level, int broken, double current, double halfbus) {
    int g1=level==1, g2=level>=0, g3=level<=0, g4=level==-1;
    if(broken==1) g1=0; if(broken==2) g2=0;
    if(broken==3) g3=0; if(broken==4) g4=0;
    if(current>=0) return g1 && g2 ? halfbus : g2 ? 0 : -halfbus;
    return g3 && g4 ? -halfbus : g3 ? 0 : halfbus;
}

API int npc_advance(void *handle, float *measured, float *actual, float *voltage, int count) {
    Plant *p=(Plant*)handle; int n,sub,k,i,j;
    if(!p || !measured || !actual || !voltage || count<1 || count>20000) return -1;
    p->window_power=0;
    for(n=0;n<count;n++) {
        controller(p);
        for(sub=0;sub<50;sub++) {
            double carrier, mean=0, next[3][3]={{0}};
            int carrier_tick=(int)(p->tick%100);
            carrier=(carrier_tick<50 ? carrier_tick+0.5 : 99.5-carrier_tick)/50;
            for(k=0;k<3;k++) {
                int level=p->m[k]>=carrier ? 1 : p->m[k]<=carrier-1 ? -1 : 0;
                int broken=(p->fault>=1 && p->fault<=12 && (p->fault-1)/4==k) ? (p->fault-1)%4+1 : 0;
                p->gates[4*k]=level==1; p->gates[4*k+1]=level>=0;
                p->gates[4*k+2]=level<=0; p->gates[4*k+3]=level==-1;
                if(broken) p->gates[4*k+broken-1]=0;
                p->poles[k]=npc_pole(level,broken,p->x[k][0],p->vdc/2);
                mean+=p->poles[k]/3;
            }
            for(k=0;k<3;k++) for(i=0;i<p->dim;i++) {
                next[k][i]=p->G[i]*(p->poles[k]-mean);
                for(j=0;j<p->dim;j++) next[k][i]+=p->F[i][j]*p->x[k][j];
            }
            /* Enforce the floating-star zero-sequence constraint to roundoff. */
            for(i=0;i<p->dim;i++) {
                mean=(next[0][i]+next[1][i]+next[2][i])/3;
                for(k=0;k<3;k++) p->x[k][i]=next[k][i]-mean;
            }
            p->tick++; p->seconds=p->tick*DT;
        }
        for(k=0;k<3;k++) {
            if(!(fabs(p->x[k][0])<10000 && fabs(p->x[k][1])<10000)) return -2;
            measured[k*count+n]=(float)measure(p,k);
            actual[k*count+n]=(float)p->x[k][0];
            voltage[k*count+n]=(float)p->x[k][1];
            p->window_power+=p->x[k][1]*(p->dim==3 ? p->x[k][2] : p->x[k][1]/p->resistance)/count;
        }
    }
    return 0;
}

API int npc_info(void *handle, double *output) {
    Plant *p=(Plant*)handle; int k;
    if(!p || !output) return -1;
    output[0]=p->seconds; output[1]=p->im;
    for(k=0;k<3;k++) output[2+k]=p->m[k];
    for(k=0;k<12;k++) output[5+k]=p->gates[k];
    output[17]=p->window_power;
    return 0;
}
