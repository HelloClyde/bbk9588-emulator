/*
 * BBK 9588 guest diagnostic recorder.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_MISC_BBK9588_DIAG_H
#define HW_MISC_BBK9588_DIAG_H

#include "hw/core/qdev.h"

#define TYPE_BBK9588_DIAG "bbk9588-diag"
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588DiagState, BBK9588_DIAG)

#define BBK9588_DIAG_EVENT_KIND_KEY       1u
#define BBK9588_DIAG_EVENT_KIND_TOUCH     2u

#define BBK9588_DIAG_STORAGE_NAND_READ    0x80000000u
#define BBK9588_DIAG_STORAGE_NAND_PROGRAM 0x20000000u
#define BBK9588_DIAG_STORAGE_NAND_ERASE   0x10000000u
#define BBK9588_DIAG_STORAGE_DMAC_TRANSFER 0x08000000u
#define BBK9588_DIAG_STORAGE_NAND_DETAIL  0x04000000u

#define BBK9588_DIAG_MSC_READ             1u
#define BBK9588_DIAG_MSC_WRITE            2u
#define BBK9588_DIAG_MSC_COMMAND          3u

void bbk9588_diag_set_storage_enabled(Bbk9588DiagState *s, bool enabled);
void bbk9588_diag_reset_input(Bbk9588DiagState *s);
void bbk9588_diag_queue_input(Bbk9588DiagState *s, uint32_t kind,
                              uint32_t arg0, uint32_t arg1, uint32_t arg2);
void bbk9588_diag_storage_record(Bbk9588DiagState *s, uint32_t logical,
                                 uint32_t absolute, uint32_t first_word);
void bbk9588_diag_msc_record(Bbk9588DiagState *s, uint32_t event,
                             uint32_t lba, uint32_t dma_phys,
                             uint32_t bytes, uint32_t command,
                             uint32_t argument, uint32_t first_word,
                             uint32_t pc);
void bbk9588_diag_nand_target_record(Bbk9588DiagState *s, uint32_t event,
                                     uint32_t a, uint32_t b, uint32_t c,
                                     uint32_t pc);
void bbk9588_diag_dmac_record(Bbk9588DiagState *s, uint32_t event,
                              unsigned channel, hwaddr offset,
                              uint32_t value, uint32_t pc,
                              uint32_t intc_pending, uint32_t intc_mask,
                              const uint32_t channel_regs[7]);

#endif
