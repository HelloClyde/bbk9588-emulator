/*
 * Ingenic JZ4740 clock and power manager.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/misc/jz4740_cpm.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define JZ4740_CPM_MMIO_SIZE 0x1000u
#define JZ4740_CPM_REGS      (JZ4740_CPM_MMIO_SIZE / sizeof(uint32_t))

#define JZ4740_CPM_CPCCR     0x00u
#define JZ4740_CPM_LCR       0x04u
#define JZ4740_CPM_CPPCR     0x10u
#define JZ4740_CPM_CLKGR     0x20u
#define JZ4740_CPM_SCR       0x24u
#define JZ4740_CPM_I2SCDR    0x60u
#define JZ4740_CPM_LPCDR     0x64u
#define JZ4740_CPM_MSCCDR    0x68u
#define JZ4740_CPM_UHCCDR    0x6cu
#define JZ4740_CPM_SSICDR    0x74u

#define JZ4740_CPM_CPCCR_RESET  0x00000008u
#define JZ4740_CPM_LCR_RESET    0x000000f8u
#define JZ4740_CPM_LCR_RW_MASK  0x000000ffu
#define JZ4740_CPM_CPPCR_RESET  0x28080011u
#define JZ4740_CPM_CPPCR_RW_MASK 0xffff03ffu
#define JZ4740_CPM_CPPCR_PLLS   0x00000400u
#define JZ4740_CPM_CPPCR_PLLEN  0x00000100u
#define JZ4740_CPM_CLKGR_RESET  0x00000000u
#define JZ4740_CPM_CLKGR_RW_MASK 0x0000ffffu
#define JZ4740_CPM_SCR_RESET    0x00001500u
#define JZ4740_CPM_SCR_RW_MASK  0x0000ffd0u
#define JZ4740_CPM_I2SCDR_RESET 0x00000004u
#define JZ4740_CPM_I2SCDR_RW_MASK 0x000001ffu
#define JZ4740_CPM_LPCDR_RESET  0x00000004u
#define JZ4740_CPM_LPCDR_RW_MASK 0x800007ffu
#define JZ4740_CPM_MSCCDR_RESET 0x00000004u
#define JZ4740_CPM_MSCCDR_RW_MASK 0x0000001fu
#define JZ4740_CPM_UHCCDR_RESET 0x00000004u
#define JZ4740_CPM_UHCCDR_RW_MASK 0x0000000fu
#define JZ4740_CPM_SSICDR_RESET 0x00000004u
#define JZ4740_CPM_SSICDR_RW_MASK 0x8000000fu

#define JZ4740_CPM_CLKGR_WAKE_MASK \
    (0x00000080u | 0x00000200u | 0x00000800u | 0x00004000u)
#define JZ4740_CPM_SCR_WAKE_MASK 0x00000080u

struct JZ4740CPMState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    uint32_t regs[JZ4740_CPM_REGS];
    JZ4740CPMUpdateFn update;
    void *update_opaque;
};

static bool jz4740_cpm_word_only_reg(hwaddr reg)
{
    switch (reg) {
    case JZ4740_CPM_CPCCR:
    case JZ4740_CPM_CPPCR:
    case JZ4740_CPM_I2SCDR:
    case JZ4740_CPM_LPCDR:
    case JZ4740_CPM_MSCCDR:
    case JZ4740_CPM_UHCCDR:
    case JZ4740_CPM_SSICDR:
        return true;
    default:
        return false;
    }
}

static uint32_t jz4740_cpm_mask_write(hwaddr reg, uint32_t value)
{
    switch (reg) {
    case JZ4740_CPM_CPCCR:
        return value;
    case JZ4740_CPM_LCR:
        return value & JZ4740_CPM_LCR_RW_MASK;
    case JZ4740_CPM_CPPCR:
        value &= JZ4740_CPM_CPPCR_RW_MASK;
        if (value & JZ4740_CPM_CPPCR_PLLEN) {
            value |= JZ4740_CPM_CPPCR_PLLS;
        }
        return value;
    case JZ4740_CPM_CLKGR:
        return value & JZ4740_CPM_CLKGR_RW_MASK;
    case JZ4740_CPM_SCR:
        return value & JZ4740_CPM_SCR_RW_MASK;
    case JZ4740_CPM_I2SCDR:
        return value & JZ4740_CPM_I2SCDR_RW_MASK;
    case JZ4740_CPM_LPCDR:
        return value & JZ4740_CPM_LPCDR_RW_MASK;
    case JZ4740_CPM_MSCCDR:
        return value & JZ4740_CPM_MSCCDR_RW_MASK;
    case JZ4740_CPM_UHCCDR:
        return value & JZ4740_CPM_UHCCDR_RW_MASK;
    case JZ4740_CPM_SSICDR:
        return value & JZ4740_CPM_SSICDR_RW_MASK;
    default:
        return value;
    }
}

static void jz4740_cpm_notify(JZ4740CPMState *s)
{
    if (s->update) {
        s->update(s->update_opaque);
    }
}

void jz4740_cpm_set_update(JZ4740CPMState *s,
                           JZ4740CPMUpdateFn update,
                           void *opaque)
{
    if (!s) {
        return;
    }
    s->update = update;
    s->update_opaque = opaque;
}

uint32_t jz4740_cpm_clkgr_wake_mask(JZ4740CPMState *s)
{
    return s ? s->regs[JZ4740_CPM_CLKGR / sizeof(uint32_t)] &
               JZ4740_CPM_CLKGR_WAKE_MASK : 0;
}

uint32_t jz4740_cpm_scr_wake_mask(JZ4740CPMState *s)
{
    return s ? s->regs[JZ4740_CPM_SCR / sizeof(uint32_t)] &
               JZ4740_CPM_SCR_WAKE_MASK : 0;
}

bool jz4740_cpm_wake_enabled(JZ4740CPMState *s)
{
    return jz4740_cpm_clkgr_wake_mask(s) != 0 ||
           jz4740_cpm_scr_wake_mask(s) != 0;
}

static uint64_t jz4740_cpm_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740CPMState *s = JZ4740_CPM(opaque);
    hwaddr reg = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t value = s->regs[reg / sizeof(uint32_t)] >> shift;

    if (size == 1) {
        return value & 0xffu;
    }
    if (size == 2) {
        return value & 0xffffu;
    }
    return value;
}

static void jz4740_cpm_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    JZ4740CPMState *s = JZ4740_CPM(opaque);
    hwaddr reg = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t index = reg / sizeof(uint32_t);
    uint32_t lane_mask;
    uint32_t merged;

    if (jz4740_cpm_word_only_reg(reg) &&
        ((offset & 3u) != 0 || size != sizeof(uint32_t))) {
        return;
    }
    if (size == 1) {
        lane_mask = 0xffu << shift;
    } else if (size == 2) {
        lane_mask = 0xffffu << shift;
    } else {
        lane_mask = 0xffffffffu;
        shift = 0;
    }
    merged = (s->regs[index] & ~lane_mask) |
             (((uint32_t)value << shift) & lane_mask);
    s->regs[index] = jz4740_cpm_mask_write(reg, merged);
    if (reg == JZ4740_CPM_CLKGR || reg == JZ4740_CPM_SCR) {
        jz4740_cpm_notify(s);
    }
}

static const MemoryRegionOps jz4740_cpm_ops = {
    .read = jz4740_cpm_read,
    .write = jz4740_cpm_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
};

static void jz4740_cpm_reset_hold(Object *obj, ResetType type)
{
    JZ4740CPMState *s = JZ4740_CPM(obj);

    memset(s->regs, 0, sizeof(s->regs));
    s->regs[JZ4740_CPM_CPCCR / sizeof(uint32_t)] = JZ4740_CPM_CPCCR_RESET;
    s->regs[JZ4740_CPM_LCR / sizeof(uint32_t)] = JZ4740_CPM_LCR_RESET;
    s->regs[JZ4740_CPM_CPPCR / sizeof(uint32_t)] = JZ4740_CPM_CPPCR_RESET;
    s->regs[JZ4740_CPM_CLKGR / sizeof(uint32_t)] = JZ4740_CPM_CLKGR_RESET;
    s->regs[JZ4740_CPM_SCR / sizeof(uint32_t)] = JZ4740_CPM_SCR_RESET;
    s->regs[JZ4740_CPM_I2SCDR / sizeof(uint32_t)] = JZ4740_CPM_I2SCDR_RESET;
    s->regs[JZ4740_CPM_LPCDR / sizeof(uint32_t)] = JZ4740_CPM_LPCDR_RESET;
    s->regs[JZ4740_CPM_MSCCDR / sizeof(uint32_t)] = JZ4740_CPM_MSCCDR_RESET;
    s->regs[JZ4740_CPM_UHCCDR / sizeof(uint32_t)] = JZ4740_CPM_UHCCDR_RESET;
    s->regs[JZ4740_CPM_SSICDR / sizeof(uint32_t)] = JZ4740_CPM_SSICDR_RESET;
}

static void jz4740_cpm_reset_exit(Object *obj, ResetType type)
{
    jz4740_cpm_notify(JZ4740_CPM(obj));
}

static int jz4740_cpm_post_load(void *opaque, int version_id)
{
    JZ4740CPMState *s = opaque;

    jz4740_cpm_notify(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_cpm = {
    .name = TYPE_JZ4740_CPM,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = jz4740_cpm_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4740CPMState, JZ4740_CPM_REGS),
        VMSTATE_END_OF_LIST()
    },
};

static void jz4740_cpm_init(Object *obj)
{
    JZ4740CPMState *s = JZ4740_CPM(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &jz4740_cpm_ops, s,
                          TYPE_JZ4740_CPM, JZ4740_CPM_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
}

static void jz4740_cpm_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_cpm;
    rc->phases.hold = jz4740_cpm_reset_hold;
    rc->phases.exit = jz4740_cpm_reset_exit;
}

static const TypeInfo jz4740_cpm_type_info = {
    .name = TYPE_JZ4740_CPM,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740CPMState),
    .instance_init = jz4740_cpm_init,
    .class_init = jz4740_cpm_class_init,
};

static void jz4740_cpm_register_types(void)
{
    type_register_static(&jz4740_cpm_type_info);
}

type_init(jz4740_cpm_register_types)
