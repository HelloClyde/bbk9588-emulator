/*
 * BBK 9588 DMA peripheral bridge.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_DMA_BBK9588_DMA_BRIDGE_H
#define HW_DMA_BBK9588_DMA_BRIDGE_H

#include "hw/core/qdev.h"

#define TYPE_BBK9588_DMA_BRIDGE "bbk9588-dma-bridge"
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588DMABridgeState, BBK9588_DMA_BRIDGE)

typedef struct Bbk9588DiagState Bbk9588DiagState;
typedef struct JZ4740AICState JZ4740AICState;
typedef struct JZ4740DMACState JZ4740DMACState;
typedef struct JZ4740MSCState JZ4740MSCState;

void bbk9588_dma_bridge_connect(Bbk9588DMABridgeState *s,
                                JZ4740DMACState *dmac,
                                JZ4740MSCState *msc,
                                JZ4740AICState *aic,
                                Bbk9588DiagState *diag);

#endif
