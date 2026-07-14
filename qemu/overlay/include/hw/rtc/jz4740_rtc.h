/*
 * Ingenic JZ4740 real-time clock.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_RTC_JZ4740_RTC_H
#define HW_RTC_JZ4740_RTC_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_RTC "jz4740-rtc"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740RTCState, JZ4740_RTC)

typedef void (*JZ4740RTCPowerDownCallback)(void *opaque);

typedef struct JZ4740RTCDiagnostics {
    uint32_t control;
    uint32_t seconds;
    uint32_t alarm_seconds;
    uint32_t hibernate_control;
    uint32_t wake_status;
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    bool irq_level;
} JZ4740RTCDiagnostics;

uint32_t jz4740_rtc_seconds(JZ4740RTCState *s);
void jz4740_rtc_set_power_down_callback(
    JZ4740RTCState *s, JZ4740RTCPowerDownCallback callback, void *opaque);
void jz4740_rtc_get_diagnostics(JZ4740RTCState *s,
                                JZ4740RTCDiagnostics *diagnostics);

#endif
