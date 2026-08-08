/*
 * Ingenic JZ4730 I2C controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_I2C_JZ4730_I2C_H
#define HW_I2C_JZ4730_I2C_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4730_I2C "jz4730-i2c"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4730I2CState, JZ4730_I2C)

#endif
