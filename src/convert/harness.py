"""Runs the plugin in the VST host (src/host/harness.c, build/harness.exe) under Wine and returns its chip
register writes."""
import os, subprocess
import numpy as np
from paths import DLL, DATA, ROOT, table_resource

EXE = os.path.join(ROOT, 'build', 'harness.exe')


def reglog(events, blocks, tag='r', binpatch=None):
    """Plays events [(block, MIDI bytes)] for `blocks` blocks of 128 samples, with the voice table replaced by
    binpatch when given. Returns [(block, voice, register, value)] for every chip register write."""
    d = os.path.join(DATA, 'harness')
    os.makedirs(d, exist_ok=True)
    ev, log = os.path.join(d, tag + '.txt'), os.path.join(d, tag + '.regs')
    with open(ev, 'w') as f:
        for b, data in events: f.write('%d %s\n' % (b, bytes(data).hex()))
    args = ['wine', EXE, DLL, ev, str(blocks), log]
    if binpatch is not None:
        bp = os.path.join(d, tag + '.bin')
        with open(bp, 'wb') as f: f.write(binpatch)
        args += [bp, table_resource()]
    r = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                       env=dict(os.environ, WINEDEBUG='-all'))
    if r.returncode:
        print(r.stdout, r.stderr)
        raise SystemExit('harness failed')
    res = []
    for b, a, v in np.fromfile(log, dtype='<i4').reshape(-1, 3).tolist():
        a &= 0xffffffff
        if a & 0x80000000: continue
        # Address = register + (voice >> 5) * 0x1000 + (voice & 31) * 2
        res.append((int(b), ((a >> 12) & 0xf) * 32 + ((a >> 1) & 0x1f), a & 0xfc1, int(v) & 0xffffffff))
    return res
