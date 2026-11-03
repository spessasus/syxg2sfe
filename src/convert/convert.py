"""Turn the DLL measurements (measure.json in the build folder) into a bank description for the TS builder.

Output: bank.json in the build folder with samples (ROM references), instruments (one per measured element),
presets and the generator/modulator values. Units are SF2 units (cents, centibels, timecents).
"""
import json, math, os, sys
import numpy as np
from collections import defaultdict
from rom import *
from paths import MEASURE, MEASURE_VEL, BANK, FILTER_MAP, wave_base
from fegfit import fit as fit_filter_eg
from presets import melodic, KITS, KIT_NAMES, wave_records
from chip import *
from lfo import lfo_for, fit_halves, _curve
from functools import lru_cache

HERE = os.path.dirname(os.path.abspath(__file__))
WAVE_BASE = wave_base()
LEVEL_CB = 10 * OCT_DB / 16  # one 0x5c0 unit in cB (3.76)
PAN_STEP_DB = OCT_DB / 4     # one 0x5c1 nibble step (1.5 dB)

D = json.load(open(MEASURE))
TARGETS = {t['id']: t for t in D['targets']}
RES = D['results']

# ---------- samples ----------

# addr -> wave record (for root key / fine tune)
_REC_BY_ADDR = {}
for w in range(SIZE[15] // 2):
    for r in wave_records(w): _REC_BY_ADDR.setdefault(r['addr'], r)

samples = []
_sample_index = {}

def sample_for(on):
    """Sample entry for the note-on registers (pointer, A, B, format)."""
    addr = on['0x640'] - WAVE_BASE
    a, b, fmt = on['0x140'], on['0x6c0'], on['0x600']
    key = (addr, a, b, fmt & 0xc0)
    if key not in _sample_index:
        rec = _REC_BY_ADDR.get(addr)
        _sample_index[key] = len(samples)
        samples.append({'addr': addr, 'attack': a, 'loop': b, 'bits': 8 if fmt & 0x80 else 16,
                        'root': rec['root'] if rec else 60, 'name': 'W%06X' % addr + ('' if a == (rec or {}).get('attack', a) else '+%d' % a)})
    return _sample_index[key]

# ---------- conversions ----------

def pitch_cents(reg):
    """Playback cents relative to 44.1 kHz unity for pitch register 0x680."""
    oct_ = (reg >> 12) & 0xf
    if oct_ >= 8: oct_ -= 16
    inc = (reg & 0xfff) * 2.0 ** (oct_ + 4) / 32768.0
    return 1200 * math.log2(inc) if inc > 0 else -12000

def level_and_pan(on):
    """(attenuation cB, SF2 pan) from element level 0x5c0 and pan 0x5c1."""
    lv = on.get('0x5c0', 0xff)
    p = on.get('0x5c1', 0x22)
    nl, nr = p >> 4, p & 0xf
    gl = 0.0 if nl >= 15 else 10 ** (-nl * PAN_STEP_DB / 20)
    gr = 0.0 if nr >= 15 else 10 ** (-nr * PAN_STEP_DB / 20)
    g = math.hypot(gl, gr)
    if g == 0: return 1440.0, 0
    theta = math.atan2(gr, gl) / (math.pi / 2)  # 0 left .. 1 right
    return lv * LEVEL_CB - 200 * math.log10(g), round((theta * 2 - 1) * 500)

def filter_regs(on):
    v = on.get('0x400', 0)
    c, r = v & 0x7ff, (v >> 11) & 0x1f
    if r < 4: c = min(c, 0x7c0)
    return c, r

if not os.path.exists(FILTER_MAP): sys.exit('%s missing: run build.py (or filtermap.py)' % FILTER_MAP)
FMAP = json.load(open(FILTER_MAP))

def filter_match(c, r):
    """(biquad abs cents, Q cB, gain dB the biquad needs) matching the DLL filter (filtermap.py)."""
    v = FMAP.get('%d,%d' % (c, r))
    if v: return v
    # Grid every 16 cutoff units (plus 0x7c0 and 0x7ff): interpolate between the neighbours
    lo = (c // 16) * 16
    hi = lo + 16 if lo + 16 < 0x800 else 0x7ff
    a, b = FMAP['%d,%d' % (lo, r)], FMAP['%d,%d' % (hi, r)]
    t = (c - lo) / (hi - lo)
    return a[0] + (b[0] - a[0]) * t, (a[1] if t < 0.5 else b[1]), a[2] + (b[2] - a[2]) * t

def cutoff_cents(c, r=0):
    return filter_match(c, r)[0]

def q_cb(r, c=0x7c0):
    return filter_match(c, r)[1]

def amp_eg(on, off, long_seq=None, decay2=None, hold_extra=0.0):
    """SF2 volume envelope (timecents/cB) fitted to the DLL segments."""
    a_reg, a_lvl = on.get('0x4c0', 0x7f), on.get('0x4c1', 0)
    if a_reg & 0x80:
        attack, hold = attack_time(a_reg & 0x7f, a_lvl), 0.0
    else:
        attack, hold = 0.0, hold_time(a_reg & 0x7f)
    r1, l1 = on.get('0x500', 0x7f), on.get('0x501', 0)
    segs = [(decay_db_per_s(r1), level_db(l1))]
    if decay2: segs.append((decay_db_per_s(decay2[0]), level_db(decay2[1])))
    # A decay 1 that is over within 30 ms ahead of a slower decay 2 is only an onset click (Orgel 0/64/10:
    # 12.6 dB in 12 ms). One SF2 decay from 0 dB can't follow it and plays the whole note too loud, so its level
    # goes to the zone attenuation and the decay is fitted to the rest.
    offset, click = 0.0, 0.0
    if decay2 and len(segs) == 2 and segs[0][0] > 0 and segs[0][1] > 0.5 and segs[0][1] / segs[0][0] < 0.03 \
            and segs[1][1] > segs[0][1] + 0.5:
        # The level holds there until decay 2 starts (decay 1 time plus the firmware's poll delay)
        offset, click = segs[0][1], segs[0][1] / segs[0][0]
        segs = [(segs[1][0], segs[1][1] - offset)]
    decay, sustain, hold2 = fit_decay(segs, '0x500' in off)
    hold2 += click
    hold2 += hold_extra
    if '0x500' not in off:
        # No release (drums ignore note-off): keep the decay slope after note-off
        # A decay 2 at rate 0x7e+ is the stop at the end of a one-shot sample, the sample ends there anyway
        last = decay2[0] if decay2 and (decay2[0] & 0x7f) < 0x7e else r1
        release = 96.0 / max(decay_db_per_s(last), 1e-6)
        rate = None
    else:
        rate = off['0x500'] & 0x7f
    if rate is None:
        pass
    elif rate > 0x5f:
        # Linear fade: seconds to silence from the sustain level, mapped to a similar dB slope
        step = RELEASE_STEP[rate]
        t = (0xff000 - min(0xff000, int((sustain + offset * 10) / 10 / OCT_DB * 65536))) / step * BLOCK
        release = max(0.001, 4.3 * t)
    else:
        release = 96.0 / max(decay_db_per_s(rate, True), 1e-6)
    return {'attackVolEnv': seconds_to_timecents(attack), 'holdVolEnv': seconds_to_timecents(hold + hold2),
            'decayVolEnv': seconds_to_timecents(decay), 'sustainVolEnv': round(min(sustain, 1000)),
            'releaseVolEnv': seconds_to_timecents(release), 'levelOffset': offset * 10}

DECAY_FIT_SECONDS = 8.0

def fit_decay(segs, sustain=False):
    return _fit_decay(tuple(tuple(sg) for sg in segs), sustain)

@lru_cache(maxsize=None)
def _fit_decay(segs, allow_sustain):
    """Fit hold + one linear-dB decay to sustain to a chain of (dB/s, target dB) segments.

    Returns (decay seconds per 96 dB, sustain cB, extra hold seconds)."""
    # Segments that do not move the level (decay 1 to 0 dB) act as holds, handled by the caller
    segs = [sg for i, sg in enumerate(segs) if abs(sg[1] - (segs[i - 1][1] if i else 0.0)) > 0.01] or segs[-1:]
    # Build the DLL curve (dB over time) until the last target or -96 dB
    pts, t, db = [(0.0, 0.0)], 0.0, 0.0
    for rate, target in segs:
        target = min(target, 96.0)
        if rate <= 0: break
        dt = abs(target - db) / rate
        t += dt
        db = target
        pts.append((t, db))
    final = pts[-1][1]
    if final >= 90: final = 100.0  # silence
    if len(segs) == 1 or abs(segs[0][1] - segs[-1][1]) < 0.5:
        rate = segs[0][0]
        return (96.0 / rate if rate > 0 else 100.0), min(final, 100) * 10, 0.0
    # Two segments: least squares in dB weighted by gain, over the curve until -60 dB (or its end)
    def dll(tt):
        for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
            if tt <= t1: return d0 + (d1 - d0) * (tt - t0) / max(t1 - t0, 1e-9)
        return pts[-1][1]
    tend = pts[-1][0]
    for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
        if d1 >= 60 and d0 < 60:
            tend = t0 + (60 - d0) / max(d1 - d0, 1e-9) * (t1 - t0); break
    import numpy as np
    lo, hi = min(sg[0] for sg in segs), max(sg[0] for sg in segs)
    rates = lo * 2 ** (np.arange(-12, int(12 * math.log2(hi / lo)) + 13) / 12) if lo > 0 else np.array([hi])

    def search(tend, sustains):
        """Best (error, rate, hold, sustain) of clip((t - hold) * rate, 0, sustain) on [0, tend], gain weighted."""
        ts = np.linspace(0, tend, 201)
        target = np.interp(ts, [p[0] for p in pts], [p[1] for p in pts])
        g = 10 ** (-target / 20)
        holds = np.linspace(0, tend, 41)
        m = np.clip((ts[None, None, None, :] - holds[None, :, None, None]) * rates[None, None, :, None], 0,
                    sustains[:, None, None, None])
        err = (g * (m - target) ** 2).sum(axis=3)
        si, hi_, ri = np.unravel_index(np.argmin(err), err.shape)
        return float(err[si, hi_, ri]), float(rates[ri]), float(holds[hi_]), float(sustains[si])
    _, rate, hold, sus = search(tend, np.array([final]))
    # A fast decay 1 then a slow decay 2 (FunkGtr2 0/43/28 element 2: -17 dB in 150 ms, then about 3 dB/s) fits
    # decay 1 to a sustain level much better than one line to silence (that was 11 dB too quiet at the start,
    # 3 dB loud later). Judged over the first 8 s and only when it halves the error: a steady piano decay
    # (GrandPno 7..8 dB/s) does not reach its end level in that time, any sustain below that ties, and a held note
    # would stop decaying there. Over 4 s BritePno's low keys (decay 2 at 2.7 dB/s) took a sustain and held notes
    # in Storms Over Jupiter ch 7 got worse. Not for elements that ignore note-off (drums), SF2 would hold the
    # sustain until the note-off.
    if allow_sustain and tend > DECAY_FIT_SECONDS:
        t4 = DECAY_FIT_SECONDS
        ts = np.linspace(0, t4, 201)
        target = np.interp(ts, [p[0] for p in pts], [p[1] for p in pts])
        g = 10 ** (-target / 20)
        e_line = float((g * (np.clip((ts - hold) * rate, 0, final) - target) ** 2).sum())
        e_sus, r2, h2, s2 = search(t4, np.linspace(1, min(final, 96), 24))
        if e_sus < 0.5 * e_line and s2 < final - 3: rate, hold, sus = r2, h2, s2
    return 96.0 / rate, min(sus, 100) * 10, hold

# spessasynth_core modulator curves (unipolar, 0..1)
VEL_CURVES = {
    'concave': lambda x: 0.0 if x <= 0 else (1.0 if x >= 1 else min(1.0, -400 / 960 * math.log10(1 - x))),
    'linear': lambda x: x,
    'convex': lambda x: 0.0 if x <= 0 else (1.0 if x >= 1 else 1 - min(1.0, -400 / 960 * math.log10(x))),
}

def vel_x(curve, v):
    """Negative velocity modulator value relative to velocity 127. spessasynth_core reads velocity v as v / 128,
    so velocity 127 still gives a bit of the curve (12 % for convex)."""
    f = VEL_CURVES[curve]
    return f(1 - v / 128) - f(1 / 128)

def vel_at127(fit, ref=127):
    """What a velocity modulator still adds at velocity 127 (or at ref, the top velocity of an element with a
    velocity range), in its destination's units."""
    return VEL_CURVES[fit[0]](1 - ref / 128) * fit[1] if fit else 0.0

def fit_velocity(points, ref=127):
    """points: [(velocity, extra attenuation cB relative to velocity ref)] -> (curve, amount) for a negative
    unipolar velocity modulator."""
    best = None
    for name in VEL_CURVES:
        xs = [vel_x(name, v) - vel_x(name, ref) for v, _ in points]
        den = sum(x * x for x in xs)
        amt = sum(x * y for x, (_, y) in zip(xs, points)) / den if den else 0
        err = sum((amt * x - y) ** 2 for x, (_, y) in zip(xs, points))
        if best is None or err < best[0]: best = (err, name, amt)
    return best[1], round(best[2])

def fit_velocity_pair(points, ref=127):
    """fit_velocity, or two modulators with different curves where that fits clearly better: (fit, second fit or
    None). E.Piano1 0/0/4 element 1 lies between linear and concave, and the biquad gain correction drops 3 dB
    from velocity 112 to 127 (the cutoff reaches the ceiling): linear alone was 2.8 dB too quiet at 48..72."""
    one = fit_velocity(points, ref)
    y = np.array([y for _, y in points], dtype=float)
    col = lambda name: np.array([vel_x(name, v) - vel_x(name, ref) for v, _ in points])
    rms1 = float(np.sqrt(np.mean((one[1] * col(one[0]) - y) ** 2)))
    best = None
    for a, b in (('concave', 'linear'), ('concave', 'convex'), ('linear', 'convex')):
        A = np.stack([col(a), col(b)], axis=1)
        c, *_ = np.linalg.lstsq(A, y, rcond=None)
        rms = float(np.sqrt(np.mean((A @ c - y) ** 2)))
        if best is None or rms < best[0]: best = (rms, (a, round(c[0])), (b, round(c[1])))
    if best[0] < 0.7 * rms1 and rms1 - best[0] > 3: return best[1], best[2]
    return one, None

def fit_velocity_offset(points, ref=127):
    """Like fit_velocity, plus an offset (the value at velocity ref): (curve, amount, offset). No convex: it is
    steep near 127, so the offset ran away (E.Grand 0/0/2 to a 35950 cent cutoff, past the 16 bit generator)."""
    best = None
    for name in ('linear', 'concave'):
        A = np.array([[1.0, vel_x(name, v) - vel_x(name, ref)] for v, _ in points])
        y = np.array([y for _, y in points], dtype=float)
        (off, amt), *_ = np.linalg.lstsq(A, y, rcond=None)
        err = float(((A @ [off, amt] - y) ** 2).sum())
        if best is None or err < best[0]: best = (err, name, amt, off)
    return best[1], round(best[2]), float(best[3])

def linfit(xs, ys):
    n = len(xs)
    if n < 2: return 0.0, (ys[0] if ys else 0.0)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
    return b, my - b * mx

# ---------- per element ----------

def decay2_regs(tid):
    """(block, rate, level) of the decay 2 write in the long note, else None."""
    lg = RES[tid]['long'] or {}
    later = [(b, v) for b, v in lg.get('0x500', []) if 0 < b < 690]
    later1 = [(b, v) for b, v in lg.get('0x501', []) if 0 < b < 690]
    if later and later1: return later[0][0], later[0][1], later1[0][1]
    return None

def segment_start(on):
    """Seconds from note on until decay 1 starts (attack or hold)."""
    a_reg, a_lvl = on.get('0x4c0', 0x7f), on.get('0x4c1', 0)
    return attack_time(a_reg & 0x7f, a_lvl) if a_reg & 0x80 else hold_time(a_reg & 0x7f)

from presets import voice_addr, elements, drum_notes

def element_bytes(tid):
    """The 0x4e element bytes behind a target, or None for direct-wave drum notes."""
    t = TARGETS[tid]
    parts = tid.split(':')
    e = int(parts[4])
    if parts[0] == 'm':
        from presets import melodic as _m
        voice = next(p['voice'] for p in MELODIC if (p['msb'], p['lsb'], p['program']) == (t['msb'], t['lsb'], t['program']))
        return elements(voice_addr(voice))[e]
    kit = KITS[(int(parts[1]), int(parts[2]))]
    rec, voice = drum_notes(kit)[int(parts[3])]
    return None if voice is None else elements(voice)[e]

MELODIC = melodic()
# Long notes at a lower velocity (collect_vel.py) for the filter EG velocity response
_VEL_PATH = MEASURE_VEL
VEL_LONG = json.load(open(_VEL_PATH)) if os.path.exists(_VEL_PATH) else {}
VEL_LONG_VELOCITY = 64

def decay2_for(el, on, d2_long):
    if el is None: return d2_long
    r1 = on.get('0x500', 0x7f)
    rate = max(0, min(0x7f, r1 + 2 * (el[0x47] - el[0x46])))
    lvl = min(0xfe, 2 * (127 - el[0x4a]))
    return rate, lvl

def _delta(on, off, cref):
    """Key offset of the EG rates against the reference key, in rate units."""
    a, b = on.get('0x500', 0) & 0x7f, cref['on'].get('0x500', 0) & 0x7f
    if 2 < a < 0x7e and 2 < b < 0x7e: return a - b
    a, b = off.get('0x500'), cref['off'].get('0x500')
    if a is not None and b is not None and 2 < (a & 0x7f) < 0x7e and 2 < (b & 0x7f) < 0x7e: return (a & 0x7f) - (b & 0x7f)
    return 0

def z_d2(on, off, d2, el, cref):
    if d2 is None: return decay2_for(el, on, None) if el is not None else None
    return max(0, min(0x7f, d2[1] + _delta(on, off, cref))), d2[2]

def z_hold(d2, cref):
    """Extra wait before decay 2 that the rates do not explain (decay 1 to 0 dB acts as a hold)."""
    if d2 is None: return 0.0
    on = cref['on']
    r1, l1 = on.get('0x500', 0x7f), on.get('0x501', 0)
    expected = segment_start(on) + level_db(l1) / max(decay_db_per_s(r1), 1e-6)
    return max(0.0, d2[0] * BLOCK - expected)

CC74_VEL_CURVES = ('linear', 'convex', 'concave')
CAP, RIDGE = 3000, 1e-3

def fit_cc74_velocity(vels):
    """CC74 cutoff response over velocity. vels: ((velocity, c, r), ..) register cutoff and resonance per velocity.
    The DLL adds CC74 (16 units per step) and velocity in register units and clamps at the ceiling, the map to cents
    is steeper near the top. So a soft note gets much more out of CC74 than a loud one (GrandPno key 60 with CC74 95
    and CC71 85: +3136 cents at velocity 42, +980 at 100). One curve per half, fitted at one velocity, played the
    soft notes 15..40 dB too dark above 4 kHz. Fitted here over CC74 x velocity: the gated curves per half plus odd
    CC74 curves with 1 - velocity / 128 as the second source (no gate, a modulator has two sources)."""
    ccs = np.arange(128)
    x = (ccs - 64) / 64
    odd = {'linear': x}
    for name in ('convex', 'concave'):
        odd[name] = np.array([math.copysign(_curve(name, abs(v)), v) for v in x])
    up, down = (x > 0).astype(float), (x < 0).astype(float)
    rows, ys, bases, ws = [], [], [], []
    # Small offsets from 64 are the common case, they count more (as in fit_halves)
    wcc = 1 + 3 * np.exp(-np.abs(ccs - 64) / 8)
    for vel, c, r in vels:
        top = 0x7ff if r >= 4 else 0x7c0
        c = min(c, top)
        b = filter_match(c, r)[0]
        ys.append([filter_match(max(0, min(top, c + (cc - 64) * 16)), r)[0] - b for cc in ccs])
        bases.append(np.full(128, b))
        u = 1 - vel / 128
        cols = [up, up * odd['linear'], up * odd['convex'], up * odd['concave'],
                down, down * odd['linear'], down * odd['convex'], down * odd['concave']]
        for vc in CC74_VEL_CURVES:
            uc = _curve(vc, u)
            cols += [uc * up, uc * down, uc * odd['linear'], uc * odd['convex'], uc * odd['concave']]
        rows.append(np.stack(cols, axis=1))
        ws.append(wcc)
    A, y, base, w0 = np.concatenate(rows), np.concatenate(ys), np.concatenate(bases), np.concatenate(ws)
    names = [('up', 'step'), ('up', 'linear'), ('up', 'convex'), ('up', 'concave'),
             ('down', 'step'), ('down', 'linear'), ('down', 'convex'), ('down', 'concave'),
             ] + [('vel', (p, vc)) for vc in CC74_VEL_CURVES for p in ('stepUp', 'stepDown', 'linear', 'convex', 'concave')]
    # The synth clamps the cutoff at 13500 like the map's open filter, so a target at 13500 only needs the sum to
    # get there (dropped once the fit is above it). Above 12000 cents (10 kHz) an error is hard to hear, those
    # points count less. A little ridge: convex, concave and linear are close, unregularised they cancel in big
    # amounts.
    # Overshooting an open target is free up to 600 cents only: CC71 brings the ceiling down (GrandPno at resonance
    # 31 plays 11870 at CC74 127), and the CC71 term cannot pull back a curve that went 9800 cents over.
    pred = np.zeros_like(y)
    is_open = base + y >= 13490
    for _ in range(12):
        hi = np.minimum(base + y, base + pred)
        w = w0 * np.clip((13000 - hi) / 1000, 0.15, 1)
        t = y.copy()
        over = is_open & (pred > y + CAP)
        t[over] = y[over] + CAP
        w[over] = w0[over] * 0.5
        w[is_open & (pred >= y) & ~over] = 0
        Aw = A * w[:, None]
        lam = RIDGE * (w ** 2).sum() / len(names)
        coef = np.linalg.solve(Aw.T @ Aw + lam * np.eye(len(names)), Aw.T @ (t * w))
        pred = A @ coef
    out = {'up': [], 'down': [], 'vel': []}
    for (side, curve), v in zip(names, np.round(coef)):
        if abs(v) >= 5: out[side].append(list(curve) + [int(v)] if side == 'vel' else [curve, int(v)])
    return out

@lru_cache(maxsize=None)
def brightness_fits(c, r, vels=None):
    """CC74 and CC71 cutoff response at register cutoff c, resonance r.
    CC74 moves c 16 units per step, clamped at 0x7ff (0x7c0 below resonance 4). In SF2 cents that is not a
    line (ceiling a few steps away, the map bends at the bottom), so it is fitted as curves per half, over
    velocity when vels (velocity, c, r) is given (fit_cc74_velocity), else at c.
    CC71 sets the resonance, and the same c plays lower at a higher one, so it gets a cutoff fit too. CC74 maps
    differently at the resonance CC71 sets: that goes on CC74 curves with CC71 as the second source."""
    base = filter_match(c, r)
    top = 0x7ff if r >= 4 else 0x7c0
    m74 = lambda cc, rr, t: filter_match(max(0, min(t, c + (cc - 64) * 16)), rr)
    fc = fit_halves(lambda cc: m74(cc, r, top)[0], base[0], 20)
    if vels:
        fc = fit_cc74_velocity(vels)
    att = fit_halves(lambda cc: -m74(cc, r, top)[2] * 10, -base[2] * 10, 5)
    r71 = lambda v: max(r, (v - 64) // 2) if v > 64 else max(0, r - (64 - v) // 2)
    cc71 = fit_halves(lambda v: filter_match(min(c, 0x7ff if r71(v) >= 4 else 0x7c0), r71(v))[0], base[0], 20)
    # CC74 x CC71: how CC74 maps at the resonance CC71 sets, minus how it maps at the element's own. Fitted over
    # both controllers as CC74 curves with CC71 as the second source, a switch (full from 65 up) plus bipolar
    # linear and convex ones. With the switch alone it played the resonance 31 difference at CC71 85 already
    # (GrandPno 1200 cents darker at velocity 42, the DLL at resonance 10 about 650).
    x = (np.arange(128) - 64) / 64
    prim = {'stepUp': (x > 0).astype(float), 'stepDown': (x < 0).astype(float), 'linear': x}
    for name in ('convex', 'concave'):
        prim[name] = np.array([math.copysign(_curve(name, abs(v)), v) for v in x])
    own = np.array([m74(cc, r, top)[0] - base[0] for cc in range(128)])
    cc71s = list(range(0, 128, 4)) + [127]
    sec = {'switch': lambda v: float(v > 64), 'linear': lambda v: (v - 64) / 64,
           'convex': lambda v: math.copysign(_curve('convex', abs(v - 64) / 64), v - 64)}
    rows, ys = [], []
    for v in cc71s:
        rv = r71(v)
        tv = 0x7ff if rv >= 4 else 0x7c0
        bv = filter_match(min(c, tv), rv)[0]
        ys.append(np.array([filter_match(max(0, min(tv, c + (cc - 64) * 16)), rv)[0] - bv for cc in range(128)]) - own)
        rows.append(np.stack([prim[p] * sec[q](v) for q in sec for p in prim], axis=1))
    A, y = np.concatenate(rows), np.concatenate(ys)
    names = [(p, q) for q in sec for p in prim]
    lam = 1e-3 * len(y) / len(names)
    coef = np.round(np.linalg.solve(A.T @ A + lam * np.eye(len(names)), A.T @ y))
    cross71 = [[p, q, int(v)] for (p, q), v in zip(names, coef) if abs(v) >= 10] if np.abs(y).max() >= 20 else []
    return {'fc': fc, 'att': att, 'cross71': cross71, 'cc71': cc71}

def element_zones(tid):
    """Zones (dicts) for one element: contiguous keys sharing the sample."""
    r = RES[tid]
    keys = sorted((int(k), v) for k, v in r['keys'].items() if '0x640' in v['on'])
    if not keys: return [], None
    # Global (per element) velocity response from the center key
    vels = r['vels']
    vel_mod = vel_mod2 = None
    vfc_mod = None
    vfc_offset = 0.0
    vq_mod = None
    # The velocity fits are relative to velocity 127, or to the element's top velocity when its range ends lower
    # (the low element of a velocity switch, VelGtHrm 0/43/24 1..120). Those had no velocity fits at all, and the
    # keys are measured at that top velocity (collect.py), so the zones hold its values.
    vref = max((int(v) for v, x in vels.items() if x.get('on')), default=127)
    rk = str(vref)
    if rk in vels and '0x5c0' in vels[rk]['on']:
        base_att, _ = level_and_pan(vels[rk]['on'])
        pts = [(int(v), level_and_pan(x['on'])[0] - base_att) for v, x in vels.items() if '0x5c0' in x['on']]
        vel_mod = fit_velocity(pts, vref)
        # The first cutoff write at note on is the filter EG start, like in the zones
        start = lambda x: filter_regs({**x['on'], **{k: v for k, v in x.get('first', {}).items() if k == '0x400'}})
        f127 = filter_match(*start(vels[rk]))
        fpts, qpts = [], []
        for v, x in vels.items():
            fm = filter_match(*start(x))
            fpts.append((int(v), f127[0] - fm[0]))
            qpts.append((int(v), f127[1] - fm[1]))
            # The biquad gain compensation follows the velocity too
            pts = [(vv, a - (fm[2] - f127[2]) * 10 if vv == int(v) else a) for vv, a in pts]
        vel_mod, vel_mod2 = fit_velocity_pair(pts, vref)
        # Velocities whose cutoff sits on the ceiling (GrandPno 0/0/0 from 112 up: 1984, the map's open filter at
        # 13500) do not show how far velocity really moves it. Through that point at 127 the line came out 850
        # cents too bright at velocity 64. So the fit leaves them out and gets an offset, which moves the zone
        # cutoff to where the unclamped velocity 127 would be (the synth caps it at 13500 like the DLL's ceiling).
        ceil = lambda x: start(x)[0] >= (0x7ff if start(x)[1] >= 4 else 0x7c0)
        free = [(int(v), y) for (v, x), (_, y) in zip(vels.items(), fpts) if not ceil(x)]
        # Only where the filter EG does not scale with velocity (velocity_shift_only) or there is none: the joint
        # EG depth fit measures the velocity 64 note against the clamped velocity 127 start with this shift
        # (NylonEP 0/40/99 went 20 dB dark with it)
        lng = (r['long'] or {}).get('0x400', [])
        moves = len(lng) >= 2 and len({v for _, v in lng}) > 1
        if ceil(vels[rk]) and len(free) >= 4 and len(free) < len(fpts) and \
                (not moves or velocity_shift_only(lng, (VEL_LONG.get(tid) or {}).get('0x400'))):
            name, amt, off = fit_velocity_offset(free, vref)
            vfc_mod = (name, amt)
            vfc_offset = -off
        elif any(abs(y) > 20 for _, y in fpts): vfc_mod = fit_velocity(fpts, vref)
        if any(abs(y) > 5 for _, y in qpts): vq_mod = fit_velocity(qpts, vref)
    d2 = decay2_regs(tid)
    el = element_bytes(tid)
    # Reference: the short note at the long note's key
    ck = str(TARGETS[tid]['center'])
    cref = r['keys'].get(ck) or keys[len(keys) // 2][1]
    # Short notes give the pitch 3 blocks in (pre), past any fast pitch EG start. Some voices start low only at
    # some keys, so the start (first write) is no reference. A slow pitch EG still moves after that: the long
    # note's offset from block 3 to its steady pitch (median of the second half, vibrato averages out).
    # The firmware ticks are not locked to the blocks, so the same pitch EG step can land one block earlier or
    # later in the short note than in the long one (Trombone 0/0/57: start, -210, +72 cents, 71 cents flat when
    # matched by block). key_pitch() lines the short note's writes up with the long note's.
    peg_offset = 0.0
    ps = [(b, v) for b, v in (r['long'] or {}).get('0x680', []) if b < 690]
    L = []
    if ps:
        early = [v for b, v in ps if b < 4][-1] if ps[0][0] < 4 else ps[0][1]
        steady = [pitch_cents(v) for b, v in ps if b >= 345] or [pitch_cents(ps[-1][1])]
        peg_offset = float(np.median(steady)) - pitch_cents(early)
        L = [pitch_cents(v) for b, v in ps if b < 60]
        L_steady = float(np.median(steady))

    def key_pitch(v):
        last = pitch_cents(v.get('pre', {}).get('0x680', v['on']['0x680']))
        S = [pitch_cents(x['0x680']) for x in (v['first'], v['on'], v['pre']) if '0x680' in x]
        S = [c for i, c in enumerate(S) if i == 0 or abs(c - S[i - 1]) > 0.01]
        # Both notes start with the EG start write, so the short note's writes match the long note's by index.
        # Keys whose start differs (Square Lead starts low at some keys only) fail the step check and keep the
        # block based offset.
        if len(S) >= 2 and len(L) >= len(S) and np.abs(np.diff(L[:len(S)]) - np.diff(S)).sum() < 30:
            return last + L_steady - L[len(S) - 1]
        return last + peg_offset
    # The chip clamps the pitch at the ends of the key range (same pitch on neighbouring keys). Those keys get
    # their own zone (scale tuning 0), the slope of the others must not include them.
    kc = {k: key_pitch(v) for k, v in keys}
    # A jump a few blocks in (Gt.Pinch 0/43/29 element 2: two octaves up at block 3, ChrchBel 0/96/14 at block 6)
    # lands past the short note's 4 blocks at most keys, depending on the tick phase. Those keys only saw the start
    # write. Take the start to steady offset from the nearest key on the same sample whose short note did get a
    # later write: the long note's step there (Gt.Pinch) or none (Square Lead 0/0/80 starts low above key 60 only).
    if len(L) >= 2 and abs(L[0] - L_steady) > 30:
        seen = {k: kc[k] - pitch_cents(v['on']['0x680']) for k, v in keys
                if '0x680' in v.get('pre', {}) or '0x680' in v.get('first', {})}
        smp_of = {k: sample_for(v['on']) for k, v in keys}
        for k, v in keys:
            if k in seen: continue
            near = [n for n in seen if smp_of[n] == smp_of[k]]
            if not near: continue
            d = seen[min(near, key=lambda n: abs(n - k))]
            last = pitch_cents(v['on']['0x680'])
            kc[k] = last + min((peg_offset, L_steady - L[0]), key=lambda o: abs(o - d))
    flat ={k for k in kc if any(abs(kc.get(n, 1e9) - kc[k]) < 1 for n in (k - 1, k + 1))}
    groups = []
    for k, v in keys:
        s = sample_for(v['on'])
        if groups and groups[-1]['sample'] == s and groups[-1]['keys'][-1][0] == k - 1 \
                and (groups[-1]['keys'][-1][0] in flat) == (k in flat):
            groups[-1]['keys'].append((k, v))
        else:
            groups.append({'sample': s, 'keys': [(k, v)]})
    pitch_curve = None
    if ps:
        # Drums that ignore note-off keep going after it, melodic notes stop at note-off
        ignores_off = tid.startswith('d:') and '0x500' not in cref['off']
        seq = [(b, v) for b, v in (r['long'] or {}).get('0x680', []) if ignores_off or b < 690]
        cs = np.array([pitch_cents(v) for b, v in seq if b >= 4])
        if len(cs) >= 3:
            fin = float(np.median(cs[-5:]))
            late = [b for b, v in seq if b >= 4 and abs(pitch_cents(v) - fin) > 20]
            if cs.max() - cs.min() > 100 and late and max(late) * BLOCK > 0.3:
                ref = float(np.median(steady))
                pitch_curve = [[b * BLOCK, pitch_cents(v) - ref] for b, v in seq]
                # The last write holds, so the curve (and the baked sample) can end one block after it
                pitch_curve.append([pitch_curve[-1][0] + BLOCK, pitch_curve[-1][1]])
    # Level and cutoff follow the key on one line per zone (key number modulators). The XG level and cutoff key
    # scaling has breakpoints (OrchHit2 element 2: flat to key 88, near silent from 96), so a zone splits where
    # a line misses a key by more than 1 dB or 50 cents.
    egs = {}

    def key_eg(k, v):
        """Amplitude EG of one key from its own registers (element 2 short notes missing the release use the
        center key's note-off)."""
        if k not in egs:
            o2 = v['off'] if '0x500' in v['off'] or not tid.startswith('m:') else cref['off']
            egs[k] = amp_eg(v['on'], o2, decay2=z_d2(v['on'], o2, d2, el, cref), hold_extra=z_hold(d2, cref))
        return egs[k]

    def key_values(k, v):
        a = level_and_pan(v['on'])[0]
        c, rr = filter_regs({**v['on'], **{x: y for x, y in v.get('first', {}).items() if x == '0x400'}})
        m = filter_match(c, rr)
        e = key_eg(k, v)
        # Times under 4 ms all sound instant, -12000 (none) would swamp the line fit
        return a - m[2] * 10, m[0], max(-8000, e['decayVolEnv']), max(-8000, e['releaseVolEnv'])

    def misfit(keys):
        ks = [k for k, _ in keys]
        vals = [key_values(k, v) for k, v in keys]
        err = 0.0
        # The decay and release rates have breakpoints too (Glocken 0/0/9: rate 48 up to key 75, then 50).
        # 100 timecents is under one rate step (150).
        for i, tol in ((0, 10), (1, 50), (2, 100), (3, 100)):
            ys = [x[i] for x in vals]
            b, c = linfit(ks, ys)
            err = max(err, max(abs(b * k + c - y) for k, y in zip(ks, ys)) / tol)
        return err

    def split(keys):
        if len(keys) < 3 or misfit(keys) <= 1: return [keys]
        best = min(range(1, len(keys)), key=lambda i: max(misfit(keys[:i]), misfit(keys[i:])))
        return split(keys[:best]) + split(keys[best:])
    # The tuning stays fitted on the whole group: per key pitch can be noisy, a zone of 2 keys would turn that
    # into a wild scale tuning.
    groups = [{'sample': g['sample'], 'keys': part, 'tuneKeys': g['keys']} for g in groups
              for part in split(g['keys'])]
    zones = []
    for g in groups:
        ks = [k for k, _ in g['keys']]
        center = g['keys'][len(ks) // 2]
        smp = samples[g['sample']]
        # Tuning: cents(k) = scale * (k - root) + tune
        tks = [k for k, _ in g.get('tuneKeys', g['keys'])]
        cents = [kc[k] for k, _ in g.get('tuneKeys', g['keys'])]
        scale, tune = linfit([k - smp['root'] for k in tks], cents)
        if len(tks) < 2: scale = 0.0 if tks[0] in flat else 100.0
        if len(tks) < 2: tune = cents[0] - scale * (tks[0] - smp['root'])
        att = [level_and_pan(v['on']) for _, v in g['keys']]
        att = [(a - filter_match(*filter_regs(v['on']))[2] * 10, pn) for (a, pn), (_, v) in zip(att, g['keys'])]
        att_slope, att_icpt = linfit(ks, [a for a, _ in att])
        pan = att[len(att) // 2][1]
        # The first cutoff write is the filter EG start (a 10 ms tick may follow in the same block)
        flt = [filter_regs({**v['on'], **{k: x for k, x in v.get('first', {}).items() if k == '0x400'}})
               for _, v in g['keys']]
        fms = [filter_match(c, rr) for c, rr in flt]
        # Keys on the ceiling get the unclamped velocity 127 offset (see the velocity cutoff fit)
        fc = [f[0] + (vfc_offset if c >= (0x7ff if rr >= 4 else 0x7c0) else 0) for f, (c, rr) in zip(fms, flt)]
        fc_slope, fc_icpt = linfit(ks, fc)
        on, off = center[1]['on'], center[1]['off']
        if tid.startswith('m:') and '0x500' not in off:
            # Some element 2 short notes miss the release write, the long note has it (shifted like the decay)
            rel = [v for b, v in (r['long'] or {}).get('0x500', []) if b >= 690]
            if rel:
                off = {**off, '0x500': (rel[0] & 0x80) | max(0, min(0x7f, (rel[0] & 0x7f) + _delta(on, {}, cref)))}
        eg = amp_eg(on, off, decay2=z_d2(on, off, d2, el, cref), hold_extra=z_hold(d2, cref))
        level_offset = eg.pop('levelOffset')
        # The decay and release rates follow the key (Orgel 0/64/10: decay rate +1 every 3 keys, the time halves
        # every 24 keys). Slopes in timecents per key from each key's own registers. Keys where the fit changes
        # shape (sustain, folded click) stay out.
        dk, rk = [], []
        for k, v in g['keys']:
            e = key_eg(k, v)
            if abs(e['sustainVolEnv'] - eg['sustainVolEnv']) < 5 and abs(e['levelOffset'] - level_offset) < 5:
                if e['decayVolEnv'] > -12000: dk.append((k, e['decayVolEnv']))
                if '0x500' in v['off'] or not tid.startswith('m:'): rk.append((k, max(-8000, e['releaseVolEnv'])))
        decay_slope = linfit(*zip(*dk))[0] if len(dk) >= 2 else 0.0
        release_slope = linfit(*zip(*rk))[0] if len(rk) >= 2 else 0.0
        # Drum notes that ignore note-off but hold before decaying: SF2 would end the hold at note-off, so the
        # builder bakes the envelope into the sample and the zone plays it to the end
        tc = lambda v: 0.0 if v <= -12000 else 2 ** (v / 1200)
        # Only where cutting the hold at note-off would lose more than 1 dB of the decay
        lost_db = (tc(eg['attackVolEnv']) + tc(eg['holdVolEnv'])) * 96 / max(tc(eg['decayVolEnv']), 1e-3)
        bake = tid.startswith('d:') and '0x500' not in off and eg['sustainVolEnv'] >= 960 and lost_db > 1
        zones.append({'keyLo': ks[0], 'keyHi': ks[-1], 'sample': g['sample'],
                      'scaleTuning': round(scale), 'tuneCents': tune, 'attenuation': att_icpt + att_slope * center[0] + level_offset,
                      'attSlope': att_slope, 'pan': pan, 'filterFc': fc_icpt + fc_slope * center[0], 'fcSlope': fc_slope,
                      'filterQ': fms[len(fms) // 2][1], 'centerKey': center[0], 'eg': eg, 'bakeEnvelope': bake,
                      'decaySlope': decay_slope, 'releaseSlope': release_slope,
                      '_c': flt[len(flt) // 2][0], 'bakeRoot': tks[len(tks) // 2]})
    # CC71 (harmonic content): above 64 the resonance rises to 31 at 127, below 64 it falls to 0 (0.5 steps per
    # CC value). Both halves as linear modulators, plus the gain the biquad needs at the new resonance.
    c_ref, r_ref = filter_regs(cref['on'])
    base_m, up_m, down_m = filter_match(c_ref, r_ref), filter_match(c_ref, 31), filter_match(c_ref, 0)
    cc71 = {'up': [round(up_m[1] - base_m[1]), round((up_m[2] - base_m[2]) * 10)],
            'down': [round(base_m[1] - down_m[1]), round((base_m[2] - down_m[2]) * 10)]}
    # CC74 / CC71 cutoff curves at the cutoff that sounds: the zone's center key register, moved by the filter
    # EG from its start to its sustain (long note). 0/64/83 starts at the ceiling 1984 and sustains at 1032,
    # Syn.Drum 0/0/118 runs 1648..2047 over the keys. The instrument global zone gets them when every zone
    # is within one CC74 step of the others, else each zone its own.
    # The cutoff over the long note weighted by how loud the note is then (center zone's amp EG): a decaying
    # Clavi 0/0/7 is heard at its start cutoff, a held lead at its sustain
    seq = [(b, v & 0x7ff) for b, v in (r['long'] or {}).get('0x400', []) if b < 690]
    eg_delta = 0
    if len(seq) >= 2 and zones:
        ez = min(zones, key=lambda z: abs(z['centerKey'] - TARGETS[tid]['center']))['eg']
        sec = lambda tc: 0.0 if tc <= -12000 else 2 ** (tc / 1200)
        t0 = sec(ez['attackVolEnv']) + sec(ez['holdVolEnv'])
        t = np.arange(690) * BLOCK
        db = -np.minimum(ez['sustainVolEnv'] / 10, 100 * np.maximum(0, t - t0) / max(sec(ez['decayVolEnv']), 1e-3))
        idx = np.searchsorted([b for b, _ in seq], np.arange(690), side='right') - 1
        cs_t = np.array([seq[max(0, i)][1] for i in idx])
        w = 10 ** (db / 20)
        eg_delta = int(round((cs_t * w).sum() / w.sum())) - seq[0][1]
    top = 0x7ff if r_ref >= 4 else 0x7c0
    # The measured notes play at velocity 127, songs mostly lower, and velocity moves the cutoff register on a line
    # (GrandPno key 60: 1558 at 64, 1846 at 100, 1984 = the ceiling at 127). Fitted at 127, GrandPno sat on the
    # ceiling, so CC74 got no upper half and CC71 read the map's jump to an open filter there (1100 cents darker
    # at CC71 85, the DLL is barely darker). So the curves are fitted at velocity 100, between the long notes at
    # 64 and 127. The 127 one can be clamped, then this shift is a bit short.
    v64 = [(b, v & 0x7ff) for b, v in (VEL_LONG.get(tid) or {}).get('0x400', [])]
    vel_shift = 0
    if seq and v64:
        vel_shift = round((min(v64[0][1], top) - min(seq[0][1], top)) * (127 - 100) / (127 - VEL_LONG_VELOCITY))
    # The CC74 curves are fitted over velocity (fit_cc74_velocity), from the velocity sweep at the center key
    # (24..127) moved to each zone's own cutoff
    sweep = sorted((int(v), filter_regs({**d['on'], **{k: x for k, x in d.get('first', {}).items() if k == '0x400'}}))
                   for v, d in (r.get('vels') or {}).items() if int(v) >= 24 and d.get('on'))
    s127 = dict(sweep).get(127)
    def zone_vels(c127):
        if not s127: return None
        return tuple((v, max(0, c127 + cv - s127[0]) // 16 * 16, rv) for v, (cv, rv) in sweep)
    for z in zones:
        c = z.pop('_c') + eg_delta
        z['_v'] = zone_vels(max(0, min(top, c)))
        z['_c'] = max(0, min(top, c + vel_shift)) // 16 * 16
    cs = [z['_c'] for z in zones]
    bright = None
    if cs and max(cs) - min(cs) < 16:
        mid = min(zones, key=lambda z: abs(z['_c'] - int(np.median(cs))))
        bright = brightness_fits(int(np.median(cs)) // 16 * 16, r_ref, mid['_v'])
        for z in zones: z.pop('_c'), z.pop('_v')
    else:
        for z in zones: z['brightness'] = brightness_fits(z.pop('_c'), r_ref, z.pop('_v'))
    # Filter EG from the long note, weighted by the loudness of the zone that plays the long note's key.
    # Fitted later in a process pool (filter_eg_job), the zones only get the base offset then.
    # Pitch that keeps moving a lot (SFX sweeps, random steps): SF2 has no free pitch envelope for it, so the
    # builder bakes the measured curve into a copy of the sample. Relative to the steady pitch the zones use.
    if pitch_curve:
        for z in zones: z['pitchCurve'] = pitch_curve
        zones = bake_windows(zones, pitch_curve, TARGETS[tid]['center'])
    # Amplitude LFO (reg 0x441 low 5 bits): the DLL attenuates by 0..depth * 1.41 dB along the LFO (a triangle up
    # from 0, or a sawtooth). The bank's mod LFO swings around a center, so the zones take half the depth.
    amp_lfo = (cref['on'].get('0x441', 0) & 0x1f) * AMP_LFO_CB
    for z in zones: z['attenuation'] += amp_lfo / 2
    # Filter LFO (bits 5..7): the cutoff word drops by 0..depth * 2^15 (half an octave per unit) along the same
    # unipolar LFO, the zones take half of it like the amplitude LFO
    filter_lfo = (cref['on'].get('0x441', 0) >> 5) * FILTER_LFO_CENTS
    for z in zones: z['filterFc'] -= filter_lfo / 2
    amp_delay = AMP_DELAY_MS[el[0x44]] / 1000 if el and el[0x44] < 16 else 0.0
    if amp_delay and amp_delay >= DELAY_PREFIX and zones:
        zones = delay_windows(zones, amp_delay, TARGETS[tid]['center'])
    feg_job = None
    if zones and r['long'] and len(r['long'].get('0x400', [])) >= 2:
        ref = min(zones, key=lambda z: abs(z['centerKey'] - TARGETS[tid]['center']))
        feg_job = (r['long']['0x400'], ref['eg'], (VEL_LONG.get(tid) or {}).get('0x400'), vfc_mod, TARGETS[tid]['vel'])
    return zones, {'ampDelay': amp_delay, 'ampLfo': amp_lfo, 'filterLfo': filter_lfo, 'velRef': vref, 'velocity': vel_mod, 'velocity2': vel_mod2, 'velocityFc': vfc_mod, 'velocityQ': vq_mod, 'filterEG': feg_job, 'cc71': cc71, 'brightness': bright}

def velocity_shift_only(seq, seq_vel):
    """True when the velocity 127 long note starts on the cutoff ceiling and velocity only shifts the register.
    Such a note hides the EG: the register stays clamped until the EG has taken it below (GrandPno 0/0/0: 1984
    for 2 s, fitted as a 2 s hold), and the velocity 64 note then read as a different depth (GrandPno at velocity
    42: -1630 cents, 550 Hz instead of 1.7 kHz, 15..24 dB too dark above 1 kHz). For many elements velocity only
    shifts the register (GrandPno key 60: -174 over 2.3 s at velocity 42 and 100), the EG comes from the velocity
    64 note then, without a depth change. Others really scale the EG with velocity (E.Piano1 0/0/4, see Filter
    EG), they keep the joint fit. Told apart where the velocity 127 note is off the ceiling: a shift keeps the
    register difference to velocity 64 constant (std under 6 units: 38 of 149 such elements). No such part in the
    690 block window (10, GrandPno): a shift."""
    if not seq or not seq_vel: return False
    on, von = filter_regs({'0x400': seq[0][1]}), filter_regs({'0x400': seq_vel[0][1]})
    if on[0] < (0x7ff if on[1] >= 4 else 0x7c0) or von[0] >= (0x7ff if von[1] >= 4 else 0x7c0): return False
    def regs(sq):
        bs = [b for b, _ in sq]
        idx = np.searchsorted(bs, np.arange(690), side='right') - 1
        return np.array([filter_regs({'0x400': sq[max(0, i)][1]}) for i in idx])
    a, b = regs(seq), regs(seq_vel)
    free = a[:, 0] < np.where(a[:, 1] >= 4, 0x7ff, 0x7c0)
    return bool(free.sum() < 20 or np.std(a[free, 0] - b[free, 0]) < 6)

BAKE_RANGE, BAKE_STRETCH = (36, 96), 0.2

def key_windows(zones, semis, center, field):
    """Zones split into key windows on one grid centered on the long note's key, each window's key in `field`.
    semis: how far (semitones of pitch, so in keys at the zone's scale tuning) a key may sit from its window's
    key, inside BAKE_RANGE. The keys outside it get one window per side."""
    lo_r, hi_r = BAKE_RANGE
    ext_lo, ext_hi = min(z['keyLo'] for z in zones), max(z['keyHi'] for z in zones)
    out = []
    for z in zones:
        w = min(128, int(2 * semis * 100 / max(1, abs(z['scaleTuning']))) + 1)
        if w > hi_r - lo_r:
            out.append(z)
            continue
        g0 = center - w // 2
        window = lambda k: (0, lo_r - 1) if k < lo_r else (hi_r + 1, 127) if k > hi_r else \
            (max(lo_r, g0 + (k - g0) // w * w), min(hi_r, g0 + (k - g0) // w * w + w - 1))
        k = z['keyLo']
        while k <= z['keyHi']:
            a, b = window(k)
            hi = min(b, z['keyHi'])
            # The low outer window takes its top key: a bake far down runs for seconds (one-shot data, long attacks)
            key = min(b, ext_hi) if a == 0 and b < lo_r else (max(a, ext_lo) + min(b, ext_hi)) // 2
            out.append({**z, 'keyLo': k, 'keyHi': hi, field: key})
            k = hi + 1
    return out

def bake_windows(zones, curve, center):
    """Pitch baked zones split into key windows, each baked at its own middle key. A baked sample played at
    another key also runs the curve faster or slower (Stardust 0/68/102: keys 0..71 shared one bake at key 36,
    key 60 ran the 0.8 s glide in 0.2 s). Windows keep that within BAKE_STRETCH (or 30 ms) inside BAKE_RANGE.
    The grid is centered on the long note's key, where the curve was measured: on a grid from key 36, key 60 sat
    9 keys off its bake in Tweet 0/0/123 (24 cents flat on its fast pitch sweep). Zones split elsewhere (level or
    cutoff breakpoints) still share their bakes. 64/0/55 moves 5 cents per key: one window."""
    fin = curve[-1][1]
    act = max([t for t, c in curve if abs(c - fin) > 5] + [0.0])
    if act <= 0: return zones
    return key_windows(zones, 12 * math.log2(1 + max(BAKE_STRETCH, 0.03 / act)), center, 'bakeRoot')

# Amplitude LFO attenuation per unit of reg 0x441 & 0x1f, cB. Measured on Tremolo Strings 0/0/44 element 1 with
# element byte 0x0c swept (level folded over the 32 block LFO period): 1.72, 3.23, 11.25, 22.51, 43.86 dB at
# depth 1, 2, 8, 16, 31.
AMP_LFO_CB = 14.1
# Filter LFO cents per unit of reg 0x441 >> 5. The chip (`FUN_10019b40`) subtracts
# depth * lfo >> 4 from the cutoff word (65536 per octave), the triangle's unipolar table peaks at 2^19 (from the
# amplitude measurement: >> 5 there gives 1.5 dB per unit). All 19 elements with one use the triangle.
FILTER_LFO_CENTS = 600

# Element byte 0x44 = 1..15: delay before the element sounds, ms (measured, the same at every key and velocity).
# The sample and the amplitude EG start after it, the firmware EGs (pitch, filter) run from note-on. The ROM only
# uses 0..15 (higher values behave differently, 0x7f has no release segment).
AMP_DELAY_MS = (0, 5.8, 17.4, 37.7, 75.5, 116.1, 156.7, 238.0, 316.4, 435.4, 635.6, 835.9, 1277.1, 1715.4,
                2597.7, 3477.2)
DELAY_SLIP = 0.005
# Shorter delays would only delay the amplitude EG, the sample running from note-on (the first 6 or 17 ms go
# missing). The prefixed copies of those 41 elements take 4.4 MB, at 0.03 song channels with them got worse
# (0/66/38 in Cybergate ch 13 dev 3.6 -> 4.3, 2..8 kHz band p90 5.67 -> 6.19).
DELAY_PREFIX = 0.0

def delay_windows(zones, delay, center):
    """The bank delays the amplitude EG (delayVolEnv) and starts a silence prefixed copy of the sample, skipping
    as much of the prefix as the zone's key plays in the delay (src/writer/main.ts). Inside a zone the sample start then
    slips against the EG at other keys, windows keep that within DELAY_SLIP."""
    return key_windows(zones, 12 * math.log2(1 + DELAY_SLIP / delay), center, 'delayKey')

def filter_eg_job(job):
    """SF2 modulation envelope generators for one element's filter EG, plus the base cutoff offset."""
    if job is None: return None
    seq, eg, seq_vel, vfc, lvel = job
    # The note-on fit (17 velocities) sets the start cutoff, the vel 64 long note then only gives the depth. The
    # long note plays at lvel: 127, or the top of the element's velocity range (VeloSlap 0/43/37 element 1: 99)
    rel = lambda curve: vel_x(curve, VEL_LONG_VELOCITY) - vel_x(curve, lvel)
    shift = -vfc[1] * rel(vfc[0]) if vfc else None
    if velocity_shift_only(seq, seq_vel): seq, seq_vel = seq_vel, None
    f = fit_filter_eg(seq, lambda v: filter_match(*filter_regs({'0x400': v}))[0], eg, seq_vel, shift)
    if not f: return None
    vel = {}
    if 'vel_depth' in f:
        # The depth modulator shares the curve of the note-on cutoff fit, scaled to the vel 64 value
        curve = vfc[0] if vfc else 'linear'
        at64 = rel(curve)
        vel = {'velocityDepth': [curve, -f['vel_depth'] / at64] if abs(f['vel_depth']) > 20 else None}
        if not vfc: vel['velocityFc'] = [curve, -f['vel_fc'] / at64] if abs(f['vel_fc']) > 20 else None
    return {**vel, 'velRef': lvel, 'delayModEnv': seconds_to_timecents(f['delay']), 'attackModEnv': seconds_to_timecents(f['attack']),
            'holdModEnv': seconds_to_timecents(f['hold']), 'decayModEnv': seconds_to_timecents(f['decay']),
            'sustainModEnv': round(1000 * (1 - f['sustain'])),
            'releaseModEnv': seconds_to_timecents(f['release']), 'modEnvToFilterFc': round(f['amount']),
            'base': f['base'], 'rms': round(f['rms']), 'span': round(f['span'])}

def main():
    instruments = []
    inst_index = {}
    from multiprocessing import Pool
    results = [element_zones(tid) for tid in TARGETS]
    # At most 8 worker processes (the machine is shared)
    with Pool(8) as pool:
        fegs = pool.map(filter_eg_job, [g['filterEG'] if g else None for _, g in results], chunksize=4)
    for tid, (zones, glob), feg in zip(TARGETS, results, fegs):
        if not zones: continue
        glob['filterEG'] = feg
        if feg:
            for z in zones: z['filterFc'] += feg['base']
            # The vel 64 long note replaces the note-on velocity fit (that one shifts the whole sweep)
            if 'velocityFc' in feg: glob['velocityFc'] = feg.pop('velocityFc')
            # The generator holds the velocity 127 depth, the modulator takes away what it adds at 127
            feg['modEnvToFilterFc'] = round(feg['modEnvToFilterFc'] + vel_at127(feg.get('velocityDepth'), feg.pop('velRef')))
        # Same for the note-on velocity modulators (attenuation adds, cutoff and resonance subtract)
        for z in zones:
            z['attenuation'] -= vel_at127(glob['velocity'], glob['velRef']) + vel_at127(glob['velocity2'], glob['velRef'])
            z['filterFc'] += vel_at127(glob['velocityFc'], glob['velRef'])
            z['filterQ'] = round(z['filterQ'] + vel_at127(glob['velocityQ'], glob['velRef']))
        if tid.startswith('d:'):
            # Drum note record byte 3: alternate group (closed hi-hat cuts the open one)
            _, msb, prog, key, _ = tid.split(':')
            alt = BIN[drum_notes(KITS[(int(msb), int(prog))])[int(key)][0] + 3]
            for z in zones: z['exclusiveClass'] = alt
        # Element LFO: vibrato rate, delay and depth plus the CC76/77/78 responses (lfo.py)
        glob['lfo'] = lfo_for(element_bytes(tid))
        inst_index[tid] = len(instruments)
        instruments.append({'id': tid, 'zones': zones, 'global': glob})
    presets = []
    for p in melodic():
        ids = ['m:%d:%d:%d:%d' % (p['msb'], p['lsb'], p['program'], e) for e in range(2)]
        insts = [inst_index[i] for i in ids if i in inst_index]
        # Element velocity ranges (element bytes 3 / 4, E.Piano1 0/0/4 element 2 from 80, VelGtHrm 0/43/24 1..120 and
        # 121..127). They were computed from the measured velocities and then left out, so both elements of a
        # velocity switch played at every velocity. The key ranges are in the zones (only keys the element plays).
        els = []
        for i in ids:
            if i not in inst_index: continue
            eb = element_bytes(i)
            el = {'instrument': inst_index[i]}
            if eb is not None and (eb[3], eb[4]) != (1, 127): el.update(velLo=eb[3], velHi=eb[4])
            els.append(el)
        presets.append({'name': p['name'], 'msb': p['msb'], 'lsb': p['lsb'], 'program': p['program'], 'drum': False,
                        'elements': els})
    for (msb, prog), kit in KITS.items():
        els = []
        for tid in TARGETS:
            parts = tid.split(':')
            if parts[0] == 'd' and int(parts[1]) == msb and int(parts[2]) == prog and tid in inst_index:
                els.append({'instrument': inst_index[tid], 'key': int(parts[3])})
        presets.append({'name': KIT_NAMES[kit], 'msb': msb, 'lsb': 0, 'program': prog, 'drum': True, 'elements': els})
    # Instrument names: every instrument belongs to one preset. Melodic ones are the voice's 8 character name and
    # the element number (GrandPno 1), drum notes the kit and the key (Standard Kit #36, -2 for a note's second
    # element). A name used twice (the MU80 calls 0/0/96 and 64/0/32 both Rain) gets the bank added.
    for p in melodic():
        for e in range(2):
            tid = 'm:%d:%d:%d:%d' % (p['msb'], p['lsb'], p['program'], e)
            if tid in inst_index: instruments[inst_index[tid]]['name'] = '%s %d' % (p['short'], e + 1)
    for tid, i in inst_index.items():
        if not tid.startswith('d:'): continue
        _, msb, prog, key, e = tid.split(':')
        two = any(t in inst_index for t in ('d:%s:%s:%s:%d' % (msb, prog, key, x) for x in (0, 1)) if t != tid)
        instruments[i]['name'] = '%s #%s' % (KIT_NAMES[KITS[(int(msb), int(prog))]], key) + ('-%d' % (int(e) + 1) if two else '')
    seen = set()
    for inst in instruments:
        if inst['name'] in seen:
            _, msb, lsb = inst['id'].split(':')[:3]
            inst['name'] += ' (%s/%s)' % (msb, lsb)
        seen.add(inst['name'])
    json.dump({'samples': samples, 'instruments': instruments, 'presets': presets},
              open(BANK, 'w'))
    print(len(samples), 'samples,', len(instruments), 'instruments,', len(presets), 'presets')

if __name__ == '__main__':
    main()
