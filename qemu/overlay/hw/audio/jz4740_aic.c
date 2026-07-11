/*
 * Ingenic JZ4740 AC97/I2S controller and internal codec.
 *
 * The BBK 9588 firmware uses the internal codec in I2S mode.  The model keeps
 * the complete software-visible AIC register block, FIFO service requests,
 * audio DMA handshakes and the two internal codec control registers.  Host
 * audio is deliberately downstream of the hardware FIFO clock so a missing or
 * slow host backend cannot stall the guest DMA engine.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/audio/jz4740_aic.h"
#include "hw/core/qdev-properties.h"
#include "hw/core/irq.h"
#include "migration/vmstate.h"
#include "qemu/audio.h"
#include "qemu/bswap.h"
#include "qemu/log.h"
#include "qemu/module.h"
#include "qemu/timer.h"

#define JZ4740_AIC_MMIO_SIZE       0x1000u
#define JZ4740_AIC_FIFO_DEPTH      32u
#define JZ4740_AIC_TICK_NS         1000000LL
#define JZ4740_AIC_MAX_TICK_FRAMES 256u

#define AICFR      0x00u
#define AICCR      0x04u
#define ACCR1      0x08u
#define ACCR2      0x0cu
#define I2SCR      0x10u
#define AICSR      0x14u
#define ACSR       0x18u
#define I2SSR      0x1cu
#define ACCAR      0x20u
#define ACCDR      0x24u
#define ACSAR      0x28u
#define ACSDR      0x2cu
#define I2SDIV     0x30u
#define AICDR      0x34u
#define CDCCR1     0x80u
#define CDCCR2     0x84u

#define AICFR_RESET        0x00007800u
#define AICFR_RW_MASK      0x0000ff7fu
#define AICFR_RFTH_SHIFT   12u
#define AICFR_TFTH_SHIFT   8u
#define AICFR_LSMP         (1u << 6)
#define AICFR_ICDC         (1u << 5)
#define AICFR_AUSEL        (1u << 4)
#define AICFR_RST          (1u << 3)
#define AICFR_ENB          (1u << 0)

#define AICCR_RW_MASK      0x003fcf7fu
#define AICCR_OSS_SHIFT    19u
#define AICCR_ISS_SHIFT    16u
#define AICCR_SAMPLE_MASK  0x7u
#define AICCR_RDMS         (1u << 15)
#define AICCR_TDMS         (1u << 14)
#define AICCR_M2S          (1u << 11)
#define AICCR_ENDSW        (1u << 10)
#define AICCR_ASVTSU       (1u << 9)
#define AICCR_FLUSH        (1u << 8)
#define AICCR_EROR         (1u << 6)
#define AICCR_ETUR         (1u << 5)
#define AICCR_ERFS         (1u << 4)
#define AICCR_ETFS         (1u << 3)
#define AICCR_ENLBF        (1u << 2)
#define AICCR_ERPL         (1u << 1)
#define AICCR_EREC         (1u << 0)

#define ACCR1_RW_MASK      0x03ff03ffu
#define ACCR2_RW_MASK      0x0007000fu
#define I2SCR_RW_MASK      0x00001011u
#define I2SCR_STPBK        (1u << 12)

#define AICSR_RFL_SHIFT    24u
#define AICSR_TFL_SHIFT    8u
#define AICSR_ROR          (1u << 6)
#define AICSR_TUR          (1u << 5)
#define AICSR_RFS          (1u << 4)
#define AICSR_TFS          (1u << 3)
#define AICSR_W0C_MASK     (AICSR_ROR | AICSR_TUR)

#define I2SSR_BSY          (1u << 2)
#define I2SDIV_RESET       0x00000003u
#define I2SDIV_RW_MASK     0x0000000fu

#define CDCCR1_RESET       0x021b2302u
#define CDCCR1_RW_MASK     0x3f1f7f03u
#define CDCCR1_SW2ON       (1u << 25)
#define CDCCR1_EDAC        (1u << 24)
#define CDCCR1_HPMUTE      (1u << 14)
#define CDCCR1_SUSPD       (1u << 1)
#define CDCCR1_RST         (1u << 0)

#define CDCCR2_RESET       0x00170803u
#define CDCCR2_RW_MASK     0x001f0f33u
#define CDCCR2_SMPR_SHIFT  8u
#define CDCCR2_SMPR_MASK   0x0fu
#define CDCCR2_HPVOL_MASK  0x03u

struct JZ4740AICState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irqs[JZ4740_AIC_NUM_IRQS];

    uint32_t aicfr;
    uint32_t aiccr;
    uint32_t accr1;
    uint32_t accr2;
    uint32_t i2scr;
    uint32_t status_flags;
    uint32_t acsr;
    uint32_t accar;
    uint32_t accdr;
    uint32_t acsar;
    uint32_t acsdr;
    uint32_t i2sdiv;
    uint32_t cdccr1;
    uint32_t cdccr2;

    uint32_t tx_fifo[JZ4740_AIC_FIFO_DEPTH];
    uint32_t rx_fifo[JZ4740_AIC_FIFO_DEPTH];
    uint8_t tx_head;
    uint8_t tx_count;
    uint8_t rx_head;
    uint8_t rx_count;

    uint32_t last_tx_sample[2];
    uint8_t next_tx_channel;

    AudioBackend *audio_be;
    SWVoiceOut *out_voice;
    SWVoiceIn *in_voice;
    QEMUTimer *sample_timer;
    int64_t last_tick_ns;
    uint64_t frame_fraction;
    uint64_t pending_output_frames;
    uint32_t host_sample_rate;
    int out_free_bytes;
    int in_available_bytes;
    bool timer_running;
    bool input_voice_attempted;
    bool tx_dma_boundary;

    uint64_t tx_dma_samples;
    uint64_t rx_dma_samples;
    uint64_t output_frames;
    uint64_t input_frames;
    uint64_t underruns;
    uint64_t overruns;
};

static const uint32_t jz4740_codec_sample_rates[9] = {
    8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000,
};

static unsigned jz4740_aic_sample_bits(uint32_t field)
{
    static const unsigned sizes[5] = { 8, 16, 18, 20, 24 };

    return field < ARRAY_SIZE(sizes) ? sizes[field] : 16;
}

static unsigned jz4740_aic_output_bits(JZ4740AICState *s)
{
    return jz4740_aic_sample_bits((s->aiccr >> AICCR_OSS_SHIFT) &
                                  AICCR_SAMPLE_MASK);
}

static unsigned jz4740_aic_input_bits(JZ4740AICState *s)
{
    return jz4740_aic_sample_bits((s->aiccr >> AICCR_ISS_SHIFT) &
                                  AICCR_SAMPLE_MASK);
}

static uint32_t jz4740_aic_sample_rate(JZ4740AICState *s)
{
    uint32_t index = (s->cdccr2 >> CDCCR2_SMPR_SHIFT) & CDCCR2_SMPR_MASK;

    return index < ARRAY_SIZE(jz4740_codec_sample_rates) ?
           jz4740_codec_sample_rates[index] : 48000;
}

static bool jz4740_aic_playing(JZ4740AICState *s)
{
    return (s->aicfr & AICFR_ENB) && (s->aiccr & AICCR_ERPL) &&
           !(s->aiccr & AICCR_ENLBF);
}

static bool jz4740_aic_recording(JZ4740AICState *s)
{
    return (s->aicfr & AICFR_ENB) && (s->aiccr & AICCR_EREC) &&
           !(s->aiccr & AICCR_ENLBF);
}

static bool jz4740_aic_codec_muted(JZ4740AICState *s)
{
    uint32_t required = CDCCR1_SW2ON | CDCCR1_EDAC;

    return (s->cdccr1 & required) != required ||
           (s->cdccr1 & (CDCCR1_HPMUTE | CDCCR1_SUSPD | CDCCR1_RST));
}

void jz4740_aic_get_diagnostics(JZ4740AICState *s,
                                JZ4740AICDiagnostics *diagnostics)
{
    uint32_t flags = 0;

    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    if (jz4740_aic_playing(s)) {
        flags |= JZ4740_AIC_DIAG_PLAYING;
    }
    if (jz4740_aic_recording(s)) {
        flags |= JZ4740_AIC_DIAG_RECORDING;
    }
    if (jz4740_aic_codec_muted(s)) {
        flags |= JZ4740_AIC_DIAG_MUTED;
    }
    if (s->timer_running) {
        flags |= JZ4740_AIC_DIAG_TIMER_RUNNING;
    }
    if (s->out_voice) {
        flags |= JZ4740_AIC_DIAG_OUTPUT_VOICE;
    }
    if (s->in_voice) {
        flags |= JZ4740_AIC_DIAG_INPUT_VOICE;
    }

    diagnostics->sample_rate = jz4740_aic_sample_rate(s);
    diagnostics->tx_fifo_level = s->tx_count;
    diagnostics->rx_fifo_level = s->rx_count;
    diagnostics->flags = flags;
    diagnostics->aicfr = s->aicfr;
    diagnostics->aiccr = s->aiccr;
    diagnostics->cdccr1 = s->cdccr1;
    diagnostics->cdccr2 = s->cdccr2;
    diagnostics->tx_dma_samples = s->tx_dma_samples;
    diagnostics->rx_dma_samples = s->rx_dma_samples;
    diagnostics->output_frames = s->output_frames;
    diagnostics->input_frames = s->input_frames;
    diagnostics->underruns = s->underruns;
    diagnostics->overruns = s->overruns;
}

static uint32_t jz4740_aic_tx_threshold(JZ4740AICState *s)
{
    return ((s->aicfr >> AICFR_TFTH_SHIFT) & 0xfu) * 2u;
}

static uint32_t jz4740_aic_rx_threshold(JZ4740AICState *s)
{
    return (((s->aicfr >> AICFR_RFTH_SHIFT) & 0xfu) + 1u) * 2u;
}

bool jz4740_aic_tx_dma_requested(JZ4740AICState *s)
{
    return s && (s->aiccr & AICCR_TDMS) &&
           s->tx_count <= jz4740_aic_tx_threshold(s);
}

bool jz4740_aic_rx_dma_requested(JZ4740AICState *s)
{
    return s && (s->aiccr & AICCR_RDMS) &&
           s->rx_count >= jz4740_aic_rx_threshold(s);
}

void jz4740_aic_notify_tx_dma_boundary(JZ4740AICState *s)
{
    if (s) {
        s->tx_dma_boundary = true;
    }
}

static uint32_t jz4740_aic_status(JZ4740AICState *s)
{
    uint32_t status = s->status_flags;

    status |= (uint32_t)s->rx_count << AICSR_RFL_SHIFT;
    status |= (uint32_t)s->tx_count << AICSR_TFL_SHIFT;
    if (s->rx_count >= jz4740_aic_rx_threshold(s)) {
        status |= AICSR_RFS;
    }
    if (s->tx_count <= jz4740_aic_tx_threshold(s)) {
        status |= AICSR_TFS;
    }
    return status;
}

static void jz4740_aic_update_lines(JZ4740AICState *s)
{
    uint32_t status = jz4740_aic_status(s);
    bool irq = ((status & AICSR_ROR) && (s->aiccr & AICCR_EROR)) ||
               ((status & AICSR_TUR) && (s->aiccr & AICCR_ETUR)) ||
               ((status & AICSR_RFS) && (s->aiccr & AICCR_ERFS)) ||
               ((status & AICSR_TFS) && (s->aiccr & AICCR_ETFS));

    qemu_set_irq(s->irqs[JZ4740_AIC_IRQ], irq);
    qemu_set_irq(s->irqs[JZ4740_AIC_TX_DMA_REQUEST],
                 jz4740_aic_tx_dma_requested(s));
    qemu_set_irq(s->irqs[JZ4740_AIC_RX_DMA_REQUEST],
                 jz4740_aic_rx_dma_requested(s));
}

static void jz4740_aic_fifo_clear(JZ4740AICState *s)
{
    s->tx_head = 0;
    s->tx_count = 0;
    s->rx_head = 0;
    s->rx_count = 0;
    memset(s->tx_fifo, 0, sizeof(s->tx_fifo));
    memset(s->rx_fifo, 0, sizeof(s->rx_fifo));
    s->next_tx_channel = 0;
}

static bool jz4740_aic_tx_push(JZ4740AICState *s, uint32_t sample)
{
    uint32_t tail;

    if (s->tx_count >= JZ4740_AIC_FIFO_DEPTH) {
        return false;
    }
    tail = (s->tx_head + s->tx_count) % JZ4740_AIC_FIFO_DEPTH;
    s->tx_fifo[tail] = sample & 0x00ffffffu;
    s->tx_count++;
    jz4740_aic_update_lines(s);
    return true;
}

static bool jz4740_aic_tx_pop(JZ4740AICState *s, uint32_t *sample)
{
    jz4740_aic_update_lines(s);
    if (s->tx_count == 0) {
        s->status_flags |= AICSR_TUR;
        s->underruns++;
        jz4740_aic_update_lines(s);
        return false;
    }
    *sample = s->tx_fifo[s->tx_head];
    s->tx_head = (s->tx_head + 1u) % JZ4740_AIC_FIFO_DEPTH;
    s->tx_count--;
    jz4740_aic_update_lines(s);
    return true;
}

static bool jz4740_aic_rx_push(JZ4740AICState *s, uint32_t sample)
{
    uint32_t tail;

    if (s->rx_count >= JZ4740_AIC_FIFO_DEPTH) {
        s->status_flags |= AICSR_ROR;
        s->overruns++;
        jz4740_aic_update_lines(s);
        return false;
    }
    tail = (s->rx_head + s->rx_count) % JZ4740_AIC_FIFO_DEPTH;
    s->rx_fifo[tail] = sample & 0x00ffffffu;
    s->rx_count++;
    jz4740_aic_update_lines(s);
    return true;
}

static bool jz4740_aic_rx_pop(JZ4740AICState *s, uint32_t *sample)
{
    if (s->rx_count == 0) {
        *sample = 0;
        return false;
    }
    *sample = s->rx_fifo[s->rx_head];
    s->rx_head = (s->rx_head + 1u) % JZ4740_AIC_FIFO_DEPTH;
    s->rx_count--;
    jz4740_aic_update_lines(s);
    return true;
}

static uint32_t jz4740_aic_load_sample(const uint8_t *p,
                                       unsigned sample_bytes)
{
    switch (sample_bytes) {
    case 1:
        return p[0];
    case 2:
        return lduw_le_p(p);
    case 4:
        return ldl_le_p(p) & 0x00ffffffu;
    default:
        return 0;
    }
}

static void jz4740_aic_store_sample(uint8_t *p, unsigned sample_bytes,
                                    uint32_t sample)
{
    switch (sample_bytes) {
    case 1:
        p[0] = sample;
        break;
    case 2:
        stw_le_p(p, sample);
        break;
    case 4:
        stl_le_p(p, sample & 0x00ffffffu);
        break;
    }
}

size_t jz4740_aic_dma_write_tx(JZ4740AICState *s, const uint8_t *buf,
                               size_t bytes, unsigned sample_bytes)
{
    size_t done = 0;

    if (!s || !buf || (sample_bytes != 1 && sample_bytes != 2 &&
                       sample_bytes != 4)) {
        return 0;
    }
    while (done + sample_bytes <= bytes &&
           s->tx_count < JZ4740_AIC_FIFO_DEPTH) {
        if (!jz4740_aic_tx_push(s,
                jz4740_aic_load_sample(buf + done, sample_bytes))) {
            break;
        }
        done += sample_bytes;
    }
    s->tx_dma_samples += done / sample_bytes;
    return done;
}

size_t jz4740_aic_dma_read_rx(JZ4740AICState *s, uint8_t *buf,
                              size_t bytes, unsigned sample_bytes)
{
    size_t done = 0;
    uint32_t sample;

    if (!s || !buf || (sample_bytes != 1 && sample_bytes != 2 &&
                       sample_bytes != 4)) {
        return 0;
    }
    while (done + sample_bytes <= bytes && s->rx_count != 0) {
        if (!jz4740_aic_rx_pop(s, &sample)) {
            break;
        }
        jz4740_aic_store_sample(buf + done, sample_bytes, sample);
        done += sample_bytes;
    }
    s->rx_dma_samples += done / sample_bytes;
    return done;
}

static int16_t jz4740_aic_to_s16(JZ4740AICState *s, uint32_t sample)
{
    unsigned bits = jz4740_aic_output_bits(s);
    uint32_t mask = (1u << bits) - 1u;
    uint32_t sign = 1u << (bits - 1u);
    int32_t value;

    sample &= mask;
    if ((s->aiccr & AICCR_ENDSW) && bits == 16) {
        sample = bswap16(sample);
    }
    if (s->aiccr & AICCR_ASVTSU) {
        sample ^= sign;
    }
    value = (sample ^ sign) - sign;
    if (bits > 16) {
        value >>= bits - 16;
    } else if (bits < 16) {
        value <<= 16 - bits;
    }
    return value;
}

static uint32_t jz4740_aic_from_s16(JZ4740AICState *s, int16_t value)
{
    unsigned bits = jz4740_aic_input_bits(s);
    uint32_t sample;

    if (bits > 16) {
        sample = (uint32_t)(int32_t)value << (bits - 16);
    } else {
        sample = (uint32_t)((int32_t)value >> (16 - bits));
    }
    sample &= (1u << bits) - 1u;
    if (s->aiccr & AICCR_ASVTSU) {
        sample ^= 1u << (bits - 1u);
    }
    return sample;
}

static int16_t jz4740_aic_next_output_sample(JZ4740AICState *s,
                                             unsigned channel)
{
    uint32_t sample;

    if (jz4740_aic_tx_pop(s, &sample)) {
        s->last_tx_sample[channel] = sample;
        return jz4740_aic_to_s16(s, sample);
    }
    if (s->aicfr & AICFR_LSMP) {
        return jz4740_aic_to_s16(s, s->last_tx_sample[channel]);
    }
    return 0;
}

static void jz4740_aic_out_cb(void *opaque, int free_bytes)
{
    JZ4740AICState *s = opaque;

    s->out_free_bytes = MAX(free_bytes, 0);
}

static void jz4740_aic_in_cb(void *opaque, int available_bytes)
{
    JZ4740AICState *s = opaque;

    s->in_available_bytes = MAX(available_bytes, 0);
}

static void jz4740_aic_set_voice_active(JZ4740AICState *s)
{
    bool playing = jz4740_aic_playing(s);
    bool recording = jz4740_aic_recording(s);

    if (s->out_voice) {
        audio_be_set_active_out(s->audio_be, s->out_voice, playing);
        audio_be_set_volume_out_lr(
            s->audio_be, s->out_voice, jz4740_aic_codec_muted(s),
            128 + ((s->cdccr2 & CDCCR2_HPVOL_MASK) * 42),
            128 + ((s->cdccr2 & CDCCR2_HPVOL_MASK) * 42));
    }
    if (s->in_voice) {
        audio_be_set_active_in(s->audio_be, s->in_voice, recording);
    }
}

static void jz4740_aic_open_voices(JZ4740AICState *s)
{
    struct audsettings settings = {
        .freq = jz4740_aic_sample_rate(s),
        .nchannels = 2,
        .fmt = AUDIO_FORMAT_S16,
        .big_endian = false,
    };
    bool recording = jz4740_aic_recording(s);

    if (!s->audio_be) {
        return;
    }
    if (s->host_sample_rate == settings.freq && s->out_voice &&
        (!recording || s->in_voice || s->input_voice_attempted)) {
        jz4740_aic_set_voice_active(s);
        return;
    }
    if (s->host_sample_rate != settings.freq && s->out_voice) {
        audio_be_close_out(s->audio_be, s->out_voice);
        s->out_voice = NULL;
    }
    if (s->host_sample_rate != settings.freq && s->in_voice) {
        audio_be_close_in(s->audio_be, s->in_voice);
        s->in_voice = NULL;
    }
    if (s->host_sample_rate != settings.freq) {
        s->input_voice_attempted = false;
    }
    if (!s->out_voice) {
        s->out_voice = audio_be_open_out(s->audio_be, NULL,
                                         "jz4740-aic.out", s,
                                         jz4740_aic_out_cb, &settings);
    }
    if (recording && !s->in_voice && !s->input_voice_attempted) {
        s->input_voice_attempted = true;
        s->in_voice = audio_be_open_in(s->audio_be, NULL,
                                       "jz4740-aic.in", s,
                                       jz4740_aic_in_cb, &settings);
    }
    s->host_sample_rate = settings.freq;
    s->out_free_bytes = INT_MAX;
    s->in_available_bytes = 0;
    jz4740_aic_set_voice_active(s);
}

static void jz4740_aic_update_timer(JZ4740AICState *s)
{
    bool recording = jz4740_aic_recording(s);
    bool run = jz4740_aic_playing(s) || recording;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (!recording) {
        s->input_voice_attempted = false;
    } else if (!s->in_voice && !s->input_voice_attempted) {
        jz4740_aic_open_voices(s);
    }

    if (run && !s->timer_running) {
        s->timer_running = true;
        s->last_tick_ns = now;
        s->frame_fraction = 0;
        s->pending_output_frames = 0;
        timer_mod(s->sample_timer, now + JZ4740_AIC_TICK_NS);
    } else if (!run && s->timer_running) {
        s->timer_running = false;
        s->pending_output_frames = 0;
        s->tx_dma_boundary = false;
        timer_del(s->sample_timer);
    }
    jz4740_aic_set_voice_active(s);
}

static uint32_t jz4740_aic_process_output(JZ4740AICState *s,
                                          uint32_t frames)
{
    int16_t output[JZ4740_AIC_MAX_TICK_FRAMES * 2u];
    uint32_t processed = 0;

    while (frames != 0) {
        uint32_t count = MIN(frames, JZ4740_AIC_MAX_TICK_FRAMES);
        uint32_t actual = 0;
        bool yield = false;

        for (uint32_t i = 0; i < count; i++) {
            if (s->aiccr & AICCR_M2S) {
                int16_t sample = jz4740_aic_next_output_sample(s, 0);

                output[i * 2] = sample;
                output[i * 2 + 1] = sample;
            } else {
                output[i * 2] = jz4740_aic_next_output_sample(s, 0);
                output[i * 2 + 1] = jz4740_aic_next_output_sample(s, 1);
            }
            actual++;
            if (s->tx_dma_boundary) {
                s->tx_dma_boundary = false;
                yield = true;
                break;
            }
        }
        if (s->out_voice && s->out_free_bytes > 0) {
            size_t bytes = actual * 2u * sizeof(int16_t);
            size_t offered = MIN(bytes, (size_t)s->out_free_bytes);
            size_t written = audio_be_write(s->audio_be, s->out_voice,
                                            output, offered);

            s->out_free_bytes -= MIN((size_t)s->out_free_bytes, written);
        }
        s->output_frames += actual;
        processed += actual;
        frames -= actual;
        if (yield) {
            break;
        }
    }
    return processed;
}

static void jz4740_aic_process_input(JZ4740AICState *s, uint32_t frames)
{
    int16_t input[JZ4740_AIC_MAX_TICK_FRAMES * 2u];

    while (frames != 0) {
        uint32_t count = MIN(frames, JZ4740_AIC_MAX_TICK_FRAMES);
        size_t bytes = count * 2u * sizeof(int16_t);
        size_t read = 0;

        memset(input, 0, bytes);
        if (s->in_voice && s->in_available_bytes > 0) {
            size_t requested = MIN(bytes, (size_t)s->in_available_bytes);

            read = audio_be_read(s->audio_be, s->in_voice, input, requested);
            s->in_available_bytes -= MIN((size_t)s->in_available_bytes, read);
        }
        for (uint32_t i = 0; i < count; i++) {
            if (!jz4740_aic_rx_push(s,
                    jz4740_aic_from_s16(s, input[i * 2]))) {
                break;
            }
        }
        s->input_frames += count;
        frames -= count;
    }
}

static void jz4740_aic_sample_timer(void *opaque)
{
    JZ4740AICState *s = opaque;
    int64_t now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint64_t scaled;
    uint32_t frames;

    if (!s->timer_running) {
        return;
    }
    scaled = (uint64_t)MAX(now - s->last_tick_ns, 0LL) *
             jz4740_aic_sample_rate(s) + s->frame_fraction;
    frames = scaled / NANOSECONDS_PER_SECOND;
    s->frame_fraction = scaled % NANOSECONDS_PER_SECOND;
    s->last_tick_ns = now;

    if (jz4740_aic_playing(s)) {
        uint32_t pending;

        s->pending_output_frames =
            MIN(s->pending_output_frames + frames, (uint64_t)UINT32_MAX);
        pending = (uint32_t)s->pending_output_frames;
        if (pending != 0) {
            s->pending_output_frames -=
                jz4740_aic_process_output(s, pending);
        }
    } else {
        s->pending_output_frames = 0;
        s->tx_dma_boundary = false;
    }
    if (jz4740_aic_recording(s) && frames != 0) {
        jz4740_aic_process_input(s, frames);
    }
    timer_mod(s->sample_timer, now + JZ4740_AIC_TICK_NS);
}

static void jz4740_aic_soft_reset(JZ4740AICState *s)
{
    s->aiccr = 0;
    s->accr1 = 0;
    s->accr2 = 0;
    s->i2scr = 0;
    s->status_flags = 0;
    s->acsr = 0;
    s->accar = 0;
    s->accdr = 0;
    s->acsar = 0;
    s->acsdr = 0;
    jz4740_aic_fifo_clear(s);
}

static uint64_t jz4740_aic_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740AICState *s = opaque;
    uint32_t sample;

    switch (offset) {
    case AICFR:
        return s->aicfr;
    case AICCR:
        return s->aiccr;
    case ACCR1:
        return s->accr1;
    case ACCR2:
        return s->accr2;
    case I2SCR:
        return s->i2scr;
    case AICSR:
        return jz4740_aic_status(s);
    case ACSR:
        return s->acsr;
    case I2SSR:
        return (jz4740_aic_playing(s) || jz4740_aic_recording(s)) ?
               I2SSR_BSY : 0;
    case ACCAR:
        return s->accar;
    case ACCDR:
        return s->accdr;
    case ACSAR:
        return s->acsar;
    case ACSDR:
        return s->acsdr;
    case I2SDIV:
        return s->i2sdiv;
    case AICDR:
        return jz4740_aic_rx_pop(s, &sample) ? sample : 0;
    case CDCCR1:
        return s->cdccr1;
    case CDCCR2:
        return s->cdccr2;
    default:
        qemu_log_mask(LOG_GUEST_ERROR,
                      "jz4740-aic: read from unknown offset 0x%" HWADDR_PRIx
                      "\n", offset);
        return 0;
    }
}

static void jz4740_aic_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    JZ4740AICState *s = opaque;
    uint32_t v = value;
    bool format_changed = false;

    switch (offset) {
    case AICFR:
        s->aicfr = v & AICFR_RW_MASK & ~AICFR_RST;
        if (v & AICFR_RST) {
            jz4740_aic_soft_reset(s);
        }
        break;
    case AICCR:
        s->aiccr = v & AICCR_RW_MASK & ~AICCR_FLUSH;
        if (v & AICCR_FLUSH) {
            jz4740_aic_fifo_clear(s);
        }
        break;
    case ACCR1:
        s->accr1 = v & ACCR1_RW_MASK;
        break;
    case ACCR2:
        s->accr2 = v & ACCR2_RW_MASK;
        break;
    case I2SCR:
        s->i2scr = v & I2SCR_RW_MASK;
        break;
    case AICSR:
        s->status_flags &= v | ~AICSR_W0C_MASK;
        break;
    case ACSR:
        s->acsr &= v;
        break;
    case ACCAR:
        s->accar = v & 0x000fffffu;
        break;
    case ACCDR:
        s->accdr = v & 0x000fffffu;
        break;
    case I2SDIV:
        s->i2sdiv = (v & I2SDIV_RW_MASK) | 1u;
        break;
    case AICDR:
        jz4740_aic_tx_push(s, v);
        break;
    case CDCCR1:
        if (s->aicfr & AICFR_ICDC) {
            s->cdccr1 = v & CDCCR1_RW_MASK;
        }
        break;
    case CDCCR2:
        if (s->aicfr & AICFR_ICDC) {
            format_changed = ((s->cdccr2 ^ v) &
                              (CDCCR2_SMPR_MASK << CDCCR2_SMPR_SHIFT)) != 0;
            s->cdccr2 = v & CDCCR2_RW_MASK;
        }
        break;
    case ACSAR:
    case ACSDR:
    case I2SSR:
        break;
    default:
        qemu_log_mask(LOG_GUEST_ERROR,
                      "jz4740-aic: write to unknown offset 0x%" HWADDR_PRIx
                      " value 0x%08x\n", offset, v);
        break;
    }

    if (format_changed) {
        jz4740_aic_open_voices(s);
    }
    jz4740_aic_update_timer(s);
    jz4740_aic_update_lines(s);
}

static const MemoryRegionOps jz4740_aic_ops = {
    .read = jz4740_aic_read,
    .write = jz4740_aic_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 4,
        .max_access_size = 4,
        .unaligned = false,
    },
};

static void jz4740_aic_reset_hold(Object *obj, ResetType type)
{
    JZ4740AICState *s = JZ4740_AIC(obj);

    s->aicfr = AICFR_RESET;
    s->i2sdiv = I2SDIV_RESET;
    s->cdccr1 = CDCCR1_RESET;
    s->cdccr2 = CDCCR2_RESET;
    s->tx_dma_samples = 0;
    s->rx_dma_samples = 0;
    s->output_frames = 0;
    s->input_frames = 0;
    s->underruns = 0;
    s->overruns = 0;
    s->last_tx_sample[0] = 0;
    s->last_tx_sample[1] = 0;
    s->last_tick_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->frame_fraction = 0;
    s->pending_output_frames = 0;
    s->timer_running = false;
    s->input_voice_attempted = false;
    s->tx_dma_boundary = false;
    timer_del(s->sample_timer);
    jz4740_aic_soft_reset(s);
    jz4740_aic_open_voices(s);
    jz4740_aic_update_lines(s);
}

static int jz4740_aic_post_load(void *opaque, int version_id)
{
    JZ4740AICState *s = opaque;

    s->last_tick_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->timer_running = false;
    s->pending_output_frames = 0;
    s->tx_dma_boundary = false;
    jz4740_aic_open_voices(s);
    jz4740_aic_update_timer(s);
    jz4740_aic_update_lines(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_aic = {
    .name = TYPE_JZ4740_AIC,
    .version_id = 2,
    .minimum_version_id = 1,
    .post_load = jz4740_aic_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(aicfr, JZ4740AICState),
        VMSTATE_UINT32(aiccr, JZ4740AICState),
        VMSTATE_UINT32(accr1, JZ4740AICState),
        VMSTATE_UINT32(accr2, JZ4740AICState),
        VMSTATE_UINT32(i2scr, JZ4740AICState),
        VMSTATE_UINT32(status_flags, JZ4740AICState),
        VMSTATE_UINT32(acsr, JZ4740AICState),
        VMSTATE_UINT32(accar, JZ4740AICState),
        VMSTATE_UINT32(accdr, JZ4740AICState),
        VMSTATE_UINT32(acsar, JZ4740AICState),
        VMSTATE_UINT32(acsdr, JZ4740AICState),
        VMSTATE_UINT32(i2sdiv, JZ4740AICState),
        VMSTATE_UINT32(cdccr1, JZ4740AICState),
        VMSTATE_UINT32(cdccr2, JZ4740AICState),
        VMSTATE_UINT32_ARRAY(tx_fifo, JZ4740AICState,
                             JZ4740_AIC_FIFO_DEPTH),
        VMSTATE_UINT32_ARRAY(rx_fifo, JZ4740AICState,
                             JZ4740_AIC_FIFO_DEPTH),
        VMSTATE_UINT8(tx_head, JZ4740AICState),
        VMSTATE_UINT8(tx_count, JZ4740AICState),
        VMSTATE_UINT8(rx_head, JZ4740AICState),
        VMSTATE_UINT8(rx_count, JZ4740AICState),
        VMSTATE_UINT32_ARRAY(last_tx_sample, JZ4740AICState, 2),
        VMSTATE_UINT8(next_tx_channel, JZ4740AICState),
        VMSTATE_UINT64(frame_fraction, JZ4740AICState),
        VMSTATE_UINT64_V(tx_dma_samples, JZ4740AICState, 2),
        VMSTATE_UINT64_V(rx_dma_samples, JZ4740AICState, 2),
        VMSTATE_UINT64_V(output_frames, JZ4740AICState, 2),
        VMSTATE_UINT64_V(input_frames, JZ4740AICState, 2),
        VMSTATE_UINT64_V(underruns, JZ4740AICState, 2),
        VMSTATE_UINT64_V(overruns, JZ4740AICState, 2),
        VMSTATE_END_OF_LIST()
    },
};

static void jz4740_aic_realize(DeviceState *dev, Error **errp)
{
    JZ4740AICState *s = JZ4740_AIC(dev);

    if (!audio_be_check(&s->audio_be, errp)) {
        return;
    }
    jz4740_aic_open_voices(s);
}

static void jz4740_aic_unrealize(DeviceState *dev)
{
    JZ4740AICState *s = JZ4740_AIC(dev);

    timer_del(s->sample_timer);
    if (s->out_voice) {
        audio_be_close_out(s->audio_be, s->out_voice);
        s->out_voice = NULL;
    }
    if (s->in_voice) {
        audio_be_close_in(s->audio_be, s->in_voice);
        s->in_voice = NULL;
    }
}

static void jz4740_aic_init(Object *obj)
{
    JZ4740AICState *s = JZ4740_AIC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &jz4740_aic_ops, s,
                          TYPE_JZ4740_AIC, JZ4740_AIC_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    for (unsigned i = 0; i < JZ4740_AIC_NUM_IRQS; i++) {
        sysbus_init_irq(sbd, &s->irqs[i]);
    }
    s->sample_timer = timer_new_ns(QEMU_CLOCK_VIRTUAL,
                                   jz4740_aic_sample_timer, s);
}

static void jz4740_aic_finalize(Object *obj)
{
    JZ4740AICState *s = JZ4740_AIC(obj);

    timer_free(s->sample_timer);
}

static const Property jz4740_aic_properties[] = {
    DEFINE_AUDIO_PROPERTIES(JZ4740AICState, audio_be),
};

static void jz4740_aic_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->realize = jz4740_aic_realize;
    dc->unrealize = jz4740_aic_unrealize;
    dc->vmsd = &vmstate_jz4740_aic;
    device_class_set_props(dc, jz4740_aic_properties);
    set_bit(DEVICE_CATEGORY_SOUND, dc->categories);
    rc->phases.hold = jz4740_aic_reset_hold;
}

static const TypeInfo jz4740_aic_info = {
    .name = TYPE_JZ4740_AIC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740AICState),
    .instance_init = jz4740_aic_init,
    .instance_finalize = jz4740_aic_finalize,
    .class_init = jz4740_aic_class_init,
};

static void jz4740_aic_register_types(void)
{
    type_register_static(&jz4740_aic_info);
}

type_init(jz4740_aic_register_types)
