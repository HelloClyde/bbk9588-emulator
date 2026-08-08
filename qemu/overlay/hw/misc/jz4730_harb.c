/*
 * Ingenic JZ4730 AHB bus arbiter.
 *
 * The programming manual exposes seven software-visible arbitration
 * registers.  Arbitration itself is not observable in QEMU's memory model,
 * but the registers must retain their values because the boot firmware uses
 * read/modify/write sequences during early board setup.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/misc/jz4730_harb.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define JZ4730_HARB_MMIO_SIZE 0x10000u
#define JZ4730_HARB_REGS      10u

struct JZ4730HARBState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    uint32_t regs[JZ4730_HARB_REGS];
};

static bool jz4730_harb_reg_index(hwaddr offset, unsigned *index)
{
    if (offset == 0x00u) {
        *index = 0;
        return true;
    }
    if (offset >= 0x10u && offset <= 0x24u && !(offset & 3u)) {
        *index = 1u + (offset - 0x10u) / 4u;
        return true;
    }
    return false;
}

static uint64_t jz4730_harb_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4730HARBState *s = JZ4730_HARB(opaque);
    hwaddr reg = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    unsigned index;
    uint32_t value = 0;

    if (jz4730_harb_reg_index(reg, &index)) {
        value = s->regs[index];
    }
    value >>= shift;
    if (size == 1) {
        return value & 0xffu;
    }
    if (size == 2) {
        return value & 0xffffu;
    }
    return value;
}

static void jz4730_harb_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    JZ4730HARBState *s = JZ4730_HARB(opaque);
    hwaddr reg = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    unsigned index;
    uint32_t mask;

    if (!jz4730_harb_reg_index(reg, &index)) {
        return;
    }
    if (size == 1) {
        mask = 0xffu << shift;
    } else if (size == 2) {
        mask = 0xffffu << shift;
    } else {
        mask = UINT32_MAX;
        shift = 0;
    }
    s->regs[index] = (s->regs[index] & ~mask) |
                     (((uint32_t)value << shift) & mask);
}

static const MemoryRegionOps jz4730_harb_ops = {
    .read = jz4730_harb_read,
    .write = jz4730_harb_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
        .unaligned = false,
    },
};

static void jz4730_harb_reset_hold(Object *obj, ResetType type)
{
    JZ4730HARBState *s = JZ4730_HARB(obj);

    memset(s->regs, 0, sizeof(s->regs));
}

static const VMStateDescription vmstate_jz4730_harb = {
    .name = TYPE_JZ4730_HARB,
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4730HARBState, JZ4730_HARB_REGS),
        VMSTATE_END_OF_LIST(),
    },
};

static void jz4730_harb_init(Object *obj)
{
    JZ4730HARBState *s = JZ4730_HARB(obj);

    memory_region_init_io(&s->iomem, obj, &jz4730_harb_ops, s,
                          TYPE_JZ4730_HARB, JZ4730_HARB_MMIO_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
}

static void jz4730_harb_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->vmsd = &vmstate_jz4730_harb;
    rc->phases.hold = jz4730_harb_reset_hold;
}

static const TypeInfo jz4730_harb_info = {
    .name = TYPE_JZ4730_HARB,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4730HARBState),
    .instance_init = jz4730_harb_init,
    .class_init = jz4730_harb_class_init,
};

static void jz4730_harb_register_types(void)
{
    type_register_static(&jz4730_harb_info);
}

type_init(jz4730_harb_register_types)
