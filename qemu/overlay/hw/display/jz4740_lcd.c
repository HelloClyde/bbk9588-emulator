/*
 * Ingenic JZ4740 LCD controller.
 *
 * The controller owns the software-visible register block, descriptor DMA,
 * SOF/EOF state and interrupt output.  Panel scanout remains a board concern:
 * the BBK 9588 machine consumes the selected RGB565 framebuffer through the
 * frame-source callback.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "system/address-spaces.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/display/jz4740_lcd.h"
#include "migration/vmstate.h"
#include "qemu/bswap.h"
#include "qemu/error-report.h"
#include "qemu/module.h"
#include "qemu/units.h"

#define JZ4740_LCD_MMIO_SIZE       0x1000u
#define JZ4740_LCD_REGS            (JZ4740_LCD_MMIO_SIZE / sizeof(uint32_t))
#define JZ4740_LCD_PHYS_MASK       0x1fffffffu
#define JZ4740_LCD_DEFAULT_RAM_SIZE (160u * MiB)
#define JZ4740_LCD_DEFAULT_FRAME_BYTES (240u * 320u * 2u)

#define LCD_CFG                    0x00u
#define LCD_VSYNC                  0x04u
#define LCD_HSYNC                  0x08u
#define LCD_VAT                    0x0cu
#define LCD_DAH                    0x10u
#define LCD_DAV                    0x14u
#define LCD_PS                     0x18u
#define LCD_CLS                    0x1cu
#define LCD_SPL                    0x20u
#define LCD_REV                    0x24u
#define LCD_CTRL                   0x30u
#define LCD_STATE                  0x34u
#define LCD_IID                    0x38u
#define LCD_DA0                    0x40u
#define LCD_SA0                    0x44u
#define LCD_FID0                   0x48u
#define LCD_CMD0                   0x4cu
#define LCD_DA1                    0x50u
#define LCD_SA1                    0x54u
#define LCD_FID1                   0x58u
#define LCD_CMD1                   0x5cu

#define LCD_CTRL_EOFM              0x00002000u
#define LCD_CTRL_SOFM              0x00001000u
#define LCD_CTRL_OFUM              0x00000800u
#define LCD_CTRL_IFUM0             0x00000400u
#define LCD_CTRL_IFUM1             0x00000200u
#define LCD_CTRL_LDDM              0x00000100u
#define LCD_CTRL_QDM               0x00000080u
#define LCD_CTRL_DIS               0x00000010u
#define LCD_CTRL_ENA               0x00000008u
#define LCD_CTRL_RW_MASK           0x3fff3fffu
#define LCD_CFG_RW_MASK            0x80ffffbfu
#define LCD_VSYNC_RW_MASK          0x000007ffu
#define LCD_TIMING_RW_MASK         0x07ff07ffu
#define LCD_REV_RW_MASK            0x07ff0000u

#define LCD_STATE_QD               0x00000080u
#define LCD_STATE_EOF              0x00000020u
#define LCD_STATE_SOF              0x00000010u
#define LCD_STATE_OUF              0x00000008u
#define LCD_STATE_IFU0             0x00000004u
#define LCD_STATE_IFU1             0x00000002u
#define LCD_STATE_LDD              0x00000001u
#define LCD_STATE_MASK             0x000000bfu

#define LCD_DA_ALIGN_MASK          0x0000000fu
#define LCD_CMD_SOFINT             0x80000000u
#define LCD_CMD_EOFINT             0x40000000u
#define LCD_CMD_PAL                0x10000000u
#define LCD_CMD_LEN_MASK           0x00ffffffu
#define LCD_CMD_RW_MASK            \
    (LCD_CMD_SOFINT | LCD_CMD_EOFINT | LCD_CMD_PAL | LCD_CMD_LEN_MASK)

#define LCD_DESC_BYTES             16u
#define LCD_DESC_NEXT              0x00u
#define LCD_DESC_SOURCE            0x04u
#define LCD_DESC_FID               0x08u
#define LCD_DESC_CMD               0x0cu

struct JZ4740LCDState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t regs[JZ4740_LCD_REGS];
    uint32_t command[2];
    uint32_t descriptor_address;
    uint32_t framebuffer_address;
    uint64_t ram_size;
    uint32_t frame_bytes;
    bool descriptor_valid;
    bool framebuffer_valid;
    bool irq_level;
    bool trace_enabled;
    uint32_t trace_count;

    JZ4740LCDFrameSourceCallback frame_source_callback;
    void *frame_source_opaque;
};

static uint32_t jz4740_lcd_reg(JZ4740LCDState *s, hwaddr offset)
{
    return s->regs[offset / sizeof(uint32_t)];
}

static void jz4740_lcd_set_reg(JZ4740LCDState *s, hwaddr offset,
                               uint32_t value)
{
    s->regs[offset / sizeof(uint32_t)] = value;
}

static bool jz4740_lcd_irq_pending(JZ4740LCDState *s)
{
    uint32_t state = jz4740_lcd_reg(s, LCD_STATE);
    uint32_t ctrl = jz4740_lcd_reg(s, LCD_CTRL);

    return ((state & LCD_STATE_EOF) && (ctrl & LCD_CTRL_EOFM)) ||
           ((state & LCD_STATE_SOF) && (ctrl & LCD_CTRL_SOFM)) ||
           ((state & LCD_STATE_OUF) && (ctrl & LCD_CTRL_OFUM)) ||
           ((state & LCD_STATE_IFU0) && (ctrl & LCD_CTRL_IFUM0)) ||
           ((state & LCD_STATE_IFU1) && (ctrl & LCD_CTRL_IFUM1)) ||
           ((state & LCD_STATE_LDD) && (ctrl & LCD_CTRL_LDDM)) ||
           ((state & LCD_STATE_QD) && (ctrl & LCD_CTRL_QDM));
}

static void jz4740_lcd_update_irq(JZ4740LCDState *s)
{
    bool level = jz4740_lcd_irq_pending(s);

    if (level != s->irq_level) {
        s->irq_level = level;
        qemu_set_irq(s->irq, level);
    }
}

static void jz4740_lcd_latch_iid(JZ4740LCDState *s, uint32_t state_bit,
                                 uint32_t ctrl_bit, uint32_t fid)
{
    if ((jz4740_lcd_reg(s, LCD_CTRL) & ctrl_bit) &&
        !jz4740_lcd_irq_pending(s)) {
        jz4740_lcd_set_reg(s, LCD_IID, fid);
    }
    jz4740_lcd_set_reg(s, LCD_STATE,
                       jz4740_lcd_reg(s, LCD_STATE) | state_bit);
}

static bool jz4740_lcd_guest_range_valid(JZ4740LCDState *s, uint32_t va,
                                         size_t bytes)
{
    uint64_t phys = va & JZ4740_LCD_PHYS_MASK;

    return phys >= 0x1000u && phys < s->ram_size &&
           bytes <= s->ram_size - phys;
}

static bool jz4740_lcd_candidate_va(JZ4740LCDState *s, uint32_t value,
                                    size_t bytes, uint32_t *resolved)
{
    uint32_t candidates[] = {
        value,
        0x80000000u | (value & JZ4740_LCD_PHYS_MASK),
        0xa0000000u | (value & JZ4740_LCD_PHYS_MASK),
    };

    for (unsigned i = 0; i < ARRAY_SIZE(candidates); i++) {
        uint32_t candidate = candidates[i];

        if ((candidate & 3u) != 0 ||
            !jz4740_lcd_guest_range_valid(s, candidate, bytes)) {
            continue;
        }
        *resolved = candidate;
        return true;
    }
    return false;
}

static bool jz4740_lcd_read_le32(JZ4740LCDState *s, uint32_t va,
                                 uint32_t *value)
{
    uint32_t raw = 0;
    MemTxResult result;

    if (!jz4740_lcd_guest_range_valid(s, va, sizeof(raw))) {
        return false;
    }
    result = address_space_read(&address_space_memory,
                                va & JZ4740_LCD_PHYS_MASK,
                                MEMTXATTRS_UNSPECIFIED, &raw, sizeof(raw));
    if (result != MEMTX_OK) {
        return false;
    }
    *value = le32_to_cpu(raw);
    return true;
}

static void jz4740_lcd_notify_frame_source(JZ4740LCDState *s)
{
    if (s->frame_source_callback) {
        s->frame_source_callback(s->frame_source_opaque);
    }
}

static bool jz4740_lcd_fetch_descriptor(JZ4740LCDState *s,
                                        unsigned channel)
{
    hwaddr base = channel ? LCD_DA1 : LCD_DA0;
    uint32_t descriptor;
    uint32_t next;
    uint32_t source;
    uint32_t fid;
    uint32_t command;
    uint32_t framebuffer;

    if (!jz4740_lcd_candidate_va(s, jz4740_lcd_reg(s, base),
                                 LCD_DESC_BYTES, &descriptor) ||
        !jz4740_lcd_read_le32(s, descriptor + LCD_DESC_NEXT, &next) ||
        !jz4740_lcd_read_le32(s, descriptor + LCD_DESC_SOURCE, &source) ||
        !jz4740_lcd_read_le32(s, descriptor + LCD_DESC_FID, &fid) ||
        !jz4740_lcd_read_le32(s, descriptor + LCD_DESC_CMD, &command)) {
        return false;
    }

    command &= LCD_CMD_RW_MASK;
    jz4740_lcd_set_reg(s, base + LCD_DESC_NEXT, next);
    jz4740_lcd_set_reg(s, base + LCD_DESC_SOURCE, source);
    jz4740_lcd_set_reg(s, base + LCD_DESC_FID, fid);
    jz4740_lcd_set_reg(s, base + LCD_DESC_CMD, command);
    s->command[channel] = command;
    s->descriptor_address = descriptor;
    s->descriptor_valid = true;
    if (command & LCD_CMD_SOFINT) {
        jz4740_lcd_latch_iid(s, LCD_STATE_SOF, LCD_CTRL_SOFM, fid);
    }
    if (!jz4740_lcd_candidate_va(s, source, s->frame_bytes,
                                 &framebuffer)) {
        return false;
    }
    s->framebuffer_address = framebuffer;
    s->framebuffer_valid = true;
    return true;
}

static void jz4740_lcd_finish_channel(JZ4740LCDState *s, unsigned channel)
{
    hwaddr base = channel ? LCD_DA1 : LCD_DA0;
    uint32_t command;

    if (channel >= ARRAY_SIZE(s->command)) {
        return;
    }
    command = s->command[channel];
    if (command == 0) {
        return;
    }

    s->command[channel] = command & ~LCD_CMD_LEN_MASK;
    jz4740_lcd_set_reg(s, base + LCD_DESC_CMD, s->command[channel]);
    if (command & LCD_CMD_EOFINT) {
        jz4740_lcd_latch_iid(s, LCD_STATE_EOF, LCD_CTRL_EOFM,
                             jz4740_lcd_reg(s, base + LCD_DESC_FID));
    }
    if (jz4740_lcd_reg(s, base) != 0) {
        jz4740_lcd_fetch_descriptor(s, channel);
    }
}

static bool jz4740_lcd_dma_reg_readonly(hwaddr offset)
{
    switch (offset) {
    case LCD_IID:
    case LCD_SA0:
    case LCD_FID0:
    case LCD_CMD0:
    case LCD_SA1:
    case LCD_FID1:
    case LCD_CMD1:
        return true;
    default:
        return false;
    }
}

static void jz4740_lcd_trace_write(JZ4740LCDState *s, hwaddr offset,
                                   uint64_t value, unsigned size)
{
    if (!s->trace_enabled || s->trace_count++ >= 4096) {
        return;
    }
    error_report(
        "jz4740-lcd[%u] off=0x%04" HWADDR_PRIx
        " size=%u value=0x%08" PRIx64
        " ctrl=0x%08x state=0x%08x iid=0x%08x"
        " desc=0x%08x fb=0x%08x",
        s->trace_count - 1, offset, size, value,
        jz4740_lcd_reg(s, LCD_CTRL), jz4740_lcd_reg(s, LCD_STATE),
        jz4740_lcd_reg(s, LCD_IID), s->descriptor_address,
        s->framebuffer_address);
}

static uint64_t jz4740_lcd_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740LCDState *s = JZ4740_LCD(opaque);
    hwaddr reg = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t value = jz4740_lcd_reg(s, reg) >> shift;

    if (size == 1) {
        return value & 0xffu;
    }
    if (size == 2) {
        return value & 0xffffu;
    }
    return value;
}

static void jz4740_lcd_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    JZ4740LCDState *s = JZ4740_LCD(opaque);
    hwaddr reg = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t lane_mask;
    uint32_t lane_value;
    uint32_t old_value = jz4740_lcd_reg(s, reg);
    uint32_t new_value;
    bool frame_source_changed = false;

    if (size == 1) {
        lane_mask = 0xffu << shift;
    } else if (size == 2) {
        lane_mask = 0xffffu << shift;
    } else {
        lane_mask = 0xffffffffu;
        shift = 0;
    }
    lane_value = ((uint32_t)value << shift) & lane_mask;
    new_value = (old_value & ~lane_mask) | lane_value;
    jz4740_lcd_set_reg(s, reg, new_value);
    jz4740_lcd_trace_write(s, offset, value, size);

    switch (reg) {
    case LCD_CFG:
        jz4740_lcd_set_reg(s, reg, new_value & LCD_CFG_RW_MASK);
        break;
    case LCD_VSYNC:
        jz4740_lcd_set_reg(s, reg, new_value & LCD_VSYNC_RW_MASK);
        break;
    case LCD_HSYNC:
    case LCD_VAT:
    case LCD_DAH:
    case LCD_DAV:
    case LCD_PS:
    case LCD_CLS:
    case LCD_SPL:
        jz4740_lcd_set_reg(s, reg, new_value & LCD_TIMING_RW_MASK);
        break;
    case LCD_REV:
        jz4740_lcd_set_reg(s, reg, new_value & LCD_REV_RW_MASK);
        break;
    case LCD_CTRL:
        new_value &= LCD_CTRL_RW_MASK;
        if (new_value & LCD_CTRL_DIS) {
            new_value &= ~LCD_CTRL_ENA;
            jz4740_lcd_set_reg(s, LCD_STATE,
                               jz4740_lcd_reg(s, LCD_STATE) | LCD_STATE_LDD);
        } else if ((old_value & LCD_CTRL_ENA) &&
                   !(new_value & LCD_CTRL_ENA)) {
            jz4740_lcd_set_reg(s, LCD_STATE,
                               jz4740_lcd_reg(s, LCD_STATE) | LCD_STATE_QD);
        }
        jz4740_lcd_set_reg(s, LCD_CTRL, new_value);
        if (new_value & LCD_CTRL_ENA) {
            jz4740_lcd_set_reg(
                s, LCD_STATE,
                jz4740_lcd_reg(s, LCD_STATE) & ~(LCD_STATE_LDD | LCD_STATE_QD));
            frame_source_changed |= jz4740_lcd_fetch_descriptor(s, 0);
            if (jz4740_lcd_reg(s, LCD_DA1) != 0) {
                frame_source_changed |= jz4740_lcd_fetch_descriptor(s, 1);
            }
        }
        break;
    case LCD_STATE:
        jz4740_lcd_set_reg(s, LCD_STATE,
                           old_value & ~(new_value & LCD_STATE_MASK));
        break;
    case LCD_DA0:
    case LCD_DA1:
        jz4740_lcd_set_reg(s, reg, new_value & ~LCD_DA_ALIGN_MASK);
        if (jz4740_lcd_reg(s, LCD_CTRL) & LCD_CTRL_ENA) {
            frame_source_changed = jz4740_lcd_fetch_descriptor(
                s, reg == LCD_DA1 ? 1 : 0);
        }
        break;
    default:
        if (jz4740_lcd_dma_reg_readonly(reg)) {
            jz4740_lcd_set_reg(s, reg, old_value);
        }
        break;
    }

    jz4740_lcd_update_irq(s);
    if (frame_source_changed) {
        jz4740_lcd_notify_frame_source(s);
    }
}

static const MemoryRegionOps jz4740_lcd_ops = {
    .read = jz4740_lcd_read,
    .write = jz4740_lcd_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
        .unaligned = false,
    },
};

void jz4740_lcd_get_diagnostics(JZ4740LCDState *s,
                                JZ4740LCDDiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    diagnostics->control = jz4740_lcd_reg(s, LCD_CTRL);
    diagnostics->state = jz4740_lcd_reg(s, LCD_STATE);
    diagnostics->interrupt_id = jz4740_lcd_reg(s, LCD_IID);
    diagnostics->descriptor_address = s->descriptor_address;
    diagnostics->framebuffer_address = s->framebuffer_address;
    diagnostics->descriptor_valid = s->descriptor_valid;
    diagnostics->framebuffer_valid = s->framebuffer_valid;
    diagnostics->irq_level = s->irq_level;
}

void jz4740_lcd_set_frame_source_callback(
    JZ4740LCDState *s, JZ4740LCDFrameSourceCallback callback, void *opaque)
{
    if (!s) {
        return;
    }
    s->frame_source_callback = callback;
    s->frame_source_opaque = opaque;
}

void jz4740_lcd_set_trace_enabled(JZ4740LCDState *s, bool enabled)
{
    if (!s) {
        return;
    }
    s->trace_enabled = enabled;
    if (!enabled) {
        s->trace_count = 0;
    }
}

bool jz4740_lcd_get_frame_source(JZ4740LCDState *s, uint32_t *frame_va)
{
    if (!s || !s->framebuffer_valid ||
        !jz4740_lcd_guest_range_valid(s, s->framebuffer_address,
                                      s->frame_bytes)) {
        return false;
    }
    if (frame_va) {
        *frame_va = s->framebuffer_address;
    }
    return true;
}

bool jz4740_lcd_refresh_frame_source(JZ4740LCDState *s)
{
    uint32_t framebuffer;

    if (!s || !s->descriptor_valid ||
        !jz4740_lcd_read_le32(s, s->descriptor_address + LCD_DESC_SOURCE,
                              &framebuffer) ||
        !jz4740_lcd_candidate_va(s, framebuffer, s->frame_bytes,
                                 &framebuffer)) {
        return false;
    }
    s->framebuffer_address = framebuffer;
    s->framebuffer_valid = true;
    return true;
}

bool jz4740_lcd_observe_alias_write(JZ4740LCDState *s, hwaddr offset,
                                    uint32_t value)
{
    uint32_t address;

    if (!s) {
        return false;
    }
    if ((offset == LCD_DA0 || offset == LCD_DA1) &&
        jz4740_lcd_candidate_va(s, value, LCD_DESC_BYTES, &address)) {
        s->descriptor_address = address;
        s->descriptor_valid = true;
        if (jz4740_lcd_refresh_frame_source(s)) {
            jz4740_lcd_notify_frame_source(s);
            return true;
        }
        return false;
    }
    if (jz4740_lcd_candidate_va(s, value, s->frame_bytes, &address)) {
        s->framebuffer_address = address;
        s->framebuffer_valid = true;
        jz4740_lcd_notify_frame_source(s);
        return true;
    }
    return false;
}

void jz4740_lcd_signal_frame_done(JZ4740LCDState *s)
{
    if (!s || !(jz4740_lcd_reg(s, LCD_CTRL) & LCD_CTRL_ENA)) {
        return;
    }
    jz4740_lcd_finish_channel(s, 0);
    if (jz4740_lcd_reg(s, LCD_DA1) != 0 || s->command[1] != 0) {
        jz4740_lcd_finish_channel(s, 1);
    }
    jz4740_lcd_update_irq(s);
}

static void jz4740_lcd_reset_hold(Object *obj, ResetType type)
{
    JZ4740LCDState *s = JZ4740_LCD(obj);

    memset(s->regs, 0, sizeof(s->regs));
    memset(s->command, 0, sizeof(s->command));
    s->descriptor_address = 0;
    s->framebuffer_address = 0;
    s->descriptor_valid = false;
    s->framebuffer_valid = false;
    s->irq_level = false;
    s->trace_count = 0;
    qemu_set_irq(s->irq, 0);
}

static int jz4740_lcd_post_load(void *opaque, int version_id)
{
    JZ4740LCDState *s = opaque;

    s->irq_level = false;
    jz4740_lcd_update_irq(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_lcd = {
    .name = TYPE_JZ4740_LCD,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = jz4740_lcd_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4740LCDState, JZ4740_LCD_REGS),
        VMSTATE_UINT32_ARRAY(command, JZ4740LCDState, 2),
        VMSTATE_UINT32(descriptor_address, JZ4740LCDState),
        VMSTATE_UINT32(framebuffer_address, JZ4740LCDState),
        VMSTATE_BOOL(descriptor_valid, JZ4740LCDState),
        VMSTATE_BOOL(framebuffer_valid, JZ4740LCDState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property jz4740_lcd_properties[] = {
    DEFINE_PROP_UINT64("ram-size", JZ4740LCDState, ram_size,
                       JZ4740_LCD_DEFAULT_RAM_SIZE),
    DEFINE_PROP_UINT32("frame-bytes", JZ4740LCDState, frame_bytes,
                       JZ4740_LCD_DEFAULT_FRAME_BYTES),
};

static void jz4740_lcd_init(Object *obj)
{
    JZ4740LCDState *s = JZ4740_LCD(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &jz4740_lcd_ops, s,
                          TYPE_JZ4740_LCD, JZ4740_LCD_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void jz4740_lcd_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_lcd;
    device_class_set_props(dc, jz4740_lcd_properties);
    set_bit(DEVICE_CATEGORY_DISPLAY, dc->categories);
    rc->phases.hold = jz4740_lcd_reset_hold;
}

static const TypeInfo jz4740_lcd_type_info = {
    .name = TYPE_JZ4740_LCD,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740LCDState),
    .instance_init = jz4740_lcd_init,
    .class_init = jz4740_lcd_class_init,
};

static void jz4740_lcd_register_types(void)
{
    type_register_static(&jz4740_lcd_type_info);
}

type_init(jz4740_lcd_register_types)
