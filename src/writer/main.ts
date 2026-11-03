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
    bakeEnvelope: boolean;
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
    /**
     * Key whose playback rate sets how much of a delayed element's silence prefix the zone skips
     */
    delayKey?: number;
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
 * Renders a zone's volume envelope (attack, hold, decay to silence) into the sample, unrolling the loop.
 * The envelope runs in output time, so it is stretched by the zone's playback rate at its key.
 */
function bakeEnvelope(s: BankSample, z: BankZone) {
    const data = decodeSample(s);
    const seconds = (tc: number) => (tc <= -12_000 ? 0 : 2 ** (tc / 1200));
    const cents = (z.scaleTuning / 100) * (z.centerKey - s.root) * 100 + z.tuneCents;
    const rate = 44_100 * 2 ** (cents / 1200);
    const attack = seconds(z.eg.attackVolEnv) * rate;
    const holdEnd = attack + seconds(z.eg.holdVolEnv) * rate;
    const decay = seconds(z.eg.decayVolEnv) * rate;
    // Stop at -80 dB (the rest is inaudible and only grows the bank), or at the end of a one-shot sample
    let length = Math.ceil(holdEnd + (decay * 80) / 96);
    if (s.loop === 0) length = Math.min(length, data.length);
    const out = new Float32Array(length);
    for (let i = 0; i < length; i++) {
        const pos = i < data.length ? i : s.attack + ((i - s.attack) % s.loop);
        let gain = 1;
        if (i < attack) gain = i / attack;
        else if (i > holdEnd) gain = 10 ** ((-96 * (i - holdEnd)) / decay / 20);
        out[i] = data[pos] * gain;
    }
    return out;
}

/**
 * Renders a zone's sample along its measured pitch curve, in output time at the zone's center key, so the
 * baked sample plays at its own rate there (root key = center key, no tuning). After the curve it keeps the
 * last pitch for one more pass of the ROM loop and loops that. A baked envelope is applied on top.
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
    let length = curveEnd + tail;
    const seconds = (tc: number) => (tc <= -12_000 ? 0 : 2 ** (tc / 1200));
    const attack = seconds(z.eg.attackVolEnv) * 44_100;
    const holdEnd = attack + seconds(z.eg.holdVolEnv) * 44_100;
    const decay = seconds(z.eg.decayVolEnv) * 44_100;
    if (z.bakeEnvelope) length = Math.min(length, Math.ceil(holdEnd + (decay * 80) / 96));
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
        let v = data[p0] + (data[p1] - data[p0]) * (pos - p0);
        if (z.bakeEnvelope) {
            if (i < attack) v *= i / attack;
            else if (i > holdEnd) v *= 10 ** ((-96 * (i - holdEnd)) / decay / 20);
        }
        out[i] = v;
        pos += i < curveEnd ? step(curve[c][1]) : lastStep;
    }
    const looped = s.loop > 0 && !z.bakeEnvelope && i === length;
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
// Prefixed copies per sample and prefix length (baked sample names repeat across tunings)
const delayedSamples = new Map<BasicSample, Map<number, BasicSample>>();
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
    // Element LFO. The vibrato LFO carries the element's own depth and delay, the mod LFO (same rate, no delay)
    // the mod wheel. CC76/77/78 move rate, depth and delay like the firmware does, fitted per half of the
    // controller as switch gated terms, so 64 leaves the element as it is.
    const lfo = inst.global.lfo;
    gen(g, GeneratorTypes.freqVibLFO, lfo.freq);
    gen(g, GeneratorTypes.freqModLFO, lfo.freq);
    if (lfo.depth) gen(g, GeneratorTypes.vibLfoToPitch, lfo.depth);
    // Amplitude LFO: the DLL attenuates from 0 to the depth and back (the zones hold half of it). The mod LFO
    // starts at 0 rising, so the bank runs a quarter period behind. Sawtooth ones (11 elements) get the triangle.
    if (inst.global.ampLfo) gen(g, GeneratorTypes.modLfoToVolume, inst.global.ampLfo / 2);
    // Filter LFO: the cutoff drops by up to the depth along the same LFO (the zones hold half of it)
    if (inst.global.filterLfo) gen(g, GeneratorTypes.modLfoToFilterFc, inst.global.filterLfo / 2);
    if (lfo.delay !== undefined && lfo.delay > -12_000) gen(g, GeneratorTypes.delayVibLFO, lfo.delay);
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
    // The sample each zone plays (baked copies where needed), first for all zones: a delayed element's prefixed
    // copies need the longest prefix over the zones sharing a sample
    const zoneSamples = inst.zones.map((z) => {
        let sample = samples[z.sample];
        let pitchBaked = false;
        if (z.pitchCurve) {
            // Zones of one key window (convert.py bake_windows) share one baked sample, baked at the window's middle
            // key: other keys play it faster or slower, which also stretches the curve in time.
            const bakeKey = [z.sample, z.scaleTuning, z.tuneCents, z.bakeRoot, JSON.stringify(z.pitchCurve)].join("|") +
                (z.bakeEnvelope ? JSON.stringify(z.eg) : "");
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
        } else if (z.bakeEnvelope) {
            const s = bank.samples[z.sample];
            sample = new BasicSample(
                `${s.name} k${z.centerKey}`,
                44_100,
                s.root,
                0,
                SampleTypes.monoSample,
                0,
                0
            );
            const data = bakeEnvelope(s, z);
            sample.setAudioData(data, 44_100);
            sample.loopEnd = data.length;
            bakedSamples.push(sample);
        }
        return { sample, pitchBaked };
    });
    const prefixes = new Map<BasicSample, number>();
    const delayFrames = (z: BankZone, sample: BasicSample, pitchBaked: boolean) => {
        // Sample frames the zone's window key plays in the delay
        const key = z.delayKey ?? z.centerKey;
        const cents = (z.scaleTuning / 100) * (key - sample.originalKey) * 100 + (pitchBaked ? 0 : z.tuneCents);
        return Math.round(inst.global.ampDelay * 44_100 * 2 ** (cents / 1200));
    };
    // Delays the converter split into windows (convert.py DELAY_PREFIX) get the prefixed copies
    const prefixed = inst.zones.some((z) => z.delayKey !== undefined);
    if (prefixed) {
        inst.zones.forEach((z, i) => {
            const { sample, pitchBaked } = zoneSamples[i];
            prefixes.set(sample, Math.max(prefixes.get(sample) ?? 0, delayFrames(z, sample, pitchBaked)));
        });
    }
    inst.zones.forEach((z, i) => {
        let { sample } = zoneSamples[i];
        const { pitchBaked } = zoneSamples[i];
        // Before the delay copy below, which moves the loop points behind its prefix
        const loops = pitchBaked ? sample.loopEnd > sample.loopStart && sample.loopStart > 0 : bank.samples[z.sample].loop > 0;
        // The DLL starts the element's sample and amplitude EG after the delay (element byte 0x44). The volume
        // envelope gets the delay and the zone plays a copy of the sample behind a silence, skipping as much of
        // it as its window key plays in the delay
        let startOffset = 0;
        if (prefixed) {
            const prefix = prefixes.get(sample)!;
            const copies = delayedSamples.get(sample) ?? new Map<number, BasicSample>();
            delayedSamples.set(sample, copies);
            let copy = copies.get(prefix);
            if (!copy) {
                const data = sample.getAudioData();
                const padded = new Float32Array(prefix + data.length);
                padded.set(data, prefix);
                copy = new BasicSample(
                    `${sample.name} d${Math.round(inst.global.ampDelay * 1000)}`,
                    44_100,
                    sample.originalKey,
                    sample.pitchCorrection,
                    SampleTypes.monoSample,
                    sample.loopStart + prefix,
                    sample.loopEnd + prefix
                );
                copy.setAudioData(padded, 44_100);
                bakedSamples.push(copy);
                copies.set(prefix, copy);
            }
            startOffset = prefix - delayFrames(z, sample, pitchBaked);
            sample = copy;
        }
        const zone = instrument.createZone(sample);
        zone.keyRange = { min: z.keyLo, max: z.keyHi };
        if (inst.global.ampDelay) {
            gen(zone, GeneratorTypes.delayVolEnv, Math.round(1200 * Math.log2(inst.global.ampDelay)));
            const coarseOffset = Math.floor(startOffset / 32_768);
            if (coarseOffset) gen(zone, GeneratorTypes.startAddrsCoarseOffset, coarseOffset);
            if (startOffset - coarseOffset * 32_768) {
                gen(zone, GeneratorTypes.startAddrsOffset, startOffset - coarseOffset * 32_768);
            }
        }
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
        if (z.bakeEnvelope) {
            // The envelope is in the sample, note-off must not cut it: flat envelope, the longest release
            gen(zone, GeneratorTypes.releaseVolEnv, 8000);
            return;
        }
        gen(zone, GeneratorTypes.attackVolEnv, z.eg.attackVolEnv);
        gen(zone, GeneratorTypes.holdVolEnv, z.eg.holdVolEnv);
        // The DLL's decay and release rates follow the key. keyNumToVolEnvDecay adds (60 - key) * value to the
        // decay, the release gets a key number modulator like the level (no SF2 generator for it).
        const decaySlope = Math.abs(z.decaySlope) >= 1 ? z.decaySlope : 0;
        const releaseSlope = Math.abs(z.releaseSlope * span) >= 10 ? z.releaseSlope : 0;
        gen(zone, GeneratorTypes.decayVolEnv, z.eg.decayVolEnv + decaySlope * (60 - z.centerKey));
        if (decaySlope) gen(zone, GeneratorTypes.keyNumToVolEnvDecay, -decaySlope);
        gen(zone, GeneratorTypes.sustainVolEnv, z.eg.sustainVolEnv);
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
// A baked sample a delayed element only plays as its prefixed copy is not referenced by any zone
const used = new Set(instruments.flatMap((i) => i.zones.map((z) => z.sample)));
soundBank.addSamples(...bakedSamples.filter((s) => used.has(s)));
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
const WORKERS = 8;
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
