/*
 * Ingenic JZ4730 watchdog timer.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_TIMER_JZ4730_WDT_H
#define HW_TIMER_JZ4730_WDT_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4730_WDT "jz4730-wdt"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4730WDTState, JZ4730_WDT)

#endif
