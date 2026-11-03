"""Where the build of one DLL keeps its data. build.py sets SYXG_DLL and SYXG_DATA, without them the tools use
the original S-YXG50 (input/syxg50.dll, build/syxg50/)."""
import json, os

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
DLL = os.path.abspath(os.environ.get('SYXG_DLL') or os.path.join(ROOT, 'input', 'syxg50.dll'))
NAME = os.path.splitext(os.path.basename(DLL))[0]
DATA = os.path.abspath(os.environ.get('SYXG_DATA') or os.path.join(ROOT, 'build', NAME))
# The two ROM resources (extract_rom.py), named by role: the DLLs call them differently
VOICE_TBL = os.path.join(DATA, 'voice.tbl')
WAVE_PCM = os.path.join(DATA, 'wave.pcm')
RESOURCES = os.path.join(DATA, 'resources.json')
MEASURE = os.path.join(DATA, 'measure.json')
MEASURE_VEL = os.path.join(DATA, 'measure_vel.json')
BANK = os.path.join(DATA, 'bank.json')
# The chip filter is the same in every DLL, so its map (filtermap.py) is shared by all builds
FILTER_MAP = os.path.join(ROOT, 'build', 'filter_map.json')


def table_resource():
    """Resource name of the voice table inside the DLL (the harness patches it in memory)."""
    try:
        return json.load(open(RESOURCES))['table']
    except OSError:
        return 'SXGBIN41.TBL'


def wave_base():
    """Address of the wave data in the loaded DLL (the chip's wave pointer, reg 0x640, points into it)."""
    try:
        return json.load(open(RESOURCES))['waveBase']
    except (OSError, KeyError):
        return 0x1007e768
