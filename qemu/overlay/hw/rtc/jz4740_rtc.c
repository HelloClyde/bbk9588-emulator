/*
 * Ingenic JZ4740 real-time clock.
 *
 * Models the seconds counter, alarm, 1 Hz status, hibernate registers and
 * interrupt output using QEMU's configured RTC clock.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/rtc/jz4740_rtc.h"
#include "migration/vmstate.h"
#include "qemu/cutils.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "system/rtc.h"
#include "system/system.h"

#define RTC_MMIO_SIZE              0x1000u

#define RTC_RTCCR                  0x00u
#define RTC_RTCSR                  0x04u
#define RTC_RTCSAR                 0x08u
#define RTC_RTCGR                  0x0cu
#define RTC_HCR                    0x20u
#define RTC_HWFCR                  0x24u
#define RTC_HRCR                   0x28u
#define RTC_HWCR                   0x2cu
#define RTC_HWRSR                  0x30u
#define RTC_HSPR                   0x34u

#define RTC_MAGIC                  0x87654321u
#define RTC_RTCCR_RESET            0x00000081u
#define RTC_RTCCR_WRDY             0x00000080u
#define RTC_RTCCR_1HZ              0x00000040u
#define RTC_RTCCR_1HZIE            0x00000020u
#define RTC_RTCCR_AF               0x00000010u
#define RTC_RTCCR_AIE              0x00000008u
#define RTC_RTCCR_AE               0x00000004u
#define RTC_RTCCR_RTCE             0x00000001u
#define RTC_RTCCR_RW_MASK          \
    (RTC_RTCCR_1HZIE | RTC_RTCCR_AIE | RTC_RTCCR_AE | RTC_RTCCR_RTCE)
#define RTC_RTCGR_RW_MASK          0x83ffffffu
#define RTC_RTCGR_LOCK             0x80000000u
#define RTC_HCR_PD                 0x00000001u
#define RTC_HWFCR_MASK             0x0000ffe0u
#define RTC_HRCR_MASK              0x00000fe0u
#define RTC_HWCR_EALM              0x00000001u
#define RTC_HWRSR_MASK             0x00000033u
#define RTC_HWRSR_HR               0x00000020u
#define RTC_HWRSR_PPR              0x00000010u
#define RTC_HWRSR_PIN              0x00000002u
#define RTC_HWRSR_ALM              0x00000001u

struct JZ4740RTCState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    QEMUTimer *timer;
    uint32_t control;
    uint32_t base_seconds;
    int64_t base_ns;
    uint32_t regulator;
    uint32_t alarm_seconds;
    uint32_t one_hz_latched_seconds;
    bool alarm_latched;
    uint32_t hcr;
    uint32_t hwfcr;
    uint32_t hrcr;
    uint32_t hwcr;
    uint32_t hwrsr;
    uint32_t scratch;
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    bool irq_level;
    bool hibernate_wakeup;
    JZ4740RTCPowerDownCallback power_down_callback;
    void *power_down_opaque;
};

static uint32_t rtc_host_seconds(void)
{
    struct tm tm;
    time_t seconds;

    qemu_get_timedate(&tm, 0);
    seconds = mktimegm(&tm);
    return seconds < 0 ? 0 : (uint32_t)seconds;
}

uint32_t jz4740_rtc_seconds(JZ4740RTCState *s)
{
    int64_t now_ns;
    uint64_t elapsed = 0;

    if (!s) {
        return 0;
    }
    if (!(s->control & RTC_RTCCR_RTCE)) {
        return s->base_seconds;
    }
    now_ns = qemu_clock_get_ns(rtc_clock);
    if (now_ns > s->base_ns) {
        elapsed = (uint64_t)(now_ns - s->base_ns) /
                  NANOSECONDS_PER_SECOND;
    }
    return s->base_seconds + (uint32_t)elapsed;
}

static uint32_t rtc_latch_flags(JZ4740RTCState *s)
{
    uint32_t control = s->control;
    uint32_t seconds = jz4740_rtc_seconds(s);

    if ((control & RTC_RTCCR_RTCE) &&
        seconds != s->one_hz_latched_seconds) {
        control |= RTC_RTCCR_1HZ;
        s->one_hz_latched_seconds = seconds;
    }
    if ((control & (RTC_RTCCR_RTCE | RTC_RTCCR_AE)) ==
        (RTC_RTCCR_RTCE | RTC_RTCCR_AE) &&
        seconds >= s->alarm_seconds && !s->alarm_latched) {
        control |= RTC_RTCCR_AF;
        s->alarm_latched = true;
        if ((s->hcr & RTC_HCR_PD) && (s->hwcr & RTC_HWCR_EALM)) {
            s->hwrsr |= RTC_HWRSR_ALM;
            s->hcr &= ~RTC_HCR_PD;
        }
    }
    s->control = control;
    return control;
}

static bool rtc_irq_pending(JZ4740RTCState *s)
{
    uint32_t control = rtc_latch_flags(s);

    return ((control & (RTC_RTCCR_1HZ | RTC_RTCCR_1HZIE)) ==
            (RTC_RTCCR_1HZ | RTC_RTCCR_1HZIE)) ||
           ((control & (RTC_RTCCR_AF | RTC_RTCCR_AIE)) ==
            (RTC_RTCCR_AF | RTC_RTCCR_AIE));
}

static void rtc_update_irq(JZ4740RTCState *s)
{
    s->irq_level = rtc_irq_pending(s);
    qemu_set_irq(s->irq, s->irq_level);
}

static void rtc_schedule(JZ4740RTCState *s)
{
    uint32_t control = s->control;
    uint32_t seconds;
    uint64_t elapsed;
    int64_t now_ns;
    int64_t next_ns = 0;

    if (!(control & RTC_RTCCR_RTCE)) {
        timer_del(s->timer);
        return;
    }
    now_ns = qemu_clock_get_ns(rtc_clock);
    seconds = jz4740_rtc_seconds(s);
    if ((control & RTC_RTCCR_1HZIE) && !(control & RTC_RTCCR_1HZ)) {
        elapsed = now_ns > s->base_ns ?
            (uint64_t)(now_ns - s->base_ns) / NANOSECONDS_PER_SECOND : 0;
        next_ns = s->base_ns +
                  (int64_t)((elapsed + 1) * NANOSECONDS_PER_SECOND);
        if (next_ns <= now_ns) {
            next_ns = now_ns + NANOSECONDS_PER_SECOND;
        }
    }
    if ((control & RTC_RTCCR_AE) && !s->alarm_latched) {
        int64_t alarm_ns = now_ns;

        if (seconds < s->alarm_seconds) {
            alarm_ns += (int64_t)(s->alarm_seconds - seconds) *
                        NANOSECONDS_PER_SECOND;
        }
        if (next_ns == 0 || alarm_ns < next_ns) {
            next_ns = alarm_ns;
        }
    }
    if (next_ns == 0) {
        timer_del(s->timer);
    } else {
        timer_mod(s->timer, next_ns);
    }
}

static void rtc_timer_cb(void *opaque)
{
    JZ4740RTCState *s = opaque;

    rtc_update_irq(s);
    rtc_schedule(s);
}

static uint32_t rtc_read_reg(JZ4740RTCState *s, hwaddr offset)
{
    switch (offset) {
    case RTC_RTCCR:
        return rtc_latch_flags(s) | RTC_RTCCR_WRDY;
    case RTC_RTCSR:
        return jz4740_rtc_seconds(s);
    case RTC_RTCSAR:
        return s->alarm_seconds;
    case RTC_RTCGR:
        return s->regulator & RTC_RTCGR_RW_MASK;
    case RTC_HCR:
        return s->hcr & RTC_HCR_PD;
    case RTC_HWFCR:
        return s->hwfcr & RTC_HWFCR_MASK;
    case RTC_HRCR:
        return s->hrcr & RTC_HRCR_MASK;
    case RTC_HWCR:
        return s->hwcr & RTC_HWCR_EALM;
    case RTC_HWRSR:
        return s->hwrsr & RTC_HWRSR_MASK;
    case RTC_HSPR:
        return s->scratch;
    default:
        return 0;
    }
}

static void rtc_enter_hibernate(JZ4740RTCState *s)
{
    bool was_powered_down = (s->hcr & RTC_HCR_PD) != 0;

    s->hcr |= RTC_HCR_PD;
    s->hwrsr &= ~(RTC_HWRSR_ALM | RTC_HWRSR_PIN);
    if (!was_powered_down && s->power_down_callback) {
        s->power_down_callback(s->power_down_opaque);
    }
}

static void rtc_write_while_hibernating(JZ4740RTCState *s, hwaddr offset,
                                        uint32_t value)
{
    if (offset != RTC_RTCCR) {
        return;
    }
    if (!(value & RTC_RTCCR_1HZ)) {
        s->control &= ~RTC_RTCCR_1HZ;
    }
    s->control = (s->control & ~RTC_RTCCR_1HZIE) |
                 (value & RTC_RTCCR_1HZIE);
}

static void rtc_write_reg(JZ4740RTCState *s, hwaddr offset, uint32_t value)
{
    uint32_t flags = s->control & (RTC_RTCCR_1HZ | RTC_RTCCR_AF);
    bool was_enabled = (s->control & RTC_RTCCR_RTCE) != 0;

    if (s->hcr & RTC_HCR_PD) {
        rtc_write_while_hibernating(s, offset, value);
        return;
    }
    switch (offset) {
    case RTC_RTCCR:
        if (!(value & RTC_RTCCR_1HZ)) {
            flags &= ~RTC_RTCCR_1HZ;
        }
        if (!(value & RTC_RTCCR_AF)) {
            flags &= ~RTC_RTCCR_AF;
        }
        s->control = flags | (value & RTC_RTCCR_RW_MASK);
        if (!was_enabled && (s->control & RTC_RTCCR_RTCE)) {
            s->base_ns = qemu_clock_get_ns(rtc_clock);
            s->one_hz_latched_seconds = s->base_seconds;
        }
        break;
    case RTC_RTCSR:
        s->base_seconds = value;
        s->base_ns = qemu_clock_get_ns(rtc_clock);
        s->one_hz_latched_seconds = s->base_seconds;
        if (value < s->alarm_seconds) {
            s->alarm_latched = false;
        }
        s->control &= ~RTC_RTCCR_1HZ;
        break;
    case RTC_RTCSAR:
        s->alarm_seconds = value;
        s->alarm_latched = false;
        s->control &= ~RTC_RTCCR_AF;
        break;
    case RTC_RTCGR:
        if (!(s->regulator & RTC_RTCGR_LOCK)) {
            s->regulator = value & RTC_RTCGR_RW_MASK;
        }
        break;
    case RTC_HCR:
        if (value & RTC_HCR_PD) {
            rtc_enter_hibernate(s);
        } else {
            s->hcr = 0;
        }
        break;
    case RTC_HWFCR:
        s->hwfcr = value & RTC_HWFCR_MASK;
        break;
    case RTC_HRCR:
        s->hrcr = value & RTC_HRCR_MASK;
        break;
    case RTC_HWCR:
        s->hwcr = value & RTC_HWCR_EALM;
        break;
    case RTC_HWRSR:
        s->hwrsr &= value & RTC_HWRSR_MASK;
        break;
    case RTC_HSPR:
        s->scratch = value;
        break;
    default:
        break;
    }
}

static uint64_t rtc_extract32(uint32_t value, hwaddr offset, unsigned size)
{
    value >>= (offset & 3u) * 8u;
    switch (size) {
    case 1:
        return value & 0xffu;
    case 2:
        return value & 0xffffu;
    default:
        return value;
    }
}

static uint64_t rtc_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740RTCState *s = opaque;
    hwaddr aligned_offset = offset & ~3u;
    uint32_t value = rtc_read_reg(s, aligned_offset);

    s->last_read_offset = offset;
    s->last_read_value = value;
    rtc_update_irq(s);
    rtc_schedule(s);
    return rtc_extract32(value, offset, size);
}

static void rtc_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    JZ4740RTCState *s = opaque;
    hwaddr aligned_offset = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t current = rtc_read_reg(s, aligned_offset);
    uint32_t mask;
    uint32_t merged;

    switch (size) {
    case 1:
        mask = 0xffu << shift;
        break;
    case 2:
        mask = 0xffffu << shift;
        break;
    default:
        mask = 0xffffffffu;
        shift = 0;
        break;
    }
    merged = (current & ~mask) | (((uint32_t)value << shift) & mask);
    rtc_write_reg(s, aligned_offset, merged);
    s->last_write_offset = offset;
    s->last_write_value = merged;
    rtc_update_irq(s);
    rtc_schedule(s);
}

static const MemoryRegionOps rtc_ops = {
    .read = rtc_read,
    .write = rtc_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
    .impl = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
};

void jz4740_rtc_get_diagnostics(JZ4740RTCState *s,
                                JZ4740RTCDiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    diagnostics->control = rtc_latch_flags(s) | RTC_RTCCR_WRDY;
    diagnostics->seconds = jz4740_rtc_seconds(s);
    diagnostics->alarm_seconds = s->alarm_seconds;
    diagnostics->hibernate_control = s->hcr;
    diagnostics->wake_status = s->hwrsr;
    diagnostics->last_read_offset = s->last_read_offset;
    diagnostics->last_read_value = s->last_read_value;
    diagnostics->last_write_offset = s->last_write_offset;
    diagnostics->last_write_value = s->last_write_value;
    diagnostics->irq_level = s->irq_level;
}

void jz4740_rtc_set_power_down_callback(
    JZ4740RTCState *s, JZ4740RTCPowerDownCallback callback, void *opaque)
{
    if (!s) {
        return;
    }
    s->power_down_callback = callback;
    s->power_down_opaque = opaque;
}

static void rtc_reset_hold(Object *obj, ResetType type)
{
    JZ4740RTCState *s = JZ4740_RTC(obj);

    s->control = RTC_RTCCR_RESET & ~RTC_RTCCR_WRDY;
    s->base_seconds = rtc_host_seconds();
    s->base_ns = qemu_clock_get_ns(rtc_clock);
    s->regulator = 0;
    s->alarm_seconds = 0xffffffffu;
    s->one_hz_latched_seconds = s->base_seconds;
    s->alarm_latched = false;
    s->hcr = 0;
    s->hwfcr = 0;
    s->hrcr = 0;
    s->hwcr = 0;
    s->hwrsr = s->hibernate_wakeup ?
               RTC_HWRSR_HR | RTC_HWRSR_PIN : RTC_HWRSR_PPR;
    s->scratch = RTC_MAGIC;
    s->last_read_offset = 0;
    s->last_read_value = 0;
    s->last_write_offset = 0;
    s->last_write_value = 0;
    s->irq_level = false;
    qemu_set_irq(s->irq, 0);
    rtc_schedule(s);
}

static int rtc_post_load(void *opaque, int version_id)
{
    JZ4740RTCState *s = opaque;

    rtc_update_irq(s);
    rtc_schedule(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_rtc = {
    .name = TYPE_JZ4740_RTC,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = rtc_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(control, JZ4740RTCState),
        VMSTATE_UINT32(base_seconds, JZ4740RTCState),
        VMSTATE_INT64(base_ns, JZ4740RTCState),
        VMSTATE_UINT32(regulator, JZ4740RTCState),
        VMSTATE_UINT32(alarm_seconds, JZ4740RTCState),
        VMSTATE_UINT32(one_hz_latched_seconds, JZ4740RTCState),
        VMSTATE_BOOL(alarm_latched, JZ4740RTCState),
        VMSTATE_UINT32(hcr, JZ4740RTCState),
        VMSTATE_UINT32(hwfcr, JZ4740RTCState),
        VMSTATE_UINT32(hrcr, JZ4740RTCState),
        VMSTATE_UINT32(hwcr, JZ4740RTCState),
        VMSTATE_UINT32(hwrsr, JZ4740RTCState),
        VMSTATE_UINT32(scratch, JZ4740RTCState),
        VMSTATE_UINT32(last_read_offset, JZ4740RTCState),
        VMSTATE_UINT32(last_read_value, JZ4740RTCState),
        VMSTATE_UINT32(last_write_offset, JZ4740RTCState),
        VMSTATE_UINT32(last_write_value, JZ4740RTCState),
        VMSTATE_END_OF_LIST()
    },
};

static void rtc_init(Object *obj)
{
    JZ4740RTCState *s = JZ4740_RTC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &rtc_ops, s, TYPE_JZ4740_RTC,
                          RTC_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
    s->timer = timer_new_ns(rtc_clock, rtc_timer_cb, s);
}

static void rtc_finalize(Object *obj)
{
    JZ4740RTCState *s = JZ4740_RTC(obj);

    timer_free(s->timer);
}

static const Property rtc_properties[] = {
    DEFINE_PROP_BOOL("hibernate-wakeup", JZ4740RTCState,
                     hibernate_wakeup, false),
};

static void rtc_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_rtc;
    device_class_set_props(dc, rtc_properties);
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
    rc->phases.hold = rtc_reset_hold;
}

static const TypeInfo rtc_type_info = {
    .name = TYPE_JZ4740_RTC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740RTCState),
    .instance_init = rtc_init,
    .instance_finalize = rtc_finalize,
    .class_init = rtc_class_init,
};

static void rtc_register_types(void)
{
    type_register_static(&rtc_type_info);
}

type_init(rtc_register_types)
