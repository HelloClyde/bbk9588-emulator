/*
 * BBK 9588 DMA peripheral bridge.
 *
 * This host-only device routes JZ4740 DMAC requests to the board's AIC and
 * MSC devices.  It has no guest-visible MMIO window.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "exec/cpu-common.h"
#include "hw/audio/jz4740_aic.h"
#include "hw/dma/bbk9588_dma_bridge.h"
#include "hw/dma/jz4740_dmac.h"
#include "hw/misc/bbk9588_diag.h"
#include "hw/sd/jz4740_msc.h"
#include "qemu/module.h"

#define BBK9588_KSEG_TO_PHYS(addr) ((addr) & 0x1fffffffu)
#define BBK9588_AIC_DATA_PHYS      0x10020034u

struct Bbk9588DMABridgeState {
    DeviceState parent_obj;

    JZ4740DMACState *dmac;
    JZ4740MSCState *msc;
    JZ4740AICState *aic;
    Bbk9588DiagState *diag;
};

static uint32_t bridge_ldl_le(const uint8_t *p)
{
    return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24);
}

static void bridge_msc_kick_dmac(void *opaque)
{
    Bbk9588DMABridgeState *s = opaque;

    if (s->dmac) {
        jz4740_dmac_kick(s->dmac);
    }
}

static void bridge_msc_command(void *opaque, uint32_t command,
                               uint32_t argument)
{
    Bbk9588DMABridgeState *s = opaque;

    bbk9588_diag_msc_record(s->diag, BBK9588_DIAG_MSC_COMMAND,
                            argument >> 9, 0, 0, command, argument, 0);
}

static bool bridge_msc_dma_transfer(Bbk9588DMABridgeState *s,
                                    unsigned channel, uint32_t source,
                                    uint32_t target, uint32_t words)
{
    JZ4740MSCDMATransfer transfer;
    uint8_t *buf;
    bool ok = true;

    if (!s->msc ||
        !jz4740_msc_begin_dma(s->msc, channel, source, target, words,
                              &transfer)) {
        return false;
    }
    bbk9588_diag_storage_record(
        s->diag, BBK9588_DIAG_STORAGE_DMAC_TRANSFER |
        (transfer.read ? 1u : 2u), transfer.words, transfer.dma_phys);
    if (transfer.bytes == 0) {
        jz4740_msc_finish_dma(s->msc, false);
        return true;
    }
    if (transfer.sectors == 0 || transfer.sectors > 128) {
        return false;
    }

    buf = g_malloc0(transfer.sectors * 512u);
    if (transfer.read) {
        /* No removable MSC medium is attached by default. */
        bbk9588_diag_msc_record(
            s->diag, BBK9588_DIAG_MSC_READ, transfer.lba,
            transfer.dma_phys, transfer.bytes, transfer.command,
            transfer.argument, ok ? bridge_ldl_le(buf) : 0xffffffffu);
        if (ok) {
            cpu_physical_memory_write(
                transfer.dma_phys, buf,
                MIN(transfer.bytes, transfer.sectors * 512u));
        }
    } else {
        cpu_physical_memory_read(
            transfer.dma_phys, buf,
            MIN(transfer.bytes, transfer.sectors * 512u));
        bbk9588_diag_msc_record(
            s->diag, BBK9588_DIAG_MSC_WRITE, transfer.lba,
            transfer.dma_phys, transfer.bytes, transfer.command,
            transfer.argument, bridge_ldl_le(buf));
    }
    jz4740_msc_finish_dma(s->msc, ok);
    g_free(buf);
    return true;
}

static void bridge_dmac_trace(void *opaque, uint32_t event,
                              unsigned channel, hwaddr offset,
                              uint32_t value)
{
    Bbk9588DMABridgeState *s = opaque;

    bbk9588_diag_dmac_sample(s->diag, event, channel, offset, value);
}

static bool bridge_dmac_bulk_transfer(void *opaque, unsigned channel,
                                      uint32_t request, uint32_t source,
                                      uint32_t target, uint32_t count,
                                      uint32_t command)
{
    Bbk9588DMABridgeState *s = opaque;

    return bridge_msc_dma_transfer(s, channel, source, target, count);
}

static bool bridge_dmac_address_valid(void *opaque, unsigned request,
                                      uint32_t address)
{
    return (request == JZ4740_DMAC_REQUEST_AIC_TX ||
            request == JZ4740_DMAC_REQUEST_AIC_RX) &&
           BBK9588_KSEG_TO_PHYS(address) == BBK9588_AIC_DATA_PHYS;
}

static size_t bridge_dmac_write(void *opaque, unsigned request,
                                const uint8_t *buf, size_t bytes,
                                unsigned width)
{
    Bbk9588DMABridgeState *s = opaque;

    if (!s->aic || request != JZ4740_DMAC_REQUEST_AIC_TX) {
        return 0;
    }
    return jz4740_aic_dma_write_tx(s->aic, buf, bytes, width);
}

static size_t bridge_dmac_read(void *opaque, unsigned request,
                               uint8_t *buf, size_t bytes, unsigned width)
{
    Bbk9588DMABridgeState *s = opaque;

    if (!s->aic || request != JZ4740_DMAC_REQUEST_AIC_RX) {
        return 0;
    }
    return jz4740_aic_dma_read_rx(s->aic, buf, bytes, width);
}

static void bridge_dmac_complete(void *opaque, unsigned request)
{
    Bbk9588DMABridgeState *s = opaque;

    if (s->aic && request == JZ4740_DMAC_REQUEST_AIC_TX) {
        jz4740_aic_notify_tx_dma_boundary(s->aic);
    }
}

static void bridge_dmac_get_diagnostics(
    void *opaque, unsigned request,
    JZ4740DMACEndpointDiagnostics *diagnostics)
{
    Bbk9588DMABridgeState *s = opaque;
    JZ4740AICDiagnostics aic;

    if (!s->aic) {
        return;
    }
    jz4740_aic_get_diagnostics(s->aic, &aic);
    diagnostics->underruns = aic.underruns;
    diagnostics->fifo_level = request == JZ4740_DMAC_REQUEST_AIC_RX ?
                              aic.rx_fifo_level : aic.tx_fifo_level;
}

static const JZ4740DMACPeripheralOps bridge_dmac_peripheral_ops = {
    .bulk_transfer = bridge_dmac_bulk_transfer,
    .address_valid = bridge_dmac_address_valid,
    .write = bridge_dmac_write,
    .read = bridge_dmac_read,
    .complete = bridge_dmac_complete,
    .get_diagnostics = bridge_dmac_get_diagnostics,
    .trace = bridge_dmac_trace,
};

void bbk9588_dma_bridge_connect(Bbk9588DMABridgeState *s,
                                JZ4740DMACState *dmac,
                                JZ4740MSCState *msc,
                                JZ4740AICState *aic,
                                Bbk9588DiagState *diag)
{
    if (!s) {
        return;
    }
    s->dmac = dmac;
    s->msc = msc;
    s->aic = aic;
    s->diag = diag;
    if (s->dmac) {
        jz4740_dmac_set_peripheral_ops(s->dmac,
                                       &bridge_dmac_peripheral_ops, s);
    }
    if (s->msc) {
        jz4740_msc_set_kick_callback(s->msc, bridge_msc_kick_dmac, s);
        jz4740_msc_set_command_callback(s->msc, bridge_msc_command, s);
    }
}

static void bridge_finalize(Object *obj)
{
    Bbk9588DMABridgeState *s = BBK9588_DMA_BRIDGE(obj);

    if (s->dmac) {
        jz4740_dmac_set_peripheral_ops(s->dmac, NULL, NULL);
    }
    if (s->msc) {
        jz4740_msc_set_kick_callback(s->msc, NULL, NULL);
        jz4740_msc_set_command_callback(s->msc, NULL, NULL);
    }
}

static const TypeInfo bridge_type_info = {
    .name = TYPE_BBK9588_DMA_BRIDGE,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(Bbk9588DMABridgeState),
    .instance_finalize = bridge_finalize,
};

static void bridge_register_types(void)
{
    type_register_static(&bridge_type_info);
}

type_init(bridge_register_types)
