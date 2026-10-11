#!/usr/bin/env python3
"""
akai2sf2.py - build SoundFont (.sf2) files straight from Akai S3000/CD3000
program (.P3) files plus the WAV files exported by akaiutil (tarcwav / sample2wav).

No third-party packages needed (Python 3.8+).

Usage
-----
    python akai2sf2.py INPUT_FOLDER [-o OUTPUT_FOLDER] [--all-samples] [--no-envelopes] [-v]

INPUT_FOLDER can be
  * one volume folder  (contains .P3 files and the .wav files), or
  * any folder tree, e.g. the extracted B_wav.tar - every sub-folder that
    contains .P3 files is treated as a volume and gets its own .sf2.

One .sf2 is written per volume. Each .P3 program becomes one preset.

How the pieces are matched
--------------------------
A program's keygroups name their samples (12 character Akai names). akaiutil
embeds the original 192-byte Akai sample header in every WAV it exports (an
'S3H ' chunk), so WAVs are matched to programs by the sample name stored
INSIDE the file, not by file name. Nothing is ever picked by hand.

What is translated
------------------
key ranges, velocity ranges, root key, fine tuning (keygroup + zone + program),
sample loops, loop mode, pan (program + zone), loudness offsets, and an
approximation of the amplitude envelope (attack/decay/sustain/release).

What is NOT translated: filters, LFOs, modulation routing, effects, velocity
crossfades. SoundFont has no equivalent for most of these.
"""
import argparse
import hashlib
import math
import os
import struct
import sys
from array import array

# --------------------------------------------------------------------------
# Akai parsing
# --------------------------------------------------------------------------
_CHARS = "0123456789 ABCDEFGHIJKLMNOPQRSTUVWXYZ#+-."


def akai_str(b):
    return "".join(_CHARS[c] if c < len(_CHARS) else "?" for c in b).rstrip()


def sbyte(x):
    return x - 256 if x > 127 else x


def tune_cents(lo, hi):
    """Akai 16-bit tune = signed 8.8 fixed point, in semitones -> cents."""
    v = struct.unpack("<h", bytes([lo, hi]))[0]
    return v * 100.0 / 256.0


def parse_p3(path):
    d = open(path, "rb").read()
    if len(d) < 0xC0 or d[0] != 1:
        raise ValueError("not an S3000 program file")
    prog = dict(
        name=akai_str(d[3:15]),
        midi_prog=d[15],
        pan=sbyte(d[24]),
        loudness=d[25],
        tune=tune_cents(d[65], d[66]),
        nkg=d[42],
        keygroups=[],
    )
    for k in range(prog["nkg"]):
        o = 0xC0 + 192 * k
        kg = d[o:o + 192]
        if len(kg) < 192 or kg[0] != 2:
            break
        g = dict(
            lo=kg[3], hi=kg[4], tune=tune_cents(kg[5], kg[6]),
            att=kg[12], dec=kg[13], sus=kg[14], rel=kg[15],
            zones=[],
        )
        for z in range(4):
            zo = 34 + 24 * z
            name = akai_str(kg[zo:zo + 12])
            if not name:
                continue
            g["zones"].append(dict(
                name=name, lovel=kg[zo + 12], hivel=kg[zo + 13],
                tune=tune_cents(kg[zo + 14], kg[zo + 15]),
                loud=sbyte(kg[zo + 16]), pan=sbyte(kg[zo + 18]),
                play=kg[zo + 19],
            ))
        prog["keygroups"].append(g)
    return prog


def parse_s3h(h):
    loops = []
    for i in range(4):
        o = 38 + 12 * i
        loops.append(dict(
            at=struct.unpack_from("<I", h, o)[0],
            length=struct.unpack_from("<I", h, o + 6)[0],
            dwell=struct.unpack_from("<H", h, o + 10)[0],
        ))
    return dict(
        name=akai_str(h[3:15]), root=h[2], nloops=h[16], first_loop=h[17],
        ptype=h[19], tune=tune_cents(h[20], h[21]),
        length=struct.unpack_from("<I", h, 26)[0],
        start=struct.unpack_from("<I", h, 30)[0],
        end=struct.unpack_from("<I", h, 34)[0],
        partner=struct.unpack_from("<H", h, 136)[0],
        rate=struct.unpack_from("<H", h, 138)[0],
        loops=loops,
    )


def read_wav(path):
    d = open(path, "rb").read()
    if d[:4] != b"RIFF" or d[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    p, fmt, data, s3h = 12, None, None, None
    while p + 8 <= len(d):
        cid, sz = d[p:p + 4], struct.unpack_from("<I", d, p + 4)[0]
        body = d[p + 8:p + 8 + sz]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", body[:16])
        elif cid == b"data":
            data = body
        elif cid == b"S3H " and len(body) >= 192:
            s3h = parse_s3h(body)
        p += 8 + sz + (sz & 1)
    if fmt is None or data is None:
        raise ValueError("incomplete WAV")
    fmt_tag, channels, rate, _, _, bits = fmt
    if fmt_tag != 1 or bits != 16:
        raise ValueError(f"unsupported WAV format (tag {fmt_tag}, {bits}-bit); need 16-bit PCM")
    pcm = array("h")
    pcm.frombytes(data[:len(data) - (len(data) % 2)])
    if sys.byteorder == "big":
        pcm.byteswap()
    chans = [pcm[c::channels] for c in range(channels)]
    return dict(channels=chans, rate=rate, s3h=s3h)


# --------------------------------------------------------------------------
# SoundFont 2 writer
# --------------------------------------------------------------------------
G = dict(pan=17, attack=34, decay=36, sustain=37, release=38, instrument=41,
         keyRange=43, velRange=44, attenuation=48, coarse=51, fine=52,
         sampleID=53, sampleModes=54)


def _chunk(cid, data):
    # RIFF chunks are padded to an even length; the size field excludes the pad
    return cid + struct.pack("<I", len(data)) + data + (b"\0" if len(data) & 1 else b"")


def _zstr(s):
    """INFO text chunks: NUL terminated and even length (size field includes the pad)."""
    b = s.encode("latin-1", "replace")[:250] + b"\0"
    return b + b"\0" if len(b) & 1 else b


def _list(kind, payload):
    return _chunk(b"LIST", kind + payload)


def _name20(s):
    return s.encode("latin-1", "replace")[:19].ljust(20, b"\0")


def _gen(op, amount):
    if op in (G["keyRange"], G["velRange"]):
        lo, hi = amount
        return struct.pack("<HBB", op, lo, hi)
    return struct.pack("<HH", op, amount & 0xFFFF)


class SF2:
    def __init__(self, name):
        self.name = name
        self.samples = []      # dicts
        self.sample_ids = {}   # key -> id
        self.insts = []        # (name, [zone gen lists])
        self.presets = []      # (name, bank, number, inst index, attenuation)

    def add_sample(self, key, name, pcm, rate, root, loop, stype=1, link=0):
        if key in self.sample_ids:
            return self.sample_ids[key]
        sid = len(self.samples)
        self.samples.append(dict(name=name, pcm=pcm, rate=rate, root=root,
                                 loop=loop, type=stype, link=link))
        self.sample_ids[key] = sid
        return sid

    def build(self):
        # --- sdta
        smpl = bytearray()
        shdr = bytearray()
        pos = 0
        for s in self.samples:
            n = len(s["pcm"])
            ls, le = s["loop"] if s["loop"] else (0, 0)
            data = array("h", s["pcm"])
            if sys.byteorder == "big":
                data.byteswap()
            pad = array("h", [0] * 46)
            if s["loop"] and s["loop"][1] - s["loop"][0] > 0:
                # the spec wants the points after the loop end to repeat the loop start
                ll = s["loop"][1] - s["loop"][0]
                pad = array("h", [s["pcm"][s["loop"][0] + (k % ll)] for k in range(46)])
            if sys.byteorder == "big":
                pad.byteswap()
            smpl += data.tobytes() + pad.tobytes()
            shdr += _name20(s["name"]) + struct.pack(
                "<IIIIIBbHH", pos, pos + n, pos + ls, pos + le, s["rate"],
                s["root"], 0, s["link"], s["type"])
            pos += n + 46
        shdr += _name20("EOS") + struct.pack("<IIIIIBbHH", 0, 0, 0, 0, 0, 0, 0, 0, 0)

        # --- instruments
        inst, ibag, igen = bytearray(), bytearray(), bytearray()
        bag_n = gen_n = 0
        for name, zones in self.insts:
            inst += _name20(name) + struct.pack("<H", bag_n)
            for z in zones:
                ibag += struct.pack("<HH", gen_n, 0)
                for op, amt in z:
                    igen += _gen(op, amt)
                gen_n += len(z)
                bag_n += 1
        inst += _name20("EOI") + struct.pack("<H", bag_n)
        ibag += struct.pack("<HH", gen_n, 0)
        igen += _gen(0, 0)
        imod = b"\0" * 10

        # --- presets (one zone each -> one instrument)
        phdr, pbag, pgen = bytearray(), bytearray(), bytearray()
        pb = pg = 0
        for name, bank, num, inst_i, atten in sorted(self.presets, key=lambda p: (p[1], p[2])):
            phdr += _name20(name) + struct.pack("<HHHIII", num, bank, pb, 0, 0, 0)
            gens = []
            if atten:
                gens.append((G["attenuation"], atten))
            gens.append((G["instrument"], inst_i))
            pbag += struct.pack("<HH", pg, 0)
            for op, amt in gens:
                pgen += _gen(op, amt)
            pg += len(gens)
            pb += 1
        phdr += _name20("EOP") + struct.pack("<HHHIII", 0, 0, pb, 0, 0, 0)
        pbag += struct.pack("<HH", pg, 0)
        pgen += _gen(0, 0)
        pmod = b"\0" * 10

        info = (_chunk(b"ifil", struct.pack("<HH", 2, 1))
                + _chunk(b"isng", b"EMU8000\0")
                + _chunk(b"INAM", _zstr(self.name))
                + _chunk(b"ISFT", _zstr("akai2sf2")))
        sdta = _chunk(b"smpl", bytes(smpl))
        pdta = (_chunk(b"phdr", bytes(phdr)) + _chunk(b"pbag", bytes(pbag))
                + _chunk(b"pmod", pmod) + _chunk(b"pgen", bytes(pgen))
                + _chunk(b"inst", bytes(inst)) + _chunk(b"ibag", bytes(ibag))
                + _chunk(b"imod", imod) + _chunk(b"igen", bytes(igen))
                + _chunk(b"shdr", bytes(shdr)))
        body = b"sfbk" + _list(b"INFO", info) + _list(b"sdta", sdta) + _list(b"pdta", pdta)
        return b"RIFF" + struct.pack("<I", len(body)) + body


# --------------------------------------------------------------------------
# Akai -> SoundFont translation helpers
# --------------------------------------------------------------------------
def _tc(seconds):
    return int(round(1200 * math.log2(max(seconds, 0.001))))


def _rate_to_time(r, tmin, tmax):
    """Akai envelope 'rate' 0..99 behaves like a time: bigger = slower."""
    return tmin * (tmax / tmin) ** (min(max(r, 0), 99) / 99.0)


def envelope_gens(g):
    sus = g["sus"]
    sus_cb = 0 if sus >= 99 else int(min(1000, -200 * math.log10(max(sus, 0.5) / 99.0)))
    return [
        (G["attack"], _tc(_rate_to_time(g["att"], 0.002, 10.0))),
        (G["decay"], _tc(_rate_to_time(g["dec"], 0.01, 120.0))),
        (G["sustain"], sus_cb),
        (G["release"], _tc(_rate_to_time(g["rel"], 0.01, 40.0))),
    ]


def pan_of(prog_pan, zone_pan):
    return max(-50, min(50, prog_pan + zone_pan)) * 10     # SF2 pan: -500..500


def split_tuning(total_cents):
    coarse = int(round(total_cents / 100.0))
    fine = int(round(total_cents - coarse * 100))
    return coarse, fine


def find_loop(s3h, n):
    """Decide how a sample loops. Returns (loop, sampleModes, reason_if_not_looping).

    The Akai fields 'number of loops' / 'first active loop' are documented as
    internal use, so the decision is made from the loop data itself: every one of
    the four loop slots is examined and the first one that is switched on (dwell
    time > 0) and usable wins. Loops that overshoot the audio by a few points are
    clamped to it instead of being thrown away.
    """
    if not s3h:
        return None, 0, "no Akai header in WAV, loop info unavailable"
    if s3h["ptype"] == 2:
        return None, 0, "Akai playback type is 'no looping' (one-shot by design)"
    if s3h["ptype"] == 3:
        return None, 0, "Akai playback type is 'play to sample end' (one-shot by design)"
    first = min(s3h["first_loop"], 3)
    problems = []
    for i in [first] + [j for j in range(4) if j != first]:
        lp = s3h["loops"][i]
        if lp["dwell"] == 0:
            continue                                     # this loop slot is switched off
        if lp["length"] < 2 or lp["at"] > n + 1024 or lp["length"] > lp["at"] + 1024:
            problems.append(f"loop {i + 1} has implausible values (at {lp['at']}, length {lp['length']}, "
                            f"audio is {n} points)")
            continue
        end = lp["at"]                                   # Akai 'loop at' is the loop END
        start = end - lp["length"]
        if end > n:
            # The loop runs a few points past the audio. Its LENGTH is what sets the
            # pitch of short single-cycle waveforms, so keep it and slide the loop
            # back so it ends on the last point (shortening it detunes and clicks).
            start -= end - n
            end = n
        start = max(0, start)
        if end - start < 8:
            problems.append(f"loop {i + 1} is shorter than 8 points once fitted to the audio")
            continue
        return (start, end), (3 if s3h["ptype"] == 1 else 1), None
    if problems:
        return None, 0, "; ".join(problems)
    return None, 0, "no loop is switched on in the Akai header (every loop dwell time is 0)"


class Siblings:
    """Finds samples that a program uses but that live in ANOTHER volume of the same
    partition (e.g. a stereo piano whose '-L' samples are in '3044 CS PF-L' and whose
    '-R' samples are in '3045 CS PF-R'). Only unambiguous matches are used."""

    def __init__(self, volume_dirs):
        self.dirs = list(volume_dirs)
        self.index = None

    def _build(self):
        self.index = {}
        for v in self.dirs:
            for fn in sorted(os.listdir(v)):
                if fn.lower().endswith(".wav"):
                    try:
                        w = read_wav(os.path.join(v, fn))
                    except Exception:       # noqa
                        continue
                    if w["s3h"]:
                        w["file"] = fn
                        self.index.setdefault(w["s3h"]["name"].upper(), []).append((v, w))

    def find(self, key, exclude):
        """-> (volume_dir, wav) if exactly one other volume (or several identical copies) has it."""
        if self.index is None:
            self._build()
        hits = [(v, w) for v, w in self.index.get(key, []) if v != exclude]
        if not hits:
            return None
        first = hashlib.md5(b"".join(c.tobytes() for c in hits[0][1]["channels"])).hexdigest()
        for v, w in hits[1:]:
            if hashlib.md5(b"".join(c.tobytes() for c in w["channels"])).hexdigest() != first:
                return None              # same name, different audio in different volumes: ambiguous
        return hits[0]


# --------------------------------------------------------------------------
# Volume conversion
# --------------------------------------------------------------------------
def load_wavs(folder, warnings):
    """index: akai sample name -> wav dict (matched by name stored inside the file)."""
    idx = {}
    for fn in sorted(os.listdir(folder)):
        if not fn.lower().endswith(".wav"):
            continue
        try:
            w = read_wav(os.path.join(folder, fn))
        except Exception as e:      # noqa
            warnings.append(f"cannot read {fn}: {e}")
            continue
        if w["s3h"]:
            key = w["s3h"]["name"]
        else:
            key = os.path.splitext(fn)[0]
            warnings.append(f"{fn}: no Akai header inside, matching by file name only "
                            f"(root note/loops unavailable, root defaulted to C4)")
        w["file"] = fn
        idx[key.upper()] = w
    return idx


def convert_volume(folder, out_path, all_samples=False, envelopes=True, verbose=False,
                   sf=None, prefix="", siblings=None):
    """Convert one volume. With sf=None a .sf2 is written to out_path. If an SF2 builder is
    passed in, the volume's presets are ADDED to it (merging) and nothing is written."""
    warnings = []
    p3s = sorted(f for f in os.listdir(folder) if f.lower().endswith(".p3"))
    progs = []
    for f in p3s:
        try:
            progs.append(parse_p3(os.path.join(folder, f)))
        except Exception as e:      # noqa
            warnings.append(f"{f}: skipped ({e})")
    wavs = load_wavs(folder, warnings)
    merging = sf is not None
    if sf is None:
        sf = SF2(os.path.basename(os.path.normpath(folder)))
    first_preset = len(sf.presets)
    first_sample = len(sf.samples)
    used = set()
    borrowed = {}    # sample name -> sibling volume it was taken from
    flags = {}       # sample name -> reason it does not loop (only for used samples)
    env_short = {}   # program name -> list of (lo, hi, seconds until near-silent)
    max_loud = max([p["loudness"] for p in progs] or [0])

    def sample_id(akai_name, channel=0, nch=1):
        """Create/lookup the SF2 sample for one channel of a WAV. Returns id or None."""
        w = wavs[akai_name.upper()]
        s3h = w["s3h"]
        pcm = w["channels"][channel]
        n = len(pcm)
        loop, _, _ = find_loop(s3h, n)
        if s3h and s3h["end"] and s3h["start"] < s3h["end"] <= n and \
                (s3h["start"] > 0 or s3h["end"] < n):
            st, en = s3h["start"], s3h["end"]
            if loop is None or (loop[0] >= st and loop[1] <= en):
                pcm = pcm[st:en]
                if loop:
                    loop = (loop[0] - st, loop[1] - st)
        root = s3h["root"] if s3h else 60
        stype = 1
        link = 0
        name = akai_name.strip()
        if nch == 2:
            stype = 4 if channel == 0 else 2
            name = (name[:17] + (" L" if channel == 0 else " R"))[:19]
        digest = hashlib.md5(pcm.tobytes()).hexdigest()
        return sf.add_sample((akai_name.upper(), channel, nch, digest, loop, root, w["rate"], stype),
                             name, pcm, w["rate"] or 22050, root, loop, stype, link)

    for prog in progs:
        zones = []
        zone_atten = []   # (zone gens, loudness offset)
        for kg in prog["keygroups"]:
            if kg["hi"] < kg["lo"]:
                continue
            for z in kg["zones"]:
                key = z["name"].upper()
                if key not in wavs and siblings is not None:
                    hit = siblings.find(key, folder)
                    if hit:
                        wavs[key] = hit[1]
                        borrowed.setdefault(z["name"], os.path.basename(hit[0]))
                if key not in wavs:
                    # a '-R' sample may live inside a stereo '-L' wav
                    warnings.append(f"{prog['name']}: sample '{z['name']}' (keys {kg['lo']}-{kg['hi']}) "
                                    f"has no matching WAV in this folder - zone skipped")
                    continue
                w = wavs[key]
                s3h = w["s3h"]
                used.add(key)
                # tuning: keygroup + zone + program + any residual sample offset
                # (sample header offset minus the automatic 22.05k/44.1k compensation)
                resid = 0.0
                if s3h and w["rate"]:
                    resid = s3h["tune"] - 1200.0 * math.log2(w["rate"] / 44100.0)
                total = kg["tune"] + z["tune"] + prog["tune"] + resid
                coarse, fine = split_tuning(total)
                loop, modes, why = find_loop(s3h, len(w["channels"][0]))
                play = z["play"]
                if play in (3, 4):
                    why = "keygroup zone is set to 'no loop / play to end' in the program"
                if why:
                    flags[z["name"]] = why
                if envelopes:
                    sus_cb = 0 if kg["sus"] >= 99 else min(1000, -200 * math.log10(max(kg["sus"], 0.5) / 99.0))
                    t_fade = _rate_to_time(kg["dec"], 0.01, 120.0) * sus_cb / 1000.0
                    if sus_cb >= 400 and t_fade < 1.0:
                        env_short.setdefault(prog["name"], []).append((kg["lo"], kg["hi"], t_fade))
                if play == 1:
                    modes = 1 if loop else 0
                elif play == 2:
                    modes = 3 if loop else 0
                elif play in (3, 4):
                    modes = 0
                base = [(G["keyRange"], (kg["lo"], kg["hi"]))]
                if (z["lovel"], z["hivel"]) != (0, 127):
                    base.append((G["velRange"], (z["lovel"], z["hivel"])))
                tail = []
                if coarse:
                    tail.append((G["coarse"], coarse))
                if fine:
                    tail.append((G["fine"], fine))
                if envelopes:
                    tail += envelope_gens(kg)
                if modes:
                    tail.append((G["sampleModes"], modes))
                nch = len(w["channels"])
                if nch == 2:   # real stereo WAV: hard L / hard R pair, linked
                    ids = [sample_id(z["name"], c, 2) for c in (0, 1)]
                    sf.samples[ids[0]]["link"], sf.samples[ids[1]]["link"] = ids[1], ids[0]
                    for sid in ids:
                        zone_atten.append((base + tail + [(G["sampleID"], sid)], z["loud"]))
                else:
                    sid = sample_id(z["name"])
                    pan = pan_of(prog["pan"], z["pan"])
                    gens = base + ([(G["pan"], pan)] if pan else []) + tail + [(G["sampleID"], sid)]
                    zone_atten.append((gens, z["loud"]))
        if not zone_atten:
            warnings.append(f"{prog['name']}: no playable zones, preset skipped")
            continue
        lmax = max(l for _, l in zone_atten)
        for gens, loud in zone_atten:
            cb = int(round(5 * (lmax - loud)))      # 0.5 dB per Akai loudness step
            if cb:
                # attenuation goes before the sampleID (must stay last)
                gens.insert(len(gens) - 1, (G["attenuation"], cb))
            zones.append(gens)
        pname = (prefix + " " + prog["name"] if prefix else prog["name"]).strip()[:19]
        idx = len(sf.presets)
        if idx >= 128 * 128:
            warnings.append(f"{prog['name']}: more than 16384 presets, skipped")
            continue
        sf.insts.append((pname, zones))
        sf.presets.append((pname, idx // 128, idx % 128, len(sf.insts) - 1,
                           int(round(5 * (max_loud - prog["loudness"])))))

    if borrowed:
        vols_from = sorted(set(borrowed.values()))
        warnings.append(f"note: {len(borrowed)} sample(s) used by this volume's programs are stored in "
                        f"another volume and were taken from there: {', '.join(vols_from)}")
    for name, why in sorted(flags.items()):
        dur = len(wavs[name.upper()]["channels"][0]) / float(wavs[name.upper()]["rate"] or 22050)
        if dur < 3.0:
            warnings.append(f"short note: '{name}' plays once and stops after {dur:.1f}s - {why}")
    for pname, items in env_short.items():
        rng = ", ".join(f"{lo}-{hi}" for lo, hi, _ in items[:8]) + (" ..." if len(items) > 8 else "")
        warnings.append(f"short note: '{pname}' envelope fades to near-silence within ~1s "
                        f"in key ranges {rng} (Akai decay/sustain settings)")
    if all_samples:
        for key, w in wavs.items():
            if key not in used:
                for c in range(len(w["channels"])):
                    sample_id(w["s3h"]["name"] if w["s3h"] else key, c, len(w["channels"]))
    else:
        unused = [w["file"] for k, w in wavs.items() if k not in used]
        if unused:
            warnings.append(f"{len(unused)} WAV(s) not used by any program (left out): "
                            + ", ".join(unused))
    if len(sf.presets) == first_preset:
        return None, warnings
    if merging:
        return dict(presets=len(sf.presets) - first_preset, samples=len(sf.samples) - first_sample,
                    zones=0, size=0), warnings
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(sf.build())
    stats = dict(presets=len(sf.presets), samples=len(sf.samples),
                 zones=sum(len(z) for _, z in sf.insts), size=os.path.getsize(out_path))
    return stats, warnings


def find_volumes(root):
    for dirpath, _, files in os.walk(root):
        if any(f.lower().endswith(".p3") for f in files):
            yield dirpath


def safe(name):
    return "".join("_" if c in '<>:"/\\|?*' else c for c in name).strip() or "volume"


PART_NAMES = {}


def load_part_names(path):
    """Text file, one partition per line, e.g.  'A. Piano/Keyboard/Mallet'.
    The text before the first '.' is the partition letter the folder is called."""
    names = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "." not in line:
                continue
            key = line.split(".", 1)[0].strip().upper()
            names[key] = line.replace("/", "-").replace("\\", "-")
    return names


def pretty(folder_name):
    """Partition folder name ('A', 'a', 'A_wav' ...) -> its full name, if one was supplied."""
    if not PART_NAMES:
        return folder_name
    key = folder_name.strip().upper()
    if key in PART_NAMES:
        return PART_NAMES[key]
    first = "".join(c if c.isalnum() else " " for c in key).split()
    if first and first[0] in PART_NAMES:
        return PART_NAMES[first[0]]
    return folder_name


def volume_prefix(vname):
    """Short tag put in front of preset names when volumes are merged: the volume
    number if the name starts with one (e.g. '3001 G.PF 2' -> '3001'), else its first letters."""
    parts = vname.split()
    if parts and parts[0].isdigit():
        return parts[0]
    return vname.strip()[:6]


def main():
    ap = argparse.ArgumentParser(description="Build SoundFonts from Akai .P3 programs + akaiutil WAVs")
    ap.add_argument("input", help="volume folder, or a folder tree containing volume folders")
    ap.add_argument("-o", "--output", help="output folder (default: <input>/../sf2_out)")
    ap.add_argument("--all-samples", action="store_true",
                    help="also store WAVs that no program uses (default: leave them out)")
    ap.add_argument("--no-envelopes", action="store_true",
                    help="do not translate Akai amp envelopes (keeps SoundFont defaults)")
    ap.add_argument("--merge", action="store_true",
                    help="ALSO write one combined .sf2 per partition (all volumes in the same parent "
                         "folder) into <output>/_merged/")
    ap.add_argument("--merge-only", action="store_true",
                    help="write only the combined per-partition .sf2 files, not one per volume")
    ap.add_argument("--names", metavar="FILE",
                    help="text file with full partition names, one per line ('A. Piano/Keyboard/Mallet'). "
                         "If omitted, partition_names.txt next to the script is used when it exists.")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    names_file = a.names
    if not names_file:
        guess = os.path.join(os.path.dirname(os.path.abspath(__file__)), "partition_names.txt")
        if os.path.isfile(guess):
            names_file = guess
    if names_file:
        PART_NAMES.update(load_part_names(names_file))
        print(f"Using partition names from {names_file} ({len(PART_NAMES)} entries)")
    do_merge = a.merge or a.merge_only

    root = os.path.abspath(a.input)
    out_root = os.path.abspath(a.output) if a.output else os.path.join(os.path.dirname(root), "sf2_out")
    vols = sorted(find_volumes(root))
    if not vols:
        sys.exit("No folders containing .P3 files were found under " + root)
    sib_by_parent = {}
    for v in vols:
        sib_by_parent.setdefault(os.path.dirname(v), []).append(v)
    sib_by_parent = {p: Siblings(vs) for p, vs in sib_by_parent.items()}
    log, ok, bad = [], 0, 0
    for v in ([] if a.merge_only else vols):
        rel = os.path.relpath(v, root)
        if rel == ".":
            sub, vname = "", os.path.basename(root)
        else:
            sub, vname = os.path.split(rel)
        sub = os.sep.join(safe(pretty(p)) for p in sub.split(os.sep)) if sub else sub
        out = os.path.join(out_root, sub, safe(vname) + ".sf2")
        try:
            stats, warns = convert_volume(v, out, a.all_samples, not a.no_envelopes, a.verbose,
                                          siblings=sib_by_parent[os.path.dirname(v)])
        except Exception as e:      # noqa
            stats, warns = None, [f"FAILED: {e}"]
        if stats:
            ok += 1
            line = (f"[ok]   {rel:40} -> {stats['presets']} presets, {stats['samples']} samples, "
                    f"{stats['zones']} zones, {stats['size'] / 1e6:.1f} MB")
        else:
            bad += 1
            line = f"[FAIL] {rel}"
        print(line)
        log.append(line)
        for w in warns:
            print("       warning:", w)
            log.append("       warning: " + w)
    if do_merge:
        groups = {}
        for v in vols:
            rel = os.path.relpath(v, root)
            parent = os.path.dirname(rel)
            gname = os.path.basename(parent) if parent else os.path.basename(root)
            groups.setdefault((parent, gname), []).append(v)
        for (parent, gname), members in sorted(groups.items()):
            gname = pretty(gname)
            sf = SF2(gname)
            nvol = 0
            for v in members:                      # sorted order = volume order on the disc
                try:
                    stats, warns = convert_volume(v, None, a.all_samples, not a.no_envelopes,
                                                  a.verbose, sf=sf, prefix=volume_prefix(os.path.basename(v)),
                                                  siblings=sib_by_parent[os.path.dirname(v)])
                except Exception as e:             # noqa
                    stats, warns = None, [f"FAILED: {e}"]
                if stats:
                    nvol += 1
                else:
                    bad += 1
                    line = f"[FAIL] merge: {os.path.relpath(v, root)}"
                    print(line); log.append(line)
                    for w in warns:
                        print("       warning:", w); log.append("       warning: " + w)
            if not sf.presets:
                continue
            out = os.path.join(out_root, "_merged", safe(gname) + ".sf2")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(sf.build())
            ok += 1
            line = (f"[ok]   MERGED {gname:33} -> {nvol} volumes, {len(sf.presets)} presets, "
                    f"{len(sf.samples)} samples, {os.path.getsize(out) / 1e6:.1f} MB")
            print(line); log.append(line)
    os.makedirs(out_root, exist_ok=True)
    with open(os.path.join(out_root, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(log) + "\n")
    print(f"\nDone: {ok} soundfont(s) written, {bad} failed. Output: {out_root}")


if __name__ == "__main__":
    main()
