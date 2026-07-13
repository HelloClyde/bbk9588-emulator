/*
 * Ingenic JZ4740 USB device controller.
 *
 * Models the no-host register path used by the BBK9588 firmware: interrupt
 * status/enables, indexed endpoint configuration, reset state and IRQ output.
 * USB packet transport and endpoint FIFOs are not implemented yet.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/usb/jz4740_udc.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define UDC_MMIO_SIZE              0x1000u
#define UDC_EP_COUNT               16u

#define UDC_FADDR                  0x00u
#define UDC_POWER                  0x01u
#define UDC_INTRIN                 0x02u
#define UDC_INTROUT                0x04u
#define UDC_INTRINE                0x06u
#define UDC_INTROUTE               0x08u
#define UDC_INTRUSB                0x0au
#define UDC_INTRUSBE               0x0bu
#define UDC_FRAME                  0x0cu
#define UDC_INDEX                  0x0eu
#define UDC_TESTMODE               0x0fu
#define UDC_INMAXP                 0x10u
#define UDC_CSR0_INCSR             0x12u
#define UDC_OUTMAXP                0x14u
#define UDC_OUTCSR                 0x16u
#define UDC_COUNT                  0x18u
#define UDC_FIFO_BASE              0x20u
#define UDC_FIFO_END               0x60u
#define UDC_EPINFO                 0x78u
#define UDC_RAMINFO                0x79u

#define UDC_POWER_RESET            0x20u
#define UDC_POWER_RW_MASK          0xe5u
#define UDC_INTRINE_RESET          0xffffu
#define UDC_INTROUTE_RESET         0xfffeu
#define UDC_INTRUSBE_RESET         0x06u
#define UDC_INTRUSBE_MASK          0x0fu
#define UDC_INTRIN_ENDPOINT_MASK   0x000fu
#define UDC_INTROUT_ENDPOINT_MASK  0x0006u
#define UDC_INDEX_MASK             0x0fu
#define UDC_TESTMODE_MASK          0x3fu
#define UDC_EPINFO_VALUE           0x23u
#define UDC_RAMINFO_VALUE          0x00u
#define UDC_MAXP_MASK              0x07ffu
#define UDC_INCSR_RW_MASK          0xfc10u
#define UDC_OUTCSR_RW_MASK         0xf820u

struct JZ4740UDCState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint8_t faddr;
    uint8_t power;
    uint16_t intr_in;
    uint16_t intr_out;
    uint16_t intr_in_enable;
    uint16_t intr_out_enable;
    uint8_t intr_usb;
    uint8_t intr_usb_enable;
    uint16_t frame;
    uint8_t index;
    uint8_t testmode;
    uint16_t in_maxp[UDC_EP_COUNT];
    uint16_t in_csr[UDC_EP_COUNT];
    uint16_t out_maxp[UDC_EP_COUNT];
    uint16_t out_csr[UDC_EP_COUNT];
    bool irq_level;
};

static bool udc_in_ep_valid(unsigned ep)
{
    return (UDC_INTRIN_ENDPOINT_MASK & (1u << ep)) != 0;
}

static bool udc_out_ep_valid(unsigned ep)
{
    return (UDC_INTROUT_ENDPOINT_MASK & (1u << ep)) != 0;
}

static bool udc_irq_pending(JZ4740UDCState *s)
{
    return ((s->intr_in & s->intr_in_enable &
             UDC_INTRIN_ENDPOINT_MASK) != 0) ||
           ((s->intr_out & s->intr_out_enable &
             UDC_INTROUT_ENDPOINT_MASK) != 0) ||
           ((s->intr_usb & s->intr_usb_enable &
             UDC_INTRUSBE_MASK) != 0);
}

static void udc_update_irq(JZ4740UDCState *s)
{
    s->irq_level = udc_irq_pending(s);
    qemu_set_irq(s->irq, s->irq_level);
}

static bool udc_range_intersects(hwaddr offset, unsigned size,
                                 hwaddr reg_offset, unsigned reg_size)
{
    return offset < reg_offset + reg_size &&
           reg_offset < offset + size;
}

static uint8_t udc_read_byte(JZ4740UDCState *s, hwaddr offset)
{
    unsigned ep = s->index & UDC_INDEX_MASK;

    switch (offset) {
    case UDC_FADDR:
        return s->faddr;
    case UDC_POWER:
        return s->power & UDC_POWER_RW_MASK;
    case UDC_INTRIN:
        return (s->intr_in & UDC_INTRIN_ENDPOINT_MASK) & 0xffu;
    case UDC_INTRIN + 1:
        return (s->intr_in & UDC_INTRIN_ENDPOINT_MASK) >> 8;
    case UDC_INTROUT:
        return (s->intr_out & UDC_INTROUT_ENDPOINT_MASK) & 0xffu;
    case UDC_INTROUT + 1:
        return (s->intr_out & UDC_INTROUT_ENDPOINT_MASK) >> 8;
    case UDC_INTRINE:
        return (s->intr_in_enable & UDC_INTRIN_ENDPOINT_MASK) & 0xffu;
    case UDC_INTRINE + 1:
        return (s->intr_in_enable & UDC_INTRIN_ENDPOINT_MASK) >> 8;
    case UDC_INTROUTE:
        return (s->intr_out_enable & UDC_INTROUT_ENDPOINT_MASK) & 0xffu;
    case UDC_INTROUTE + 1:
        return (s->intr_out_enable & UDC_INTROUT_ENDPOINT_MASK) >> 8;
    case UDC_INTRUSB:
        return s->intr_usb & UDC_INTRUSBE_MASK;
    case UDC_INTRUSBE:
        return s->intr_usb_enable & UDC_INTRUSBE_MASK;
    case UDC_FRAME:
        return s->frame & 0xffu;
    case UDC_FRAME + 1:
        return (s->frame >> 8) & 0x07u;
    case UDC_INDEX:
        return s->index & UDC_INDEX_MASK;
    case UDC_TESTMODE:
        return s->testmode & UDC_TESTMODE_MASK;
    case UDC_INMAXP:
        return udc_in_ep_valid(ep) ? s->in_maxp[ep] & 0xffu : 0;
    case UDC_INMAXP + 1:
        return udc_in_ep_valid(ep) ? s->in_maxp[ep] >> 8 : 0;
    case UDC_CSR0_INCSR:
        return ep != 0 && udc_in_ep_valid(ep) ?
               s->in_csr[ep] & 0xffu : 0;
    case UDC_CSR0_INCSR + 1:
        return ep != 0 && udc_in_ep_valid(ep) ? s->in_csr[ep] >> 8 : 0;
    case UDC_OUTMAXP:
        return udc_out_ep_valid(ep) ? s->out_maxp[ep] & 0xffu : 0;
    case UDC_OUTMAXP + 1:
        return udc_out_ep_valid(ep) ? s->out_maxp[ep] >> 8 : 0;
    case UDC_OUTCSR:
        return udc_out_ep_valid(ep) ? s->out_csr[ep] & 0xffu : 0;
    case UDC_OUTCSR + 1:
        return udc_out_ep_valid(ep) ? s->out_csr[ep] >> 8 : 0;
    case UDC_COUNT:
    case UDC_COUNT + 1:
        return 0;
    case UDC_EPINFO:
        return UDC_EPINFO_VALUE;
    case UDC_RAMINFO:
        return UDC_RAMINFO_VALUE;
    default:
        if (offset >= UDC_FIFO_BASE && offset < UDC_FIFO_END) {
            return 0;
        }
        return 0;
    }
}

static uint64_t udc_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740UDCState *s = opaque;
    uint32_t value = 0;

    for (unsigned i = 0; i < size; i++) {
        value |= (uint32_t)udc_read_byte(s, offset + i) << (i * 8);
    }
    if (udc_range_intersects(offset, size, UDC_INTRIN, 2)) {
        s->intr_in = 0;
    }
    if (udc_range_intersects(offset, size, UDC_INTROUT, 2)) {
        s->intr_out = 0;
    }
    if (udc_range_intersects(offset, size, UDC_INTRUSB, 1)) {
        s->intr_usb = 0;
        s->power &= ~0x02u;
    }
    udc_update_irq(s);
    return value;
}

static void udc_write_byte(JZ4740UDCState *s, hwaddr offset, uint8_t value)
{
    unsigned ep = s->index & UDC_INDEX_MASK;

    switch (offset) {
    case UDC_FADDR:
        s->faddr = (value & 0x7fu) | 0x80u;
        break;
    case UDC_POWER:
        s->power = (value & UDC_POWER_RW_MASK) |
                   (s->power & ~UDC_POWER_RW_MASK);
        s->power &= ~0x1au;
        break;
    case UDC_INTRINE:
        s->intr_in_enable = (s->intr_in_enable & 0xff00u) | value;
        s->intr_in_enable &= UDC_INTRIN_ENDPOINT_MASK;
        break;
    case UDC_INTRINE + 1:
        s->intr_in_enable = (s->intr_in_enable & 0x00ffu) |
                            ((uint16_t)value << 8);
        s->intr_in_enable &= UDC_INTRIN_ENDPOINT_MASK;
        break;
    case UDC_INTROUTE:
        s->intr_out_enable = ((s->intr_out_enable & 0xff00u) | value) &
                             UDC_INTROUT_ENDPOINT_MASK;
        break;
    case UDC_INTROUTE + 1:
        s->intr_out_enable = ((s->intr_out_enable & 0x00ffu) |
                              ((uint16_t)value << 8)) &
                             UDC_INTROUT_ENDPOINT_MASK;
        break;
    case UDC_INTRUSBE:
        s->intr_usb_enable = value & UDC_INTRUSBE_MASK;
        break;
    case UDC_INDEX:
        s->index = value & UDC_INDEX_MASK;
        break;
    case UDC_TESTMODE:
        s->testmode = value & UDC_TESTMODE_MASK;
        break;
    case UDC_INMAXP:
        if (udc_in_ep_valid(ep)) {
            s->in_maxp[ep] = (s->in_maxp[ep] & 0xff00u) | value;
            s->in_maxp[ep] &= UDC_MAXP_MASK;
        }
        break;
    case UDC_INMAXP + 1:
        if (udc_in_ep_valid(ep)) {
            s->in_maxp[ep] = ((s->in_maxp[ep] & 0x00ffu) |
                              ((uint16_t)value << 8)) & UDC_MAXP_MASK;
        }
        break;
    case UDC_CSR0_INCSR:
        if (ep != 0 && udc_in_ep_valid(ep)) {
            s->in_csr[ep] = (s->in_csr[ep] & 0xff00u) | value;
            s->in_csr[ep] &= UDC_INCSR_RW_MASK;
        }
        break;
    case UDC_CSR0_INCSR + 1:
        if (ep != 0 && udc_in_ep_valid(ep)) {
            s->in_csr[ep] = ((s->in_csr[ep] & 0x00ffu) |
                             ((uint16_t)value << 8)) & UDC_INCSR_RW_MASK;
        }
        break;
    case UDC_OUTMAXP:
        if (udc_out_ep_valid(ep)) {
            s->out_maxp[ep] = (s->out_maxp[ep] & 0xff00u) | value;
            s->out_maxp[ep] &= UDC_MAXP_MASK;
        }
        break;
    case UDC_OUTMAXP + 1:
        if (udc_out_ep_valid(ep)) {
            s->out_maxp[ep] = ((s->out_maxp[ep] & 0x00ffu) |
                               ((uint16_t)value << 8)) & UDC_MAXP_MASK;
        }
        break;
    case UDC_OUTCSR:
        if (udc_out_ep_valid(ep)) {
            s->out_csr[ep] = (s->out_csr[ep] & 0xff00u) | value;
            s->out_csr[ep] &= UDC_OUTCSR_RW_MASK;
        }
        break;
    case UDC_OUTCSR + 1:
        if (udc_out_ep_valid(ep)) {
            s->out_csr[ep] = ((s->out_csr[ep] & 0x00ffu) |
                              ((uint16_t)value << 8)) & UDC_OUTCSR_RW_MASK;
        }
        break;
    default:
        break;
    }
}

static void udc_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    JZ4740UDCState *s = opaque;

    for (unsigned i = 0; i < size; i++) {
        udc_write_byte(s, offset + i, (value >> (i * 8)) & 0xffu);
    }
    udc_update_irq(s);
}

static const MemoryRegionOps udc_ops = {
    .read = udc_read,
    .write = udc_write,
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

static void udc_reset_hold(Object *obj, ResetType type)
{
    JZ4740UDCState *s = JZ4740_UDC(obj);

    s->faddr = 0;
    s->power = UDC_POWER_RESET;
    s->intr_in = 0;
    s->intr_out = 0;
    s->intr_in_enable = UDC_INTRINE_RESET & UDC_INTRIN_ENDPOINT_MASK;
    s->intr_out_enable = UDC_INTROUTE_RESET & UDC_INTROUT_ENDPOINT_MASK;
    s->intr_usb = 0;
    s->intr_usb_enable = UDC_INTRUSBE_RESET;
    s->frame = 0;
    s->index = 0;
    s->testmode = 0;
    memset(s->in_maxp, 0, sizeof(s->in_maxp));
    memset(s->in_csr, 0, sizeof(s->in_csr));
    memset(s->out_maxp, 0, sizeof(s->out_maxp));
    memset(s->out_csr, 0, sizeof(s->out_csr));
    s->irq_level = false;
    qemu_set_irq(s->irq, 0);
}

static int udc_post_load(void *opaque, int version_id)
{
    JZ4740UDCState *s = opaque;

    s->faddr &= 0xffu;
    s->power &= UDC_POWER_RW_MASK;
    s->intr_in &= UDC_INTRIN_ENDPOINT_MASK;
    s->intr_out &= UDC_INTROUT_ENDPOINT_MASK;
    s->intr_in_enable &= UDC_INTRIN_ENDPOINT_MASK;
    s->intr_out_enable &= UDC_INTROUT_ENDPOINT_MASK;
    s->intr_usb &= UDC_INTRUSBE_MASK;
    s->intr_usb_enable &= UDC_INTRUSBE_MASK;
    s->frame &= 0x07ffu;
    s->index &= UDC_INDEX_MASK;
    s->testmode &= UDC_TESTMODE_MASK;
    udc_update_irq(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_udc = {
    .name = TYPE_JZ4740_UDC,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = udc_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT8(faddr, JZ4740UDCState),
        VMSTATE_UINT8(power, JZ4740UDCState),
        VMSTATE_UINT16(intr_in, JZ4740UDCState),
        VMSTATE_UINT16(intr_out, JZ4740UDCState),
        VMSTATE_UINT16(intr_in_enable, JZ4740UDCState),
        VMSTATE_UINT16(intr_out_enable, JZ4740UDCState),
        VMSTATE_UINT8(intr_usb, JZ4740UDCState),
        VMSTATE_UINT8(intr_usb_enable, JZ4740UDCState),
        VMSTATE_UINT16(frame, JZ4740UDCState),
        VMSTATE_UINT8(index, JZ4740UDCState),
        VMSTATE_UINT8(testmode, JZ4740UDCState),
        VMSTATE_UINT16_ARRAY(in_maxp, JZ4740UDCState, UDC_EP_COUNT),
        VMSTATE_UINT16_ARRAY(in_csr, JZ4740UDCState, UDC_EP_COUNT),
        VMSTATE_UINT16_ARRAY(out_maxp, JZ4740UDCState, UDC_EP_COUNT),
        VMSTATE_UINT16_ARRAY(out_csr, JZ4740UDCState, UDC_EP_COUNT),
        VMSTATE_END_OF_LIST()
    },
};

static void udc_init(Object *obj)
{
    JZ4740UDCState *s = JZ4740_UDC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &udc_ops, s, TYPE_JZ4740_UDC,
                          UDC_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void udc_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_udc;
    set_bit(DEVICE_CATEGORY_USB, dc->categories);
    rc->phases.hold = udc_reset_hold;
}

static const TypeInfo udc_type_info = {
    .name = TYPE_JZ4740_UDC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740UDCState),
    .instance_init = udc_init,
    .class_init = udc_class_init,
};

static void udc_register_types(void)
{
    type_register_static(&udc_type_info);
}

type_init(udc_register_types)
