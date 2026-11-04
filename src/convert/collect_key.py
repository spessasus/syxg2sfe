"""Long notes at the low and high end of each key range for every element whose cutoff moves on the long note.

The DLL scales the filter EG with the key (FastResB closes at 0.2 s at key 63 and stays open at key 40). Comparing
these with the center key long note gives that key scaling. Output: measure_key.json in the build folder,
{id: {key: condensed long note}}.
"""
import json, time
from measure import run_all
from paths import MEASURE, MEASURE_KEY
from collect import condense_long, LONG_HOLD, LONG_REL

def main():
    D = json.load(open(MEASURE))
    ts = [t for t in D['targets'] if len((D['results'][t['id']]['long'] or {}).get('0x400', [])) > 2 and len(t['keys']) >= 12]
    print(len(ts), 'elements', flush=True)
    res = {t['id']: {} for t in ts}
    for mask in (1, 2):
        for end in (0, -1):
            mt = [t for t in ts if t['mask'] == mask]
            if not mt: continue
            t0 = time.time()
            jobs = [dict(ch=t['ch'], msb=t['msb'], lsb=t['lsb'], program=t['program'], key=t['keys'][end], vel=t['vel'],
                         hold=LONG_HOLD, rel=LONG_REL) for t in mt]
            out = run_all(jobs, mask=mask, per=40, name='k%d%d_' % (mask, end == 0))
            for t, w in zip(mt, out): res[t['id']][t['keys'][end]] = condense_long(w)
            print('mask', mask, 'end', end, len(jobs), 'jobs', round(time.time() - t0), 's', flush=True)
    json.dump(res, open(MEASURE_KEY, 'w'))

if __name__ == '__main__':
    main()
