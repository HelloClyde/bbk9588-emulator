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
#include "qemu/module.h"
#include "qemu/units.h"

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

#define DMAC_TRACE_VA               (BBK9588_DIAG_VA + 0x0300u)
#define DMAC_TRACE_MAGIC            0x444d4b42u
#define DMAC_TRACE_WORDS            16u

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

    bool storage_enabled;
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

void bbk9588_diag_set_storage_enabled(Bbk9588DiagState *s, bool enabled)
{
    if (s) {
        s->storage_enabled = enabled;
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
                             uint32_t argument, uint32_t first_word,
                             uint32_t pc)
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
    diag_write_le32(base + 0x20, pc);
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

void bbk9588_diag_dmac_record(Bbk9588DiagState *s, uint32_t event,
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
