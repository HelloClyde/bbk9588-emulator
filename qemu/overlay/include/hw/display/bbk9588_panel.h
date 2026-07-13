/*
 * BBK 9588 board panel/status interface.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_DISPLAY_BBK9588_PANEL_H
#define HW_DISPLAY_BBK9588_PANEL_H

#include "hw/core/sysbus.h"

#define TYPE_BBK9588_PANEL "bbk9588-panel"
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588PanelState, BBK9588_PANEL)

typedef void (*Bbk9588PanelWriteCallback)(void *opaque, hwaddr offset,
                                          uint64_t value, unsigned size);

void bbk9588_panel_set_write_callback(Bbk9588PanelState *s,
                                      Bbk9588PanelWriteCallback callback,
                                      void *opaque);
void bbk9588_panel_set_frame_done(Bbk9588PanelState *s);
uint32_t bbk9588_panel_get_reg(Bbk9588PanelState *s, hwaddr offset);

#endif
