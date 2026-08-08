/*
 * Ingenic JZ4730 operating-system timer.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_TIMER_JZ4730_OST_H
#define HW_TIMER_JZ4730_OST_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4730_OST "jz4730-ost"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4730OSTState, JZ4730_OST)

#define JZ4730_OST_CHANNELS 3u

#endif
