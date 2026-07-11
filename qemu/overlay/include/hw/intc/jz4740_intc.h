/*
 * Ingenic JZ4740 interrupt controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_INTC_JZ4740_INTC_H
#define HW_INTC_JZ4740_INTC_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_INTC "jz4740-intc"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740INTCState, JZ4740_INTC)

enum {
    JZ4740_INTC_IRQ_I2C = 2,
    JZ4740_INTC_IRQ_EMC = 3,
    JZ4740_INTC_IRQ_UHC = 4,
    JZ4740_INTC_IRQ_UART0 = 9,
    JZ4740_INTC_IRQ_SADC = 12,
    JZ4740_INTC_IRQ_MSC = 14,
    JZ4740_INTC_IRQ_RTC = 15,
    JZ4740_INTC_IRQ_SSI = 16,
    JZ4740_INTC_IRQ_CIM = 17,
    JZ4740_INTC_IRQ_AIC = 18,
    JZ4740_INTC_IRQ_DMA = 20,
    JZ4740_INTC_IRQ_TCU2 = 21,
    JZ4740_INTC_IRQ_TCU1 = 22,
    JZ4740_INTC_IRQ_TCU0 = 23,
    JZ4740_INTC_IRQ_UDC = 24,
    JZ4740_INTC_IRQ_GPIO3 = 25,
    JZ4740_INTC_IRQ_GPIO2 = 26,
    JZ4740_INTC_IRQ_GPIO1 = 27,
    JZ4740_INTC_IRQ_GPIO0 = 28,
    JZ4740_INTC_IRQ_IPU = 29,
    JZ4740_INTC_IRQ_LCD = 30,
    JZ4740_INTC_NUM_IRQS = 32,
};

#define JZ4740_INTC_SOURCE_MASK \
    ((1u << JZ4740_INTC_IRQ_LCD) | \
     (1u << JZ4740_INTC_IRQ_IPU) | \
     (1u << JZ4740_INTC_IRQ_GPIO0) | \
     (1u << JZ4740_INTC_IRQ_GPIO1) | \
     (1u << JZ4740_INTC_IRQ_GPIO2) | \
     (1u << JZ4740_INTC_IRQ_GPIO3) | \
     (1u << JZ4740_INTC_IRQ_UDC) | \
     (1u << JZ4740_INTC_IRQ_TCU0) | \
     (1u << JZ4740_INTC_IRQ_TCU1) | \
     (1u << JZ4740_INTC_IRQ_TCU2) | \
     (1u << JZ4740_INTC_IRQ_DMA) | \
     (1u << JZ4740_INTC_IRQ_AIC) | \
     (1u << JZ4740_INTC_IRQ_CIM) | \
     (1u << JZ4740_INTC_IRQ_SSI) | \
     (1u << JZ4740_INTC_IRQ_RTC) | \
     (1u << JZ4740_INTC_IRQ_MSC) | \
     (1u << JZ4740_INTC_IRQ_SADC) | \
     (1u << JZ4740_INTC_IRQ_UART0) | \
     (1u << JZ4740_INTC_IRQ_UHC) | \
     (1u << JZ4740_INTC_IRQ_EMC) | \
     (1u << JZ4740_INTC_IRQ_I2C))

typedef void (*JZ4740INTCRefreshFn)(void *opaque);

typedef struct JZ4740INTCDiagnostics {
    uint32_t pending;
    uint32_t mask;
    uint32_t unmasked_pending;
    uint32_t output_level;
    uint32_t update_count;
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
} JZ4740INTCDiagnostics;

void jz4740_intc_set_irq(JZ4740INTCState *s, unsigned irq, bool level);
void jz4740_intc_set_pending_mask(JZ4740INTCState *s, uint32_t mask,
                                  uint32_t levels);
uint32_t jz4740_intc_pending(JZ4740INTCState *s);
uint32_t jz4740_intc_mask(JZ4740INTCState *s);
bool jz4740_intc_output_level(JZ4740INTCState *s);
void jz4740_intc_set_refresh(JZ4740INTCState *s,
                             JZ4740INTCRefreshFn refresh,
                             void *opaque);
void jz4740_intc_get_diagnostics(JZ4740INTCState *s,
                                 JZ4740INTCDiagnostics *diagnostics);

#endif
