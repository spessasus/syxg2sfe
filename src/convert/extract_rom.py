"""Extract the voice table and the wave data (two RCDATA resources) from the DLL into the build folder as
voice.tbl and wave.pcm, plus resources.json with their names in the DLL. python3 src/convert/extract_rom.py
The original names them SXGBIN41.TBL / SXGWAVE4.TBL, other builds SXGMU50A1.TBL / SXGMU50A1.UPCM and so on.
The table is the resource whose header holds 17 section sizes that add up to its length, and it names the
wave resource at 0x10."""
import json, os, struct, sys
import pefile
from paths import DLL, DATA, VOICE_TBL, WAVE_PCM, RESOURCES


def resources(pe, addresses=None):
    """{name: bytes} of the named RCDATA entries. addresses, a dict, gets {name: address in the loaded DLL}."""
    out = {}
    for t in pe.DIRECTORY_ENTRY_RESOURCE.entries:
        for e in t.directory.entries:
            name = str(e.name) if e.name else str(e.id)
            for lang in e.directory.entries:
                d = lang.data.struct
                out[name] = pe.get_data(d.OffsetToData, d.Size)
                if addresses is not None: addresses[name] = pe.OPTIONAL_HEADER.ImageBase + d.OffsetToData
    return out


def find(res):
    """(table name, wave name) of the ROM resources."""
    for name, d in res.items():
        if len(d) > 0x64 and sum(struct.unpack('<17I', d[0x20:0x64])) + 100 == len(d):
            wave = d[0x10:0x20].split(b'\0')[0].decode('ascii').upper()
            if wave in res: return name, wave
    sys.exit('no voice table resource in %s' % DLL)


def main():
    addresses = {}
    res = resources(pefile.PE(DLL), addresses)
    table, wave = find(res)
    os.makedirs(DATA, exist_ok=True)
    open(VOICE_TBL, 'wb').write(res[table])
    open(WAVE_PCM, 'wb').write(res[wave])
    # The chip's wave pointer (reg 0x640) is an address in the loaded resource (0x1007e768 in the original)
    json.dump({'table': table, 'wave': wave, 'header': res[table][:0x10].split(b'\0')[0].decode('ascii'),
               'waveBase': addresses[wave]}, open(RESOURCES, 'w'))
    print('%s: %s (%d bytes), %s (%d bytes)' % (os.path.basename(DLL), table, len(res[table]), wave, len(res[wave])))


if __name__ == '__main__':
    main()
