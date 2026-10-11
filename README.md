# akai2sf2

Convert Akai S3000 / CD3000 sample libraries into SoundFont 2 (`.sf2`) files.

`akai2sf2.py` reads the program files (`.P3`) and the WAV samples exported by
akaiutil and builds ready-to-play SoundFonts that keep the
keymaps, velocity layers, loops, tuning, panning and an approximation of the amplitude envelope.

- One `.sf2` per Akai volume, and/or one combined `.sf2` per partition
- Samples are matched to programs automatically, using the Akai sample header that akaiutil embeds
  in each WAV. Nothing has to be located by hand
- Handles stereo programs whose `-L` and `-R` samples live in sibling volumes
- Reports anything it could not translate or that will sound unusual

> **This repository contains a converter only. It contains no Akai samples and no SoundFonts.**
> Sample libraries are normally copyrighted. See [Legal notes](#legal-notes).

---

## Requirements

- Python 3.8 or newer (python.org). On Windows, tick **"Add python.exe to PATH"** during install
- **akaiutil** by Klaus Michael Indlekofer, to read the Akai disc image (it runs on Windows, macOS and Linux; on macOS/Linux you compile it from source). https://sourceforge.net/projects/akaiutil/
- A way to extract `.tar` files (7-Zip, WinRAR, or `tar` on macOS/Linux)

Check Python with `python --version` (or `py --version` on Windows). If only `py` works, use `py`
wherever this guide says `python`.

---

## Quick start

### 1. Export the disc with akaiutil

Open the image read-only and export it with WAV conversion:

```
akaiutil.exe -r "C:\path\to\disc.iso"
```

At the `akaiutil` prompt:

```
df                                  list the partitions it found
cd /disk0/A                         enter a partition
cdinfo                              show the CD label
dir                                 list the volumes (type `cdi 1` to enter volume 1)
lcd C:\akai_out                     local folder the files are written to
tarcwav C:\akai_out\A_wav.tar       export programs as-is and samples as WAV
```

Go up with `cd ..`. `tarcwav` exports everything below the current directory, so running it from
`/disk0` exports the whole disc and running it inside a partition exports just that partition.
Volume names contain spaces. Use the volume's number with `cdi` or underscores in `cd`.

Use **`tarcwav`**, not `tarc`. The converter needs the WAV files, and akaiutil embeds the original
192-byte Akai sample header in each one (an `S3H ` chunk). That header carries the root note, loop
points and playback mode.

### 2. Extract the tar

Extract with 7-Zip (or `tar -xf`). Each volume ends up as a folder holding its `.P3` programs and
`.wav` samples side by side:

```
CD3000_wav\
  A\
    3001 G.PF 2\   (*.P3, *.wav, EFFECTS FILE.X, VOLPARAM.DAT)
    3012 E.PF 1\
  B\
  ...
```

Any folder structure works. The script treats every folder that contains `.P3` files as a volume.

### 3. Convert

One volume or a whole tree:

```
python akai2sf2.py "C:\akai_out\CD3000_wav" -o "C:\akai_out\sf2_all"
```

Always put paths in quotes (they contain spaces) and do not end a quoted path with a backslash.

---

## Command line

```
python akai2sf2.py INPUT [-o OUTPUT] [--merge | --merge-only] [--names FILE]
                         [--all-samples] [--no-envelopes] [-v]
```

| Option | What it does |
|---|---|
| `INPUT` | A volume folder, or any folder tree containing volume folders |
| `-o`, `--output` | Output folder. Default: `sf2_out` next to the input folder |
| `--merge` | **Also** write one combined `.sf2` per partition (all volumes sharing a parent folder) into `OUTPUT/_merged/` |
| `--merge-only` | Write only the combined partition files |
| `--names FILE` | Text file with full partition names (see below). If omitted, `partition_names.txt` next to the script is used when present |
| `--all-samples` | Also store WAVs that no program uses. By default they are left out |
| `--no-envelopes` | Do not translate Akai amplitude envelopes (keeps SoundFont defaults) |
| `-v`, `--verbose` | Reserved for extra output |

### Examples

One folder per volume, mirroring the input tree:

```
python akai2sf2.py "C:\akai_out\CD3000_wav" -o "C:\akai_out\sf2_all"
```

One combined SoundFont per partition (A, B, C, ...):

```
python akai2sf2.py "C:\akai_out\CD3000_wav" -o "C:\akai_out\sf2_all" --merge-only
```

A single partition:

```
python akai2sf2.py "C:\akai_out\CD3000_wav\A" -o "C:\akai_out\sf2_A"
```

### Output layout

```
sf2_all\
  A\3001 G.PF 2.sf2            one file per volume   (omitted with --merge-only)
  A\3012 E.PF 1.sf2
  _merged\A.sf2                one file per partition (--merge / --merge-only)
  report.txt                   what was converted, plus every warning
```

Existing `.sf2` files with the same name are overwritten. Files you have renamed are left alone.

### Merged files

- Every program from the partition's volumes becomes a preset, in disc order.
- Preset names get the volume number in front so you can tell where they came from
  (for example `3001 B-GRAND PF`). Names are limited to 19 characters.
- Presets are numbered 0 to 127 in bank 0. If a partition has more than 128 presets the rest go to
  bank 1, and so on. Some players show only bank 0 by default.
- Identical samples are stored once, even if several volumes use them.

### Naming partitions

Partitions are folders called `A`, `B`, ... To give them descriptive names, create
`partition_names.txt` next to the script with one line per partition. The text before the first `.`
is the folder's letter:

```
A. Piano/Keyboard/Mallet
B. Strings
C. Brass/Wind
```

The names are applied to the output folders, to the merged filenames and to the name stored inside
each `.sf2`. Characters Windows does not allow in filenames are replaced, and `/` becomes `-`
(`A. Piano-Keyboard-Mallet`). Folder names such as `a` or `A_wav` still match. Partitions that are
not listed keep their plain name.

---

## How it works

An Akai program (`.P3`) is a header plus up to 99 keygroups. Each keygroup has a key range, tuning,
an amplitude envelope, and up to four velocity zones that each name a sample. The script:

1. Reads each `.P3` and collects the sample names each zone uses.
2. Loads the WAVs in the volume and indexes them by the sample name stored inside the file, not by
   file name.
3. Builds one SoundFont instrument per program and one preset per instrument. Each velocity zone
   becomes a SoundFont zone, with the sample, loop and tuning taken from its header.
4. Writes everything as a standard SoundFont 2.01 file.

### What is translated

- Key ranges, velocity ranges and root key
- Tuning (keygroup + zone + program), including the automatic half-rate compensation the Akai
  header stores for 22.05 kHz samples
- Sample loops and loop mode (loop, loop until release, one-shot)
- Pan (program + zone) and per-zone loudness offsets
- Program loudness, relative to the loudest program in the volume
- An approximation of the amplitude envelope (attack, decay, sustain, release)
- Real stereo WAVs as linked left/right samples
- Stereo programs whose `-L` samples are in one volume and `-R` samples in a sibling volume. A
  missing sample is looked up in the other volumes of the same partition, and only when that is
  unambiguous. The report says which volume it came from

### What is not translated

Filters and filter envelopes, LFOs, modulation routing, effects (`EFFECTS FILE.X`), velocity and
keygroup crossfades, mute groups, individual output routing and `VOLPARAM.DAT`. SoundFont has no
equivalent for most of these, so some patches sound brighter or less animated than on the hardware.
The envelope time scales are an approximation and may need tweaking for some sounds.

Scope: only S3000-family programs (`.P3`) are read. S900/S1000 programs and drum files are not
handled. It has been developed and tested against a CD3000 library.

---

## Reading the report

`report.txt` (also printed to the console) has one line per file (example output):

```
[ok]   A/3001 G.PF 2          -> 4 presets, 18 samples, 92 zones, 1.5 MB
[ok]   MERGED A. Piano-Keyboard-Mallet -> 14 volumes, 61 presets, 203 samples, 34.0 MB
```

Warnings appear underneath:

| Warning | Meaning |
|---|---|
| `sample '...' has no matching WAV in this folder - zone skipped` | A program uses a sample that is in no volume the script can see. Check the export |
| `note: N sample(s) ... were taken from [volume]` | Informational. A stereo or shared sample was found in a sibling volume |
| `short note: '...' plays once and stops ... one-shot by design` | The sample is meant to be a one-shot, so a short sound is correct |
| `short note: ... loop points are invalid ...` or `... dwell is 0` | The Akai header has no usable loop, so the note will not sustain. Worth a look |
| `short note: '<program>' envelope fades to near-silence within ~1s` | The program's decay and sustain settings make it die quickly. Compare with `--no-envelopes` |
| `N WAV(s) not used by any program (left out)` | Samples in the folder that no program references. `--all-samples` keeps them |
| `no Akai header inside, matching by file name only` | The WAV was not exported by akaiutil, so root note and loops are unavailable (root defaults to C4) |

---

## Troubleshooting

- **`python` is not recognized.** Reinstall Python with "Add python.exe to PATH" ticked, or use `py`.
- **"No folders containing .P3 files were found".** Point at the extracted export, not at the
  `.tar` or the `.iso`. Make sure it was exported with `tarcwav` and that `.P3` files are present.
- **A patch plays one short click.** Look for a `short note:` warning for that sample. An
  invalid or missing loop is the usual cause.
- **A stereo patch is panned to one side.** One half of the pair is probably in a volume the script
  cannot see. Keep both volumes of the partition in the same parent folder.
- **A preset seems to be missing in a merged file.** Check the bank selector in your player (banks
  hold 128 presets each).

---

## Legal notes

- This repository contains **no Akai audio, no SoundFonts and no copy of akaiutil**.
- Commercial sample libraries are normally licensed for use in your own productions, not for
  redistribution. Converting a library to another format does not change who owns it, so **do not
  upload converted SoundFonts or the WAV/P3 files of a library you do not have redistribution rights
  to**. Keep them out of your repository (see `.gitignore` below) and out of public hosting sites.
- akaiutil has its own license and usage terms. Download it from its author rather than
  redistributing it here.

Suggested `.gitignore`:

```
*.sf2
*.wav
*.P3
*.p3
*.S3
*.tar
*.iso
sf2_out/
```

---

## Credits

- Akai disk and file format documentation: Hiroyuki Ohsaki (akaitools), and the S2800/S3000/S3200
  MIDI System Exclusive documentation transcribed by Frank Neumann (lakai)
- akaiutil by Klaus Michael Indlekofer, used to read Akai disks and export WAV
- SoundFont 2.01 specification, E-mu / Creative

## License

`MIT`
