/*
 * Ingenic JZ4740 interrupt controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/intc/jz4740_intc.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define JZ4740_INTC_MMIO_SIZE 0x1000u
#define JZ4740_INTC_ICSR      0x00u
#define JZ4740_INTC_ICMR      0x04u
#define JZ4740_INTC_ICMSR     0x08u
#define JZ4740_INTC_ICMCR     0x0cu
#define JZ4740_INTC_ICPR      0x10u
#define JZ4740_INTC_MASK_RESET 0xffffffffu

struct JZ4740INTCState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq output;
    uint32_t pending;
    uint32_t mask;
    uint32_t unmasked_pending;
    uint32_t update_count;
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    bool output_level;

    JZ4740INTCRefreshFn refresh;
    void *refresh_opaque;
};

static void jz4740_intc_update(JZ4740INTCState *s)
{
    bool level;

    s->pending &= JZ4740_INTC_SOURCE_MASK;
    s->unmasked_pending = s->pending & ~s->mask;
    level = s->unmasked_pending != 0;
    s->update_count++;
    if (level != s->output_level) {
        s->output_level = level;
        qemu_set_irq(s->output, level);
    }
}

void jz4740_intc_set_irq(JZ4740INTCState *s, unsigned irq, bool level)
{
    uint32_t bit;

    if (!s || irq >= JZ4740_INTC_NUM_IRQS) {
        return;
    }
    bit = 1u << irq;
    if (!(JZ4740_INTC_SOURCE_MASK & bit)) {
        return;
    }
    if (level) {
        s->pending |= bit;
    } else {
        s->pending &= ~bit;
    }
    jz4740_intc_update(s);
}

void jz4740_intc_set_pending_mask(JZ4740INTCState *s, uint32_t mask,
                                  uint32_t levels)
{
    if (!s) {
        return;
    }
    mask &= JZ4740_INTC_SOURCE_MASK;
    s->pending = (s->pending & ~mask) | (levels & mask);
    jz4740_intc_update(s);
}

uint32_t jz4740_intc_pending(JZ4740INTCState *s)
{
    return s ? s->pending : 0;
}

uint32_t jz4740_intc_mask(JZ4740INTCState *s)
{
    return s ? s->mask : JZ4740_INTC_MASK_RESET;
}

bool jz4740_intc_output_level(JZ4740INTCState *s)
{
    return s && s->output_level;
}

void jz4740_intc_set_refresh(JZ4740INTCState *s,
                             JZ4740INTCRefreshFn refresh,
                             void *opaque)
{
    if (!s) {
        return;
    }
    s->refresh = refresh;
    s->refresh_opaque = opaque;
}

void jz4740_intc_get_diagnostics(JZ4740INTCState *s,
                                 JZ4740INTCDiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    diagnostics->pending = s->pending;
    diagnostics->mask = s->mask;
    diagnostics->unmasked_pending = s->unmasked_pending;
    diagnostics->output_level = s->output_level ? 1u : 0u;
    diagnostics->update_count = s->update_count;
    diagnostics->last_read_offset = s->last_read_offset;
    diagnostics->last_read_value = s->last_read_value;
    diagnostics->last_write_offset = s->last_write_offset;
    diagnostics->last_write_value = s->last_write_value;
}

static void jz4740_intc_input(void *opaque, int irq, int level)
{
    jz4740_intc_set_irq(JZ4740_INTC(opaque), irq, level != 0);
}

static uint64_t jz4740_intc_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740INTCState *s = JZ4740_INTC(opaque);
    hwaddr reg = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t value;

    if (s->refresh) {
        s->refresh(s->refresh_opaque);
    }
    jz4740_intc_update(s);
    switch (reg) {
    case JZ4740_INTC_ICSR:
        value = s->pending;
        break;
    case JZ4740_INTC_ICMR:
        value = s->mask;
        break;
    case JZ4740_INTC_ICPR:
        value = s->unmasked_pending;
        break;
    default:
        value = 0;
        break;
    }
    s->last_read_offset = reg;
    s->last_read_value = value;
    value >>= shift;
    if (size == 1) {
        return value & 0xffu;
    }
    if (size == 2) {
        return value & 0xffffu;
    }
    return value;
}

static void jz4740_intc_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    JZ4740INTCState *s = JZ4740_INTC(opaque);
    hwaddr reg = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t lane_mask;
    uint32_t lane_value;

    if (size == 1) {
        lane_mask = 0xffu << shift;
    } else if (size == 2) {
        lane_mask = 0xffffu << shift;
    } else {
        lane_mask = 0xffffffffu;
        shift = 0;
    }
    lane_value = ((uint32_t)value << shift) & lane_mask;
    s->last_write_offset = reg;
    s->last_write_value = lane_value;

    switch (reg) {
    case JZ4740_INTC_ICMR:
        s->mask = (s->mask & ~lane_mask) | lane_value;
        break;
    case JZ4740_INTC_ICMSR:
        s->mask |= lane_value;
        break;
    case JZ4740_INTC_ICMCR:
        s->mask &= ~lane_value;
        break;
    case JZ4740_INTC_ICSR:
    case JZ4740_INTC_ICPR:
    default:
        break;
    }
    jz4740_intc_update(s);
}

static const MemoryRegionOps jz4740_intc_ops = {
    .read = jz4740_intc_read,
    .write = jz4740_intc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
        .unaligned = false,
    },
};

static void jz4740_intc_reset_hold(Object *obj, ResetType type)
{
    JZ4740INTCState *s = JZ4740_INTC(obj);

    s->pending = 0;
    s->mask = JZ4740_INTC_MASK_RESET;
    s->unmasked_pending = 0;
    s->update_count = 0;
    s->last_read_offset = 0;
    s->last_read_value = 0;
    s->last_write_offset = 0;
    s->last_write_value = 0;
    s->output_level = false;
    qemu_set_irq(s->output, 0);
}

static int jz4740_intc_post_load(void *opaque, int version_id)
{
    JZ4740INTCState *s = opaque;

    s->output_level = false;
    jz4740_intc_update(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_intc = {
    .name = TYPE_JZ4740_INTC,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = jz4740_intc_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(pending, JZ4740INTCState),
        VMSTATE_UINT32(mask, JZ4740INTCState),
        VMSTATE_END_OF_LIST()
    },
};

static void jz4740_intc_init(Object *obj)
{
    JZ4740INTCState *s = JZ4740_INTC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &jz4740_intc_ops, s,
                          TYPE_JZ4740_INTC, JZ4740_INTC_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->output);
    qdev_init_gpio_in(DEVICE(obj), jz4740_intc_input,
                      JZ4740_INTC_NUM_IRQS);
}

static void jz4740_intc_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_intc;
    rc->phases.hold = jz4740_intc_reset_hold;
}

static const TypeInfo jz4740_intc_type_info = {
    .name = TYPE_JZ4740_INTC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740INTCState),
    .instance_init = jz4740_intc_init,
    .class_init = jz4740_intc_class_init,
};

static void jz4740_intc_register_types(void)
{
    type_register_static(&jz4740_intc_type_info);
}

type_init(jz4740_intc_register_types)
