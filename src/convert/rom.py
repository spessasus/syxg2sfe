"""Section access for the voice table (SXGBIN41.TBL in the original)."""
import struct, os
from paths import VOICE_TBL, WAVE_PCM
HERE = os.path.dirname(os.path.abspath(__file__))
BIN = open(VOICE_TBL, 'rb').read()
WAVE = open(WAVE_PCM, 'rb').read()
_sizes = struct.unpack('<17I', BIN[0x20:0x64])
SEC = []
_o = 100
for _s in _sizes:
    SEC.append(_o)
    _o += _s
SIZE = list(_sizes)
def sec(i): return BIN[SEC[i]:SEC[i] + SIZE[i]]
def u16(b, o): return struct.unpack_from('<H', b, o)[0]

def name_prefix(s):
    """8 when the voice records of section s carry a name in front (the MU50 / MU80 wave ROM builds), else 0: the
    walk that ends exactly at the section end."""
    for name in (0, 8):
        o, end = SEC[s], SEC[s] + SIZE[s]
        while o + name + 80 <= end: o += name + (158 if BIN[o + name + 1] & 2 else 80)
        if o == end: return name
    raise ValueError('voice records of section %d match neither layout' % s)
