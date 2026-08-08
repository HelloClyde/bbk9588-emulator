/*
 * Ingenic JZ4730 I2C controller.
 *
 * The board firmware uses polling transfers to read low-speed management
 * devices.  This model implements the controller handshake and an attached
 * zero-filled responder; individual I2C peripherals can replace the responder
 * once their BBK board wiring is known.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/i2c/jz4730_i2c.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define JZ4730_I2C_MMIO_SIZE 0x1000u
#define I2C_DR               0x00u
#define I2C_CR               0x04u
#define I2C_SR               0x08u
#define I2C_GR               0x0cu

#define I2C_CR_IEN           0x10u
#define I2C_CR_STA           0x08u
#define I2C_CR_STO           0x04u
#define I2C_CR_AC            0x02u
#define I2C_CR_I2CE          0x01u

#define I2C_SR_BUSY          0x08u
#define I2C_SR_TEND          0x04u
#define I2C_SR_DRF           0x02u

struct JZ4730I2CState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint8_t data;
    uint8_t control;
    uint8_t status;
    uint16_t divider;
    bool read_transfer;
};

static void jz4730_i2c_update_irq(JZ4730I2CState *s)
{
    qemu_set_irq(s->irq,
                 (s->control & I2C_CR_IEN) && (s->status & I2C_SR_TEND));
}

static uint64_t jz4730_i2c_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4730I2CState *s = JZ4730_I2C(opaque);

    switch (offset) {
    case I2C_DR:
        return s->data;
    case I2C_CR:
        return s->control;
    case I2C_SR:
        return s->status;
    case I2C_GR:
        return s->divider;
    default:
        return 0;
    }
}

static void jz4730_i2c_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    JZ4730I2CState *s = JZ4730_I2C(opaque);

    switch (offset) {
    case I2C_DR:
        s->data = value & 0xffu;
        break;
    case I2C_CR: {
        uint8_t command = value & 0x1fu;

        s->control = command & ~(I2C_CR_STA | I2C_CR_STO);
        if (command & I2C_CR_STA) {
            s->read_transfer = (s->data & 1u) != 0;
            s->status = I2C_SR_BUSY | I2C_SR_TEND |
                        (s->read_transfer ? I2C_SR_DRF : 0);
            if (s->read_transfer) {
                s->data = 0;
            }
        }
        if (command & I2C_CR_STO) {
            s->status = I2C_SR_TEND;
            s->read_transfer = false;
        }
        if (!(command & I2C_CR_I2CE)) {
            s->status = I2C_SR_TEND;
            s->read_transfer = false;
        }
        break;
    }
    case I2C_SR:
        if (s->read_transfer) {
            s->data = 0;
            s->status = I2C_SR_BUSY | I2C_SR_TEND | I2C_SR_DRF;
        } else {
            s->status = (s->status & I2C_SR_BUSY) | I2C_SR_TEND;
        }
        break;
    case I2C_GR:
        s->divider = value & 0xffffu;
        break;
    default:
        break;
    }
    jz4730_i2c_update_irq(s);
}

static const MemoryRegionOps jz4730_i2c_ops = {
    .read = jz4730_i2c_read,
    .write = jz4730_i2c_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 2,
        .unaligned = false,
    },
};

static void jz4730_i2c_reset_hold(Object *obj, ResetType type)
{
    JZ4730I2CState *s = JZ4730_I2C(obj);

    s->data = 0;
    s->control = 0;
    s->status = I2C_SR_TEND;
    s->divider = 0;
    s->read_transfer = false;
    qemu_set_irq(s->irq, 0);
}

static int jz4730_i2c_post_load(void *opaque, int version_id)
{
    jz4730_i2c_update_irq(opaque);
    return 0;
}

static const VMStateDescription vmstate_jz4730_i2c = {
    .name = TYPE_JZ4730_I2C,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = jz4730_i2c_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT8(data, JZ4730I2CState),
        VMSTATE_UINT8(control, JZ4730I2CState),
        VMSTATE_UINT8(status, JZ4730I2CState),
        VMSTATE_UINT16(divider, JZ4730I2CState),
        VMSTATE_BOOL(read_transfer, JZ4730I2CState),
        VMSTATE_END_OF_LIST(),
    },
};

static void jz4730_i2c_init(Object *obj)
{
    JZ4730I2CState *s = JZ4730_I2C(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &jz4730_i2c_ops, s,
                          TYPE_JZ4730_I2C, JZ4730_I2C_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void jz4730_i2c_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->vmsd = &vmstate_jz4730_i2c;
    rc->phases.hold = jz4730_i2c_reset_hold;
}

static const TypeInfo jz4730_i2c_info = {
    .name = TYPE_JZ4730_I2C,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4730I2CState),
    .instance_init = jz4730_i2c_init,
    .class_init = jz4730_i2c_class_init,
};

static void jz4730_i2c_register_types(void)
{
    type_register_static(&jz4730_i2c_info);
}

type_init(jz4730_i2c_register_types)
