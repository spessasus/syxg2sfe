"""Map the DLL filter (cutoff c, resonance r) to the spessasynth biquad cutoff (abs cents) and Q (cB)
by matching magnitude responses on a log frequency grid (50 Hz..16 kHz). Writes build/filter_map.json, pure
filter math (no DLL input), about 20 s at 8 processes."""
import numpy as np, json, os
from chip import damping
from paths import FILTER_MAP, JOBS
FS = 44100.0
F = np.geomspace(50, 16000, 160)
Z = np.exp(1j * 2 * np.pi * F / FS)

def dll_response(c, r):
    a = (min(c, 0x7ff) + 0x800) << 8
    f = (a & 0xffff | 0x10000) / (2 ** (24 - (a >> 16)) * 255.0)
    k = damping(r)
    # band_n = band_{n-1}(1 - f k) - f low_{n-1} + f x ; low_n = low_{n-1} + f band_n
    # => low = f^2 z^2 / (z^2 - (2 - f k - f^2) z + (1 - f k))
    num = f * f * Z * Z
    den = Z * Z - (2 - f * k - f * f) * Z + (1 - f * k)
    return 20 * np.log10(np.abs(num / den) + 1e-12)

def ss_response(cents, qcb):
    hz = min(8.176 * 2 ** (cents / 1200), FS * 0.45)
    res_gain = 10 ** ((qcb - 3.01) / 200)
    qgain = 1 / np.sqrt(10 ** (qcb / 200))
    w = 2 * np.pi * hz / FS
    cw, alpha = np.cos(w), np.sin(w) / (2 * res_gain)
    b1 = (1 - cw) * qgain; b0 = b2 = b1 / 2
    a0, a1, a2 = 1 + alpha, -2 * cw, 1 - alpha
    zi = 1 / Z
    h = (b0 + b1 * zi + b2 * zi * zi) / (a0 + a1 * zi + a2 * zi * zi)
    return 20 * np.log10(np.abs(h) + 1e-12)

CENTS = np.arange(1500, 13501, 10)
_SS = {}

def ss_bank(qcb):
    """Responses of every biquad cutoff for one Q (rows follow CENTS)."""
    if qcb not in _SS: _SS[qcb] = np.array([ss_response(c, qcb) for c in CENTS])
    return _SS[qcb]

def best_fast(c, r):
    d = dll_response(c, r)
    w = np.clip(10 ** (np.maximum(d, -60) / 40), 0.05, None)
    q0 = max(0, 200 * np.log10(1 / damping(r)) + 3.01)
    out = None
    for qcb in sorted({max(0, int(round(q0 / 5) * 5 + dq)) for dq in range(-40, 41, 5)}):
        S = ss_bank(qcb)
        off = np.median(d[None, :20] - S[:, :20], axis=1)
        e = np.sum(w[None, :] * (d[None, :] - S - off[:, None]) ** 2, axis=1)
        i = int(np.argmin(e))
        if out is None or e[i] < out[0]: out = (float(e[i]), int(CENTS[i]), qcb, float(off[i]))
    return out[1:]

def _row(r):
    return r, {c: best_fast(c, r) for c in list(range(0, 0x800, 16)) + [0x7c0, 0x7ff]}

if __name__ == '__main__':
    import concurrent.futures as cf
    with cf.ProcessPoolExecutor(JOBS) as ex:
        rows = dict(ex.map(_row, range(32)))
    table = {'%d,%d' % (c, r): v for r, row in rows.items() for c, v in row.items()}
    os.makedirs(os.path.dirname(FILTER_MAP), exist_ok=True)
    json.dump(table, open(FILTER_MAP, 'w'))
    print('%d entries -> %s' % (len(table), FILTER_MAP))
