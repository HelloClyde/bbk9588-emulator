/*
 * Ingenic JZ4730 pulse-width modulator.
 *
 * The BBK firmware uses PWM1 for the LCD backlight.  QEMU does not expose a
 * physical PWM pin, but the guest-visible control, period and duty registers
 * still need their documented widths, masks and reset values.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/misc/jz4730_pwm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define JZ4730_PWM_MMIO_SIZE 0x2000u
#define PWM_CHANNEL_STRIDE   0x1000u
#define PWM_CTR              0x00u
#define PWM_PER              0x04u
#define PWM_DUT              0x08u

struct JZ4730PWMState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    uint8_t control[JZ4730_PWM_CHANNELS];
    uint16_t period[JZ4730_PWM_CHANNELS];
    uint16_t duty[JZ4730_PWM_CHANNELS];
};

static bool jz4730_pwm_decode(hwaddr offset, unsigned *channel,
                              hwaddr *reg)
{
    *channel = offset / PWM_CHANNEL_STRIDE;
    *reg = offset % PWM_CHANNEL_STRIDE;
    return *channel < JZ4730_PWM_CHANNELS;
}

static uint64_t jz4730_pwm_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4730PWMState *s = JZ4730_PWM(opaque);
    unsigned channel;
    hwaddr reg;

    if (!jz4730_pwm_decode(offset, &channel, &reg)) {
        return 0;
    }
    switch (reg) {
    case PWM_CTR:
        return s->control[channel];
    case PWM_PER:
        return s->period[channel];
    case PWM_DUT:
        return s->duty[channel];
    default:
        return 0;
    }
}

static void jz4730_pwm_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    JZ4730PWMState *s = JZ4730_PWM(opaque);
    unsigned channel;
    hwaddr reg;

    if (!jz4730_pwm_decode(offset, &channel, &reg)) {
        return;
    }
    switch (reg) {
    case PWM_CTR:
        s->control[channel] = value & 0xffu;
        break;
    case PWM_PER:
        s->period[channel] = value & 0x03ffu;
        break;
    case PWM_DUT:
        s->duty[channel] = value & 0x07ffu;
        break;
    default:
        break;
    }
}

static const MemoryRegionOps jz4730_pwm_ops = {
    .read = jz4730_pwm_read,
    .write = jz4730_pwm_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        /* C200 uses word accesses even though the registers are 8/16-bit. */
        .max_access_size = 4,
        .unaligned = false,
    },
};

static void jz4730_pwm_reset_hold(Object *obj, ResetType type)
{
    JZ4730PWMState *s = JZ4730_PWM(obj);

    memset(s->control, 0, sizeof(s->control));
    for (unsigned channel = 0; channel < JZ4730_PWM_CHANNELS; channel++) {
        s->period[channel] = 4;
        s->duty[channel] = 0;
    }
}

static const VMStateDescription vmstate_jz4730_pwm = {
    .name = TYPE_JZ4730_PWM,
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT8_ARRAY(control, JZ4730PWMState, JZ4730_PWM_CHANNELS),
        VMSTATE_UINT16_ARRAY(period, JZ4730PWMState, JZ4730_PWM_CHANNELS),
        VMSTATE_UINT16_ARRAY(duty, JZ4730PWMState, JZ4730_PWM_CHANNELS),
        VMSTATE_END_OF_LIST(),
    },
};

static void jz4730_pwm_init(Object *obj)
{
    JZ4730PWMState *s = JZ4730_PWM(obj);

    memory_region_init_io(&s->iomem, obj, &jz4730_pwm_ops, s,
                          TYPE_JZ4730_PWM, JZ4730_PWM_MMIO_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
}

static void jz4730_pwm_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->vmsd = &vmstate_jz4730_pwm;
    rc->phases.hold = jz4730_pwm_reset_hold;
}

static const TypeInfo jz4730_pwm_info = {
    .name = TYPE_JZ4730_PWM,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4730PWMState),
    .instance_init = jz4730_pwm_init,
    .class_init = jz4730_pwm_class_init,
};

static void jz4730_pwm_register_types(void)
{
    type_register_static(&jz4730_pwm_info);
}

type_init(jz4730_pwm_register_types)
