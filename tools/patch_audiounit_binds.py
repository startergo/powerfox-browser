#!/usr/bin/env python3
"""
Patch XUL's lazy-bind opcodes so AudioComponent* symbols bind to AudioUnit
instead of AudioToolbox.

Why this exists
--------------
On Mac OS X 10.6.8, the Audio Component API (_AudioComponentFindNext,
_AudioComponentInstanceNew, _AudioComponentInstanceDispose, etc.) is defined
in AudioUnit.framework. On newer macOS SDKs (10.7+), Apple merged these
symbols into AudioToolbox.framework, where they live today.

PowerFox is built against the 10.6 SDK on a modern host. The linker records
the source library for each symbol as a "library ordinal" in the lazy_bind
stream of the Mach-O __LINKEDIT segment. For AudioComponent* symbols the
linker picks AudioToolbox (because that's where they live on the build
SDK's view of the 10.6 SDK headers — the headers were retroactively updated).

On a real 10.6.8 runtime, AudioToolbox.framework does NOT export these
symbols (they're undefined references, marked `U` in its symbol table), so
dyld aborts at launch with:

    dyld: lazy symbol binding failed: Symbol not found: _AudioComponentFindNext
      Referenced from: .../XUL
      Expected in: /System/Library/Frameworks/AudioToolbox.framework/...

This script rewrites the library ordinal attached to each AudioComponent
symbol in the lazy_bind stream so dyld binds them from AudioUnit instead.
It dynamically resolves the ordinals of AudioToolbox and AudioUnit from
the LC_LOAD_DYLIB load commands, so it keeps working if link order shifts.

Idempotent: if the symbols are already bound to AudioUnit, or are not
referenced at all, the script exits 0 without modifying the file.

Usage
-----
    patch_audiocomponent_binds.py <path-to-XUL> [--audit]

Exit codes
----------
    0  success (patches applied, already correct, or nothing to do)
    1  unexpected state (investigation needed)
    2  wrong arguments / usage
"""

import argparse
import struct
import sys


# ---------------------------------------------------------------------------
# Mach-O load command constants (mach-o/loader.h)
# ---------------------------------------------------------------------------

MH_MAGIC = 0xFEEDFACE       # 32-bit
MH_MAGIC_64 = 0xFEEDFACF    # 64-bit

LC_LOAD_DYLIB = 0x0C
LC_LOAD_WEAK_DYLIB = 0x80000018
LC_REEXPORT_DYLIB = 0x8000001F
LC_LAZY_LOAD_DYLIB = 0x20
ORDINAL_PROVIDING_LCMDS = {
    LC_LOAD_DYLIB,
    LC_LOAD_WEAK_DYLIB,
    LC_REEXPORT_DYLIB,
    LC_LAZY_LOAD_DYLIB,
}

LC_DYLD_INFO = 0x22
LC_DYLD_INFO_ONLY = 0x80000022


# ---------------------------------------------------------------------------
# Bind opcodes (mach-o/loader.h)
# ---------------------------------------------------------------------------

BIND_OPCODE_DONE = 0x00
BIND_OPCODE_SET_DYLIB_ORDINAL_IMM = 0x10  # 0x10 | ordinal (ordinal <= 15)
BIND_OPCODE_SET_DYLIB_ORDINAL_ULEB = 0x20
BIND_OPCODE_SET_DYLIB_SPECIAL_IMM = 0x30  # 0x30 | special-ordinal
BIND_OPCODE_SET_SYMBOL_TRAILING_FLAGS_IMM = 0x40  # 0x40 | flags
BIND_OPCODE_SET_TYPE_IMM = 0x50  # 0x50 | type
BIND_OPCODE_SET_ADDEND_SLEB = 0x60
BIND_OPCODE_SET_SEGMENT_AND_OFFSET_ULEB = 0x70  # 0x70 | segment
BIND_OPCODE_ADD_ADDR_ULEB = 0x80
BIND_OPCODE_DO_BIND = 0x90
BIND_OPCODE_DO_BIND_ADD_ADDR_ULEB = 0xA0
BIND_OPCODE_DO_BIND_ADD_ADDR_IMM_SCALED = 0xB0  # 0xB0 | scale
BIND_OPCODE_DO_BIND_ULEB_TIMES_SKIPPING_ULEB = 0xC0
BIND_OPCODE_THREADED = 0xD0


# Symbols that must be rebound from AudioToolbox -> AudioUnit on 10.6.
# This is the complete set of public exports from AudioUnit.framework on
# Mac OS X 10.6.8 (extracted via `nm -arch x86_64` against the shipping
# binary). On newer macOS these all live in AudioToolbox.framework (AudioUnit
# was demoted to a re-export stub), so a binary linked against a modern SDK
# records AudioToolbox as the source library for every one of them — and
# fails to load on 10.6.8 where AudioToolbox only has undefined references
# to them. Used as the fallback when --audiounit-framework is not given.
AUDIOUNIT_EXPORTS_10_6 = frozenset({
    b"_AudioCodecAppendInputData",
    b"_AudioCodecGetProperty",
    b"_AudioCodecGetPropertyInfo",
    b"_AudioCodecInitialize",
    b"_AudioCodecProduceOutputPackets",
    b"_AudioCodecReset",
    b"_AudioCodecSetProperty",
    b"_AudioCodecUninitialize",
    b"_AudioComponentCopyName",
    b"_AudioComponentCount",
    b"_AudioComponentFindNext",
    b"_AudioComponentGetDescription",
    b"_AudioComponentGetVersion",
    b"_AudioComponentInstanceCanDo",
    b"_AudioComponentInstanceDispose",
    b"_AudioComponentInstanceGetComponent",
    b"_AudioComponentInstanceNew",
    b"_AudioOutputUnitStart",
    b"_AudioOutputUnitStop",
    b"_AudioUnitAddPropertyListener",
    b"_AudioUnitAddRenderNotify",
    b"_AudioUnitGetParameter",
    b"_AudioUnitGetProperty",
    b"_AudioUnitGetPropertyInfo",
    b"_AudioUnitInitialize",
    b"_AudioUnitRemovePropertyListenerWithUserData",
    b"_AudioUnitRemoveRenderNotify",
    b"_AudioUnitRender",
    b"_AudioUnitReset",
    b"_AudioUnitScheduleParameters",
    b"_AudioUnitSetParameter",
    b"_AudioUnitSetProperty",
    b"_AudioUnitUninitialize",
    b"_MusicDeviceMIDIEvent",
    b"_MusicDevicePrepareInstrument",
    b"_MusicDeviceReleaseInstrument",
    b"_MusicDeviceStartNote",
    b"_MusicDeviceStopNote",
    b"_MusicDeviceSysEx",
})


def read_audiounit_exports(framework_path):
    """
    Parse a Mach-O (the 10.6 SDK's AudioUnit.framework binary) and return
    the set of symbols it exports in its text section. Used when the caller
    wants to override the static fallback list with the authoritative
    export set from a specific SDK.
    """
    with open(framework_path, "rb") as f:
        data = f.read()
    # Walk nlist entries to find globally defined external symbols.
    # For universal binaries, pick the x86_64 slice.
    magic = struct.unpack("<I", data[0:4])[0]
    if magic == 0xCAFEBABE:  # FAT/universal header
        nfat = struct.unpack(">I", data[4:8])[0]
        slice_data = None
        for i in range(nfat):
            off = 8 + i * 20
            cpu, _sub, offset, size, _align = struct.unpack(
                ">IIIII", data[off:off + 20]
            )
            if cpu == 0x01000007:  # CPU_TYPE_X86_64
                slice_data = data[offset:offset + size]
                break
        if slice_data is None:
            raise RuntimeError(f"no x86_64 slice in {framework_path}")
        data = slice_data
    return parse_defined_text_symbols(data, framework_path)


def parse_defined_text_symbols(data, source_label):
    """Return the set of symbols marked T (defined, text) in a Mach-O."""
    magic = struct.unpack("<I", data[0:4])[0]
    is_64 = magic == MH_MAGIC_64
    if magic != MH_MAGIC and magic != MH_MAGIC_64:
        raise RuntimeError(f"{source_label}: not a Mach-O")
    header_size = 32 if is_64 else 28
    ncmds = struct.unpack("<I", data[16:20])[0]
    symtab = None
    off = header_size
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack("<II", data[off:off + 8])
        if cmd == 2:  # LC_SYMTAB
            symoff, nsyms, stroff, strsize = struct.unpack(
                "<IIII", data[off + 8:off + 24]
            )
            symtab = (symoff, nsyms, stroff, strsize)
            break
        off += cmdsize
    if symtab is None:
        raise RuntimeError(f"{source_label}: no LC_SYMTAB")
    symoff, nsyms, stroff, strsize = symtab
    nlist_size = 16 if is_64 else 12
    exports = set()
    for i in range(nsyms):
        noff = symoff + i * nlist_size
        # struct nlist_64 { uint32_t n_strx; uint8_t n_type; uint8_t n_sect;
        #                   uint16_t n_desc; uint64_t n_value; }
        # struct nlist     { uint32_t n_strx; uint8_t n_type; uint8_t n_sect;
        #                   uint16_t n_desc; uint32_t n_value; }
        n_strx, n_type = struct.unpack("<IB", data[noff:noff + 5])
        # External + defined (N_EXT | N_SECT, no N_PEXT, no N_UNDF, no N_ABS).
        # We want globally visible defined symbols: type bits = N_EXT (0x01)
        # and N_TYPE = N_SECT (0x0e). N_STAB bit (0xe0) clear.
        if (n_type & 0x0e) != 0x0e:   # N_TYPE field must be N_SECT
            continue
        if not (n_type & 0x01):       # must be external
            continue
        if n_type & 0xe0:             # skip stab symbols
            continue
        name_end = data.index(b"\x00", stroff + n_strx)
        name = bytes(data[stroff + n_strx:name_end])
        if name.startswith(b"_"):
            exports.add(name)
    return exports


# ---------------------------------------------------------------------------
# ULEB128 / SLEB128 readers
# ---------------------------------------------------------------------------

def read_uleb128(data, off):
    """Decode ULEB128 at offset. Returns (value, new_offset)."""
    result = 0
    shift = 0
    start = off
    while True:
        if off >= len(data):
            raise RuntimeError("ULEB128 ran past end of data")
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            break
        shift += 7
    return result, off, (off - start)  # also return byte length


def read_sleb128(data, off):
    """Decode SLEB128 at offset. Returns (value, new_offset)."""
    result = 0
    shift = 0
    while True:
        b = data[off]
        off += 1
        result |= (b & 0x7F) << shift
        shift += 7
        if (b & 0x80) == 0:
            if b & 0x40:
                result |= -(1 << shift)
            break
    return result, off


# ---------------------------------------------------------------------------
# Mach-O parsing
# ---------------------------------------------------------------------------

def parse_macho_header(data):
    """Return (is_64bit, ncmds, header_size)."""
    if len(data) < 4:
        raise RuntimeError("file too small to be Mach-O")
    magic = struct.unpack("<I", data[0:4])[0]
    if magic == MH_MAGIC_64:
        ncmds, _sizeofcmds = struct.unpack("<II", data[16:24])
        return True, ncmds, 32
    if magic == MH_MAGIC:
        ncmds, _sizeofcmds = struct.unpack("<II", data[16:24])
        return False, ncmds, 28
    raise RuntimeError(f"not a Mach-O magic: 0x{magic:08x}")


def iter_load_commands(data):
    """Yield (cmd, cmdsize, cmd_off, payload_off) for each load command."""
    _is64, ncmds, header_size = parse_macho_header(data)
    off = header_size
    for _ in range(ncmds):
        if off + 8 > len(data):
            raise RuntimeError("load command header truncated")
        cmd, cmdsize = struct.unpack("<II", data[off:off + 8])
        yield cmd, cmdsize, off, off + 8
        off += cmdsize


def map_ordinals_to_paths(data):
    """
    Return {ordinal: framework_path} for every load command that supplies
    a library ordinal (LC_LOAD_DYLIB, LC_LOAD_WEAK_DYLIB, etc.).
    Ordinals are 1-indexed in the order the load commands appear.
    """
    out = {}
    ordinal = 0
    for cmd, _cmdsize, cmd_off, _payload_off in iter_load_commands(data):
        if cmd not in ORDINAL_PROVIDING_LCMDS:
            continue
        ordinal += 1
        # struct dylib_command { uint32_t cmd, cmdsize; struct dylib dylib; }
        # struct dylib { union lc_str { uint32_t offset; } name;
        #                uint32_t timestamp, current_version, compat; }
        name_offset = struct.unpack("<I", data[cmd_off + 8:cmd_off + 12])[0]
        # name_offset is relative to the start of the load command
        name_start = cmd_off + name_offset
        name_end = data.index(b"\x00", name_start)
        path = data[name_start:name_end].decode("utf-8", "replace")
        out[ordinal] = path
    return out


def find_dyld_info_streams(data):
    """
    Return a dict of {stream_name: (offset, size)} for every bind-related
    stream in LC_DYLD_INFO(_ONLY). Stream names: 'bind', 'weak_bind',
    'lazy_bind'. Zero-size streams are omitted.
    """
    streams = {}
    for cmd, _cmdsize, _cmd_off, payload_off in iter_load_commands(data):
        if cmd not in (LC_DYLD_INFO, LC_DYLD_INFO_ONLY):
            continue
        # struct dyld_info_command {
        #   uint32_t cmd, cmdsize;            // 0..7
        #   uint32_t rebase_off, rebase_size; // 8..15
        #   uint32_t bind_off, bind_size;     // 16..23
        #   uint32_t weak_bind_off, weak_bind_size;   // 24..31
        #   uint32_t lazy_bind_off, lazy_bind_size;   // 32..39
        #   uint32_t export_off, export_size;         // 40..47
        # };
        bind_off, bind_size = struct.unpack(
            "<II", data[payload_off + 8:payload_off + 16]
        )
        weak_off, weak_size = struct.unpack(
            "<II", data[payload_off + 16:payload_off + 24]
        )
        lazy_off, lazy_size = struct.unpack(
            "<II", data[payload_off + 24:payload_off + 32]
        )
        if bind_size:
            streams["bind"] = (bind_off, bind_size)
        if weak_size:
            streams["weak_bind"] = (weak_off, weak_size)
        if lazy_size:
            streams["lazy_bind"] = (lazy_off, lazy_size)
    return streams


# ---------------------------------------------------------------------------
# Ordinal resolver
# ---------------------------------------------------------------------------

def find_audio_ordinals(ord_to_path):
    """
    Return (audio_toolbox_ord, audio_unit_ord) by matching path substrings.
    Raises if either framework isn't found.
    """
    at_ord = None
    au_ord = None
    for ord_, path in ord_to_path.items():
        low = path.lower()
        if "/audiotoolbox.framework/" in low:
            at_ord = ord_
        elif "/audiounit.framework/" in low:
            au_ord = ord_
    if at_ord is None:
        raise RuntimeError("AudioToolbox.framework not in LC_LOAD_DYLIB")
    if au_ord is None:
        raise RuntimeError("AudioUnit.framework not in LC_LOAD_DYLIB")
    return at_ord, au_ord


# ---------------------------------------------------------------------------
# Bind-opcode walker
# ---------------------------------------------------------------------------

def scan_lazy_bind(data, stream_off, stream_size, target_symbols, at_ord):
    """
    Walk the lazy_bind stream. For each DO_BIND opcode whose current symbol
    is one of `target_symbols` AND whose current ordinal is `at_ord`, record
    the file offset of the ULEB128 ordinal value (so it can be rewritten).

    If `at_ord` is None, matches any ordinal (used for diagnostic scans).

    Returns a list of dicts: {symbol, ordinal_off, old_ordinal, seg, addr_offset}.
    """
    end = stream_off + stream_size
    off = stream_off

    current_symbol = None
    current_ordinal = None
    current_ordinal_off = None   # file offset of the ULEB128 ordinal bytes
    current_ordinal_len = None   # byte length of the ULEB128 ordinal
    current_seg = None
    current_seg_offset = None

    hits = []

    while off < end:
        op = data[off]
        opcode = op & 0xF0
        imm = op & 0x0F
        off += 1

        if op == BIND_OPCODE_DONE:
            # Each lazy bind is a self-contained segment ended by DONE.
            current_symbol = None
            current_ordinal = None
            current_ordinal_off = None
            current_ordinal_len = None
            current_seg = None
            current_seg_offset = None

        elif opcode == BIND_OPCODE_SET_DYLIB_ORDINAL_IMM:
            # Ordinal encoded in the low nibble (0..15).
            current_ordinal = imm
            current_ordinal_off = None   # immediate, no separate byte to patch
            current_ordinal_len = 0

        elif op == BIND_OPCODE_SET_DYLIB_ORDINAL_ULEB:
            val_off = off
            val, off, n = read_uleb128(data, off)
            current_ordinal = val
            current_ordinal_off = val_off
            current_ordinal_len = n

        elif opcode == BIND_OPCODE_SET_DYLIB_SPECIAL_IMM:
            # Special ordinals (e.g., BIND_SPECIAL_DYLIB_MAIN_EXECUTABLE).
            current_ordinal = None
            current_ordinal_off = None
            current_ordinal_len = 0

        elif opcode == BIND_OPCODE_SET_SYMBOL_TRAILING_FLAGS_IMM:
            name_start = off
            name_end = data.index(b"\x00", off)
            current_symbol = bytes(data[name_start:name_end])
            off = name_end + 1

        elif opcode == BIND_OPCODE_SET_TYPE_IMM:
            pass  # binding type in imm; not relevant for patching

        elif op == BIND_OPCODE_SET_ADDEND_SLEB:
            _v, off = read_sleb128(data, off)

        elif opcode == BIND_OPCODE_SET_SEGMENT_AND_OFFSET_ULEB:
            current_seg = imm
            current_seg_offset, off, _n = read_uleb128(data, off)

        elif op == BIND_OPCODE_ADD_ADDR_ULEB:
            _v, off, _n = read_uleb128(data, off)

        elif op == BIND_OPCODE_DO_BIND:
            if (
                current_symbol in target_symbols
                and current_ordinal_off is not None
                and (at_ord is None or current_ordinal == at_ord)
            ):
                hits.append({
                    "symbol": current_symbol.decode(),
                    "ordinal_off": current_ordinal_off,
                    "ordinal_len": current_ordinal_len,
                    "old_ordinal": current_ordinal,
                    "seg": current_seg,
                    "seg_offset": current_seg_offset,
                })
            # Lazy binds reset per-symbol; rely on DONE to clear state.

        elif op == BIND_OPCODE_DO_BIND_ADD_ADDR_ULEB:
            _v, off, _n = read_uleb128(data, off)
            if (
                current_symbol in target_symbols
                and current_ordinal_off is not None
                and (at_ord is None or current_ordinal == at_ord)
            ):
                hits.append({
                    "symbol": current_symbol.decode(),
                    "ordinal_off": current_ordinal_off,
                    "ordinal_len": current_ordinal_len,
                    "old_ordinal": current_ordinal,
                    "seg": current_seg,
                    "seg_offset": current_seg_offset,
                })

        elif opcode == BIND_OPCODE_DO_BIND_ADD_ADDR_IMM_SCALED:
            if (
                current_symbol in target_symbols
                and current_ordinal_off is not None
                and (at_ord is None or current_ordinal == at_ord)
            ):
                hits.append({
                    "symbol": current_symbol.decode(),
                    "ordinal_off": current_ordinal_off,
                    "ordinal_len": current_ordinal_len,
                    "old_ordinal": current_ordinal,
                    "seg": current_seg,
                    "seg_offset": current_seg_offset,
                })

        elif op == BIND_OPCODE_DO_BIND_ULEB_TIMES_SKIPPING_ULEB:
            _count, off, _n = read_uleb128(data, off)
            _skip, off, _n = read_uleb128(data, off)
            if (
                current_symbol in target_symbols
                and current_ordinal_off is not None
                and (at_ord is None or current_ordinal == at_ord)
            ):
                hits.append({
                    "symbol": current_symbol.decode(),
                    "ordinal_off": current_ordinal_off,
                    "ordinal_len": current_ordinal_len,
                    "old_ordinal": current_ordinal,
                    "seg": current_seg,
                    "seg_offset": current_seg_offset,
                })

        elif opcode == BIND_OPCODE_THREADED:
            # Used by modern bind format (BIND_OPCODE_THREADED).
            # We don't expect this in 10.6-era binaries, but bail loudly.
            raise RuntimeError(
                f"threaded bind opcodes at offset {off-1} not supported"
            )

        else:
            # Unknown opcode — refuse to silently corrupt.
            raise RuntimeError(
                f"unknown bind opcode 0x{op:02x} at offset {off-1}"
            )

    return hits


def encode_uleb128(value):
    """Encode an integer as ULEB128 bytes."""
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def patch(path, audiounit_framework=None, audit=False, verbose=True):
    with open(path, "rb") as f:
        data = bytearray(f.read())

    ord_to_path = map_ordinals_to_paths(data)
    at_ord, au_ord = find_audio_ordinals(ord_to_path)

    # Resolve the authoritative set of AudioUnit.framework exports.
    if audiounit_framework:
        target_symbols = read_audiounit_exports(audiounit_framework)
        if verbose:
            print(f"AudioUnit exports (from {audiounit_framework}): "
                  f"{len(target_symbols)} symbols")
    else:
        target_symbols = set(AUDIOUNIT_EXPORTS_10_6)
        if verbose:
            print(f"AudioUnit exports (built-in 10.6.8 list): "
                  f"{len(target_symbols)} symbols")

    if verbose:
        print(f"AudioToolbox ordinal: {at_ord}  ({ord_to_path[at_ord]})")
        print(f"AudioUnit    ordinal: {au_ord}  ({ord_to_path[au_ord]})")

    streams = find_dyld_info_streams(data)
    if not streams:
        print("no LC_DYLD_INFO(_ONLY) bind streams — nothing to patch")
        return 0
    if verbose:
        for name, (off, size) in sorted(streams.items()):
            print(f"  {name}: offset=0x{off:x} size={size}")

    # Scan every bind stream. AudioToolbox-bound symbols may live in any.
    hits = []
    for name, (off, size) in sorted(streams.items()):
        h = scan_lazy_bind(data, off, size, target_symbols, at_ord)
        for entry in h:
            entry["stream"] = name
        hits.extend(h)

    if not hits:
        # Either no AudioUnit-family symbols are referenced, or they're
        # already bound to AudioUnit. Distinguish for clearer CI output.
        any_target_seen = []
        for name, (off, size) in sorted(streams.items()):
            any_target_seen.extend(
                scan_lazy_bind(data, off, size, target_symbols, at_ord=None)
            )
        if not any_target_seen:
            print("no AudioUnit-family symbols referenced — nothing to do")
        else:
            by_ord = {}
            for h in any_target_seen:
                by_ord.setdefault(h["old_ordinal"], []).append(h["symbol"])
            print("AudioUnit-family symbols already bound to a non-"
                  "AudioToolbox ordinal (likely already patched):")
            for ord_ in sorted(by_ord):
                lib = ord_to_path.get(ord_, "<unknown>")
                print(f"  ordinal {ord_:>3} ({lib})")
                for s in sorted(set(by_ord[ord_])):
                    print(f"      {s}")
        return 0

    # Validate we can safely rewrite each ordinal ULEB128.
    old_enc = encode_uleb128(at_ord)
    new_enc = encode_uleb128(au_ord)

    for h in hits:
        if h["ordinal_len"] != len(old_enc):
            raise RuntimeError(
                f"{h['symbol']}: ordinal ULEB128 is {h['ordinal_len']} bytes, "
                f"expected {len(old_enc)} — bailing rather than risk corruption"
            )

    if verbose:
        for h in hits:
            print(
                f"  [{h['stream']}] {h['symbol']}: "
                f"ordinal {h['old_ordinal']} -> {au_ord} "
                f"@ file off 0x{h['ordinal_off']:x} "
                f"(seg {h['seg']} + 0x{h['seg_offset']:x})"
            )

    if audit:
        print(f"AUDIT: would apply {len(hits)} patches (no file changes)")
        return 0

    if len(old_enc) != len(new_enc):
        raise RuntimeError(
            f"ordinal byte-length differs ({len(old_enc)} -> {len(new_enc)}); "
            f"this would require shifting bytes in __LINKEDIT — not supported"
        )

    # Apply: overwrite the ordinal ULEB128 bytes.
    for h in hits:
        for i, b in enumerate(new_enc):
            data[h["ordinal_off"] + i] = b

    with open(path, "wb") as f:
        f.write(data)

    print(f"applied {len(hits)} patches to {path}")
    return 0


def main(argv):
    p = argparse.ArgumentParser(
        description="Rewrite AudioUnit/AudioComponent bind opcodes from "
                    "AudioToolbox to AudioUnit in a Mach-O binary, so it "
                    "loads on Mac OS X 10.6.",
    )
    p.add_argument("path", help="Path to Mach-O binary to patch (e.g. XUL)")
    p.add_argument(
        "--audiounit-framework",
        metavar="PATH",
        help="Path to a 10.6-era AudioUnit.framework binary to extract "
             "the authoritative export list from. If omitted, uses a "
             "built-in list captured from 10.6.8.",
    )
    p.add_argument(
        "--audit", action="store_true",
        help="Report what would change without modifying the file",
    )
    args = p.parse_args(argv)
    try:
        return patch(
            args.path,
            audiounit_framework=args.audiounit_framework,
            audit=args.audit,
        )
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
