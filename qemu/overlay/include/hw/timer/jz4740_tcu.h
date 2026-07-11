/*
 * Ingenic JZ4740 timer/counter unit.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_TIMER_JZ4740_TCU_H
#define HW_TIMER_JZ4740_TCU_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_TCU "jz4740-tcu"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740TCUState, JZ4740_TCU)

#define JZ4740_TCU_CHANNELS 8u

enum {
    JZ4740_TCU_IRQ_TCU0,
    JZ4740_TCU_IRQ_TCU1,
    JZ4740_TCU_IRQ_TCU2,
    JZ4740_TCU_EVENT,
    JZ4740_TCU_NUM_OUTPUTS,
};

typedef struct JZ4740TCUDiagnostics {
    uint32_t enabled_mask;
    uint32_t stop_mask;
    uint32_t pending_mask;
    uint32_t irq_mask;
    uint32_t compare[JZ4740_TCU_CHANNELS];
    uint32_t half_compare[JZ4740_TCU_CHANNELS];
    uint32_t period_ms[JZ4740_TCU_CHANNELS];
    uint32_t half_period_ms[JZ4740_TCU_CHANNELS];
    int64_t deadline_ns[JZ4740_TCU_CHANNELS];
    int64_t half_deadline_ns[JZ4740_TCU_CHANNELS];
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    uint32_t irq_raise_count;
} JZ4740TCUDiagnostics;

bool jz4740_tcu_irq_level(JZ4740TCUState *s, unsigned output);
void jz4740_tcu_get_diagnostics(JZ4740TCUState *s,
                                JZ4740TCUDiagnostics *diagnostics);

#endif
