/*
 * Ingenic JZ4730 operating-system timer.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/timer/jz4730_ost.h"
#include "migration/vmstate.h"
#include "qemu/host-utils.h"
#include "qemu/module.h"
#include "qemu/timer.h"

#define OST_MMIO_SIZE       0x1000u
#define OST_OTER            0x00u
#define OST_CHANNEL_BASE    0x10u
#define OST_CHANNEL_STRIDE  0x20u
#define OST_OTDR            0x00u
#define OST_OTCNT           0x04u
#define OST_OTCSR           0x08u
#define OST_OTCRD           0x0cu

#define OST_OTCSR_UF        0x0040u
#define OST_OTCSR_UIE       0x0020u
#define OST_OTCSR_CKC_MASK  0x0007u
#define OST_OTCSR_RW_MASK   (OST_OTCSR_UF | OST_OTCSR_UIE | OST_OTCSR_CKC_MASK)

#define OST_PCLK_HZ         84000000u
#define OST_RTCCLK_HZ       32768u
#define OST_EXCLK_HZ        12000000u

struct JZ4730OSTState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq[JZ4730_OST_CHANNELS];
    QEMUTimer *timer;
    uint32_t data[JZ4730_OST_CHANNELS];
    uint32_t counter[JZ4730_OST_CHANNELS];
    uint32_t control[JZ4730_OST_CHANNELS];
    uint32_t read_latch[JZ4730_OST_CHANNELS];
    int64_t anchor_ns[JZ4730_OST_CHANNELS];
    uint8_t enabled;
};

static uint32_t ost_clock_hz(uint32_t control)
{
    switch (control & OST_OTCSR_CKC_MASK) {
    case 0:
        return OST_PCLK_HZ / 4u;
    case 1:
        return OST_PCLK_HZ / 16u;
    case 2:
        return OST_PCLK_HZ / 64u;
    case 3:
        return OST_PCLK_HZ / 256u;
    case 4:
        return OST_RTCCLK_HZ;
    case 5:
        return OST_EXCLK_HZ;
    default:
        return 0;
    }
}

static void ost_update_irq(JZ4730OSTState *s, unsigned channel)
{
    bool level = (s->control[channel] &
                  (OST_OTCSR_UF | OST_OTCSR_UIE)) ==
                 (OST_OTCSR_UF | OST_OTCSR_UIE);

    qemu_set_irq(s->irq[channel], level);
}

static bool ost_running(JZ4730OSTState *s, unsigned channel)
{
    return (s->enabled & (1u << channel)) &&
           ost_clock_hz(s->control[channel]) != 0;
}

static void ost_sync_channel(JZ4730OSTState *s, unsigned channel,
                             int64_t now_ns)
{
    uint32_t hz;
    uint64_t elapsed;
    uint64_t first_period;
    uint64_t reload_period;
    uint64_t remaining;

    if (!ost_running(s, channel) || now_ns <= s->anchor_ns[channel]) {
        return;
    }
    hz = ost_clock_hz(s->control[channel]);
    elapsed = muldiv64(now_ns - s->anchor_ns[channel], hz,
                       NANOSECONDS_PER_SECOND);
    if (!elapsed) {
        return;
    }
    s->anchor_ns[channel] = now_ns;
    first_period = (uint64_t)s->counter[channel] + 1u;
    if (elapsed < first_period) {
        s->counter[channel] -= elapsed;
        return;
    }

    s->control[channel] |= OST_OTCSR_UF;
    remaining = elapsed - first_period;
    reload_period = (uint64_t)s->data[channel] + 1u;
    remaining %= reload_period;
    s->counter[channel] = s->data[channel] - remaining;
    ost_update_irq(s, channel);
}

static void ost_schedule(JZ4730OSTState *s)
{
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    int64_t earliest = -1;

    for (unsigned channel = 0; channel < JZ4730_OST_CHANNELS; channel++) {
        uint32_t hz;
        uint64_t ticks;
        int64_t deadline;

        ost_sync_channel(s, channel, now_ns);
        if (!ost_running(s, channel)) {
            continue;
        }
        hz = ost_clock_hz(s->control[channel]);
        ticks = (uint64_t)s->counter[channel] + 1u;
        deadline = now_ns + muldiv64_round_up(
            ticks, NANOSECONDS_PER_SECOND, hz);
        if (earliest < 0 || deadline < earliest) {
            earliest = deadline;
        }
    }
    if (earliest < 0) {
        timer_del(s->timer);
    } else {
        timer_mod_ns(s->timer, earliest);
    }
}

static void ost_timer_cb(void *opaque)
{
    ost_schedule(JZ4730_OST(opaque));
}

static bool ost_decode_channel(hwaddr offset, unsigned *channel,
                               hwaddr *reg)
{
    hwaddr relative;

    if (offset < OST_CHANNEL_BASE) {
        return false;
    }
    relative = offset - OST_CHANNEL_BASE;
    *channel = relative / OST_CHANNEL_STRIDE;
    *reg = relative % OST_CHANNEL_STRIDE;
    return *channel < JZ4730_OST_CHANNELS && *reg <= OST_OTCRD;
}

static uint64_t ost_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4730OSTState *s = JZ4730_OST(opaque);
    unsigned channel;
    hwaddr reg;
    uint32_t value = 0;

    if (offset == OST_OTER) {
        return s->enabled;
    }
    if (!ost_decode_channel(offset & ~3u, &channel, &reg)) {
        return 0;
    }
    ost_sync_channel(s, channel, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
    switch (reg) {
    case OST_OTDR:
        value = s->data[channel];
        break;
    case OST_OTCNT:
        value = s->counter[channel];
        s->read_latch[channel] = value;
        break;
    case OST_OTCSR:
        value = s->control[channel];
        break;
    case OST_OTCRD:
        value = s->read_latch[channel];
        break;
    default:
        break;
    }
    value >>= (offset & 3u) * 8u;
    if (size == 1) {
        return value & 0xffu;
    }
    if (size == 2) {
        return value & 0xffffu;
    }
    return value;
}

static void ost_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    JZ4730OSTState *s = JZ4730_OST(opaque);
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    unsigned channel;
    hwaddr reg;

    if (offset == OST_OTER) {
        for (channel = 0; channel < JZ4730_OST_CHANNELS; channel++) {
            ost_sync_channel(s, channel, now_ns);
        }
        s->enabled = value & 0x07u;
        for (channel = 0; channel < JZ4730_OST_CHANNELS; channel++) {
            s->anchor_ns[channel] = now_ns;
        }
        ost_schedule(s);
        return;
    }
    if (!ost_decode_channel(offset & ~3u, &channel, &reg)) {
        return;
    }
    ost_sync_channel(s, channel, now_ns);
    switch (reg) {
    case OST_OTDR:
        s->data[channel] = value;
        s->counter[channel] = value;
        s->read_latch[channel] = value;
        s->anchor_ns[channel] = now_ns;
        break;
    case OST_OTCNT:
        s->counter[channel] = value;
        s->read_latch[channel] = value;
        s->anchor_ns[channel] = now_ns;
        break;
    case OST_OTCSR: {
        uint32_t old_uf = s->control[channel] & OST_OTCSR_UF;
        uint32_t written = value & OST_OTCSR_RW_MASK;

        s->control[channel] = written & ~OST_OTCSR_UF;
        if (old_uf && (written & OST_OTCSR_UF)) {
            s->control[channel] |= OST_OTCSR_UF;
        }
        s->anchor_ns[channel] = now_ns;
        ost_update_irq(s, channel);
        break;
    }
    case OST_OTCRD:
    default:
        break;
    }
    ost_schedule(s);
}

static const MemoryRegionOps ost_ops = {
    .read = ost_read,
    .write = ost_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
        .unaligned = false,
    },
};

static void ost_reset_hold(Object *obj, ResetType type)
{
    JZ4730OSTState *s = JZ4730_OST(obj);
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    s->enabled = 0;
    for (unsigned channel = 0; channel < JZ4730_OST_CHANNELS; channel++) {
        s->data[channel] = UINT32_MAX;
        s->counter[channel] = UINT32_MAX;
        s->control[channel] = 0;
        s->read_latch[channel] = UINT32_MAX;
        s->anchor_ns[channel] = now_ns;
        qemu_set_irq(s->irq[channel], 0);
    }
    timer_del(s->timer);
}

static int ost_post_load(void *opaque, int version_id)
{
    JZ4730OSTState *s = opaque;

    for (unsigned channel = 0; channel < JZ4730_OST_CHANNELS; channel++) {
        ost_update_irq(s, channel);
    }
    ost_schedule(s);
    return 0;
}

static const VMStateDescription vmstate_jz4730_ost = {
    .name = TYPE_JZ4730_OST,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = ost_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(data, JZ4730OSTState, JZ4730_OST_CHANNELS),
        VMSTATE_UINT32_ARRAY(counter, JZ4730OSTState, JZ4730_OST_CHANNELS),
        VMSTATE_UINT32_ARRAY(control, JZ4730OSTState, JZ4730_OST_CHANNELS),
        VMSTATE_UINT32_ARRAY(read_latch, JZ4730OSTState,
                             JZ4730_OST_CHANNELS),
        VMSTATE_INT64_ARRAY(anchor_ns, JZ4730OSTState, JZ4730_OST_CHANNELS),
        VMSTATE_UINT8(enabled, JZ4730OSTState),
        VMSTATE_END_OF_LIST(),
    },
};

static void ost_init(Object *obj)
{
    JZ4730OSTState *s = JZ4730_OST(obj);

    memory_region_init_io(&s->iomem, obj, &ost_ops, s, TYPE_JZ4730_OST,
                          OST_MMIO_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    for (unsigned channel = 0; channel < JZ4730_OST_CHANNELS; channel++) {
        sysbus_init_irq(SYS_BUS_DEVICE(obj), &s->irq[channel]);
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, ost_timer_cb, s);
}

static void ost_finalize(Object *obj)
{
    JZ4730OSTState *s = JZ4730_OST(obj);

    timer_free(s->timer);
}

static void ost_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->vmsd = &vmstate_jz4730_ost;
    rc->phases.hold = ost_reset_hold;
}

static const TypeInfo ost_info = {
    .name = TYPE_JZ4730_OST,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4730OSTState),
    .instance_init = ost_init,
    .instance_finalize = ost_finalize,
    .class_init = ost_class_init,
};

static void ost_register_types(void)
{
    type_register_static(&ost_info);
}

type_init(ost_register_types)
