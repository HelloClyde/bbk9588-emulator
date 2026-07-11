/*
 * Ingenic JZ4740 clock and power manager.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_MISC_JZ4740_CPM_H
#define HW_MISC_JZ4740_CPM_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_CPM "jz4740-cpm"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740CPMState, JZ4740_CPM)

typedef void (*JZ4740CPMUpdateFn)(void *opaque);

void jz4740_cpm_set_update(JZ4740CPMState *s,
                           JZ4740CPMUpdateFn update,
                           void *opaque);
uint32_t jz4740_cpm_clkgr_wake_mask(JZ4740CPMState *s);
uint32_t jz4740_cpm_scr_wake_mask(JZ4740CPMState *s);
bool jz4740_cpm_wake_enabled(JZ4740CPMState *s);

#endif
