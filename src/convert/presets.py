"""Enumerate the S-YXG50 presets (melodic banks, SFX, drum kits) and their voice records."""
import struct, json, os
from rom import *
HERE = os.path.dirname(os.path.abspath(__file__))
_t = struct.unpack('<%dH' % (SIZE[12] // 2), sec(12))
_s9 = sec(9)
_s8 = sec(8)
_idx15 = struct.unpack('<%dH' % (SIZE[15] // 2), sec(15))

def voice_addr(off):
    """BIN offset of a voice record from its sec 12 / sec 6 word offset."""
    return SEC[13] + off * 2 if off < 0x8000 else SEC[14] + (off - 0x8000) * 2

def elements(addr):
    n = 2 if BIN[addr + 1] & 2 else 1
    return [BIN[addr + 2 + e * 0x4e:addr + 2 + (e + 1) * 0x4e] for e in range(n)]

def wave_records(wave):
    """Multisample records of a wave: list of dicts."""
    s16 = sec(16)
    o = _idx15[wave]
    out = []
    while True:
        r = s16[o:o + 16]
        out.append({'flags': r[0], 'root': r[1], 'fine': struct.unpack('b', r[2:3])[0],
                    'attack': int.from_bytes(r[3:6], 'big'), 'loop': int.from_bytes(r[6:9], 'big'),
                    'addr': int.from_bytes(r[9:12], 'big'), 'format': r[12], 'keyLo': r[14], 'keyHi': r[15] & 0x7f})
        if r[15] & 0x80: break
        o += 16
    return out

def melodic():
    """(msb, lsb, program, voice offset) for every melodic preset that exists."""
    # Full and 8 character names: the XG spec voice list, with the MU80's own names where the spec renamed or
    # replaced a voice (generated, see the file)
    names = {(v['msb'], v['lsb'], v['program']): v for v in json.load(open(os.path.join(HERE, 'xg_voices.json')))}
    out = []
    for p in range(128): out.append((0, 0, p, _t[128 + p]))
    for lsb in range(1, 128):
        row = _s9[lsb]
        if row == 1: continue
        for p in range(128):
            if _t[row * 128 + p] != _t[128 + p]: out.append((0, lsb, p, _t[row * 128 + p]))
    # MSB 64 (SFX) has a row of its own: sec 8 gives it (45 in the original, 46 in the MU80 build, whose LSB rows
    # all moved up one for a drum part row)
    sfx = _s8[64]
    for p in range(128):
        if _t[sfx * 128 + p] != _t[p]: out.append((64, 0, p, _t[sfx * 128 + p]))
    res = []
    for msb, lsb, p, off in out:
        n = names.get((msb, lsb, p))
        # The MU50 / MU80 wave ROM builds name their voice records (8 characters), the original does not
        own = record_name(off)
        own = own.replace('**', '').strip() if own else None
        name = n['name'] if n else own or 'Voice %d-%d-%d' % (msb, lsb, p + 1)
        res.append({'msb': msb, 'lsb': lsb, 'program': p, 'voice': off, 'name': name,
                    'short': own or (n['short'] if n else name[:8])})
    return res

def record_name(off):
    """The 8 character name in front of a voice record, None when the table has none."""
    addr = voice_addr(off)
    s = 13 if off < 0x8000 else 14
    if not name_prefix(s): return None
    return BIN[addr - 8:addr].decode('latin-1').rstrip(' \x00') or None

# Kits from the kit index tables: sec 1 for MSB 127, sec 2 for MSB 126. Programs without a kit of their own hold
# the default (last entry). The MU50 wave ROM build numbers its kits one lower than the original (0x0a..0x14).
_s1, _s2 = sec(1), sec(2)
KITS = {(127, p): _s1[p] for p in range(128) if p == 0 or _s1[p] != _s1[127]}
KITS.update({(126, p): _s2[p] for p in range(128) if _s2[p] != _s2[127]})
_KIT_PROGRAM_NAMES = {(127, 0): 'Standard Kit', (127, 1): 'Standard Kit 2', (127, 8): 'Room Kit', (127, 16): 'Rock Kit',
                      (127, 24): 'Electro Kit', (127, 25): 'Analog Kit', (127, 32): 'Jazz Kit', (127, 40): 'Brush Kit',
                      (127, 48): 'Classic Kit', (126, 0): 'SFX Kit 1', (126, 1): 'SFX Kit 2'}
KIT_NAMES = {kit: _KIT_PROGRAM_NAMES.get(mp, 'Kit %d-%d' % mp) for mp, kit in KITS.items()}

def drum_notes(kit):
    """key -> (note record address, voice record address or None) for a kit."""
    s4 = SEC[4] + kit * 256
    s6 = struct.unpack('<%dH' % (SIZE[6] // 2), sec(6))
    out = {}
    for k in range(128):
        o = u16(BIN, s4 + k * 2)
        if o == 0xffff: continue
        rec = SEC[5] + o
        # Byte 0x10 = 0xff: the record carries its own wave (bytes 0x12..0x1d). Else byte 0x11 picks a voice.
        # sec 6 holds byte offsets into sec 13 (FUN_1000af80 adds them without scaling)
        out[k] = (rec, None if BIN[rec + 0x10] == 0xff else SEC[13] + s6[BIN[rec + 0x11]])
    return out

if __name__ == '__main__':
    m = melodic()
    print(len(m), 'melodic presets,', len({x['voice'] for x in m}), 'unique voices')
    for kit in sorted(set(KITS.values())): print(KIT_NAMES[kit], len(drum_notes(kit)), 'notes')
