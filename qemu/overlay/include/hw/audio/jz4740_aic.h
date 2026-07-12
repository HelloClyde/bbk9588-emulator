/*
 * Ingenic JZ4740 AC97/I2S controller and internal codec.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_AUDIO_JZ4740_AIC_H
#define HW_AUDIO_JZ4740_AIC_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_AIC "jz4740-aic"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740AICState, JZ4740_AIC)

enum {
    JZ4740_AIC_IRQ,
    JZ4740_AIC_TX_DMA_REQUEST,
    JZ4740_AIC_RX_DMA_REQUEST,
    JZ4740_AIC_NUM_IRQS,
};

enum {
    JZ4740_AIC_DIAG_PLAYING = 1u << 0,
    JZ4740_AIC_DIAG_RECORDING = 1u << 1,
    JZ4740_AIC_DIAG_MUTED = 1u << 2,
    JZ4740_AIC_DIAG_TIMER_RUNNING = 1u << 3,
    JZ4740_AIC_DIAG_OUTPUT_VOICE = 1u << 4,
    JZ4740_AIC_DIAG_INPUT_VOICE = 1u << 5,
};

typedef struct JZ4740AICDiagnostics {
    uint32_t sample_rate;
    uint32_t tx_fifo_level;
    uint32_t rx_fifo_level;
    uint32_t flags;
    uint32_t aicfr;
    uint32_t aiccr;
    uint32_t cdccr1;
    uint32_t cdccr2;
    uint64_t tx_dma_samples;
    uint64_t rx_dma_samples;
    uint64_t output_frames;
    uint64_t input_frames;
    uint64_t underruns;
    uint64_t overruns;
} JZ4740AICDiagnostics;

typedef void (*JZ4740AICOutputCallback)(void *opaque, uint32_t sample_rate,
                                       const int16_t *samples, size_t frames);

bool jz4740_aic_tx_dma_requested(JZ4740AICState *s);
bool jz4740_aic_rx_dma_requested(JZ4740AICState *s);
void jz4740_aic_notify_tx_dma_boundary(JZ4740AICState *s);

size_t jz4740_aic_dma_write_tx(JZ4740AICState *s, const uint8_t *buf,
                               size_t bytes, unsigned sample_bytes);
size_t jz4740_aic_dma_read_rx(JZ4740AICState *s, uint8_t *buf,
                              size_t bytes, unsigned sample_bytes);
void jz4740_aic_get_diagnostics(JZ4740AICState *s,
                                JZ4740AICDiagnostics *diagnostics);
void jz4740_aic_set_output_callback(JZ4740AICState *s,
                                    JZ4740AICOutputCallback callback,
                                    void *opaque);

#endif
