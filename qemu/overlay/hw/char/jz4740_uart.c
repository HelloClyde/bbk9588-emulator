/*
 * Ingenic JZ4740 UART.
 *
 * Models the 16550-style register bank, receive FIFO, loopback, chardev
 * frontend and interrupt output used by the BBK9588 firmware.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "chardev/char-fe.h"
#include "hw/char/jz4740_uart.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties-system.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define UART_MMIO_SIZE              0x1000u
#define UART_FIFO_SIZE              16u

#define UART_RBR                    0x00u
#define UART_THR                    0x00u
#define UART_DLL                    0x00u
#define UART_IER                    0x04u
#define UART_DLH                    0x04u
#define UART_IIR                    0x08u
#define UART_FCR                    0x08u
#define UART_LCR                    0x0cu
#define UART_MCR                    0x10u
#define UART_LSR                    0x14u
#define UART_MSR                    0x18u
#define UART_SPR                    0x1cu
#define UART_ISR                    0x20u
#define UART_UMR                    0x24u
#define UART_UACR                   0x28u

#define UART_IER_RDRIE              0x01u
#define UART_IER_TDRIE              0x02u
#define UART_IER_RLSIE              0x04u
#define UART_IER_MSIE               0x08u
#define UART_IER_RTOIE              0x10u
#define UART_IER_MASK               0x1fu

#define UART_IIR_NONE               0x01u
#define UART_IIR_MODEM              0x00u
#define UART_IIR_TDR                0x02u
#define UART_IIR_RDR                0x04u
#define UART_IIR_RLS                0x06u
#define UART_IIR_RTO                0x0cu
#define UART_IIR_FIFO               0xc0u

#define UART_FCR_FME                0x01u
#define UART_FCR_RFRT               0x02u
#define UART_FCR_TFRT               0x04u
#define UART_FCR_DME                0x08u
#define UART_FCR_UME                0x10u
#define UART_FCR_RDTR_MASK          0xc0u

#define UART_LCR_DLAB               0x80u

#define UART_MCR_MDCE               0x80u
#define UART_MCR_LOOP               0x10u
#define UART_MCR_RTS                0x02u
#define UART_MCR_MASK               0x92u

#define UART_LSR_DRY                0x01u
#define UART_LSR_OVER               0x02u
#define UART_LSR_PARER              0x04u
#define UART_LSR_FMER               0x08u
#define UART_LSR_BI                 0x10u
#define UART_LSR_TDRQ               0x20u
#define UART_LSR_TEMP               0x40u
#define UART_LSR_FIFOE              0x80u
#define UART_LSR_RESET              (UART_LSR_TDRQ | UART_LSR_TEMP)
#define UART_LSR_ERROR_MASK         \
    (UART_LSR_OVER | UART_LSR_PARER | UART_LSR_FMER | UART_LSR_BI | \
     UART_LSR_FIFOE)

#define UART_MSR_CCTS               0x01u
#define UART_MSR_CTS                0x10u
#define UART_ISR_MASK               0x1fu
#define UART_UMR_MASK               0x3fu
#define UART_UACR_MASK              0x0fffu

struct JZ4740UARTState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    CharFrontend chr;
    uint32_t regs[UART_MMIO_SIZE / sizeof(uint32_t)];
    uint32_t status;
    bool thr_irq_latched;
    uint8_t ier;
    uint8_t fcr;
    uint8_t lcr;
    uint8_t mcr;
    uint8_t msr;
    uint8_t spr;
    uint8_t isr;
    uint8_t umr;
    uint16_t uacr;
    uint8_t dll;
    uint8_t dlh;
    uint8_t rx_fifo[UART_FIFO_SIZE];
    uint32_t rx_head;
    uint32_t rx_len;
    bool irq_level;
};

static unsigned uart_rx_trigger_level(JZ4740UARTState *s)
{
    if (!(s->fcr & UART_FCR_FME)) {
        return 1;
    }
    switch ((s->fcr & UART_FCR_RDTR_MASK) >> 6) {
    case 0:
        return 1;
    case 1:
        return 4;
    case 2:
        return 8;
    default:
        return 15;
    }
}

static bool uart_rx_data_ready(JZ4740UARTState *s)
{
    return s->rx_len >= uart_rx_trigger_level(s);
}

static void uart_latch_thr_irq(JZ4740UARTState *s)
{
    if ((s->fcr & UART_FCR_UME) &&
        (s->ier & UART_IER_TDRIE) &&
        (s->status & UART_LSR_TDRQ)) {
        s->thr_irq_latched = true;
    }
}

static uint8_t uart_iir_value(JZ4740UARTState *s)
{
    uint8_t fifo_bits = (s->fcr & UART_FCR_FME) ? UART_IIR_FIFO : 0;

    if (!(s->fcr & UART_FCR_UME)) {
        return fifo_bits | UART_IIR_NONE;
    }
    if ((s->ier & UART_IER_RLSIE) &&
        (s->status & UART_LSR_ERROR_MASK)) {
        return fifo_bits | UART_IIR_RLS;
    }
    if ((s->ier & UART_IER_RDRIE) && uart_rx_data_ready(s)) {
        return fifo_bits | UART_IIR_RDR;
    }
    if ((s->ier & UART_IER_RTOIE) && s->rx_len != 0) {
        return fifo_bits | UART_IIR_RTO;
    }
    if (s->thr_irq_latched &&
        (s->ier & UART_IER_TDRIE) &&
        (s->status & UART_LSR_TDRQ)) {
        return fifo_bits | UART_IIR_TDR;
    }
    if ((s->ier & UART_IER_MSIE) && (s->msr & UART_MSR_CCTS)) {
        return fifo_bits | UART_IIR_MODEM;
    }
    return fifo_bits | UART_IIR_NONE;
}

static void uart_update_irq(JZ4740UARTState *s)
{
    s->irq_level = (uart_iir_value(s) & UART_IIR_NONE) == 0;
    qemu_set_irq(s->irq, s->irq_level);
}

static void uart_update_lsr(JZ4740UARTState *s)
{
    uint32_t sticky = s->status & UART_LSR_ERROR_MASK;

    s->status = sticky | UART_LSR_RESET;
    if (s->rx_len != 0) {
        s->status |= UART_LSR_DRY;
    }
    if ((s->fcr & UART_FCR_FME) &&
        (sticky & (UART_LSR_OVER | UART_LSR_PARER |
                   UART_LSR_FMER | UART_LSR_BI))) {
        s->status |= UART_LSR_FIFOE;
    }
}

static void uart_push_rx(JZ4740UARTState *s, uint8_t value)
{
    if (s->rx_len < UART_FIFO_SIZE) {
        unsigned tail = (s->rx_head + s->rx_len) % UART_FIFO_SIZE;

        s->rx_fifo[tail] = value;
        s->rx_len++;
    } else {
        s->status |= UART_LSR_OVER;
    }
    uart_update_lsr(s);
    uart_update_irq(s);
}

static int uart_can_receive(void *opaque)
{
    JZ4740UARTState *s = opaque;

    return UART_FIFO_SIZE - s->rx_len;
}

static void uart_receive(void *opaque, const uint8_t *buf, int size)
{
    JZ4740UARTState *s = opaque;

    for (int i = 0; i < size; i++) {
        uart_push_rx(s, buf[i]);
    }
}

static uint8_t uart_pop_rx(JZ4740UARTState *s)
{
    uint8_t value = 0xff;

    if (s->rx_len != 0) {
        value = s->rx_fifo[s->rx_head];
        s->rx_head = (s->rx_head + 1) % UART_FIFO_SIZE;
        s->rx_len--;
    }
    uart_update_lsr(s);
    uart_update_irq(s);
    qemu_chr_fe_accept_input(&s->chr);
    return value;
}

static void uart_clear_rx(JZ4740UARTState *s)
{
    s->rx_head = 0;
    s->rx_len = 0;
    s->status &= ~UART_LSR_ERROR_MASK;
    uart_update_lsr(s);
    uart_update_irq(s);
    qemu_chr_fe_accept_input(&s->chr);
}

static void uart_update_msr(JZ4740UARTState *s)
{
    uint8_t old_cts = s->msr & UART_MSR_CTS;
    uint8_t cts = 0;

    if ((s->mcr & (UART_MCR_MDCE | UART_MCR_LOOP | UART_MCR_RTS)) ==
        (UART_MCR_MDCE | UART_MCR_LOOP | UART_MCR_RTS)) {
        cts = UART_MSR_CTS;
    }
    s->msr = (s->msr & UART_MSR_CCTS) | cts;
    if (old_cts != cts) {
        s->msr |= UART_MSR_CCTS;
    }
}

static uint32_t uart_read_reg(JZ4740UARTState *s, hwaddr offset)
{
    uint32_t index = offset / sizeof(uint32_t);
    uint8_t value;

    switch (offset) {
    case UART_RBR:
        value = (s->lcr & UART_LCR_DLAB) ? s->dll : uart_pop_rx(s);
        break;
    case UART_IER:
        value = (s->lcr & UART_LCR_DLAB) ? s->dlh : s->ier;
        break;
    case UART_IIR:
        value = uart_iir_value(s);
        if ((value & 0x0fu) == UART_IIR_TDR) {
            s->thr_irq_latched = false;
            uart_update_irq(s);
        }
        break;
    case UART_LCR:
        value = s->lcr;
        break;
    case UART_MCR:
        value = s->mcr;
        break;
    case UART_LSR:
        uart_update_lsr(s);
        value = s->status;
        s->status &= ~UART_LSR_ERROR_MASK;
        uart_update_lsr(s);
        uart_update_irq(s);
        break;
    case UART_MSR:
        uart_update_msr(s);
        value = s->msr;
        s->msr &= ~UART_MSR_CCTS;
        uart_update_irq(s);
        break;
    case UART_SPR:
        value = s->spr;
        break;
    case UART_ISR:
        value = s->isr;
        break;
    case UART_UMR:
        value = s->umr;
        break;
    case UART_UACR:
        value = s->uacr;
        break;
    default:
        value = s->regs[index];
        break;
    }
    s->regs[index] = value;
    return value;
}

static uint64_t uart_extract32(uint32_t value, hwaddr offset, unsigned size)
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

static uint64_t uart_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740UARTState *s = opaque;
    hwaddr aligned_offset = offset & ~3u;

    return uart_extract32(uart_read_reg(s, aligned_offset), offset, size);
}

static void uart_write_reg(JZ4740UARTState *s, hwaddr offset, uint32_t reg)
{
    unsigned shift = (offset & 3u) * 8u;
    uint8_t value = (reg >> shift) & 0xffu;
    uint32_t index = (offset & ~3u) / sizeof(uint32_t);

    if (offset & 3u) {
        return;
    }
    switch (offset) {
    case UART_THR:
        if (s->lcr & UART_LCR_DLAB) {
            s->dll = value;
            s->regs[index] = s->dll;
            break;
        }
        s->thr_irq_latched = false;
        if (s->mcr & UART_MCR_LOOP) {
            uart_push_rx(s, value);
        } else if (qemu_chr_fe_backend_connected(&s->chr)) {
            qemu_chr_fe_write_all(&s->chr, &value, 1);
        }
        uart_update_lsr(s);
        uart_latch_thr_irq(s);
        s->regs[index] = value;
        break;
    case UART_IER:
        if (s->lcr & UART_LCR_DLAB) {
            s->dlh = value;
            s->regs[index] = s->dlh;
        } else {
            bool was_tdrie = (s->ier & UART_IER_TDRIE) != 0;

            s->ier = value & UART_IER_MASK;
            if (!(s->ier & UART_IER_TDRIE)) {
                s->thr_irq_latched = false;
            } else if (!was_tdrie) {
                uart_latch_thr_irq(s);
            }
            s->regs[index] = s->ier;
        }
        break;
    case UART_FCR:
        if (value & UART_FCR_RFRT) {
            uart_clear_rx(s);
        }
        if (value & UART_FCR_TFRT) {
            s->thr_irq_latched = false;
        }
        s->fcr = value & (UART_FCR_FME | UART_FCR_DME |
                          UART_FCR_UME | UART_FCR_RDTR_MASK);
        if (s->fcr & UART_FCR_UME) {
            uart_latch_thr_irq(s);
        } else {
            s->thr_irq_latched = false;
        }
        s->regs[index] = s->fcr;
        break;
    case UART_LCR:
        s->lcr = value;
        s->regs[index] = s->lcr;
        break;
    case UART_MCR:
        s->mcr = value & UART_MCR_MASK;
        uart_update_msr(s);
        s->regs[index] = s->mcr;
        break;
    case UART_SPR:
        s->spr = value;
        s->regs[index] = s->spr;
        break;
    case UART_ISR:
        s->isr = value & UART_ISR_MASK;
        s->regs[index] = s->isr;
        break;
    case UART_UMR:
        s->umr = value & UART_UMR_MASK;
        s->regs[index] = s->umr;
        break;
    case UART_UACR:
        s->uacr = reg & UART_UACR_MASK;
        s->regs[index] = s->uacr;
        break;
    default:
        s->regs[index] = reg;
        break;
    }
    uart_update_lsr(s);
    uart_update_irq(s);
}

static void uart_write(void *opaque, hwaddr offset, uint64_t value,
                       unsigned size)
{
    JZ4740UARTState *s = opaque;
    uint32_t index = offset / sizeof(uint32_t);
    unsigned shift = (offset & 3u) * 8u;
    uint32_t mask;
    uint32_t reg;

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
    reg = (s->regs[index] & ~mask) | (((uint32_t)value << shift) & mask);
    s->regs[index] = reg;
    uart_write_reg(s, offset, reg);
}

static const MemoryRegionOps uart_ops = {
    .read = uart_read,
    .write = uart_write,
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

static void uart_reset_hold(Object *obj, ResetType type)
{
    JZ4740UARTState *s = JZ4740_UART(obj);

    memset(s->regs, 0, sizeof(s->regs));
    s->status = UART_LSR_RESET;
    s->thr_irq_latched = false;
    s->ier = 0;
    s->fcr = 0;
    s->lcr = 0;
    s->mcr = 0;
    s->msr = 0;
    s->spr = 0;
    s->isr = 0;
    s->umr = 0;
    s->uacr = 0;
    s->dll = 0;
    s->dlh = 0;
    memset(s->rx_fifo, 0, sizeof(s->rx_fifo));
    s->rx_head = 0;
    s->rx_len = 0;
    s->irq_level = false;
    qemu_set_irq(s->irq, 0);
    qemu_chr_fe_accept_input(&s->chr);
}

static int uart_post_load(void *opaque, int version_id)
{
    JZ4740UARTState *s = opaque;

    s->rx_head %= UART_FIFO_SIZE;
    s->rx_len = MIN(s->rx_len, UART_FIFO_SIZE);
    uart_update_lsr(s);
    uart_update_irq(s);
    qemu_chr_fe_accept_input(&s->chr);
    return 0;
}

static const VMStateDescription vmstate_jz4740_uart = {
    .name = TYPE_JZ4740_UART,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = uart_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4740UARTState,
                             UART_MMIO_SIZE / sizeof(uint32_t)),
        VMSTATE_UINT32(status, JZ4740UARTState),
        VMSTATE_BOOL(thr_irq_latched, JZ4740UARTState),
        VMSTATE_UINT8(ier, JZ4740UARTState),
        VMSTATE_UINT8(fcr, JZ4740UARTState),
        VMSTATE_UINT8(lcr, JZ4740UARTState),
        VMSTATE_UINT8(mcr, JZ4740UARTState),
        VMSTATE_UINT8(msr, JZ4740UARTState),
        VMSTATE_UINT8(spr, JZ4740UARTState),
        VMSTATE_UINT8(isr, JZ4740UARTState),
        VMSTATE_UINT8(umr, JZ4740UARTState),
        VMSTATE_UINT16(uacr, JZ4740UARTState),
        VMSTATE_UINT8(dll, JZ4740UARTState),
        VMSTATE_UINT8(dlh, JZ4740UARTState),
        VMSTATE_UINT8_ARRAY(rx_fifo, JZ4740UARTState, UART_FIFO_SIZE),
        VMSTATE_UINT32(rx_head, JZ4740UARTState),
        VMSTATE_UINT32(rx_len, JZ4740UARTState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property uart_properties[] = {
    DEFINE_PROP_CHR("chardev", JZ4740UARTState, chr),
};

static void uart_realize(DeviceState *dev, Error **errp)
{
    JZ4740UARTState *s = JZ4740_UART(dev);

    qemu_chr_fe_set_handlers(&s->chr, uart_can_receive, uart_receive,
                             NULL, NULL, s, NULL, true);
}

static void uart_init(Object *obj)
{
    JZ4740UARTState *s = JZ4740_UART(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &uart_ops, s, TYPE_JZ4740_UART,
                          UART_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void uart_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->realize = uart_realize;
    dc->vmsd = &vmstate_jz4740_uart;
    device_class_set_props(dc, uart_properties);
    set_bit(DEVICE_CATEGORY_MISC, dc->categories);
    rc->phases.hold = uart_reset_hold;
}

static const TypeInfo uart_type_info = {
    .name = TYPE_JZ4740_UART,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740UARTState),
    .instance_init = uart_init,
    .class_init = uart_class_init,
};

static void uart_register_types(void)
{
    type_register_static(&uart_type_info);
}

type_init(uart_register_types)
