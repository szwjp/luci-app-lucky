#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
#
# apk-verify.py -- verify that a built .apk really is Alpine apk (ADB) v3.
#
# OpenWrt 25.12+ builds .apk instead of .ipk (CONFIG_USE_APK=y) using
# "apk mkpkg", which emits the binary ADB v3 container. This tool parses that
# container directly, so it can be used as a build-time gate:
#
#   scripts/apk-verify.py --arch noarch --require-file /usr/bin/lucky lucky-2.27.2-r1.apk
#
# apk v2 packages (three concatenated gzip streams) are detected and reported
# as failures, because v2 has no dedicated architecture field semantics for
# "noarch" as a container-level constant and no ADB metadata to inspect.
#
# Requires only the Python 3 standard library (zlib, hashlib, lzma).
# Zstandard-compressed v3 packages need the optional "zstandard" module.

import argparse
import hashlib
import json
import os
import struct
import sys
import zlib

# ---------------------------------------------------------------------------
# adb format constants (from apk-tools src/adb.h, src/apk_adb.h)
# ---------------------------------------------------------------------------

ADB_FORMAT_MAGIC = b"ADB."
ADB_SCHEMA_PACKAGE = 0x676B6370  # "pckg"
ADB_SCHEMA_INDEX = 0x78646E69  # "indx"
ADB_SCHEMA_IMPLIED = 0x80000000

TYPE_SPECIAL = 0x00000000
TYPE_INT = 0x10000000
TYPE_INT_32 = 0x20000000
TYPE_INT_64 = 0x30000000
TYPE_BLOB_8 = 0x80000000
TYPE_BLOB_16 = 0x90000000
TYPE_BLOB_32 = 0xA0000000
TYPE_ARRAY = 0xD0000000
TYPE_OBJECT = 0xE0000000
TYPE_ERROR = 0xF0000000
TYPE_MASK = 0xF0000000
VALUE_MASK = 0x0FFFFFFF
NULL_VAL = 0

BLOCK_ADB = 0
BLOCK_SIG = 1
BLOCK_DATA = 2

ADBI_PKG_PKGINFO = 1
ADBI_PKG_PATHS = 2
ADBI_PKG_SCRIPTS = 3
ADBI_PKG_TRIGGERS = 4

# package info slots, schema order == slot id
PKGINFO_FIELDS = {
    1: "name",
    2: "version",
    3: "hashes",
    4: "description",
    5: "arch",
    6: "license",
    7: "origin",
    8: "maintainer",
    9: "url",
    10: "repo_commit",
    11: "build_time",
    12: "installed_size",
    13: "file_size",
    14: "provider_priority",
    15: "depends",
    16: "provides",
    17: "replaces",
    18: "install_if",
    19: "recommends",
    20: "layer",
    21: "tags",
}
PKGINFO_STRINGS = {
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 21,
}
PKGINFO_INTS = {11, 12, 13, 14, 20}
PKGINFO_DEPS = {15, 16, 17, 18, 19}

DEP_FIELDS = {1: "name", 2: "version", 3: "match"}

SCRIPTS_FIELDS = {
    1: "trigger",
    2: "pre-install",
    3: "post-install",
    4: "pre-deinstall",
    5: "post-deinstall",
    6: "pre-upgrade",
    7: "post-upgrade",
}

# ADBI_FI_TARGET carries a leading 16-bit st_mode type tag
S_IFMT = 0o170000
S_IFIFO = 0o010000
S_IFCHR = 0o020000
S_IFDIR = 0o040000
S_IFBLK = 0o060000
S_IFREG = 0o100000
S_IFLNK = 0o120000
S_IFSOCK = 0o140000

TARGET_KIND = {
    S_IFIFO: "fifo",
    S_IFCHR: "char",
    S_IFDIR: "dir",
    S_IFBLK: "block",
    S_IFREG: "hardlink",
    S_IFLNK: "symlink",
    S_IFSOCK: "socket",
}

COMPRESSION = {
    0: "none",
    1: "deflate",
    2: "zstd",
}


class ApkError(Exception):
    """Raised when the file is not a well-formed apk v3 (ADB) container."""


class AdbReader:
    """Random-access reader over an ADB blob.

    Value semantics follow apk-tools src/adb.c:

    * INT/INT_32/INT_64 hold the number either inline (INT) or at
      payload + (value & 0x0fffffff).
    * BLOB_8/16/32 store the length at that offset and the bytes right after.
    * ARRAY/OBJECT store a u32 element count including the count itself, then
      that many u32 values.
    * Every offset is relative to the start of the enclosing object payload,
      which begins with the 8-byte adb_hdr.
    """

    def __init__(self, buf):
        self.buf = buf

    # -- primitive value accessors -----------------------------------------

    def _at(self, off, size):
        if off < 0 or off + size > len(self.buf):
            raise ApkError("offset 0x%x out of range" % off)
        return self.buf[off:off + size]

    @staticmethod
    def _type(val):
        return val & TYPE_MASK

    @staticmethod
    def _value(val):
        return val & VALUE_MASK

    def int(self, val):
        t = self._type(val)
        if t == TYPE_INT:
            return self._value(val)
        if t == TYPE_INT_32:
            return struct.unpack("<I", self._at(self._value(val), 4))[0]
        if t == TYPE_INT_64:
            return struct.unpack("<Q", self._at(self._value(val), 8))[0]
        raise ApkError("value 0x%08x is not an integer" % val)

    def blob(self, val):
        t = self._type(val)
        if t == TYPE_BLOB_8:
            off = self._value(val)
            (n,) = struct.unpack("<B", self._at(off, 1))
            return self._at(off + 1, n)
        if t == TYPE_BLOB_16:
            off = self._value(val)
            (n,) = struct.unpack("<H", self._at(off, 2))
            return self._at(off + 2, n)
        if t == TYPE_BLOB_32:
            off = self._value(val)
            (n,) = struct.unpack("<I", self._at(off, 4))
            return self._at(off + 4, n)
        raise ApkError("value 0x%08x is not a blob" % val)

    def text(self, val):
        if self._type(val) == TYPE_SPECIAL and self._value(val) == NULL_VAL:
            return None
        return self.blob(val).decode("utf-8", "replace")

    def array(self, val):
        """Return the list of element values of an ARRAY/OBJECT."""
        t = self._type(val)
        if t not in (TYPE_ARRAY, TYPE_OBJECT):
            raise ApkError("value 0x%08x is not an array/object" % val)
        off = self._value(val)
        (count,) = struct.unpack("<I", self._at(off, 4))
        if count == 0:
            return []
        raw = self._at(off, 4 * count)
        vals = struct.unpack("<%dI" % count, raw)
        return list(vals[1:])

    def digest(self, val):
        """Read a scalar_hexblob field (raw digest bytes) as lowercase hex."""
        if self._type(val) == TYPE_SPECIAL and self._value(val) == NULL_VAL:
            return None
        return self.blob(val).hex()

    def obj(self, val, schema_fields):
        """Decode an OBJECT into {field_name: raw_value} by 1-based slot id."""
        out = {}
        for slot, v in enumerate(self.array(val), start=1):
            if v == NULL_VAL or slot not in schema_fields:
                continue
            out[schema_fields[slot]] = v
        return out


def decompress_body(raw):
    """Strip the 4-byte ADB file header and return (body, compression_name).

    The header only tags the compression of what follows; the decompressed
    stream is a complete standalone ADB container that starts with "ADB."
    again.
    """
    if raw[:3] != b"ADB":
        raise ApkError("bad magic %r, not an APK v3 (ADB) file" % raw[:4])
    tag = raw[3:4]
    if tag == b".":
        return raw, "none"
    if tag == b"d":
        return zlib.decompress(raw[4:], -zlib.MAX_WBITS), "deflate"
    if tag == b"c":
        if len(raw) < 6:
            raise ApkError("truncated ADB compression header")
        method, _level = raw[4], raw[5]
        rest = raw[6:]
        if method == 0:
            body = rest
        elif method == 1:
            body = zlib.decompress(rest, -zlib.MAX_WBITS)
        elif method == 2:
            try:
                import zstandard  # type: ignore
            except ImportError as exc:  # pragma: no cover
                raise ApkError(
                    "package uses zstd compression; install the 'zstandard' "
                    "python module to verify it"
                ) from exc
            body = zstandard.ZstdDecompressor().decompressobj().decompress(rest)
        else:
            raise ApkError("unknown ADB compression method %d" % method)
        return body, COMPRESSION.get(method, str(method))
    raise ApkError("unknown ADB compression tag %r" % tag)


def iter_blocks(body):
    """Yield (type, payload) for each 8-byte-aligned ADB block."""
    off = 8  # "ADB.pckg" / "ADB.indx"
    while off < len(body):
        if off + 4 > len(body):
            raise ApkError("truncated block header at 0x%x" % off)
        (type_size,) = struct.unpack("<I", body[off:off + 4])
        if type_size >> 30 == 3:
            if off + 16 > len(body):
                raise ApkError("truncated extended block header at 0x%x" % off)
            btype = type_size & 0x3FFFFFFF
            (size,) = struct.unpack("<Q", body[off + 8:off + 16])
            hdr = 16
        else:
            btype = type_size >> 30
            size = type_size & 0x3FFFFFFF
            hdr = 4
        if size < hdr or off + size > len(body):
            raise ApkError("block at 0x%x has invalid size %d" % (off, size))
        payload_start = off + hdr
        payload_end = off + size
        yield btype, body[payload_start:payload_end]
        # blocks are padded to the next 8-byte boundary
        off += (size + 7) & ~7


def decode_dep(reader, val):
    dep = reader.obj(val, DEP_FIELDS)
    item = {}
    if "name" in dep:
        item["name"] = reader.text(dep["name"])
    if "version" in dep:
        item["version"] = reader.text(dep["version"])
    if "match" in dep:
        item["match"] = reader.int(dep["match"])
    return item


def decode_pkginfo(reader, val):
    info = {}
    for slot, raw in enumerate(reader.array(val), start=1):
        if raw == NULL_VAL or slot not in PKGINFO_FIELDS:
            continue
        name = PKGINFO_FIELDS[slot]
        if slot == 3:
            info[name] = reader.digest(raw)
        elif slot in PKGINFO_STRINGS:
            info[name] = reader.text(raw)
        elif slot in PKGINFO_INTS:
            info[name] = reader.int(raw)
        elif slot in PKGINFO_DEPS:
            info[name] = [decode_dep(reader, v) for v in reader.array(raw)]
        else:
            info[name] = None
    return info


def decode_target(reader, raw):
    blob = reader.blob(raw)
    if len(blob) < 2:
        raise ApkError("file target blob is too short")
    (mode,) = struct.unpack("<H", blob[:2])
    kind = TARGET_KIND.get(mode & S_IFMT, "unknown(%o)" % (mode & S_IFMT))
    return mode, kind, blob[2:]


def decode_paths(reader, val):
    """Return (entries, data_blocks_expected) for ADBI_PKG_PATHS.

    Each entry is a dict with dir name, ACL and the list of files.
    """
    entries = []
    for path_val in reader.array(val):
        if path_val == NULL_VAL:
            continue
        path = reader.obj(path_val, {1: "name", 2: "acl", 3: "files"})
        entry = {
            "dir": reader.text(path["name"]) if "name" in path else "",
            "acl": decode_acl(reader, path["acl"]) if "acl" in path else None,
            "files": [],
        }
        if "files" in path:
            for file_val in reader.array(path["files"]):
                if file_val == NULL_VAL:
                    continue
                f = reader.obj(
                    file_val,
                    {
                        1: "name",
                        2: "acl",
                        3: "size",
                        4: "mtime",
                        5: "hashes",
                        6: "target",
                    },
                )
                finfo = {
                    "name": reader.text(f["name"]) if "name" in f else None,
                    "acl": decode_acl(reader, f["acl"]) if "acl" in f else None,
                    "size": reader.int(f["size"]) if "size" in f else 0,
                    "mtime": reader.int(f["mtime"]) if "mtime" in f else 0,
                    "sha256": reader.digest(f["hashes"]) if "hashes" in f else None,
                    "type": "file",
                    "target": None,
                }
                if "target" in f:
                    mode, kind, tgt = decode_target(reader, f["target"])
                    finfo["type"] = kind
                    finfo["mode"] = mode
                    finfo["target"] = tgt if kind == "symlink" else tgt.hex()
                entry["files"].append(finfo)
        entries.append(entry)
    return entries


def decode_acl(reader, val):
    acl = reader.obj(val, {1: "mode", 2: "user", 3: "group", 4: "xattrs"})
    out = {}
    if "mode" in acl:
        out["mode"] = oct(reader.int(acl["mode"]))
    if "user" in acl:
        out["user"] = reader.text(acl["user"])
    if "group" in acl:
        out["group"] = reader.text(acl["group"])
    if "xattrs" in acl:
        out["xattrs"] = [reader.blob(v).hex() for v in reader.array(acl["xattrs"])]
    return out


def parse_package(path):
    """Parse an apk v3 package file into a dict."""
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:3] != b"ADB":
        raise ApkError(
            "%s is not an APK v3 (ADB) container; first bytes %r. "
            "A v2 apk starts with gzip magic 1f 8b, an ipk starts with '!<arch>'."
            % (os.path.basename(path), raw[:4])
        )

    deflated, compression = decompress_body(raw)
    if deflated[:8] != b"ADB." + struct.pack("<I", ADB_SCHEMA_PACKAGE):
        schema = struct.unpack("<I", deflated[4:8])[0]
        if schema == ADB_SCHEMA_INDEX:
            raise ApkError("%s is an ADB *index*, not a package" % path)
        raise ApkError("unexpected ADB schema 0x%08x" % schema)

    body = deflated
    result = {
        "path": path,
        "size_bytes": len(raw),
        "compression": compression,
        "container": "adb-v3",
        "blocks": [],
        "pkginfo": {},
        "scripts": {},
        "dirs": [],
        "files": [],
        "data_blocks": 0,
    }

    for btype, payload in iter_blocks(body):
        result["blocks"].append({"type": btype, "size": len(payload)})
        if btype != BLOCK_ADB:
            if btype == BLOCK_DATA:
                result["data_blocks"] += 1
            continue
        # ADB block: 8-byte adb_hdr, then everything is relative to payload+8
        if len(payload) < 8:
            raise ApkError("ADB block payload too small")
        compat_ver, adb_ver, _reserved, root = struct.unpack("<BBHI", payload[:8])
        result["adb_compat_ver"] = compat_ver
        result["adb_ver"] = adb_ver
        reader = AdbReader(payload)
        obj = reader.obj(root, {1: "pkginfo", 2: "paths", 3: "scripts", 4: "triggers"})
        if "pkginfo" in obj:
            result["pkginfo"] = decode_pkginfo(reader, obj["pkginfo"])
        if "paths" in obj:
            result["dirs"] = decode_paths(reader, obj["paths"])
        if "scripts" in obj:
            scripts = reader.obj(obj["scripts"], SCRIPTS_FIELDS)
            for sname, raw_script in scripts.items():
                result["scripts"][sname] = reader.blob(raw_script).decode(
                    "utf-8", "replace"
                )
        if "triggers" in obj:
            result["triggers"] = [
                reader.text(v) for v in reader.array(obj["triggers"])
            ]

    files = []
    for d in result["dirs"]:
        base = d["dir"].strip("/")
        for f in d["files"]:
            full = "/" + "/".join(p for p in (base, f["name"]) if p)
            f = dict(f)
            f["path"] = full
            files.append(f)
    result["files"] = files
    return result


def verify(path, want_arch=None, want_name=None, want_version=None,
           require_files=(), extract_dir=None, check_owner=True,
           allow_arch_all=False):
    """Verify one package; returns (ok, messages, info)."""
    problems = []
    notes = []
    info = parse_package(path)

    if info["adb_compat_ver"] != 0 or info["adb_ver"] != 0:
        problems.append(
            "ADB header version is %d/%d, expected 0/0"
            % (info["adb_compat_ver"], info["adb_ver"])
        )

    pkginfo = info["pkginfo"]
    arch = pkginfo.get("arch")
    if not arch:
        problems.append("pkginfo has no arch field")
    elif want_arch and arch != want_arch:
        problems.append("arch is %r, expected %r" % (arch, want_arch))
    if arch == "all" and not allow_arch_all:
        problems.append(
            "arch is 'all'; apk v3 requires 'noarch' for arch-independent packages"
        )

    name = pkginfo.get("name")
    if not name:
        problems.append("pkginfo has no name field")
    elif want_name and name != want_name:
        problems.append("name is %r, expected %r" % (name, want_name))

    version = pkginfo.get("version")
    if not version:
        problems.append("pkginfo has no version field")
    elif want_version and version != want_version:
        problems.append("version is %r, expected %r" % (version, want_version))

    hashes = pkginfo.get("hashes")
    if not hashes:
        problems.append("pkginfo has no package digest (hashes) field")
    else:
        raw_digest = bytes.fromhex(hashes)
        if len(raw_digest) not in (20, 32, 48, 64):
            problems.append(
                "package digest is %d bytes, not a SHA-1/256/384/512 digest"
                % len(raw_digest)
            )

    for d in info["dirs"]:
        acl = d.get("acl") or {}
        if check_owner and acl and acl.get("user") not in (None, "root"):
            problems.append(
                "directory %r owned by %r, expected root" % (d["dir"], acl["user"])
            )

    for f in info["files"]:
        if f["type"] == "file" and f["size"] > 0:
            if not f["sha256"] or len(f["sha256"]) != 64:
                problems.append(
                    "file %s has no SHA-256 digest in the file metadata" % f["path"]
                )
        acl = f.get("acl") or {}
        if check_owner and acl and acl.get("user") not in (None, "root"):
            problems.append(
                "file %s owned by %r, expected root" % (f["path"], acl["user"])
            )

    existing = {f["path"] for f in info["files"]}
    for wanted in require_files:
        if wanted not in existing:
            problems.append("required path %s is absent from the package" % wanted)

    if info["data_blocks"] and not existing:
        problems.append("package carries data blocks but no file metadata")

    if extract_dir:
        ok, msg = extract(path, extract_dir, info)
        if not ok:
            problems.append(msg)
        else:
            notes.append(msg)

    return not problems, problems, notes, info


def extract(path, out_dir, info):
    """Write every regular file out and check size + sha256 against metadata."""
    body = decompress_body(open(path, "rb").read())[0]
    blobs = {}
    for btype, payload in iter_blocks(body):
        if btype != BLOCK_DATA:
            continue
        if len(payload) < 8:
            raise ApkError("data block too small")
        path_idx, file_idx = struct.unpack("<II", payload[:8])
        blobs[(path_idx, file_idx)] = payload[8:]

    # Data blocks address (path index, file index), both 1-based, so rebuild
    # the same addressing that parse_package() walks.
    by_index = {}
    for pi, d in enumerate(info["dirs"], start=1):
        base = d["dir"].strip("/")
        for fi, f in enumerate(d["files"], start=1):
            by_index[(pi, fi)] = (base, f)

    ok = True
    written = 0
    for key, data in blobs.items():
        if key not in by_index:
            return False, "data block %r has no metadata" % (key,)
        base, finfo = by_index[key]
        rel = "/".join(p for p in (base, finfo["name"]) if p)
        target = os.path.join(out_dir, rel)
        os.makedirs(os.path.dirname(target) or out_dir, exist_ok=True)
        with open(target, "wb") as fh:
            fh.write(data)
        written += 1
        if len(data) != finfo["size"]:
            ok = False
            print(
                "  size mismatch for /%s: %d != %d"
                % (rel, len(data), finfo["size"]),
                file=sys.stderr,
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != finfo["sha256"]:
            ok = False
            print(
                "  sha256 mismatch for /%s: %s != %s"
                % (rel, digest, finfo["sha256"]),
                file=sys.stderr,
            )
    return ok, "extracted %d file(s) to %s" % (written, out_dir)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Verify that .apk files are apk-tools v3 (ADB) packages."
    )
    parser.add_argument("packages", nargs="+", help=".apk file(s) to verify")
    parser.add_argument("--arch", help="expected arch field (use noarch for arch-independent)")
    parser.add_argument("--name", help="expected package name")
    parser.add_argument("--version", help="expected package version")
    parser.add_argument(
        "--require-file",
        action="append",
        default=[],
        metavar="PATH",
        help="absolute path that must exist inside the package (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="print parsed metadata as JSON")
    parser.add_argument(
        "--list", action="store_true", help="print the file list instead of a summary"
    )
    parser.add_argument("--extract", metavar="DIR", help="extract payload and re-check hashes")
    parser.add_argument(
        "--allow-arch-all",
        action="store_true",
        help="accept the legacy 'all' arch value (only for ipk->apk comparisons)",
    )
    parser.add_argument(
        "--allow-non-root-owner",
        action="store_true",
        help="do not fail when files are not owned by root (use for builds "
        "made without fakeroot, e.g. on macOS)",
    )
    args = parser.parse_args(argv)

    failures = 0
    for path in args.packages:
        if not os.path.isfile(path):
            print("FAIL %s: no such file" % path, file=sys.stderr)
            failures += 1
            continue
        try:
            ok, problems, notes, info = verify(
                path,
                want_arch=args.arch,
                want_name=args.name,
                want_version=args.version,
                require_files=args.require_file,
                extract_dir=args.extract,
                check_owner=not args.allow_non_root_owner,
                allow_arch_all=args.allow_arch_all,
            )
        except ApkError as exc:
            print("FAIL %s: %s" % (path, exc), file=sys.stderr)
            failures += 1
            continue
        except (OSError, zlib.error, struct.error) as exc:
            print("FAIL %s: malformed package: %s" % (path, exc), file=sys.stderr)
            failures += 1
            continue

        if args.json:
            print(json.dumps(info, indent=2, sort_keys=True))
        elif args.list:
            for f in info["files"]:
                extra = ""
                if f["type"] != "file":
                    extra = " -> %s (%s)" % (f["target"], f["type"])
                print("%s%s" % (f["path"], extra))
        else:
            pkg = info["pkginfo"]
            print(
                "%s: %s %s arch=%s files=%d compression=%s"
                % (
                    "OK  " if ok else "FAIL",
                    pkg.get("name"),
                    pkg.get("version"),
                    pkg.get("arch"),
                    len(info["files"]),
                    info["compression"],
                )
            )
            for note in notes:
                print("     %s" % note)

        for problem in problems:
            print("     - %s" % problem, file=sys.stderr)
        if not ok:
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
