/*
 * Ingenic JZ4740 GPIO controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_GPIO_JZ4740_GPIO_H
#define HW_GPIO_JZ4740_GPIO_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4740_GPIO "jz4740-gpio"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4740GPIOState, JZ4740_GPIO)

#define JZ4740_GPIO_NUM_PORTS 4u
#define JZ4740_GPIO_PINS_PER_PORT 32u

enum {
    JZ4740_GPIO_PORT_A = 0,
    JZ4740_GPIO_PORT_B = 1,
    JZ4740_GPIO_PORT_C = 2,
    JZ4740_GPIO_PORT_D = 3,
};

typedef struct JZ4740GPIODiagnostics {
    uint32_t input_level[JZ4740_GPIO_NUM_PORTS];
    uint32_t flag[JZ4740_GPIO_NUM_PORTS];
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_flag_offset;
    uint32_t last_flag_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    bool irq_level[JZ4740_GPIO_NUM_PORTS];
} JZ4740GPIODiagnostics;

typedef uint32_t (*JZ4740GPIOInputSampleCallback)(void *opaque,
                                                  unsigned port,
                                                  uint32_t level);
typedef void (*JZ4740GPIOTraceCallback)(void *opaque, uint32_t reason);

bool jz4740_gpio_set_input_level(JZ4740GPIOState *s, unsigned port,
                                 uint32_t mask, bool high,
                                 bool latch_flag);
void jz4740_gpio_raise_flag(JZ4740GPIOState *s, unsigned port,
                            uint32_t mask);
uint32_t jz4740_gpio_flag(JZ4740GPIOState *s, unsigned port);
void jz4740_gpio_get_diagnostics(JZ4740GPIOState *s,
                                 JZ4740GPIODiagnostics *diagnostics);
void jz4740_gpio_set_input_sample_callback(
    JZ4740GPIOState *s, JZ4740GPIOInputSampleCallback callback,
    void *opaque);
void jz4740_gpio_set_trace_callback(JZ4740GPIOState *s,
                                    JZ4740GPIOTraceCallback callback,
                                    void *opaque);

#endif
