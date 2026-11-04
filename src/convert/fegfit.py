"""Filter EG -> SF2 modulation envelope.

The DLL rewrites the cutoff (reg 0x400) on every 10 ms tick. The long note (2 s hold, 1 s release, center key)
gives the trajectory. It is converted to biquad cents (filter_match) and fitted with spessasynth_core's
modulation envelope: Fc(t) = base + amount * env(t), env 0 -> 1 (convex attack) -> hold -> linear decay to
sustain -> linear release to 0.
"""
import math
import numpy as np
from scipy.optimize import least_squares

BLOCK = 128 / 44100
NOTE_OFF = 690 * BLOCK
END = (690 + 345) * BLOCK
GRID = np.arange(0, END, 0.005)
# ModulationEnvelope.tc2Sec: times of -10114 timecents (2.9 ms) or less are 0
MIN_TIME = 2 ** (-10114 / 1200)


def _convex(x):
    x = np.clip(x, 1e-9, 1)
    return np.clip(1 - (-400 / 960) * np.log10(x), 0, 1)


def mod_env(t, delay, attack, hold, decay, sustain, release, note_off=NOTE_OFF, core_times=False):
    """spessasynth_core ModulationEnvelope.process on a time grid (times in seconds, decay/release full scale).
    core_times: times the core plays as 0 (MIN_TIME) are 0 here too. Without it a 1 ms attack starts the fit at env
    0 on the first grid point, where the core is already at the peak, and that one point pushed Synth Drum
    0/0/118's pitch glide (a straight 0.42 s fall) to a 21 cent RMS shape. The filter EG fits keep the old model:
    with core times they fitted closer (FastResB 0/20/38 395 -> 89 cents RMS) but closed the filter too early at
    the other keys, the songs got worse (Cybergate ch 13 3.6 -> 8.2, the filter EG is measured at one key)."""
    if core_times:
        z = lambda v: 0.0 if v <= MIN_TIME else v
        delay, attack, hold = z(delay), z(attack), z(hold)
    a_end = delay + attack
    h_end = a_end + hold
    d_len = decay * (1 - sustain)
    d_end = h_end + d_len
    e = np.full_like(t, sustain)
    e = np.where(t < d_end, 1 + (sustain - 1) * np.clip((t - h_end) / max(d_len, 1e-9), 0, 1), e)
    e = np.where(t < h_end, 1.0, e)
    e = np.where(t < a_end, _convex((t - delay) / max(attack, 1e-9)), e)
    e = np.where(t < delay, 0.0, e)
    # Release from the level at note-off
    pre = t < note_off
    lvl = np.interp(note_off, t[pre], e[pre]) if pre.any() else 0.0
    rd = max(release * lvl, 1e-9)
    rel = np.clip(1 - (t - note_off) / rd, 0, 1) * lvl
    return np.where(pre, e, rel)



def core_env(t, delay, attack, hold, decay, sustain, release, note_off=NOTE_OFF):
    """mod_env as the core runs it: computed once per 128 sample block (at the block start), short times are 0.
    A fitted attack of a few ms looks harmless on the 5 ms grid, but in the core the whole first block then sits
    at the env 0 cutoff (Stardust 0/68/102: open filter for 2.9 ms where the DLL starts at 59 Hz)."""
    z = lambda v: 0.0 if v <= MIN_TIME else v
    tq = np.floor(t / BLOCK) * BLOCK
    return mod_env(tq, z(delay), z(attack), z(hold), z(decay), sustain, max(release, 2 ** (-7200 / 1200)), note_off)


def trajectory(seq, to_cents, shift=0.0):
    """Step trajectory (cents) on GRID from the long note's register writes, shift seconds after note-on."""
    bs = np.array([b * BLOCK for b, _ in seq])
    cs = np.array([to_cents(v) for _, v in seq])
    idx = np.searchsorted(bs, GRID + shift, side='right') - 1
    return cs[np.clip(idx, 0, len(cs) - 1)]


def _unpack(p):
    base, amount, ld, la, lh, ldc, sus, lr = p
    return base, amount, math.exp(ld), math.exp(la), math.exp(lh), math.exp(ldc), sus, math.exp(lr)


def vol_env_db(t, eg, note_off=NOTE_OFF):
    """Rough dB curve of the zone's SF2 volume envelope (timecents/cB as written to the bank), both decays."""
    tc = lambda v: 0.0 if v <= -12000 else 2 ** (v / 1200)
    a, h, d, r = tc(eg['attackVolEnv']), tc(eg['holdVolEnv']), tc(eg['decayVolEnv']), tc(eg['releaseVolEnv'])
    sus = eg['sustainVolEnv'] / 10
    sus2 = min(100.0, max(0.0, sus + eg.get('sustain2VolEnv', 0) / 10))
    d2 = tc(eg.get('decay2VolEnv', -12000))
    t1 = a + h + d * sus / 96
    db = np.where(t < a + h, 0.0, -np.minimum((t - a - h) / max(d, 1e-9) * 96, sus))
    if d2 and sus2 != sus:
        step = np.clip((t - t1) / d2 * 96, 0, abs(sus2 - sus)) * (1 if sus2 > sus else -1)
        db = np.where(t > t1, -(sus + step), db)
    at_off = float(np.interp(note_off, t, db))
    return np.where(t < note_off, db, at_off - (t - note_off) / max(r, 1e-9) * 96)


def fit(seq, to_cents, eg=None, seq_vel=None, vel_fc=None, shift=0.0, releases=True, flat=50, ref=None,
        core_times=False):
    """Fitted envelope, relative to the cutoff at note-on: dict or None when the cutoff barely moves.

    eg (the zone's volume envelope) weights the error by loudness, so the audible part fits best.
    seq_vel is the same note at a lower velocity. The DLL shifts the cutoff and scales the EG depth with
    velocity, so with the envelope shape fixed, cutoff shift (vel_fc) and depth change (vel_depth) are a
    linear fit. The DLL rates are slopes, so a smaller depth also runs faster, which SF2 cannot follow.
    A given vel_fc (cents at seq_vel's velocity, from the note-on cutoffs) is kept and only the depth is fitted.
    shift: seconds the voice starts after note-on (delayVoice), the firmware EG runs from note-on, the SF2 one
    from the voice start. releases=False for notes that ignore note-off (no release in the bank either).
    flat: below this span (cents) the curve counts as flat. ref (absolute cents): also report the error of a flat
    line there (rms0), to judge whether the envelope helps.
    """
    note_off = (NOTE_OFF if releases else 1e9) - shift
    y = trajectory(seq, to_cents, shift)
    w = np.ones_like(GRID) if eg is None else np.maximum(10 ** (vol_env_db(GRID, eg, note_off) / 40), 0.01)
    # Relative to the first write, which is what the zones use as the cutoff at note-on. Block 0 can hold a
    # second write (a 10 ms tick) and the grid only sees that one.
    y0 = to_cents(seq[0][1])
    rms0 = None if ref is None else math.sqrt(float(np.sum(((y - ref) * w) ** 2)) / float(np.sum(w * w)))
    y = y - y0
    pre = GRID < note_off
    if np.ptp(y[pre]) < flat:
        # Flat, but maybe away from the first write: keep that as a static offset
        off = float(np.median(y[pre]))
        if abs(off) < 20: return None
        return {'base': off, 'amount': 0.0, 'delay': 0.001, 'attack': 0.001, 'hold': 0.001, 'decay': 0.001,
                'sustain': 1.0, 'release': 0.001, 'rms': 0.0, 'span': 0.0, 'rms0': rms0, 'y0': y0}
    lo, hi = y.min(), y.max()

    def resid(p):
        base, amount, d, a, h, dc, s, r = _unpack(p)
        return (base + amount * mod_env(GRID, d, a, h, dc, s, r, note_off, core_times) - y) * w

    ln = math.log
    lb = [lo - 2400, -12000, ln(1e-3), ln(1e-3), ln(1e-3), ln(1e-3), 0.0, ln(1e-3)]
    ub = [hi + 2400, 12000, ln(20), ln(20), ln(20), ln(100), 1.0, ln(100)]
    best = None
    end_pre = y[pre][-1]
    starts = []
    for amount in (hi - lo, lo - hi):
        for base in (0.0, y[-1], y.min() if amount > 0 else y.max()):
            for att in (0.002, 0.2):
                for dec in (0.3, 3.0):
                    s = 0.5
                    if abs(amount) > 1:
                        s = float(np.clip((end_pre - base) / amount, 0.01, 0.99))
                    starts.append([base, amount, ln(0.002), ln(att), ln(0.002), ln(dec), s, ln(0.5)])
    for x0 in starts:
        x0 = np.clip(x0, np.array(lb) + 1e-6, np.array(ub) - 1e-6)
        r = least_squares(resid, x0, bounds=(lb, ub), x_scale='jac', max_nfev=400)
        if best is None or r.cost < best.cost:
            best = r
    base, amount, d, a, h, dc, s, rel = _unpack(best.x)
    # The DLL often starts at the EG peak (base + amount), the fit then takes a short attack from base. Without the
    # attack the core starts there too.
    cost = lambda d, a: float(np.sum(((base + amount * core_env(GRID, d, a, h, dc, s, rel, note_off) - y) * w) ** 2))
    if a < 0.05 and cost(d, 0.0) < cost(d, a):
        a = 0.001
        if d < 0.05 and cost(0.0, a) < cost(d, a): d = 0.001
    err = math.sqrt(2 * best.cost / float(np.sum(w * w)))
    out = {'base': base, 'amount': amount, 'delay': d, 'attack': a, 'hold': h, 'decay': dc, 'sustain': s,
           'release': rel, 'rms': err, 'span': float(np.ptp(y)), 'rms0': rms0, 'y0': y0}
    if seq_vel:
        e = mod_env(GRID, d, a, h, dc, s, rel, note_off, core_times)
        yv = trajectory(seq_vel, to_cents, shift) - y0 - base - amount * e
        if vel_fc is None:
            m = np.stack([np.ones_like(e), e], axis=1) * w[:, None]
            (kf, ka), *_ = np.linalg.lstsq(m, yv * w, rcond=None)
        else:
            kf = vel_fc
            ka = float(np.sum((yv - kf) * e * w * w) / max(np.sum(e * e * w * w), 1e-9))
        out['vel_fc'], out['vel_depth'] = float(kf), float(ka)
    return out
