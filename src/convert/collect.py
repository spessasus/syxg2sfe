"""Run all measurements and store condensed results in the build folder (measure.json).

Per element (melodic voice element or drum note):
- keys: every key in the element range, velocity 127, short note: note-on registers + release registers.
- vels: velocities 1..127 at the center key, short notes.
- long: center key, velocity 127, 2 s hold + 1 s release, full sequences of the time varying registers.
"""
import json, sys, time
from measure import run_all
from paths import MEASURE
from presets import melodic, KITS, drum_notes, voice_addr, elements

SHORT_HOLD, SHORT_REL = 4, 4
LONG_HOLD, LONG_REL = 690, 345
VELS = [1, 8, 16, 24, 32, 40, 48, 56, 64, 72, 80, 88, 96, 104, 112, 120, 127]
SEQ = (0x400, 0x440, 0x680, 0x4c0, 0x4c1, 0x500, 0x501, 0x5c0, 0x5c1, 0x441, 0x480, 0x481)

def condense_short(w, hold):
    """on: last value per register at the note-on block, pre: last value before note-off, off: release writes."""
    on, first, pre, off = {}, {}, {}, {}
    for b, vo, reg, val in w:
        if b == 0:
            on[reg] = val
            first.setdefault(reg, val)
        if b < hold: pre[reg] = val
        elif b == hold and reg in (0x500, 0x501, 0x4c0, 0x4c1): off[reg] = val
    h = lambda d: {hex(k): v for k, v in d.items()}
    # first: the first write at note on (EG start values before any 10 ms tick), only where it differs
    return {'on': h(on), 'first': h({k: v for k, v in first.items() if on.get(k) != v}),
            'pre': h({k: v for k, v in pre.items() if on.get(k) != v}), 'off': h(off)}

def condense_long(w):
    seq = {}
    for b, vo, reg, val in w:
        if reg in SEQ:
            s = seq.setdefault(hex(reg), [])
            if not s or s[-1][1] != val or reg in (0x500, 0x501): s.append((b, val))
    return seq

FIRST_REGS = (0x680, 0x400)

def element_targets():
    """(id, setup, key range, center key, mask) for every element to measure."""
    out = []
    for p in melodic():
        addr = voice_addr(p['voice'])
        els = elements(addr)
        for e, el in enumerate(els):
            lo, hi = el[1], el[2]
            vlo, vhi = el[3], el[4]
            out.append({'id': 'm:%d:%d:%d:%d' % (p['msb'], p['lsb'], p['program'], e), 'ch': 0, 'msb': p['msb'],
                        'lsb': p['lsb'], 'program': p['program'], 'keys': list(range(lo, hi + 1)),
                        'center': (lo + hi) // 2 if lo <= 60 <= hi else (lo + hi) // 2, 'vel': max(vlo, min(vhi, 127)),
                        'vels': [v for v in VELS if vlo <= v <= vhi], 'mask': (1 if e == 0 else 2) if len(els) == 2 else 1})
    for (msb, prog), kit in KITS.items():
        for key, (rec, voice) in drum_notes(kit).items():
            masks = [1]
            if voice is not None and len(elements(voice)) == 2: masks = [1, 2]
            for m in masks:
                out.append({'id': 'd:%d:%d:%d:%d' % (msb, prog, key, m - 1), 'ch': 9, 'msb': msb, 'lsb': 0, 'program': prog,
                            'keys': [key], 'center': key, 'vel': 127, 'vels': VELS, 'mask': m})
    return out

def main():
    targets = element_targets()
    print(len(targets), 'elements', flush=True)
    res = {t['id']: {'keys': {}, 'vels': {}, 'long': None} for t in targets}
    for mask in (1, 2):
        ts = [t for t in targets if t['mask'] == mask]
        if not ts: continue
        t0 = time.time()
        jobs, owners = [], []
        for t in ts:
            for k in t['keys']:
                jobs.append(dict(ch=t['ch'], msb=t['msb'], lsb=t['lsb'], program=t['program'], key=k, vel=t['vel'], hold=SHORT_HOLD, rel=SHORT_REL))
                owners.append((t['id'], 'keys', k))
            for v in t['vels']:
                jobs.append(dict(ch=t['ch'], msb=t['msb'], lsb=t['lsb'], program=t['program'], key=t['center'], vel=v, hold=SHORT_HOLD, rel=SHORT_REL))
                owners.append((t['id'], 'vels', v))
        out = run_all(jobs, mask=mask, per=1500, name='s%d_' % mask)
        for (tid, kind, x), w in zip(owners, out): res[tid][kind][x] = condense_short(w, SHORT_HOLD)
        print('mask', mask, 'short', len(jobs), 'jobs', round(time.time() - t0), 's', flush=True)
        t0 = time.time()
        jobs = [dict(ch=t['ch'], msb=t['msb'], lsb=t['lsb'], program=t['program'], key=t['center'], vel=t['vel'], hold=LONG_HOLD, rel=LONG_REL) for t in ts]
        out = run_all(jobs, mask=mask, per=40, name='l%d_' % mask)
        for t, w in zip(ts, out): res[t['id']]['long'] = condense_long(w)
        print('mask', mask, 'long', len(jobs), 'jobs', round(time.time() - t0), 's', flush=True)
    json.dump({'targets': targets, 'results': res}, open(MEASURE, 'w'))

if __name__ == '__main__':
    main()
