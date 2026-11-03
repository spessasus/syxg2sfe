"""Batch measurements of the DLL: play notes, keep the register writes of the voice each note starts.

Job: dict(kind, ch, msb, lsb, program, key, vel, hold, rel, tag...). Jobs of one batch share one Wine run.
Element isolation: `mask` patches every voice record header so only element 1 (mask 1) or element 2
(mask 2, copied over element 1) plays. Drum direct-wave notes are unaffected by the mask.
"""
import os, json, struct, concurrent.futures as cf
from rom import *
from harness import reglog
from presets import voice_addr

XG_ON = [0xf0, 0x43, 0x10, 0x4c, 0x00, 0x00, 0x7e, 0x00, 0xf7]

def patched_bin(mask):
    b = bytearray(BIN)
    if mask is None: return bytes(b)
    for s in (13, 14):
        # Walk the voice records of both sections (80 or 158 bytes each). The tables of the MU50 / MU80 wave ROM
        # builds put the 8 character voice name in front of each record (the voice address points past it).
        name = name_prefix(s)
        o, end = SEC[s], SEC[s] + SIZE[s]
        while o + name + 80 <= end:
            o += name
            two = b[o + 1] & 2
            n = 158 if two else 80
            if two:
                # Element 2 plays from the element 1 slot (an empty element 1 crashes FUN_10017060)
                if mask == 2: b[o + 2:o + 2 + 0x4e] = b[o + 2 + 0x4e:o + 2 + 0x9c]
                b[o + 1] = b[o + 1] & ~2
            o += n
    return bytes(b)

def run_batch(jobs, mask=None, tag='m'):
    ev = [(0, XG_ON)]
    t = 40
    starts = []
    last = {}
    for j in jobs:
        ch = j['ch']
        if last.get(ch) != (j['msb'], j['lsb'], j['program']):
            ev += [(t, [0xb0 | ch, 0, j['msb']]), (t, [0xb0 | ch, 32, j['lsb']]), (t, [0xc0 | ch, j['program']])]
            last[ch] = (j['msb'], j['lsb'], j['program'])
            t += 2
        ev.append((t, [0x90 | ch, j['key'], j['vel']]))
        ev.append((t + j['hold'], [0x80 | ch, j['key'], 0]))
        starts.append(t)
        t += j['hold'] + j['rel']
    res = reglog(ev, blocks=t + 2, tag=tag, binpatch=patched_bin(mask))
    # Index writes by block
    byblock = {}
    for b, vo, reg, val in res: byblock.setdefault(b, []).append((vo, reg, val))
    out = []
    for j, s in zip(jobs, starts):
        mine = {vo for vo, reg, val in byblock.get(s, []) if reg == 0x640}
        w = []
        for b in range(s, s + j['hold'] + j['rel']):
            for vo, reg, val in byblock.get(b, []):
                if vo in mine and reg not in (0, 1, 8): w.append((b - s, vo, reg, val))
        out.append(w)
    return out

def doubled(w):
    """True when the note's voice gets two different values for a pitch or filter register in one block after
    the note on. The MU50 wave ROM build does that deep into a long batch (Dulcimer at voice 37: 0xf7bd, then
    0xc7bd every tick, and it plays the second one, 3 octaves down). A note in a fresh state plays right.
    In the note on block the EG start writes come first (0x400, 0x480, 0x500, 0x501, 0x680), any other register
    with two values there is the same fault (DrawOrgn: 0x440 127, then 34 with a wrong pitch)."""
    seen = {}
    for b, vo, reg, val in w:
        if b == 0 and reg in (0x400, 0x480, 0x500, 0x501, 0x680): continue
        if b > 0 and reg not in (0x400, 0x440, 0x680): continue
        if seen.setdefault((b, vo, reg), val) != val: return True
    return False

def run_all(jobs, mask=None, per=150, workers=6, name='m'):
    chunks = [jobs[i:i + per] for i in range(0, len(jobs), per)]
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = [ex.submit(run_batch, c, mask, '%s%d' % (name, i)) for i, c in enumerate(chunks)]
        res = []
        for f in futs: res += f.result()
        # Measure the notes hit by the doubled writes again in short batches, then alone
        for size in (16, 1):
            bad = [i for i, w in enumerate(res) if doubled(w)]
            if not bad: break
            print('   %d notes with doubled writes, again in batches of %d' % (len(bad), size), flush=True)
            parts = [bad[i:i + size] for i in range(0, len(bad), size)]
            futs = [ex.submit(run_batch, [jobs[i] for i in p], mask, '%sr%d_%d' % (name, size, n)) for n, p in enumerate(parts)]
            for p, f in zip(parts, futs):
                for i, w in zip(p, f.result()): res[i] = w
    return res
