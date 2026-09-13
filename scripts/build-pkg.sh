#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
#
# Lightweight packaging for luci-app-lucky + lucky, without the OpenWrt
# buildroot or SDK. Produces BOTH apk (apk-tools v3, ADB format) and ipk
# (opkg) from the same staged file tree.
#
# Requires on the build host:
#   apk        - apk-tools >= 3.0 ("apk mkpkg"), see .github/workflows/build.yml
#   ipkg-build - https://raw.githubusercontent.com/openwrt/openwrt/master/scripts/ipkg-build
#   python3    - runs scripts/po2lmo.py for the luci-i18n-* packages
#   fakeroot   - optional but recommended; apk mkpkg otherwise records the
#                builder's uid/gid instead of root:root
#   wget/curl, tar, sha256sum, find, sed, awk
#
# Packages produced per run:
#   lucky                  core binary, arch specific
#   luci-app-lucky         LuCI shell, noarch
#   luci-i18n-lucky-<loc>  one per po/<lang>/lucky.po found, noarch
#
# Usage:
#   scripts/build-pkg.sh --arch arm64 --pkg both --out dist
#   scripts/build-pkg.sh --arch mipsle_softfloat --pkg apk --prefix mipsel
#
# The version is taken from the Makefiles as <PKG_VERSION>-r<PKG_RELEASE>, the
# apk-native form, e.g. lucky-2.27.2-r1.apk. The file name can additionally be
# tagged with --prefix so CI can build one directory per architecture.
#
# Translation catalogs are compiled with the bundled scripts/po2lmo.py, a
# dependency-free reimplementation of OpenWrt's po2lmo(1); no Lua headers or C
# toolchain are needed on the build host.

set -euo pipefail

PKG_ARCH="arm64"
PKG_MANAGER="both"
OUT_DIR=""
FILE_PREFIX=""
APK_ARCH_NAME=""
SKIP_DOWNLOAD=0

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LUCKY_DIR="$REPO_DIR/lucky"
LUCI_DIR="$REPO_DIR/luci-app-lucky"

# Upstream lucky release assets are named by these identifiers; this is the
# same mapping lucky/Makefile applies to OpenWrt's $(ARCH).
LUCKY_ARCHES="i386 x86_64 armv5 armv6 armv7 arm64 mips_softfloat mipsle_softfloat"

usage() {
	cat <<EOF
Usage: $(basename "$0") [options]

  --arch ARCH        lucky architecture to package: $LUCKY_ARCHES
                     (default: $PKG_ARCH)
  --arch-name NAME   override the OpenWrt package architecture recorded in the
                     core package metadata (default: derived from --arch)
  --pkg MANAGER      both | apk | ipk   (default: $PKG_MANAGER)
  --out DIR          output directory (default: <repo>/dist)
  --prefix STR       prepend STR to artifact file names (default: none)
  --skip-download    reuse an existing lucky/lucky binary instead of downloading
  -h, --help         show this help
EOF
}

while [ $# -gt 0 ]; do
	case "$1" in
		--arch) PKG_ARCH="$2"; shift 2 ;;
		--arch-name) APK_ARCH_NAME="$2"; shift 2 ;;
		--pkg) PKG_MANAGER="$2"; shift 2 ;;
		--out) OUT_DIR="$2"; shift 2 ;;
		--prefix) FILE_PREFIX="$2"; shift 2 ;;
		--skip-download) SKIP_DOWNLOAD=1; shift ;;
		-h|--help) usage; exit 0 ;;
		*) echo "error: unknown argument '$1'" >&2; usage >&2; exit 1 ;;
	esac
done

if ! printf '%s\n' $LUCKY_ARCHES | grep -qx "$PKG_ARCH"; then
	echo "error: unsupported --arch '$PKG_ARCH'" >&2
	usage >&2
	exit 1
fi
case "$PKG_MANAGER" in
	both|apk|ipk) ;;
	*) echo "error: --pkg must be both, apk or ipk" >&2; exit 1 ;;
esac

OUT_DIR="${OUT_DIR:-$REPO_DIR/dist}"
mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# download URL DEST - wget when present, curl otherwise (macOS has no wget)
download() {
	if command -v wget >/dev/null; then
		wget -q -O "$2" "$1"
	else
		curl -fsSL -o "$2" "$1"
	fi
}

# apk mkpkg records uid/gid/mtime from the staged files; run it under fakeroot
# when available so the payload is always root:root regardless of the builder
# account. Plain apk mkpkg is used when fakeroot is absent.
maybe_fakeroot() {
	if command -v fakeroot >/dev/null; then
		fakeroot "$@"
	else
		"$@"
	fi
}

# sha256 FILE - coreutils or BSD shasum
sha256() {
	if command -v sha256sum >/dev/null; then
		sha256sum "$1" | awk '{print $1}'
	else
		shasum -a 256 "$1" | awk '{print $1}'
	fi
}

# get_mk_value VAR Makefile -> value of "VAR:=..." (first match).
# The pattern must match the whole line start, otherwise PKG_VERSION would also
# match the "VERSION:=" substring of a longer variable name.
get_mk_value() {
	awk -v key="$1" '
		index($0, key ":=") == 1 {
			line = substr($0, length(key) + 3)
			sub(/[[:space:]]*#.*$/, "", line)
			print line
			exit
		}
	' "$2" | xargs
}

# get_mk_multiline_value VAR Makefile -> "\\"-continued value, "+" prefixes stripped.
# Portable (no GNU-only sed/:a;N;$!ba;s/\n/ /g).
get_mk_multiline_value() {
	awk -v key="$1" '
		$0 ~ "^"key"[[:space:]]*:?=" {
			sub("^"key"[[:space:]]*:?=[[:space:]]*", "")
			sub(/[[:space:]]*#.*$/, "")
			printf "%s", $0
			while (sub(/\\[[:space:]]*$/, "")) {
				if ((getline line) <= 0) break
				sub(/^[[:space:]]+/, "", line)
				sub(/[[:space:]]*#.*$/, "", line)
				printf "%s", line
			}
			print ""
			exit
		}
	' "$2" | tr -s ' \t' ' ' | sed -E 's/(^| )\+/\1/g' | xargs || true
}

# get_conffiles PKGNAME Makefile -> newline separated conffile list
get_conffiles() {
	awk -v pkg="$1" '
		$0 ~ "^define Package/"pkg"/conffiles" { flag=1; next }
		flag && /^endef/ { flag=0; next }
		flag && NF { print }
	' "$2"
}

# stage an apk bookkeeping file list: /lib/apk/packages/<name>.list
apk_write_filelist() {
	local stage="$1" name="$2"
	mkdir -p "$stage/lib/apk/packages"
	( cd "$stage" && find . \( -type f -o -type l \) -print | sed 's|^\.|/|' | LC_ALL=C sort ) \
		> "$stage/lib/apk/packages/$name.list"
}

# write <name>.conffiles and <name>.conffiles_static (mode + sha256 per entry)
apk_write_conffiles() {
	local stage="$1" name="$2"
	shift 2
	local files=("$@")
	local dir="$stage/lib/apk/packages"
	[ "${#files[@]}" -gt 0 ] || return 0
	mkdir -p "$dir"
	printf '%s\n' "${files[@]}" > "$dir/$name.conffiles"
	local f
	for f in "${files[@]}"; do
		[ -f "$stage$f" ] || continue
		printf '%s %s\n' "$f" "$(sha256 "$stage$f")" \
			>> "$dir/$name.conffiles_static"
	done
}

# ipk CONTROL/control
ipk_write_control() {
	local stage="$1" name="$2" version="$3" depends="$4" desc="$5" arch="${6:-$IPK_ARCH}"
	mkdir -p "$stage/CONTROL"
	{
		echo "Package: $name"
		echo "Version: $version"
		[ -n "$depends" ] && echo "Depends: $depends"
		echo "Source: $name"
		echo "SourceName: $name"
		echo "Section: net"
		echo "SourceDateEpoch: ${PKG_SOURCE_DATE_EPOCH:-0}"
		echo "Maintainer: ${MAINTAINER:-szwjp <szwjp@users.noreply.github.com>}"
		echo "Architecture: $arch"
		echo "Installed-Size: 0"
		echo "Description: $desc"
	} > "$stage/CONTROL/control"
	chmod 0644 "$stage/CONTROL/control"
}

# ipk maintainer scripts, mirroring include/package-pack.mk.
# $3 is the package architecture recorded in CONTROL/control: the core package
# is arch specific, the luci shell package is "all".
ipk_write_scripts() {
	local stage="$1" name="$2"
	mkdir -p "$stage/CONTROL"
	cat > "$stage/CONTROL/postinst" <<-'EOF'
		#!/bin/sh
		[ "${IPKG_NO_SCRIPT}" = "1" ] && exit 0
		[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0
		. ${IPKG_INSTROOT}/lib/functions.sh
		default_postinst $0 $@
	EOF
	cat > "$stage/CONTROL/prerm" <<-'EOF'
		#!/bin/sh
		[ -s ${IPKG_INSTROOT}/lib/functions.sh ] || exit 0
		. ${IPKG_INSTROOT}/lib/functions.sh
		default_prerm $0 $@
	EOF
	cat > "$stage/CONTROL/postinst-pkg" <<-EOF
		[ -n "\${IPKG_INSTROOT}" ] || {
			export pkgname="$name"
			[ -f /etc/uci-defaults/66_luci-lucky ] && . /etc/uci-defaults/66_luci-lucky && rm -f /etc/uci-defaults/66_luci-lucky
			rm -f /tmp/luci-indexcache
			rm -rf /tmp/luci-modulecache/
			killall -HUP rpcd 2>/dev/null
			exit 0
		}
	EOF
	chmod 0755 "$stage/CONTROL/postinst" "$stage/CONTROL/prerm" "$stage/CONTROL/postinst-pkg"
}

# apk maintainer scripts, mirroring include/package-pack.mk.
# They live OUTSIDE the staged tree: a script inside the tree would also end up
# as a payload file.
apk_write_scripts() {
	local dir="$1" name="$2"
	mkdir -p "$dir"
	cat > "$dir/post-install" <<-EOF
		#!/bin/sh
		[ "\${IPKG_NO_SCRIPT}" = "1" ] && exit 0
		[ -s \${IPKG_INSTROOT}/lib/functions.sh ] || exit 0
		. \${IPKG_INSTROOT}/lib/functions.sh
		export root="\${IPKG_INSTROOT}"
		export pkgname="$name"
		add_group_and_user
		default_postinst
	EOF
	cat > "$dir/post-upgrade" <<-EOF
		#!/bin/sh
		export PKG_UPGRADE=1
		[ "\${IPKG_NO_SCRIPT}" = "1" ] && exit 0
		[ -s \${IPKG_INSTROOT}/lib/functions.sh ] || exit 0
		. \${IPKG_INSTROOT}/lib/functions.sh
		export root="\${IPKG_INSTROOT}"
		export pkgname="$name"
		add_group_and_user
		default_postinst
	EOF
	cat > "$dir/pre-deinstall" <<-EOF
		#!/bin/sh
		[ -s \${IPKG_INSTROOT}/lib/functions.sh ] || exit 0
		. \${IPKG_INSTROOT}/lib/functions.sh
		export root="\${IPKG_INSTROOT}"
		export pkgname="$name"
		default_prerm
	EOF
	chmod 0755 "$dir/post-install" "$dir/post-upgrade" "$dir/pre-deinstall"
}

# LuCI caches must be invalidated when a LuCI package is installed or upgraded.
apk_append_luci_cache_reset() {
	local dir="$1"
	cat >> "$dir/post-install" <<-'EOF'
		[ -n "${IPKG_INSTROOT}" ] || {
			rm -f /tmp/luci-indexcache.* 2>/dev/null
			rm -rf /tmp/luci-modulecache/ 2>/dev/null
			rm -f /tmp/luci-indexcache 2>/dev/null
			killall -HUP rpcd 2>/dev/null
		}
	EOF
}

require_tools() {
	local want_apk="$1" want_ipk="$2" missing=""
	command -v wget >/dev/null || command -v curl >/dev/null || missing="$missing wget-or-curl"
	command -v tar >/dev/null || missing="$missing tar"
	command -v sha256sum >/dev/null || command -v shasum >/dev/null || missing="$missing sha256sum"
	command -v find >/dev/null || missing="$missing find"
	if [ "$want_apk" = 1 ]; then
		command -v apk >/dev/null || missing="$missing apk"
	fi
	if [ "$want_ipk" = 1 ]; then
		command -v ipkg-build >/dev/null || missing="$missing ipkg-build"
	fi
	if [ -n "$missing" ]; then
		echo "error: missing build tools:$missing" >&2
		exit 1
	fi
	if [ "$want_apk" = 1 ]; then
		local apk_ver
		apk_ver="$(apk --version 2>/dev/null | head -1)"
		case "$apk_ver" in
			*3.*) ;;
			*) echo "error: apk mkpkg needs apk-tools >= 3.0, found: ${apk_ver:-none}" >&2; exit 1 ;;
		esac
	fi
}

# ---------------------------------------------------------------------------
# versions
# ---------------------------------------------------------------------------

CORE_VERSION="$(get_mk_value PKG_VERSION "$LUCKY_DIR/Makefile")"
CORE_RELEASE="$(get_mk_value PKG_RELEASE "$LUCKY_DIR/Makefile")"
LUCI_VERSION="$(get_mk_value PKG_VERSION "$LUCI_DIR/Makefile")"
LUCI_RELEASE="$(get_mk_value PKG_RELEASE "$LUCI_DIR/Makefile")"

[ -n "$CORE_VERSION" ] || { echo "error: PKG_VERSION not found in lucky/Makefile" >&2; exit 1; }
[ -n "$LUCI_VERSION" ] || { echo "error: PKG_VERSION not found in luci-app-lucky/Makefile" >&2; exit 1; }
CORE_RELEASE="${CORE_RELEASE:-1}"
LUCI_RELEASE="${LUCI_RELEASE:-1}"

# apk wants "version-r<release>" with no "_" or "~"; ipk uses "_<release>_<arch>".
APK_VERSION="$CORE_VERSION-r$CORE_RELEASE"
LUCI_APK_VERSION="$LUCI_VERSION-r$LUCI_RELEASE"
IPK_VERSION="${CORE_VERSION}_${CORE_RELEASE}"
LUCI_IPK_VERSION="${LUCI_VERSION}_${LUCI_RELEASE}"

export PKG_SOURCE_DATE_EPOCH="${PKG_SOURCE_DATE_EPOCH:-$(date +%s)}"

# OpenWrt package architecture for the device this build targets. The core
# package ships a target binary, so its arch must match the device exactly or
# apk refuses to install it. The luci shell package is arch independent and
# always records "noarch".
case "$PKG_ARCH" in
	arm64) IPK_ARCH="aarch64_generic" ;;
	x86_64) IPK_ARCH="x86_64" ;;
	armv7) IPK_ARCH="arm_cortex-a7" ;;
	armv6) IPK_ARCH="arm_arm1176jzf-s_vfp" ;;
	armv5) IPK_ARCH="arm_arm926ej-s" ;;
	mips_softfloat) IPK_ARCH="mips_24kc" ;;
	mipsle_softfloat) IPK_ARCH="mipsel_24kc" ;;
	i386) IPK_ARCH="i386_pentium4" ;;
	*) IPK_ARCH="all" ;;
esac
APK_ARCH="${APK_ARCH_NAME:-$IPK_ARCH}"

# LuCI locale directory (po/<lang>) -> apk/lmo locale suffix.
locale_suffix() {
	case "$1" in
		zh_Hans) echo "zh-cn" ;;
		zh_Hant) echo "zh-tw" ;;
		pt_BR) echo "pt-br" ;;
		bn_BD) echo "bn" ;;
		*) echo "$1" | tr '[:upper:]_' '[:lower:]-' ;;
	esac
}

# po/<lang>/<pkgname>.po files, one i18n package each
I18N_PO_FILES=()
if [ -d "$LUCI_DIR/po" ]; then
	for po_dir in "$LUCI_DIR"/po/*/; do
		[ -d "$po_dir" ] || continue
		# LuCI names catalogs after the app id without the luci-app- prefix
		# (po/zh_Hans/lucky.po); accept the full package name as a fallback.
		for candidate in "$po_dir/lucky.po" "$po_dir/luci-app-lucky.po"; do
			[ -f "$candidate" ] || continue
			I18N_PO_FILES+=("$candidate")
			break
		done
	done
fi

echo "==> packaging lucky $APK_VERSION / luci-app-lucky $LUCI_APK_VERSION"
echo "    arch:  $PKG_ARCH (package arch: $APK_ARCH, ipk arch: $IPK_ARCH)"
echo "    pkg:   $PKG_MANAGER"
echo "    out:   $OUT_DIR"
if [ "${#I18N_PO_FILES[@]}" -gt 0 ]; then
	i18n_names=""
	for po in "${I18N_PO_FILES[@]}"; do
		i18n_names="$i18n_names $(basename "$(dirname "$po")")"
	done
	echo "    i18n:  ${#I18N_PO_FILES[@]} catalog(s):$i18n_names"
fi

WANT_APK=0; WANT_IPK=0
case "$PKG_MANAGER" in
	apk) WANT_APK=1 ;;
	ipk) WANT_IPK=1 ;;
	both) WANT_APK=1; WANT_IPK=1 ;;
esac
require_tools "$WANT_APK" "$WANT_IPK"

# ---------------------------------------------------------------------------
# payload: fetch the upstream lucky binary once
# ---------------------------------------------------------------------------

WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

# The upstream tarball unpacks a file literally named "lucky", so keep the
# extracted assets and the package staging trees in separate directories.
ASSET_DIR="$WORK_DIR/assets"
mkdir -p "$ASSET_DIR"
CORE_STAGE="$WORK_DIR/stage/lucky"
LUCI_STAGE="$WORK_DIR/stage/luci-app-lucky"
mkdir -p "$CORE_STAGE" "$LUCI_STAGE"

BIN="$ASSET_DIR/lucky"
if [ "$SKIP_DOWNLOAD" = 1 ]; then
	[ -f "$REPO_DIR/lucky/lucky" ] || { echo "error: --skip-download needs lucky/lucky" >&2; exit 1; }
	cp -f "$REPO_DIR/lucky/lucky" "$BIN"
else
	ASSET="lucky_${CORE_VERSION}_Linux_${PKG_ARCH}.tar.gz"
	URL="https://github.com/gdy666/lucky/releases/download/v${CORE_VERSION}/${ASSET}"
	echo "==> downloading $URL"
	download "$URL" "$ASSET_DIR/$ASSET"
	tar -xzf "$ASSET_DIR/$ASSET" -C "$ASSET_DIR"
	[ -f "$BIN" ] || { echo "error: archive did not contain 'lucky'" >&2; exit 1; }
fi

# Stage the payload fresh for every (package, format) pair. The apk build adds
# /lib/apk bookkeeping files to its tree, and reusing that tree for the ipk
# would leak those files into the opkg package.
stage_payload() {
	local stage="$1" which="$2"
	rm -rf "$stage"
	if [ "$which" = lucky ]; then
		# install(1) does not create parent directories, so make them explicitly.
		mkdir -p "$stage/usr/bin" "$stage/etc/init.d" "$stage/etc/config"
		install -m0755 "$BIN" "$stage/usr/bin/lucky"
		install -m0755 "$LUCKY_DIR/files/lucky.init" "$stage/etc/init.d/lucky"
		install -m0600 "$LUCKY_DIR/files/luckyuci" "$stage/etc/config/lucky"
	else
		# luasrc -> /usr/lib/lua/luci, root/ -> /, plus the i18n directory
		mkdir -p "$stage/usr/lib/lua/luci/i18n"
		cp -fpR "$LUCI_DIR/luasrc/." "$stage/usr/lib/lua/luci/"
		cp -fpR "$LUCI_DIR/root/." "$stage/"
	fi
}

# ---------------------------------------------------------------------------
# build the requested formats
# ---------------------------------------------------------------------------

# 1: manager (apk|ipk)  2: stage  3: name  4: version  5: depends  6: description
# 7: apk arch (the core package ships a target binary, so it is arch specific)
build_lucky_core() {
	local mgr="$1" stage="$2" name="$3" version="$4" depends="$5" desc="$6"
	local arch="${7:-$APK_ARCH}"
	local out scripts="$WORK_DIR/scripts/$name"
	stage_payload "$stage" lucky
	if [ "$mgr" = apk ]; then
		apk_write_conffiles "$stage" "$name" /etc/config/lucky
		apk_write_filelist "$stage" "$name"
		apk_write_scripts "$scripts" "$name"
		rm -f "$scripts/post-upgrade"
		out="$OUT_DIR/${FILE_PREFIX}${name}-${version}.apk"
		maybe_fakeroot apk mkpkg \
			--info "name:$name" \
			--info "version:$version" \
			--info "arch:$arch" \
			--info "description:$desc" \
			--info "license:GPL-3.0-only" \
			--info "origin:lucky" \
			--info "url:https://github.com/gdy666/lucky" \
			--info "maintainer:GDY666 <gdy666@foxmail.com>" \
			${depends:+--info "depends:$depends"} \
			--script "post-install:$scripts/post-install" \
			--script "pre-deinstall:$scripts/pre-deinstall" \
			--files "$stage" \
			--output "$out"
	else
		ipk_write_control "$stage" "$name" "$IPK_VERSION" "${depends// /, }" "$desc" "$IPK_ARCH"
		ipk_write_scripts "$stage" "$name"
		echo "/etc/config/lucky" > "$stage/CONTROL/conffiles"
		out="$OUT_DIR/${FILE_PREFIX}${name}_${IPK_VERSION}_${IPK_ARCH}.ipk"
		before="$(mktemp)"; after="$(mktemp)"
		find "$OUT_DIR" -maxdepth 1 -name '*.ipk' | sort > "$before"
		ipkg-build -m "" "$stage" "$OUT_DIR" >/dev/null
		find "$OUT_DIR" -maxdepth 1 -name '*.ipk' | sort > "$after"
		built="$(comm -13 "$before" "$after" | head -n1)"
		rm -f "$before" "$after"
		[ -n "$built" ] || { echo "error: ipkg-build produced no package" >&2; exit 1; }
		mv "$built" "$out"
	fi
}

# 1: manager  2: stage  3: name  4: version  5: depends  6: description
build_lucky_luci() {
	local mgr="$1" stage="$2" name="$3" version="$4" depends="$5" desc="$6"
	local out scripts="$WORK_DIR/scripts/$name"
	stage_payload "$stage" luci-app-lucky
	if [ "$mgr" = apk ]; then
		apk_write_filelist "$stage" "$name"
		apk_write_scripts "$scripts" "$name"
		apk_append_luci_cache_reset "$scripts"
		out="$OUT_DIR/${FILE_PREFIX}${name}-${version}.apk"
		maybe_fakeroot apk mkpkg \
			--info "name:$name" \
			--info "version:$version" \
			--info "arch:noarch" \
			--info "description:$desc" \
			--info "license:GPL-3.0-only" \
			--info "origin:luci-app-lucky" \
			--info "url:https://github.com/gdy666/lucky" \
			--info "maintainer:szwjp <szwjp@users.noreply.github.com>" \
			${depends:+--info "depends:$depends"} \
			--script "post-install:$scripts/post-install" \
			--script "post-upgrade:$scripts/post-upgrade" \
			--script "pre-deinstall:$scripts/pre-deinstall" \
			--files "$stage" \
			--output "$out"
	else
		ipk_write_control "$stage" "$name" "$LUCI_IPK_VERSION" "${depends// /, }" "$desc" all
		ipk_write_scripts "$stage" "$name"
		out="$OUT_DIR/${FILE_PREFIX}${name}_${LUCI_IPK_VERSION}_all.ipk"
		before="$(mktemp)"; after="$(mktemp)"
		find "$OUT_DIR" -maxdepth 1 -name '*.ipk' | sort > "$before"
		ipkg-build -m "" "$stage" "$OUT_DIR" >/dev/null
		find "$OUT_DIR" -maxdepth 1 -name '*.ipk' | sort > "$after"
		built="$(comm -13 "$before" "$after" | head -n1)"
		rm -f "$before" "$after"
		[ -n "$built" ] || { echo "error: ipkg-build produced no package" >&2; exit 1; }
		mv "$built" "$out"
	fi
}

# 1: manager (apk|ipk)  2: po file  3: i18n package name  4: locale suffix
build_lucky_i18n() {
	local mgr="$1" po="$2" name="$3" locale="$4"
	local stage="$WORK_DIR/stage/$name"
	local scripts="$WORK_DIR/scripts/$name"
	local apk_version="$LUCI_VERSION-r$LUCI_RELEASE"
	# LuCI loads <app-id>.<locale>.lmo, where the app id has no luci-app- prefix
	local lmo_name="lucky.$locale.lmo"
	local depends="luci-app-lucky luci-base"
	local out

	rm -rf "$stage"
	mkdir -p "$stage/usr/lib/lua/luci/i18n"
	python3 "$REPO_DIR/scripts/po2lmo.py" "$po" "$stage/usr/lib/lua/luci/i18n/$lmo_name"
	# Invalidate the LuCI index cache once the catalog is unpacked.
	mkdir -p "$stage/etc/uci-defaults"
	cat > "$stage/etc/uci-defaults/$name" <<-EOF
		rm -f /tmp/luci-indexcache* 2>/dev/null
		rm -rf /tmp/luci-modulecache/* 2>/dev/null
		exit 0
	EOF
	chmod 0755 "$stage/etc/uci-defaults/$name"

	if [ "$mgr" = apk ]; then
		apk_write_filelist "$stage" "$name"
		apk_write_scripts "$scripts" "$name"
		apk_append_luci_cache_reset "$scripts"
		out="$OUT_DIR/${FILE_PREFIX}${name}-${apk_version}.apk"
		maybe_fakeroot apk mkpkg \
			--info "name:$name" \
			--info "version:$apk_version" \
			--info "arch:noarch" \
			--info "description:Chinese translation for luci-app-lucky ($locale)" \
			--info "license:GPL-3.0-only" \
			--info "origin:luci-app-lucky" \
			--info "url:https://github.com/gdy666/lucky" \
			--info "maintainer:szwjp <szwjp@users.noreply.github.com>" \
			--info "depends:$depends" \
			--script "post-install:$scripts/post-install" \
			--script "post-upgrade:$scripts/post-upgrade" \
			--script "pre-deinstall:$scripts/pre-deinstall" \
			--files "$stage" \
			--output "$out"
	else
		ipk_write_control "$stage" "$name" "$LUCI_IPK_VERSION" "${depends// /, }" \
			"Chinese translation for luci-app-lucky ($locale)" all
		ipk_write_scripts "$stage" "$name"
		out="$OUT_DIR/${FILE_PREFIX}${name}_${LUCI_IPK_VERSION}_all.ipk"
		before="$(mktemp)"; after="$(mktemp)"
		find "$OUT_DIR" -maxdepth 1 -name '*.ipk' | sort > "$before"
		ipkg-build -m "" "$stage" "$OUT_DIR" >/dev/null
		find "$OUT_DIR" -maxdepth 1 -name '*.ipk' | sort > "$after"
		built="$(comm -13 "$before" "$after" | head -n1)"
		rm -f "$before" "$after"
		[ -n "$built" ] || { echo "error: ipkg-build produced no package" >&2; exit 1; }
		mv "$built" "$out"
	fi
}

# Fallback dependency strings when the Makefiles provide only ipk semantics.
CORE_DEPENDS=""
LUCI_DEPENDS="$(get_mk_multiline_value LUCI_DEPENDS "$LUCI_DIR/Makefile")"
LUCI_DEPENDS="${LUCI_DEPENDS:-lucky luci-compat}"

CORE_DESC="$(get_mk_value TITLE "$LUCKY_DIR/Makefile")"
CORE_DESC="${CORE_DESC:-Lucky gdy - portforward, ddns, reverse proxy and more}"
LUCI_DESC="$(get_mk_value LUCI_TITLE "$LUCI_DIR/Makefile")"
LUCI_DESC="${LUCI_DESC:-LuCI Support for lucky}"

if [ "$WANT_APK" = 1 ]; then
	echo "==> building apk"
	build_lucky_core apk "$CORE_STAGE" lucky "$APK_VERSION" "$CORE_DEPENDS" "$CORE_DESC" "$APK_ARCH"
	build_lucky_luci apk "$LUCI_STAGE" luci-app-lucky "$LUCI_APK_VERSION" "$LUCI_DEPENDS" "$LUCI_DESC"
	for po in ${I18N_PO_FILES[@]+"${I18N_PO_FILES[@]}"}; do
		lang="$(basename "$(dirname "$po")")"
		build_lucky_i18n apk "$po" "luci-i18n-lucky-$(locale_suffix "$lang")" "$(locale_suffix "$lang")"
	done
fi

if [ "$WANT_IPK" = 1 ]; then
	echo "==> building ipk"
	build_lucky_core ipk "$CORE_STAGE" lucky "$IPK_VERSION" "$CORE_DEPENDS" "$CORE_DESC" "$APK_ARCH"
	build_lucky_luci ipk "$LUCI_STAGE" luci-app-lucky "$LUCI_IPK_VERSION" "$LUCI_DEPENDS" "$LUCI_DESC"
	for po in ${I18N_PO_FILES[@]+"${I18N_PO_FILES[@]}"}; do
		lang="$(basename "$(dirname "$po")")"
		build_lucky_i18n ipk "$po" "luci-i18n-lucky-$(locale_suffix "$lang")" "$(locale_suffix "$lang")"
	done
fi

echo "==> done"
ls -l "$OUT_DIR"
