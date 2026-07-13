/*
 * Ingenic JZ4740 multimedia card controller.
 *
 * This models the register, response FIFO, interrupt and DMA handshake used
 * by the BBK9588 firmware.  Card media transport remains a board concern so
 * the controller can be reused independently of the current no-card policy.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/sd/jz4740_msc.h"
#include "migration/vmstate.h"
#include "qemu/module.h"

#define MSC_MMIO_SIZE              0x1000u
#define MSC_REGS                   (MSC_MMIO_SIZE / sizeof(uint32_t))
#define MSC_RESPONSE_BYTES         16u

#define MSC_STRPCL                 0x0000u
#define MSC_STAT                   0x0004u
#define MSC_RESTO                  0x0010u
#define MSC_RDTO                   0x0014u
#define MSC_IMASK                  0x0024u
#define MSC_IREG                   0x0028u
#define MSC_CMD                    0x002cu
#define MSC_ARG                    0x0030u
#define MSC_RES                    0x0034u

#define MSC_STAT_RESET             0x00000040u
#define MSC_RESTO_RESET            0x00000040u
#define MSC_RDTO_RESET             0x0000ffffu
#define MSC_IMASK_RESET            0x000000ffu
#define MSC_INTERRUPT_MASK         0x000000ffu
#define MSC_DATA_READY_MASK        0x00000003u
#define MSC_STRPCL_START_COMPAT    0x00000006u
#define MSC_STAT_CLOCK_EN          0x00000800u
#define MSC_STAT_CLOCK_STOP        0x00000100u
#define MSC_DMA_PHYS_MASK          0x1fffffffu
#define MSC_READ_SINGLE_BLOCK      0x11u
#define MSC_READ_MULTIPLE_BLOCK    0x12u
#define MSC_WRITE_BLOCK            0x18u
#define MSC_WRITE_MULTIPLE_BLOCK   0x19u

struct JZ4740MSCState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t regs[MSC_REGS];
    uint8_t response[MSC_RESPONSE_BYTES];
    uint32_t response_len;
    uint32_t response_index;
    bool read_pending;
    bool write_pending;
    bool data_ready;
    bool irq_level;
    uint32_t read_lba;
    uint32_t write_lba;
    uint32_t last_command;
    uint32_t last_argument;
    uint32_t last_dma_phys;
    uint32_t last_dma_words;
    uint32_t dma_complete_count;

    JZ4740MSCKickCallback kick_callback;
    void *kick_opaque;
    JZ4740MSCCommandCallback command_callback;
    void *command_opaque;
};

static uint32_t msc_reg(JZ4740MSCState *s, hwaddr offset)
{
    return s->regs[offset / sizeof(uint32_t)];
}

static void msc_set_reg(JZ4740MSCState *s, hwaddr offset, uint32_t value)
{
    s->regs[offset / sizeof(uint32_t)] = value;
}

static uint32_t msc_interrupt_status(JZ4740MSCState *s)
{
    uint32_t status = msc_reg(s, MSC_IREG) & MSC_INTERRUPT_MASK;

    if (s->data_ready) {
        status |= MSC_DATA_READY_MASK;
    }
    return status;
}

static void msc_update_irq(JZ4740MSCState *s)
{
    uint32_t mask = msc_reg(s, MSC_IMASK) & MSC_INTERRUPT_MASK;
    bool level = (msc_interrupt_status(s) & ~mask) != 0;

    if (level != s->irq_level) {
        s->irq_level = level;
        qemu_set_irq(s->irq, level);
    }
}

static void msc_prepare_response(JZ4740MSCState *s)
{
    uint32_t command = msc_reg(s, MSC_CMD) & 0xffu;
    uint32_t argument = msc_reg(s, MSC_ARG);

    memset(s->response, 0, sizeof(s->response));
    s->response[0] = command;
    s->response_len = sizeof(s->response);
    s->response_index = 0;
    s->last_command = command;
    s->last_argument = argument;
    s->read_pending = false;
    s->write_pending = false;
    s->data_ready = false;
    msc_set_reg(s, MSC_IREG,
                msc_reg(s, MSC_IREG) & ~MSC_DATA_READY_MASK);

    switch (command) {
    case MSC_READ_SINGLE_BLOCK:
    case MSC_READ_MULTIPLE_BLOCK:
        s->read_lba = argument >> 9;
        s->read_pending = true;
        break;
    case MSC_WRITE_BLOCK:
    case MSC_WRITE_MULTIPLE_BLOCK:
        s->write_lba = argument >> 9;
        s->write_pending = true;
        break;
    default:
        break;
    }

    if (s->command_callback) {
        s->command_callback(s->command_opaque, command, argument);
    }
    msc_update_irq(s);
    if ((s->read_pending || s->write_pending) && s->kick_callback) {
        s->kick_callback(s->kick_opaque);
    }
}

static uint32_t msc_read_response(JZ4740MSCState *s, unsigned size)
{
    uint32_t value = 0;

    /*
     * C200 reads halfwords and stores the high byte first.  Present each FIFO
     * pair as a big-endian halfword while keeping byte accesses sequential.
     */
    if (size <= 1) {
        if (s->response_index < s->response_len) {
            value = s->response[s->response_index++];
        }
        return value;
    }
    for (unsigned i = 0; i < size; i += 2) {
        uint32_t hi = 0;
        uint32_t lo = 0;

        if (s->response_index < s->response_len) {
            hi = s->response[s->response_index++];
        }
        if (s->response_index < s->response_len) {
            lo = s->response[s->response_index++];
        }
        value |= ((hi << 8) | lo) << (i * 8);
    }
    return value;
}

static uint64_t msc_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740MSCState *s = opaque;
    hwaddr reg_offset = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t value;

    if (offset >= MSC_MMIO_SIZE || size > MSC_MMIO_SIZE - offset) {
        return 0;
    }
    if (offset == MSC_RES) {
        return msc_read_response(s, size);
    }
    value = reg_offset == MSC_IREG ?
            msc_interrupt_status(s) : msc_reg(s, reg_offset);
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

static void msc_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    JZ4740MSCState *s = opaque;
    hwaddr reg_offset = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t mask;
    uint32_t lane_value;
    uint32_t reg;

    if (offset >= MSC_MMIO_SIZE || size > MSC_MMIO_SIZE - offset) {
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

    if (reg_offset == MSC_IREG) {
        msc_set_reg(s, MSC_IREG, msc_reg(s, MSC_IREG) & ~lane_value);
        if (lane_value & MSC_DATA_READY_MASK) {
            s->data_ready = false;
        }
        msc_update_irq(s);
        return;
    }
    if (reg_offset == MSC_RES) {
        return;
    }

    reg = msc_reg(s, reg_offset);
    reg = (reg & ~mask) | lane_value;
    msc_set_reg(s, reg_offset, reg);
    if (reg_offset == MSC_STRPCL &&
        (reg & 0xffffu) == MSC_STRPCL_START_COMPAT) {
        msc_prepare_response(s);
        msc_set_reg(s, MSC_STAT,
                    (msc_reg(s, MSC_STAT) & ~MSC_STAT_CLOCK_STOP) |
                    MSC_STAT_CLOCK_EN);
    }
    if (reg_offset == MSC_IMASK) {
        msc_update_irq(s);
    }
}

static const MemoryRegionOps msc_ops = {
    .read = msc_read,
    .write = msc_write,
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

void jz4740_msc_set_kick_callback(JZ4740MSCState *s,
                                  JZ4740MSCKickCallback callback,
                                  void *opaque)
{
    s->kick_callback = callback;
    s->kick_opaque = opaque;
}

void jz4740_msc_set_command_callback(JZ4740MSCState *s,
                                     JZ4740MSCCommandCallback callback,
                                     void *opaque)
{
    s->command_callback = callback;
    s->command_opaque = opaque;
}

bool jz4740_msc_begin_dma(JZ4740MSCState *s, unsigned channel,
                          uint32_t source, uint32_t target, uint32_t words,
                          JZ4740MSCDMATransfer *transfer)
{
    bool read = s->read_pending;

    if ((!s->read_pending && !s->write_pending) ||
        (s->read_pending && channel != 0) ||
        (s->write_pending && channel != 1)) {
        return false;
    }

    memset(transfer, 0, sizeof(*transfer));
    transfer->read = read;
    transfer->lba = read ? s->read_lba : s->write_lba;
    transfer->dma_phys = (read ? target : source) & MSC_DMA_PHYS_MASK;
    transfer->words = words;
    transfer->bytes = words * sizeof(uint32_t);
    transfer->sectors = (transfer->bytes + 511u) / 512u;
    transfer->command = s->last_command;
    transfer->argument = s->last_argument;

    s->last_dma_phys = transfer->dma_phys;
    s->last_dma_words = words;
    s->dma_complete_count++;
    return true;
}

void jz4740_msc_finish_dma(JZ4740MSCState *s, bool data_ready)
{
    s->read_pending = false;
    s->write_pending = false;
    s->data_ready = data_ready;
    msc_update_irq(s);
}

void jz4740_msc_get_diagnostics(JZ4740MSCState *s,
                                JZ4740MSCDiagnostics *diagnostics)
{
    memset(diagnostics, 0, sizeof(*diagnostics));
    diagnostics->read_pending = s->read_pending;
    diagnostics->write_pending = s->write_pending;
    diagnostics->data_ready = s->data_ready;
    diagnostics->read_lba = s->read_lba;
    diagnostics->write_lba = s->write_lba;
    diagnostics->last_command = s->last_command;
    diagnostics->last_argument = s->last_argument;
    diagnostics->last_dma_phys = s->last_dma_phys;
    diagnostics->last_dma_words = s->last_dma_words;
    diagnostics->dma_complete_count = s->dma_complete_count;
}

static void msc_reset_hold(Object *obj, ResetType type)
{
    JZ4740MSCState *s = JZ4740_MSC(obj);

    memset(s->regs, 0, sizeof(s->regs));
    memset(s->response, 0, sizeof(s->response));
    msc_set_reg(s, MSC_STAT, MSC_STAT_RESET);
    msc_set_reg(s, MSC_RESTO, MSC_RESTO_RESET);
    msc_set_reg(s, MSC_RDTO, MSC_RDTO_RESET);
    msc_set_reg(s, MSC_IMASK, MSC_IMASK_RESET);
    s->response_len = 0;
    s->response_index = 0;
    s->read_pending = false;
    s->write_pending = false;
    s->data_ready = false;
    s->irq_level = false;
    s->read_lba = 0;
    s->write_lba = 0;
    s->last_command = 0;
    s->last_argument = 0;
    s->last_dma_phys = 0;
    s->last_dma_words = 0;
    s->dma_complete_count = 0;
    qemu_set_irq(s->irq, 0);
}

static int msc_post_load(void *opaque, int version_id)
{
    JZ4740MSCState *s = opaque;

    s->response_len = MIN(s->response_len, MSC_RESPONSE_BYTES);
    s->response_index = MIN(s->response_index, s->response_len);
    if (s->read_pending && s->write_pending) {
        s->write_pending = false;
    }
    msc_update_irq(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_msc = {
    .name = TYPE_JZ4740_MSC,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = msc_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4740MSCState, MSC_REGS),
        VMSTATE_UINT8_ARRAY(response, JZ4740MSCState, MSC_RESPONSE_BYTES),
        VMSTATE_UINT32(response_len, JZ4740MSCState),
        VMSTATE_UINT32(response_index, JZ4740MSCState),
        VMSTATE_BOOL(read_pending, JZ4740MSCState),
        VMSTATE_BOOL(write_pending, JZ4740MSCState),
        VMSTATE_BOOL(data_ready, JZ4740MSCState),
        VMSTATE_UINT32(read_lba, JZ4740MSCState),
        VMSTATE_UINT32(write_lba, JZ4740MSCState),
        VMSTATE_UINT32(last_command, JZ4740MSCState),
        VMSTATE_UINT32(last_argument, JZ4740MSCState),
        VMSTATE_UINT32(last_dma_phys, JZ4740MSCState),
        VMSTATE_UINT32(last_dma_words, JZ4740MSCState),
        VMSTATE_UINT32(dma_complete_count, JZ4740MSCState),
        VMSTATE_END_OF_LIST()
    },
};

static void msc_init(Object *obj)
{
    JZ4740MSCState *s = JZ4740_MSC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &msc_ops, s, TYPE_JZ4740_MSC,
                          MSC_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void msc_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_msc;
    set_bit(DEVICE_CATEGORY_STORAGE, dc->categories);
    rc->phases.hold = msc_reset_hold;
}

static const TypeInfo msc_type_info = {
    .name = TYPE_JZ4740_MSC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740MSCState),
    .instance_init = msc_init,
    .class_init = msc_class_init,
};

static void msc_register_types(void)
{
    type_register_static(&msc_type_info);
}

type_init(msc_register_types)
