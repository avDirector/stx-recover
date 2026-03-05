#!/usr/bin/env python3
"""
stx2wav - Convert StackTraxx .stx files to WAV

Extracts individual stems and creates a combined mix from STX multi-track
FLAC containers. Requires ffmpeg.

Usage:
    python3 stx2wav.py input.stx [output_directory]
    python3 stx2wav.py *.stx                          # batch mode
    python3 stx2wav.py input.stx -o ~/Desktop/output
"""

import struct
import subprocess
import sys
import tempfile
import shutil
import os
from pathlib import Path

FLAC_MAGIC = b"fLaC"
LFLC2_MAGIC = b"LFLC2"
BASE_HEADER_SIZE = 1143


def find_ffmpeg():
    """Locate ffmpeg binary."""
    path = shutil.which("ffmpeg")
    if path:
        return path
    for candidate in [
        os.path.expanduser("~/bin/ffmpeg"),
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/usr/bin/ffmpeg",
    ]:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def read_u32_le(data, offset):
    """Read a little-endian uint32."""
    if offset + 4 > len(data):
        return 0
    return struct.unpack_from("<I", data, offset)[0]


def detect_format(data):
    """Detect STX format version and header size."""
    if len(data) < 10:
        return ("lflac", 86)
    if data[4:9] == LFLC2_MAGIC:
        header_size = detect_header_size(data)
        return ("lflc2", header_size)
    return ("lflac", 86)


def detect_header_size(data):
    """Measure distance between first two fLaC markers."""
    first = data.find(FLAC_MAGIC)
    if first < 0:
        return BASE_HEADER_SIZE
    second = data.find(FLAC_MAGIC, first + 100)
    if second < 0:
        return BASE_HEADER_SIZE
    return second - first


def count_tracks(data, first_flac, header_size):
    """Count consecutive fLaC markers at header_size intervals."""
    count = 1
    pos = first_flac + header_size
    while pos + 4 <= len(data):
        if data[pos : pos + 4] == FLAC_MAGIC:
            count += 1
            pos += header_size
        else:
            break
    return count


def parse_track_names(data, count, fmt, header_size):
    """Extract track names encoded as UTF-16LE."""
    names = []
    track_marker = "Track".encode("utf-16-le")
    search_limit = min(len(data), 20000 if fmt == "lflc2" else 4000)
    search_start = 0

    for _ in range(count):
        idx = data.find(track_marker, search_start, search_limit)
        if idx < 0:
            names.append(f"Track {len(names) + 1}")
            continue

        end = idx
        while end + 2 <= len(data):
            if data[end] == 0 and data[end + 1] == 0:
                if end + 3 < len(data) and data[end + 2] == 0:
                    break
            end += 2

        try:
            name = data[idx:end].decode("utf-16-le")
            names.append(extract_track_description(name))
        except (UnicodeDecodeError, ValueError):
            names.append(f"Track {len(names) + 1}")
        search_start = end

    # Rotate: move first name to end (matches Swift app behavior)
    if len(names) > 1:
        names.append(names.pop(0))
    return names


def extract_track_description(full_name):
    """Pull the descriptive part from a track filename."""
    name = full_name
    dot = name.rfind(".")
    if dot >= 0:
        name = name[:dot]
    underscore = name.rfind("_")
    if underscore >= 0:
        part = name[underscore + 1 :]
        if part:
            return part
    if name.startswith("Track "):
        parts = name.split(" ", 2)
        if len(parts) >= 2:
            return f"Track {parts[1]}"
    return name


def parse_sample_rate(data, header_offset):
    """Read sample rate from FLAC STREAMINFO block."""
    si = header_offset + 8
    if si + 13 > len(data):
        return 44100
    b10, b11, b12 = data[si + 10], data[si + 11], data[si + 12]
    rate = (b10 << 12) | (b11 << 4) | (b12 >> 4)
    return rate if rate > 0 else 44100


def parse_block_size(data, header_offset):
    """Read max block size from FLAC STREAMINFO block."""
    si = header_offset + 8
    if si + 4 > len(data):
        return 4608
    max_bs = (data[si + 2] << 8) | data[si + 3]
    return max_bs if max_bs > 0 else 4608


def parse_frame_number(data, pos):
    """Parse UTF-8 style frame number starting at pos."""
    if pos >= len(data):
        return -1
    b = data[pos]
    if b < 0x80:
        return b
    if 0xC0 <= b < 0xE0 and pos + 1 < len(data):
        return ((b & 0x1F) << 6) | (data[pos + 1] & 0x3F)
    if 0xE0 <= b < 0xF0 and pos + 2 < len(data):
        return ((b & 0x0F) << 12) | ((data[pos + 1] & 0x3F) << 6) | (data[pos + 2] & 0x3F)
    return -1


def parse_frame_index_lflac(data, first_flac, track_count):
    """Parse stored frame index for original LFLAC format.

    Returns (frame_index, boundary_offsets) where boundary_offsets is used for
    frame boundary detection (same as frame_index for stored indices).
    """
    header_offsets_start = 0xB0 + track_count * 0x13C + 8
    frame_index_start = header_offsets_start + track_count * 4
    offsets = []
    pos = frame_index_start
    while pos + 4 <= first_flac:
        val = read_u32_le(data, pos)
        if val == 0:
            break
        offsets.append(val)
        pos += 4
    if not offsets:
        raise ValueError("No frame index table found in STX file.")
    return offsets, sorted(offsets)


def parse_frame_index_lflc2(data, first_flac, track_count, header_size):
    """Parse or reconstruct frame index for LFLC2 format.

    Returns (frame_index, boundary_offsets) where boundary_offsets includes ALL
    frame positions per group (not just the first track_count) for accurate frame
    boundary detection when there are more actual tracks than detected.
    """
    if header_size > BASE_HEADER_SIZE:
        return reconstruct_frame_index(data, first_flac, track_count, header_size)

    # Find the header offset table pattern
    search_end = first_flac - (track_count + 1) * 4
    table_start = None
    for pos in range(0, search_end, 4):
        val0 = read_u32_le(data, pos)
        val1 = read_u32_le(data, pos + 4)
        if val0 == 0 and val1 == header_size:
            match = True
            for i in range(2, track_count):
                if read_u32_le(data, pos + i * 4) != i * header_size:
                    match = False
                    break
            if match:
                table_start = pos
                break

    if table_start is None:
        raise ValueError("No frame index table found in STX file.")

    frame_index_start = table_start + (track_count + 1) * 4
    offsets = []
    pos = frame_index_start
    while pos + 4 <= first_flac:
        val = read_u32_le(data, pos)
        if val == 0:
            break
        offsets.append(val)
        pos += 4

    if not offsets:
        raise ValueError("No frame index table found in STX file.")
    return offsets, sorted(offsets)


def reconstruct_frame_index(data, first_flac, track_count, header_size):
    """Rebuild frame index by scanning sync codes (for broken stored indices).

    Returns (frame_index, boundary_offsets):
    - frame_index: first track_count offsets per valid group (for extraction)
    - boundary_offsets: ALL offsets per valid group (for frame boundary detection),
      which correctly handles files with more actual tracks than detected.
    """
    audio_start = first_flac + track_count * header_size

    # Pass 1: find most common block_size and channel codes
    bs_counts = {}
    ch_counts = {}
    pos = audio_start
    total = 0
    while pos + 6 < len(data) and total < 500:
        sync = (data[pos] << 8) | data[pos + 1]
        if sync in (0xFFF8, 0xFFF9):
            bs_code = data[pos + 2] >> 4
            sr_code = data[pos + 2] & 0x0F
            ch_code = data[pos + 3] >> 4
            if bs_code > 0 and sr_code < 15 and ch_code <= 10:
                bs_counts[bs_code] = bs_counts.get(bs_code, 0) + 1
                ch_counts[ch_code] = ch_counts.get(ch_code, 0) + 1
                total += 1
                pos += 10
            else:
                pos += 1
        else:
            pos += 1

    if not bs_counts:
        raise ValueError("No frame index table found in STX file.")

    expected_bs = max(bs_counts, key=bs_counts.get)
    expected_ch = max(ch_counts, key=ch_counts.get)

    # Pass 2: collect all sync positions with frame numbers
    syncs = []
    pos = audio_start
    while pos + 6 < len(data):
        sync_val = (data[pos] << 8) | data[pos + 1]
        if sync_val in (0xFFF8, 0xFFF9):
            bs_code = data[pos + 2] >> 4
            sr_code = data[pos + 2] & 0x0F
            ch_code = data[pos + 3] >> 4
            if bs_code == expected_bs and sr_code < 15 and ch_code == expected_ch:
                fn = parse_frame_number(data, pos + 4)
                if fn >= 0:
                    syncs.append((pos - first_flac, fn))
                pos += 6
            else:
                pos += 1
        else:
            pos += 1
        if len(syncs) > 2000000:
            break

    if not syncs:
        raise ValueError("No frame index table found in STX file.")

    # Group by frame number. Some files have more actual tracks than count_tracks()
    # detected (later tracks have smaller header slots). Accept any group with at
    # least track_count entries and take the first track_count (sorted by position).
    from collections import defaultdict

    groups = defaultdict(list)
    for offset, fn in syncs:
        groups[fn].append(offset)

    offsets = []
    all_offsets = []
    for fn in sorted(groups):
        if len(groups[fn]) >= track_count:
            sorted_group = sorted(groups[fn])
            offsets.extend(sorted_group[:track_count])
            all_offsets.extend(sorted_group)  # ALL entries — needed for correct boundaries

    if not offsets:
        raise ValueError("No frame index table found in STX file.")
    return offsets, sorted(all_offsets)


def is_grouped_lflc2_format(data, first_flac, entries):
    """Return True if entries are segment boundaries (new grouped format) vs per-frame offsets."""
    if len(entries) < 2:
        return False
    # In the grouped format each entry spans a whole track-segment (multiple FLAC frames).
    # Detect by counting sync codes between the first two entries.
    start = first_flac + entries[0]
    end = first_flac + entries[1]
    count = 0
    pos = start
    while pos + 4 <= end and count < 2:
        sync = (data[pos] << 8) | data[pos + 1]
        if sync in (0xFFF8, 0xFFF9):
            bs = data[pos + 2] >> 4
            sr = data[pos + 2] & 0x0F
            ch = data[pos + 3] >> 4
            if bs > 0 and sr < 15 and ch <= 10:
                count += 1
                pos += 6
                continue
        pos += 1
    return count >= 2


def extract_track_bytes_grouped(data, first_flac, track_count, header_size, entries):
    """
    Extract raw audio bytes per track for the new grouped LFLC2 format.

    entries[g*track_count + t] is the END offset (relative to first_flac) of
    group g's track-t segment.  Audio begins at first_flac + track_count*header_size.

    Returns a list of bytearrays, one per track (audio only, no FLAC header).
    """
    audio_start_rel = track_count * header_size
    num_groups = len(entries) // track_count
    track_bufs = [bytearray() for _ in range(track_count)]

    for g in range(num_groups):
        for t in range(track_count):
            entry_idx = g * track_count + t
            seg_start = audio_start_rel if (g == 0 and t == 0) else entries[entry_idx - 1]
            seg_end = entries[entry_idx]
            abs_start = first_flac + seg_start
            abs_end = first_flac + seg_end
            if abs_end > abs_start and abs_end <= len(data):
                track_bufs[t].extend(data[abs_start:abs_end])

    return track_bufs


def find_frame_end(data, frame_start):
    """Scan forward for the next FLAC sync code to find frame boundary."""
    pos = frame_start + 6
    max_scan = min(frame_start + 20000, len(data))
    while pos + 2 <= max_scan:
        sync = (data[pos] << 8) | data[pos + 1]
        if sync in (0xFFF8, 0xFFF9) and pos + 4 <= len(data):
            bs_code = data[pos + 2] >> 4
            sr_code = data[pos + 2] & 0x0F
            ch_code = data[pos + 3] >> 4
            if bs_code > 0 and sr_code < 15 and ch_code <= 10:
                return pos
        pos += 1
    return len(data)


def parse_frame_offset(data, frame_offset):
    """Parse frame/sample number from a FLAC frame header."""
    if frame_offset + 6 > len(data):
        return (0, 0)
    sync = (data[frame_offset] << 8) | data[frame_offset + 1]
    if sync not in (0xFFF8, 0xFFF9):
        return (0, 0)
    variable_block = (data[frame_offset + 1] & 0x01) == 1
    coded_start = frame_offset + 4
    if coded_start >= len(data):
        return (0, 0)

    b = data[coded_start]
    value = 0
    if b < 0x80:
        value = b
    elif 0xC0 <= b < 0xE0 and coded_start + 1 < len(data):
        value = ((b & 0x1F) << 6) | (data[coded_start + 1] & 0x3F)
    elif 0xE0 <= b < 0xF0 and coded_start + 2 < len(data):
        value = ((b & 0x0F) << 12) | ((data[coded_start + 1] & 0x3F) << 6) | (data[coded_start + 2] & 0x3F)

    return (0, value) if variable_block else (value, 0)


def run_ffmpeg(ffmpeg, input_path, output_path):
    """Convert a single file with ffmpeg."""
    result = subprocess.run(
        [ffmpeg, "-y", "-i", input_path, output_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0 and not os.path.exists(output_path):
        stderr = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}): {stderr}")


def mix_tracks(ffmpeg, track_paths, delays, output_path):
    """Mix multiple WAV tracks with optional delays."""
    args = [ffmpeg, "-y"]
    for t in track_paths:
        args += ["-i", str(t)]

    filter_parts = []
    for i, delay in enumerate(delays):
        if delay > 0:
            filter_parts.append(f"[{i}:a]adelay={int(delay * 1000)}:all=1[a{i}]")
        else:
            filter_parts.append(f"[{i}:a]acopy[a{i}]")

    delayed = "".join(f"[a{i}]" for i in range(len(track_paths)))
    mix_filter = f"{delayed}amix=inputs={len(track_paths)}:duration=longest:normalize=0"
    full_filter = ";".join(filter_parts) + ";" + mix_filter

    args += ["-filter_complex", full_filter, str(output_path)]

    result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if result.returncode != 0 and not os.path.exists(str(output_path)):
        stderr = result.stderr.decode("utf-8", errors="replace")[-1200:]
        raise RuntimeError(f"ffmpeg mix failed (exit {result.returncode}): {stderr}")


def sanitize_filename(name):
    """Remove characters unsafe for filenames."""
    for ch in '/\\:*?"<>|':
        name = name.replace(ch, "_")
    return name


def convert_stx(stx_path, output_dir=None):
    """
    Convert an STX file to individual WAV stems + a combined mix.

    Returns (track_wav_paths, combined_wav_path, track_names).
    """
    stx_path = Path(stx_path)
    if stx_path.suffix.lower() != ".stx":
        raise ValueError(f"Not an .stx file: {stx_path.name}")

    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install with: brew install ffmpeg  (macOS) or apt install ffmpeg  (Linux)")

    data = stx_path.read_bytes()
    base_name = stx_path.stem
    out = Path(output_dir) if output_dir else stx_path.parent
    out.mkdir(parents=True, exist_ok=True)

    fmt, header_size = detect_format(data)
    first_flac = data.find(FLAC_MAGIC)
    if first_flac < 0:
        raise ValueError("No embedded FLAC stream found (missing 'fLaC' marker).")

    track_count = count_tracks(data, first_flac, header_size)
    track_names = parse_track_names(data, track_count, fmt, header_size)

    print(f"  Format: {fmt.upper()}, header size: {header_size}, tracks: {track_count}")
    for i, name in enumerate(track_names):
        print(f"    Track {i + 1}: {name}")

    # Single-track file
    if track_count == 1:
        wav = out / f"{base_name}.wav"
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
            tmp.write(data[first_flac:])
            tmp_path = tmp.name
        try:
            run_ffmpeg(ffmpeg, tmp_path, str(wav))
        finally:
            os.unlink(tmp_path)
        print(f"  -> {wav}")
        return ([wav], wav, track_names)

    # Multi-track: parse frame index
    if fmt == "lflac":
        frame_index, boundary_offsets = parse_frame_index_lflac(data, first_flac, track_count)
        grouped = False
    else:
        frame_index, boundary_offsets = parse_frame_index_lflc2(data, first_flac, track_count, header_size)
        # Small-header LFLC2 files use a START-offset stored index (not an END-offset
        # "grouped" chain).  The old is_grouped_lflc2_format heuristic mis-detected them
        # as grouped because each stored segment contains multiple FLAC frames.
        grouped = False

    if grouped:
        print(f"  (grouped LFLC2 — segment-per-track interleaving)")

    use_stored_index = not grouped
    sample_rate = parse_sample_rate(data, first_flac)
    samples_per_frame = parse_block_size(data, first_flac)
    # boundary_offsets includes ALL frame positions per group (including from extra
    # tracks beyond track_count), ensuring correct frame end detection for track N-1.
    sorted_offsets = boundary_offsets if not grouped else []

    grouped_track_bytes = None  # grouped path disabled

    # Pre-index gap: small-header LFLC2 files (e.g. ABeautifulJourney) store one track's
    # initial frames (fn=0..X-1) in the region before the first stored frame index entry.
    # Detect when exactly one track's first stored frame has fn > 0, and prepend the gap
    # bytes to that track's audio so its stream starts at fn=0 with delay=0.
    pre_gap_track = None
    pre_gap_data = b""
    if fmt == "lflc2" and header_size <= BASE_HEADER_SIZE and use_stored_index and frame_index:
        audio_gap_start_abs = first_flac + track_count * header_size
        first_idx_abs = first_flac + frame_index[0]
        if first_idx_abs > audio_gap_start_abs:
            late_tracks = []
            for t in range(track_count):
                t_frames = frame_index[t::track_count]
                if t_frames:
                    fn_t, sn_t = parse_frame_offset(data, first_flac + t_frames[0])
                    sample_off = sn_t if sn_t > 0 else fn_t * samples_per_frame
                    if sample_off > 0:
                        late_tracks.append(t)
            if len(late_tracks) == 1:
                pre_gap_track = late_tracks[0]
                pre_gap_data = bytes(data[audio_gap_start_abs:first_idx_abs])

    track_wavs = []
    track_delays = []
    temp_files = []

    for track in range(track_count):
        h_start = first_flac + track * header_size
        h_end = h_start + header_size
        if h_start < 0 or h_end > len(data):
            continue

        # For LFLC2 files, the stride between fLaC markers (header_size) may exceed the
        # actual 1143-byte FLAC metadata.  The gap can contain the track's initial audio
        # frames (fn=0, 1, 2, …) which precede the main interleaved audio area.  Include
        # those frames up to the first backward fn jump, which marks a duplicate copy.
        if fmt == "lflc2" and header_size > BASE_HEADER_SIZE:
            extra_start = h_start + BASE_HEADER_SIZE
            extra_data = data[extra_start:h_end]
            prev_fn = -1
            stop_pos = len(extra_data)       # assume include all until proven otherwise
            pos = 0
            while pos + 4 < len(extra_data):
                sync = (extra_data[pos] << 8) | extra_data[pos + 1]
                if sync in (0xFFF8, 0xFFF9):
                    bs = extra_data[pos + 2] >> 4
                    sr = extra_data[pos + 2] & 0x0F
                    ch = extra_data[pos + 3] >> 4
                    if bs > 0 and sr < 15 and ch <= 10:
                        fn = parse_frame_number(extra_data, pos + 4)
                        if fn >= 0:
                            if fn <= prev_fn:
                                stop_pos = pos   # backward jump → duplicate, stop here
                                break
                            prev_fn = fn
                pos += 1
            if prev_fn == -1:
                stop_pos = 0  # no valid frames found — gap is empty/garbage, include nothing
            track_data = bytearray(data[h_start : h_start + BASE_HEADER_SIZE])
            track_data.extend(data[extra_start : extra_start + stop_pos])
        else:
            track_data = bytearray(data[h_start:h_end])

        if grouped:
            track_data.extend(grouped_track_bytes[track])
            track_delays.append(0.0)
        else:
            track_frame_offsets = frame_index[track::track_count]
            if not track_frame_offsets:
                continue

            # Calculate delay for stored indices
            if pre_gap_track is not None and track == pre_gap_track:
                # Pre-gap frames start at fn=0 — no delay needed
                track_delays.append(0.0)
            elif use_stored_index and header_size <= BASE_HEADER_SIZE and track_frame_offsets:
                first_offset = track_frame_offsets[0]
                frame_abs = first_flac + first_offset
                frame_num, sample_num = parse_frame_offset(data, frame_abs)
                sample_offset = sample_num if sample_num > 0 else frame_num * samples_per_frame
                track_delays.append(sample_offset / sample_rate)
            else:
                track_delays.append(0.0)

            # Prepend pre-index gap frames (fn=0..X-1) for the identified track
            if pre_gap_track is not None and track == pre_gap_track:
                track_data.extend(pre_gap_data)

            # Extract frames
            for offset in track_frame_offsets:
                frame_start = first_flac + offset
                if use_stored_index:
                    try:
                        next_idx = next(i for i, o in enumerate(sorted_offsets) if o > offset)
                        frame_end = first_flac + sorted_offsets[next_idx]
                    except StopIteration:
                        frame_end = len(data)
                else:
                    frame_end = find_frame_end(data, frame_start)

                if 0 <= frame_start < len(data) and frame_end > frame_start and frame_end <= len(data):
                    track_data.extend(data[frame_start:frame_end])

        # Write temp FLAC and convert to WAV
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as tmp:
            tmp.write(track_data)
            tmp_path = tmp.name
        temp_files.append(tmp_path)

        safe_name = sanitize_filename(track_names[track] if track < len(track_names) else f"Track {track + 1}")
        wav_path = out / f"{base_name}_{safe_name}.wav"
        run_ffmpeg(ffmpeg, tmp_path, str(wav_path))
        track_wavs.append(wav_path)
        print(f"  -> {wav_path.name}")

    # Create combined mix
    combined = out / f"{base_name}_Combined.wav"
    mix_tracks(ffmpeg, [str(w) for w in track_wavs], track_delays, str(combined))
    print(f"  -> {combined.name}  (combined mix)")

    # Cleanup temp files
    for tmp in temp_files:
        try:
            os.unlink(tmp)
        except OSError:
            pass

    return (track_wavs, combined, track_names)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__.strip())
        sys.exit(0)

    # Parse arguments
    args = sys.argv[1:]
    output_dir = None

    if "-o" in args:
        idx = args.index("-o")
        if idx + 1 < len(args):
            output_dir = args[idx + 1]
            args = args[:idx] + args[idx + 2 :]
        else:
            print("Error: -o requires an output directory", file=sys.stderr)
            sys.exit(1)

    stx_files = [a for a in args if a.lower().endswith(".stx")]
    if not stx_files:
        print("Error: No .stx files provided.", file=sys.stderr)
        sys.exit(1)

    # Check ffmpeg upfront
    if not find_ffmpeg():
        print("Error: ffmpeg not found.", file=sys.stderr)
        print("  macOS:  brew install ffmpeg", file=sys.stderr)
        print("  Linux:  sudo apt install ffmpeg", file=sys.stderr)
        print("  Windows: https://ffmpeg.org/download.html", file=sys.stderr)
        sys.exit(1)

    errors = 0
    for stx in stx_files:
        print(f"\nConverting: {stx}")
        try:
            convert_stx(stx, output_dir)
        except Exception as e:
            print(f"  ERROR: {e}", file=sys.stderr)
            errors += 1

    if errors:
        print(f"\n{errors} file(s) failed.", file=sys.stderr)
        sys.exit(1)

    print("\nDone.")


if __name__ == "__main__":
    main()
