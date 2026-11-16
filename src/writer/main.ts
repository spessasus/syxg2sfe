import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import {
    BasicInstrument,
    BasicPreset,
    BasicSample,
    BasicSoundBank,
    CustomCurve,
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
    /**
     * Velocity range of this zone (the cutoff steps of convert.py velocity_steps), all velocities when left out
     */
    velLo?: number;
    velHi?: number;
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
    velocityDepth?: CurveFit;
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
        velocityFc2?: CurveFit;
        /**
         * CC73 (attack) and CC72 (release) timecents response of the element at every CC value (converter
         * envelope_cc)
         */
        envCc?: EnvelopeResponse | null;
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

interface EnvelopeResponse {
    cc72: number[] | null;
    cc73: number[] | null;
}

/**
 * [amount, shape of the controller, shape of the second controller] with both shapes at unit peak
 */
type SeparableTerm = [number, number[], number[]];

/**
 * CC74 and CC71 cutoff response of one zone (src/convert/convert.py brightness_raw), shapes sampled at every
 * controller value
 */
interface Brightness {
    /**
     * CC74 x velocity cutoff terms
     */
    fc: SeparableTerm[];
    /**
     * [amount, CC71 shape] of the cutoff
     */
    cc71: [number, number[]] | null;
    /**
     * How CC74 maps at the resonance CC71 sets (CC74 shape, CC71 shape)
     */
    x71: SeparableTerm | null;
    /**
     * [amount, CC74 shape] of the attenuation
     */
    att: [number, number[]] | null;
}

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
 * Adds modulators to a zone as copies, so the source objects of one can be shared by several (a modulator and its
 * sources belong to one zone only)
 */
function add(zone: BasicZone, ...modulators: Modulator[]) {
    zone.addModulators(...modulators.map((m) => Modulator.copyFrom(m)));
}

/**
 * Creates a modulator source with a built in curve type
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
    // Core's resonance compensator (CC71 to gain), instruments without their own CC71 terms get none
    if (src.index === 71 && m.destination === GeneratorTypes.gain) m.transformAmount = 0;
}
// - Envelope time CCs. The DLL has no CC75 (decay) and moves the attack and release EG rates of each voice by its own
//   amounts, so core's defaults for CC72, CC73 and CC75 go and every instrument carries its own (addEnvelopeCc).
const envelopeDestinations: GeneratorType[] = [GeneratorTypes.attackVolEnv, GeneratorTypes.releaseVolEnv, GeneratorTypes.decayVolEnv];
soundBank.defaultModulators = soundBank.defaultModulators.filter(
    (m) => !([72, 73, 75].includes(m.primarySource.isCC ? m.primarySource.index : -1) && envelopeDestinations.includes(m.destination))
);

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
const INSTANT = -32_768;

function gen(zone: BasicZone, type: GeneratorType, value: number) {
    zone.setGenerator(type, Math.round(value), false);
}

/**
 * Writes an envelope time. Times at the old -12000 floor were the bank's way of saying instant, the SFe
 * generators now have a real zero time at -32768 (the voice starts at the peak in its first block).
 */
function genTime(zone: BasicZone, type: GeneratorType, value: number) {
    gen(zone, type, value <= -12_000 ? INSTANT : value);
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
    add(zone,
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
                add(zone,
                    curve === "step"
                        ? new Modulator(gate, none, d, amount, 0)
                        : new Modulator(source(cc, CURVES[curve], false, true, true), gate, d, amount, 0)
                );
            }
        }
    }
}

/**
 * Largest error in timecents a shared envelope curve may add to the DLL's response (src/convert/convert.py envelope_cc).
 * Where the DLL moves the time less than ENV_SMALL_TC (small CC moves around 64) a third of it is allowed, those
 * settings are the common ones.
 */
const ENV_CURVE_TOLERANCE = 100;
const ENV_SMALL_TC = 80;

interface SharedCurve {
    shape: number[];
    curve: CustomCurve;
}

/**
 * Whole bipolar response shapes of CC72 and CC73 shared bank wide (unit normalised, the amount of each modulator
 * scales them). The first response that no curve fits within the tolerance starts a new one.
 */
const envCurves = { 72: new Array<SharedCurve>(), 73: new Array<SharedCurve>() };

/**
 * The attack (CC73) and release (CC72) time response of one element as one modulator per CC, a bipolar custom
 * curve (exactly 0 at 64) shared with the other instruments.
 */
function addEnvelopeCc(zone: BasicZone, env: EnvelopeResponse) {
    for (const [cc, y, destination] of [
        [72, env.cc72, GeneratorTypes.releaseVolEnv],
        [73, env.cc73, GeneratorTypes.attackVolEnv]
    ] as const) {
        if (!y) continue;
        let found: SharedCurve | undefined;
        let amount = 0;
        for (const c of envCurves[cc]) {
            const a: number = y.reduce((sum, v, i) => sum + v * c.shape[i], 0) / c.shape.reduce((sum, v) => sum + v * v, 0);
            const fits = (v: number, i: number) =>
                Math.abs(a * c.shape[i] - v) <= (Math.abs(v) < ENV_SMALL_TC ? ENV_CURVE_TOLERANCE / 3 : ENV_CURVE_TOLERANCE);
            if (y.every(fits)) {
                found = c;
                amount = a;
                break;
            }
        }
        if (!found) {
            const peak = Math.max(...y.map(Math.abs));
            const shape = y.map((v) => v / peak);
            const points = shape.map((v, i) => ({ x: i * 128, y: Math.round(8192 + 8192 * v) }));
            points.push({ x: 16_383, y: points[127].y });
            found = { shape, curve: new CustomCurve(points, `cc${cc} ${envCurves[cc].length}`) };
            envCurves[cc].push(found);
            amount = peak;
        }
        add(zone,
            new Modulator(
                new ModulatorSource(cc, found.curve, true, true, false),
                source(ModulatorControllerSources.noController, 0),
                destination,
                Math.round(amount),
                0
            )
        );
    }
}

/**
 * Largest error in cents the breakpoint cut and the sharing of a curve may each add to the response
 */
const CURVE_CUT_CENTS = 25;
const CURVE_SHARE_CENTS = 25;

/**
 * Shared cutoff and attenuation curves. Controller shapes (CC74, CC71, exactly 0 at 64) and velocity shapes (no
 * neutral value) are kept apart, a velocity curve reused for CC74 would move the cutoff at CC74 64 already.
 */
const sharedCurves = { controller: new Array<SharedCurve>(), velocity: new Array<SharedCurve>() };
const curveStats = {
    cut: new Array<number>(),
    share: new Array<number>(),
    points: new Array<number>(),
    /**
     * Largest error in cents of each whole modulator (both curves, the cut, the sharing and the rounded amount)
     * against the converter's response
     */
    term: new Array<number>()
};

/**
 * Breakpoints (indices) of a 128 value shape that keep the straight lines between them within eps, always with the
 * ends and the forced indices (the neutral controller value 64)
 */
function breakpoints(shape: number[], eps: number, forced: number[]) {
    const keep = new Set([0, 127, ...forced]);
    const cut = (lo: number, hi: number) => {
        let worst = -1;
        let at = -1;
        for (let i = lo + 1; i < hi; i++) {
            const line = shape[lo] + ((shape[hi] - shape[lo]) * (i - lo)) / (hi - lo);
            const d = Math.abs(shape[i] - line);
            if (d > worst) {
                worst = d;
                at = i;
            }
        }
        if (at >= 0 && worst > eps) {
            keep.add(at);
            cut(lo, at);
            cut(at, hi);
        }
    };
    const sorted = [...keep].sort((a, b) => a - b);
    for (let i = 0; i + 1 < sorted.length; i++) cut(sorted[i], sorted[i + 1]);
    return [...keep].sort((a, b) => a - b);
}

/**
 * The shared custom curve for a response of the given shape (unit peak) and amount, and the amount to use with it.
 * The shape is cut to breakpoints first, then matched to a curve of the bank within the sharing tolerance (any
 * scale), else it becomes a new one.
 */
function sharedCurve(shape: number[], amount: number, neutral: number | null) {
    const eps = CURVE_CUT_CENTS / Math.abs(amount);
    const idx = breakpoints(shape, eps, neutral === null ? [] : [neutral]);
    const cut = shape.map((_, i) => {
        let k = 0;
        while (idx[k + 1] < i) k++;
        const lo = idx[k];
        const hi = idx[Math.min(k + 1, idx.length - 1)];
        return hi === lo ? shape[lo] : shape[lo] + ((shape[hi] - shape[lo]) * (i - lo)) / (hi - lo);
    });
    curveStats.cut.push(Math.max(...cut.map((v, i) => Math.abs(v - shape[i]))) * Math.abs(amount));
    const pool = neutral === null ? sharedCurves.velocity : sharedCurves.controller;
    for (const c of pool) {
        const k = cut.reduce((sum, v, i) => sum + v * c.shape[i], 0) / c.shape.reduce((sum, v) => sum + v * v, 0);
        const dev = Math.max(...cut.map((v, i) => Math.abs(k * c.shape[i] - v))) * Math.abs(amount);
        if (dev <= CURVE_SHARE_CENTS) {
            curveStats.share.push(dev);
            return { curve: c.curve, shape: c.shape, scale: k };
        }
    }
    const points = idx.map((i) => ({ x: i * 128, y: Math.max(0, Math.min(16_384, Math.round(8192 + 8192 * cut[i]))) }));
    points.push({ x: 16_383, y: points[points.length - 1].y });
    const made = { shape: cut, curve: new CustomCurve(points, `${neutral === null ? "velocity" : "controller"} ${pool.length}`) };
    pool.push(made);
    curveStats.points.push(points.length);
    curveStats.share.push(0);
    return { curve: made.curve, shape: made.shape, scale: 1 };
}

/**
 * Notes the largest error of one modulator over both its sources: amount x g x h as the converter has it against
 * the rounded amount x the two shared curves (h and its curve left out for a one source modulator)
 */
function noteTermError(amount: number, g: number[], cg: SharedCurve, rounded: number, h?: number[], ch?: SharedCurve) {
    let worst = 0;
    for (let j = 0; j < (h ? 128 : 1); j++) {
        const want = h ? h[j] : 1;
        const got = h && ch ? ch.shape[j] : 1;
        for (let i = 0; i < 128; i++) worst = Math.max(worst, Math.abs(amount * g[i] * want - rounded * cg.shape[i] * got));
    }
    curveStats.term.push(worst);
}

/**
 * CC74 and CC71 cutoff and attenuation response of one zone: one custom curve per controller and term, velocity (or
 * CC71) as the second source of the CC74 terms, curves shared bank wide (converter brightness_raw)
 */
function addBrightness(zone: BasicZone, b: Brightness) {
    const none = source(ModulatorControllerSources.noController, 0);
    const fc = GeneratorTypes.initialFilterFc;
    for (const [amount, g, h] of b.fc) {
        const cg = sharedCurve(g, amount, 64);
        const ch = sharedCurve(h, amount, null);
        const rounded = Math.round(amount * cg.scale * ch.scale);
        noteTermError(amount, g, cg, rounded, h, ch);
        add(zone,
            new Modulator(
                new ModulatorSource(74, cg.curve, true, true, false),
                new ModulatorSource(ModulatorControllerSources.noteOnVelocity, ch.curve, false, true, false),
                fc,
                rounded,
                0
            )
        );
    }
    if (b.cc71) {
        const c = sharedCurve(b.cc71[1], b.cc71[0], 64);
        const rounded = Math.round(b.cc71[0] * c.scale);
        noteTermError(b.cc71[0], b.cc71[1], c, rounded);
        add(zone, new Modulator(new ModulatorSource(71, c.curve, true, true, false), none, fc, rounded, 0));
    }
    if (b.x71) {
        const [amount, g, h] = b.x71;
        const cg = sharedCurve(g, amount, 64);
        const ch = sharedCurve(h, amount, 64);
        const rounded = Math.round(amount * cg.scale * ch.scale);
        noteTermError(amount, g, cg, rounded, h, ch);
        add(zone,
            new Modulator(
                new ModulatorSource(74, cg.curve, true, true, false),
                new ModulatorSource(71, ch.curve, true, true, false),
                fc,
                rounded,
                0
            )
        );
    }
    if (b.att) {
        const c = sharedCurve(b.att[1], b.att[0], 64);
        noteTermError(b.att[0], b.att[1], c, Math.round(b.att[0] * c.scale));
        add(zone,
            new Modulator(
                new ModulatorSource(74, c.curve, true, true, false),
                none,
                GeneratorTypes.initialAttenuation,
                Math.round(b.att[0] * c.scale),
                0
            )
        );
    }
}

const bakedSamples = new Array<BasicSample>();
const pitchBakes = new Map<string, BasicSample>();
// A bank.json from before the per value responses (envelope terms per half, brightness curve fits) would lose its
// envelope CCs or fail halfway, so it stops here
const staleBrightness = (b: Brightness | null | undefined) => !!b && !Array.isArray(b.fc);
for (const inst of bank.instruments) {
    const env = inst.global.envCc;
    if (
        (env && [env.cc72, env.cc73].some((y) => y !== null && !Array.isArray(y))) ||
        staleBrightness(inst.global.brightness) ||
        inst.zones.some((z) => staleBrightness(z.brightness))
    ) {
        throw new Error(`${path.join(dataDir, "bank.json")} is from an older converter, run build.py again`);
    }
}

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
        add(g,
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
    addVelocityModulator(g, inst.global.velocityFc2, GeneratorTypes.initialFilterFc, -1);
    addVelocityModulator(g, inst.global.velocityQ, GeneratorTypes.initialFilterQ, -1);
    // CC71. The upper half uses the default resonant modulator's exact source (bipolar CC71, no secondary), so it
    // overrides it. The level that rises with the resonance goes on the gain generator, which sits after the
    // attenuation clamp and can boost. It has the identity of core's default compensator (CC71 linear bipolar plus
    // CC71 switch), so it replaces that one and carries the whole measured gain. A second modulator (switch
    // secondary) reshapes the lower half.
    const cc71 = source(71, ModulatorCurveTypes.linear, false, true, true);
    const none = source(ModulatorControllerSources.noController, 0);
    const upper = source(71, ModulatorCurveTypes.switch, false, true);
    const lower = source(71, ModulatorCurveTypes.switch, true, true);
    const { up, down } = inst.global.cc71;
    add(g, new Modulator(cc71, none, GeneratorTypes.initialFilterQ, up[0], 0));
    add(g, new Modulator(cc71, upper, GeneratorTypes.gain, Math.round(up[1]), 0));
    if (down[0] !== up[0]) {
        add(g, new Modulator(cc71, lower, GeneratorTypes.initialFilterQ, down[0] - up[0], 0));
    }
    if (down[1]) add(g, new Modulator(cc71, lower, GeneratorTypes.initialAttenuation, -down[1], 0));
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
        !!b && b.fc.length + (b.att ? 1 : 0) + (b.x71 ? 1 : 0) > 0;
    if (hasCC74(inst.global.brightness) || inst.zones.some((z) => hasCC74(z.brightness))) {
        add(g,
            new Modulator(source(74, ModulatorCurveTypes.linear, false, true, true), none, GeneratorTypes.initialFilterFc, 0, 0)
        );
    }
    if (inst.global.brightness) addBrightness(g, inst.global.brightness);
    if (inst.global.envCc) addEnvelopeCc(g, inst.global.envCc);
    // Filter EG, measured on the long note at the element's center key
    const feg = inst.global.filterEG;
    if (feg) {
        genTime(g, GeneratorTypes.delayModEnv, feg.delayModEnv);
        genTime(g, GeneratorTypes.attackModEnv, feg.attackModEnv);
        genTime(g, GeneratorTypes.holdModEnv, feg.holdModEnv);
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
        genTime(g, GeneratorTypes.delayModEnv2, peg.delayModEnv2);
        genTime(g, GeneratorTypes.attackModEnv2, peg.attackModEnv2);
        genTime(g, GeneratorTypes.holdModEnv2, peg.holdModEnv2);
        gen(g, GeneratorTypes.decayModEnv2, peg.decayModEnv2);
        gen(g, GeneratorTypes.sustainModEnv2, peg.sustainModEnv2);
        gen(g, GeneratorTypes.releaseModEnv2, peg.releaseModEnv2);
        gen(g, GeneratorTypes.modEnv2ToPitch, peg.modEnv2ToPitch);
        // Some pitch EGs scale their depth with velocity
        addVelocityModulator(g, peg.velocityDepth, GeneratorTypes.modEnv2ToPitch, -1);
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
        if (z.velLo !== undefined && z.velHi !== undefined) zone.velRange = { min: z.velLo, max: z.velHi };
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
            add(zone,
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
        genTime(zone, GeneratorTypes.attackVolEnv, z.eg.attackVolEnv);
        // The voice only starts at its peak when the delay is exactly zero as well
        if (z.eg.attackVolEnv <= -12_000) gen(zone, GeneratorTypes.delayVolEnv, INSTANT);
        genTime(zone, GeneratorTypes.holdVolEnv, z.eg.holdVolEnv);
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
            add(zone,
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
soundBank.addCustomCurves(...envCurves[72].map((c) => c.curve), ...envCurves[73].map((c) => c.curve), ...[...sharedCurves.controller, ...sharedCurves.velocity].map((c) => c.curve));
// The new bank's own copies of core's two envelope time curves go with the default modulators that used them
for (const c of [...soundBank.customCurves]) if (c.useCount === 0) soundBank.deleteCustomCurve(c);
const percentile = (a: number[], p: number) => [...a].sort((x, y) => x - y)[Math.floor((a.length - 1) * p)] ?? 0;
console.log(
    `Custom curves: ${soundBank.customCurves.length} (${envCurves[72].length + envCurves[73].length} envelope, ${sharedCurves.controller.length} controller and ` +
        `${sharedCurves.velocity.length} velocity, ${curveStats.points.reduce((a, b) => a + b, 0)} points), breakpoint cut error cents median ` +
        `${percentile(curveStats.cut, 0.5).toFixed(1)} p99 ${percentile(curveStats.cut, 0.99).toFixed(1)} max ${percentile(curveStats.cut, 1).toFixed(1)}, ` +
        `sharing error median ${percentile(curveStats.share, 0.5).toFixed(1)} p99 ${percentile(curveStats.share, 0.99).toFixed(1)} ` +
        `max ${percentile(curveStats.share, 1).toFixed(1)}, whole modulator error median ${percentile(curveStats.term, 0.5).toFixed(1)} ` +
        `p99 ${percentile(curveStats.term, 0.99).toFixed(1)} max ${percentile(curveStats.term, 1).toFixed(1)}`
);
soundBank.flush();

const out = soundBank.writeSFe({ rf64: false });
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
const vorbis = soundBank.writeSFe({ rf64: false });
fs.writeFileSync(vorbisFile, new Uint8Array(vorbis));
console.log(
    `Wrote ${vorbisFile}: ${long.length} of ${soundBank.samples.length} samples compressed, ${(vorbis.byteLength / 1048576).toFixed(1)} MB`
);
