/*
 * Ingenic JZ4740 LCD controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_DISPLAY_JZ4740_LCD_H
#define HW_DISPLAY_JZ4740_LCD_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_LCD "jz4740-lcd"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740LCDState, JZ4740_LCD)

typedef struct JZ4740LCDDiagnostics {
    uint32_t control;
    uint32_t state;
    uint32_t interrupt_id;
    uint32_t descriptor_address;
    uint32_t framebuffer_address;
    bool descriptor_valid;
    bool framebuffer_valid;
    bool irq_level;
} JZ4740LCDDiagnostics;

typedef void (*JZ4740LCDFrameSourceCallback)(void *opaque);

void jz4740_lcd_get_diagnostics(JZ4740LCDState *s,
                                JZ4740LCDDiagnostics *diagnostics);
void jz4740_lcd_set_frame_source_callback(
    JZ4740LCDState *s, JZ4740LCDFrameSourceCallback callback, void *opaque);
void jz4740_lcd_set_trace_enabled(JZ4740LCDState *s, bool enabled);
bool jz4740_lcd_get_frame_source(JZ4740LCDState *s, uint32_t *frame_va);
bool jz4740_lcd_refresh_frame_source(JZ4740LCDState *s);
bool jz4740_lcd_observe_alias_write(JZ4740LCDState *s, hwaddr offset,
                                    uint32_t value);
void jz4740_lcd_signal_frame_done(JZ4740LCDState *s);

#endif
