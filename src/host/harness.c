// Minimal VST2 host for the S-YXG50 plugin (32-bit Windows, runs under Wine). Plays MIDI events through the
// plugin and logs every write to the emulated sound chip's registers, which is what the converter measures.
//
// Usage: harness.exe <dll> <events.txt> <blocks> <reglog.bin> [<table.bin> <table resource>]
//
// events.txt: one event per line, "<block> <hex bytes...>". Short MIDI (1-3 bytes) or SysEx (F0 .. F7).
// blocks: how many 128-sample blocks to render at 44.1 kHz.
// reglog.bin: every chip register write (the plugin's FUN_10017340) as 3 int32: block, address, value.
// table.bin: replaces the voice table resource (named <table resource>, SXGBIN41.TBL in the original) in memory
//   before the plugin starts, same size. The converter uses it to make one element of a voice play alone.
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#define BLOCK 128
#define RATE 44100

typedef struct AEffect AEffect;
typedef intptr_t (__cdecl *AudioMasterCallback)(AEffect *, int32_t, int32_t, intptr_t, void *, float);
typedef intptr_t (__cdecl *DispatcherProc)(AEffect *, int32_t, int32_t, intptr_t, void *, float);
typedef void (__cdecl *ProcessProc)(AEffect *, float **, float **, int32_t);
typedef void (__cdecl *SetParameterProc)(AEffect *, int32_t, float);
typedef float (__cdecl *GetParameterProc)(AEffect *, int32_t);

struct AEffect {
    int32_t magic;
    DispatcherProc dispatcher;
    ProcessProc process;
    SetParameterProc setParameter;
    GetParameterProc getParameter;
    int32_t numPrograms, numParams, numInputs, numOutputs, flags;
    intptr_t resvd1, resvd2;
    int32_t initialDelay, realQualities, offQualities;
    float ioRatio;
    void *object, *user;
    int32_t uniqueID, version;
    ProcessProc processReplacing;
};

typedef struct {
    int32_t type, byteSize, deltaFrames, flags;
    int32_t dumpBytes;
    intptr_t resvd1;
    char *sysexDump;
    intptr_t resvd2;
} VstMidiSysexEvent;

typedef struct {
    int32_t type, byteSize, deltaFrames, flags;
    int32_t noteLength, noteOffset;
    char midiData[4];
    char detune, noteOffVelocity, reserved1, reserved2;
} VstMidiEvent;

typedef struct {
    int32_t numEvents;
    intptr_t reserved;
    void *events[512];
} VstEvents;

static int g_block;
static FILE *g_regLog;

static void __cdecl logReg(uint32_t addr, uint32_t value) {
    int32_t rec[3] = {g_block, (int32_t)addr, (int32_t)value};
    fwrite(rec, 4, 3, g_regLog);
}

uintptr_t g_regResume __asm__("_g_regResume"); // 0x10017345 relocated
uintptr_t g_logRegAddr __asm__("_g_logRegAddr");

// Replaces the first 5 bytes of FUN_10017340 (mov eax,[esp+4]; push ebx). Thiscall with 2 stack args.
__attribute__((naked)) static void regStub(void) {
    __asm__ volatile(
        "push %ecx\n\t"
        "push 12(%esp)\n\t"
        "push 12(%esp)\n\t"
        "call *_g_logRegAddr\n\t"
        "add $8, %esp\n\t"
        "pop %ecx\n\t"
        "mov 4(%esp), %eax\n\t"
        "push %ebx\n\t"
        "jmp *_g_regResume\n\t");
}

uintptr_t g_voiceTarget __asm__("_g_voiceTarget"); // 0x10018900 relocated

// The voice stage (0x10018680, thiscall, ecx = render object) runs the voice renderer on the chip object
// (ecx + 0xc) and nothing else.
__attribute__((naked)) static void voiceStub(void) {
    __asm__ volatile(
        "push %ecx\n\t"
        "mov 0xc(%ecx), %ecx\n\t"
        "call *_g_voiceTarget\n\t"
        "pop %ecx\n\t"
        "ret\n\t");
}

static intptr_t __cdecl hostCallback(AEffect *e, int32_t op, int32_t idx, intptr_t val, void *ptr, float opt) {
    (void)e; (void)idx; (void)val; (void)ptr; (void)opt;
    return op == 1 ? 2400 : 0; // AudioMasterVersion
}

static void patchJump(uint8_t *site, void *target) {
    DWORD old;
    VirtualProtect(site, 8, PAGE_EXECUTE_READWRITE, &old);
    site[0] = 0xe9;
    *(int32_t *)(site + 1) = (int32_t)((uintptr_t)target - ((uintptr_t)site + 5));
    VirtualProtect(site, 8, old, &old);
    FlushInstructionCache(GetCurrentProcess(), site, 8);
}

static int parseHex(const char *s, unsigned char *out, int max) {
    int n = 0;
    while (*s && n < max) {
        while (*s == ' ' || *s == '\t') s++;
        if (!*s || *s == '\n' || *s == '\r') break;
        unsigned v;
        if (sscanf(s, "%2x", &v) != 1) break;
        out[n++] = (unsigned char)v;
        s += 2;
    }
    return n;
}

// A crash in the plugin ends the process with exit code 3 and a line on stderr. Without this Wine starts its
// debugger (winedbg), which opens a window that has to be closed by hand.
static LONG WINAPI crashed(EXCEPTION_POINTERS *e) {
    fprintf(stderr, "harness: exception 0x%08lx at %p\n", e->ExceptionRecord->ExceptionCode,
            e->ExceptionRecord->ExceptionAddress);
    fflush(stderr);
    ExitProcess(3);
    return EXCEPTION_EXECUTE_HANDLER;
}

int main(int argc, char **argv) {
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    SetUnhandledExceptionFilter(crashed);
    if (argc != 5 && argc != 7) {
        fprintf(stderr, "usage: harness <dll> <events> <blocks> <reglog> [<table> <table resource>]\n");
        return 1;
    }
    int blocks = atoi(argv[3]);

    HMODULE mod = LoadLibraryA(argv[1]);
    if (!mod) { fprintf(stderr, "LoadLibrary failed %lu\n", GetLastError()); return 1; }
    uintptr_t base = (uintptr_t)mod;
    AEffect *(__cdecl *entry)(AudioMasterCallback) = (void *)GetProcAddress(mod, "main");
    if (!entry) { fprintf(stderr, "no main\n"); return 1; }

    g_voiceTarget = base + 0x18900;
    patchJump((uint8_t *)(base + 0x18680), voiceStub);
    g_regLog = fopen(argv[4], "wb");
    if (!g_regLog) { fprintf(stderr, "cannot open the register log\n"); return 1; }
    g_regResume = base + 0x17345;
    g_logRegAddr = (uintptr_t)logReg;
    patchJump((uint8_t *)(base + 0x17340), regStub);

    if (argc == 7) {
        HRSRC hr = FindResourceA(mod, argv[6], MAKEINTRESOURCEA(10));
        DWORD size = hr ? SizeofResource(mod, hr) : 0;
        uint8_t *res = hr ? LockResource(LoadResource(mod, hr)) : NULL;
        FILE *bf = fopen(argv[5], "rb");
        if (!res || !bf) { fprintf(stderr, "voice table patch failed\n"); return 1; }
        DWORD old;
        VirtualProtect(res, size, PAGE_READWRITE, &old);
        if (fread(res, 1, size, bf) != size) { fprintf(stderr, "voice table size mismatch\n"); return 1; }
        fclose(bf);
    }

    AEffect *fx = entry(hostCallback);
    if (!fx || fx->magic != 0x56737450) { fprintf(stderr, "bad effect\n"); return 1; }
    fx->dispatcher(fx, 0, 0, 0, NULL, 0);             // Open
    fx->dispatcher(fx, 10, 0, 0, NULL, (float)RATE);  // SetSampleRate
    fx->dispatcher(fx, 11, 0, BLOCK, NULL, 0);        // SetBlockSize
    fx->dispatcher(fx, 12, 0, 1, NULL, 0);            // MainsChanged on

    typedef struct { int block; int len; unsigned char data[512]; } Ev;
    Ev *evs = calloc(4096, sizeof(Ev));
    int evCount = 0;
    FILE *ef = fopen(argv[2], "r");
    if (!ef) { fprintf(stderr, "cannot open the events\n"); return 1; }
    char line[2048];
    while (fgets(line, sizeof line, ef) && evCount < 4096) {
        if (line[0] == '#' || line[0] == '\n') continue;
        char *p = line;
        evs[evCount].block = (int)strtol(p, &p, 10);
        evs[evCount].len = parseHex(p, evs[evCount].data, 512);
        if (evs[evCount].len > 0) evCount++;
    }
    fclose(ef);

    float l[BLOCK], r[BLOCK];
    float *outs[2] = {l, r};
    float *ins[2] = {l, r};
    VstEvents ve;
    VstMidiSysexEvent sx[512];
    VstMidiEvent me[512];
    int evIdx = 0;
    for (g_block = 0; g_block < blocks; g_block++) {
        ve.numEvents = 0;
        ve.reserved = 0;
        while (evIdx < evCount && evs[evIdx].block <= g_block && ve.numEvents < 512) {
            Ev *e = &evs[evIdx++];
            int n = ve.numEvents;
            if (e->data[0] == 0xf0) {
                memset(&sx[n], 0, sizeof sx[n]);
                sx[n].type = 6;
                sx[n].byteSize = sizeof sx[n];
                sx[n].dumpBytes = e->len;
                sx[n].sysexDump = (char *)e->data;
                ve.events[n] = &sx[n];
            } else {
                memset(&me[n], 0, sizeof me[n]);
                me[n].type = 1;
                me[n].byteSize = sizeof me[n];
                memcpy(me[n].midiData, e->data, e->len > 3 ? 3 : e->len);
                ve.events[n] = &me[n];
            }
            ve.numEvents++;
        }
        if (ve.numEvents) fx->dispatcher(fx, 25, 0, 0, &ve, 0); // ProcessEvents
        memset(l, 0, sizeof l);
        memset(r, 0, sizeof r);
        fx->processReplacing(fx, ins, outs, BLOCK);
    }
    fclose(g_regLog);
    fx->dispatcher(fx, 12, 0, 0, NULL, 0);
    fx->dispatcher(fx, 1, 0, 0, NULL, 0); // Close
    return 0;
}
