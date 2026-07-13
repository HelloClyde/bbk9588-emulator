/*
 * BBK 9588 guest diagnostic recorder.
 *
 * This host-only device owns trace sequence/ring state and mirrors records to
 * the reserved guest diagnostic RAM.  It has no guest-visible MMIO window.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "exec/cpu-common.h"
#include "hw/misc/bbk9588_diag.h"
#include "qemu/error-report.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qemu/units.h"
#include "target/mips/cpu.h"

#define BBK9588_DIAG_RAM_SIZE       (160u * MiB)
#define BBK9588_DIAG_KSEG_TO_PHYS(a) ((a) & 0x1fffffffu)
#define BBK9588_DIAG_VA             0x89f00000u

#define EVENT_SCRATCH_VA            (BBK9588_DIAG_VA + 0x0000u)
#define EVENT_SCRATCH_MAGIC         0x45564b42u
#define EVENT_QUEUE_VA              (BBK9588_DIAG_VA + 0x0040u)
#define EVENT_QUEUE_MAGIC           0x514b4242u
#define EVENT_QUEUE_SLOTS           8u
#define EVENT_QUEUE_WORDS           5u
#define EVENT_QUEUE_HEADER_WORDS    4u
#define EVENT_CODE_INPUT            3u

#define TOUCH_TRACE_VA              (BBK9588_DIAG_VA + 0x0100u)
#define TOUCH_TRACE_MAGIC           0x54434b42u
#define TOUCH_TRACE_BYTES           0x154u

#define DMAC_TRACE_VA               (BBK9588_DIAG_VA + 0x0300u)
#define DMAC_TRACE_MAGIC            0x444d4b42u
#define DMAC_TRACE_WORDS            16u

#define PROGRESS_TRACE_VA           (BBK9588_DIAG_VA + 0x0500u)
#define PROGRESS_TRACE_MAGIC        0x50544b42u
#define PROGRESS_TRACE_SLOTS        8u
#define PROGRESS_TRACE_WORDS        12u
#define PROGRESS_TRACE_HEADER_WORDS 4u

#define NAND_TARGET_TRACE_VA        (BBK9588_DIAG_VA + 0x0600u)
#define NAND_TARGET_TRACE_MAGIC     0x4e544b42u
#define NAND_TARGET_TRACE_SLOTS     8u
#define NAND_TARGET_TRACE_WORDS     6u

#define MSC_TRACE_VA                (BBK9588_DIAG_VA + 0x1000u)
#define MSC_TRACE_MAGIC             0x4d534b42u
#define MSC_TRACE_SLOTS             113u
#define MSC_TRACE_WORDS             9u
#define MSC_TRACE_HEADER_WORDS      4u

#define STORAGE_TRACE_VA            (BBK9588_DIAG_VA + 0x2000u)
#define STORAGE_TRACE_MAGIC         0x53544b42u
#define STORAGE_TRACE_SLOTS         4096u
#define STORAGE_TRACE_WORDS         4u
#define STORAGE_TRACE_HEADER_WORDS  4u

struct Bbk9588DiagState {
    DeviceState parent_obj;

    Bbk9588DiagSources sources;
    bool storage_enabled;
    bool graphics_enabled;
    bool touch_enabled;
    bool progress_enabled;
    uint32_t graphics_count;
    uint32_t progress_seq;
    uint32_t nand_ready_count;
    uint32_t storage_seq;
    uint32_t msc_seq;
    uint32_t nand_target_seq;
    uint32_t dmac_seq;
    uint32_t input_read_idx;
    uint32_t input_write_idx;
    uint32_t input_count;
    uint32_t input_words[EVENT_QUEUE_SLOTS][EVENT_QUEUE_WORDS];
};

static bool diag_guest_ram_va_valid(uint32_t va, uint32_t size)
{
    uint32_t phys = BBK9588_DIAG_KSEG_TO_PHYS(va);

    return (va & 0xe0000000u) == 0x80000000u &&
           size <= BBK9588_DIAG_RAM_SIZE &&
           phys <= BBK9588_DIAG_RAM_SIZE - size;
}

static void diag_write_le32(hwaddr addr, uint32_t value)
{
    uint8_t buf[4] = {
        value & 0xffu,
        (value >> 8) & 0xffu,
        (value >> 16) & 0xffu,
        (value >> 24) & 0xffu,
    };

    cpu_physical_memory_write(BBK9588_DIAG_KSEG_TO_PHYS(addr), buf,
                              sizeof(buf));
}

static uint32_t diag_read_le32(hwaddr addr)
{
    uint8_t buf[4];

    cpu_physical_memory_read(BBK9588_DIAG_KSEG_TO_PHYS(addr), buf,
                             sizeof(buf));
    return buf[0] | (buf[1] << 8) | (buf[2] << 16) | (buf[3] << 24);
}

static uint32_t diag_cpu_pc(Bbk9588DiagState *s)
{
    MIPSCPU *cpu;

    if (!s || !s->sources.cpu) {
        return 0;
    }
    cpu = MIPS_CPU(s->sources.cpu);
    return cpu->env.active_tc.PC & 0xffffffffu;
}

static void diag_write_ring_header(uint32_t base, uint32_t magic,
                                   uint32_t seq, uint32_t slot,
                                   uint32_t slots)
{
    diag_write_le32(base + 0x00, magic);
    diag_write_le32(base + 0x04, seq);
    diag_write_le32(base + 0x08, slot);
    diag_write_le32(base + 0x0c, slots);
}

static void diag_mirror_input(Bbk9588DiagState *s)
{
    uint32_t total_size = (EVENT_QUEUE_HEADER_WORDS +
                           EVENT_QUEUE_SLOTS * EVENT_QUEUE_WORDS) * 4u;

    if (!diag_guest_ram_va_valid(EVENT_QUEUE_VA, total_size)) {
        return;
    }

    diag_write_le32(EVENT_QUEUE_VA + 0x00, EVENT_QUEUE_MAGIC);
    diag_write_le32(EVENT_QUEUE_VA + 0x04, s->input_read_idx);
    diag_write_le32(EVENT_QUEUE_VA + 0x08, s->input_write_idx);
    diag_write_le32(EVENT_QUEUE_VA + 0x0c, s->input_count);
    for (uint32_t slot = 0; slot < EVENT_QUEUE_SLOTS; slot++) {
        uint32_t base = EVENT_QUEUE_VA + EVENT_QUEUE_HEADER_WORDS * 4u +
                        slot * EVENT_QUEUE_WORDS * 4u;

        for (uint32_t word = 0; word < EVENT_QUEUE_WORDS; word++) {
            diag_write_le32(base + word * 4u, s->input_words[slot][word]);
        }
    }
}

void bbk9588_diag_connect_sources(Bbk9588DiagState *s,
                                  const Bbk9588DiagSources *sources)
{
    if (!s || !sources) {
        return;
    }
    s->sources = *sources;
    if (s->sources.lcd) {
        jz4740_lcd_set_trace_enabled(s->sources.lcd, s->graphics_enabled);
    }
}

void bbk9588_diag_set_storage_enabled(Bbk9588DiagState *s, bool enabled)
{
    if (s) {
        s->storage_enabled = enabled;
    }
}

bool bbk9588_diag_graphics_enabled(Bbk9588DiagState *s)
{
    return s && s->graphics_enabled;
}

void bbk9588_diag_set_graphics_enabled(Bbk9588DiagState *s, bool enabled)
{
    if (!s) {
        return;
    }
    s->graphics_enabled = enabled;
    s->graphics_count = 0;
    if (s->sources.lcd) {
        jz4740_lcd_set_trace_enabled(s->sources.lcd, enabled);
    }
}

bool bbk9588_diag_touch_enabled(Bbk9588DiagState *s)
{
    return s && s->touch_enabled;
}

void bbk9588_diag_set_touch_enabled(Bbk9588DiagState *s, bool enabled)
{
    if (s) {
        s->touch_enabled = enabled;
    }
}

bool bbk9588_diag_progress_enabled(Bbk9588DiagState *s)
{
    return s && s->progress_enabled;
}

void bbk9588_diag_set_progress_enabled(Bbk9588DiagState *s, bool enabled)
{
    if (s) {
        s->progress_enabled = enabled;
    }
}

void bbk9588_diag_reset_input(Bbk9588DiagState *s)
{
    if (!s) {
        return;
    }
    s->input_read_idx = 0;
    s->input_write_idx = 0;
    s->input_count = 0;
    memset(s->input_words, 0, sizeof(s->input_words));
    diag_mirror_input(s);
    if (diag_guest_ram_va_valid(EVENT_SCRATCH_VA, 0x1c)) {
        diag_write_le32(EVENT_SCRATCH_VA + 0x18, 0);
    }
}

void bbk9588_diag_queue_input(Bbk9588DiagState *s, uint32_t kind,
                              uint32_t arg0, uint32_t arg1, uint32_t arg2)
{
    uint32_t read_idx;
    uint32_t write_idx;
    uint32_t count;

    if (!s) {
        return;
    }

    read_idx = s->input_read_idx;
    write_idx = s->input_write_idx;
    count = s->input_count;
    if (read_idx >= EVENT_QUEUE_SLOTS || write_idx >= EVENT_QUEUE_SLOTS ||
        count > EVENT_QUEUE_SLOTS) {
        read_idx = 0;
        write_idx = 0;
        count = 0;
    }
    if (count == EVENT_QUEUE_SLOTS) {
        read_idx = (read_idx + 1) % EVENT_QUEUE_SLOTS;
        count--;
    }

    s->input_words[write_idx][0] = EVENT_CODE_INPUT;
    s->input_words[write_idx][1] = kind;
    s->input_words[write_idx][2] = arg0;
    s->input_words[write_idx][3] = arg1;
    s->input_words[write_idx][4] = arg2;
    s->input_read_idx = read_idx;
    s->input_write_idx = (write_idx + 1) % EVENT_QUEUE_SLOTS;
    s->input_count = count + 1;
    diag_mirror_input(s);
    diag_write_le32(EVENT_SCRATCH_VA + 0x18, EVENT_SCRATCH_MAGIC);
}

void bbk9588_diag_note_nand_ready(Bbk9588DiagState *s)
{
    if (s) {
        s->nand_ready_count++;
    }
}

void bbk9588_diag_touch_record(Bbk9588DiagState *s, uint32_t reason,
                               const Bbk9588DiagBoardSnapshot *board)
{
    JZ4740GPIODiagnostics gpio;
    JZ4740INTCDiagnostics intc;
    JZ4740EMCDiagnostics emc;
    Bbk9588NandDiagnostics nand;
    JZ4740MSCDiagnostics msc;
    JZ4740SADCDiagnostics sadc;
    JZ4740TCUDiagnostics tcu;
    CPUState *cs;
    MIPSCPU *cpu;
    uint32_t pc;

    if (!s || !s->touch_enabled || !board ||
        !diag_guest_ram_va_valid(TOUCH_TRACE_VA, TOUCH_TRACE_BYTES) ||
        !s->sources.cpu || !s->sources.cpm || !s->sources.dmac ||
        !s->sources.emc || !s->sources.gpio || !s->sources.intc ||
        !s->sources.msc || !s->sources.nand || !s->sources.sadc ||
        !s->sources.tcu) {
        return;
    }

    cs = s->sources.cpu;
    cpu = MIPS_CPU(cs);
    pc = cpu->env.active_tc.PC;
    jz4740_gpio_get_diagnostics(s->sources.gpio, &gpio);
    jz4740_intc_get_diagnostics(s->sources.intc, &intc);
    jz4740_emc_get_diagnostics(s->sources.emc, &emc);
    bbk9588_nand_get_diagnostics(s->sources.nand, &nand);
    jz4740_msc_get_diagnostics(s->sources.msc, &msc);
    jz4740_sadc_get_diagnostics(s->sources.sadc, &sadc);
    jz4740_tcu_get_diagnostics(s->sources.tcu, &tcu);

    diag_write_le32(TOUCH_TRACE_VA + 0x00, TOUCH_TRACE_MAGIC);
    diag_write_le32(TOUCH_TRACE_VA + 0x04, 0);
    diag_write_le32(TOUCH_TRACE_VA + 0x08, 0);
    diag_write_le32(TOUCH_TRACE_VA + 0x0c, 0);
    diag_write_le32(TOUCH_TRACE_VA + 0x10, sadc.touch_down ? 1u : 0u);
    diag_write_le32(TOUCH_TRACE_VA + 0x14, sadc.touch_raw_x);
    diag_write_le32(TOUCH_TRACE_VA + 0x18, sadc.touch_raw_y);
    diag_write_le32(TOUCH_TRACE_VA + 0x1c, sadc.status);
    diag_write_le32(TOUCH_TRACE_VA + 0x20, sadc.next_axis);
    diag_write_le32(TOUCH_TRACE_VA + 0x24, intc.pending);
    diag_write_le32(TOUCH_TRACE_VA + 0x28, pc);
    diag_write_le32(TOUCH_TRACE_VA + 0x2c, reason);
    diag_write_le32(TOUCH_TRACE_VA + 0x30,
                    sadc.conversion_events_remaining);
    diag_write_le32(TOUCH_TRACE_VA + 0x34, sadc.control);
    diag_write_le32(TOUCH_TRACE_VA + 0x38, sadc.last_read_offset);
    diag_write_le32(TOUCH_TRACE_VA + 0x3c, sadc.last_read_value);
    diag_write_le32(TOUCH_TRACE_VA + 0x40, sadc.last_write_offset);
    diag_write_le32(TOUCH_TRACE_VA + 0x44, sadc.last_write_value);
    diag_write_le32(TOUCH_TRACE_VA + 0x48, gpio.last_read_offset);
    diag_write_le32(TOUCH_TRACE_VA + 0x4c, gpio.last_read_value);
    diag_write_le32(TOUCH_TRACE_VA + 0x50, gpio.last_flag_offset);
    diag_write_le32(TOUCH_TRACE_VA + 0x54, gpio.last_flag_value);
    diag_write_le32(TOUCH_TRACE_VA + 0x58, intc.mask);
    diag_write_le32(TOUCH_TRACE_VA + 0x5c, tcu.enabled_mask);
    diag_write_le32(TOUCH_TRACE_VA + 0x60, tcu.pending_mask);
    diag_write_le32(TOUCH_TRACE_VA + 0x64, tcu.compare[0]);
    diag_write_le32(TOUCH_TRACE_VA + 0x68, tcu.compare[1]);
    diag_write_le32(TOUCH_TRACE_VA + 0x6c, tcu.period_ms[0]);
    diag_write_le32(TOUCH_TRACE_VA + 0x70, tcu.period_ms[1]);
    diag_write_le32(TOUCH_TRACE_VA + 0x74,
                    (uint32_t)(tcu.deadline_ns[0] / SCALE_MS));
    diag_write_le32(TOUCH_TRACE_VA + 0x78,
                    (uint32_t)(tcu.deadline_ns[1] / SCALE_MS));
    diag_write_le32(TOUCH_TRACE_VA + 0x7c, intc.unmasked_pending);
    diag_write_le32(TOUCH_TRACE_VA + 0x80, intc.output_level);
    diag_write_le32(TOUCH_TRACE_VA + 0x84, intc.update_count);
    diag_write_le32(TOUCH_TRACE_VA + 0x88, board->intc_last_cp0_status);
    diag_write_le32(TOUCH_TRACE_VA + 0x8c, board->intc_last_cp0_cause);
    diag_write_le32(TOUCH_TRACE_VA + 0x90,
                    cpu->env.bbk9588_irq_ip2_level ? 1u : 0u);
    diag_write_le32(TOUCH_TRACE_VA + 0x94,
                    (uint32_t)cs->interrupt_request);
    diag_write_le32(TOUCH_TRACE_VA + 0x98, intc.last_read_offset);
    diag_write_le32(TOUCH_TRACE_VA + 0x9c, intc.last_read_value);
    diag_write_le32(TOUCH_TRACE_VA + 0xa0, intc.last_write_offset);
    diag_write_le32(TOUCH_TRACE_VA + 0xa4, intc.last_write_value);
    diag_write_le32(TOUCH_TRACE_VA + 0xa8, 0);
    diag_write_le32(TOUCH_TRACE_VA + 0xac, 0);
    diag_write_le32(TOUCH_TRACE_VA + 0xb0, tcu.last_read_offset);
    diag_write_le32(TOUCH_TRACE_VA + 0xb4, tcu.last_read_value);
    diag_write_le32(TOUCH_TRACE_VA + 0xb8, tcu.last_write_offset);
    diag_write_le32(TOUCH_TRACE_VA + 0xbc, tcu.last_write_value);
    diag_write_le32(TOUCH_TRACE_VA + 0xc0, tcu.irq_raise_count);
    diag_write_le32(TOUCH_TRACE_VA + 0xc4, 0);
    diag_write_le32(TOUCH_TRACE_VA + 0xc8, msc.read_pending ? 1u : 0u);
    diag_write_le32(TOUCH_TRACE_VA + 0xcc, msc.write_pending ? 1u : 0u);
    diag_write_le32(TOUCH_TRACE_VA + 0xd0, msc.data_ready ? 1u : 0u);
    diag_write_le32(TOUCH_TRACE_VA + 0xd4, msc.read_lba);
    diag_write_le32(TOUCH_TRACE_VA + 0xd8, msc.write_lba);
    diag_write_le32(TOUCH_TRACE_VA + 0xdc, msc.dma_complete_count);
    diag_write_le32(TOUCH_TRACE_VA + 0xe0, msc.last_command);
    diag_write_le32(TOUCH_TRACE_VA + 0xe4, msc.last_argument);
    diag_write_le32(TOUCH_TRACE_VA + 0xe8, msc.last_dma_phys);
    diag_write_le32(TOUCH_TRACE_VA + 0xec, msc.last_dma_words);
    diag_write_le32(TOUCH_TRACE_VA + 0xf0, s->nand_ready_count);
    diag_write_le32(TOUCH_TRACE_VA + 0xf4, nand.page_read_count);
    diag_write_le32(TOUCH_TRACE_VA + 0xf8, nand.program_count);
    diag_write_le32(TOUCH_TRACE_VA + 0xfc, nand.erase_count);
    diag_write_le32(TOUCH_TRACE_VA + 0x100, nand.command);
    diag_write_le32(TOUCH_TRACE_VA + 0x104, nand.last_page);
    diag_write_le32(TOUCH_TRACE_VA + 0x108, nand.last_column);
    diag_write_le32(TOUCH_TRACE_VA + 0x10c, nand.last_block);
    diag_write_le32(TOUCH_TRACE_VA + 0x110, nand.busy_reads);
    diag_write_le32(TOUCH_TRACE_VA + 0x114, emc.nfints);
    diag_write_le32(TOUCH_TRACE_VA + 0x118, nand.address_count);
    diag_write_le32(TOUCH_TRACE_VA + 0x11c,
                    gpio.flag[JZ4740_GPIO_PORT_C]);
    diag_write_le32(TOUCH_TRACE_VA + 0x120,
                    jz4740_cpm_clkgr_wake_mask(s->sources.cpm));
    diag_write_le32(TOUCH_TRACE_VA + 0x124,
                    jz4740_cpm_scr_wake_mask(s->sources.cpm));
    diag_write_le32(TOUCH_TRACE_VA + 0x128,
                    board->extgpio_wake_enable);
    diag_write_le32(TOUCH_TRACE_VA + 0x12c,
                    board->sysctrl_wake_pending ? 1u : 0u);
    diag_write_le32(TOUCH_TRACE_VA + 0x130, board->sysctrl_wake_count);
    diag_write_le32(TOUCH_TRACE_VA + 0x134, tcu.irq_mask);
    diag_write_le32(TOUCH_TRACE_VA + 0x138, tcu.compare[4]);
    diag_write_le32(TOUCH_TRACE_VA + 0x13c, tcu.period_ms[4]);
    diag_write_le32(TOUCH_TRACE_VA + 0x140,
                    (uint32_t)(tcu.deadline_ns[4] / SCALE_MS));
    diag_write_le32(TOUCH_TRACE_VA + 0x144,
                    jz4740_dmac_get_reg(s->sources.dmac, 0x304));
    diag_write_le32(TOUCH_TRACE_VA + 0x148,
                    jz4740_dmac_get_reg(s->sources.dmac, 0x70));
    diag_write_le32(TOUCH_TRACE_VA + 0x14c,
                    jz4740_dmac_get_reg(s->sources.dmac, 0x74));
    diag_write_le32(TOUCH_TRACE_VA + 0x150,
                    jz4740_dmac_get_reg(s->sources.dmac, 0x68));
}

void bbk9588_diag_progress_record(Bbk9588DiagState *s, uint32_t reason)
{
    JZ4740TCUDiagnostics tcu;
    MIPSCPU *cpu;
    uint32_t total_size = (PROGRESS_TRACE_HEADER_WORDS +
                           PROGRESS_TRACE_SLOTS * PROGRESS_TRACE_WORDS) * 4u;
    uint32_t seq;
    uint32_t slot;
    uint32_t entry;

    if (!s || !s->progress_enabled || !s->sources.cpu ||
        !s->sources.intc || !s->sources.tcu ||
        !diag_guest_ram_va_valid(PROGRESS_TRACE_VA, total_size)) {
        return;
    }

    cpu = MIPS_CPU(s->sources.cpu);
    jz4740_tcu_get_diagnostics(s->sources.tcu, &tcu);
    seq = ++s->progress_seq;
    slot = (seq - 1) % PROGRESS_TRACE_SLOTS;
    entry = PROGRESS_TRACE_VA + (PROGRESS_TRACE_HEADER_WORDS +
            slot * PROGRESS_TRACE_WORDS) * 4u;

    diag_write_ring_header(PROGRESS_TRACE_VA, PROGRESS_TRACE_MAGIC, seq,
                           slot, PROGRESS_TRACE_SLOTS);
    diag_write_le32(entry + 0x00, seq);
    diag_write_le32(entry + 0x04, reason);
    diag_write_le32(entry + 0x08, cpu->env.active_tc.PC & 0xffffffffu);
    diag_write_le32(entry + 0x0c, jz4740_intc_pending(s->sources.intc));
    diag_write_le32(entry + 0x10, jz4740_intc_mask(s->sources.intc));
    diag_write_le32(entry + 0x14, tcu.pending_mask);
    diag_write_le32(entry + 0x18, diag_read_le32(0x804bf440u));
    diag_write_le32(entry + 0x1c, diag_read_le32(0x804bf444u));
    diag_write_le32(entry + 0x20, diag_read_le32(0x80473f08u));
    diag_write_le32(entry + 0x24, diag_read_le32(0x80473f38u));
    diag_write_le32(entry + 0x28, cpu->env.CP0_Cause);
    diag_write_le32(entry + 0x2c, cpu->env.CP0_Status);
}

void bbk9588_diag_panel_write(void *opaque, hwaddr offset, uint64_t value,
                              unsigned size)
{
    Bbk9588DiagState *s = opaque;
    JZ4740LCDDiagnostics lcd;

    if (!s || !s->graphics_enabled || !s->sources.lcd ||
        !s->sources.panel || s->graphics_count++ >= 4096) {
        return;
    }
    jz4740_lcd_get_diagnostics(s->sources.lcd, &lcd);
    error_report(
        "bbk9588-panel[%u] off=0x%04" HWADDR_PRIx
        " size=%u value=0x%08" PRIx64
        " r0000=0x%08x r0004=0x%08x r0008=0x%08x r000c=0x%08x"
        " r0010=0x%08x r0014=0x%08x r0018=0x%08x r001c=0x%08x"
        " r0020=0x%08x r0024=0x%08x r0028=0x%08x r002c=0x%08x"
        " r0030=0x%08x r0034=0x%08x r0038=0x%08x r003c=0x%08x"
        " r0040=0x%08x r0044=0x%08x r0048=0x%08x r004c=0x%08x"
        " lcd_desc=0x%08x lcd_fb=0x%08x lcd_source=%u",
        s->graphics_count - 1, offset, size, value,
        bbk9588_panel_get_reg(s->sources.panel, 0x0000),
        bbk9588_panel_get_reg(s->sources.panel, 0x0004),
        bbk9588_panel_get_reg(s->sources.panel, 0x0008),
        bbk9588_panel_get_reg(s->sources.panel, 0x000c),
        bbk9588_panel_get_reg(s->sources.panel, 0x0010),
        bbk9588_panel_get_reg(s->sources.panel, 0x0014),
        bbk9588_panel_get_reg(s->sources.panel, 0x0018),
        bbk9588_panel_get_reg(s->sources.panel, 0x001c),
        bbk9588_panel_get_reg(s->sources.panel, 0x0020),
        bbk9588_panel_get_reg(s->sources.panel, 0x0024),
        bbk9588_panel_get_reg(s->sources.panel, 0x0028),
        bbk9588_panel_get_reg(s->sources.panel, 0x002c),
        bbk9588_panel_get_reg(s->sources.panel, 0x0030),
        bbk9588_panel_get_reg(s->sources.panel, 0x0034),
        bbk9588_panel_get_reg(s->sources.panel, 0x0038),
        bbk9588_panel_get_reg(s->sources.panel, 0x003c),
        bbk9588_panel_get_reg(s->sources.panel, 0x0040),
        bbk9588_panel_get_reg(s->sources.panel, 0x0044),
        bbk9588_panel_get_reg(s->sources.panel, 0x0048),
        bbk9588_panel_get_reg(s->sources.panel, 0x004c),
        lcd.descriptor_address, lcd.framebuffer_address,
        lcd.frame_source_kind);
}

void bbk9588_diag_storage_record(Bbk9588DiagState *s, uint32_t logical,
                                 uint32_t absolute, uint32_t first_word)
{
    uint32_t seq;
    uint32_t slot;
    uint32_t base;

    if (!s || !s->storage_enabled) {
        return;
    }
    seq = ++s->storage_seq;
    slot = (seq - 1) % STORAGE_TRACE_SLOTS;
    base = STORAGE_TRACE_VA + STORAGE_TRACE_HEADER_WORDS * 4u +
           slot * STORAGE_TRACE_WORDS * 4u;
    diag_write_ring_header(STORAGE_TRACE_VA, STORAGE_TRACE_MAGIC, seq, slot,
                           STORAGE_TRACE_SLOTS);
    diag_write_le32(base + 0x00, seq);
    diag_write_le32(base + 0x04, logical);
    diag_write_le32(base + 0x08, absolute);
    diag_write_le32(base + 0x0c, first_word);
}

void bbk9588_diag_msc_record(Bbk9588DiagState *s, uint32_t event,
                             uint32_t lba, uint32_t dma_phys,
                             uint32_t bytes, uint32_t command,
                             uint32_t argument, uint32_t first_word)
{
    uint32_t seq;
    uint32_t slot;
    uint32_t base;

    if (!s || !s->storage_enabled) {
        return;
    }
    seq = ++s->msc_seq;
    slot = (seq - 1) % MSC_TRACE_SLOTS;
    base = MSC_TRACE_VA + MSC_TRACE_HEADER_WORDS * 4u +
           slot * MSC_TRACE_WORDS * 4u;
    diag_write_ring_header(MSC_TRACE_VA, MSC_TRACE_MAGIC, seq, slot,
                           MSC_TRACE_SLOTS);
    diag_write_le32(base + 0x00, seq);
    diag_write_le32(base + 0x04, event);
    diag_write_le32(base + 0x08, lba);
    diag_write_le32(base + 0x0c, dma_phys);
    diag_write_le32(base + 0x10, bytes);
    diag_write_le32(base + 0x14, command);
    diag_write_le32(base + 0x18, argument);
    diag_write_le32(base + 0x1c, first_word);
    diag_write_le32(base + 0x20, diag_cpu_pc(s));
}

void bbk9588_diag_nand_target_record(Bbk9588DiagState *s, uint32_t event,
                                     uint32_t a, uint32_t b, uint32_t c,
                                     uint32_t pc)
{
    uint32_t seq;
    uint32_t slot;
    uint32_t base;

    if (!s || !s->storage_enabled) {
        return;
    }
    seq = ++s->nand_target_seq;
    slot = (seq - 1) % NAND_TARGET_TRACE_SLOTS;
    base = NAND_TARGET_TRACE_VA + STORAGE_TRACE_HEADER_WORDS * 4u +
           slot * NAND_TARGET_TRACE_WORDS * 4u;
    diag_write_ring_header(NAND_TARGET_TRACE_VA, NAND_TARGET_TRACE_MAGIC,
                           seq, slot, NAND_TARGET_TRACE_SLOTS);
    diag_write_le32(base + 0x00, seq);
    diag_write_le32(base + 0x04, event);
    diag_write_le32(base + 0x08, a);
    diag_write_le32(base + 0x0c, b);
    diag_write_le32(base + 0x10, c);
    diag_write_le32(base + 0x14, pc);
}

static void diag_dmac_record(Bbk9588DiagState *s, uint32_t event,
                             unsigned channel, hwaddr offset,
                             uint32_t value, uint32_t pc,
                             uint32_t intc_pending, uint32_t intc_mask,
                             const uint32_t channel_regs[7])
{
    if (!s || !s->storage_enabled ||
        !diag_guest_ram_va_valid(DMAC_TRACE_VA, DMAC_TRACE_WORDS * 4u)) {
        return;
    }

    s->dmac_seq++;
    diag_write_le32(DMAC_TRACE_VA + 0x00, DMAC_TRACE_MAGIC);
    diag_write_le32(DMAC_TRACE_VA + 0x04, s->dmac_seq);
    diag_write_le32(DMAC_TRACE_VA + 0x08, event);
    diag_write_le32(DMAC_TRACE_VA + 0x0c, channel);
    diag_write_le32(DMAC_TRACE_VA + 0x10, offset);
    diag_write_le32(DMAC_TRACE_VA + 0x14, value);
    diag_write_le32(DMAC_TRACE_VA + 0x18, pc);
    diag_write_le32(DMAC_TRACE_VA + 0x1c, intc_pending);
    diag_write_le32(DMAC_TRACE_VA + 0x20, intc_mask);
    for (uint32_t i = 0; i < 7; i++) {
        diag_write_le32(DMAC_TRACE_VA + 0x24 + i * 4u, channel_regs[i]);
    }
}

void bbk9588_diag_dmac_sample(Bbk9588DiagState *s, uint32_t event,
                              unsigned channel, hwaddr offset,
                              uint32_t value)
{
    uint32_t channel_regs[7];

    if (!s || !s->storage_enabled || !s->sources.dmac ||
        !s->sources.intc) {
        return;
    }
    channel_regs[0] = jz4740_dmac_get_reg(s->sources.dmac, 0x304);
    channel_regs[1] = jz4740_dmac_get_reg(s->sources.dmac, 0x50);
    channel_regs[2] = jz4740_dmac_get_reg(s->sources.dmac, 0x54);
    channel_regs[3] = jz4740_dmac_get_reg(s->sources.dmac, 0x48);
    channel_regs[4] = jz4740_dmac_get_reg(s->sources.dmac, 0x70);
    channel_regs[5] = jz4740_dmac_get_reg(s->sources.dmac, 0x74);
    channel_regs[6] = jz4740_dmac_get_reg(s->sources.dmac, 0x68);
    diag_dmac_record(s, event, channel, offset, value, diag_cpu_pc(s),
                     jz4740_intc_pending(s->sources.intc),
                     jz4740_intc_mask(s->sources.intc), channel_regs);
}

static const TypeInfo diag_type_info = {
    .name = TYPE_BBK9588_DIAG,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(Bbk9588DiagState),
};

static void diag_register_types(void)
{
    type_register_static(&diag_type_info);
}

type_init(diag_register_types)
