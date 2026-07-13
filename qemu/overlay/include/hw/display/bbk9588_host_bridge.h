/*
 * BBK 9588 host display/audio/performance bridge.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_DISPLAY_BBK9588_HOST_BRIDGE_H
#define HW_DISPLAY_BBK9588_HOST_BRIDGE_H

#include "hw/core/qdev.h"

#define TYPE_BBK9588_HOST_BRIDGE "bbk9588-host-bridge"
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588HostBridgeState, BBK9588_HOST_BRIDGE)

typedef struct Bbk9588PanelState Bbk9588PanelState;
typedef struct JZ4740AICState JZ4740AICState;
typedef struct JZ4740DMACState JZ4740DMACState;
typedef struct JZ4740LCDState JZ4740LCDState;

typedef uint64_t (*Bbk9588GuestInsnCallback)(void *opaque);

void bbk9588_host_bridge_configure(
    Bbk9588HostBridgeState *s, const char *frame_chardev,
    uint32_t refresh_period_ms, Bbk9588GuestInsnCallback guest_insns,
    void *guest_insns_opaque);
void bbk9588_host_bridge_connect_display(Bbk9588HostBridgeState *s,
                                         JZ4740LCDState *lcd,
                                         Bbk9588PanelState *panel);
void bbk9588_host_bridge_connect_audio(Bbk9588HostBridgeState *s,
                                       JZ4740AICState *aic,
                                       JZ4740DMACState *dmac);
void bbk9588_host_bridge_reset_metrics(Bbk9588HostBridgeState *s);
void bbk9588_host_bridge_start(Bbk9588HostBridgeState *s);

#endif
