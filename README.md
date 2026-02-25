# stx-recover

Command-line tool to convert StackTraxx `.stx` files into WAV audio files. Extracts individual stems (tracks) and creates a combined mix.

## Requirements

- **Python 3.7+** (no pip packages needed)
- **ffmpeg** installed and on your PATH

### Install ffmpeg

```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Windows (via chocolatey)
choco install ffmpeg
```

## Usage

```bash
# Convert a single file (outputs to same directory as the .stx file)
python3 stx2wav.py song.stx

# Convert to a specific output directory
python3 stx2wav.py song.stx -o ~/Desktop/output

# Batch convert multiple files
python3 stx2wav.py *.stx -o ./output
```

### Output

For a file called `MySong.stx` with 5 tracks (Drums, Bass, Guitar, Synth, Strings), you'll get:

```
MySong_Drums.wav
MySong_Bass.wav
MySong_Guitar.wav
MySong_Synth.wav
MySong_Strings.wav
MySong_Combined.wav    <- all tracks mixed together
```

## What are STX files?

STX files are multi-track FLAC containers used by StackTraxx music software. Each file contains up to 5 interleaved audio stems that can be mixed together. See [STX_FORMAT.md](STX_FORMAT.md) for the full reverse-engineered format specification.

## Supported formats

- **LFLAC** — Original format with fixed 86-byte FLAC headers
- **LFLC2** — Newer format with variable header sizes (10s, 15s, 30s, 60s, full-length files)
