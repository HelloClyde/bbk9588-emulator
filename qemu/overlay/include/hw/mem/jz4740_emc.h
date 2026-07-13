/*
 * Ingenic JZ4740 external memory controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_MEM_JZ4740_EMC_H
#define HW_MEM_JZ4740_EMC_H

#include "hw/core/sysbus.h"

typedef struct Bbk9588NandState Bbk9588NandState;

#define TYPE_JZ4740_EMC "jz4740-emc"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740EMCState, JZ4740_EMC)

typedef struct JZ4740EMCDiagnostics {
    uint32_t nfcsr;
    uint32_t nfeccr;
    uint32_t nfecc;
    uint32_t nfpar[3];
    uint32_t nfints;
    uint32_t nfinte;
    uint32_t nferr[4];
    uint32_t ecc_data_count;
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    bool irq_level;
} JZ4740EMCDiagnostics;

typedef void (*JZ4740EMCBoardWriteCallback)(void *opaque, hwaddr offset,
                                            uint32_t value);

void jz4740_emc_attach_nand(JZ4740EMCState *s, Bbk9588NandState *nand);
void jz4740_emc_set_board_write_callback(
    JZ4740EMCState *s, JZ4740EMCBoardWriteCallback callback, void *opaque);
void jz4740_emc_get_diagnostics(JZ4740EMCState *s,
                                JZ4740EMCDiagnostics *diagnostics);

#endif
