/*
 * Ingenic JZ4740 SADC controller.
 *
 * Models the software-visible SADC registers, touchscreen FIFO, conversion
 * timing and interrupt output. Board code supplies physical touch samples and
 * the board-specific PBAT/SADCIN raw values.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/input/jz4740_sadc.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "qemu/timer.h"

#define SADC_MMIO_SIZE              0x1000u
#define SADC_DATA_MASK              0x0fffu
#define SADC_CONFIG_RESET           0x0002002cu
#define SADC_FIFO_DEPTH             2u

#define SADC_ADENA                  0x00u
#define SADC_ADCFG                  0x04u
#define SADC_ADCTRL                 0x08u
#define SADC_ADSTATE                0x0cu
#define SADC_ADSAME                 0x10u
#define SADC_ADWAIT                 0x14u
#define SADC_ADTCH                  0x18u
#define SADC_ADBDAT                 0x1cu
#define SADC_ADSDAT                 0x20u

#define SADC_CONFIG_XYZ_MASK        0x00006000u
#define SADC_CONFIG_XYZ_SHIFT       13u
#define SADC_CONFIG_XYZ_XY          0u
#define SADC_CONFIG_XYZ_ZS          1u
#define SADC_CONFIG_XYZ_Z12         2u
#define SADC_CONFIG_XYZ_RESERVED    3u

#define SADC_ADENA_SADCINEN         0x01u
#define SADC_ADENA_PBATEN           0x02u
#define SADC_ADENA_TCHEN            0x04u

#define SADC_STATE_SRDY             0x01u
#define SADC_STATE_DRDY             0x02u
#define SADC_STATE_DTCH             0x04u
#define SADC_STATE_PENU             0x08u
#define SADC_STATE_PEND             0x10u
#define SADC_STATE_MASK             0x1fu

#define SADC_TOUCH_TYPE0            0x00008000u
#define SADC_TOUCH_TYPE1            0x80000000u
#define SADC_TOUCH_ZS_RAW           0x0800u
#define SADC_TOUCH_Z1_RAW           0x0400u
#define SADC_TOUCH_Z2_RAW           0x0c00u

struct JZ4740SADCState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    QEMUTimer *timer;

    bool touch_down;
    bool touch_move_pending;
    uint16_t touch_raw_x;
    uint16_t touch_raw_y;
    uint8_t enable;
    uint32_t config;
    uint8_t control;
    uint8_t status_event;
    uint8_t pending_enable;
    uint16_t same_time;
    uint16_t wait_time;
    bool touch_sample_is_move;
    uint16_t battery_data;
    uint16_t sadcin_data;
    uint32_t battery_raw;
    uint32_t sadcin_raw;
    uint32_t touch_fifo[SADC_FIFO_DEPTH];
    uint32_t touch_fifo_head;
    uint32_t touch_fifo_count;
    uint32_t next_axis;
    uint32_t conversion_events_remaining;
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    bool irq_level;

    JZ4740SADCTraceCallback trace_callback;
    void *trace_opaque;
};

static void sadc_notify_trace(JZ4740SADCState *s, uint32_t reason)
{
    if (s->trace_callback) {
        s->trace_callback(s->trace_opaque, reason);
    }
}

static bool sadc_irq_pending(JZ4740SADCState *s)
{
    return (s->status_event & ~s->control & SADC_STATE_MASK) != 0;
}

static void sadc_update_irq(JZ4740SADCState *s)
{
    bool level = sadc_irq_pending(s);

    s->irq_level = level;
    qemu_set_irq(s->irq, level);
}

static void sadc_touch_fifo_clear(JZ4740SADCState *s)
{
    s->touch_fifo_head = 0;
    s->touch_fifo_count = 0;
    memset(s->touch_fifo, 0, sizeof(s->touch_fifo));
}

static uint32_t sadc_pack_touch_pair(uint16_t data0, uint16_t data1,
                                     bool type0, bool type1)
{
    uint32_t value = ((uint32_t)(data1 & SADC_DATA_MASK) << 16) |
                     (uint32_t)(data0 & SADC_DATA_MASK);

    if (type0) {
        value |= SADC_TOUCH_TYPE0;
    }
    if (type1) {
        value |= SADC_TOUCH_TYPE1;
    }
    return value;
}

static void sadc_touch_fifo_push(JZ4740SADCState *s, uint32_t sample)
{
    unsigned index;

    if (s->touch_fifo_count == SADC_FIFO_DEPTH) {
        s->touch_fifo_head = (s->touch_fifo_head + 1) % SADC_FIFO_DEPTH;
        s->touch_fifo_count--;
    }
    index = (s->touch_fifo_head + s->touch_fifo_count) % SADC_FIFO_DEPTH;
    s->touch_fifo[index] = sample;
    s->touch_fifo_count++;
}

static uint32_t sadc_touch_fifo_pop(JZ4740SADCState *s)
{
    uint32_t sample;

    if (s->touch_fifo_count == 0) {
        return 0;
    }
    sample = s->touch_fifo[s->touch_fifo_head];
    s->touch_fifo[s->touch_fifo_head] = 0;
    s->touch_fifo_head = (s->touch_fifo_head + 1) % SADC_FIFO_DEPTH;
    s->touch_fifo_count--;
    s->next_axis++;
    return sample;
}

static unsigned sadc_touch_xyz_mode(JZ4740SADCState *s)
{
    return (s->config & SADC_CONFIG_XYZ_MASK) >> SADC_CONFIG_XYZ_SHIFT;
}

static void sadc_queue_touch_sample(JZ4740SADCState *s)
{
    sadc_touch_fifo_clear(s);
    s->next_axis = 0;

    switch (sadc_touch_xyz_mode(s)) {
    case SADC_CONFIG_XYZ_ZS:
        sadc_touch_fifo_push(
            s, sadc_pack_touch_pair(s->touch_raw_x, s->touch_raw_y,
                                    false, false));
        sadc_touch_fifo_push(
            s, sadc_pack_touch_pair(SADC_TOUCH_ZS_RAW, 0, false, false));
        break;
    case SADC_CONFIG_XYZ_Z12:
        sadc_touch_fifo_push(
            s, sadc_pack_touch_pair(s->touch_raw_x, s->touch_raw_y,
                                    true, true));
        sadc_touch_fifo_push(
            s, sadc_pack_touch_pair(SADC_TOUCH_Z1_RAW, SADC_TOUCH_Z2_RAW,
                                    true, true));
        break;
    case SADC_CONFIG_XYZ_RESERVED:
    case SADC_CONFIG_XYZ_XY:
    default:
        sadc_touch_fifo_push(
            s, sadc_pack_touch_pair(s->touch_raw_x, s->touch_raw_y,
                                    false, false));
        break;
    }
    s->status_event |= SADC_STATE_DTCH;
}

static uint32_t sadc_touch_delay_ms(JZ4740SADCState *s, bool same_point)
{
    uint32_t ticks = same_point ? s->same_time : s->wait_time;
    uint64_t scaled = (uint64_t)ticks * 128u;

    /* ADSAME/ADWAIT use the manual's 12 MHz / 128 counter clock. */
    return MAX(1u, (uint32_t)((scaled + 11999u) / 12000u));
}

static bool sadc_queue_next_touch_sample(JZ4740SADCState *s)
{
    bool conversion_pending;

    if (!s->touch_down || !(s->enable & SADC_ADENA_TCHEN) ||
        (s->status_event & SADC_STATE_DTCH) || s->touch_fifo_count != 0 ||
        (s->pending_enable & SADC_ADENA_TCHEN)) {
        return false;
    }
    conversion_pending = s->pending_enable != 0;
    if (s->conversion_events_remaining > 0) {
        s->touch_sample_is_move = false;
        s->pending_enable |= SADC_ADENA_TCHEN;
        if (!conversion_pending) {
            timer_mod(s->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
                                sadc_touch_delay_ms(s, true));
        }
        return true;
    }
    if (s->touch_move_pending) {
        s->touch_sample_is_move = true;
        s->pending_enable |= SADC_ADENA_TCHEN;
        if (!conversion_pending) {
            timer_mod(s->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
                                sadc_touch_delay_ms(s, false));
        }
        return true;
    }
    return false;
}

static void sadc_complete_cpu_samples(JZ4740SADCState *s, uint8_t requested)
{
    if (requested & SADC_ADENA_SADCINEN) {
        s->sadcin_data = s->sadcin_raw & SADC_DATA_MASK;
        s->status_event |= SADC_STATE_SRDY;
        s->enable &= ~SADC_ADENA_SADCINEN;
    }
    if (requested & SADC_ADENA_PBATEN) {
        s->battery_data = s->battery_raw & SADC_DATA_MASK;
        s->status_event |= SADC_STATE_DRDY;
        s->enable &= ~SADC_ADENA_PBATEN;
    }
}

static void sadc_schedule_conversion(JZ4740SADCState *s, uint8_t requested)
{
    const uint8_t cpu_channels = SADC_ADENA_SADCINEN | SADC_ADENA_PBATEN;
    uint8_t previous_pending = s->pending_enable;
    uint8_t new_cpu_channels = requested & cpu_channels & ~previous_pending;
    uint32_t delay_ms;

    s->pending_enable |= requested;
    if (previous_pending && !new_cpu_channels) {
        return;
    }
    delay_ms = new_cpu_channels ? 1u : sadc_touch_delay_ms(s, false);
    timer_mod(s->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + delay_ms);
}

static void sadc_timer_cb(void *opaque)
{
    JZ4740SADCState *s = opaque;
    uint8_t requested = s->pending_enable;

    s->pending_enable = 0;
    sadc_complete_cpu_samples(s, requested);
    if ((requested & SADC_ADENA_TCHEN) && s->touch_down) {
        sadc_queue_touch_sample(s);
        if (s->touch_sample_is_move) {
            s->touch_move_pending = false;
        }
    }
    s->touch_sample_is_move = false;
    sadc_update_irq(s);
    sadc_notify_trace(s, 13u);
}

void jz4740_sadc_set_touch(JZ4740SADCState *s, uint16_t raw_x,
                           uint16_t raw_y, bool down)
{
    bool was_down;
    bool position_changed;

    if (!s) {
        return;
    }
    was_down = s->touch_down;
    position_changed = s->touch_raw_x != raw_x || s->touch_raw_y != raw_y;
    s->touch_raw_x = raw_x;
    s->touch_raw_y = raw_y;
    s->touch_down = down;

    if (down) {
        if (!was_down) {
            s->status_event = (s->status_event & ~SADC_STATE_PENU) |
                              SADC_STATE_PEND;
            s->touch_move_pending = false;
            s->conversion_events_remaining = 5;
            sadc_queue_next_touch_sample(s);
        } else if (position_changed) {
            s->touch_move_pending = true;
            sadc_queue_next_touch_sample(s);
        }
    } else if (was_down) {
        s->touch_move_pending = false;
        s->pending_enable &= ~SADC_ADENA_TCHEN;
        s->touch_sample_is_move = false;
        s->conversion_events_remaining = 0;
        s->status_event = (s->status_event & ~SADC_STATE_PEND) |
                          SADC_STATE_PENU;
    }
    if (was_down != down) {
        sadc_update_irq(s);
    }
    sadc_notify_trace(s, down ? 1u : 2u);
}

bool jz4740_sadc_touch_down(JZ4740SADCState *s)
{
    return s && s->touch_down;
}

void jz4740_sadc_get_diagnostics(JZ4740SADCState *s,
                                 JZ4740SADCDiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    diagnostics->touch_down = s->touch_down;
    diagnostics->touch_raw_x = s->touch_raw_x;
    diagnostics->touch_raw_y = s->touch_raw_y;
    diagnostics->enable = s->enable;
    diagnostics->control = s->control;
    diagnostics->status = s->status_event;
    diagnostics->pending_enable = s->pending_enable;
    diagnostics->fifo_count = s->touch_fifo_count;
    diagnostics->next_axis = s->next_axis;
    diagnostics->conversion_events_remaining = s->conversion_events_remaining;
    diagnostics->last_read_offset = s->last_read_offset;
    diagnostics->last_read_value = s->last_read_value;
    diagnostics->last_write_offset = s->last_write_offset;
    diagnostics->last_write_value = s->last_write_value;
    diagnostics->irq_level = s->irq_level;
}

void jz4740_sadc_set_trace_callback(JZ4740SADCState *s,
                                    JZ4740SADCTraceCallback callback,
                                    void *opaque)
{
    if (!s) {
        return;
    }
    s->trace_callback = callback;
    s->trace_opaque = opaque;
}

static uint64_t sadc_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740SADCState *s = opaque;
    uint32_t value;

    switch (offset) {
    case SADC_ADENA:
        value = s->enable;
        break;
    case SADC_ADCFG:
        value = s->config;
        break;
    case SADC_ADCTRL:
        value = s->control;
        break;
    case SADC_ADSTATE:
        value = s->status_event;
        break;
    case SADC_ADSAME:
        value = s->same_time;
        break;
    case SADC_ADWAIT:
        value = s->wait_time;
        break;
    case SADC_ADTCH:
        value = sadc_touch_fifo_pop(s);
        if (s->touch_fifo_count == 0 && sadc_queue_next_touch_sample(s)) {
            sadc_update_irq(s);
        }
        break;
    case SADC_ADBDAT:
        value = s->battery_data & SADC_DATA_MASK;
        break;
    case SADC_ADSDAT:
        value = s->sadcin_data & SADC_DATA_MASK;
        break;
    default:
        value = 0;
        break;
    }
    s->last_read_offset = offset;
    s->last_read_value = value;
    sadc_notify_trace(s, 9u);
    return value;
}

static void sadc_write(void *opaque, hwaddr offset, uint64_t value,
                       unsigned size)
{
    JZ4740SADCState *s = opaque;

    s->last_write_offset = offset;
    s->last_write_value = value;
    switch (offset) {
    case SADC_ADENA: {
        uint8_t requested = value & (SADC_ADENA_SADCINEN |
                                     SADC_ADENA_PBATEN |
                                     SADC_ADENA_TCHEN);

        s->enable = requested;
        if (requested) {
            sadc_schedule_conversion(s, requested);
        } else {
            s->pending_enable = 0;
            timer_del(s->timer);
        }
        sadc_update_irq(s);
        sadc_notify_trace(s, 8u);
        break;
    }
    case SADC_ADCFG:
        s->config = value;
        sadc_notify_trace(s, 8u);
        break;
    case SADC_ADCTRL:
        s->control = value & SADC_STATE_MASK;
        sadc_update_irq(s);
        sadc_notify_trace(s, 8u);
        break;
    case SADC_ADSTATE: {
        bool cleared_conversion = (value & SADC_STATE_DTCH) != 0;

        s->status_event &= ~((uint8_t)value & SADC_STATE_MASK);
        if (cleared_conversion && s->conversion_events_remaining > 0) {
            s->conversion_events_remaining--;
        }
        if (cleared_conversion) {
            sadc_queue_next_touch_sample(s);
        }
        sadc_update_irq(s);
        sadc_notify_trace(s, 7u);
        break;
    }
    case SADC_ADSAME:
        s->same_time = value;
        sadc_notify_trace(s, 8u);
        break;
    case SADC_ADWAIT:
        s->wait_time = value;
        sadc_notify_trace(s, 8u);
        break;
    case SADC_ADTCH:
        sadc_touch_fifo_clear(s);
        sadc_update_irq(s);
        sadc_notify_trace(s, 7u);
        break;
    case SADC_ADBDAT:
        s->battery_data = 0;
        s->status_event &= ~SADC_STATE_DRDY;
        sadc_update_irq(s);
        sadc_notify_trace(s, 7u);
        break;
    case SADC_ADSDAT:
        s->sadcin_data = 0;
        s->status_event &= ~SADC_STATE_SRDY;
        sadc_update_irq(s);
        sadc_notify_trace(s, 7u);
        break;
    default:
        sadc_notify_trace(s, 10u);
        break;
    }
}

static const MemoryRegionOps sadc_ops = {
    .read = sadc_read,
    .write = sadc_write,
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

static void sadc_reset_hold(Object *obj, ResetType type)
{
    JZ4740SADCState *s = JZ4740_SADC(obj);

    s->touch_down = false;
    s->touch_move_pending = false;
    s->touch_raw_x = 0x0e74;
    s->touch_raw_y = 0x0dde;
    s->enable = 0;
    s->config = SADC_CONFIG_RESET;
    s->control = 0;
    s->status_event = 0;
    s->pending_enable = 0;
    s->same_time = 0;
    s->wait_time = 0;
    s->touch_sample_is_move = false;
    s->battery_data = 0;
    s->sadcin_data = 0;
    sadc_touch_fifo_clear(s);
    s->next_axis = 0;
    s->conversion_events_remaining = 0;
    s->last_read_offset = 0;
    s->last_read_value = 0;
    s->last_write_offset = 0;
    s->last_write_value = 0;
    s->irq_level = false;
    timer_del(s->timer);
    qemu_set_irq(s->irq, 0);
}

static int sadc_post_load(void *opaque, int version_id)
{
    JZ4740SADCState *s = opaque;

    if (s->pending_enable) {
        timer_mod(s->timer, qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 1);
    }
    sadc_update_irq(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_sadc = {
    .name = TYPE_JZ4740_SADC,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = sadc_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_BOOL(touch_down, JZ4740SADCState),
        VMSTATE_BOOL(touch_move_pending, JZ4740SADCState),
        VMSTATE_UINT16(touch_raw_x, JZ4740SADCState),
        VMSTATE_UINT16(touch_raw_y, JZ4740SADCState),
        VMSTATE_UINT8(enable, JZ4740SADCState),
        VMSTATE_UINT32(config, JZ4740SADCState),
        VMSTATE_UINT8(control, JZ4740SADCState),
        VMSTATE_UINT8(status_event, JZ4740SADCState),
        VMSTATE_UINT8(pending_enable, JZ4740SADCState),
        VMSTATE_UINT16(same_time, JZ4740SADCState),
        VMSTATE_UINT16(wait_time, JZ4740SADCState),
        VMSTATE_BOOL(touch_sample_is_move, JZ4740SADCState),
        VMSTATE_UINT16(battery_data, JZ4740SADCState),
        VMSTATE_UINT16(sadcin_data, JZ4740SADCState),
        VMSTATE_UINT32(battery_raw, JZ4740SADCState),
        VMSTATE_UINT32(sadcin_raw, JZ4740SADCState),
        VMSTATE_UINT32_ARRAY(touch_fifo, JZ4740SADCState, SADC_FIFO_DEPTH),
        VMSTATE_UINT32(touch_fifo_head, JZ4740SADCState),
        VMSTATE_UINT32(touch_fifo_count, JZ4740SADCState),
        VMSTATE_UINT32(next_axis, JZ4740SADCState),
        VMSTATE_UINT32(conversion_events_remaining, JZ4740SADCState),
        VMSTATE_UINT32(last_read_offset, JZ4740SADCState),
        VMSTATE_UINT32(last_read_value, JZ4740SADCState),
        VMSTATE_UINT32(last_write_offset, JZ4740SADCState),
        VMSTATE_UINT32(last_write_value, JZ4740SADCState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property sadc_properties[] = {
    DEFINE_PROP_UINT32("battery-raw", JZ4740SADCState, battery_raw,
                       JZ4740_SADC_DEFAULT_BATTERY_RAW),
    DEFINE_PROP_UINT32("sadcin-raw", JZ4740SADCState, sadcin_raw,
                       JZ4740_SADC_DEFAULT_SADCIN_RAW),
};

static void sadc_init(Object *obj)
{
    JZ4740SADCState *s = JZ4740_SADC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &sadc_ops, s, TYPE_JZ4740_SADC,
                          SADC_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
    s->timer = timer_new_ms(QEMU_CLOCK_REALTIME, sadc_timer_cb, s);
}

static void sadc_finalize(Object *obj)
{
    JZ4740SADCState *s = JZ4740_SADC(obj);

    timer_free(s->timer);
}

static void sadc_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_sadc;
    device_class_set_props(dc, sadc_properties);
    set_bit(DEVICE_CATEGORY_INPUT, dc->categories);
    rc->phases.hold = sadc_reset_hold;
}

static const TypeInfo sadc_type_info = {
    .name = TYPE_JZ4740_SADC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740SADCState),
    .instance_init = sadc_init,
    .instance_finalize = sadc_finalize,
    .class_init = sadc_class_init,
};

static void sadc_register_types(void)
{
    type_register_static(&sadc_type_info);
}

type_init(sadc_register_types)
