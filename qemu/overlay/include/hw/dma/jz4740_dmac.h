/*
 * Ingenic JZ4740 DMA controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_DMA_JZ4740_DMAC_H
#define HW_DMA_JZ4740_DMAC_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_DMAC "jz4740-dmac"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740DMACState, JZ4740_DMAC)

#define JZ4740_DMAC_CHANNELS 6u
#define JZ4740_DMAC_REQUESTS 32u
#define JZ4740_DMAC_REQUEST_AIC_TX 24u
#define JZ4740_DMAC_REQUEST_AIC_RX 25u

typedef struct JZ4740DMACEndpointDiagnostics {
    uint64_t underruns;
    uint32_t fifo_level;
} JZ4740DMACEndpointDiagnostics;

typedef struct JZ4740DMACDiagnostics {
    uint64_t audio_completion_count;
    uint64_t audio_rearm_count;
    uint64_t audio_last_rearm_gap_ns;
    uint64_t audio_max_rearm_gap_ns;
    uint64_t audio_total_rearm_gap_ns;
    uint64_t audio_last_gap_underruns;
    uint64_t audio_total_gap_underruns;
    uint32_t audio_last_units;
    uint32_t audio_completion_fifo;
    uint32_t audio_rearm_fifo;
} JZ4740DMACDiagnostics;

typedef struct JZ4740DMACPeripheralOps {
    bool (*bulk_transfer)(void *opaque, unsigned channel, uint32_t request,
                          uint32_t source, uint32_t target, uint32_t count,
                          uint32_t command);
    bool (*address_valid)(void *opaque, unsigned request, uint32_t address);
    size_t (*write)(void *opaque, unsigned request, const uint8_t *buf,
                    size_t bytes, unsigned width);
    size_t (*read)(void *opaque, unsigned request, uint8_t *buf,
                   size_t bytes, unsigned width);
    void (*complete)(void *opaque, unsigned request);
    void (*get_diagnostics)(void *opaque, unsigned request,
                            JZ4740DMACEndpointDiagnostics *diagnostics);
    void (*trace)(void *opaque, uint32_t event, unsigned channel,
                  hwaddr offset, uint32_t value);
} JZ4740DMACPeripheralOps;

void jz4740_dmac_set_peripheral_ops(JZ4740DMACState *s,
                                    const JZ4740DMACPeripheralOps *ops,
                                    void *opaque);
void jz4740_dmac_set_request(JZ4740DMACState *s, unsigned request,
                             bool level);
void jz4740_dmac_kick(JZ4740DMACState *s);
bool jz4740_dmac_irq_pending(JZ4740DMACState *s);
uint32_t jz4740_dmac_get_reg(JZ4740DMACState *s, hwaddr offset);
void jz4740_dmac_get_diagnostics(JZ4740DMACState *s,
                                 JZ4740DMACDiagnostics *diagnostics);

#endif
