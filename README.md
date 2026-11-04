# syxg2sfe

The ultimate XG soundfont generator,
brought to you by a million tokens and gallons of water wasted


This essentially converts `syxg50.dll`
into a soundfont, as accurately as it can. 
It uses SFe because SF2 isn't enough for it to play accurately.

Currently, you can use SFe with:
- [SpessaSynth](https://spessasus.github.io/SpessaSynth)
- [SpessaFont](https://spessasus.github.io/SpessaFont) - This one can convert to SF2 but it will break many things, including the filter.

Note that you need your own copy of the plugin. This repository does not ship it.

`spessasynth_core` 4.4.5 improved sfe with new features
to make this port more accurate (such as modEnv2 for pitch envelope).

This is vibecoded.
Text below is written by AI too. I only wrote this section.
And invented spessasynth, DMOD and the library used for writing.
But i can't take credit for this repo i guess `¯\_(ツ)_/¯`

## Requirements

Linux only. On Windows, use [WSL 2](https://learn.microsoft.com/windows/wsl/install) (for example Ubuntu) and
follow the same steps inside it. Please don't ask for a native Windows version, it will not come.

- Python 3 with numpy, scipy and pefile
- Wine with 32-bit support (the plugin is a 32-bit DLL)
- MinGW-w64 for 32-bit Windows (`i686-w64-mingw32-gcc`), to build the small VST host the plugin runs in
- Node.js 18 or newer and npm
- ffmpeg with libvorbis

Arch: `pacman -S python-numpy python-scipy python-pefile wine mingw-w64-gcc nodejs npm ffmpeg`

Debian / Ubuntu: `dpkg --add-architecture i386 && apt update`, then
`apt install python3-numpy python3-scipy python3-pefile wine wine32 gcc-mingw-w64-i686 nodejs npm ffmpeg`

## Usage

```sh
git clone <this repository> syxg2sfe && cd syxg2sfe
npm ci
cp /path/to/syxg50.dll input/
python3 build.py --dll input/syxg50.dll --output out
```

This writes `out/syxg50.sf4` and `out/syxg50_vorbis.sf4` (the same bank with longer samples in Ogg Vorbis).
The first run measures every voice in the plugin under Wine, which takes a while. The results are kept in
`build/syxg50/`, so later runs only redo the fitting and writing. `--full` starts from scratch. The slow steps use
3 worker processes, set `SYXG_JOBS` to change that (for example `SYXG_JOBS=8 python3 build.py ...`).

The banks use SFE generators that spessasynth_core 4.4.5 added (see Design choices). A player on an older
version ignores them, so pitch envelopes, second decays, delayed elements and drum notes play wrong there.

Supported plugin files (sha256):

| File           | Sound set              | sha256                                                             |
|----------------|------------------------|--------------------------------------------------------------------|
| `syxg50.dll`   | S-YXG50, MU50 4 MB ROM | `2f81ad9fd3fea46a950ab47486d5af721353989f54c67a6d731e29445d0f210d` |

Some experiments have been made with replacing the tables with higher quality one.
It may work, but it is not guaranteed.

Presets carry the full voice names (Grand Piano), instruments the 8 character names with the element number
(GrandPno 1).

Banks: XG voices on bank MSB 0 with the variation number as bank LSB (SFE's extended bank field), SFX voices on
MSB 64, drum kits on MSB 127, SFX kits on MSB 126, and GS copies of the kits as GM/GS drum presets (bank 128 in
SF2). The bank plays about 7 dB below the plugin, so that every voice keeps its level relative to the others.

## Design choices

The plugin is not parsed, it is measured. Its voice parameters go through firmware code that drives an emulated
sound chip, and the chip registers are what decides the sound. The converter runs the plugin under Wine in a small
VST host, plays every element of every voice at every key and at many velocities, logs the register writes, and
decodes them: sample address and loop, pitch, level, pan, cutoff and resonance, the amplitude, filter and pitch
envelopes, LFOs, and how controllers (velocity, CC71, CC74, CC76 to 78) change them. Those values are then fitted
to SF2 generators and modulators, one instrument per element, zones split where the sample or the key scaling
changes. Samples come straight from the wave ROM with the plugin's loop points, no resampling. The result is
checked against the plugin by rendering the same notes in both.

### Why SFE and not SF2

Plain SF2 cannot hold this bank:

- **Bank numbers.** An SF2 preset has one bank number. XG addresses voices by bank MSB and LSB: 40 variation
  voices sit on MSB 0 / LSB 64 and 42 SFX voices on MSB 64 / LSB 0, and one SF2 bank number cannot tell them
  apart. SFE's bank field carries MSB, LSB and the drum flag, so every XG bank maps 1:1.
- **Size limits.** SF2 counts zones, generators and modulators with 16-bit indexes (65,536 each). The bank has
  173,063 generators and 457,890 modulators. SFE extends the indexes.
- **Default modulators.** SF2's default modulators are fixed. The plugin differs (CC74 moves the cutoff half as
  far, the mod wheel vibrato has no delay, channel pressure does nothing), so the bank brings its own list.
- **Generators SF2 lacks.** An XG voice does more than the SF2 synthesis model, and SFE adds generators for it:
  - a second modulation envelope, so the pitch envelope gets its own (SF2 has one for both pitch and filter),
  - a second decay segment, XG envelopes decay to one level, then to another at their own rate,
  - ignoring note-off, which XG drum notes do (their envelope plays to the end even for very short notes),
  - a delayed voice start, XG elements can start up to 3.5 s after the note,
  - LFO fade-in, start phase and a sawtooth waveform,
  - a cutoff ceiling, the plugin's filter can not open past a point and velocity or the envelope saturate there.

### What SFE still can not express

- Modulators add in cents, from two sources. XG adds velocity, CC74, resonance and the filter envelope in its own
  register units and maps the sum to a frequency that bends near the top. The bank fits controller x velocity
  terms per zone instead, about 30 modulators per zone.
- XG envelopes move at fixed rates, so a shallower depth also finishes sooner. SF2 envelope segments have fixed
  times.
- The plugin takes the larger of the element's vibrato and the mod wheel's, SF2 modulators add them.
- Pitch that moves in random steps or in several sweeps (SFX voices) is more than one envelope can draw. Those
  elements get their measured pitch curve baked into sample copies, so the bank ends up around 40 MB from a 4 MB
  ROM.

## License

The converter is licensed under the Apache License 2.0, see [LICENSE](LICENSE).

## Legal notice

This project is not affiliated with, endorsed by or connected to Yamaha Corporation. Yamaha, S-YXG50, XG, MU50
and MU80 are trademarks of Yamaha Corporation, used here only to describe what the converter works with.

This repository contains no Yamaha software, samples, wave ROM data or voice parameters, and it distributes no
sound banks and no samples. The only Yamaha material in it is the voice name list (`src/convert/xg_voices.json`):
program numbers with the voice names of the public XG specification and of the MU80. Everything else the
converter produces is extracted from and measured in the plugin file you supply.

Only use it with a copy of the Yamaha plugin that you obtained legally, and within the terms of the license that
came with it. The banks it writes contain Yamaha's samples and voice data from your copy: they are for your own
use, do not share or redistribute them.
