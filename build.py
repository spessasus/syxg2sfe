#!/usr/bin/env python3
"""Build the SFE bank of one S-YXG50 DLL.

python3 build.py [--dll input/syxg50.dll] [--output .] [--full] [--name "S-YXG50"]

Writes <dll name>.sf4 and <dll name>_vorbis.sf4 to --output (default: the current folder). The data of each DLL
(ROM tables, measurements, bank.json) lives in build/<dll name>/, so several DLLs can be built side by side.

Steps, each skipped when its output is already there (--full redoes all of them):
1. Extract the voice table and the wave data from the DLL (src/convert/extract_rom.py).
2. Measure every voice element and drum note in the DLL (under Wine on Linux; natively on Windows) (src/convert/collect.py, collect_vel.py, then collect_key.py for the filter EG at other keys). It
   needs the VST host (plus Wine on Linux) (src/host/harness.c, built into build/harness.exe with i686-w64-mingw32-gcc when
   missing or older than its source).
3. Fit the SF2 values (src/convert/convert.py), then write the banks (src/writer/main.ts, needs ffmpeg with
   libvorbis).
The filter map (src/convert/filtermap.py, the chip filter, the same in every DLL) goes to build/filter_map.json once,
--full redoes it.
"""
import argparse, json, os, shutil, subprocess, sys, time

WINDOWS = os.name == 'nt'


def find_gcc32():
    """A 32-bit MinGW gcc: on PATH, or (Windows) in an MSYS2 install (pacman -S mingw-w64-i686-gcc)."""
    exe = shutil.which('i686-w64-mingw32-gcc')
    if exe or not WINDOWS: return exe or 'i686-w64-mingw32-gcc'
    for root in (os.environ.get('MSYS2_ROOT'), r'C:\msys64', r'C:\msys32'):
        g = root and os.path.join(root, 'mingw32', 'bin', 'gcc.exe')
        if g and os.path.exists(g): return g
    sys.exit('No 32-bit MinGW gcc found: install MSYS2 and run "pacman -S mingw-w64-i686-gcc"')

ROOT = os.path.dirname(os.path.abspath(__file__))
CONVERT = os.path.join(ROOT, 'src', 'convert')
HOST = os.path.join(ROOT, 'src', 'host', 'harness.c')


def step(name, cmd, cwd=ROOT, env=None):
    print('== %s' % name, flush=True)
    t = time.time()
    subprocess.run(cmd, cwd=cwd, check=True, stdin=subprocess.DEVNULL, env=env)
    print('   done in %.0f s' % (time.time() - t), flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1], formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog='\n'.join(__doc__.split('\n')[2:]))
    ap.add_argument('--dll', '--input', default=os.path.join(ROOT, 'input', 'syxg50.dll'), help='the plugin DLL')
    ap.add_argument('--output', default=os.getcwd(), help='folder for the .sf4 files (default: current folder)')
    ap.add_argument('--full', action='store_true', help='redo every step, measurements included')
    ap.add_argument('--name', help='bank name (default: S-YXG50, plus the ROM header for other builds)')
    a = ap.parse_args()
    dll = os.path.abspath(a.dll)
    if not os.path.exists(dll): sys.exit('%s missing: copy the S-YXG50 VST2 DLL there' % dll)
    stem = os.path.splitext(os.path.basename(dll))[0]
    data = os.path.join(ROOT, 'build', stem)
    env = dict(os.environ, SYXG_DLL=dll, SYXG_DATA=data, PYTHONUTF8='1')
    py = sys.executable
    have = lambda f: os.path.exists(os.path.join(data, f))
    newer = lambda f, than: os.path.getmtime(f) > os.path.getmtime(than)

    # resources.json from before the wave base was stored there: the converter fell back to the original's base
    # (MU80 samples 327272 bytes off, past the end of the wave data)
    stale = have('resources.json') and 'waveBase' not in json.load(open(os.path.join(data, 'resources.json')))
    if a.full or stale or not have('voice.tbl') or newer(dll, os.path.join(data, 'voice.tbl')):
        step('ROM tables', [py, os.path.join(CONVERT, 'extract_rom.py')], env=env)
    if a.full or not os.path.exists(os.path.join(ROOT, 'build', 'filter_map.json')):
        step('filter map', [py, os.path.join(CONVERT, 'filtermap.py')], env=env)
    if a.full or not have('measure.json') or not have('measure_vel.json'):
        exe = os.path.join(ROOT, 'build', 'harness.exe')
        if not os.path.exists(exe) or newer(HOST, exe):
            # The VST2 host the DLL runs in (32-bit; under Wine on Linux)
            os.makedirs(os.path.dirname(exe), exist_ok=True)
            gcc = find_gcc32()
            # MinGW's gcc needs its own folder on PATH to find its DLLs and cc1
            step('build harness', [gcc, '-O2', '-Wall', '-o', exe, HOST],
                 env=dict(os.environ, PATH=os.path.dirname(os.path.abspath(gcc)) + os.pathsep + os.environ['PATH']))
        step('measure (slow)', [py, os.path.join(CONVERT, 'collect.py')], env=env)
        step('measure velocity 64', [py, os.path.join(CONVERT, 'collect_vel.py')], env=env)
    if a.full or not have('measure_key.json'):
        step('measure key ends', [py, os.path.join(CONVERT, 'collect_key.py')], env=env)
    step('fit voices', [py, os.path.join(CONVERT, 'convert.py')], env=env)
    header = json.load(open(os.path.join(data, 'resources.json')))['header']
    name = a.name or ('S-YXG50' if header.startswith('MU50 4MB') else 'S-YXG50 (%s)' % header)
    step('write banks', [shutil.which('npx') or 'npx', 'tsx', os.path.join('src', 'writer', 'main.ts'), data, os.path.abspath(a.output), stem, name], env=env)


if __name__ == '__main__':
    main()
