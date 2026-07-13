/*
 * BBK 9588 board panel/status interface.
 *
 * This is the board-specific register window at 0x10043000, not the JZ4740
 * LCD controller at 0x13050000.  It tracks the ready and frame-done status
 * consumed by the C200 firmware while scanout remains a machine host bridge.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/display/bbk9588_panel.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define PANEL_MMIO_SIZE            0x1000u
#define PANEL_REGS                 (PANEL_MMIO_SIZE / sizeof(uint32_t))
#define PANEL_CONTROL0             0x00u
#define PANEL_CONTROL1             0x04u
#define PANEL_CONTROL2             0x08u
#define PANEL_STATUS               0x0cu
#define PANEL_STATUS_FRAME_DONE    0x00000001u
#define PANEL_STATUS_READY         0x00000080u

struct Bbk9588PanelState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    uint32_t regs[PANEL_REGS];
    uint32_t status;
    uint32_t frame_status;
    Bbk9588PanelWriteCallback write_callback;
    void *write_opaque;
};

static uint32_t panel_reg(Bbk9588PanelState *s, hwaddr offset)
{
    return s->regs[offset / sizeof(uint32_t)];
}

static void panel_set_reg(Bbk9588PanelState *s, hwaddr offset,
                          uint32_t value)
{
    s->regs[offset / sizeof(uint32_t)] = value;
}

static uint64_t panel_read(void *opaque, hwaddr offset, unsigned size)
{
    Bbk9588PanelState *s = BBK9588_PANEL(opaque);
    hwaddr reg_offset = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t value;

    if (offset >= PANEL_MMIO_SIZE || size > PANEL_MMIO_SIZE - offset) {
        return 0;
    }
    value = reg_offset == PANEL_STATUS ?
            s->status | s->frame_status : panel_reg(s, reg_offset);
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

static void panel_write(void *opaque, hwaddr offset, uint64_t value,
                        unsigned size)
{
    Bbk9588PanelState *s = BBK9588_PANEL(opaque);
    hwaddr reg_offset = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t mask;
    uint32_t lane_value;
    uint32_t reg;

    if (offset >= PANEL_MMIO_SIZE || size > PANEL_MMIO_SIZE - offset) {
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

    if (reg_offset == PANEL_STATUS) {
        s->frame_status &= ~(lane_value & PANEL_STATUS_FRAME_DONE);
    } else {
        reg = panel_reg(s, reg_offset);
        panel_set_reg(s, reg_offset, (reg & ~mask) | lane_value);
        if (reg_offset == PANEL_CONTROL0 || reg_offset == PANEL_CONTROL1 ||
            reg_offset == PANEL_CONTROL2) {
            s->status |= PANEL_STATUS_READY;
        }
    }
    if (s->write_callback) {
        s->write_callback(s->write_opaque, offset, value, size);
    }
}

static const MemoryRegionOps panel_ops = {
    .read = panel_read,
    .write = panel_write,
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

void bbk9588_panel_set_write_callback(Bbk9588PanelState *s,
                                      Bbk9588PanelWriteCallback callback,
                                      void *opaque)
{
    s->write_callback = callback;
    s->write_opaque = opaque;
}

void bbk9588_panel_set_frame_done(Bbk9588PanelState *s)
{
    if (s) {
        s->frame_status |= PANEL_STATUS_FRAME_DONE;
    }
}

uint32_t bbk9588_panel_get_reg(Bbk9588PanelState *s, hwaddr offset)
{
    if (!s || offset >= PANEL_MMIO_SIZE) {
        return 0;
    }
    if ((offset & ~3u) == PANEL_STATUS) {
        return s->status | s->frame_status;
    }
    return panel_reg(s, offset & ~3u);
}

static void panel_reset_hold(Object *obj, ResetType type)
{
    Bbk9588PanelState *s = BBK9588_PANEL(obj);

    memset(s->regs, 0, sizeof(s->regs));
    s->status = 0;
    s->frame_status = 0;
}

static const VMStateDescription vmstate_bbk9588_panel = {
    .name = TYPE_BBK9588_PANEL,
    .version_id = 1,
    .minimum_version_id = 1,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, Bbk9588PanelState, PANEL_REGS),
        VMSTATE_UINT32(status, Bbk9588PanelState),
        VMSTATE_UINT32(frame_status, Bbk9588PanelState),
        VMSTATE_END_OF_LIST()
    },
};

static void panel_init(Object *obj)
{
    Bbk9588PanelState *s = BBK9588_PANEL(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &panel_ops, s,
                          TYPE_BBK9588_PANEL, PANEL_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
}

static void panel_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_bbk9588_panel;
    set_bit(DEVICE_CATEGORY_DISPLAY, dc->categories);
    rc->phases.hold = panel_reset_hold;
}

static const TypeInfo panel_type_info = {
    .name = TYPE_BBK9588_PANEL,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(Bbk9588PanelState),
    .instance_init = panel_init,
    .class_init = panel_class_init,
};

static void panel_register_types(void)
{
    type_register_static(&panel_type_info);
}

type_init(panel_register_types)
