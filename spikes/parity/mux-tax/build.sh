#!/bin/zsh
# build.sh — qcb-stamp, with the in-process mpegts/SRT sender linked in.
#
# The FFmpeg libraries come from QCView's vendored prefix, which is the same
# build the CLI rungs used, so "in-process" versus "ffmpeg child" really is
# the only thing that differs between them. libsrt is already inside
# libavformat there.
#
# Usage: build.sh [--prefix DIR]
set -eu

HERE="${0:a:h}"
PREFIX="${QCB_FFPREFIX:-$HOME/Documents/GitHub/QCView-Player/external/install}"
[[ "${1:-}" == "--prefix" ]] && PREFIX="$2"

[[ -d "$PREFIX/include/libavformat" ]] \
    || { echo "no libavformat headers under $PREFIX — pass --prefix" >&2; exit 1; }

cd "$HERE"
echo "==> muxsend.c"
clang -c -O2 -Wall -Wextra muxsend.c -o muxsend.o -I"$PREFIX/include"

echo "==> qcb-stamp"
swiftc -O qcb-stamp.swift muxsend.o \
    -import-objc-header muxsend.h \
    -I"$PREFIX/include" -L"$PREFIX/lib" \
    -lavformat -lavcodec -lavutil \
    -Xlinker -rpath -Xlinker "$PREFIX/lib" \
    -o qcb-stamp

rm -f muxsend.o
echo "==> ok: $HERE/qcb-stamp"
