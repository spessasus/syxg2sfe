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


def _convex(x):
    x = np.clip(x, 1e-9, 1)
    return np.clip(1 - (-400 / 960) * np.log10(x), 0, 1)


def mod_env(t, delay, attack, hold, decay, sustain, release):
    """spessasynth_core ModulationEnvelope.process on a time grid (times in seconds, decay/release full scale)."""
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
    pre = t < NOTE_OFF
    lvl = np.interp(NOTE_OFF, t[pre], e[pre]) if pre.any() else 0.0
    rd = max(release * lvl, 1e-9)
    rel = np.clip(1 - (t - NOTE_OFF) / rd, 0, 1) * lvl
    return np.where(pre, e, rel)


# ModulationEnvelope.tc2Sec: times of -10114 timecents (2.9 ms) or less are 0
MIN_TIME = 2 ** (-10114 / 1200)


def core_env(t, delay, attack, hold, decay, sustain, release):
    """mod_env as the core runs it: computed once per 128 sample block (at the block start), short times are 0.
    A fitted attack of a few ms looks harmless on the 5 ms grid, but in the core the whole first block then sits
    at the env 0 cutoff (Stardust 0/68/102: open filter for 2.9 ms where the DLL starts at 59 Hz)."""
    z = lambda v: 0.0 if v <= MIN_TIME else v
    tq = np.floor(t / BLOCK) * BLOCK
    return mod_env(tq, z(delay), z(attack), z(hold), z(decay), sustain, max(release, 2 ** (-7200 / 1200)))


def trajectory(seq, to_cents):
    """Step trajectory of the cutoff (cents) on GRID from the long note's 0x400 writes."""
    bs = np.array([b * BLOCK for b, _ in seq])
    cs = np.array([to_cents(v) for _, v in seq])
    idx = np.searchsorted(bs, GRID, side='right') - 1
    return cs[np.clip(idx, 0, len(cs) - 1)]


def _unpack(p):
    base, amount, ld, la, lh, ldc, sus, lr = p
    return base, amount, math.exp(ld), math.exp(la), math.exp(lh), math.exp(ldc), sus, math.exp(lr)


def vol_env_db(t, eg):
    """Rough dB curve of the zone's SF2 volume envelope (timecents/cB as written to the bank)."""
    tc = lambda v: 0.0 if v <= -12000 else 2 ** (v / 1200)
    a, h, d, r = tc(eg['attackVolEnv']), tc(eg['holdVolEnv']), tc(eg['decayVolEnv']), tc(eg['releaseVolEnv'])
    sus = eg['sustainVolEnv'] / 10
    db = np.where(t < a + h, 0.0, -np.minimum((t - a - h) / max(d, 1e-9) * 96, sus))
    at_off = float(np.interp(NOTE_OFF, t, db))
    return np.where(t < NOTE_OFF, db, at_off - (t - NOTE_OFF) / max(r, 1e-9) * 96)


def fit(seq, to_cents, eg=None, seq_vel=None, vel_fc=None):
    """Fitted envelope, relative to the cutoff at note-on: dict or None when the cutoff barely moves.

    eg (the zone's volume envelope) weights the error by loudness, so the audible part fits best.
    seq_vel is the same note at a lower velocity. The DLL shifts the cutoff and scales the EG depth with
    velocity, so with the envelope shape fixed, cutoff shift (vel_fc) and depth change (vel_depth) are a
    linear fit. The DLL rates are slopes, so a smaller depth also runs faster, which SF2 cannot follow.
    A given vel_fc (cents at seq_vel's velocity, from the note-on cutoffs) is kept and only the depth is fitted.
    """
    y = trajectory(seq, to_cents)
    w = np.ones_like(GRID) if eg is None else np.maximum(10 ** (vol_env_db(GRID, eg) / 40), 0.01)
    # Relative to the first write, which is what the zones use as the cutoff at note-on. Block 0 can hold a
    # second write (a 10 ms tick) and the grid only sees that one.
    y0 = to_cents(seq[0][1])
    y = y - y0
    pre = GRID < NOTE_OFF
    if np.ptp(y[pre]) < 50:
        # Flat, but maybe away from the first write: keep that as a static offset
        off = float(np.median(y[pre]))
        if abs(off) < 20: return None
        return {'base': off, 'amount': 0.0, 'delay': 0.001, 'attack': 0.001, 'hold': 0.001, 'decay': 0.001,
                'sustain': 1.0, 'release': 0.001, 'rms': 0.0, 'span': 0.0}
    lo, hi = y.min(), y.max()

    def resid(p):
        base, amount, d, a, h, dc, s, r = _unpack(p)
        return (base + amount * mod_env(GRID, d, a, h, dc, s, r) - y) * w

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
    cost = lambda d, a: float(np.sum(((base + amount * core_env(GRID, d, a, h, dc, s, rel) - y) * w) ** 2))
    if a < 0.05 and cost(d, 0.0) < cost(d, a):
        a = 0.001
        if d < 0.05 and cost(0.0, a) < cost(d, a): d = 0.001
    err = math.sqrt(2 * best.cost / float(np.sum(w * w)))
    out = {'base': base, 'amount': amount, 'delay': d, 'attack': a, 'hold': h, 'decay': dc, 'sustain': s,
           'release': rel, 'rms': err, 'span': float(np.ptp(y))}
    if seq_vel:
        e = mod_env(GRID, d, a, h, dc, s, rel)
        yv = trajectory(seq_vel, to_cents) - y0 - base - amount * e
        if vel_fc is None:
            m = np.stack([np.ones_like(e), e], axis=1) * w[:, None]
            (kf, ka), *_ = np.linalg.lstsq(m, yv * w, rcond=None)
        else:
            kf = vel_fc
            ka = float(np.sum((yv - kf) * e * w * w) / max(np.sum(e * e * w * w), 1e-9))
        out['vel_fc'], out['vel_depth'] = float(kf), float(ka)
    return out
