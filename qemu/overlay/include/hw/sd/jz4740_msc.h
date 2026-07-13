/*
 * Ingenic JZ4740 multimedia card controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_SD_JZ4740_MSC_H
#define HW_SD_JZ4740_MSC_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_MSC "jz4740-msc"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740MSCState, JZ4740_MSC)

typedef struct JZ4740MSCDMATransfer {
    bool read;
    uint32_t lba;
    uint32_t dma_phys;
    uint32_t words;
    uint32_t bytes;
    uint32_t sectors;
    uint32_t command;
    uint32_t argument;
} JZ4740MSCDMATransfer;

typedef struct JZ4740MSCDiagnostics {
    bool read_pending;
    bool write_pending;
    bool data_ready;
    uint32_t read_lba;
    uint32_t write_lba;
    uint32_t last_command;
    uint32_t last_argument;
    uint32_t last_dma_phys;
    uint32_t last_dma_words;
    uint32_t dma_complete_count;
} JZ4740MSCDiagnostics;

typedef void (*JZ4740MSCKickCallback)(void *opaque);
typedef void (*JZ4740MSCCommandCallback)(void *opaque, uint32_t command,
                                         uint32_t argument);

void jz4740_msc_set_kick_callback(JZ4740MSCState *s,
                                  JZ4740MSCKickCallback callback,
                                  void *opaque);
void jz4740_msc_set_command_callback(JZ4740MSCState *s,
                                     JZ4740MSCCommandCallback callback,
                                     void *opaque);
bool jz4740_msc_begin_dma(JZ4740MSCState *s, unsigned channel,
                          uint32_t source, uint32_t target, uint32_t words,
                          JZ4740MSCDMATransfer *transfer);
void jz4740_msc_finish_dma(JZ4740MSCState *s, bool data_ready);
void jz4740_msc_get_diagnostics(JZ4740MSCState *s,
                                JZ4740MSCDiagnostics *diagnostics);

#endif
