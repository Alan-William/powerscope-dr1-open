/* Original Simulink plant -> loopback UDP windows. No circuit solver or classifier.
 * Outputs are held fault controls; input signals are sampled in mdlUpdate.
 * Protocol PSL1: 96-byte LE header followed by measured/true/voltage float32
 * arrays, each phase-major 3 x 500. Commands are authenticated per session.
 */
#define S_FUNCTION_NAME powerscope_bridge
#define S_FUNCTION_LEVEL 2
#include <winsock2.h>
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
#include "simstruc.h"
#define TS 0.00005
#define COUNT 500
#define HEADER 96
#define PACKET (HEADER+4500*4)
typedef struct {
    SOCKET sock;
    unsigned char token[16], packet[PACKET];
    unsigned int seq, command, local_command, label, automatic, samples, warmed;
    unsigned int done, pace, stop_requested;
    long long warm_ticks;
    double duration, peak, wall_start, last_frame, applied, tf, power;
    LARGE_INTEGER frequency;
    float values[4500];
    int wsa_ready;
} Bridge;
static unsigned int u32(const unsigned char *p) { unsigned int v; memcpy(&v,p,4); return v; }
static double f64(const unsigned char *p) { double v; memcpy(&v,p,8); return v; }
static void put32(unsigned char *p,unsigned int v) { memcpy(p,&v,4); }
static void put64(unsigned char *p,double v) { memcpy(p,&v,8); }
static double clock_s(Bridge *b) { LARGE_INTEGER q; QueryPerformanceCounter(&q); return (double)q.QuadPart/b->frequency.QuadPart; }
static void emit(Bridge *b, const char *magic, double elapsed, double compute, double lag) {
    double now=clock_s(b);
    unsigned int size=strcmp(magic,"PSL1")==0 ? PACKET : HEADER;
    memset(b->packet,0,HEADER); memcpy(b->packet,magic,4); put32(b->packet+4,1);
    memcpy(b->packet+8,b->token,16); put32(b->packet+24,b->seq);
    put32(b->packet+28,b->label); put32(b->packet+32,b->command);
    put32(b->packet+36,b->automatic);
    put64(b->packet+40,elapsed); put64(b->packet+48,b->warmed ? now-b->wall_start : 0);
    put64(b->packet+56,compute); put64(b->packet+64,lag);
    put64(b->packet+72,b->peak); put64(b->packet+80,b->power/COUNT);
    put64(b->packet+88,b->applied);
    if(size==PACKET) memcpy(b->packet+HEADER,b->values,4500*4);
    send(b->sock,(const char*)b->packet,(int)size,0);
}
static void set_fault(Bridge *b,unsigned int label,double sim_t) {
    if(label==b->label) return;
    b->label=label; b->applied=sim_t;
    b->tf=label>=13 ? b->warm_ticks*TS+sim_t : 1e9;
}
static void commands(SimStruct *S,Bridge *b,double sim_t) {
    unsigned char msg[64]; int n, limit=0;
    while(limit++<32 && (n=recv(b->sock,(char*)msg,sizeof(msg),0))>0) {
        unsigned int id,op,label;
        if(n!=36 || memcmp(msg,"PSC1",4) || u32(msg+4)!=1 || memcmp(msg+8,b->token,16)) continue;
        id=u32(msg+24); op=u32(msg+28); label=u32(msg+32);
        if(id<=b->command || (op!=1 && op!=2) || label>21) continue;
        b->command=id;
        if(op==2) { b->stop_requested=1; ssSetStopRequested(S,1); }
        else { b->automatic=0; set_fault(b,label,sim_t); }
    }
}
static void mdlInitializeSizes(SimStruct *S) {
    int i;
    ssSetNumSFcnParams(S,0);
    if(ssGetNumSFcnParams(S)!=ssGetSFcnParamsCount(S)) return;
    ssSetNumContStates(S,0); ssSetNumDiscStates(S,0);
    if(!ssSetNumInputPorts(S,5)) return;
    for(i=0;i<4;i++) {
        ssSetInputPortWidth(S,i,3); ssSetInputPortDirectFeedThrough(S,i,0);
        ssSetInputPortRequiredContiguous(S,i,1);
    }
    ssSetInputPortWidth(S,4,2); ssSetInputPortDirectFeedThrough(S,4,0);
    ssSetInputPortRequiredContiguous(S,4,1);
    if(!ssSetNumOutputPorts(S,5)) return;
    ssSetOutputPortWidth(S,0,12);
    for(i=1;i<5;i++) ssSetOutputPortWidth(S,i,1);
    ssSetNumSampleTimes(S,1); ssSetNumPWork(S,1);
    ssSetNumRWork(S,0); ssSetNumIWork(S,0); ssSetNumModes(S,0); ssSetNumNonsampledZCs(S,0);
    ssSetOptions(S,SS_OPTION_EXCEPTION_FREE_CODE | SS_OPTION_CALL_TERMINATE_ON_EXIT);
}
static void mdlInitializeSampleTimes(SimStruct *S) {
    ssSetSampleTime(S,0,TS); ssSetOffsetTime(S,0,0);
}
#define MDL_START
static void mdlStart(SimStruct *S) {
    unsigned char cfg[60]; FILE *f; char path[1024]; DWORD path_length; Bridge *b;
    struct sockaddr_in address; WSADATA wsa; u_long nonblocking=1; int buffer_size=1048576;
    /* MATLAB and MinGW can use separate CRT environment caches. Read the
       process environment through Win32 after MATLAB setenv(). */
    path_length=GetEnvironmentVariableA("POWERSCOPE_SIMULINK_CONFIG",path,sizeof(path));
    f=path_length>0 && path_length<sizeof(path) ? fopen(path,"rb") : NULL;
    if(!f) { ssSetErrorStatus(S,"PowerScope session configuration is unavailable."); return; }
    if(fread(cfg,1,60,f)!=60) { fclose(f); ssSetErrorStatus(S,"Invalid PowerScope configuration size."); return; }
    fclose(f);
    if(memcmp(cfg,"PSCF",4) || u32(cfg+4)!=1 || u32(cfg+24)>65535 || u32(cfg+24)==0) {
        ssSetErrorStatus(S,"Invalid PowerScope configuration."); return;
    }
    b=(Bridge*)calloc(1,sizeof(Bridge));
    if(!b) { ssSetErrorStatus(S,"PowerScope allocation failed."); return; }
    ssSetPWorkValue(S,0,b); b->sock=INVALID_SOCKET;
    memcpy(b->token,cfg+8,16); b->automatic=u32(cfg+28);
    b->warm_ticks=(long long)(f64(cfg+32)/TS+.5); b->duration=f64(cfg+40);
    b->peak=f64(cfg+48); b->pace=u32(cfg+56); b->tf=1e9;
    if(b->warm_ticks<0 || b->duration<=0 || b->duration>600 || b->peak<=0) {
        ssSetErrorStatus(S,"PowerScope timing parameters are out of range."); return;
    }
    QueryPerformanceFrequency(&b->frequency);
    if(WSAStartup(MAKEWORD(2,2),&wsa)) { ssSetErrorStatus(S,"Winsock initialization failed."); return; }
    b->wsa_ready=1; b->sock=socket(AF_INET,SOCK_DGRAM,IPPROTO_UDP);
    memset(&address,0,sizeof(address)); address.sin_family=AF_INET;
    address.sin_addr.s_addr=htonl(INADDR_LOOPBACK);
    if(b->sock==INVALID_SOCKET || bind(b->sock,(struct sockaddr*)&address,sizeof(address))) {
        ssSetErrorStatus(S,"Cannot bind PowerScope loopback socket."); return;
    }
    address.sin_port=htons((unsigned short)u32(cfg+24));
    if(connect(b->sock,(struct sockaddr*)&address,sizeof(address))) { ssSetErrorStatus(S,"Cannot connect PowerScope socket."); return; }
    ioctlsocket(b->sock,FIONBIO,&nonblocking);
    setsockopt(b->sock,SOL_SOCKET,SO_SNDBUF,(const char*)&buffer_size,sizeof(buffer_size));
    emit(b,"PSW1",0,0,0);
}
static void mdlOutputs(SimStruct *S,int_T tid) {
    Bridge *b=ssGetPWork(S)?(Bridge*)ssGetPWorkValue(S,0):NULL; unsigned int label=b?b->label:0; int i;
    real_T *mask=ssGetOutputPortRealSignal(S,0);
    for(i=0;i<12;i++) mask[i]=(label==i+1)?0:1;
    *ssGetOutputPortRealSignal(S,1)=label>=13 ? (label-13)/3+1 : 0;
    *ssGetOutputPortRealSignal(S,2)=label>=13 ? (label-13)%3+1 : 1;
    *ssGetOutputPortRealSignal(S,3)=label>=19 && b ? .2*b->peak : 0;
    *ssGetOutputPortRealSignal(S,4)=b?b->tf:1e9;
}
#define MDL_UPDATE
static void mdlUpdate(SimStruct *S,int_T tid) {
    Bridge *b=ssGetPWork(S)?(Bridge*)ssGetPWorkValue(S,0):NULL; long long tick;
    const real_T *v,*current; int stream,phase; double elapsed,now,compute,remaining,lag;
    if(!b || b->sock==INVALID_SOCKET || b->stop_requested) return;
    tick=(long long)(ssGetT(S)/TS+.5);
    if(tick<=b->warm_ticks) {
        if(tick%500==0) { emit(b,"PSW1",0,0,0); commands(S,b,0); }
        if(tick==b->warm_ticks) {
            b->warmed=1; b->wall_start=b->last_frame=clock_s(b); emit(b,"PSR1",0,0,0);
        }
        return;
    }
    for(stream=0;stream<3;stream++) {
        v=(const real_T*)ssGetInputPortSignal(S,stream);
        for(phase=0;phase<3;phase++) {
            if(!(fabs(v[phase])<10000)) { ssSetErrorStatus(S,"Nonfinite or excessive Simulink output."); return; }
            b->values[stream*1500+phase*COUNT+b->samples]=(float)v[phase];
        }
    }
    v=(const real_T*)ssGetInputPortSignal(S,2); current=(const real_T*)ssGetInputPortSignal(S,3);
    for(phase=0;phase<3;phase++) b->power+=v[phase]*current[phase];
    b->samples++;
    if(b->samples<COUNT) return;
    b->seq++; elapsed=b->seq*.025; now=clock_s(b); compute=(now-b->last_frame)*1000;
    if(b->pace) {
        remaining=b->wall_start+elapsed-now;
        while(remaining>.0001) {
            Sleep(remaining>.002 ? (DWORD)(remaining*1000-1) : 0);
            remaining=b->wall_start+elapsed-clock_s(b);
        }
    }
    now=clock_s(b); lag=fmax(0,(now-b->wall_start-elapsed)*1000);
    emit(b,"PSL1",elapsed,compute,lag);
    b->samples=0; b->power=0; b->last_frame=clock_s(b);
    if(elapsed+1e-9>=b->duration) { b->done=1; ssSetStopRequested(S,1); return; }
    commands(S,b,elapsed+TS);
    v=(const real_T*)ssGetInputPortSignal(S,4);
    if(v[0]>b->local_command && v[1]>=0 && v[1]<=21 && floor(v[1])==v[1]) {
        b->local_command=(unsigned int)v[0]; b->automatic=0;
        set_fault(b,(unsigned int)v[1],elapsed+TS);
    }
    if(b->automatic) {
        unsigned int label=elapsed>=18?0:elapsed>=15?21:elapsed>=12?0:elapsed>=9?16:elapsed>=6?0:elapsed>=3?2:0;
        set_fault(b,label,elapsed+TS);
    }
}
static void mdlTerminate(SimStruct *S) {
    Bridge *b;
    if(!ssGetPWork(S)) return; /* Editor sizing queries have no runtime work area. */
    b=(Bridge*)ssGetPWorkValue(S,0);
    if(!b) return;
    if(b->sock!=INVALID_SOCKET) { emit(b,"PSE1",b->seq*.025,0,0); closesocket(b->sock); }
    if(b->wsa_ready) WSACleanup();
    free(b); ssSetPWorkValue(S,0,NULL);
}
#ifdef MATLAB_MEX_FILE
#include "simulink.c"
#else
#include "cg_sfun.h"
#endif
