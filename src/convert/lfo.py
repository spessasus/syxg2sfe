"""Element LFO (vibrato) model from the firmware (FUN_10014900, FUN_100130a0, FUN_10014610, FUN_10014a30) and
the chip (FUN_1001ab80, FUN_1001a790). All tables are read from the DLL at build time.

lfo_for(el) gives the SF2 values for one element: rate, delay, pitch depth and the CC76/77/78 modulators.
"""
import math, os, struct
import pefile

BLOCK = 128 / 44100
TICK = 0.02  # firmware LFO delay and fade tick (the chip register is only rewritten every 4th tick)

from paths import DLL
_pe = pefile.PE(DLL)


def _table(va, n, signed=False):
    d = _pe.get_data(va - 0x10000000, n)
    return list(struct.unpack('%d%s' % (n, 'b' if signed else 'B'), d))


# speed (element byte 0x07, 6 bits) -> chip rate index (reg 0x480 low 6 bits)
SPEED_IDX = _table(0x10047e10, 64)
# CC76 limit speed, indexed 14 + CC (XG mode)
RATE_CC = _table(0x10047d68, 166)
# CC78 limit delay in ticks, indexed CC (+ 2 * a GS mode value that is 0 in XG)
DELAY_CC = _table(0x10046dc8, 128)
# CC77 depth offset (signed, XG mode)
DEPTH_CC = _table(0x10046f48, 128, True)
# depth state -> reg 0x481
DEPTH_REG = _table(0x10047ce8, 128)
# controller pitch modulation -> reg 0x481 (mod wheel: index = CC1 * 80 >> 10 with the XG defaults)
PMOD_REG = _table(0x10047be8, 128)


def rate_hz(idx):
    """Chip LFO: 20-bit phase advanced once per 128-sample block by 256 * 2^(idx >> 3) * (1 + (idx & 7) / 8)."""
    return 256 * 2 ** (idx >> 3) * (1 + (idx & 7) / 8) / 2 ** 20 / BLOCK


def depth_cents(reg):
    """The chip scales the phase increment by 1 + reg * lfo / 2048 (lfo -1..1). Mean of the up and down swing."""
    x = reg / 2048
    return 600 * (math.log2(1 + x) - math.log2(1 - x))


def hz_to_abs_cents(hz):
    return 1200 * math.log2(hz / 8.176)


def seconds_to_tc(s):
    return -12000 if s <= 0.001 else 1200 * math.log2(s)


def speed_at(speed, cc):
    """FUN_10014900: CC76 below 64 caps the speed, above 64 raises it to at least the table value."""
    lim = RATE_CC[14 + cc]
    if cc < 64: return min(speed, lim)
    if cc > 64: return max(speed, lim)
    return speed


def delay_ticks_at(byte8, cc):
    """FUN_100130a0: delay ticks = byte * 3 / 4 + 3 (0 stays 0). CC78 caps or raises it like CC76 does the speed."""
    n = (byte8 * 3 >> 2) + 3 if byte8 else 0
    if cc < 64: return min(n, DELAY_CC[cc])
    if cc > 64: return max(n, DELAY_CC[cc])
    return n


def depth_target(d, cc):
    """FUN_100130a0: the CC77 offset adds to the element depth when it is smaller, else the depth becomes 2 * offset."""
    o = DEPTH_CC[cc]
    if o < d: return max(0, d + o), False
    return 2 * o, True


def fade_step(target, fade, big):
    s = target // (fade + 1)
    return max(1, min(5 if big else 20, s))


def effective_delay(byte8, fade, d, cc78=64, cc77=64):
    """Seconds until the depth reaches half its target (SF2 has no fade-in). The register lags about half of the
    80 ms rewrite period behind the firmware state, measured median offset -3 blocks without that half."""
    target, big = depth_target(d, cc77)
    if target <= 0: return 0.0
    n = delay_ticks_at(byte8, cc78)
    step = fade_step(target, fade, big)
    up = -(-((target + 1) // 2) // step)
    if n == 0 and fade == 0: return 0.0
    return (n + up) * TICK + 0.01


def _curve(name, v):
    """spessasynth_core's unipolar curves (modulator_curves.ts), v in 0..1."""
    if name == 'linear': return v
    if v <= 0: return 0.0
    if v >= 1: return 1.0
    if name == 'convex': return max(0.0, 1 + 400 / 960 * math.log10(v))
    return min(1.0, -400 / 960 * math.log10(1 - v))


def fit_halves(f, base, tol=1.0):
    """Fits f(cc) - base on each half of the controller (the other half and 64 stay at 0) as a sum of SF2 switch
    gated terms: a step plus slopes on linear, convex and concave curves of (cc - 64) / 64. Takes the smallest set
    (step always in) whose RMS is within tol of the best. Returns {'up': [[curve, amount], ..], 'down': ..} with
    curve 'step', 'linear', 'convex' or 'concave'."""
    import itertools
    import numpy as np
    out = {}
    for side, ccs in (('up', range(65, 128)), ('down', range(0, 64))):
        y = np.array([f(c) - base for c in ccs], dtype=float)
        # Small offsets from 64 are the common case, they count more
        w = np.array([1 + 3 * math.exp(-abs(c - 64) / 8) for c in ccs])
        if np.abs(y).max() < tol:
            out[side] = []
            continue
        cols = {'step': np.ones(len(ccs))}
        for name in ('linear', 'convex', 'concave'):
            cols[name] = np.array([math.copysign(_curve(name, abs(c - 64) / 64), c - 64) for c in ccs])
        fits = []
        for k in range(0, 3):
            for combo in itertools.combinations(('linear', 'convex', 'concave'), k):
                names = ('step',) + combo
                A = np.stack([cols[n] for n in names], axis=1)
                coef = np.round(np.linalg.lstsq(A * w[:, None], y * w, rcond=None)[0])
                rms = float(np.sqrt(np.mean(((A @ coef - y) * w) ** 2) / np.mean(w ** 2)))
                fits.append((rms, len(names), [[n, int(c)] for n, c in zip(names, coef) if c]))
        best = min(r for r, _, _ in fits)
        out[side] = min((f for f in fits if f[0] <= best + tol), key=lambda f: (f[1], f[0]))[2]
    return out


def eval_halves(fit, cc):
    """Value of a fit_halves() result at one controller value (for checks)."""
    if cc == 64: return 0.0
    t = fit['up' if cc > 64 else 'down']
    v = 0.0
    for name, amt in t:
        v += amt * (1.0 if name == 'step' else math.copysign(_curve(name, abs(cc - 64) / 64), cc - 64))
    return v


def lfo_for(el, direct_drum=False):
    """SF2 values for one element (el = its 0x4e bytes, None for drum notes that play a wave directly)."""
    if el is None:
        # Drum notes without a voice: fixed speed 0x30, no depth (FUN_10014900 drum branch)
        hz = rate_hz(52)
        return {'freq': round(hz_to_abs_cents(hz)), 'hz': hz}
    speed = el[0x07] & 63
    random_mode = bool(el[0x05] & 2)  # firmware random pitch steps, the measured pitch register already has them
    d = 0 if random_mode else el[0x0a]
    fade = el[0x09]
    hz = rate_hz(SPEED_IDX[speed])
    freq = hz_to_abs_cents(hz)
    depth = depth_cents(DEPTH_REG[min(127, d)]) if d else 0.0
    delay = effective_delay(el[0x08], fade, d)
    # SF2 LFO starts at its trough when the delay ends, the chip one at the start of the note (from 0, rising).
    # A whole number of periods keeps every element in phase with the mod LFO (mod wheel), which has no delay.
    if delay:
        delay = round(delay * hz) / hz
    out = {'freq': round(freq), 'hz': hz, 'depth': round(depth, 1), 'delay': seconds_to_tc(delay), 'delaySeconds': delay}
    # Rates under 0.25 Hz are no vibrato anyway, the floor keeps them from dominating the fit
    out['cc76'] = fit_halves(lambda c: hz_to_abs_cents(max(0.25, rate_hz(SPEED_IDX[speed_at(speed, c)]))), freq, 10)
    if not random_mode:
        out['cc77'] = fit_halves(lambda c: depth_cents(DEPTH_REG[min(127, depth_target(d, c)[0])]), depth)
        base_tc = out['delay']
        out['cc78'] = fit_halves(lambda c: seconds_to_tc(effective_delay(el[0x08], fade, max(d, 1), c)), base_tc, 30)
    return out


# Mod wheel (CC1): reg = max(element depth, PMOD_REG[CC1 * 80 >> 10]). The bank adds it on the mod LFO instead
# (no delay, the element's rate), so this is the linear modulator amount for the default modulator list.
def mod_wheel_amount():
    xs = [c / 128 for c in range(128)]
    ys = [depth_cents(PMOD_REG[c * 80 >> 10]) for c in range(128)]
    return round(sum(x * y for x, y in zip(xs, ys)) / sum(x * x for x in xs))
