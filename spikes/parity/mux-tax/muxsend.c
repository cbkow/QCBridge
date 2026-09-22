#include "muxsend.h"

#include <libavformat/avformat.h>
#include <libavutil/opt.h>
#include <stdio.h>
#include <string.h>

static AVFormatContext *g_oc;
static AVStream *g_st;
static int g_fps;
static char g_err[512];

static void set_err(const char *what, int rc)
{
    char buf[256] = {0};
    av_strerror(rc, buf, sizeof buf);
    snprintf(g_err, sizeof g_err, "%s: %s", what, buf);
}

const char *ms_error(void)
{
    return g_err[0] ? g_err : NULL;
}

int ms_open(const char *url, int width, int height, int fps, int hevc)
{
    g_err[0] = 0;
    g_fps = fps;

    int rc = avformat_alloc_output_context2(&g_oc, NULL, "mpegts", url);
    if (rc < 0 || !g_oc) {
        set_err("alloc_output_context2", rc);
        return rc < 0 ? rc : -1;
    }

    g_st = avformat_new_stream(g_oc, NULL);
    if (!g_st) {
        snprintf(g_err, sizeof g_err, "new_stream failed");
        return -1;
    }
    g_st->codecpar->codec_type = AVMEDIA_TYPE_VIDEO;
    g_st->codecpar->codec_id = hevc ? AV_CODEC_ID_HEVC : AV_CODEC_ID_H264;
    g_st->codecpar->width = width;
    g_st->codecpar->height = height;
    // mpegts rescales to 90 kHz itself; frames are the natural unit here.
    g_st->time_base = (AVRational){1, fps};
    g_st->avg_frame_rate = (AVRational){fps, 1};

    // The same low-latency posture the CLI rungs used, so the comparison is
    // about the process boundary and nothing else.
    g_oc->max_delay = 0;
    g_oc->flags |= AVFMT_FLAG_FLUSH_PACKETS;
    av_opt_set(g_oc->priv_data, "mpegts_flags", "+latm", 0);   // harmless if unsupported

    if (!(g_oc->oformat->flags & AVFMT_NOFILE)) {
        // Blocks here for a listener URL until a peer connects.
        rc = avio_open2(&g_oc->pb, url, AVIO_FLAG_WRITE, NULL, NULL);
        if (rc < 0) {
            set_err("avio_open2", rc);
            return rc;
        }
    }

    AVDictionary *opts = NULL;
    av_dict_set(&opts, "muxdelay", "0", 0);
    av_dict_set(&opts, "muxpreload", "0", 0);
    rc = avformat_write_header(g_oc, &opts);
    av_dict_free(&opts);
    if (rc < 0) {
        set_err("write_header", rc);
        return rc;
    }
    return 0;
}

int ms_write(const uint8_t *data, int len, int64_t pts, int is_key)
{
    if (!g_oc || !g_st) return -1;

    AVPacket *pkt = av_packet_alloc();
    if (!pkt) return -1;
    // av_write_frame does not take ownership of caller memory, so the copy
    // into a reference-counted buffer is the simplest correct thing.
    int rc = av_new_packet(pkt, len);
    if (rc < 0) {
        av_packet_free(&pkt);
        set_err("new_packet", rc);
        return rc;
    }
    memcpy(pkt->data, data, (size_t)len);
    pkt->stream_index = g_st->index;
    pkt->pts = pkt->dts = pts;              // no reordering: dts == pts
    pkt->duration = 1;
    if (is_key) pkt->flags |= AV_PKT_FLAG_KEY;
    av_packet_rescale_ts(pkt, (AVRational){1, g_fps}, g_st->time_base);

    // av_write_frame, not av_interleaved_write_frame: one stream, and
    // interleaving would hold packets back to sort them.
    rc = av_write_frame(g_oc, pkt);
    av_packet_free(&pkt);
    if (rc < 0) {
        set_err("write_frame", rc);
        return rc;
    }
    av_write_frame(g_oc, NULL);   // flush the muxer's queue
    return 0;
}

void ms_close(void)
{
    if (!g_oc) return;
    if (g_st) av_write_trailer(g_oc);
    if (g_oc->pb && !(g_oc->oformat->flags & AVFMT_NOFILE)) avio_closep(&g_oc->pb);
    avformat_free_context(g_oc);
    g_oc = NULL;
    g_st = NULL;
}
