#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# po2lmo.py -- compile a gettext .po catalog into the binary .lmo format that
# LuCI loads at runtime (/usr/lib/lua/luci/i18n/<app>.<locale>.lmo).
#
# This is a dependency-free reimplementation of OpenWrt's po2lmo(1) from
# modules/luci-base/src/po2lmo.c, so packaging does not need a C toolchain or
# Lua headers. It produces byte-identical output to po2lmo for the catalogs
# used by LuCI.
#
# .lmo layout (all integers big-endian):
#   [ string table ]  NUL terminated strings, each padded to a 4-byte boundary
#   [ index ]         one 16-byte entry per message: key_id, val_id, offset, length
#                     sorted by key_id
#   [ u32 ]           byte offset of the index, always the last 4 bytes
#
# key_id/val_id are the Paul Hsieh SuperFastHash (init = length) of the key and
# of the value. Colliding key hashes are dropped, exactly as po2lmo does.
#
# Usage:
#   po2lmo.py input.po output.lmo
#   po2lmo.py --stats input.po output.lmo

import argparse
import os
import struct
import sys

# ---------------------------------------------------------------------------
# Paul Hsieh SuperFastHash (see modules/luci-base/src/lib/lmo.c)
# ---------------------------------------------------------------------------

MASK32 = 0xFFFFFFFF


def sfh_hash(data, init):
    """32-bit SuperFastHash of bytes, seeded with init (po2lmo passes len)."""
    if not data:
        return 0
    rem = len(data) & 3
    n = len(data) >> 2
    pos = 0
    h = init & MASK32

    def get16(off):
        return data[off] | (data[off + 1] << 8)

    for _ in range(n):
        h = (h + get16(pos)) & MASK32
        tmp = ((get16(pos + 2) << 11) ^ h) & MASK32
        h = ((h << 16) ^ tmp) & MASK32
        pos += 4
        h = (h + (h >> 11)) & MASK32

    if rem == 3:
        h = (h + get16(pos)) & MASK32
        h ^= (h << 16) & MASK32
        # data[2] is read as a signed char
        signed = data[pos + 2] - 256 if data[pos + 2] > 127 else data[pos + 2]
        h ^= (signed << 18) & MASK32
        h = (h + (h >> 11)) & MASK32
    elif rem == 2:
        h = (h + get16(pos)) & MASK32
        h ^= (h << 11) & MASK32
        h = (h + (h >> 17)) & MASK32
    elif rem == 1:
        h = (h + data[pos]) & MASK32
        h ^= (h << 10) & MASK32
        h = (h + (h >> 1)) & MASK32

    h ^= (h << 3) & MASK32
    h = (h + (h >> 5)) & MASK32
    h ^= (h << 4) & MASK32
    h = (h + (h >> 17)) & MASK32
    h ^= (h << 25) & MASK32
    h = (h + (h >> 6)) & MASK32
    return h & MASK32


def canon_key(msgid, ctxt=None, plural=None):
    """b"<ctxt>\\1<id>\\2<n>" style canonical key, as po2lmo builds it."""
    out = bytearray()
    if ctxt is not None:
        out += ctxt
        out += b"\1"
    out += msgid
    if plural is not None:
        out += b"\2"
        out += str(plural).encode("ascii")
    return bytes(out)


# ---------------------------------------------------------------------------
# PO parsing (the subset po2lmo understands)
# ---------------------------------------------------------------------------


class PoParseError(Exception):
    pass


def _unescape_po_string(raw):
    """Decode the body of a "..." PO string, including continuation escapes."""
    out = bytearray()
    i = 0
    while i < len(raw):
        ch = raw[i]
        if ch == 0x5C:  # backslash
            i += 1
            if i >= len(raw):
                out.append(0x5C)
                break
            esc = raw[i]
            if esc == ord("n"):
                out.append(0x0A)
            elif esc == ord("t"):
                out.append(0x09)
            elif esc == ord("r"):
                out.append(0x0D)
            elif esc == ord('"'):
                out.append(ord('"'))
            elif esc == 0x5C:
                out.append(0x5C)
            else:
                # po2lmo keeps unknown escapes verbatim (minus the backslash)
                out.append(esc)
            i += 1
        else:
            out.append(ch)
            i += 1
    return bytes(out)


def _parse_quoted(chunk):
    """Extract the content of the first "..." group in chunk, or None."""
    start = chunk.find(b'"')
    if start < 0:
        return None
    end = len(chunk)
    esc = False
    for i in range(start + 1, len(chunk)):
        b = chunk[i]
        if esc:
            esc = False
        elif b == 0x5C:
            esc = True
        elif b == ord('"'):
            end = i
            break
    return chunk[start + 1:end]


def _classify(line):
    """Return (kind, index, payload) for one PO line.

    kind is one of msgctxt, msgid, msgid_plural, msgstr or None; index is the
    plural index for msgstr; payload is the raw (still escaped) string body.
    """
    if line.startswith(b"msgctxt"):
        return "msgctxt", 0, _parse_quoted(line[7:])
    if line.startswith(b"msgid_plural"):
        return "msgid_plural", 0, _parse_quoted(line[12:])
    if line.startswith(b"msgid"):
        return "msgid", 0, _parse_quoted(line[5:])
    if line.startswith(b"msgstr["):
        end = line.index(b"]")
        return "msgstr", int(line[7:end]), _parse_quoted(line[end + 1:])
    if line.startswith(b"msgstr"):
        return "msgstr", 0, _parse_quoted(line[6:])
    stripped = line.lstrip()
    if stripped.startswith(b'"'):
        return "cont", 0, _parse_quoted(stripped)
    return None, 0, None


def parse_po(path):
    """Yield message dicts with keys ctxt, id, id_plural, val (list of bytes)."""
    # PO keyword -> message dict key
    local_field_key = {"msgctxt": "ctxt", "msgid": "id", "msgid_plural": "id_plural"}

    with open(path, "rb") as fh:
        data = fh.read()

    messages = []
    cur = {"ctxt": None, "id": None, "id_plural": None, "val": {}}
    field = None
    plural_index = 0

    for raw_line in data.split(b"\n"):
        line = raw_line.rstrip(b"\r")
        if not line.strip() or line.lstrip().startswith(b"#"):
            continue
        kind, idx, payload = _classify(line)
        if kind is None:
            continue
        if kind == "cont":
            if field is None or payload is None:
                continue
            text = _unescape_po_string(payload)
            if field == "msgstr":
                cur["val"][plural_index] = cur["val"].get(plural_index, b"") + text
            else:
                key = local_field_key[field]
                cur[key] = (cur[key] or b"") + text
            continue
        if kind == "msgstr":
            plural_index = idx
            field = "msgstr"
            cur["val"].setdefault(plural_index, b"")
            if payload:
                cur["val"][plural_index] += _unescape_po_string(payload)
            continue
        # msgctxt/msgid both start a new message; msgid_plural continues one
        if kind in ("msgctxt", "msgid"):
            if cur["id"] is not None or cur["val"]:
                messages.append(cur)
                cur = {"ctxt": None, "id": None, "id_plural": None, "val": {}}
            plural_index = 0
            field = kind
            cur[local_field_key[kind]] = _unescape_po_string(payload) if payload else b""
        elif kind == "msgid_plural":
            field = kind
            cur[local_field_key[kind]] = _unescape_po_string(payload) if payload else b""
    if cur["id"] is not None or cur["val"]:
        messages.append(cur)
    return messages


# ---------------------------------------------------------------------------
# LMO writing
# ---------------------------------------------------------------------------


def compile_lmo(po_path):
    """Return the .lmo byte string for a .po file."""
    entries = []  # (key_id, val_id, offset, length)
    table = bytearray()

    def add(key_id, val_id, value):
        offset = len(table)
        table.extend(value)
        table.append(0)
        while len(table) % 4:
            table.append(0)
        entries.append((key_id, val_id, offset, len(value)))

    for msg in parse_po(po_path):
        msgid = msg["id"]
        vals = msg["val"]
        if msgid is None and not vals:
            continue
        if msgid is not None and vals.get(0) is not None:
            # translated message
            for idx in sorted(vals):
                value = vals[idx]
                if not value:
                    continue
                id_plural = msg["id_plural"]
                key = canon_key(
                    msgid,
                    ctxt=msg["ctxt"],
                    plural=idx if id_plural is not None else None,
                )
                key_id = sfh_hash(key, len(key))
                val_id = sfh_hash(value, len(value))
                if key_id != val_id:
                    add(key_id, val_id, value)
        elif vals.get(0):
            # header: pull out the Plural-Forms value only
            for field in vals[0].split(b"\n"):
                if field.lower().startswith(b"plural-forms: "):
                    add(0, 0, field[14:])
                    break

    entries.sort(key=lambda e: e[0])
    out = bytearray(table)
    for key_id, val_id, offset, length in entries:
        out.extend(struct.pack(">IIII", key_id, val_id, offset, length))
    out.extend(struct.pack(">I", len(table)))
    return bytes(out)


# ---------------------------------------------------------------------------
# LMO reading (for verification / debugging)
# ---------------------------------------------------------------------------


def read_lmo(blob):
    """Parse .lmo bytes into a list of (key_id, val_id, offset, length).

    The reader mirrors lmo_open(): the index starts at the offset stored in the
    final u32 and holds (size - offset - 4) / 16 entries.
    """
    if len(blob) < 4:
        raise ValueError("lmo too short")
    idx = struct.unpack(">I", blob[-4:])[0]
    if idx >= len(blob) or idx % 4:
        raise ValueError("bad lmo index offset %d" % idx)
    count = (len(blob) - idx - 4) // 16
    entries = []
    for i in range(count):
        key_id, val_id, offset, length = struct.unpack(
            ">IIII", blob[idx + i * 16: idx + i * 16 + 16]
        )
        entries.append((key_id, val_id, offset, length))
    return entries


def lmo_stats(blob):
    entries = read_lmo(blob)
    return len(entries), {e[2]: blob[e[2]:e[2] + e[3]] for e in entries}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Compile a gettext .po catalog into LuCI .lmo format."
    )
    parser.add_argument("po", help="input .po file")
    parser.add_argument("lmo", help="output .lmo file")
    parser.add_argument("--stats", action="store_true", help="print entry count")
    args = parser.parse_args(argv)

    blob = compile_lmo(args.po)
    with open(args.lmo, "wb") as fh:
        fh.write(blob)

    if args.stats:
        count, _ = lmo_stats(blob)
        print(
            "%s -> %s (%d entries, %d bytes)"
            % (os.path.basename(args.po), os.path.basename(args.lmo), count, len(blob))
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
