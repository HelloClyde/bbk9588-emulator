/*
 * Ingenic JZ4730 watchdog timer.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/timer/jz4730_wdt.h"
#include "migration/vmstate.h"
#include "qemu/host-utils.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "system/runstate.h"

#define WDT_MMIO_SIZE       0x08u
#define WDT_CSR             0x00u
#define WDT_CNT             0x04u
#define WDT_CSR_ENABLE      0x10u
#define WDT_CLOCK_HZ        32768u

struct JZ4730WDTState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    QEMUTimer *timer;
    uint32_t counter;
    int64_t anchor_ns;
    bool enabled;
};

static void wdt_schedule(JZ4730WDTState *s)
{
    uint64_t ticks;
    int64_t deadline;

    if (!s->enabled) {
        timer_del(s->timer);
        return;
    }
    ticks = (uint64_t)UINT32_MAX - s->counter + 1u;
    deadline = s->anchor_ns + muldiv64_round_up(
        ticks, NANOSECONDS_PER_SECOND, WDT_CLOCK_HZ);
    timer_mod_ns(s->timer, deadline);
}

static void wdt_sync(JZ4730WDTState *s, int64_t now_ns)
{
    uint64_t elapsed;

    if (!s->enabled || now_ns <= s->anchor_ns) {
        return;
    }
    elapsed = muldiv64(now_ns - s->anchor_ns, WDT_CLOCK_HZ,
                       NANOSECONDS_PER_SECOND);
    if (!elapsed) {
        return;
    }
    s->counter += elapsed;
    s->anchor_ns += muldiv64(elapsed, NANOSECONDS_PER_SECOND,
                             WDT_CLOCK_HZ);
}

static void wdt_timer_cb(void *opaque)
{
    JZ4730WDTState *s = JZ4730_WDT(opaque);

    s->counter = 0;
    s->enabled = false;
    timer_del(s->timer);
    qemu_system_reset_request(SHUTDOWN_CAUSE_GUEST_RESET);
}

static uint64_t wdt_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4730WDTState *s = JZ4730_WDT(opaque);

    switch (offset & ~3u) {
    case WDT_CSR:
        return s->enabled ? WDT_CSR_ENABLE : 0;
    case WDT_CNT:
        wdt_sync(s, qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL));
        return s->counter;
    default:
        return 0;
    }
}

static void wdt_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    JZ4730WDTState *s = JZ4730_WDT(opaque);
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    switch (offset & ~3u) {
    case WDT_CSR:
        if (value & WDT_CSR_ENABLE) {
            wdt_sync(s, now_ns);
            s->enabled = true;
            s->anchor_ns = now_ns;
            wdt_schedule(s);
        }
        break;
    case WDT_CNT:
        wdt_sync(s, now_ns);
        s->counter = value;
        s->anchor_ns = now_ns;
        wdt_schedule(s);
        break;
    default:
        break;
    }
}

static const MemoryRegionOps wdt_ops = {
    .read = wdt_read,
    .write = wdt_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
        .unaligned = false,
    },
};

static void wdt_reset_hold(Object *obj, ResetType type)
{
    JZ4730WDTState *s = JZ4730_WDT(obj);

    s->counter = 0;
    s->anchor_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->enabled = false;
    timer_del(s->timer);
}

static int wdt_post_load(void *opaque, int version_id)
{
    JZ4730WDTState *s = opaque;

    wdt_schedule(s);
    return 0;
}

static const VMStateDescription vmstate_jz4730_wdt = {
    .name = TYPE_JZ4730_WDT,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = wdt_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(counter, JZ4730WDTState),
        VMSTATE_INT64(anchor_ns, JZ4730WDTState),
        VMSTATE_BOOL(enabled, JZ4730WDTState),
        VMSTATE_END_OF_LIST(),
    },
};

static void wdt_init(Object *obj)
{
    JZ4730WDTState *s = JZ4730_WDT(obj);

    memory_region_init_io(&s->iomem, obj, &wdt_ops, s, TYPE_JZ4730_WDT,
                          WDT_MMIO_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(obj), &s->iomem);
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, wdt_timer_cb, s);
}

static void wdt_finalize(Object *obj)
{
    JZ4730WDTState *s = JZ4730_WDT(obj);

    timer_free(s->timer);
}

static void wdt_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->vmsd = &vmstate_jz4730_wdt;
    rc->phases.hold = wdt_reset_hold;
}

static const TypeInfo wdt_info = {
    .name = TYPE_JZ4730_WDT,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4730WDTState),
    .instance_init = wdt_init,
    .instance_finalize = wdt_finalize,
    .class_init = wdt_class_init,
};

static void wdt_register_types(void)
{
    type_register_static(&wdt_info);
}

type_init(wdt_register_types)
