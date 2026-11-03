"""Chip unit conversions, all for the 44.1 kHz reference."""
import math, struct
import pefile

from paths import DLL as _DLL
_pe = pefile.PE(_DLL, fast_load=True)
def _floats(va, n): return struct.unpack('<%df' % n, _pe.get_data(va - 0x10000000, 4 * n))
_C = 67108864.0
# Steps per 128-sample block (the chip applies twice the table value, measured)
ATTACK_STEP = [2 * int(_C / x) for x in _floats(0x100512f8, 128)]
DECAY_STEP = [2 * int(_C / x) for x in _floats(0x100514f8, 128)]
RELEASE_STEP = [2 * int(_C / x) for x in _floats(0x100516f8, 128)]
BLOCK = 128 / 44100.0
OCT_DB = 20 * math.log10(2)

def att_db(units):
    """Attenuation in dB for the 20-bit log units (0x10000 per octave), using the chip's piecewise curve."""
    if units >= 0xfffff: return 200.0
    e, m = units >> 16, (~units & 0xffff) | 0x10000
    return -20 * math.log10(m / (2 ** e * 131071.0))

def level_db(reg):
    """dB of a register value shifted << 12 (level, EG targets)."""
    return att_db(reg << 12)

def decay_db_per_s(rate, release=False):
    step = (RELEASE_STEP if release else DECAY_STEP)[rate & 0x7f]
    return step / 65536.0 * OCT_DB / BLOCK

def attack_time(rate, start_reg):
    """Seconds of the linear attack from level start_reg << 12 to 0 dB."""
    step = ATTACK_STEP[rate & 0x7f]
    return math.ceil((start_reg << 12) / step) * BLOCK if step else 0.0

def hold_time(rate):
    """Attack register bit 7 clear: hold at full level (counter 0x80000 at decay steps)."""
    step = DECAY_STEP[rate & 0x7f]
    return math.ceil(0x80000 / step) * BLOCK

def cutoff_hz(c, fs=44100.0):
    if c == 0: return 0.0
    a = (min(c, 0x7ff) + 0x800) << 8
    f = (a & 0xffff | 0x10000) / (2 ** (24 - (a >> 16)) * 255.0)
    return fs / math.pi * math.asin(min(1.0, f / 2))

def damping(r):
    x = (r + 4) * 0x2000
    e, m = x >> 16, (~x & 0xffff) | 0x10000
    return 2 * m / (2 ** e * 131071.0)

def hz_to_abscents(hz): return 1200 * math.log2(hz / 8.176)
def seconds_to_timecents(s): return -12000 if s <= 0.001 else round(1200 * math.log2(s))
