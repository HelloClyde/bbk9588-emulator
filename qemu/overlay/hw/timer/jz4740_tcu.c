/*
 * Ingenic JZ4740 timer/counter unit.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/timer/jz4740_tcu.h"
#include "migration/vmstate.h"
#include "qemu/host-utils.h"
#include "qemu/module.h"
#include "qemu/timer.h"

#define TCU_MMIO_SIZE       0x1000u
#define TCU_REGS            (TCU_MMIO_SIZE / sizeof(uint32_t))
#define TCU_MASK            ((1u << JZ4740_TCU_CHANNELS) - 1u)
#define TCU_TER             0x10u
#define TCU_TESR            0x14u
#define TCU_TECR            0x18u
#define TCU_TSR             0x1cu
#define TCU_TFR             0x20u
#define TCU_TFSR            0x24u
#define TCU_TFCR            0x28u
#define TCU_TSSR            0x2cu
#define TCU_TMR             0x30u
#define TCU_TMSR            0x34u
#define TCU_TMCR            0x38u
#define TCU_TSCR            0x3cu
#define TCU_CHANNEL_BASE    0x40u
#define TCU_CHANNEL_STRIDE  0x10u
#define TCU_TDFR            0x00u
#define TCU_TDHR            0x04u
#define TCU_TCNT            0x08u
#define TCU_TCSR            0x0cu
#define TCU_HALF_SHIFT      16u
#define TCU_FLAG_MASK       (TCU_MASK | (TCU_MASK << TCU_HALF_SHIFT))
#define TCU_TCSR_PCK_EN     0x0001u
#define TCU_TCSR_RTC_EN     0x0002u
#define TCU_TCSR_EXT_EN     0x0004u
#define TCU_TCSR_PRESCALE_SHIFT 3u
#define TCU_TCSR_PRESCALE_MASK (0x7u << TCU_TCSR_PRESCALE_SHIFT)
#define TCU_TCSR_RW_MASK    0x03bfu
#define TCU_PCLK_HZ         84000000u
#define TCU_EXTAL_HZ        12000000u
#define TCU_RTCCLK_HZ       32768u

struct JZ4740TCUState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq outputs[JZ4740_TCU_NUM_OUTPUTS];
    QEMUTimer *timer;
    uint32_t regs[TCU_REGS];
    uint32_t counter[JZ4740_TCU_CHANNELS];
    int64_t counter_anchor_ns[JZ4740_TCU_CHANNELS];
    uint32_t enabled_mask;
    uint32_t stop_mask;
    uint32_t pending_mask;
    uint32_t irq_mask;
    uint32_t default_period_ms;
    uint32_t compare[JZ4740_TCU_CHANNELS];
    uint32_t half_compare[JZ4740_TCU_CHANNELS];
    uint32_t period_ms[JZ4740_TCU_CHANNELS];
    uint32_t half_period_ms[JZ4740_TCU_CHANNELS];
    uint64_t period_ns[JZ4740_TCU_CHANNELS];
    uint64_t half_period_ns[JZ4740_TCU_CHANNELS];
    int64_t deadline_ns[JZ4740_TCU_CHANNELS];
    int64_t half_deadline_ns[JZ4740_TCU_CHANNELS];
    bool output_level[3];
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    uint32_t irq_raise_count;
};

static uint32_t tcu_channel_index(unsigned channel, hwaddr reg)
{
    return (TCU_CHANNEL_BASE + channel * TCU_CHANNEL_STRIDE + reg) /
           sizeof(uint32_t);
}

static uint32_t tcu_channel_tcsr(JZ4740TCUState *s, unsigned channel)
{
    return s->regs[tcu_channel_index(channel, TCU_TCSR)] & TCU_TCSR_RW_MASK;
}

static uint32_t tcu_prescale_divisor(uint32_t tcsr)
{
    switch ((tcsr & TCU_TCSR_PRESCALE_MASK) >> TCU_TCSR_PRESCALE_SHIFT) {
    case 0:
        return 1;
    case 1:
        return 4;
    case 2:
        return 16;
    case 3:
        return 64;
    case 4:
        return 256;
    case 5:
        return 1024;
    default:
        return 0;
    }
}

static uint32_t tcu_input_hz(uint32_t tcsr)
{
    switch (tcsr & (TCU_TCSR_PCK_EN | TCU_TCSR_RTC_EN | TCU_TCSR_EXT_EN)) {
    case TCU_TCSR_PCK_EN:
        return TCU_PCLK_HZ;
    case TCU_TCSR_RTC_EN:
        return TCU_RTCCLK_HZ;
    case TCU_TCSR_EXT_EN:
        return TCU_EXTAL_HZ;
    default:
        return 0;
    }
}

static uint32_t tcu_tick_hz(JZ4740TCUState *s, unsigned channel)
{
    uint32_t tcsr = tcu_channel_tcsr(s, channel);
    uint32_t hz = tcu_input_hz(tcsr);
    uint32_t divisor = tcu_prescale_divisor(tcsr);

    return hz == 0 || divisor == 0 ? 0 : hz / divisor;
}

static uint64_t tcu_ticks_to_ns(uint64_t ticks, uint32_t hz)
{
    uint64_t ns;

    if (ticks == 0 || hz == 0) {
        return 0;
    }
    ns = muldiv64_round_up(ticks, NANOSECONDS_PER_SECOND, hz);
    return MAX(ns, 1);
}

static uint32_t tcu_ticks_to_ms(uint32_t ticks, uint32_t hz)
{
    uint64_t ms;

    if (ticks == 0 || hz == 0) {
        return 0;
    }
    ms = muldiv64_round_up(ticks, 1000, hz);
    return MIN(ms, (uint64_t)UINT32_MAX);
}

static bool tcu_counter_running(JZ4740TCUState *s, unsigned channel)
{
    uint32_t bit = 1u << channel;

    return channel < JZ4740_TCU_CHANNELS && (s->enabled_mask & bit) &&
           !(s->stop_mask & bit) && tcu_tick_hz(s, channel) != 0;
}

static uint32_t tcu_current_counter(JZ4740TCUState *s, unsigned channel,
                                    int64_t now_ns)
{
    uint32_t base;
    uint32_t full;
    uint32_t hz;
    uint64_t elapsed_ticks;

    if (channel >= JZ4740_TCU_CHANNELS) {
        channel = 0;
    }
    base = s->counter[channel] & 0xffffu;
    if (!tcu_counter_running(s, channel) ||
        now_ns <= s->counter_anchor_ns[channel]) {
        return base;
    }
    hz = tcu_tick_hz(s, channel);
    elapsed_ticks = muldiv64(now_ns - s->counter_anchor_ns[channel], hz,
                             NANOSECONDS_PER_SECOND);
    full = s->compare[channel] & 0xffffu;
    return full == 0 ? (base + elapsed_ticks) & 0xffffu :
                       (base + elapsed_ticks) % full;
}

static void tcu_latch_counter(JZ4740TCUState *s, unsigned channel,
                              int64_t now_ns)
{
    if (channel >= JZ4740_TCU_CHANNELS) {
        return;
    }
    s->counter[channel] = tcu_current_counter(s, channel, now_ns) & 0xffffu;
    s->counter_anchor_ns[channel] = now_ns;
}

static void tcu_update_period_cache(JZ4740TCUState *s, unsigned channel)
{
    uint32_t hz;
    uint32_t full_ticks;
    uint32_t half_ticks;

    if (channel >= JZ4740_TCU_CHANNELS) {
        return;
    }
    hz = tcu_tick_hz(s, channel);
    full_ticks = s->compare[channel] & 0xffffu;
    half_ticks = s->half_compare[channel] & 0xffffu;
    s->period_ns[channel] = tcu_ticks_to_ns(full_ticks, hz);
    s->period_ms[channel] = tcu_ticks_to_ms(full_ticks, hz);
    if (full_ticks != 0 && half_ticks <= full_ticks) {
        s->half_period_ns[channel] = tcu_ticks_to_ns(half_ticks, hz);
        s->half_period_ms[channel] = tcu_ticks_to_ms(half_ticks, hz);
    } else {
        s->half_period_ns[channel] = 0;
        s->half_period_ms[channel] = 0;
    }
}

static uint64_t tcu_ticks_until_match(JZ4740TCUState *s, unsigned channel,
                                      uint32_t target, int64_t now_ns)
{
    uint32_t current;
    uint32_t full = s->compare[channel] & 0xffffu;

    if (full == 0 || target > full) {
        return 0;
    }
    current = tcu_current_counter(s, channel, now_ns);
    if (target == full) {
        return current < full ? full - current : full;
    }
    if (current < target) {
        return target - current;
    }
    return (uint64_t)full - current + target;
}

static int64_t tcu_next_deadline_ns(JZ4740TCUState *s, unsigned channel,
                                    bool half, int64_t now_ns)
{
    uint32_t bit = 1u << channel;
    uint32_t flag = half ? bit << TCU_HALF_SHIFT : bit;
    uint32_t target = half ? s->half_compare[channel] & 0xffffu :
                             s->compare[channel] & 0xffffu;
    uint32_t hz;
    uint64_t ticks;
    uint64_t ns;

    if (!tcu_counter_running(s, channel) || (s->pending_mask & flag)) {
        return 0;
    }
    hz = tcu_tick_hz(s, channel);
    ticks = tcu_ticks_until_match(s, channel, target, now_ns);
    ns = tcu_ticks_to_ns(ticks, hz);
    return ns == 0 ? 0 : now_ns + ns;
}

static uint32_t tcu_parent_flags(unsigned output)
{
    uint32_t channels;

    if (output == JZ4740_TCU_IRQ_TCU0) {
        channels = 1u << 0;
    } else if (output == JZ4740_TCU_IRQ_TCU1) {
        channels = 1u << 1;
    } else {
        channels = TCU_MASK & ~0x3u;
    }
    return channels | (channels << TCU_HALF_SHIFT);
}

static void tcu_sync_irq(JZ4740TCUState *s)
{
    uint32_t active = s->pending_mask & ~s->irq_mask;

    for (unsigned output = 0; output < 3; output++) {
        bool level = (active & tcu_parent_flags(output)) != 0;

        if (level != s->output_level[output]) {
            s->output_level[output] = level;
            qemu_set_irq(s->outputs[output], level);
        }
    }
}

static void tcu_schedule(JZ4740TCUState *s)
{
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    int64_t next_deadline = 0;

    for (unsigned ch = 0; ch < JZ4740_TCU_CHANNELS; ch++) {
        int64_t full = tcu_next_deadline_ns(s, ch, false, now_ns);
        int64_t half = tcu_next_deadline_ns(s, ch, true, now_ns);

        s->deadline_ns[ch] = full;
        s->half_deadline_ns[ch] = half;
        if (full && (!next_deadline || full < next_deadline)) {
            next_deadline = full;
        }
        if (half && (!next_deadline || half < next_deadline)) {
            next_deadline = half;
        }
    }
    if (next_deadline == 0) {
        timer_del(s->timer);
    } else {
        timer_mod(s->timer, next_deadline);
    }
}

static void tcu_raise_pending(JZ4740TCUState *s)
{
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    uint32_t newly_pending = 0;

    for (unsigned ch = 0; ch < JZ4740_TCU_CHANNELS; ch++) {
        uint32_t bit = 1u << ch;
        uint32_t half_bit = bit << TCU_HALF_SHIFT;

        if (!(s->pending_mask & bit) && s->deadline_ns[ch] &&
            now_ns >= s->deadline_ns[ch]) {
            newly_pending |= bit;
        }
        if (!(s->pending_mask & half_bit) && s->half_deadline_ns[ch] &&
            now_ns >= s->half_deadline_ns[ch]) {
            newly_pending |= half_bit;
        }
    }
    if (newly_pending) {
        s->pending_mask |= newly_pending;
        s->irq_raise_count++;
        qemu_set_irq(s->outputs[JZ4740_TCU_EVENT], 1);
        qemu_set_irq(s->outputs[JZ4740_TCU_EVENT], 0);
    }
    tcu_sync_irq(s);
}

static void tcu_timer_cb(void *opaque)
{
    JZ4740TCUState *s = opaque;

    tcu_raise_pending(s);
    tcu_schedule(s);
}

static void tcu_update_compare(JZ4740TCUState *s, unsigned channel,
                               hwaddr reg, uint32_t compare)
{
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    if (channel >= JZ4740_TCU_CHANNELS) {
        return;
    }
    tcu_latch_counter(s, channel, now_ns);
    compare &= 0xffffu;
    s->regs[tcu_channel_index(channel, reg)] = compare;
    if (reg == TCU_TDHR) {
        s->half_compare[channel] = compare;
        s->half_deadline_ns[channel] = 0;
    } else {
        s->compare[channel] = compare;
        if (compare != 0 && s->counter[channel] >= compare) {
            s->counter[channel] %= compare;
        }
        s->deadline_ns[channel] = 0;
    }
    tcu_update_period_cache(s, channel);
}

static void tcu_write_counter(JZ4740TCUState *s, unsigned channel,
                              uint32_t value)
{
    if (channel >= JZ4740_TCU_CHANNELS) {
        channel = 0;
    }
    value &= 0xffffu;
    s->counter[channel] = value;
    s->counter_anchor_ns[channel] = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->regs[tcu_channel_index(channel, TCU_TCNT)] = value;
}

static uint32_t tcu_read_counter(JZ4740TCUState *s, unsigned channel)
{
    if (channel >= JZ4740_TCU_CHANNELS) {
        channel = 0;
    }
    return tcu_current_counter(s, channel,
                               qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL)) &
           0xffffu;
}

static bool tcu_channel_reg(hwaddr offset, unsigned *channel, hwaddr *reg)
{
    if (offset < TCU_CHANNEL_BASE ||
        offset >= TCU_CHANNEL_BASE +
                  JZ4740_TCU_CHANNELS * TCU_CHANNEL_STRIDE) {
        return false;
    }
    *channel = (offset - TCU_CHANNEL_BASE) / TCU_CHANNEL_STRIDE;
    *reg = (offset - TCU_CHANNEL_BASE) & (TCU_CHANNEL_STRIDE - 1u);
    return *reg == TCU_TDFR || *reg == TCU_TDHR || *reg == TCU_TCNT ||
           *reg == TCU_TCSR;
}

static uint32_t tcu_read_word(JZ4740TCUState *s, hwaddr offset)
{
    uint32_t value;
    unsigned channel;
    hwaddr reg;

    if (offset == TCU_TESR || offset == TCU_TECR || offset == TCU_TFSR ||
        offset == TCU_TFCR || offset == TCU_TSSR || offset == TCU_TMSR ||
        offset == TCU_TMCR || offset == TCU_TSCR) {
        value = 0;
    } else if (offset == 0x08) {
        value = tcu_read_counter(s, 0);
    } else if (offset == TCU_TER) {
        value = s->enabled_mask;
    } else if (offset == TCU_TSR) {
        value = s->stop_mask;
    } else if (offset == TCU_TFR) {
        value = s->pending_mask;
    } else if (offset == TCU_TMR) {
        value = s->irq_mask;
    } else if (tcu_channel_reg(offset, &channel, &reg)) {
        if (reg == TCU_TDFR) {
            value = s->compare[channel];
        } else if (reg == TCU_TDHR) {
            value = s->half_compare[channel];
        } else if (reg == TCU_TCNT) {
            value = tcu_read_counter(s, channel);
        } else {
            value = s->regs[offset / sizeof(uint32_t)] & TCU_TCSR_RW_MASK;
        }
    } else if (offset == 0x04 || offset == 0x0c) {
        value = s->regs[offset / sizeof(uint32_t)] | 1u;
    } else {
        value = s->regs[offset / sizeof(uint32_t)];
    }
    s->last_read_offset = offset;
    s->last_read_value = value;
    return value;
}

static void tcu_write_word(JZ4740TCUState *s, hwaddr offset, uint32_t value)
{
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    unsigned channel;
    hwaddr reg;

    s->last_write_offset = offset;
    s->last_write_value = value;
    switch (offset) {
    case TCU_TESR:
        value = (value & TCU_MASK) & ~s->enabled_mask;
        for (unsigned ch = 0; ch < JZ4740_TCU_CHANNELS; ch++) {
            if (value & (1u << ch)) {
                tcu_latch_counter(s, ch, now_ns);
            }
        }
        s->enabled_mask |= value;
        tcu_schedule(s);
        break;
    case TCU_TECR:
        value &= TCU_MASK;
        for (unsigned ch = 0; ch < JZ4740_TCU_CHANNELS; ch++) {
            if (value & (1u << ch)) {
                tcu_latch_counter(s, ch, now_ns);
                s->deadline_ns[ch] = 0;
                s->half_deadline_ns[ch] = 0;
            }
        }
        s->enabled_mask &= ~value;
        tcu_sync_irq(s);
        tcu_schedule(s);
        break;
    case TCU_TFSR:
        s->pending_mask |= value & TCU_FLAG_MASK;
        tcu_sync_irq(s);
        break;
    case TCU_TFCR:
        s->pending_mask &= ~(value & TCU_FLAG_MASK);
        tcu_sync_irq(s);
        tcu_schedule(s);
        break;
    case TCU_TSSR:
        value &= TCU_MASK;
        for (unsigned ch = 0; ch < JZ4740_TCU_CHANNELS; ch++) {
            if (value & (1u << ch)) {
                tcu_latch_counter(s, ch, now_ns);
                s->deadline_ns[ch] = 0;
                s->half_deadline_ns[ch] = 0;
            }
        }
        s->stop_mask |= value;
        tcu_schedule(s);
        break;
    case TCU_TMSR:
        s->irq_mask |= value & TCU_FLAG_MASK;
        tcu_sync_irq(s);
        break;
    case TCU_TMCR:
        s->irq_mask &= ~(value & TCU_FLAG_MASK);
        tcu_sync_irq(s);
        tcu_schedule(s);
        break;
    case TCU_TSCR:
        value &= TCU_MASK;
        s->stop_mask &= ~value;
        for (unsigned ch = 0; ch < JZ4740_TCU_CHANNELS; ch++) {
            uint32_t bit = 1u << ch;

            if ((value & bit) && (s->enabled_mask & bit)) {
                tcu_latch_counter(s, ch, now_ns);
            }
        }
        tcu_schedule(s);
        break;
    default:
        if (!tcu_channel_reg(offset, &channel, &reg)) {
            break;
        }
        if (reg == TCU_TCNT) {
            tcu_write_counter(s, channel, value);
        } else if (reg == TCU_TCSR) {
            tcu_latch_counter(s, channel, now_ns);
            s->regs[tcu_channel_index(channel, reg)] =
                value & TCU_TCSR_RW_MASK;
            tcu_update_period_cache(s, channel);
        } else {
            tcu_update_compare(s, channel, reg, value);
        }
        tcu_schedule(s);
        break;
    }
}

static uint64_t tcu_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740TCUState *s = JZ4740_TCU(opaque);
    uint32_t value = tcu_read_word(s, offset & ~3u);

    value >>= (offset & 3u) * 8u;
    if (size == 1) {
        return value & 0xffu;
    }
    if (size == 2) {
        return value & 0xffffu;
    }
    return value;
}

static void tcu_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    JZ4740TCUState *s = JZ4740_TCU(opaque);
    hwaddr aligned = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t old = s->regs[aligned / sizeof(uint32_t)];
    uint32_t mask;
    uint32_t lane_value;
    uint32_t merged;
    unsigned channel;
    hwaddr reg;

    if (size == 1) {
        mask = 0xffu << shift;
    } else if (size == 2) {
        mask = 0xffffu << shift;
    } else {
        mask = 0xffffffffu;
        shift = 0;
    }
    lane_value = ((uint32_t)value << shift) & mask;
    merged = (old & ~mask) | lane_value;
    s->regs[aligned / sizeof(uint32_t)] = merged;
    tcu_write_word(s, aligned,
                   tcu_channel_reg(aligned, &channel, &reg) ?
                   merged : lane_value);
}

static const MemoryRegionOps tcu_ops = {
    .read = tcu_read,
    .write = tcu_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
};

bool jz4740_tcu_irq_level(JZ4740TCUState *s, unsigned output)
{
    return s && output < 3 && s->output_level[output];
}

void jz4740_tcu_get_diagnostics(JZ4740TCUState *s,
                                JZ4740TCUDiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    diagnostics->enabled_mask = s->enabled_mask;
    diagnostics->stop_mask = s->stop_mask;
    diagnostics->pending_mask = s->pending_mask;
    diagnostics->irq_mask = s->irq_mask;
    memcpy(diagnostics->compare, s->compare, sizeof(s->compare));
    memcpy(diagnostics->half_compare, s->half_compare,
           sizeof(s->half_compare));
    memcpy(diagnostics->period_ms, s->period_ms, sizeof(s->period_ms));
    memcpy(diagnostics->half_period_ms, s->half_period_ms,
           sizeof(s->half_period_ms));
    memcpy(diagnostics->deadline_ns, s->deadline_ns, sizeof(s->deadline_ns));
    memcpy(diagnostics->half_deadline_ns, s->half_deadline_ns,
           sizeof(s->half_deadline_ns));
    diagnostics->last_read_offset = s->last_read_offset;
    diagnostics->last_read_value = s->last_read_value;
    diagnostics->last_write_offset = s->last_write_offset;
    diagnostics->last_write_value = s->last_write_value;
    diagnostics->irq_raise_count = s->irq_raise_count;
}

static void tcu_reset_hold(Object *obj, ResetType type)
{
    JZ4740TCUState *s = JZ4740_TCU(obj);
    int64_t now_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);

    memset(s->regs, 0, sizeof(s->regs));
    memset(s->counter, 0, sizeof(s->counter));
    s->enabled_mask = 0;
    s->stop_mask = 0;
    s->pending_mask = 0;
    s->irq_mask = 0;
    s->last_read_offset = 0;
    s->last_read_value = 0;
    s->last_write_offset = 0;
    s->last_write_value = 0;
    s->irq_raise_count = 0;
    for (unsigned ch = 0; ch < JZ4740_TCU_CHANNELS; ch++) {
        s->counter_anchor_ns[ch] = now_ns;
        s->compare[ch] = s->default_period_ms * 1000u;
        s->half_compare[ch] = s->default_period_ms * 1000u;
        s->period_ms[ch] = s->default_period_ms;
        s->half_period_ms[ch] = s->default_period_ms;
        s->period_ns[ch] = 0;
        s->half_period_ns[ch] = 0;
        s->deadline_ns[ch] = 0;
        s->half_deadline_ns[ch] = 0;
    }
    memset(s->output_level, 0, sizeof(s->output_level));
    for (unsigned output = 0; output < JZ4740_TCU_NUM_OUTPUTS; output++) {
        qemu_set_irq(s->outputs[output], 0);
    }
    timer_del(s->timer);
}

static int tcu_post_load(void *opaque, int version_id)
{
    JZ4740TCUState *s = opaque;

    memset(s->output_level, 0, sizeof(s->output_level));
    tcu_sync_irq(s);
    tcu_schedule(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_tcu = {
    .name = TYPE_JZ4740_TCU,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = tcu_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4740TCUState, TCU_REGS),
        VMSTATE_UINT32_ARRAY(counter, JZ4740TCUState, JZ4740_TCU_CHANNELS),
        VMSTATE_INT64_ARRAY(counter_anchor_ns, JZ4740TCUState,
                            JZ4740_TCU_CHANNELS),
        VMSTATE_UINT32(enabled_mask, JZ4740TCUState),
        VMSTATE_UINT32(stop_mask, JZ4740TCUState),
        VMSTATE_UINT32(pending_mask, JZ4740TCUState),
        VMSTATE_UINT32(irq_mask, JZ4740TCUState),
        VMSTATE_UINT32_ARRAY(compare, JZ4740TCUState, JZ4740_TCU_CHANNELS),
        VMSTATE_UINT32_ARRAY(half_compare, JZ4740TCUState,
                             JZ4740_TCU_CHANNELS),
        VMSTATE_UINT32_ARRAY(period_ms, JZ4740TCUState, JZ4740_TCU_CHANNELS),
        VMSTATE_UINT32_ARRAY(half_period_ms, JZ4740TCUState,
                             JZ4740_TCU_CHANNELS),
        VMSTATE_UINT64_ARRAY(period_ns, JZ4740TCUState, JZ4740_TCU_CHANNELS),
        VMSTATE_UINT64_ARRAY(half_period_ns, JZ4740TCUState,
                             JZ4740_TCU_CHANNELS),
        VMSTATE_INT64_ARRAY(deadline_ns, JZ4740TCUState,
                            JZ4740_TCU_CHANNELS),
        VMSTATE_INT64_ARRAY(half_deadline_ns, JZ4740TCUState,
                            JZ4740_TCU_CHANNELS),
        VMSTATE_UINT32(last_read_offset, JZ4740TCUState),
        VMSTATE_UINT32(last_read_value, JZ4740TCUState),
        VMSTATE_UINT32(last_write_offset, JZ4740TCUState),
        VMSTATE_UINT32(last_write_value, JZ4740TCUState),
        VMSTATE_UINT32(irq_raise_count, JZ4740TCUState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property tcu_properties[] = {
    DEFINE_PROP_UINT32("period-ms", JZ4740TCUState, default_period_ms, 10),
};

static void tcu_init(Object *obj)
{
    JZ4740TCUState *s = JZ4740_TCU(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &tcu_ops, s, TYPE_JZ4740_TCU,
                          TCU_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    for (unsigned output = 0; output < JZ4740_TCU_NUM_OUTPUTS; output++) {
        sysbus_init_irq(sbd, &s->outputs[output]);
    }
    s->timer = timer_new_ns(QEMU_CLOCK_VIRTUAL, tcu_timer_cb, s);
}

static void tcu_finalize(Object *obj)
{
    JZ4740TCUState *s = JZ4740_TCU(obj);

    timer_free(s->timer);
}

static void tcu_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_tcu;
    device_class_set_props(dc, tcu_properties);
    rc->phases.hold = tcu_reset_hold;
}

static const TypeInfo tcu_type_info = {
    .name = TYPE_JZ4740_TCU,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740TCUState),
    .instance_init = tcu_init,
    .instance_finalize = tcu_finalize,
    .class_init = tcu_class_init,
};

static void tcu_register_types(void)
{
    type_register_static(&tcu_type_info);
}

type_init(tcu_register_types)
