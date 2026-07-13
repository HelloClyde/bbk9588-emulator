/*
 * BBK 9588 raw NAND device.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_BLOCK_BBK9588_NAND_H
#define HW_BLOCK_BBK9588_NAND_H

#include "hw/core/sysbus.h"

#define TYPE_BBK9588_NAND "bbk9588-nand"
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588NandState, BBK9588_NAND)

#define BBK9588_NAND_PAGE_SIZE       2048u
#define BBK9588_NAND_SPARE_SIZE      64u
#define BBK9588_NAND_RAW_STRIDE      \
    (BBK9588_NAND_PAGE_SIZE + BBK9588_NAND_SPARE_SIZE)
#define BBK9588_NAND_PAGES_PER_BLOCK 64u
#define BBK9588_NAND_BLOCKS          4096u
#define BBK9588_NAND_DEFAULT_ID_CODE 0xdcu

typedef enum Bbk9588NandEventType {
    BBK9588_NAND_EVENT_COMMAND,
    BBK9588_NAND_EVENT_READ,
    BBK9588_NAND_EVENT_READ_TRACE,
    BBK9588_NAND_EVENT_READ_DETAIL,
    BBK9588_NAND_EVENT_PROGRAM,
    BBK9588_NAND_EVENT_ERASE,
    BBK9588_NAND_EVENT_DEBUG_BYTE,
} Bbk9588NandEventType;

typedef struct Bbk9588NandEvent {
    Bbk9588NandEventType type;
    bool failed;
    uint32_t page;
    uint32_t column;
    uint32_t count;
    uint32_t value;
    uint32_t flags;
    uint32_t index;
} Bbk9588NandEvent;

typedef struct Bbk9588NandDiagnostics {
    uint8_t command;
    uint8_t status;
    uint32_t address_count;
    uint32_t busy_reads;
    uint32_t page_stride;
    uint64_t size;
    uint32_t page_read_count;
    uint32_t program_count;
    uint32_t erase_count;
    uint32_t program_fail_count;
    uint32_t erase_fail_count;
    uint32_t last_page;
    uint32_t last_column;
    uint32_t last_block;
} Bbk9588NandDiagnostics;

typedef void (*Bbk9588NandEventCallback)(void *opaque,
                                         const Bbk9588NandEvent *event);
typedef void (*Bbk9588NandDataCallback)(void *opaque, uint32_t value,
                                        unsigned size, bool write);

void bbk9588_nand_set_event_callback(Bbk9588NandState *s,
                                     Bbk9588NandEventCallback callback,
                                     void *opaque);
void bbk9588_nand_set_data_callback(Bbk9588NandState *s,
                                    Bbk9588NandDataCallback callback,
                                    void *opaque);
void bbk9588_nand_set_trace_enabled(Bbk9588NandState *s, bool enabled);
void bbk9588_nand_get_diagnostics(Bbk9588NandState *s,
                                  Bbk9588NandDiagnostics *diagnostics);

const uint8_t *bbk9588_nand_raw_data(Bbk9588NandState *s);
uint64_t bbk9588_nand_size(Bbk9588NandState *s);
uint32_t bbk9588_nand_page_stride(Bbk9588NandState *s);
bool bbk9588_nand_consume_busy_read(Bbk9588NandState *s);

#endif
