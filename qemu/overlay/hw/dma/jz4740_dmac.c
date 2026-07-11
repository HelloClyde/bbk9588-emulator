/*
 * Ingenic JZ4740 DMA controller.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "exec/cpu-common.h"
#include "hw/core/irq.h"
#include "hw/core/qdev-properties.h"
#include "hw/dma/jz4740_dmac.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "qemu/timer.h"

#define DMAC_MMIO_SIZE        0x10000u
#define DMAC_REGS             (DMAC_MMIO_SIZE / sizeof(uint32_t))
#define DMAC_CHANNEL_STRIDE   0x20u
#define DMAC_DSA              0x00u
#define DMAC_DTA              0x04u
#define DMAC_DTC              0x08u
#define DMAC_DRT              0x0cu
#define DMAC_DCS              0x10u
#define DMAC_DCM              0x14u
#define DMAC_DDA              0x18u
#define DMAC_DMACR            0x300u
#define DMAC_DIRQP            0x304u
#define DMAC_DDB              0x308u
#define DMAC_DDBS             0x30cu

#define DMAC_DMACR_DMAE       0x00000001u
#define DMAC_DMACR_AR         0x00000004u
#define DMAC_DMACR_HLT        0x00000008u
#define DMAC_DMACR_PM_MASK    0x00000300u
#define DMAC_DRT_AUTO         8u
#define DMAC_DRT_MASK         0x0000001fu
#define DMAC_DTC_MASK         0x00ffffffu
#define DMAC_DCS_NDES         0x80000000u
#define DMAC_DCS_CDOA_MASK    0x00ff0000u
#define DMAC_DCS_CDOA_SHIFT   16u
#define DMAC_DCS_INV          0x00000040u
#define DMAC_DCS_AR           0x00000010u
#define DMAC_DCS_TT           0x00000008u
#define DMAC_DCS_HLT          0x00000004u
#define DMAC_DCS_CT           0x00000002u
#define DMAC_DCS_CTE          0x00000001u
#define DMAC_DCS_STATUS_MASK  \
    (DMAC_DCS_INV | DMAC_DCS_AR | DMAC_DCS_TT | DMAC_DCS_HLT | DMAC_DCS_CT)
#define DMAC_DCM_SAI          0x00800000u
#define DMAC_DCM_DAI          0x00400000u
#define DMAC_DCM_SP_SHIFT     14u
#define DMAC_DCM_DP_SHIFT     12u
#define DMAC_DCM_PORT_MASK    0x00000003u
#define DMAC_DCM_TSZ_SHIFT    8u
#define DMAC_DCM_TSZ_MASK     0x00000700u
#define DMAC_DCM_TM           0x00000080u
#define DMAC_DCM_V            0x00000010u
#define DMAC_DCM_VM           0x00000008u
#define DMAC_DCM_VIE          0x00000004u
#define DMAC_DCM_TIE          0x00000002u
#define DMAC_DCM_LINK         0x00000001u
#define DMAC_DDA_DBA_MASK     0xfffff000u
#define DMAC_DDA_DOA_MASK     0x00000ff0u
#define DMAC_DDA_DOA_SHIFT    4u
#define DMAC_DDA_ALIGN_MASK   0x0000000fu
#define DMAC_DESC_BYTES       16u
#define DMAC_DESC_DCM         0x00u
#define DMAC_DESC_DSA         0x04u
#define DMAC_DESC_DTA         0x08u
#define DMAC_DESC_DTC         0x0cu
#define DMAC_DESC_DOA_SHIFT   24u
#define DMAC_CHANNEL_MASK     ((1u << JZ4740_DMAC_CHANNELS) - 1u)
#define DMAC_PHYS_MASK        0x1fffffffu

struct JZ4740DMACState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    uint32_t regs[DMAC_REGS];
    uint32_t request_levels;
    uint64_t ram_size;
    bool irq_level;
    bool transferring;

    JZ4740DMACPeripheralOps ops;
    void *ops_opaque;

    uint64_t audio_completion_count;
    uint64_t audio_rearm_count;
    int64_t audio_last_completion_ns;
    uint64_t audio_completion_underruns;
    uint64_t audio_last_rearm_gap_ns;
    uint64_t audio_max_rearm_gap_ns;
    uint64_t audio_total_rearm_gap_ns;
    uint64_t audio_last_gap_underruns;
    uint64_t audio_total_gap_underruns;
    uint32_t audio_last_units;
    uint32_t audio_completion_fifo;
    uint32_t audio_rearm_fifo;
};

static uint32_t dmac_reg(JZ4740DMACState *s, hwaddr offset)
{
    return s->regs[offset / sizeof(uint32_t)];
}

static void dmac_set_reg(JZ4740DMACState *s, hwaddr offset, uint32_t value)
{
    s->regs[offset / sizeof(uint32_t)] = value;
}

static bool dmac_channel_offset(hwaddr offset, unsigned *channel,
                                hwaddr *reg_offset)
{
    unsigned ch = offset / DMAC_CHANNEL_STRIDE;
    hwaddr reg = (offset % DMAC_CHANNEL_STRIDE) & ~3u;

    if (ch >= JZ4740_DMAC_CHANNELS || reg > DMAC_DDA) {
        return false;
    }
    if (channel) {
        *channel = ch;
    }
    if (reg_offset) {
        *reg_offset = reg;
    }
    return true;
}

static bool dmac_irq_pending_internal(JZ4740DMACState *s)
{
    uint32_t irqp = dmac_reg(s, DMAC_DIRQP);

    for (unsigned ch = 0; ch < JZ4740_DMAC_CHANNELS; ch++) {
        uint32_t base = ch * DMAC_CHANNEL_STRIDE;
        uint32_t status = dmac_reg(s, base + DMAC_DCS);
        uint32_t command = dmac_reg(s, base + DMAC_DCM);

        if (!(irqp & (1u << ch))) {
            continue;
        }
        if ((status & (DMAC_DCS_AR | DMAC_DCS_HLT)) ||
            ((status & DMAC_DCS_INV) && (command & DMAC_DCM_VIE)) ||
            ((status & (DMAC_DCS_TT | DMAC_DCS_CT)) &&
             (command & DMAC_DCM_TIE))) {
            return true;
        }
    }
    return false;
}

static void dmac_sync_irq(JZ4740DMACState *s)
{
    uint32_t irqp = 0;
    uint32_t global_status = 0;
    bool level;

    for (unsigned ch = 0; ch < JZ4740_DMAC_CHANNELS; ch++) {
        uint32_t status = dmac_reg(s, ch * DMAC_CHANNEL_STRIDE + DMAC_DCS);

        if (status & DMAC_DCS_STATUS_MASK) {
            irqp |= 1u << ch;
        }
        if (status & DMAC_DCS_AR) {
            global_status |= DMAC_DMACR_AR;
        }
        if (status & DMAC_DCS_HLT) {
            global_status |= DMAC_DMACR_HLT;
        }
    }
    dmac_set_reg(s, DMAC_DIRQP, irqp & DMAC_CHANNEL_MASK);
    dmac_set_reg(s, DMAC_DMACR,
                 (dmac_reg(s, DMAC_DMACR) &
                  ~(DMAC_DMACR_AR | DMAC_DMACR_HLT)) | global_status);
    level = dmac_irq_pending_internal(s);
    if (level != s->irq_level) {
        s->irq_level = level;
        qemu_set_irq(s->irq, level);
    }
}

static void dmac_trace(JZ4740DMACState *s, uint32_t event,
                       unsigned channel, hwaddr offset, uint32_t value)
{
    if (s->ops.trace) {
        s->ops.trace(s->ops_opaque, event, channel, offset, value);
    }
}

static bool dmac_ram_range_valid(JZ4740DMACState *s, uint32_t addr,
                                 uint32_t bytes)
{
    uint64_t phys = addr & DMAC_PHYS_MASK;

    return bytes <= s->ram_size && phys <= s->ram_size - bytes;
}

static void dmac_memory_read(uint32_t addr, void *buf, uint32_t bytes)
{
    cpu_physical_memory_read(addr & DMAC_PHYS_MASK, buf, bytes);
}

static void dmac_memory_write(uint32_t addr, const void *buf, uint32_t bytes)
{
    cpu_physical_memory_write(addr & DMAC_PHYS_MASK, buf, bytes);
}

static uint32_t dmac_memory_read_le32(uint32_t addr)
{
    uint8_t buf[4];

    dmac_memory_read(addr, buf, sizeof(buf));
    return buf[0] | (buf[1] << 8) | (buf[2] << 16) | (buf[3] << 24);
}

static void dmac_memory_write_le32(uint32_t addr, uint32_t value)
{
    uint8_t buf[4] = {
        value,
        value >> 8,
        value >> 16,
        value >> 24,
    };

    dmac_memory_write(addr, buf, sizeof(buf));
}

static bool dmac_channel_enabled(JZ4740DMACState *s, unsigned channel)
{
    if (channel >= JZ4740_DMAC_CHANNELS ||
        !(dmac_reg(s, DMAC_DMACR) & DMAC_DMACR_DMAE)) {
        return false;
    }
    return (dmac_reg(s, channel * DMAC_CHANNEL_STRIDE + DMAC_DCS) &
            DMAC_DCS_CTE) != 0;
}

static bool dmac_audio_request(JZ4740DMACState *s, unsigned channel)
{
    uint32_t request;

    if (channel >= JZ4740_DMAC_CHANNELS) {
        return false;
    }
    request = dmac_reg(s, channel * DMAC_CHANNEL_STRIDE + DMAC_DRT) &
              DMAC_DRT_MASK;
    return request == JZ4740_DMAC_REQUEST_AIC_TX ||
           request == JZ4740_DMAC_REQUEST_AIC_RX;
}

static void dmac_endpoint_diagnostics(
    JZ4740DMACState *s, unsigned request,
    JZ4740DMACEndpointDiagnostics *diagnostics)
{
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (s->ops.get_diagnostics) {
        s->ops.get_diagnostics(s->ops_opaque, request, diagnostics);
    }
}

static void dmac_record_audio_completion(JZ4740DMACState *s,
                                         unsigned channel)
{
    JZ4740DMACEndpointDiagnostics diagnostics;
    uint32_t request;

    if (!dmac_audio_request(s, channel)) {
        return;
    }
    request = dmac_reg(s, channel * DMAC_CHANNEL_STRIDE + DMAC_DRT) &
              DMAC_DRT_MASK;
    dmac_endpoint_diagnostics(s, request, &diagnostics);
    s->audio_completion_count++;
    s->audio_last_completion_ns = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    s->audio_completion_underruns = diagnostics.underruns;
    s->audio_completion_fifo = diagnostics.fifo_level;
    if (s->ops.complete) {
        s->ops.complete(s->ops_opaque, request);
    }
}

static void dmac_record_audio_rearm(JZ4740DMACState *s, unsigned channel,
                                    uint32_t old_status,
                                    uint32_t new_status)
{
    JZ4740DMACEndpointDiagnostics diagnostics;
    uint32_t request;
    uint64_t gap;
    uint64_t gap_underruns;
    int64_t now;

    if ((old_status & DMAC_DCS_CTE) || !(new_status & DMAC_DCS_CTE) ||
        (new_status & DMAC_DCS_TT) || !dmac_audio_request(s, channel) ||
        s->audio_last_completion_ns <= 0) {
        return;
    }
    request = dmac_reg(s, channel * DMAC_CHANNEL_STRIDE + DMAC_DRT) &
              DMAC_DRT_MASK;
    now = qemu_clock_get_ns(QEMU_CLOCK_VIRTUAL);
    gap = (uint64_t)MAX(now - s->audio_last_completion_ns, 0LL);
    dmac_endpoint_diagnostics(s, request, &diagnostics);
    gap_underruns = diagnostics.underruns - s->audio_completion_underruns;

    s->audio_rearm_count++;
    s->audio_last_rearm_gap_ns = gap;
    s->audio_max_rearm_gap_ns = MAX(s->audio_max_rearm_gap_ns, gap);
    s->audio_total_rearm_gap_ns += gap;
    s->audio_last_gap_underruns = gap_underruns;
    s->audio_total_gap_underruns += gap_underruns;
    s->audio_last_units = dmac_reg(
        s, channel * DMAC_CHANNEL_STRIDE + DMAC_DTC) & DMAC_DTC_MASK;
    s->audio_rearm_fifo = diagnostics.fifo_level;
    s->audio_last_completion_ns = 0;
}

static void dmac_set_terminal_count(JZ4740DMACState *s, unsigned channel)
{
    uint32_t base;

    if (channel >= JZ4740_DMAC_CHANNELS) {
        return;
    }
    base = channel * DMAC_CHANNEL_STRIDE;
    dmac_set_reg(s, base + DMAC_DTC, 0);
    dmac_set_reg(s, base + DMAC_DCS,
                 (dmac_reg(s, base + DMAC_DCS) & ~DMAC_DCS_CTE) |
                 DMAC_DCS_TT);
    dmac_sync_irq(s);
}

static void dmac_set_address_error(JZ4740DMACState *s, unsigned channel)
{
    uint32_t base;

    if (channel >= JZ4740_DMAC_CHANNELS) {
        return;
    }
    base = channel * DMAC_CHANNEL_STRIDE;
    dmac_set_reg(s, base + DMAC_DCS,
                 dmac_reg(s, base + DMAC_DCS) | DMAC_DCS_AR);
    dmac_set_reg(s, DMAC_DMACR, dmac_reg(s, DMAC_DMACR) | DMAC_DMACR_AR);
    dmac_sync_irq(s);
}

static bool dmac_descriptor_address_valid(JZ4740DMACState *s,
                                          uint32_t desc_addr)
{
    return (desc_addr & DMAC_DDA_ALIGN_MASK) == 0 &&
           dmac_ram_range_valid(s, desc_addr, DMAC_DESC_BYTES);
}

static uint32_t dmac_descriptor_next(uint32_t desc_addr, uint32_t desc_dtc)
{
    uint32_t next_doa =
        ((desc_dtc >> DMAC_DESC_DOA_SHIFT) & 0xffu) << DMAC_DDA_DOA_SHIFT;

    return (desc_addr & DMAC_DDA_DBA_MASK) | next_doa;
}

static void dmac_fetch_descriptor(JZ4740DMACState *s, unsigned channel)
{
    uint32_t base = channel * DMAC_CHANNEL_STRIDE;
    uint32_t bit = 1u << channel;
    uint32_t status = dmac_reg(s, base + DMAC_DCS);
    uint32_t desc_addr = dmac_reg(s, base + DMAC_DDA) &
                         ~DMAC_DDA_ALIGN_MASK;
    uint32_t command;
    uint32_t desc_dtc;

    if (channel >= JZ4740_DMAC_CHANNELS || (status & DMAC_DCS_NDES) ||
        !(status & DMAC_DCS_CTE) || !(dmac_reg(s, DMAC_DDB) & bit)) {
        return;
    }
    dmac_set_reg(s, DMAC_DDB, dmac_reg(s, DMAC_DDB) & ~bit);
    if (!dmac_descriptor_address_valid(s, desc_addr)) {
        dmac_set_address_error(s, channel);
        return;
    }

    command = dmac_memory_read_le32(desc_addr + DMAC_DESC_DCM);
    if ((command & DMAC_DCM_VM) && !(command & DMAC_DCM_V)) {
        dmac_set_reg(s, base + DMAC_DCS,
                     (status & ~DMAC_DCS_CTE) | DMAC_DCS_INV);
        dmac_set_reg(s, base + DMAC_DCM, command);
        dmac_sync_irq(s);
        return;
    }
    desc_dtc = dmac_memory_read_le32(desc_addr + DMAC_DESC_DTC);
    dmac_set_reg(s, base + DMAC_DCM, command);
    dmac_set_reg(s, base + DMAC_DSA,
                 dmac_memory_read_le32(desc_addr + DMAC_DESC_DSA));
    dmac_set_reg(s, base + DMAC_DTA,
                 dmac_memory_read_le32(desc_addr + DMAC_DESC_DTA));
    dmac_set_reg(s, base + DMAC_DTC, desc_dtc & DMAC_DTC_MASK);
    dmac_trace(s, 5, channel, desc_addr, command);
}

static void dmac_finish_transfer(JZ4740DMACState *s, unsigned channel,
                                 uint32_t command)
{
    uint32_t base = channel * DMAC_CHANNEL_STRIDE;
    uint32_t status;
    uint32_t desc_addr;
    uint32_t desc_dtc;
    uint32_t cdoa;

    if (channel >= JZ4740_DMAC_CHANNELS) {
        return;
    }
    dmac_record_audio_completion(s, channel);
    if (dmac_reg(s, base + DMAC_DCS) & DMAC_DCS_NDES) {
        dmac_set_terminal_count(s, channel);
        return;
    }
    desc_addr = dmac_reg(s, base + DMAC_DDA) & ~DMAC_DDA_ALIGN_MASK;
    desc_dtc = dmac_memory_read_le32(desc_addr + DMAC_DESC_DTC);
    cdoa = ((desc_addr & DMAC_DDA_DOA_MASK) >> DMAC_DDA_DOA_SHIFT) <<
           DMAC_DCS_CDOA_SHIFT;
    dmac_set_reg(s, base + DMAC_DTC, 0);
    if (command & DMAC_DCM_VM) {
        dmac_memory_write_le32(desc_addr + DMAC_DESC_DCM,
                               command & ~DMAC_DCM_V);
    }
    status = (dmac_reg(s, base + DMAC_DCS) &
              ~(DMAC_DCS_CDOA_MASK | DMAC_DCS_CT | DMAC_DCS_TT)) | cdoa;
    if (command & DMAC_DCM_LINK) {
        dmac_set_reg(s, base + DMAC_DDA,
                     dmac_descriptor_next(desc_addr, desc_dtc));
        status |= DMAC_DCS_CT;
    } else {
        status = (status & ~DMAC_DCS_CTE) | DMAC_DCS_TT;
    }
    dmac_set_reg(s, base + DMAC_DCS, status);
    dmac_sync_irq(s);
}

static uint32_t dmac_unit_bytes(uint32_t command)
{
    switch ((command & DMAC_DCM_TSZ_MASK) >> DMAC_DCM_TSZ_SHIFT) {
    case 0:
        return 4;
    case 1:
        return 1;
    case 2:
        return 2;
    case 3:
        return 16;
    case 4:
        return 32;
    default:
        return 0;
    }
}

static uint32_t dmac_port_bytes(uint32_t command, unsigned shift)
{
    switch ((command >> shift) & DMAC_DCM_PORT_MASK) {
    case 0:
        return 4;
    case 1:
        return 1;
    case 2:
        return 2;
    default:
        return 0;
    }
}

static bool dmac_try_bulk_transfer(JZ4740DMACState *s, unsigned channel)
{
    uint32_t base;
    uint32_t count;
    uint32_t command;
    bool handled;

    if (!s->ops.bulk_transfer || channel >= JZ4740_DMAC_CHANNELS) {
        return false;
    }
    dmac_fetch_descriptor(s, channel);
    if (!dmac_channel_enabled(s, channel)) {
        return false;
    }
    base = channel * DMAC_CHANNEL_STRIDE;
    count = dmac_reg(s, base + DMAC_DTC) & DMAC_DTC_MASK;
    if (count == 0) {
        return false;
    }
    command = dmac_reg(s, base + DMAC_DCM);
    handled = s->ops.bulk_transfer(
        s->ops_opaque, channel, dmac_reg(s, base + DMAC_DRT) & DMAC_DRT_MASK,
        dmac_reg(s, base + DMAC_DSA), dmac_reg(s, base + DMAC_DTA), count,
        command);
    if (handled) {
        dmac_finish_transfer(s, channel, command);
    }
    return handled;
}

static void dmac_try_auto_transfer(JZ4740DMACState *s, unsigned channel)
{
    uint32_t base;
    uint32_t source;
    uint32_t target;
    uint32_t count;
    uint32_t command;
    uint32_t unit_bytes;
    uint64_t bytes64;
    uint32_t bytes;
    uint32_t done = 0;

    if (channel >= JZ4740_DMAC_CHANNELS ||
        !dmac_channel_enabled(s, channel)) {
        return;
    }
    base = channel * DMAC_CHANNEL_STRIDE;
    dmac_fetch_descriptor(s, channel);
    if (!dmac_channel_enabled(s, channel) ||
        (dmac_reg(s, base + DMAC_DRT) & DMAC_DRT_MASK) != DMAC_DRT_AUTO) {
        return;
    }
    count = dmac_reg(s, base + DMAC_DTC) & DMAC_DTC_MASK;
    if (count == 0) {
        return;
    }
    command = dmac_reg(s, base + DMAC_DCM);
    unit_bytes = dmac_unit_bytes(command);
    bytes64 = (uint64_t)count * unit_bytes;
    if (unit_bytes == 0 || bytes64 == 0 || bytes64 > UINT32_MAX) {
        dmac_set_address_error(s, channel);
        return;
    }
    bytes = bytes64;
    source = dmac_reg(s, base + DMAC_DSA);
    target = dmac_reg(s, base + DMAC_DTA);
    if (!dmac_ram_range_valid(s, source, bytes) ||
        !dmac_ram_range_valid(s, target, bytes)) {
        dmac_set_address_error(s, channel);
        return;
    }
    if ((command & (DMAC_DCM_SAI | DMAC_DCM_DAI)) ==
        (DMAC_DCM_SAI | DMAC_DCM_DAI)) {
        uint8_t chunk[4096];

        while (done < bytes) {
            uint32_t n = MIN((uint32_t)sizeof(chunk), bytes - done);

            dmac_memory_read(source + done, chunk, n);
            dmac_memory_write(target + done, chunk, n);
            done += n;
        }
    } else {
        uint8_t unit[32];

        while (done < bytes) {
            uint32_t source_addr = source +
                ((command & DMAC_DCM_SAI) ? done : 0);
            uint32_t target_addr = target +
                ((command & DMAC_DCM_DAI) ? done : 0);

            dmac_memory_read(source_addr, unit, unit_bytes);
            dmac_memory_write(target_addr, unit, unit_bytes);
            done += unit_bytes;
        }
    }
    if (command & DMAC_DCM_SAI) {
        dmac_set_reg(s, base + DMAC_DSA, source + bytes);
    }
    if (command & DMAC_DCM_DAI) {
        dmac_set_reg(s, base + DMAC_DTA, target + bytes);
    }
    dmac_set_reg(s, DMAC_DDB, dmac_reg(s, DMAC_DDB) & ~(1u << channel));
    dmac_finish_transfer(s, channel, command);
    dmac_trace(s, 4, channel, base, bytes);
}

static void dmac_try_audio_transfer(JZ4740DMACState *s, unsigned channel)
{
    uint32_t base;
    uint32_t source;
    uint32_t target;
    uint32_t count;
    uint32_t request;
    uint32_t command;
    uint32_t unit_bytes;
    uint32_t sample_bytes;
    uint32_t transferred = 0;
    bool transmit;

    if (channel >= JZ4740_DMAC_CHANNELS ||
        !dmac_channel_enabled(s, channel)) {
        return;
    }
    base = channel * DMAC_CHANNEL_STRIDE;
    dmac_fetch_descriptor(s, channel);
    if (!dmac_channel_enabled(s, channel)) {
        return;
    }
    count = dmac_reg(s, base + DMAC_DTC) & DMAC_DTC_MASK;
    request = dmac_reg(s, base + DMAC_DRT) & DMAC_DRT_MASK;
    if (count == 0 || (request != JZ4740_DMAC_REQUEST_AIC_TX &&
                       request != JZ4740_DMAC_REQUEST_AIC_RX) ||
        !(s->request_levels & (1u << request))) {
        return;
    }
    transmit = request == JZ4740_DMAC_REQUEST_AIC_TX;
    command = dmac_reg(s, base + DMAC_DCM);
    unit_bytes = dmac_unit_bytes(command);
    sample_bytes = dmac_port_bytes(command,
        transmit ? DMAC_DCM_DP_SHIFT : DMAC_DCM_SP_SHIFT);
    if (unit_bytes == 0 || sample_bytes == 0 ||
        unit_bytes % sample_bytes != 0) {
        dmac_set_address_error(s, channel);
        return;
    }
    source = dmac_reg(s, base + DMAC_DSA);
    target = dmac_reg(s, base + DMAC_DTA);
    if (!s->ops.address_valid ||
        !s->ops.address_valid(s->ops_opaque, request,
                              transmit ? target : source)) {
        dmac_set_address_error(s, channel);
        return;
    }
    while (count != 0) {
        uint8_t unit[32];
        size_t done;

        if (!(s->request_levels & (1u << request)) &&
            !(command & DMAC_DCM_TM)) {
            break;
        }
        if (transmit) {
            if (!dmac_ram_range_valid(s, source, unit_bytes) || !s->ops.write) {
                dmac_set_address_error(s, channel);
                return;
            }
            dmac_memory_read(source, unit, unit_bytes);
            done = s->ops.write(s->ops_opaque, request, unit, unit_bytes,
                                sample_bytes);
        } else {
            if (!dmac_ram_range_valid(s, target, unit_bytes) || !s->ops.read) {
                dmac_set_address_error(s, channel);
                return;
            }
            done = s->ops.read(s->ops_opaque, request, unit, unit_bytes,
                               sample_bytes);
            if (done == unit_bytes) {
                dmac_memory_write(target, unit, unit_bytes);
            }
        }
        if (done != unit_bytes) {
            break;
        }
        count--;
        transferred += unit_bytes;
        if (command & DMAC_DCM_SAI) {
            source += unit_bytes;
        }
        if (command & DMAC_DCM_DAI) {
            target += unit_bytes;
        }
    }
    dmac_set_reg(s, base + DMAC_DTC, count);
    dmac_set_reg(s, base + DMAC_DSA, source);
    dmac_set_reg(s, base + DMAC_DTA, target);
    if (count == 0) {
        dmac_finish_transfer(s, channel, command);
        dmac_trace(s, 3, channel, base, transferred);
    } else if (transferred != 0) {
        dmac_trace(s, 6, channel, base, transferred);
    }
    dmac_sync_irq(s);
}

static void dmac_try_channel(JZ4740DMACState *s, unsigned channel,
                             bool include_bulk)
{
    if (include_bulk && dmac_try_bulk_transfer(s, channel)) {
        return;
    }
    dmac_try_auto_transfer(s, channel);
    dmac_try_audio_transfer(s, channel);
}

void jz4740_dmac_kick(JZ4740DMACState *s)
{
    if (!s || s->transferring) {
        return;
    }
    s->transferring = true;
    for (unsigned channel = 0; channel < JZ4740_DMAC_CHANNELS; channel++) {
        dmac_try_channel(s, channel, true);
    }
    s->transferring = false;
    dmac_sync_irq(s);
}

void jz4740_dmac_set_request(JZ4740DMACState *s, unsigned request,
                             bool level)
{
    if (!s || request >= JZ4740_DMAC_REQUESTS) {
        return;
    }
    if (level) {
        s->request_levels |= 1u << request;
        jz4740_dmac_kick(s);
    } else {
        s->request_levels &= ~(1u << request);
    }
}

static void dmac_request_input(void *opaque, int request, int level)
{
    jz4740_dmac_set_request(JZ4740_DMAC(opaque), request, level != 0);
}

void jz4740_dmac_set_peripheral_ops(JZ4740DMACState *s,
                                    const JZ4740DMACPeripheralOps *ops,
                                    void *opaque)
{
    if (!s) {
        return;
    }
    memset(&s->ops, 0, sizeof(s->ops));
    if (ops) {
        s->ops = *ops;
    }
    s->ops_opaque = opaque;
}

bool jz4740_dmac_irq_pending(JZ4740DMACState *s)
{
    return s && dmac_irq_pending_internal(s);
}

uint32_t jz4740_dmac_get_reg(JZ4740DMACState *s, hwaddr offset)
{
    if (!s || offset >= DMAC_MMIO_SIZE) {
        return 0;
    }
    return dmac_reg(s, offset & ~3u);
}

void jz4740_dmac_get_diagnostics(JZ4740DMACState *s,
                                 JZ4740DMACDiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    diagnostics->audio_completion_count = s->audio_completion_count;
    diagnostics->audio_rearm_count = s->audio_rearm_count;
    diagnostics->audio_last_rearm_gap_ns = s->audio_last_rearm_gap_ns;
    diagnostics->audio_max_rearm_gap_ns = s->audio_max_rearm_gap_ns;
    diagnostics->audio_total_rearm_gap_ns = s->audio_total_rearm_gap_ns;
    diagnostics->audio_last_gap_underruns = s->audio_last_gap_underruns;
    diagnostics->audio_total_gap_underruns = s->audio_total_gap_underruns;
    diagnostics->audio_last_units = s->audio_last_units;
    diagnostics->audio_completion_fifo = s->audio_completion_fifo;
    diagnostics->audio_rearm_fifo = s->audio_rearm_fifo;
}

static uint64_t dmac_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4740DMACState *s = JZ4740_DMAC(opaque);
    uint32_t value = dmac_reg(s, offset & ~3u) >> ((offset & 3u) * 8u);

    if (size == 1) {
        return value & 0xffu;
    }
    if (size == 2) {
        return value & 0xffffu;
    }
    return value;
}

static void dmac_write(void *opaque, hwaddr offset, uint64_t value,
                       unsigned size)
{
    JZ4740DMACState *s = JZ4740_DMAC(opaque);
    hwaddr aligned = offset & ~3u;
    unsigned shift = (offset & 3u) * 8u;
    uint32_t mask;
    uint32_t old_reg = dmac_reg(s, aligned);
    uint32_t reg;
    unsigned channel;
    hwaddr channel_reg;

    if (size == 1) {
        mask = 0xffu << shift;
    } else if (size == 2) {
        mask = 0xffffu << shift;
    } else {
        mask = 0xffffffffu;
        shift = 0;
    }
    reg = (old_reg & ~mask) | (((uint32_t)value << shift) & mask);
    dmac_set_reg(s, aligned, reg);

    if (aligned == DMAC_DMACR) {
        dmac_set_reg(s, aligned, reg & (DMAC_DMACR_DMAE | DMAC_DMACR_PM_MASK));
        for (channel = 0; channel < JZ4740_DMAC_CHANNELS; channel++) {
            dmac_try_channel(s, channel, true);
        }
        dmac_sync_irq(s);
    } else if (aligned == DMAC_DIRQP) {
        dmac_sync_irq(s);
    } else if (aligned == DMAC_DDB) {
        dmac_set_reg(s, DMAC_DDB, reg & DMAC_CHANNEL_MASK);
        for (channel = 0; channel < JZ4740_DMAC_CHANNELS; channel++) {
            dmac_try_channel(s, channel, false);
        }
    } else if (aligned == DMAC_DDBS) {
        dmac_set_reg(s, DMAC_DDB,
                     dmac_reg(s, DMAC_DDB) | (reg & DMAC_CHANNEL_MASK));
        for (channel = 0; channel < JZ4740_DMAC_CHANNELS; channel++) {
            dmac_try_channel(s, channel, false);
        }
    } else if (dmac_channel_offset(aligned, &channel, &channel_reg)) {
        if (channel_reg == DMAC_DCS) {
            dmac_set_reg(s, aligned,
                         reg & (DMAC_DCS_NDES | DMAC_DCS_CDOA_MASK |
                                DMAC_DCS_STATUS_MASK | DMAC_DCS_CTE));
            dmac_record_audio_rearm(s, channel, old_reg,
                                    dmac_reg(s, aligned));
        } else if (channel_reg == DMAC_DTC) {
            dmac_set_reg(s, aligned, reg & DMAC_DTC_MASK);
        } else if (channel_reg == DMAC_DRT) {
            dmac_set_reg(s, aligned, reg & DMAC_DRT_MASK);
        } else if (channel_reg == DMAC_DDA) {
            dmac_set_reg(s, aligned, reg & ~DMAC_DDA_ALIGN_MASK);
        }
        dmac_trace(s, 1, channel, aligned, dmac_reg(s, aligned));
        dmac_sync_irq(s);
        dmac_try_channel(s, channel, true);
    }
}

static const MemoryRegionOps dmac_ops = {
    .read = dmac_read,
    .write = dmac_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
};

static void dmac_reset_hold(Object *obj, ResetType type)
{
    JZ4740DMACState *s = JZ4740_DMAC(obj);

    memset(s->regs, 0, sizeof(s->regs));
    s->request_levels = 0;
    s->irq_level = false;
    s->transferring = false;
    s->audio_completion_count = 0;
    s->audio_rearm_count = 0;
    s->audio_last_completion_ns = 0;
    s->audio_completion_underruns = 0;
    s->audio_last_rearm_gap_ns = 0;
    s->audio_max_rearm_gap_ns = 0;
    s->audio_total_rearm_gap_ns = 0;
    s->audio_last_gap_underruns = 0;
    s->audio_total_gap_underruns = 0;
    s->audio_last_units = 0;
    s->audio_completion_fifo = 0;
    s->audio_rearm_fifo = 0;
    qemu_set_irq(s->irq, 0);
}

static int dmac_post_load(void *opaque, int version_id)
{
    JZ4740DMACState *s = opaque;

    s->irq_level = false;
    s->transferring = false;
    dmac_sync_irq(s);
    return 0;
}

static const VMStateDescription vmstate_jz4740_dmac = {
    .name = TYPE_JZ4740_DMAC,
    .version_id = 1,
    .minimum_version_id = 1,
    .post_load = dmac_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT32_ARRAY(regs, JZ4740DMACState, DMAC_REGS),
        VMSTATE_UINT32(request_levels, JZ4740DMACState),
        VMSTATE_UINT64(audio_completion_count, JZ4740DMACState),
        VMSTATE_UINT64(audio_rearm_count, JZ4740DMACState),
        VMSTATE_INT64(audio_last_completion_ns, JZ4740DMACState),
        VMSTATE_UINT64(audio_completion_underruns, JZ4740DMACState),
        VMSTATE_UINT64(audio_last_rearm_gap_ns, JZ4740DMACState),
        VMSTATE_UINT64(audio_max_rearm_gap_ns, JZ4740DMACState),
        VMSTATE_UINT64(audio_total_rearm_gap_ns, JZ4740DMACState),
        VMSTATE_UINT64(audio_last_gap_underruns, JZ4740DMACState),
        VMSTATE_UINT64(audio_total_gap_underruns, JZ4740DMACState),
        VMSTATE_UINT32(audio_last_units, JZ4740DMACState),
        VMSTATE_UINT32(audio_completion_fifo, JZ4740DMACState),
        VMSTATE_UINT32(audio_rearm_fifo, JZ4740DMACState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property dmac_properties[] = {
    DEFINE_PROP_UINT64("ram-size", JZ4740DMACState, ram_size,
                       160u * 1024u * 1024u),
};

static void dmac_init(Object *obj)
{
    JZ4740DMACState *s = JZ4740_DMAC(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &dmac_ops, s, TYPE_JZ4740_DMAC,
                          DMAC_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
    qdev_init_gpio_in(DEVICE(obj), dmac_request_input, JZ4740_DMAC_REQUESTS);
}

static void dmac_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->vmsd = &vmstate_jz4740_dmac;
    device_class_set_props(dc, dmac_properties);
    rc->phases.hold = dmac_reset_hold;
}

static const TypeInfo dmac_type_info = {
    .name = TYPE_JZ4740_DMAC,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4740DMACState),
    .instance_init = dmac_init,
    .class_init = dmac_class_init,
};

static void dmac_register_types(void)
{
    type_register_static(&dmac_type_info);
}

type_init(dmac_register_types)
