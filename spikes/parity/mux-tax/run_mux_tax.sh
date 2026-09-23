#!/bin/zsh
# run_mux_tax.sh — what does putting native-encoded video on the wire cost?
#
# One VideoToolbox encoder (qcb-stamp, configured exactly as vtlat.swift was)
# feeds every rung, and every rung ends at probe_reader.py, so `lat_ms` means
# the same thing throughout and lands in the usual runs.jsonl shape. Rungs
# differ by ONE thing each:
#
#   floor      stamp -> reader                      encode + decode, no wire
#   raw-tcp    + one ffmpeg hop, Annex-B over TCP   a socket, no container
#   srt-L5     + mpegts over SRT, latency 5
#   srt-L20    + mpegts over SRT, latency 20
#   srt-L120   + mpegts over SRT, latency 120       a realistic WAN setting
#   full       srt-L20 + host demux -> local TCP    the option-(1) topology
#
# `full` is the proposed native-capture + SRT path end to end, built from two
# ffmpeg processes because that is exactly what the agent would orchestrate.
# No Rust, no Kyber, no Blender: this decides the transport question alone.
#
# Read the DELTAS, not the absolutes. The floor includes probe_reader's own
# decode, crop and rawvideo pipe, and none of this is motion-to-photon —
# capture and the hot lane sit outside the measurement on purpose. The floor
# is however the SAME floor the sibling result dirs were read against, so the
# absolutes are still comparable to the SRT20 / Kyber baselines there.
#
# There is no mpegts-over-plain-TCP rung: probe_reader's --tcp mode hardcodes
# `-f hevc`, so it can only read raw Annex-B. Isolating the container from
# the protocol would mean editing the reader, and the reader has to stay
# byte-identical for the baselines to remain comparable.
#
# Usage: run_mux_tax.sh <out-dir> [--seconds 30] [--fps 60] [--size WxH]
#                       [--bitrate 50] [--ffmpeg PATH]
set -u

HERE="${0:a:h}"
PARITY="${HERE:h}"
OUT="${1:?usage: run_mux_tax.sh <out-dir> [opts]}"
shift

SECS=30
FPS=60
SIZE=1920x1080
BITRATE=50
FFMPEG=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --seconds) SECS="$2"; shift 2 ;;
        --fps) FPS="$2"; shift 2 ;;
        --size) SIZE="$2"; shift 2 ;;
        --bitrate) BITRATE="$2"; shift 2 ;;
        --ffmpeg) FFMPEG="$2"; shift 2 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

# QCView's own ffmpeg is the right default: it is the build that ships, so
# its SRT and mpegts are the ones the product actually uses.
if [[ -z "$FFMPEG" ]]; then
    for cand in \
        "$HOME/Documents/GitHub/QCView-Player/external/install/bin/ffmpeg" \
        "$(command -v ffmpeg 2>/dev/null)"
    do
        [[ -n "$cand" && -x "$cand" ]] && { FFMPEG="$cand"; break; }
    done
fi
[[ -n "$FFMPEG" && -x "$FFMPEG" ]] || { echo "no ffmpeg; pass --ffmpeg PATH" >&2; exit 1; }
"$FFMPEG" -hide_banner -protocols 2>/dev/null | tr -s ' \n' '\n' | grep -qx srt \
    || { echo "this ffmpeg has no SRT protocol: $FFMPEG" >&2; exit 1; }

STAMP="$HERE/qcb-stamp"
[[ -x "$STAMP" ]] || { echo "build it first: swiftc -O $HERE/qcb-stamp.swift -o $STAMP" >&2; exit 1; }

mkdir -p "$OUT"
RUNS="$OUT/runs.jsonl"
# A fresh port pair per rung. Sharing one port made rungs fail with
# "Address already in use": an SRT listener does not release the port the
# instant its ffmpeg is killed, so the next rung raced it and lost. Walking
# the ports is cheaper than getting the teardown barrier exactly right.
PORT=${QCB_PORT_BASE:-19980}
STAMP_SECS=$(( SECS + 8 ))   # outlive the reader's own window

echo "ffmpeg:  $FFMPEG"
echo "out:     $RUNS"
echo "config:  $SIZE @${FPS} ${BITRATE}M, ${SECS}s per rung"
echo

pids=()
cleanup() {
    for p in ${pids[@]:-}; do
        pkill -P "$p" 2>/dev/null     # ffmpeg's children first
        kill "$p" 2>/dev/null
    done
    pids=()
    # `cmd | ffmpeg &` reports only the tail; the stamp at the head has to go
    # too, or it keeps generating into a broken pipe.
    pkill -f "$STAMP" 2>/dev/null
    sleep 1
}
trap 'cleanup; exit 130' INT TERM

# Wait until a sender is actually listening, rather than hoping a sleep was
# long enough — the reader gets one attempt and a refused connect wastes a
# whole rung.
wait_listen() {   # wait_listen tcp|udp <port> [timeout-s]
    local proto="$1" port="$2" limit="${3:-15}" i=0
    while (( i < limit * 10 )); do
        if [[ "$proto" == "tcp" ]]; then
            lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1 && return 0
        else
            lsof -nP -iUDP:"$port" >/dev/null 2>&1 && return 0
        fi
        sleep 0.1
        (( i += 1 ))
    done
    echo "  !! nothing listening on $proto/$port after ${limit}s" >&2
    return 1
}

stamp_cmd() { "$STAMP" --fps "$FPS" --size "$SIZE" --bitrate "$BITRATE" --seconds "$STAMP_SECS"; }
reader() {
    local label="$1"; shift
    python3 "$PARITY/probe_reader.py" "$@" --ffmpeg "$FFMPEG" \
        --label "$label" --seconds "$SECS" --out "$RUNS" 2>"$OUT/$label.log"
}
# A sender leg: read Annex-B on stdin, copy it to <format> at <url>.
send_to() {
    "$FFMPEG" -hide_banner -loglevel warning -nostats \
        -fflags nobuffer -flags low_delay -f hevc -i - -c copy -f "$1" "$2"
}

# ---- floor -----------------------------------------------------------------
echo "==> floor    stamp -> reader"
stamp_cmd 2>"$OUT/floor.stamp.log" | reader "mux-floor" --stdin
cleanup

# ---- one ffmpeg hop, raw Annex-B over a socket -----------------------------
TP=$(( PORT++ ))
echo "==> raw-tcp  + one hop, no container  (tcp/$TP)"
stamp_cmd 2>/dev/null | send_to hevc "tcp://127.0.0.1:$TP?listen=1" \
    2>"$OUT/raw-tcp.send.log" &
pids+=($!)
wait_listen tcp "$TP" && reader "mux-raw-tcp" --tcp "127.0.0.1:$TP"
cleanup

# ---- mpegts over SRT, three latency settings -------------------------------
for L in 5 20 120; do
    SP=$(( PORT++ ))
    echo "==> srt-L$L   + mpegts/SRT at latency $L  (udp/$SP)"
    stamp_cmd 2>/dev/null \
        | send_to mpegts "srt://127.0.0.1:$SP?mode=listener&latency=$(( L * 1000 ))" \
            2>"$OUT/srt-L$L.send.log" &
    pids+=($!)
    wait_listen udp "$SP" && reader "mux-srt-L$L" --srt "127.0.0.1:$SP" --latency "$L"
    cleanup
done

# ---- in-process mux: the sender hop removed --------------------------------
# qcb-stamp muxes mpegts and opens the SRT itself (muxsend.c), so this is the
# same wire as srt-L20 with one fewer process. Note there is no host leg at
# all: QCView's LiveStreamDecoder opens srt:// directly, which is what the
# pre-Kyber flow did.
SP=$(( PORT++ ))
echo "==> inproc   in-process mpegts/SRT, no sender hop  (udp/$SP)"
"$STAMP" --fps "$FPS" --size "$SIZE" --bitrate "$BITRATE" --seconds "$STAMP_SECS" \
    --mux-url "srt://127.0.0.1:$SP?mode=listener&latency=20000" \
    2>"$OUT/inproc.stamp.log" &
pids+=($!)
wait_listen udp "$SP" && reader "mux-inproc-L20" --srt "127.0.0.1:$SP" --latency 20
cleanup

# ---- the full option-(1) topology ------------------------------------------
SP=$(( PORT++ )); TP=$(( PORT++ ))
echo "==> full     + host demux -> local TCP  (udp/$SP -> tcp/$TP)"
stamp_cmd 2>/dev/null \
    | send_to mpegts "srt://127.0.0.1:$SP?mode=listener&latency=20000" \
        2>"$OUT/full.send.log" &
pids+=($!)
if wait_listen udp "$SP"; then
    # The host leg: pull the SRT, strip mpegts, serve Annex-B on localhost —
    # what video.rs already does for its "local TCP viewers".
    "$FFMPEG" -hide_banner -loglevel warning -nostats \
        -fflags nobuffer -flags low_delay \
        -i "srt://127.0.0.1:$SP?mode=caller&latency=20000" \
        -c copy -f hevc "tcp://127.0.0.1:$TP?listen=1" 2>"$OUT/full.host.log" &
    pids+=($!)
    wait_listen tcp "$TP" && reader "mux-full" --tcp "127.0.0.1:$TP"
fi
cleanup

# ---- in-process sender, but keeping the host fan-out hop -------------------
# Isolates the two hops from each other: this is inproc plus the host leg.
SP=$(( PORT++ )); TP=$(( PORT++ ))
echo "==> inproc-full  in-process sender + host demux -> local TCP  (udp/$SP -> tcp/$TP)"
"$STAMP" --fps "$FPS" --size "$SIZE" --bitrate "$BITRATE" --seconds "$STAMP_SECS" \
    --mux-url "srt://127.0.0.1:$SP?mode=listener&latency=20000" \
    2>"$OUT/inproc-full.stamp.log" &
pids+=($!)
if wait_listen udp "$SP"; then
    "$FFMPEG" -hide_banner -loglevel warning -nostats \
        -fflags nobuffer -flags low_delay \
        -i "srt://127.0.0.1:$SP?mode=caller&latency=20000" \
        -c copy -f hevc "tcp://127.0.0.1:$TP?listen=1" 2>"$OUT/inproc-full.host.log" &
    pids+=($!)
    wait_listen tcp "$TP" && reader "mux-inproc-full" --tcp "127.0.0.1:$TP"
fi
cleanup

trap - INT TERM
echo
python3 "$HERE/summarize.py" "$RUNS"
