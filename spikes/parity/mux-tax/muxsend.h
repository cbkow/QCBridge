// muxsend — mux Annex-B access units into mpegts and put them on a URL,
// in-process, using libavformat directly instead of piping to an ffmpeg
// child. Exists to measure what that child process was costing (~17 ms a
// hop, per the 2026-09-22 mux-tax run).
//
// Deliberately minimal: one video stream, no audio, no interleaving, no
// reconnect. It is a measurement instrument, not the agent's transport.

#ifndef MUXSEND_H
#define MUXSEND_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Opens `url` (srt://…, tcp://…, udp://…) and writes the mpegts header.
// Blocks for a listener URL until a peer connects, exactly as the ffmpeg
// CLI does. Parameter sets are expected in-band on every keyframe, so no
// extradata is needed up front. Returns 0 on success, negative on failure.
int ms_open(const char *url, int width, int height, int fps, int hevc);

// One access unit, Annex-B, with start codes. `pts` counts frames.
// Returns 0 on success, negative on failure.
int ms_write(const uint8_t *data, int len, int64_t pts, int is_key);

// Writes the trailer and closes. Safe to call when ms_open failed.
void ms_close(void);

// Human-readable text for the last failure, or NULL.
const char *ms_error(void);

#ifdef __cplusplus
}
#endif

#endif
