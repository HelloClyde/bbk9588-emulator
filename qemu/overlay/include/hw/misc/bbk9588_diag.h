/*
 * BBK 9588 guest diagnostic recorder.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_MISC_BBK9588_DIAG_H
#define HW_MISC_BBK9588_DIAG_H

#include "hw/block/bbk9588_nand.h"
#include "hw/core/cpu.h"
#include "hw/core/qdev.h"
#include "hw/display/bbk9588_panel.h"
#include "hw/display/jz4740_lcd.h"
#include "hw/dma/jz4740_dmac.h"
#include "hw/gpio/jz4740_gpio.h"
#include "hw/input/jz4740_sadc.h"
#include "hw/intc/jz4740_intc.h"
#include "hw/mem/jz4740_emc.h"
#include "hw/misc/jz4740_cpm.h"
#include "hw/sd/jz4740_msc.h"
#include "hw/timer/jz4740_tcu.h"

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

typedef struct Bbk9588DiagSources {
    CPUState *cpu;
    Bbk9588NandState *nand;
    Bbk9588PanelState *panel;
    JZ4740CPMState *cpm;
    JZ4740DMACState *dmac;
    JZ4740EMCState *emc;
    JZ4740GPIOState *gpio;
    JZ4740INTCState *intc;
    JZ4740LCDState *lcd;
    JZ4740MSCState *msc;
    JZ4740SADCState *sadc;
    JZ4740TCUState *tcu;
} Bbk9588DiagSources;

typedef struct Bbk9588DiagBoardSnapshot {
    uint32_t intc_last_cp0_status;
    uint32_t intc_last_cp0_cause;
    uint32_t extgpio_wake_enable;
    uint32_t sysctrl_wake_count;
    bool sysctrl_wake_pending;
} Bbk9588DiagBoardSnapshot;

void bbk9588_diag_connect_sources(Bbk9588DiagState *s,
                                  const Bbk9588DiagSources *sources);
void bbk9588_diag_set_storage_enabled(Bbk9588DiagState *s, bool enabled);
bool bbk9588_diag_graphics_enabled(Bbk9588DiagState *s);
void bbk9588_diag_set_graphics_enabled(Bbk9588DiagState *s, bool enabled);
bool bbk9588_diag_touch_enabled(Bbk9588DiagState *s);
void bbk9588_diag_set_touch_enabled(Bbk9588DiagState *s, bool enabled);
bool bbk9588_diag_progress_enabled(Bbk9588DiagState *s);
void bbk9588_diag_set_progress_enabled(Bbk9588DiagState *s, bool enabled);
void bbk9588_diag_reset_input(Bbk9588DiagState *s);
void bbk9588_diag_queue_input(Bbk9588DiagState *s, uint32_t kind,
                              uint32_t arg0, uint32_t arg1, uint32_t arg2);
void bbk9588_diag_note_nand_ready(Bbk9588DiagState *s);
void bbk9588_diag_touch_record(Bbk9588DiagState *s, uint32_t reason,
                               const Bbk9588DiagBoardSnapshot *board);
void bbk9588_diag_progress_record(Bbk9588DiagState *s, uint32_t reason);
void bbk9588_diag_panel_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size);
void bbk9588_diag_storage_record(Bbk9588DiagState *s, uint32_t logical,
                                 uint32_t absolute, uint32_t first_word);
void bbk9588_diag_msc_record(Bbk9588DiagState *s, uint32_t event,
                             uint32_t lba, uint32_t dma_phys,
                             uint32_t bytes, uint32_t command,
                             uint32_t argument, uint32_t first_word);
void bbk9588_diag_nand_target_record(Bbk9588DiagState *s, uint32_t event,
                                     uint32_t a, uint32_t b, uint32_t c,
                                     uint32_t pc);
void bbk9588_diag_dmac_sample(Bbk9588DiagState *s, uint32_t event,
                              unsigned channel, hwaddr offset,
                              uint32_t value);

#endif
