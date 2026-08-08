/*
 * Ingenic JZ4730 pulse-width modulator.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_MISC_JZ4730_PWM_H
#define HW_MISC_JZ4730_PWM_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4730_PWM "jz4730-pwm"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4730PWMState, JZ4730_PWM)

#define JZ4730_PWM_CHANNELS 2u

#endif
