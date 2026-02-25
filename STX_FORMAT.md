# STX File Format Notes

Technical documentation for StackTraxx `.stx` files, reverse-engineered during development of STX Recover.

## File Structure Overview

STX files are multi-track FLAC containers used by StackTraxx music software. Each file contains 5 interleaved audio tracks (stems) that can be mixed together.

```
[Metadata Header]
[Track Names (UTF-16LE)]
[Header Offset Table]
[Frame Index Table]
[FLAC Headers × 5]
[Interleaved Audio Frames]
```

## Format Versions

### LFLAC (Original)
- Fixed 86-byte FLAC headers per track
- Header offset table at `0xb0 + trackCount * 0x13c + 8`

### LFLC2 (Newer)
- Magic bytes: `LFLC2` at offset 4
- Variable header sizes based on audio duration:
  - 10 seconds: 1143 bytes
  - 15 seconds: 2286 bytes (2×)
  - 30 seconds: 3429 bytes (3×)
  - 60 seconds: 4572 bytes (4×)
  - Full length: 5715 bytes (5×)

## Key Structures

### FLAC Header Detection
- Magic: `fLaC` (0x66 0x4C 0x61 0x43)
- Header size = distance between first and second `fLaC` markers
- Track count = number of consecutive `fLaC` markers at header intervals

### Frame Index Table
Located before the FLAC headers. For files with `headerSize <= 1143`, the stored index is valid. For larger headers, the stored index contains garbage offsets and must be reconstructed.

**Finding the table:**
```
Search for pattern: [0, headerSize, headerSize*2, headerSize*3, headerSize*4]
Frame index starts immediately after this header offset table.
```

### FLAC Frame Structure
Each audio frame starts with a sync code and contains:
```
Bytes 0-1:  Sync code (0xFFF8 fixed block, 0xFFF9 variable block)
Byte 2:     [block_size_code:4][sample_rate_code:4]
Byte 3:     [channel_code:4][sample_size:3][reserved:1]
Byte 4+:    UTF-8 encoded frame/sample number
...         Frame data
Last byte:  CRC-8
```

### Frame Number Encoding (UTF-8 style)
- 1-byte: `0xxxxxxx` (0-127)
- 2-byte: `110xxxxx 10xxxxxx` (128-2047)
- 3-byte: `1110xxxx 10xxxxxx 10xxxxxx` (2048+)

## Audio Parameters

From STREAMINFO block (starts at `fLaC` + 8):
- Bytes 0-1: Min block size
- Bytes 2-3: Max block size (typically 576 samples, NOT 4608)
- Bytes 10-12: Sample rate (20 bits) = 44100 Hz

**Duration calculation:**
```
frames_per_track = duration_seconds × sample_rate / block_size
                 = 15 × 44100 / 576 = 1148 frames
```

## Frame Interleaving

Frames are stored grouped by frame number, with all 5 tracks' frames for each number stored consecutively:

```
[Frame 0: T0, T1, T2, T3, T4]
[Frame 1: T0, T1, T2, T3, T4]
[Frame 2: T0, T1, T2, T3, T4]
...
```

Frame numbers are sequential (0, 1, 2, 3...), not the alternating +3/+4 pattern initially assumed.

## Reconstruction Algorithm

For files with broken stored indices (`headerSize > 1143`):

1. **Find expected block_size and channel codes** by scanning first ~500 sync codes
2. **Scan all sync codes** matching expected block_size/channel
3. **Parse frame numbers** from UTF-8 encoded values at byte 4
4. **Group frames by frame number**
5. **Filter**: Keep only frame numbers with exactly 5 frames (removes false positives)
6. **Build index**: Flatten groups in frame number order

### False Positive Handling

Sync codes (0xFFF8/0xFFF9) can appear randomly in compressed audio data. These are filtered by:
- Requiring matching block_size and channel codes
- Requiring exactly `trackCount` frames per frame number

**Important:** Do NOT filter by minimum frame size - this incorrectly removes valid frames that happen to precede false positive sync codes.

## Track Extraction

For each track `i` (0-4):
1. Start with FLAC header from `firstFlac + i × headerSize`
2. Extract frames at indices `i, i+5, i+10, ...` from the frame index
3. For each frame, find boundaries by scanning for next sync code
4. Concatenate header + frames into valid FLAC stream
5. Convert to WAV with ffmpeg

## Delay/Alignment

- **Stored indices**: Calculate delay from first frame's frame number × block_size / sample_rate
- **Reconstructed indices**: No delay needed - frames are already in correct order

## Lessons Learned

1. **Block size matters**: FLAC block size is in STREAMINFO metadata, not hardcoded (576 vs 4608 = 8× difference)
2. **Stored indices can be broken**: Files with larger headers have invalid stored frame offsets
3. **False positives are tricky**: Sync codes appear in audio data; filter by frame count, not frame size
4. **Frame numbers are sequential**: Not interleaved pattern like initially assumed
5. **Test with multiple file lengths**: 10s worked, 15s/30s exposed different bugs
