/*
 * Ingenic JZ4730 SSI controller with the BBK TSC2100 peripheral.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_SSI_JZ4730_SSI_H
#define HW_SSI_JZ4730_SSI_H

#include "hw/core/sysbus.h"

#define TYPE_JZ4730_SSI "jz4730-ssi"
OBJECT_DECLARE_SIMPLE_TYPE(JZ4730SSIState, JZ4730_SSI)

typedef void (*JZ4730TSC2100AudioConfigCallback)(
    void *opaque, uint32_t sample_rate, uint8_t sample_bits, bool muted,
    uint8_t left_attenuation, uint8_t right_attenuation);
typedef void (*JZ4730TSC2100PintdavCallback)(void *opaque, bool high);

void jz4730_ssi_set_touch(JZ4730SSIState *s, uint16_t raw_x,
                          uint16_t raw_y, bool down);
void jz4730_ssi_set_audio_config_callback(
    JZ4730SSIState *s, JZ4730TSC2100AudioConfigCallback callback,
    void *opaque);
void jz4730_ssi_set_pintdav_callback(
    JZ4730SSIState *s, JZ4730TSC2100PintdavCallback callback, void *opaque);

#endif
