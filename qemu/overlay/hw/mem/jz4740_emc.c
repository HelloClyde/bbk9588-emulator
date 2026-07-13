/*
 * Ingenic JZ4740 external memory controller.
 *
 * Models the EMC register window and the NAND control/ECC status registers.
 * Raw NAND command/address/data handling is provided by bbk9588-nand.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/block/bbk9588_nand.h"
#include "hw/core/irq.h"
#include "hw/mem/jz4740_ecc.h"
#include "hw/mem/jz4740_emc.h"
#include "migration/vmstate.h"
#include "qemu/bswap.h"
#include "qemu/module.h"

#define EMC_MMIO_SIZE              0x10000u
#define EMC_REG_WORDS              (EMC_MMIO_SIZE / sizeof(uint32_t))

#define EMC_NFCSR                  0x050u
#define EMC_NFECCR                 0x100u
#define EMC_NFECC                  0x104u
#define EMC_NFPAR0                 0x108u
#define EMC_NFPAR1                 0x10cu
#define EMC_NFPAR2                 0x110u
#define EMC_NFINTS                 0x114u
#define EMC_NFINTE                 0x118u
#define EMC_NFERR0                 0x11cu
#define EMC_NFERR1                 0x120u
#define EMC_NFERR2                 0x124u
#define EMC_NFERR3                 0x128u

#define EMC_NFCSR_RW_MASK          0x000000ffu
#define EMC_NFECCR_RW_MASK         0x0000000du
#define EMC_NFECCR_ECCE            0x00000001u
#define EMC_NFECCR_ERST            0x00000002u
#define EMC_NFECCR_RSE             0x00000004u
#define EMC_NFECCR_ENCE            0x00000008u
#define EMC_NFECCR_PRDY            0x00000010u

#define EMC_NFINTS_ERR             0x00000001u
#define EMC_NFINTS_UNCOR           0x00000002u
#define EMC_NFINTS_ENCF            0x00000004u
#define EMC_NFINTS_DECF            0x00000008u
#define EMC_NFINTS_PADF            0x00000010u
#define EMC_NFINTS_STATUS_MASK     0x0000001fu
#define EMC_NFINTS_ERRC_SHIFT      29u
#define EMC_NFINTS_ERRC_MASK       0xe0000000u
#define EMC_NFINTE_RW_MASK         0x00000017u
#define EMC_NFERR_MASK             0x01ff01ffu

struct JZ4740EMCState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t regs[EMC_REG_WORDS];
    Bbk9588NandState *nand;
    uint32_t last_read_offset;
    uint32_t last_read_value;
    uint32_t last_write_offset;
    uint32_t last_write_value;
    uint8_t ecc_data[JZ4740_ECC_BLOCK_BYTES];
    uint32_t ecc_data_count;
    bool irq_level;
    JZ4740EMCBoardWriteCallback board_write_callback;
    void *board_write_opaque;
};

static void emc_update_irq(JZ4740EMCState *s)
{
    uint32_t status = s->regs[EMC_NFINTS / sizeof(uint32_t)];
    uint32_t enable = s->regs[EMC_NFINTE / sizeof(uint32_t)];
    bool level;

    level = ((status & EMC_NFINTS_PADF) &&
             (enable & EMC_NFINTS_PADF)) ||
            ((status & (EMC_NFINTS_ENCF | EMC_NFINTS_DECF)) &&
             (enable & EMC_NFINTS_ENCF)) ||
            ((status & EMC_NFINTS_UNCOR) &&
             (enable & EMC_NFINTS_UNCOR)) ||
            ((status & EMC_NFINTS_ERR) &&
             (enable & EMC_NFINTS_ERR));
    s->irq_level = level;
    qemu_set_irq(s->irq, level);
}

static void emc_ecc_clear_results(JZ4740EMCState *s)
{
    s->regs[EMC_NFECC / sizeof(uint32_t)] = 0;
    s->regs[EMC_NFPAR0 / sizeof(uint32_t)] = 0;
    s->regs[EMC_NFPAR1 / sizeof(uint32_t)] = 0;
    s->regs[EMC_NFPAR2 / sizeof(uint32_t)] = 0;
    s->regs[EMC_NFINTS / sizeof(uint32_t)] = 0;
    s->regs[EMC_NFERR0 / sizeof(uint32_t)] = 0;
    s->regs[EMC_NFERR1 / sizeof(uint32_t)] = 0;
    s->regs[EMC_NFERR2 / sizeof(uint32_t)] = 0;
    s->regs[EMC_NFERR3 / sizeof(uint32_t)] = 0;
    memset(s->ecc_data, 0, sizeof(s->ecc_data));
    s->ecc_data_count = 0;
    emc_update_irq(s);
}

static void emc_ecc_begin_block(JZ4740EMCState *s)
{
    memset(s->ecc_data, 0, sizeof(s->ecc_data));
    s->ecc_data_count = 0;
}

static void emc_store_rs_parity(JZ4740EMCState *s,
                                const uint8_t parity[JZ4740_RS_PARITY_BYTES])
{
    s->regs[EMC_NFPAR0 / sizeof(uint32_t)] = ldl_le_p(parity);
    s->regs[EMC_NFPAR1 / sizeof(uint32_t)] = ldl_le_p(parity + 4);
    s->regs[EMC_NFPAR2 / sizeof(uint32_t)] = parity[8];
}

static void emc_load_rs_parity(JZ4740EMCState *s,
                               uint8_t parity[JZ4740_RS_PARITY_BYTES])
{
    stl_le_p(parity, s->regs[EMC_NFPAR0 / sizeof(uint32_t)]);
    stl_le_p(parity + 4, s->regs[EMC_NFPAR1 / sizeof(uint32_t)]);
    parity[8] = s->regs[EMC_NFPAR2 / sizeof(uint32_t)] & 0xffu;
}

static void emc_rs_encode_complete(JZ4740EMCState *s)
{
    uint8_t parity[JZ4740_RS_PARITY_BYTES];

    jz4740_rs_encode(s->ecc_data, parity);
    emc_store_rs_parity(s, parity);
    s->regs[EMC_NFINTS / sizeof(uint32_t)] |= EMC_NFINTS_ENCF;
    emc_update_irq(s);
}

static void emc_rs_decode_complete(JZ4740EMCState *s)
{
    JZ4740RSCorrection corrections[JZ4740_RS_MAX_ERRORS];
    uint8_t parity[JZ4740_RS_PARITY_BYTES];
    uint32_t *status = &s->regs[EMC_NFINTS / sizeof(uint32_t)];
    int error_count;

    emc_load_rs_parity(s, parity);
    error_count = jz4740_rs_decode(s->ecc_data, parity, corrections);
    *status &= ~(EMC_NFINTS_ERRC_MASK | EMC_NFINTS_DECF |
                 EMC_NFINTS_UNCOR | EMC_NFINTS_ERR);
    for (unsigned i = 0; i < JZ4740_RS_MAX_ERRORS; i++) {
        s->regs[(EMC_NFERR0 / sizeof(uint32_t)) + i] = 0;
    }
    if (error_count < 0) {
        *status |= EMC_NFINTS_DECF | EMC_NFINTS_UNCOR | EMC_NFINTS_ERR;
    } else if (error_count > 0) {
        *status |= EMC_NFINTS_DECF | EMC_NFINTS_ERR |
                   ((uint32_t)error_count << EMC_NFINTS_ERRC_SHIFT);
        for (int i = 0; i < error_count; i++) {
            s->regs[(EMC_NFERR0 / sizeof(uint32_t)) + i] =
                ((uint32_t)corrections[i].index << 16) |
                corrections[i].mask;
        }
    } else {
        *status |= EMC_NFINTS_DECF;
    }
    emc_update_irq(s);
}

static void emc_nand_data(void *opaque, uint32_t value, unsigned size,
                          bool write)
{
    JZ4740EMCState *s = opaque;
    uint32_t control = s->regs[EMC_NFECCR / sizeof(uint32_t)];
    bool encode = (control & EMC_NFECCR_ENCE) != 0;

    if (!(control & EMC_NFECCR_ECCE) || encode != write ||
        s->ecc_data_count >= JZ4740_ECC_BLOCK_BYTES) {
        return;
    }
    for (unsigned i = 0; i < MIN(size, 4u) &&
         s->ecc_data_count < JZ4740_ECC_BLOCK_BYTES; i++) {
        s->ecc_data[s->ecc_data_count++] = value >> (i * 8);
    }
    if (s->ecc_data_count != JZ4740_ECC_BLOCK_BYTES) {
        return;
    }
    if (control & EMC_NFECCR_RSE) {
        if (encode) {
            emc_rs_encode_complete(s);
        } else {
            s->regs[EMC_NFINTS / sizeof(uint32_t)] |= EMC_NFINTS_PADF;
            emc_update_irq(s);
        }
    } else {
        s->regs[EMC_NFECC / sizeof(uint32_t)] =
            jz4740_hamming_encode(s->ecc_data, s->ecc_data_count);
    }
}

void jz4740_emc_attach_nand(JZ4740EMCState *s, Bbk9588NandState *nand)
{
    if (!s) {
        return;
    }
    if (s->nand && s->nand != nand) {
        bbk9588_nand_set_data_callback(s->nand, NULL, NULL);
    }
    s->nand = nand;
    bbk9588_nand_set_data_callback(nand, emc_nand_data, s);
}

void jz4740_emc_set_board_write_callback(
    JZ4740EMCState *s, JZ4740EMCBoardWriteCallback callback, void *opaque)
{
    if (!s) {
        return;
    }
    s->board_write_callback = callback;
    s->board_write_opaque = opaque;
}

static uint32_t emc_read_reg(JZ4740EMCState *s, hwaddr offset)
{
    switch (offset) {
    case EMC_NFCSR:
        return s->regs[offset / sizeof(uint32_t)] & EMC_NFCSR_RW_MASK;
    case EMC_NFECCR:
        return s->regs[offset / sizeof(uint32_t)] & EMC_NFECCR_RW_MASK;
    case EMC_NFECC:
        if ((s->regs[EMC_NFECCR / sizeof(uint32_t)] &
             (EMC_NFECCR_ECCE | EMC_NFECCR_RSE)) == EMC_NFECCR_ECCE) {
            s->regs[EMC_NFECC / sizeof(uint32_t)] =
                jz4740_hamming_encode(s->ecc_data, s->ecc_data_count);
        }
        return s->regs[offset / sizeof(uint32_t)] & 0x00ffffffu;
    case EMC_NFPAR2:
        return s->regs[offset / sizeof(uint32_t)] & 0xffu;
    case EMC_NFINTS:
        return s->regs[offset / sizeof(uint32_t)];
    case EMC_NFINTE:
        return s->regs[offset / sizeof(uint32_t)] & EMC_NFINTE_RW_MASK;
    case EMC_NFERR0:
    case EMC_NFERR1:
    case EMC_NFERR2:
    case EMC_NFERR3:
        return s->regs[offset / sizeof(uint32_t)] & EMC_NFERR_MASK;
    default:
        return s->regs[offset / sizeof(uint32_t)];
    }
}

static void emc_write_reg(JZ4740EMCState *s, hwaddr offset, uint32_t value)
{
    uint32_t *reg = &s->regs[offset / sizeof(uint32_t)];

    switch (offset) {
    case EMC_NFCSR:
        *reg = value & EMC_NFCSR_RW_MASK;
        break;
    case EMC_NFECCR: {
        uint32_t old_control = *reg;

        if (value & EMC_NFECCR_ERST) {
            emc_ecc_clear_results(s);
        }
        *reg = value & EMC_NFECCR_RW_MASK;
        if ((*reg & EMC_NFECCR_ECCE) &&
            !(old_control & EMC_NFECCR_ECCE) &&
            !(value & EMC_NFECCR_ERST)) {
            emc_ecc_begin_block(s);
        }
        if ((value & EMC_NFECCR_PRDY) &&
            ((*reg & (EMC_NFECCR_ECCE | EMC_NFECCR_RSE |
                      EMC_NFECCR_ENCE)) ==
             (EMC_NFECCR_ECCE | EMC_NFECCR_RSE))) {
            emc_rs_decode_complete(s);
        }
        break;
    }
    case EMC_NFECC:
        break;
    case EMC_NFPAR0:
    case EMC_NFPAR1:
        *reg = value;
        break;
    case EMC_NFPAR2:
        *reg = value & 0xffu;
        break;
    case EMC_NFINTS:
        *reg &= value | ~EMC_NFINTS_STATUS_MASK;
        emc_update_irq(s);
        break;
    case EMC_NFINTE:
        *reg = value & EMC_NFINTE_RW_MASK;
        emc_update_irq(s);
        break;
    case EMC_NFERR0:
    case EMC_NFERR1:
    case EMC_NFERR2:
    case EMC_NFERR3:
        break;
    default:
        *reg = value;
        break;
    }
    if (s->board_write_callback) {
        s->board_write_callback(s->board_write_opaque, offset, *reg);
    }
}

static uint64_t emc_extract32(uint32_t value, hwaddr offset, unsigned size)
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

static uint64_t emc_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740EMCState *s = opaque;
    hwaddr aligned_offset = offset & ~3u;
    uint32_t value;

    if (aligned_offset >= EMC_MMIO_SIZE) {
        return 0;
    }
    value = emc_read_reg(s, aligned_offset);
    s->last_read_offset = offset;
    s->last_read_value = value;
    return emc_extract32(value, offset, size);
}

static void emc_write(void *opaque, hwaddr offset, uint64_t value,
                      unsigned size)
{
    JZ4740EMCState *s = opaque;
    hwaddr aligned_offset = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t current;
    uint32_t mask;
    uint32_t merged;

    if (aligned_offset >= EMC_MMIO_SIZE) {
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
    current = s->regs[aligned_offset / sizeof(uint32_t)];
    merged = (current & ~mask) | (((uint32_t)value << shift) & mask);
    emc_write_reg(s, aligned_offset, merged);
    s->last_write_offset = offset;
    s->last_write_value = merged;
}

static const MemoryRegionOps emc_ops = {
    .read = emc_read,
    .write = emc_write,
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

void jz4740_emc_get_diagnostics(JZ4740EMCState *s,
                                JZ4740EMCDiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    diagnostics->nfcsr = s->regs[EMC_NFCSR / sizeof(uint32_t)];
    diagnostics->nfeccr = s->regs[EMC_NFECCR / sizeof(uint32_t)];
    diagnostics->nfecc = s->regs[EMC_NFECC / sizeof(uint32_t)];
    for (unsigned i = 0; i < ARRAY_SIZE(diagnostics->nfpar); i++) {
        diagnostics->nfpar[i] =
            s->regs[(EMC_NFPAR0 / sizeof(uint32_t)) + i];
    }
    diagnostics->nfints = s->regs[EMC_NFINTS / sizeof(uint32_t)];
    diagnostics->nfinte = s->regs[EMC_NFINTE / sizeof(uint32_t)];
    for (unsigned i = 0; i < ARRAY_SIZE(diagnostics->nferr); i++) {
        diagnostics->nferr[i] =
            s->regs[(EMC_NFERR0 / sizeof(uint32_t)) + i];
    }
    diagnostics->ecc_data_count = s->ecc_data_count;
    diagnostics->last_read_offset = s->last_read_offset;
    diagnostics->last_read_value = s->last_read_value;
    diagnostics->last_write_offset = s->last_write_offset;
    diagnostics->last_write_value = s->last_write_value;
    diagnostics->irq_level = s->irq_level;
}

static void emc_reset_hold(Object *obj, ResetType type)
{
    JZ4740EMCState *s = JZ4740_EMC(obj);

    memset(s->regs, 0, sizeof(s->regs));
    s->last_read_offset = 0;
    s->last_read_value = 0;
    s->last_write_offset = 0;
    s->last_write_value = 0;
    memset(s->ecc_data, 0, sizeof(s->ecc_data));
    s->ecc_data_count = 0;
    emc_update_irq(s);
}

static int emc_post_load(void *opaque, int version_id)
{
    JZ4740EMCState *s = opaque;

    if (s->ecc_data_count > JZ4740_ECC_BLOCK_BYTES) {
        s->ecc_data_count = JZ4740_ECC_BLOCK_BYTES;
    }
    emc_update_irq(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_emc = {
    .name = TYPE_JZ4740_EMC,
    .version_id = 2,
    .minimum_version_id = 1,
    .post_load = emc_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4740EMCState, EMC_REG_WORDS),
        VMSTATE_UINT32(last_read_offset, JZ4740EMCState),
        VMSTATE_UINT32(last_read_value, JZ4740EMCState),
        VMSTATE_UINT32(last_write_offset, JZ4740EMCState),
        VMSTATE_UINT32(last_write_value, JZ4740EMCState),
        VMSTATE_UINT8_ARRAY_V(ecc_data, JZ4740EMCState,
                              JZ4740_ECC_BLOCK_BYTES, 2),
        VMSTATE_UINT32_V(ecc_data_count, JZ4740EMCState, 2),
        VMSTATE_END_OF_LIST()
    },
};

static void emc_init(Object *obj)
{
    JZ4740EMCState *s = JZ4740_EMC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &emc_ops, s, TYPE_JZ4740_EMC,
                          EMC_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
}

static void emc_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_emc;
    set_bit(DEVICE_CATEGORY_STORAGE, dc->categories);
    rc->phases.hold = emc_reset_hold;
}

static const TypeInfo emc_type_info = {
    .name = TYPE_JZ4740_EMC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740EMCState),
    .instance_init = emc_init,
    .class_init = emc_class_init,
};

static void emc_register_types(void)
{
    type_register_static(&emc_type_info);
}

type_init(emc_register_types)
