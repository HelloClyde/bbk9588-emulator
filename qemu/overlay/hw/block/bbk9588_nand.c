/*
 * BBK 9588 raw NAND device.
 *
 * Models the board NAND command/address/data windows and writable raw backing.
 * The JZ4740 EMC register block is a separate device which observes transfers
 * through the NAND data window and owns the ECC engine.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "qapi/error.h"
#include "hw/block/bbk9588_nand.h"
#include "hw/core/qdev-properties-system.h"
#include "migration/vmstate.h"
#include "qemu/bswap.h"
#include "qemu/error-report.h"
#include "qemu/module.h"
#include "system/block-backend.h"

#define NAND_MMIO_SIZE            0x20000u
#define NAND_READ_BUFFER_SIZE     4096u

#define NAND_ID_MAKER             0xecu
#define NAND_ID_BYTE2             0x10u
#define NAND_ID_BYTE3             0x95u
#define NAND_ID_BYTE4             0x44u

#define NAND_READ_SOURCE_RUNTIME  1u
#define NAND_READ_REQUEST_BLANK   8u
#define NAND_READ_FINAL_BLANK     16u
#define NAND_STATUS_FAIL          0x01u
#define NAND_STATUS_READY         0x40u
#define NAND_NO_FAIL_BLOCK        UINT32_MAX

struct Bbk9588NandState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    BlockBackend *blk;
    char *image_path;
    uint32_t id_code;
    uint8_t command;
    uint8_t status;
    uint8_t address[5];
    uint32_t address_count;
    uint8_t read_buffer[NAND_READ_BUFFER_SIZE];
    uint32_t read_index;
    uint32_t read_page;
    uint32_t read_column;
    uint32_t busy_reads;
    uint8_t *data;
    uint64_t size;
    uint32_t page_stride;
    uint8_t program_buffer[BBK9588_NAND_RAW_STRIDE];
    uint32_t program_start;
    uint32_t program_len;
    uint32_t program_page;
    uint32_t program_column;
    bool program_has_data;
    bool program_page_valid;
    bool trace_enabled;
    uint32_t page_read_count;
    uint32_t program_count;
    uint32_t erase_count;
    uint32_t program_fail_count;
    uint32_t erase_fail_count;
    uint32_t fail_program_block;
    uint32_t fail_erase_block;
    uint32_t last_page;
    uint32_t last_column;
    uint32_t last_block;
    Bbk9588NandEventCallback event_callback;
    void *event_opaque;
    Bbk9588NandDataCallback data_callback;
    void *data_opaque;
};

static void nand_emit(Bbk9588NandState *s, Bbk9588NandEventType type,
                      uint32_t page, uint32_t column, uint32_t count,
                      uint32_t value, uint32_t flags, uint32_t index,
                      bool failed)
{
    Bbk9588NandEvent event = {
        .type = type,
        .failed = failed,
        .page = page,
        .column = column,
        .count = count,
        .value = value,
        .flags = flags,
        .index = index,
    };

    if (s->event_callback) {
        s->event_callback(s->event_opaque, &event);
    }
}

void bbk9588_nand_set_event_callback(Bbk9588NandState *s,
                                     Bbk9588NandEventCallback callback,
                                     void *opaque)
{
    if (!s) {
        return;
    }
    s->event_callback = callback;
    s->event_opaque = opaque;
}

void bbk9588_nand_set_data_callback(Bbk9588NandState *s,
                                    Bbk9588NandDataCallback callback,
                                    void *opaque)
{
    if (!s) {
        return;
    }
    s->data_callback = callback;
    s->data_opaque = opaque;
}

void bbk9588_nand_set_trace_enabled(Bbk9588NandState *s, bool enabled)
{
    if (s) {
        s->trace_enabled = enabled;
    }
}

const uint8_t *bbk9588_nand_raw_data(Bbk9588NandState *s)
{
    return s ? s->data : NULL;
}

uint64_t bbk9588_nand_size(Bbk9588NandState *s)
{
    return s ? s->size : 0;
}

uint32_t bbk9588_nand_page_stride(Bbk9588NandState *s)
{
    return s && s->page_stride ? s->page_stride :
                                 BBK9588_NAND_RAW_STRIDE;
}

bool bbk9588_nand_consume_busy_read(Bbk9588NandState *s)
{
    if (!s || s->busy_reads == 0) {
        return false;
    }
    s->busy_reads--;
    return true;
}

void bbk9588_nand_get_diagnostics(Bbk9588NandState *s,
                                  Bbk9588NandDiagnostics *diagnostics)
{
    if (!diagnostics) {
        return;
    }
    memset(diagnostics, 0, sizeof(*diagnostics));
    if (!s) {
        return;
    }
    diagnostics->command = s->command;
    diagnostics->status = s->status;
    diagnostics->address_count = s->address_count;
    diagnostics->busy_reads = s->busy_reads;
    diagnostics->page_stride = bbk9588_nand_page_stride(s);
    diagnostics->size = s->size;
    diagnostics->page_read_count = s->page_read_count;
    diagnostics->program_count = s->program_count;
    diagnostics->erase_count = s->erase_count;
    diagnostics->program_fail_count = s->program_fail_count;
    diagnostics->erase_fail_count = s->erase_fail_count;
    diagnostics->last_page = s->last_page;
    diagnostics->last_column = s->last_column;
    diagnostics->last_block = s->last_block;
}

static void nand_detect_geometry(Bbk9588NandState *s)
{
    if (!s->data || s->size == 0) {
        return;
    }
    if (s->size % BBK9588_NAND_RAW_STRIDE == 0) {
        s->page_stride = BBK9588_NAND_RAW_STRIDE;
    } else if (s->size % BBK9588_NAND_PAGE_SIZE == 0) {
        s->page_stride = BBK9588_NAND_PAGE_SIZE;
    } else {
        s->page_stride = BBK9588_NAND_RAW_STRIDE;
    }
}

static bool nand_load_backing(Bbk9588NandState *s, Error **errp)
{
    if (s->blk) {
        int64_t length;
        uint64_t perm;
        int ret;

        perm = BLK_PERM_CONSISTENT_READ |
               (blk_supports_write_perm(s->blk) ? BLK_PERM_WRITE : 0);
        ret = blk_set_perm(s->blk, perm, BLK_PERM_ALL, errp);
        if (ret < 0) {
            return false;
        }
        length = blk_getlength(s->blk);
        if (length <= 0) {
            error_setg(errp, "bbk9588: invalid MTD NAND image size");
            return false;
        }
        s->data = g_malloc(length);
        if (blk_pread(s->blk, 0, length, s->data, 0) < 0) {
            error_setg(errp, "bbk9588: could not read MTD NAND image");
            g_clear_pointer(&s->data, g_free);
            return false;
        }
        s->size = length;
        nand_detect_geometry(s);
        info_report("bbk9588: loaded MTD NAND image (%" PRId64
                    " bytes, page_stride=%u)", length, s->page_stride);
        return true;
    }
    if (s->image_path && s->image_path[0]) {
        GError *error = NULL;
        gchar *data = NULL;
        gsize size = 0;

        if (!g_file_get_contents(s->image_path, &data, &size, &error)) {
            error_setg(errp, "bbk9588: could not load NAND image '%s': %s",
                       s->image_path,
                       error ? error->message : "unknown error");
            g_clear_error(&error);
            return false;
        }
        s->data = (uint8_t *)data;
        s->size = size;
        nand_detect_geometry(s);
        info_report("bbk9588: loaded NAND image '%s' (%" G_GSIZE_FORMAT
                    " bytes, page_stride=%u)", s->image_path, size,
                    s->page_stride);
        return true;
    }
    info_report("bbk9588: no nand-image supplied; CS0 returns erased data");
    return true;
}

static void nand_fill_read(Bbk9588NandState *s, const uint8_t *data,
                           size_t length)
{
    memset(s->read_buffer, 0xff, sizeof(s->read_buffer));
    memcpy(s->read_buffer, data, MIN(length, sizeof(s->read_buffer)));
    s->read_index = 0;
}

static void nand_fill_read_id(Bbk9588NandState *s)
{
    uint8_t id[] = {
        NAND_ID_MAKER,
        s->id_code & 0xffu,
        NAND_ID_BYTE2,
        NAND_ID_BYTE3,
        NAND_ID_BYTE4,
    };

    nand_fill_read(s, id, sizeof(id));
}

static void nand_prepare_erased_page(Bbk9588NandState *s)
{
    memset(s->read_buffer, 0xff, sizeof(s->read_buffer));
    s->read_index = 0;
}

static bool nand_data_region_is_blank(const uint8_t *data, uint64_t size,
                                      uint32_t stride, uint32_t page,
                                      uint32_t column, uint32_t length)
{
    uint64_t offset = (uint64_t)page * stride + column;

    if (!data || column >= BBK9588_NAND_PAGE_SIZE ||
        offset + length > size) {
        return true;
    }
    for (uint32_t i = 0; i < length; i++) {
        if (data[offset + i] != 0xffu) {
            return false;
        }
    }
    return true;
}

static uint32_t nand_page_from_address(Bbk9588NandState *s)
{
    if (s->address_count < 5) {
        return 0;
    }
    return s->address[2] | (s->address[3] << 8) |
           (s->address[4] << 16);
}

static uint32_t nand_column_from_address(Bbk9588NandState *s)
{
    if (s->address_count < 2) {
        return 0;
    }
    return s->address[0] | (s->address[1] << 8);
}

static uint32_t nand_row_from_address(Bbk9588NandState *s)
{
    uint32_t row = 0;

    for (unsigned i = 0; i < MIN(s->address_count, 3u); i++) {
        row |= (uint32_t)s->address[i] << (i * 8);
    }
    return row;
}

static void nand_prepare_page_read(Bbk9588NandState *s)
{
    uint32_t column;
    uint32_t page;
    uint64_t offset;
    uint32_t stride = bbk9588_nand_page_stride(s);
    uint32_t copy_length = 0;
    uint32_t flags = NAND_READ_SOURCE_RUNTIME;

    if (s->address_count < 5) {
        nand_prepare_erased_page(s);
        return;
    }
    column = nand_column_from_address(s);
    page = nand_page_from_address(s);
    s->read_page = page;
    s->read_column = column;
    if (column < BBK9588_NAND_PAGE_SIZE &&
        nand_data_region_is_blank(s->data, s->size, stride, page, column,
                                  BBK9588_NAND_PAGE_SIZE - column)) {
        flags |= NAND_READ_REQUEST_BLANK;
    }
    offset = (uint64_t)page * stride + column;
    memset(s->read_buffer, 0xff, sizeof(s->read_buffer));
    if (s->data && column < stride && offset < s->size) {
        copy_length = MIN((uint64_t)sizeof(s->read_buffer),
                          s->size - offset);
        memcpy(s->read_buffer, s->data + offset, copy_length);
    }
    if (column >= BBK9588_NAND_PAGE_SIZE ||
        nand_data_region_is_blank(s->data, s->size, stride, page, column,
                                  BBK9588_NAND_PAGE_SIZE - column)) {
        flags |= NAND_READ_FINAL_BLANK;
    }
    if (s->trace_enabled && column < stride) {
        uint32_t first_word = 0xffffffffu;

        if (copy_length >= 4) {
            first_word = ldl_le_p(s->read_buffer);
        }
        nand_emit(s, BBK9588_NAND_EVENT_READ_TRACE, page, column,
                  copy_length, first_word, flags, 0, false);
        nand_emit(s, BBK9588_NAND_EVENT_READ_DETAIL, page, column, 0,
                  0, flags, 0, false);
    }
    s->read_index = 0;
}

static void nand_begin_program(Bbk9588NandState *s)
{
    memset(s->program_buffer, 0xff, sizeof(s->program_buffer));
    s->program_start = BBK9588_NAND_RAW_STRIDE;
    s->program_len = 0;
    s->program_page = 0;
    s->program_column = 0;
    s->program_has_data = false;
    s->program_page_valid = false;
}

static void nand_append_program_data(Bbk9588NandState *s, uint64_t value,
                                     unsigned size)
{
    unsigned max_size = MIN(size, 4u);

    if (s->command != 0x80 && s->command != 0x85) {
        return;
    }
    for (unsigned i = 0; i < max_size; i++) {
        if (s->program_column >= sizeof(s->program_buffer)) {
            return;
        }
        if (!s->program_has_data) {
            s->program_start = s->program_column;
        }
        s->program_buffer[s->program_column++] =
            (value >> (i * 8)) & 0xffu;
        s->program_has_data = true;
        s->program_len = MAX(s->program_len, s->program_column);
    }
}

static void nand_backend_update(Bbk9588NandState *s, uint64_t offset,
                                uint64_t length)
{
    uint64_t write_start;
    uint64_t write_end;
    int ret;

    if (!s->blk || !blk_is_writable(s->blk) || length == 0) {
        return;
    }
    write_start = QEMU_ALIGN_DOWN(offset, BDRV_SECTOR_SIZE);
    write_end = QEMU_ALIGN_UP(offset + length, BDRV_SECTOR_SIZE);
    if (write_start >= s->size) {
        return;
    }
    write_end = MIN(write_end, s->size);
    ret = blk_pwrite(s->blk, write_start, write_end - write_start,
                     s->data + write_start, 0);
    if (ret < 0) {
        error_report("bbk9588: could not update NAND offset=0x%" PRIx64
                     " length=0x%" PRIx64 ": %s", write_start,
                     write_end - write_start, strerror(-ret));
    }
}

static uint32_t nand_first_word(Bbk9588NandState *s, uint64_t offset,
                                uint32_t length)
{
    return length >= 4 && offset + 4 <= s->size ?
           ldl_le_p(s->data + offset) : 0xffffffffu;
}

static void nand_commit_program(Bbk9588NandState *s)
{
    uint64_t page_offset;
    uint64_t data_offset;
    uint32_t page;
    uint32_t column;
    uint32_t stride = bbk9588_nand_page_stride(s);
    uint32_t limit;
    uint32_t block;

    if (!s->data || !s->program_page_valid || !s->program_has_data) {
        s->status = NAND_STATUS_READY;
        nand_begin_program(s);
        return;
    }
    page = s->program_page;
    column = MIN(s->program_start, s->program_len);
    page_offset = (uint64_t)page * stride;
    if (page_offset >= s->size || column >= stride ||
        s->program_len <= column) {
        s->status = NAND_STATUS_READY | NAND_STATUS_FAIL;
        s->program_fail_count++;
        nand_begin_program(s);
        return;
    }
    limit = MIN(s->program_len - column, stride - column);
    limit = MIN((uint64_t)limit, s->size - page_offset - column);
    data_offset = page_offset + column;
    block = page / BBK9588_NAND_PAGES_PER_BLOCK;
    if (block == s->fail_program_block) {
        s->status = NAND_STATUS_READY | NAND_STATUS_FAIL;
        s->program_fail_count++;
        s->last_page = page;
        s->last_column = column;
        s->last_block = block;
        nand_emit(s, BBK9588_NAND_EVENT_PROGRAM, page, column, limit,
                  nand_first_word(s, data_offset, limit),
                  0, 0, true);
        nand_begin_program(s);
        return;
    }
    for (uint32_t i = 0; i < limit; i++) {
        s->data[data_offset + i] &= s->program_buffer[column + i];
    }
    nand_backend_update(s, data_offset, limit);
    s->program_count++;
    s->status = NAND_STATUS_READY;
    s->last_page = page;
    s->last_column = column;
    s->last_block = block;
    nand_emit(s, BBK9588_NAND_EVENT_PROGRAM, page, column, limit,
              nand_first_word(s, data_offset, limit), 0, 0, false);
    s->program_page = page;
    s->program_column = column;
    s->program_start = BBK9588_NAND_RAW_STRIDE;
    s->program_len = 0;
    s->program_has_data = false;
}

static void nand_commit_erase(Bbk9588NandState *s)
{
    uint32_t row;
    uint32_t block_start;
    uint32_t stride = bbk9588_nand_page_stride(s);

    if (!s->data || s->address_count < 2) {
        return;
    }
    row = nand_row_from_address(s);
    block_start = row & ~(BBK9588_NAND_PAGES_PER_BLOCK - 1u);
    if (block_start / BBK9588_NAND_PAGES_PER_BLOCK ==
        s->fail_erase_block) {
        s->status = NAND_STATUS_READY | NAND_STATUS_FAIL;
        s->erase_fail_count++;
        s->last_page = row;
        s->last_block = block_start;
        nand_emit(s, BBK9588_NAND_EVENT_ERASE, row, 0,
                  BBK9588_NAND_PAGES_PER_BLOCK, 0xffffffffu,
                  block_start, 0, true);
        return;
    }
    for (uint32_t page = block_start;
         page < block_start + BBK9588_NAND_PAGES_PER_BLOCK; page++) {
        uint64_t offset = (uint64_t)page * stride;
        uint64_t length;

        if (offset >= s->size) {
            break;
        }
        length = MIN((uint64_t)stride, s->size - offset);
        memset(s->data + offset, 0xff, length);
        nand_backend_update(s, offset, length);
    }
    s->erase_count++;
    s->status = NAND_STATUS_READY;
    s->last_page = row;
    s->last_block = block_start;
    nand_emit(s, BBK9588_NAND_EVENT_ERASE, row, 0,
              BBK9588_NAND_PAGES_PER_BLOCK, 0xffffffffu, block_start, 0,
              false);
}

static uint32_t nand_read_data(Bbk9588NandState *s, unsigned size)
{
    uint32_t value = 0xffffffffu;

    for (unsigned i = 0; i < MIN(size, 4u); i++) {
        uint32_t index = s->read_index;
        uint8_t byte = index < sizeof(s->read_buffer) ?
                       s->read_buffer[index] : 0xffu;

        if (s->trace_enabled &&
            ((s->read_page == 0x2c30cu && index >= 0x6b8u && index < 0x6c0u) ||
             (s->read_page == 0x2c30fu && index >= 0x598u && index < 0x5a0u))) {
            nand_emit(s, BBK9588_NAND_EVENT_DEBUG_BYTE, s->read_page,
                      s->read_column, 1, byte, 0, index, false);
        }
        s->read_index++;
        value = (value & ~(0xffu << (i * 8))) |
                ((uint32_t)byte << (i * 8));
    }
    return value;
}

static void nand_command(Bbk9588NandState *s, uint8_t command)
{
    s->command = command;
    nand_emit(s, BBK9588_NAND_EVENT_COMMAND, 0, 0, 0, command, 0, 0,
              false);
    if (command == 0x00 || command == 0x60 || command == 0x80 ||
        command == 0x85 || command == 0x90 || command == 0xff) {
        s->address_count = 0;
        s->read_index = 0;
    }
    switch (command) {
    case 0x30:
        nand_prepare_page_read(s);
        s->busy_reads = 1;
        s->page_read_count++;
        s->last_page = nand_page_from_address(s);
        s->last_column = nand_column_from_address(s);
        nand_emit(s, BBK9588_NAND_EVENT_READ, s->last_page, s->last_column,
                  0, 0, 0, 0, false);
        break;
    case 0x35:
        nand_prepare_page_read(s);
        memcpy(s->program_buffer, s->read_buffer,
               sizeof(s->program_buffer));
        s->program_len = sizeof(s->program_buffer);
        s->program_column = 0;
        s->program_has_data = true;
        s->program_page_valid = false;
        s->busy_reads = 1;
        s->page_read_count++;
        s->last_page = nand_page_from_address(s);
        s->last_column = nand_column_from_address(s);
        nand_emit(s, BBK9588_NAND_EVENT_READ, s->last_page, s->last_column,
                  0, 0, 0, 0, false);
        break;
    case 0x80:
        nand_begin_program(s);
        break;
    case 0x10:
        nand_commit_program(s);
        s->busy_reads = 1;
        break;
    case 0xd0:
        nand_commit_erase(s);
        s->busy_reads = 1;
        s->address_count = 0;
        s->read_index = 0;
        break;
    case 0x70:
        nand_fill_read(s, &s->status, 1);
        break;
    case 0x90:
        nand_fill_read_id(s);
        break;
    case 0xff:
        s->status = NAND_STATUS_READY;
        s->busy_reads = 0;
        nand_prepare_erased_page(s);
        break;
    default:
        break;
    }
}

static void nand_address(Bbk9588NandState *s, uint8_t value)
{
    if (s->address_count < ARRAY_SIZE(s->address)) {
        s->address[s->address_count++] = value;
    }
    if (s->command == 0x90 && s->address_count == 1 && s->address[0] == 0) {
        nand_fill_read_id(s);
    }
    if ((s->command == 0x80 || s->command == 0x85) &&
        s->address_count >= 5) {
        s->program_column = nand_column_from_address(s);
        s->program_page = nand_page_from_address(s);
        s->program_page_valid = true;
    } else if (s->command == 0x85 && s->address_count >= 2) {
        s->program_column = nand_column_from_address(s);
    }
}

static uint64_t nand_mmio_read(void *opaque, hwaddr offset, unsigned size)
{
    Bbk9588NandState *s = opaque;
    uint32_t value;

    if (offset != 0) {
        return 0xffffffffu;
    }
    value = nand_read_data(s, size);
    if (s->data_callback) {
        s->data_callback(s->data_opaque, value, size, false);
    }
    return value;
}

static void nand_mmio_write(void *opaque, hwaddr offset, uint64_t value,
                            unsigned size)
{
    Bbk9588NandState *s = opaque;

    switch (offset) {
    case 0x00000:
        nand_append_program_data(s, value, size);
        if (s->data_callback) {
            s->data_callback(s->data_opaque, value, size, true);
        }
        break;
    case 0x08000:
        if (size == 1) {
            nand_command(s, value & 0xffu);
        }
        break;
    case 0x10000:
        if (size == 1) {
            nand_address(s, value & 0xffu);
        }
        break;
    default:
        break;
    }
}

static const MemoryRegionOps nand_ops = {
    .read = nand_mmio_read,
    .write = nand_mmio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
};

static void nand_reset_hold(Object *obj, ResetType type)
{
    Bbk9588NandState *s = BBK9588_NAND(obj);

    s->command = 0xffu;
    s->status = NAND_STATUS_READY;
    memset(s->address, 0, sizeof(s->address));
    s->address_count = 0;
    s->read_page = 0;
    s->read_column = 0;
    s->busy_reads = 0;
    s->page_stride = s->page_stride ? s->page_stride :
                                      BBK9588_NAND_RAW_STRIDE;
    s->page_read_count = 0;
    s->program_count = 0;
    s->erase_count = 0;
    s->program_fail_count = 0;
    s->erase_fail_count = 0;
    s->last_page = 0;
    s->last_column = 0;
    s->last_block = 0;
    nand_begin_program(s);
    nand_prepare_erased_page(s);
}

static int nand_post_load(void *opaque, int version_id)
{
    Bbk9588NandState *s = opaque;

    if (version_id < 2) {
        s->status = NAND_STATUS_READY;
    }
    return 0;
}

static const VMStateDescription vmstate_bbk9588_nand = {
    .name = TYPE_BBK9588_NAND,
    .version_id = 2,
    .minimum_version_id = 1,
    .post_load = nand_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT8(command, Bbk9588NandState),
        VMSTATE_UINT8_V(status, Bbk9588NandState, 2),
        VMSTATE_UINT8_ARRAY(address, Bbk9588NandState, 5),
        VMSTATE_UINT32(address_count, Bbk9588NandState),
        VMSTATE_UINT8_ARRAY(read_buffer, Bbk9588NandState,
                            NAND_READ_BUFFER_SIZE),
        VMSTATE_UINT32(read_index, Bbk9588NandState),
        VMSTATE_UINT32(read_page, Bbk9588NandState),
        VMSTATE_UINT32(read_column, Bbk9588NandState),
        VMSTATE_UINT32(busy_reads, Bbk9588NandState),
        VMSTATE_UINT32(page_stride, Bbk9588NandState),
        VMSTATE_UINT8_ARRAY(program_buffer, Bbk9588NandState,
                            BBK9588_NAND_RAW_STRIDE),
        VMSTATE_UINT32(program_start, Bbk9588NandState),
        VMSTATE_UINT32(program_len, Bbk9588NandState),
        VMSTATE_UINT32(program_page, Bbk9588NandState),
        VMSTATE_UINT32(program_column, Bbk9588NandState),
        VMSTATE_BOOL(program_has_data, Bbk9588NandState),
        VMSTATE_BOOL(program_page_valid, Bbk9588NandState),
        VMSTATE_UINT32(page_read_count, Bbk9588NandState),
        VMSTATE_UINT32(program_count, Bbk9588NandState),
        VMSTATE_UINT32(erase_count, Bbk9588NandState),
        VMSTATE_UINT32_V(program_fail_count, Bbk9588NandState, 2),
        VMSTATE_UINT32_V(erase_fail_count, Bbk9588NandState, 2),
        VMSTATE_UINT32(last_page, Bbk9588NandState),
        VMSTATE_UINT32(last_column, Bbk9588NandState),
        VMSTATE_UINT32(last_block, Bbk9588NandState),
        VMSTATE_END_OF_LIST()
    },
};

static const Property nand_properties[] = {
    DEFINE_PROP_DRIVE("drive", Bbk9588NandState, blk),
    DEFINE_PROP_STRING("image-path", Bbk9588NandState, image_path),
    DEFINE_PROP_UINT32("id-code", Bbk9588NandState, id_code,
                       BBK9588_NAND_DEFAULT_ID_CODE),
    DEFINE_PROP_UINT32("fail-program-block", Bbk9588NandState,
                       fail_program_block, NAND_NO_FAIL_BLOCK),
    DEFINE_PROP_UINT32("fail-erase-block", Bbk9588NandState,
                       fail_erase_block, NAND_NO_FAIL_BLOCK),
};

static void nand_realize(DeviceState *dev, Error **errp)
{
    Bbk9588NandState *s = BBK9588_NAND(dev);

    memory_region_init_io(&s->iomem, OBJECT(dev), &nand_ops, s,
                          TYPE_BBK9588_NAND, NAND_MMIO_SIZE);
    sysbus_init_mmio(SYS_BUS_DEVICE(dev), &s->iomem);
    if (!nand_load_backing(s, errp)) {
        return;
    }
    nand_reset_hold(OBJECT(s), RESET_TYPE_COLD);
}

static void nand_finalize(Object *obj)
{
    Bbk9588NandState *s = BBK9588_NAND(obj);

    g_free(s->data);
    g_free(s->image_path);
}

static void nand_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);
    ResettableClass *rc = RESETTABLE_CLASS(oc);

    dc->realize = nand_realize;
    dc->vmsd = &vmstate_bbk9588_nand;
    device_class_set_props(dc, nand_properties);
    set_bit(DEVICE_CATEGORY_STORAGE, dc->categories);
    rc->phases.hold = nand_reset_hold;
}

static const TypeInfo nand_type_info = {
    .name = TYPE_BBK9588_NAND,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(Bbk9588NandState),
    .instance_finalize = nand_finalize,
    .class_init = nand_class_init,
};

static void nand_register_types(void)
{
    type_register_static(&nand_type_info);
}

type_init(nand_register_types)
