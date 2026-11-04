"""Long notes at velocity 64 for every element whose cutoff or pitch moves on the vel 127 long note.

The DLL scales the filter EG depth by velocity around a fixed base cutoff. Comparing the two long notes gives
that base (convert.py). The same pair gives the velocity response of the pitch EG depth. Output: measure_vel.json in the build folder, {id: condensed long note}.
"""
import json, time
from measure import run_all
from paths import MEASURE, MEASURE_VEL
from collect import condense_long, LONG_HOLD, LONG_REL
from convert import pitch_cents

VEL = 64
PITCH_MOVES_CENTS = 25

def main():
    D = json.load(open(MEASURE))
    def moves(t):
        lg = D['results'][t['id']]['long'] or {}
        cents = [pitch_cents(v) for _, v in lg.get('0x680', [])]
        return len(lg.get('0x400', [])) > 2 or (len(cents) > 1 and max(cents) - min(cents) > PITCH_MOVES_CENTS)
    ts = [t for t in D['targets'] if moves(t)]
    print(len(ts), 'elements', flush=True)
    res = {}
    for mask in (1, 2):
        mt = [t for t in ts if t['mask'] == mask]
        if not mt: continue
        t0 = time.time()
        jobs = [dict(ch=t['ch'], msb=t['msb'], lsb=t['lsb'], program=t['program'], key=t['center'], vel=VEL,
                     hold=LONG_HOLD, rel=LONG_REL) for t in mt]
        out = run_all(jobs, mask=mask, per=40, name='v%d_' % mask)
        for t, w in zip(mt, out): res[t['id']] = condense_long(w)
        print('mask', mask, len(jobs), 'jobs', round(time.time() - t0), 's', flush=True)
    json.dump(res, open(MEASURE_VEL, 'w'))

if __name__ == '__main__':
    main()
