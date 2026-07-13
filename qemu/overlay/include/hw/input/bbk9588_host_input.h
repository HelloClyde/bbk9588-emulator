/*
 * BBK 9588 host key/touch input bridge.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_INPUT_BBK9588_HOST_INPUT_H
#define HW_INPUT_BBK9588_HOST_INPUT_H

#include "hw/core/qdev.h"

#define TYPE_BBK9588_HOST_INPUT "bbk9588-host-input"
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588HostInputState, BBK9588_HOST_INPUT)

typedef void (*Bbk9588HostKeyCallback)(void *opaque, uint32_t key_code,
                                       bool down);
typedef void (*Bbk9588HostTouchCallback)(void *opaque, uint16_t raw_x,
                                         uint16_t raw_y, uint16_t x,
                                         uint16_t y, bool down);

void bbk9588_host_input_configure(Bbk9588HostInputState *s,
                                  const char *input_chardev,
                                  Bbk9588HostKeyCallback key_callback,
                                  Bbk9588HostTouchCallback touch_callback,
                                  void *opaque);

#endif
