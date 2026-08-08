/*
 * Ingenic JZ4740 GPIO controller.
 *
 * Models the four GPIO register banks, external pin levels, latched flags and
 * per-port interrupt outputs. Board code supplies reset pin levels and any
 * read-time board signals such as NAND ready/busy.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/gpio/jz4740_gpio.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define GPIO_MMIO_SIZE             0x1000u
#define GPIO_MODELED_SIZE          0x0400u
#define GPIO_PORT_STRIDE           0x0100u
#define GPIO_REG_WORDS             (GPIO_MODELED_SIZE / sizeof(uint32_t))

#define GPIO_PIN                   0x00u
#define GPIO_DAT                   0x10u
#define GPIO_DATS                  0x14u
#define GPIO_FLGC                  GPIO_DATS
#define GPIO_DATC                  0x18u
#define GPIO_IM                    0x20u
#define GPIO_IMS                   0x24u
#define GPIO_IMC                   0x28u
#define GPIO_PE                    0x30u
#define GPIO_PES                   0x34u
#define GPIO_PEC                   0x38u
#define GPIO_FUN                   0x40u
#define GPIO_FUNS                  0x44u
#define GPIO_FUNC                  0x48u
#define GPIO_SEL                   0x50u
#define GPIO_SELS                  0x54u
#define GPIO_SELC                  0x58u
#define GPIO_DIR                   0x60u
#define GPIO_DIRS                  0x64u
#define GPIO_DIRC                  0x68u
#define GPIO_TRG                   0x70u
#define GPIO_TRGS                  0x74u
#define GPIO_TRGC                  0x78u
#define GPIO_FLG                   0x80u

#define GPIO_IM_RESET              0xffffffffu

#define JZ4730_GPIO_MODELED_SIZE   0x00c0u
#define JZ4730_GPIO_PORT_STRIDE    0x0030u
#define JZ4730_GPIO_DAT            0x00u
#define JZ4730_GPIO_DIR            0x04u
#define JZ4730_GPIO_PE             0x0cu
#define JZ4730_GPIO_FSL            0x10u
#define JZ4730_GPIO_FSH            0x14u
#define JZ4730_GPIO_ITL            0x18u
#define JZ4730_GPIO_ITH            0x1cu
#define JZ4730_GPIO_IE             0x20u
#define JZ4730_GPIO_IM             0x24u
#define JZ4730_GPIO_IRQ            0x28u

struct JZ4740GPIOState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq[JZ4740_GPIO_NUM_PORTS];
    bool jz4730_layout;
    uint32_t regs[GPIO_REG_WORDS];
    uint32_t input_level[JZ4740_GPIO_NUM_PORTS];
    uint32_t input_reset[JZ4740_GPIO_NUM_PORTS];
    uint32_t flag[JZ4740_GPIO_NUM_PORTS];
    bool irq_level[JZ4740_GPIO_NUM_PORTS];
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_flag_offset;
    uint32_t last_flag_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    JZ4740GPIOInputSampleCallback input_sample_callback;
    void *input_sample_opaque;
    JZ4740GPIOTraceCallback trace_callback;
    void *trace_opaque;
};

static void gpio_notify_trace(JZ4740GPIOState *s, uint32_t reason)
{
    if (s->trace_callback) {
        s->trace_callback(s->trace_opaque, reason);
    }
}

static uint32_t *gpio_jz4730_reg(JZ4740GPIOState *s, unsigned port,
                                 hwaddr reg_offset)
{
    hwaddr offset = port * JZ4730_GPIO_PORT_STRIDE + reg_offset;

    return &s->regs[offset / sizeof(uint32_t)];
}

static unsigned gpio_jz4730_trigger(JZ4740GPIOState *s, unsigned port,
                                    unsigned pin)
{
    uint32_t value;

    if (pin < 16) {
        value = *gpio_jz4730_reg(s, port, JZ4730_GPIO_ITL);
        return (value >> (pin * 2)) & 3u;
    }
    value = *gpio_jz4730_reg(s, port, JZ4730_GPIO_ITH);
    return (value >> ((pin - 16) * 2)) & 3u;
}

static uint32_t gpio_jz4730_edge_mask(JZ4740GPIOState *s, unsigned port)
{
    uint32_t mask = 0;

    for (unsigned pin = 0; pin < JZ4740_GPIO_PINS_PER_PORT; pin++) {
        if (gpio_jz4730_trigger(s, port, pin) >= 2) {
            mask |= 1u << pin;
        }
    }
    return mask;
}

static void gpio_jz4730_recompute_levels(JZ4740GPIOState *s, unsigned port)
{
    uint32_t input = s->input_level[port];
    uint32_t enabled = *gpio_jz4730_reg(s, port, JZ4730_GPIO_IE);

    for (unsigned pin = 0; pin < JZ4740_GPIO_PINS_PER_PORT; pin++) {
        uint32_t bit = 1u << pin;
        unsigned trigger = gpio_jz4730_trigger(s, port, pin);

        if (!(enabled & bit) || trigger >= 2) {
            continue;
        }
        if ((trigger == 0 && !(input & bit)) ||
            (trigger == 1 && (input & bit))) {
            s->flag[port] |= bit;
        } else {
            s->flag[port] &= ~bit;
        }
    }
}

static void gpio_update_irq(JZ4740GPIOState *s, unsigned port)
{
    bool level;

    if (port >= JZ4740_GPIO_NUM_PORTS) {
        return;
    }
    if (s->jz4730_layout) {
        uint32_t enabled = *gpio_jz4730_reg(s, port, JZ4730_GPIO_IE);
        uint32_t masked = *gpio_jz4730_reg(s, port, JZ4730_GPIO_IM);

        gpio_jz4730_recompute_levels(s, port);
        level = (s->flag[port] & enabled & ~masked) != 0;
    } else {
        level = s->flag[port] != 0;
    }
    s->irq_level[port] = level;
    qemu_set_irq(s->irq[port], level);
}

bool jz4740_gpio_set_input_level(JZ4740GPIOState *s, unsigned port,
                                 uint32_t mask, bool high,
                                 bool latch_flag)
{
    uint32_t previous;
    uint32_t changed;

    if (!s || port >= JZ4740_GPIO_NUM_PORTS || mask == 0) {
        return false;
    }
    previous = s->input_level[port];
    if (high) {
        s->input_level[port] |= mask;
    } else {
        s->input_level[port] &= ~mask;
    }
    changed = previous ^ s->input_level[port];
    if (changed && latch_flag) {
        if (s->jz4730_layout) {
            uint32_t enabled = *gpio_jz4730_reg(s, port, JZ4730_GPIO_IE);

            for (unsigned pin = 0; pin < JZ4740_GPIO_PINS_PER_PORT; pin++) {
                uint32_t bit = 1u << pin;
                unsigned trigger;

                if (!(changed & enabled & bit)) {
                    continue;
                }
                trigger = gpio_jz4730_trigger(s, port, pin);
                if ((trigger == 2 && (previous & bit) &&
                     !(s->input_level[port] & bit)) ||
                    (trigger == 3 && !(previous & bit) &&
                     (s->input_level[port] & bit))) {
                    s->flag[port] |= bit;
                }
            }
        } else {
            s->flag[port] |= changed;
        }
        gpio_update_irq(s, port);
    }
    if (changed) {
        gpio_notify_trace(s, 1u);
    }
    return changed != 0;
}

void jz4740_gpio_raise_flag(JZ4740GPIOState *s, unsigned port,
                            uint32_t mask)
{
    if (!s || port >= JZ4740_GPIO_NUM_PORTS || mask == 0) {
        return;
    }
    s->flag[port] |= mask;
    gpio_update_irq(s, port);
    gpio_notify_trace(s, 2u);
}

uint32_t jz4740_gpio_flag(JZ4740GPIOState *s, unsigned port)
{
    return s && port < JZ4740_GPIO_NUM_PORTS ? s->flag[port] : 0;
}

void jz4740_gpio_get_diagnostics(JZ4740GPIOState *s,
                                 JZ4740GPIODiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    memcpy(diagnostics->input_level, s->input_level,
           sizeof(diagnostics->input_level));
    memcpy(diagnostics->flag, s->flag, sizeof(diagnostics->flag));
    memcpy(diagnostics->irq_level, s->irq_level,
           sizeof(diagnostics->irq_level));
    diagnostics->last_read_offset = s->last_read_offset;
    diagnostics->last_read_value = s->last_read_value;
    diagnostics->last_flag_offset = s->last_flag_offset;
    diagnostics->last_flag_value = s->last_flag_value;
    diagnostics->last_write_offset = s->last_write_offset;
    diagnostics->last_write_value = s->last_write_value;
}

void jz4740_gpio_set_input_sample_callback(
    JZ4740GPIOState *s, JZ4740GPIOInputSampleCallback callback,
    void *opaque)
{
    if (!s) {
        return;
    }
    s->input_sample_callback = callback;
    s->input_sample_opaque = opaque;
}

void jz4740_gpio_set_trace_callback(JZ4740GPIOState *s,
                                    JZ4740GPIOTraceCallback callback,
                                    void *opaque)
{
    if (!s) {
        return;
    }
    s->trace_callback = callback;
    s->trace_opaque = opaque;
}

static void gpio_pin_input(void *opaque, int n, int level)
{
    JZ4740GPIOState *s = opaque;
    unsigned port = (unsigned)n / JZ4740_GPIO_PINS_PER_PORT;
    unsigned pin = (unsigned)n % JZ4740_GPIO_PINS_PER_PORT;

    jz4740_gpio_set_input_level(s, port, 1u << pin, level != 0, true);
}

static uint64_t gpio_extract32(uint32_t value, hwaddr offset, unsigned size)
{
    unsigned shift = (offset & 3u) * 8u;

    value >>= shift;
    switch (size) {
    case 1:
        return value & 0xffu;
    case 2:
        return value & 0xffffu;
    default:
        return value;
    }
}

static uint64_t gpio_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740GPIOState *s = opaque;
    hwaddr aligned_offset = offset & ~3u;
    unsigned port;
    hwaddr reg_offset;
    uint32_t value;

    if (s->jz4730_layout) {
        if (aligned_offset >= JZ4730_GPIO_MODELED_SIZE) {
            return 0;
        }
        port = aligned_offset / JZ4730_GPIO_PORT_STRIDE;
        reg_offset = aligned_offset % JZ4730_GPIO_PORT_STRIDE;
        value = s->regs[aligned_offset / sizeof(uint32_t)];
        if (reg_offset == JZ4730_GPIO_DAT) {
            uint32_t direction = *gpio_jz4730_reg(
                s, port, JZ4730_GPIO_DIR);

            value = (value & direction) |
                    (s->input_level[port] & ~direction);
            if (s->input_sample_callback) {
                value = s->input_sample_callback(s->input_sample_opaque,
                                                 port, value);
            }
            s->last_read_offset = offset;
            s->last_read_value = value;
            gpio_notify_trace(s, 11u);
        } else if (reg_offset == JZ4730_GPIO_IRQ) {
            gpio_jz4730_recompute_levels(s, port);
            value = s->flag[port];
            s->last_flag_offset = offset;
            s->last_flag_value = value;
            gpio_notify_trace(s, 12u);
        }
        return gpio_extract32(value, offset, size);
    }

    if (aligned_offset >= GPIO_MODELED_SIZE) {
        return 0;
    }
    port = aligned_offset / GPIO_PORT_STRIDE;
    reg_offset = aligned_offset & (GPIO_PORT_STRIDE - 1u);
    value = s->regs[aligned_offset / sizeof(uint32_t)];
    if ((offset & (GPIO_PORT_STRIDE - 1u)) == GPIO_PIN) {
        value |= s->input_level[port];
        if (s->input_sample_callback) {
            value = s->input_sample_callback(s->input_sample_opaque,
                                             port, value);
        }
        s->last_read_offset = offset;
        s->last_read_value = value;
        gpio_notify_trace(s, 11u);
    } else if ((offset & (GPIO_PORT_STRIDE - 1u)) == GPIO_FLG) {
        value |= s->flag[port];
        s->last_flag_offset = offset;
        s->last_flag_value = value;
        gpio_notify_trace(s, 12u);
    }
    (void)reg_offset;
    return gpio_extract32(value, offset, size);
}

static uint32_t *gpio_canonical_reg(JZ4740GPIOState *s, unsigned port,
                                    hwaddr reg_offset)
{
    hwaddr offset = port * GPIO_PORT_STRIDE + reg_offset;

    return &s->regs[offset / sizeof(uint32_t)];
}

static void gpio_write(void *opaque, hwaddr offset, uint64_t value,
                       unsigned size)
{
    JZ4740GPIOState *s = opaque;
    hwaddr aligned_offset = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    unsigned port;
    hwaddr reg_offset;
    uint32_t mask;
    uint32_t lane_value;
    uint32_t *write_only;
    uint32_t *reg = NULL;

    if (s->jz4730_layout && aligned_offset >= JZ4730_GPIO_MODELED_SIZE) {
        return;
    }
    if (!s->jz4730_layout && aligned_offset >= GPIO_MODELED_SIZE) {
        return;
    }
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
    lane_value = ((uint32_t)value << shift) & mask;

    if (s->jz4730_layout) {
        uint32_t *target;

        port = aligned_offset / JZ4730_GPIO_PORT_STRIDE;
        reg_offset = aligned_offset % JZ4730_GPIO_PORT_STRIDE;
        target = &s->regs[aligned_offset / sizeof(uint32_t)];
        switch (reg_offset) {
        case JZ4730_GPIO_DAT:
        case JZ4730_GPIO_DIR:
        case JZ4730_GPIO_PE:
        case JZ4730_GPIO_FSL:
        case JZ4730_GPIO_FSH:
        case JZ4730_GPIO_ITL:
        case JZ4730_GPIO_ITH:
        case JZ4730_GPIO_IE:
        case JZ4730_GPIO_IM:
            *target = (*target & ~mask) | lane_value;
            break;
        case JZ4730_GPIO_IRQ:
            s->flag[port] &= ~(lane_value &
                               gpio_jz4730_edge_mask(s, port));
            break;
        default:
            return;
        }
        gpio_update_irq(s, port);
        s->last_write_offset = offset;
        s->last_write_value = lane_value;
        gpio_notify_trace(s, 8u);
        return;
    }

    write_only = &s->regs[aligned_offset / sizeof(uint32_t)];
    *write_only = (*write_only & ~mask) | lane_value;
    port = aligned_offset / GPIO_PORT_STRIDE;
    reg_offset = aligned_offset & (GPIO_PORT_STRIDE - 1u);
    switch (reg_offset) {
    case GPIO_DATS:
        reg = gpio_canonical_reg(s, port, GPIO_DAT);
        *reg |= lane_value;
        s->flag[port] &= ~lane_value;
        gpio_update_irq(s, port);
        *write_only = 0;
        break;
    case GPIO_DATC:
        reg = gpio_canonical_reg(s, port, GPIO_DAT);
        *reg &= ~lane_value;
        *write_only = 0;
        break;
    case GPIO_IMS:
        reg = gpio_canonical_reg(s, port, GPIO_IM);
        *reg |= lane_value;
        *write_only = 0;
        break;
    case GPIO_IMC:
        reg = gpio_canonical_reg(s, port, GPIO_IM);
        *reg &= ~lane_value;
        *write_only = 0;
        break;
    case GPIO_PES:
        reg = gpio_canonical_reg(s, port, GPIO_PE);
        *reg |= lane_value;
        *write_only = 0;
        break;
    case GPIO_PEC:
        reg = gpio_canonical_reg(s, port, GPIO_PE);
        *reg &= ~lane_value;
        *write_only = 0;
        break;
    case GPIO_FUNS:
        reg = gpio_canonical_reg(s, port, GPIO_FUN);
        *reg |= lane_value;
        *write_only = 0;
        break;
    case GPIO_FUNC:
        reg = gpio_canonical_reg(s, port, GPIO_FUN);
        *reg &= ~lane_value;
        *write_only = 0;
        break;
    case GPIO_SELS:
        reg = gpio_canonical_reg(s, port, GPIO_SEL);
        *reg |= lane_value;
        *write_only = 0;
        break;
    case GPIO_SELC:
        reg = gpio_canonical_reg(s, port, GPIO_SEL);
        *reg &= ~lane_value;
        *write_only = 0;
        break;
    case GPIO_DIRS:
        reg = gpio_canonical_reg(s, port, GPIO_DIR);
        *reg |= lane_value;
        *write_only = 0;
        break;
    case GPIO_DIRC:
        reg = gpio_canonical_reg(s, port, GPIO_DIR);
        *reg &= ~lane_value;
        *write_only = 0;
        break;
    case GPIO_TRGS:
        reg = gpio_canonical_reg(s, port, GPIO_TRG);
        *reg |= lane_value;
        *write_only = 0;
        break;
    case GPIO_TRGC:
        reg = gpio_canonical_reg(s, port, GPIO_TRG);
        *reg &= ~lane_value;
        *write_only = 0;
        break;
    default:
        break;
    }
    s->last_write_offset = offset;
    s->last_write_value = lane_value;
    gpio_notify_trace(s, 8u);
}

static const MemoryRegionOps gpio_ops = {
    .read = gpio_read,
    .write = gpio_write,
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

static void gpio_reset_hold(Object *obj, ResetType type)
{
    JZ4740GPIOState *s = JZ4740_GPIO(obj);

    memset(s->regs, 0, sizeof(s->regs));
    memset(s->flag, 0, sizeof(s->flag));
    memset(s->irq_level, 0, sizeof(s->irq_level));
    memcpy(s->input_level, s->input_reset, sizeof(s->input_level));
    s->last_read_offset = 0;
    s->last_read_value = 0;
    s->last_flag_offset = 0;
    s->last_flag_value = 0;
    s->last_write_offset = 0;
    s->last_write_value = 0;
    for (unsigned port = 0; port < JZ4740_GPIO_NUM_PORTS; port++) {
        if (s->jz4730_layout) {
            *gpio_jz4730_reg(s, port, JZ4730_GPIO_PE) = 0xffffffffu;
        } else {
            *gpio_canonical_reg(s, port, GPIO_IM) = GPIO_IM_RESET;
        }
        qemu_set_irq(s->irq[port], 0);
    }
}

static int gpio_post_load(void *opaque, int version_id)
{
    JZ4740GPIOState *s = opaque;

    for (unsigned port = 0; port < JZ4740_GPIO_NUM_PORTS; port++) {
        gpio_update_irq(s, port);
    }
    return 0;
}

static const VMStateDescription vmstate_jz4740_gpio = {
    .name = TYPE_JZ4740_GPIO,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = gpio_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4740GPIOState, GPIO_REG_WORDS),
        VMSTATE_UINT32_ARRAY(input_level, JZ4740GPIOState,
                             JZ4740_GPIO_NUM_PORTS),
        VMSTATE_UINT32_ARRAY(input_reset, JZ4740GPIOState,
                             JZ4740_GPIO_NUM_PORTS),
        VMSTATE_UINT32_ARRAY(flag, JZ4740GPIOState, JZ4740_GPIO_NUM_PORTS),
        VMSTATE_UINT32(last_read_offset, JZ4740GPIOState),
        VMSTATE_UINT32(last_read_value, JZ4740GPIOState),
        VMSTATE_UINT32(last_flag_offset, JZ4740GPIOState),
        VMSTATE_UINT32(last_flag_value, JZ4740GPIOState),
        VMSTATE_UINT32(last_write_offset, JZ4740GPIOState),
        VMSTATE_UINT32(last_write_value, JZ4740GPIOState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property gpio_properties[] = {
    DEFINE_PROP_BOOL("jz4730-layout", JZ4740GPIOState,
                     jz4730_layout, false),
    DEFINE_PROP_UINT32("input-reset-a", JZ4740GPIOState,
                       input_reset[JZ4740_GPIO_PORT_A], 0),
    DEFINE_PROP_UINT32("input-reset-b", JZ4740GPIOState,
                       input_reset[JZ4740_GPIO_PORT_B], 0),
    DEFINE_PROP_UINT32("input-reset-c", JZ4740GPIOState,
                       input_reset[JZ4740_GPIO_PORT_C], 0),
    DEFINE_PROP_UINT32("input-reset-d", JZ4740GPIOState,
                       input_reset[JZ4740_GPIO_PORT_D], 0),
};

static void gpio_init(Object *obj)
{
    JZ4740GPIOState *s = JZ4740_GPIO(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &gpio_ops, s, TYPE_JZ4740_GPIO,
                          GPIO_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    for (unsigned port = 0; port < JZ4740_GPIO_NUM_PORTS; port++) {
        sysbus_init_irq(sbd, &s->irq[port]);
    }
    qdev_init_gpio_in_named(DEVICE(obj), gpio_pin_input, "pin",
                            JZ4740_GPIO_NUM_PORTS *
                            JZ4740_GPIO_PINS_PER_PORT);
}

static void gpio_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_gpio;
    device_class_set_props(dc, gpio_properties);
    set_bit(DEVICE_CATEGORY_INPUT, dc->categories);
    rc->phases.hold = gpio_reset_hold;
}

static const TypeInfo gpio_type_info = {
    .name = TYPE_JZ4740_GPIO,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740GPIOState),
    .instance_init = gpio_init,
    .class_init = gpio_class_init,
};

static void gpio_register_types(void)
{
    type_register_static(&gpio_type_info);
}

type_init(gpio_register_types)
