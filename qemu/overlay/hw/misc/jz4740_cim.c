/*
 * Ingenic JZ4740 camera interface module.
 *
 * BBK 9588 has no attached camera sensor.  The model therefore implements
 * the software-visible idle controller, FIFO-empty state and interrupt
 * semantics without synthesizing image data or descriptor DMA.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/misc/jz4740_cim.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define CIM_MMIO_SIZE              0x1000u
#define CIM_CFG                    0x00u
#define CIM_CR                     0x04u
#define CIM_ST                     0x08u
#define CIM_IID                    0x0cu
#define CIM_RXFIFO                 0x10u
#define CIM_DA                     0x20u
#define CIM_FA                     0x24u
#define CIM_FID                    0x28u
#define CIM_CMD                    0x2cu

#define CIM_CFG_RW_MASK            0x0000f373u
#define CIM_CR_RW_MASK             0xff0f3f77u
#define CIM_DA_ALIGN_MASK          0x0000000fu

#define CIM_CR_VDDM                0x00002000u
#define CIM_CR_DMA_SOFM            0x00001000u
#define CIM_CR_DMA_EOFM            0x00000800u
#define CIM_CR_DMA_STOPM           0x00000400u
#define CIM_CR_RF_TRIGM            0x00000200u
#define CIM_CR_RF_OFM              0x00000100u
#define CIM_CR_RF_RST              0x00000002u
#define CIM_CR_ENA                 0x00000001u

#define CIM_ST_DMA_SOF             0x00000040u
#define CIM_ST_DMA_EOF             0x00000020u
#define CIM_ST_DMA_STOP            0x00000010u
#define CIM_ST_RF_OF               0x00000008u
#define CIM_ST_RF_TRIG             0x00000004u
#define CIM_ST_RF_EMPTY            0x00000002u
#define CIM_ST_VDD                 0x00000001u
#define CIM_ST_W0C_MASK            0x00000079u
#define CIM_ST_RESET               CIM_ST_RF_EMPTY

struct JZ4740CIMState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t config;
    uint32_t control;
    uint32_t status;
    uint32_t interrupt_id;
    uint32_t descriptor_address;
    uint32_t framebuffer_address;
    uint32_t frame_id;
    uint32_t command;
    bool irq_level;
};

static bool cim_irq_pending(JZ4740CIMState *s)
{
    return ((s->status & CIM_ST_DMA_SOF) &&
            (s->control & CIM_CR_DMA_SOFM)) ||
           ((s->status & CIM_ST_DMA_EOF) &&
            (s->control & CIM_CR_DMA_EOFM)) ||
           ((s->status & CIM_ST_DMA_STOP) &&
            (s->control & CIM_CR_DMA_STOPM)) ||
           ((s->status & CIM_ST_RF_OF) &&
            (s->control & CIM_CR_RF_OFM)) ||
           ((s->status & CIM_ST_RF_TRIG) &&
            (s->control & CIM_CR_RF_TRIGM)) ||
           ((s->status & CIM_ST_VDD) && (s->control & CIM_CR_VDDM));
}

static void cim_update_irq(JZ4740CIMState *s)
{
    bool level = cim_irq_pending(s);

    if (level != s->irq_level) {
        s->irq_level = level;
        qemu_set_irq(s->irq, level);
    }
}

static uint64_t cim_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740CIMState *s = JZ4740_CIM(opaque);

    switch (offset) {
    case CIM_CFG:
        return s->config;
    case CIM_CR:
        return s->control;
    case CIM_ST:
        return s->status;
    case CIM_IID:
        return s->interrupt_id;
    case CIM_RXFIFO:
        return 0;
    case CIM_DA:
        return s->descriptor_address;
    case CIM_FA:
        return s->framebuffer_address;
    case CIM_FID:
        return s->frame_id;
    case CIM_CMD:
        return s->command;
    default:
        return 0;
    }
}

static void cim_write_control(JZ4740CIMState *s, uint32_t value)
{
    uint32_t old_control = s->control;

    s->control = value & CIM_CR_RW_MASK;
    if (s->control & CIM_CR_RF_RST) {
        s->status &= ~(CIM_ST_DMA_SOF | CIM_ST_DMA_EOF |
                       CIM_ST_DMA_STOP | CIM_ST_RF_OF | CIM_ST_RF_TRIG);
        s->status |= CIM_ST_RF_EMPTY;
    }
    if (s->control & CIM_CR_ENA) {
        s->status &= ~CIM_ST_VDD;
    } else if (old_control & CIM_CR_ENA) {
        s->status |= CIM_ST_VDD;
    }
}

static void cim_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    JZ4740CIMState *s = JZ4740_CIM(opaque);

    switch (offset) {
    case CIM_CFG:
        s->config = value & CIM_CFG_RW_MASK;
        break;
    case CIM_CR:
        cim_write_control(s, value);
        break;
    case CIM_ST:
        s->status &= ((uint32_t)value | ~CIM_ST_W0C_MASK);
        s->status |= CIM_ST_RF_EMPTY;
        break;
    case CIM_DA:
        s->descriptor_address = value & ~CIM_DA_ALIGN_MASK;
        break;
    default:
        break;
    }
    cim_update_irq(s);
}

static const MemoryRegionOps cim_ops = {
    .read = cim_read,
    .write = cim_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 4,
        .max_access_size = 4,
        .unaligned = false,
    },
    .impl = {
        .min_access_size = 4,
        .max_access_size = 4,
    },
};

static void cim_reset_hold(Object *obj, ResetType type)
{
    JZ4740CIMState *s = JZ4740_CIM(obj);

    s->config = 0;
    s->control = 0;
    s->status = CIM_ST_RESET;
    s->interrupt_id = 0;
    s->descriptor_address = 0;
    s->framebuffer_address = 0;
    s->frame_id = 0;
    s->command = 0;
    s->irq_level = false;
    qemu_set_irq(s->irq, 0);
}

static int cim_post_load(void *opaque, int version_id)
{
    JZ4740CIMState *s = opaque;

    s->config &= CIM_CFG_RW_MASK;
    s->control &= CIM_CR_RW_MASK;
    s->status &= CIM_ST_W0C_MASK | CIM_ST_RF_TRIG | CIM_ST_RF_EMPTY;
    s->status |= CIM_ST_RF_EMPTY;
    s->descriptor_address &= ~CIM_DA_ALIGN_MASK;
    cim_update_irq(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_cim = {
    .name = TYPE_JZ4740_CIM,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = cim_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32(config, JZ4740CIMState),
        VMSTATE_UINT32(control, JZ4740CIMState),
        VMSTATE_UINT32(status, JZ4740CIMState),
        VMSTATE_UINT32(interrupt_id, JZ4740CIMState),
        VMSTATE_UINT32(descriptor_address, JZ4740CIMState),
        VMSTATE_UINT32(framebuffer_address, JZ4740CIMState),
        VMSTATE_UINT32(frame_id, JZ4740CIMState),
        VMSTATE_UINT32(command, JZ4740CIMState),
        VMSTATE_END_OF_LIST()
    },
};

static void cim_init(Object *obj)
{
    JZ4740CIMState *s = JZ4740_CIM(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &cim_ops, s, TYPE_JZ4740_CIM,
                          CIM_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void cim_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_cim;
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
    rc->phases.hold = cim_reset_hold;
}

static const TypeInfo cim_type_info = {
    .name = TYPE_JZ4740_CIM,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740CIMState),
    .instance_init = cim_init,
    .class_init = cim_class_init,
};

static void cim_register_types(void)
{
    type_register_static(&cim_type_info);
}

type_init(cim_register_types)
