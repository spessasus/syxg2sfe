import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {
    BasicInstrument,
    BasicPreset,
    BasicSample,
    BasicSoundBank,
    GeneratorTypes,
    Modulator,
    ModulatorControllerSources,
    ModulatorCurveTypes,
    ModulatorSource,
    SampleTypes
} from "spessasynth_core";
import type { BasicZone, GeneratorType, ModulatorCurveType } from "spessasynth_core";

interface BankSample {
    addr: number;
    attack: number;
    loop: number;
    bits: 8 | 16;
    root: number;
    name: string;
}

interface EnvelopeValues {
    attackVolEnv: number;
    holdVolEnv: number;
    decayVolEnv: number;
    sustainVolEnv: number;
    decay2VolEnv: number;
    sustain2VolEnv: number;
    releaseVolEnv: number;
}

interface BankZone {
    keyLo: number;
    keyHi: number;
    sample: number;
    scaleTuning: number;
    tuneCents: number;
    attenuation: number;
    attSlope: number;
    pan: number;
    filterFc: number;
    modEnvToFilterFc?: number;
    fcSlope: number;
    filterQ: number;
    centerKey: number;
    /**
     * Root key of the sample baked for this zone's key group (pitch curve)
     */
    bakeRoot: number;
    eg: EnvelopeValues;
    /**
     * Decay and release time change per key, timecents
     */
    decaySlope: number;
    releaseSlope: number;
    /**
     * Drum note that ignores note-off (XG Rcv Note Off)
     */
    ignoreNoteOff: boolean;
    /**
     * CC74 / CC71 cutoff response of this zone, when the element's zones play at different cutoffs
     */
    brightness?: Brightness;
    /**
     * Drum alternate group (0 = none)
     */
    exclusiveClass?: number;
    /**
     * Measured pitch curve, [seconds, cents relative to the steady pitch], baked into the sample
     */
    pitchCurve?: [number, number][];
}

type CurveFit = [string, number] | null;

/**
 * The DLL filter EG fitted to the modulation envelope (timecents, 0.1% sustain, cents).
 */
interface FilterEnvelope {
    delayModEnv: number;
    attackModEnv: number;
    holdModEnv: number;
    decayModEnv: number;
    sustainModEnv: number;
    releaseModEnv: number;
    modEnvToFilterFc: number;
    velocityDepth?: CurveFit;
}

/**
 * The DLL pitch EG fitted to the second modulation envelope (timecents, 0.1% sustain, cents).
 */
interface PitchEnvelope {
    delayModEnv2: number;
    attackModEnv2: number;
    holdModEnv2: number;
    decayModEnv2: number;
    sustainModEnv2: number;
    releaseModEnv2: number;
    modEnv2ToPitch: number;
}

interface BankInstrument {
    id: string;
    /**
     * Display name (the voice's 8 character name and the element, or the kit and the key)
     */
    name: string;
    zones: BankZone[];
    global: {
        /**
         * Seconds before the element sounds (element byte 0x44), 0 for none
         */
        ampDelay: number;
        /**
         * Amplitude LFO depth, cB from the loudest to the quietest point
         */
        ampLfo: number;
        /**
         * Filter LFO depth, cents from the highest to the lowest cutoff
         */
        filterLfo: number;
        /**
         * The element LFO is a sawtooth (else a triangle)
         */
        saw: boolean;
        /**
         * Cutoff ceiling in absolute cents (13500 = none) and how CC71 moves it
         */
        maxFc: number;
        maxFcCC71: ControllerFit;
        pitchEG: PitchEnvelope | null;
        velocity: CurveFit;
        velocity2?: CurveFit;
        velocityFc: CurveFit;
        velocityQ: CurveFit;
        filterEG: FilterEnvelope | null;
        cc71: { up: [number, number]; down: [number, number] };
        brightness: Brightness | null;
        lfo: ElementLFO;
    };
}

/**
 * One half of a controller response: a step and curve terms, each gated to its half by a switch source.
 */
type ControllerHalf = ["step" | "linear" | "convex" | "concave", number][];

interface ControllerFit {
    up: ControllerHalf;
    down: ControllerHalf;
}

/**
 * CC74 and CC71 cutoff response at one cutoff (src/convert/convert.py brightness_fits)
 */
interface Brightness {
    /**
     * CC74 cutoff curves per half, plus CC74 curves times a curve of 1 - velocity / 128 (vel)
     */
    fc: ControllerFit & { vel?: [CC74Curve, "linear" | "convex" | "concave", number][] };
    att: ControllerFit;
    cc71: ControllerFit;
    cross71: [CC74Curve, "switch" | "linear" | "convex", number][];
}

type CC74Curve = "stepUp" | "stepDown" | "linear" | "convex" | "concave";

/**
 * The element LFO (src/convert/lfo.py). Frequency in absolute cents, delay in timecents, depth in cents.
 */
interface ElementLFO {
    freq: number;
    depth?: number;
    delay?: number;
    fade?: number;
    /**
     * Degrees the chip LFO has run when the vibrato LFO starts
     */
    phase?: number;
    cc76?: ControllerFit;
    cc77?: ControllerFit;
    cc78?: ControllerFit;
}

interface BankPreset {
    name: string;
    msb: number;
    lsb: number;
    program: number;
    drum: boolean;
    /**
     * key: drum note, velLo / velHi: the element's velocity range (velocity switched voices)
     */
    elements: { instrument: number; key?: number; velLo?: number; velHi?: number }[];
}

interface BankDescription {
    samples: BankSample[];
    instruments: BankInstrument[];
    presets: BankPreset[];
}

// tsx src/writer/main.ts <build folder> <output folder> <file name> <bank name>, called by build.py. The build folder
// holds wave.pcm (extract_rom.py) and bank.json (convert.py).
const [dataDir, outDir, fileName, bankName] = process.argv.slice(2);
if (!bankName) {
    console.error("usage: tsx src/writer/main.ts <build folder> <output folder> <file name> <bank name>");
    process.exit(1);
}
const WAVE = fs.readFileSync(path.join(dataDir, "wave.pcm"));
const bank = JSON.parse(fs.readFileSync(path.join(dataDir, "bank.json"), "utf8")) as BankDescription;
fs.mkdirSync(outDir, { recursive: true });
const outFile = path.join(outDir, `${fileName}.sf4`);
const vorbisFile = path.join(outDir, `${fileName}_vorbis.sf4`);

const CURVES: Record<string, ModulatorCurveType> = {
    linear: ModulatorCurveTypes.linear,
    concave: ModulatorCurveTypes.concave,
    convex: ModulatorCurveTypes.convex
};

/**
 * Creates a modulator source, the declaration file hides the constructor arguments.
 */
function source(
    index: number,
    curveType: ModulatorCurveType,
    isNegative = false,
    isCC = false,
    isBipolar = false
) {
    const s = new ModulatorSource();
    s.index = index as ModulatorSource["index"];
    s.curveType = curveType;
    s.isNegative = isNegative;
    s.isCC = isCC;
    s.isBipolar = isBipolar;
    return s;
}

/**
 * Decodes one ROM wave: 8-bit unsigned (offset 0x80) or 16-bit little endian unsigned (offset 0x8000).
 * The record address is the loop start, the attack part sits before it.
 */
function decodeSample(s: BankSample) {
    const length = s.attack + s.loop;
    const data = new Float32Array(length);
    if (s.bits === 8) {
        const start = s.addr - s.attack;
        for (let i = 0; i < length; i++) {
            data[i] = (WAVE[start + i] - 0x80) / 128;
        }
    } else {
        const start = s.addr - s.attack * 2;
        for (let i = 0; i < length; i++) {
            data[i] = (WAVE.readUInt16LE(start + i * 2) - 0x8000) / 32768;
        }
    }
    return data;
}

/**
 * Renders a zone's sample along its measured pitch curve, in output time at the zone's center key, so the
 * baked sample plays at its own rate there (root key = center key, no tuning). After the curve it keeps the
 * last pitch for one more pass of the ROM loop and loops that.
 */
function bakePitch(s: BankSample, z: BankZone) {
    const data = decodeSample(s);
    const curve = z.pitchCurve!;
    const base = (z.scaleTuning / 100) * (z.centerKey - s.root) * 100 + z.tuneCents;
    const step = (cents: number) => 2 ** ((base + cents) / 1200);
    const curveEnd = Math.round(curve[curve.length - 1][0] * 44_100);
    const lastStep = step(curve[curve.length - 1][1]);
    // Where the curve leaves the sample data: past its end the last pitch holds until the data reaches its loop,
    // then one pass of the loop is baked and looped
    let endPos = 0;
    for (let i = 0, c = 0; i < curveEnd; i++) {
        while (c + 1 < curve.length && curve[c + 1][0] * 44_100 <= i) c++;
        endPos += step(curve[c][1]);
    }
    const toLoop = s.loop > 0 && endPos < s.attack ? Math.ceil((s.attack - endPos) / lastStep) : 0;
    const loopStart = curveEnd + toLoop;
    // A one-shot sample plays on at the last pitch until its data ends (the measured curve stops at 2 s, the
    // DLL does not), the loop below stops there
    const tail = s.loop > 0 ? toLoop + Math.max(1, Math.round(s.loop / lastStep)) : Math.ceil(data.length / lastStep);
    const length = curveEnd + tail;
    const out = new Float32Array(length);
    let pos = 0;
    let c = 0;
    let i = 0;
    for (; i < length; i++) {
        // The DLL holds each pitch write until the next one
        while (c + 1 < curve.length && curve[c + 1][0] * 44_100 <= i) c++;
        if (pos >= data.length) {
            if (s.loop === 0) break;
            pos = s.attack + ((pos - s.attack) % s.loop);
        }
        const p0 = Math.floor(pos);
        const p1 = p0 + 1 < data.length ? p0 + 1 : s.loop > 0 ? s.attack : p0;
        out[i] = data[p0] + (data[p1] - data[p0]) * (pos - p0);
        pos += i < curveEnd ? step(curve[c][1]) : lastStep;
    }
    const looped = s.loop > 0 && i === length;
    return { data: out.subarray(0, i), loopStart: looped ? loopStart : 0, loopEnd: looped ? i : 0 };
}

const soundBank = new BasicSoundBank("sfe");
soundBank.soundBankInfo.name = bankName;
soundBank.soundBankInfo.engineer = "syxg2sfe";

// Default modulators (DMOD), changed where the DLL differs from spessasynth_core's defaults:
// - CC74 (brightness) moves the cutoff 16 DLL units (75 cents) per step, so +-4800 cents instead of +-9600.
// - CC71 (harmonic content) depends on each element's own resonance, every instrument overrides it.
// - The mod wheel adds up to +-48 cents (reg 0x481 up to 58) without the element's LFO delay, so it goes to the
//   mod LFO, which runs at the element's rate with no delay. The element's own vibrato uses the vibrato LFO.
// - Channel pressure does not touch the LFO in the DLL.
for (const m of soundBank.defaultModulators) {
    const src = m.primarySource;
    if (m.destination === GeneratorTypes.vibLfoToPitch) {
        if (src.isCC && src.index === 1) {
            m.destination = GeneratorTypes.modLfoToPitch;
            m.transformAmount = 48;
        } else if (!src.isCC && src.index === ModulatorControllerSources.channelPressure) {
            m.transformAmount = 0;
        }
    }
    if (!src.isCC) continue;
    if (src.index === 74 && m.destination === GeneratorTypes.initialFilterFc) m.transformAmount = 4800;
}
soundBank.customDefaultModulators = true;

const samples = bank.samples.map((s) => {
    const loopStart = s.attack;
    const loopEnd = s.loop > 0 ? s.attack + s.loop : s.attack;
    const sample = new BasicSample(
        s.name,
        44_100,
        s.root,
        0,
        SampleTypes.monoSample,
        loopStart,
        loopEnd
    );
    sample.setAudioData(decodeSample(s), 44_100);
    return sample;
});
soundBank.addSamples(...samples);

// Measured DLL attenuation written as 0 cB. Zone levels stay exact relative to each other. The bank plays
// about 7 dB quieter than the DLL (the median over all voices), since spessasynth_core cannot boost a zone
// above 0 cB. 19 cB is the 1st percentile of all zones, so about 1% of zones clamp to 0 cB.
const LEVEL_REFERENCE_CB = 19;
// spessasynth_core multiplies the initialAttenuation generator by 0.4 (the EMU quirk), modulators are not scaled
const EMU_ATTENUATION_FACTOR = 0.4;

/**
 * Sets a generator without the spec range validation.
 */
function gen(zone: BasicZone, type: GeneratorType, value: number) {
    zone.setGenerator(type, Math.round(value), false);
}

/**
 * Adds a negative unipolar velocity modulator fitted by the converter (amount at velocity 0).
 */
function addVelocityModulator(
    zone: BasicZone,
    fit: CurveFit | undefined,
    destination: GeneratorType,
    sign: number
) {
    if (!fit) return;
    zone.modulators.push(
        new Modulator(
            source(ModulatorControllerSources.noteOnVelocity, CURVES[fit[0]], true),
            source(ModulatorControllerSources.noController, 0),
            destination,
            sign * fit[1],
            0
        )
    );
}

/**
 * Adds a controller response fitted per half of the controller (step and curve terms, switch gated), so 64
 * leaves the zone as it is.
 */
function addControllerResponse(
    zone: BasicZone,
    cc: number,
    fit: ControllerFit | undefined,
    destinations: GeneratorType[]
) {
    if (!fit) return;
    const none = source(ModulatorControllerSources.noController, 0);
    for (const [half, terms] of [
        ["up", fit.up],
        ["down", fit.down]
    ] as const) {
        const gate = source(cc, ModulatorCurveTypes.switch, half === "down", true);
        for (const [curve, amount] of terms) {
            for (const d of destinations) {
                zone.modulators.push(
                    curve === "step"
                        ? new Modulator(gate, none, d, amount, 0)
                        : new Modulator(source(cc, CURVES[curve], false, true, true), gate, d, amount, 0)
                );
            }
        }
    }
}

/**
 * CC74 and CC71 cutoff curves of one element at one cutoff (converter brightness_fits)
 */
function addBrightness(zone: BasicZone, b: Brightness) {
    addControllerResponse(zone, 74, b.fc, [GeneratorTypes.initialFilterFc]);
    // A soft note starts further below the DLL's cutoff ceiling, so CC74 opens it more
    for (const [kind, velocityCurve, amount] of b.fc.vel ?? []) {
        const soft = source(ModulatorControllerSources.noteOnVelocity, CURVES[velocityCurve], true);
        zone.modulators.push(new Modulator(cc74Source(kind), soft, GeneratorTypes.initialFilterFc, amount, 0));
    }
    addControllerResponse(zone, 74, b.att, [GeneratorTypes.initialAttenuation]);
    addControllerResponse(zone, 71, b.cc71, [GeneratorTypes.initialFilterFc]);
    // How CC74 maps at the resonance CC71 sets, on top of the curves above (CC71 as the second source)
    const cc71Sources = {
        switch: source(71, ModulatorCurveTypes.switch, false, true),
        linear: source(71, ModulatorCurveTypes.linear, false, true, true),
        convex: source(71, ModulatorCurveTypes.convex, false, true, true)
    };
    for (const [kind, secondary, amount] of b.cross71) {
        zone.modulators.push(
            new Modulator(cc74Source(kind), cc71Sources[secondary], GeneratorTypes.initialFilterFc, amount, 0)
        );
    }
}

/**
 * A CC74 source for the velocity and CC71 terms: unipolar switches for the steps (bipolar ones read -1 at 64),
 * bipolar curves otherwise
 */
function cc74Source(kind: CC74Curve) {
    return kind === "stepUp" || kind === "stepDown"
        ? source(74, ModulatorCurveTypes.switch, kind === "stepDown", true)
        : source(74, CURVES[kind], false, true, true);
}

const bakedSamples = new Array<BasicSample>();
const pitchBakes = new Map<string, BasicSample>();
const instruments = bank.instruments.map((inst) => {
    const instrument = new BasicInstrument();
    instrument.name = inst.name;
    const g = instrument.globalZone;
    // Velocity to attenuation replaces the default modulator only when the curve is concave (identical source).
    // Other curves would add up with it, so the default one gets a zero amount override.
    addVelocityModulator(g, inst.global.velocity, GeneratorTypes.initialAttenuation, 1);
    // A second curve where one does not fit (convert.py fit_velocity_pair), always another curve than the first
    addVelocityModulator(g, inst.global.velocity2, GeneratorTypes.initialAttenuation, 1);
    const velocityCurves = [inst.global.velocity, inst.global.velocity2].filter((f) => f).map((f) => f![0]);
    if (velocityCurves.length && !velocityCurves.includes("concave")) {
        g.modulators.push(
            new Modulator(
                source(ModulatorControllerSources.noteOnVelocity, ModulatorCurveTypes.concave, true),
                source(ModulatorControllerSources.noController, 0),
                GeneratorTypes.initialAttenuation,
                0,
                0
            )
        );
    }
    // Filter cutoff and resonance drop as the velocity falls
    addVelocityModulator(g, inst.global.velocityFc, GeneratorTypes.initialFilterFc, -1);
    addVelocityModulator(g, inst.global.velocityQ, GeneratorTypes.initialFilterQ, -1);
    // CC71. The upper half uses the default resonant modulator's exact source (bipolar CC71, no secondary), so it
    // overrides it and spessasynth_core adds back half the added Q as gain, keeping the passband level. That
    // gain is applied after the attenuation clamp, so it can boost. The DLL needs a bit more gain than Q / 2, the
    // rest goes to attenuation. A second modulator (switch secondary) reshapes the lower half.
    const cc71 = source(71, ModulatorCurveTypes.linear, false, true, true);
    const none = source(ModulatorControllerSources.noController, 0);
    const upper = source(71, ModulatorCurveTypes.switch, false, true);
    const lower = source(71, ModulatorCurveTypes.switch, true, true);
    const { up, down } = inst.global.cc71;
    g.modulators.push(new Modulator(cc71, none, GeneratorTypes.initialFilterQ, up[0], 0));
    const extraGain = up[1] - up[0] / 2;
    if (Math.round(extraGain)) {
        g.modulators.push(
            new Modulator(cc71, upper, GeneratorTypes.initialAttenuation, -Math.round(extraGain), 0)
        );
    }
    if (down[0] !== up[0]) {
        g.modulators.push(new Modulator(cc71, lower, GeneratorTypes.initialFilterQ, down[0] - up[0], 0));
    }
    if (down[1]) g.modulators.push(new Modulator(cc71, lower, GeneratorTypes.initialAttenuation, -down[1], 0));
    // Element LFO. The chip runs one LFO per element for pitch, filter and amplitude, a triangle or a sawtooth that
    // starts at 0 and rising with the element (like the core's LFOs at their default phase). The vibrato LFO
    // carries the element's own pitch depth with its delay and fade-in, the mod LFO (same rate, no delay) the mod
    // wheel and the amplitude and filter LFO. CC76/77/78 move rate, depth and delay like the firmware does,
    // fitted per half of the controller as switch gated terms, so 64 leaves the element as it is.
    const lfo = inst.global.lfo;
    gen(g, GeneratorTypes.freqVibLFO, lfo.freq);
    gen(g, GeneratorTypes.freqModLFO, lfo.freq);
    if (inst.global.saw) {
        gen(g, GeneratorTypes.vibLfoWaveform, 1);
        gen(g, GeneratorTypes.modLfoWaveform, 1);
    }
    if (lfo.depth) gen(g, GeneratorTypes.vibLfoToPitch, lfo.depth);
    if (lfo.delay !== undefined && lfo.delay > -12_000) gen(g, GeneratorTypes.delayVibLFO, lfo.delay);
    if (lfo.fade !== undefined && lfo.fade > -12_000) gen(g, GeneratorTypes.fadeVibLFO, lfo.fade);
    // The core LFO starts at its delay, the chip one with the element: start where the chip LFO is by then
    if (lfo.phase) gen(g, GeneratorTypes.vibLfoStartPhase, lfo.phase);
    // Amplitude and filter LFO: the DLL attenuates from 0 up to the depth and back (triangle) or up and resets
    // (sawtooth), and lowers the cutoff along the same shape. The zones hold half of it and the mod LFO swings
    // around that. A triangle started at its peak (90 degrees) falls like the DLL's attenuation rises, a sawtooth
    // started at 180 degrees rises from -1, so its depths turn negative.
    const { ampLfo, filterLfo, saw } = inst.global;
    if (ampLfo || filterLfo) gen(g, GeneratorTypes.modLfoStartPhase, saw ? 180 : 90);
    if (ampLfo) gen(g, GeneratorTypes.modLfoToVolume, ((saw ? -1 : 1) * ampLfo) / 2);
    if (filterLfo) gen(g, GeneratorTypes.modLfoToFilterFc, ((saw ? -1 : 1) * filterLfo) / 2);
    addControllerResponse(g, 76, lfo.cc76, [GeneratorTypes.freqVibLFO, GeneratorTypes.freqModLFO]);
    addControllerResponse(g, 77, lfo.cc77, [GeneratorTypes.vibLfoToPitch]);
    addControllerResponse(g, 78, lfo.cc78, [GeneratorTypes.delayVibLFO]);
    // CC74 follows the element's own cutoff map, the default modulator gets a zero override. The curves sit on
    // the global zone or on each zone (when the zones play at different cutoffs). Without curves the default
    // modulator stays.
    const hasCC74 = (b: Brightness | null | undefined) =>
        !!b && b.fc.up.length + b.fc.down.length + (b.fc.vel?.length ?? 0) + b.att.up.length + b.att.down.length + b.cross71.length > 0;
    if (hasCC74(inst.global.brightness) || inst.zones.some((z) => hasCC74(z.brightness))) {
        g.modulators.push(
            new Modulator(source(74, ModulatorCurveTypes.linear, false, true, true), none, GeneratorTypes.initialFilterFc, 0, 0)
        );
    }
    if (inst.global.brightness) addBrightness(g, inst.global.brightness);
    // Filter EG, measured on the long note at the element's center key
    const feg = inst.global.filterEG;
    if (feg) {
        gen(g, GeneratorTypes.delayModEnv, feg.delayModEnv);
        gen(g, GeneratorTypes.attackModEnv, feg.attackModEnv);
        gen(g, GeneratorTypes.holdModEnv, feg.holdModEnv);
        gen(g, GeneratorTypes.decayModEnv, feg.decayModEnv);
        gen(g, GeneratorTypes.sustainModEnv, feg.sustainModEnv);
        gen(g, GeneratorTypes.releaseModEnv, feg.releaseModEnv);
        gen(g, GeneratorTypes.modEnvToFilterFc, feg.modEnvToFilterFc);
        // The DLL scales the filter EG depth with velocity
        addVelocityModulator(g, feg.velocityDepth, GeneratorTypes.modEnvToFilterFc, -1);
    }
    // Pitch EG on the second modulation envelope, measured on the same long note
    const peg = inst.global.pitchEG;
    if (peg) {
        gen(g, GeneratorTypes.delayModEnv2, peg.delayModEnv2);
        gen(g, GeneratorTypes.attackModEnv2, peg.attackModEnv2);
        gen(g, GeneratorTypes.holdModEnv2, peg.holdModEnv2);
        gen(g, GeneratorTypes.decayModEnv2, peg.decayModEnv2);
        gen(g, GeneratorTypes.sustainModEnv2, peg.sustainModEnv2);
        gen(g, GeneratorTypes.releaseModEnv2, peg.releaseModEnv2);
        gen(g, GeneratorTypes.modEnv2ToPitch, peg.modEnv2ToPitch);
    }
    // Cutoff ceiling of the DLL filter at the element's resonance, CC71 moves it with the resonance
    if (inst.global.maxFc < 13_500) gen(g, GeneratorTypes.maxFilterFc, inst.global.maxFc);
    addControllerResponse(g, 71, inst.global.maxFcCC71, [GeneratorTypes.maxFilterFc]);
    // Element delay (byte 0x44): the DLL starts the sample and the amplitude EG this late
    if (inst.global.ampDelay) gen(g, GeneratorTypes.delayVoice, 1200 * Math.log2(inst.global.ampDelay));
    inst.zones.forEach((z) => {
        let sample = samples[z.sample];
        let pitchBaked = false;
        if (z.pitchCurve) {
            // Zones of one key window (convert.py bake_windows) share one baked sample, baked at the window's middle
            // key: other keys play it faster or slower, which also stretches the curve in time.
            const bakeKey = [z.sample, z.scaleTuning, z.tuneCents, z.bakeRoot, JSON.stringify(z.pitchCurve)].join("|");
            let shared = pitchBakes.get(bakeKey);
            if (!shared) {
                const s = bank.samples[z.sample];
                const baked = bakePitch(s, { ...z, centerKey: z.bakeRoot });
                shared = new BasicSample(
                    `${s.name} p${z.bakeRoot}`,
                    44_100,
                    z.bakeRoot,
                    0,
                    SampleTypes.monoSample,
                    baked.loopStart,
                    baked.loopEnd
                );
                shared.setAudioData(baked.data, 44_100);
                if (baked.loopEnd === 0) shared.loopEnd = baked.data.length;
                bakedSamples.push(shared);
                pitchBakes.set(bakeKey, shared);
            }
            sample = shared;
            pitchBaked = true;
        }
        const loops = pitchBaked ? sample.loopEnd > sample.loopStart && sample.loopStart > 0 : bank.samples[z.sample].loop > 0;
        const zone = instrument.createZone(sample);
        zone.keyRange = { min: z.keyLo, max: z.keyHi };
        // A pitch baked sample already plays at the right pitch at its root key (its key window's middle key, the
        // zones sharing it have the same tuning)
        const tune = pitchBaked ? 0 : z.tuneCents;
        const coarse = Math.round(tune / 100);
        gen(zone, GeneratorTypes.coarseTune, coarse);
        gen(zone, GeneratorTypes.fineTune, tune - coarse * 100);
        if (z.scaleTuning !== 100) {
            gen(zone, GeneratorTypes.scaleTuning, z.scaleTuning);
        }
        // Level and cutoff follow the key inside a zone (the converter's slopes per key, centered on the center
        // key). A bipolar key number modulator adds slope * (key - 64), so the generator holds the value at key
        // 64. The synth clamps the attenuation after adding the modulators, the same clamp as for a zone without
        // a slope. The amount is 16 bits; a steeper slope (only in zones of a few keys) is left out.
        const span = (z.keyHi - z.keyLo) / 2;
        const keySlope = (slope: number, min: number) =>
            Math.abs(slope * span) >= min && Math.abs(slope * 64) < 32_767 ? slope : 0;
        const attSlope = keySlope(z.attSlope, 2);
        const fcSlope = keySlope(z.fcSlope, 5);
        gen(
            zone,
            GeneratorTypes.initialAttenuation,
            attSlope
                ? (z.attenuation - LEVEL_REFERENCE_CB - attSlope * (z.centerKey - 64)) / EMU_ATTENUATION_FACTOR
                : Math.max(0, z.attenuation - LEVEL_REFERENCE_CB) / EMU_ATTENUATION_FACTOR
        );
        if (z.pan !== 0) gen(zone, GeneratorTypes.pan, z.pan);
        if (z.exclusiveClass) gen(zone, GeneratorTypes.exclusiveClass, z.exclusiveClass);
        // Generators are 16 bit. The cutoff can sit above 13500 on purpose (the synth only caps the sum), but must
        // not wrap
        gen(zone, GeneratorTypes.initialFilterFc, Math.min(20_000, z.filterFc - fcSlope * (z.centerKey - 64)));
        if (z.modEnvToFilterFc !== undefined) gen(zone, GeneratorTypes.modEnvToFilterFc, z.modEnvToFilterFc);
        if (z.brightness) addBrightness(zone, z.brightness);
        for (const [slope, destination] of [
            [attSlope, GeneratorTypes.initialAttenuation],
            [fcSlope, GeneratorTypes.initialFilterFc]
        ] as const) {
            if (!slope) continue;
            zone.modulators.push(
                new Modulator(
                    source(ModulatorControllerSources.noteOnKeyNum, ModulatorCurveTypes.linear, false, false, true),
                    source(ModulatorControllerSources.noController, 0),
                    destination,
                    Math.round(slope * 64),
                    0
                )
            );
        }
        if (z.filterQ > 0) gen(zone, GeneratorTypes.initialFilterQ, z.filterQ);
        // XG drum notes can ignore note-off, their envelope plays to its end
        if (z.ignoreNoteOff) gen(zone, GeneratorTypes.ignoreNoteOff, 1);
        gen(zone, GeneratorTypes.attackVolEnv, z.eg.attackVolEnv);
        gen(zone, GeneratorTypes.holdVolEnv, z.eg.holdVolEnv);
        // The DLL's decay and release rates follow the key. keyNumToVolEnvDecay adds (60 - key) * value to both
        // decays, the release gets a key number modulator like the level (no SF2 generator for it). The generators
        // hold the value at key 60 (64 for the release), the core clamps that to the generator range before the key
        // term is added. A zone of a few keys can sit on a rate breakpoint (Slap Bass 2 0/0/37 keys 85..86: -386
        // per key, the decay stored at key 60 came to 15588 and played as -2036, 13 dB quiet), so slopes go on
        // zones of 3 keys or more and only while the stored values stay in range.
        const inRange = (...values: number[]) => values.every((v) => v >= -12_000 && v <= 8000);
        const decaySlope =
            span >= 1 &&
            Math.abs(z.decaySlope) >= 1 &&
            inRange(
                z.eg.decayVolEnv + z.decaySlope * (60 - z.centerKey),
                z.eg.sustain2VolEnv ? z.eg.decay2VolEnv + z.decaySlope * (60 - z.centerKey) : 0
            )
                ? z.decaySlope
                : 0;
        const releaseSlope =
            Math.abs(z.releaseSlope * span) >= 10 && inRange(z.eg.releaseVolEnv - z.releaseSlope * (z.centerKey - 64))
                ? z.releaseSlope
                : 0;
        gen(zone, GeneratorTypes.decayVolEnv, z.eg.decayVolEnv + decaySlope * (60 - z.centerKey));
        if (decaySlope) gen(zone, GeneratorTypes.keyNumToVolEnvDecay, -decaySlope);
        gen(zone, GeneratorTypes.sustainVolEnv, z.eg.sustainVolEnv);
        // XG decay 2: from the decay 1 level (sustainVolEnv) to the decay 2 level at its own rate
        if (z.eg.sustain2VolEnv) {
            gen(zone, GeneratorTypes.decay2VolEnv, z.eg.decay2VolEnv + decaySlope * (60 - z.centerKey));
            gen(zone, GeneratorTypes.sustain2VolEnv, z.eg.sustain2VolEnv);
        }
        gen(zone, GeneratorTypes.releaseVolEnv, z.eg.releaseVolEnv - releaseSlope * (z.centerKey - 64));
        if (releaseSlope) {
            zone.modulators.push(
                new Modulator(
                    source(ModulatorControllerSources.noteOnKeyNum, ModulatorCurveTypes.linear, false, false, true),
                    source(ModulatorControllerSources.noController, 0),
                    GeneratorTypes.releaseVolEnv,
                    Math.round(releaseSlope * 64),
                    0
                )
            );
        }
        if (loops) {
            gen(zone, GeneratorTypes.sampleModes, 1);
        }
    });
    return instrument;
});
soundBank.addSamples(...bakedSamples);
soundBank.addInstruments(...instruments);

for (const p of bank.presets) {
    const preset = new BasicPreset(soundBank);
    preset.name = p.name;
    preset.program = p.program;
    preset.bankMSB = p.msb;
    preset.bankLSB = p.lsb;
    for (const e of p.elements) {
        const zone = preset.createZone(instruments[e.instrument]);
        if (e.key !== undefined) zone.keyRange = { min: e.key, max: e.key };
        if (e.velLo !== undefined && e.velHi !== undefined) zone.velRange = { min: e.velLo, max: e.velHi };
    }
    soundBank.addPresets(preset);
}

// GS drum aliases (isGMGSDrum, the SF2 bank 128). The XG kits already use the GS program numbers, SFX 1 takes
// the GS SFX set slot (56) since program 0 is the Standard Kit there. SFX 2 has no GS slot.
for (const p of bank.presets) {
    if (!p.drum) continue;
    const program = p.msb === 127 ? p.program : p.program === 0 ? 56 : -1;
    if (program < 0) continue;
    const preset = new BasicPreset(soundBank);
    preset.name = `${p.name} GS`;
    preset.program = program;
    preset.isGMGSDrum = true;
    for (const e of p.elements) {
        const zone = preset.createZone(instruments[e.instrument]);
        if (e.key !== undefined) zone.keyRange = { min: e.key, max: e.key };
    }
    soundBank.addPresets(preset);
}
soundBank.flush();

const out = soundBank.writeSFE();
fs.writeFileSync(outFile, new Uint8Array(out));
console.log(
    `Wrote ${outFile}: ${samples.length} samples (+${bakedSamples.length} baked), ${instruments.length} instruments, ${soundBank.presets.length} presets, ${(out.byteLength / 1048576).toFixed(1)} MB`
);

/**
 * Encodes mono float audio to Ogg Vorbis with ffmpeg (libvorbis).
 */
function encodeVorbis(data: Float32Array, sampleRate: number) {
    return new Promise<Uint8Array>((resolve, reject) => {
        const ff = spawn(
            "ffmpeg",
            ["-v", "error", "-f", "f32le", "-ar", String(sampleRate), "-ac", "1", "-i", "pipe:0"]
                .concat(["-c:a", "libvorbis", "-q:a", "8", "-f", "ogg", "pipe:1"]),
            { stdio: ["pipe", "pipe", "inherit"] }
        );
        const chunks = new Array<Buffer>();
        ff.stdout.on("data", (c: Buffer) => chunks.push(c));
        ff.on("error", reject);
        ff.on("close", (code) => {
            if (code === 0) resolve(new Uint8Array(Buffer.concat(chunks)));
            else reject(new Error(`ffmpeg exited with ${code}`));
        });
        ff.stdin.end(Buffer.from(data.buffer, data.byteOffset, data.byteLength));
    });
}

// Vorbis version: only samples of 0.25 s or longer. Short ones are mostly single-cycle loops, where the
// codec would smear the loop and save almost nothing.
const MIN_VORBIS_SECONDS = 0.25;
const long = soundBank.samples.filter(
    (s) => s.getAudioData().length >= MIN_VORBIS_SECONDS * s.sampleRate
);
// ffmpeg processes at once (SYXG_JOBS, see src/convert/paths.py)
const WORKERS = Math.max(1, Number(process.env.SYXG_JOBS) || 3);
for (let i = 0; i < long.length; i += WORKERS) {
    const batch = long.slice(i, i + WORKERS);
    const encoded = await Promise.all(
        batch.map((s) => encodeVorbis(s.getAudioData(), s.sampleRate))
    );
    batch.forEach((s, j) => s.setCompressedData(encoded[j]));
}
// spessasynth_core's writer keeps one byte offset for all samples and gives a PCM sample offset / 2 as its start.
// After an Ogg stream of odd length that start is half a sample off and the PCM sample reads one byte shifted
// (noise at 1/50 of the level). With every PCM sample ahead of the compressed ones the offsets stay even.
soundBank.samples.sort((a, b) => Number(a.isCompressed) - Number(b.isCompressed));
const vorbis = soundBank.writeSFE();
fs.writeFileSync(vorbisFile, new Uint8Array(vorbis));
console.log(
    `Wrote ${vorbisFile}: ${long.length} of ${soundBank.samples.length} samples compressed, ${(vorbis.byteLength / 1048576).toFixed(1)} MB`
);
