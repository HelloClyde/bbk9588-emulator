/*
 * Ingenic JZ4740 SADC controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_INPUT_JZ4740_SADC_H
#define HW_INPUT_JZ4740_SADC_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_SADC "jz4740-sadc"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740SADCState, JZ4740_SADC)

#define JZ4740_SADC_DEFAULT_BATTERY_RAW 0x0e68u
#define JZ4740_SADC_DEFAULT_SADCIN_RAW  0x0000u

typedef struct JZ4740SADCDiagnostics {
    bool touch_down;
    uint16_t touch_raw_x;
    uint16_t touch_raw_y;
    uint8_t enable;
    uint8_t control;
    uint8_t status;
    uint8_t pending_enable;
    uint32_t fifo_count;
    uint32_t next_axis;
    uint32_t conversion_events_remaining;
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    bool irq_level;
} JZ4740SADCDiagnostics;

typedef void (*JZ4740SADCTraceCallback)(void *opaque, uint32_t reason);

void jz4740_sadc_set_touch(JZ4740SADCState *s, uint16_t raw_x,
                           uint16_t raw_y, bool down);
bool jz4740_sadc_touch_down(JZ4740SADCState *s);
void jz4740_sadc_get_diagnostics(JZ4740SADCState *s,
                                 JZ4740SADCDiagnostics *diagnostics);
void jz4740_sadc_set_trace_callback(JZ4740SADCState *s,
                                    JZ4740SADCTraceCallback callback,
                                    void *opaque);

#endif
