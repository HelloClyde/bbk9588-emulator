/*
 * BBK 9588 board skeleton.
 *
 * This is the first step away from running the firmware on the Malta board.
 * It intentionally models only CPU, RAM, raw firmware loading, and named MMIO
 * holes for the JZ47xx-style devices that the firmware already probes.
 */

#include "qemu/osdep.h"
#include "qemu/units.h"
#include "qapi/error.h"
#include "qapi/visitor.h"
#include "system/address-spaces.h"
#include "system/block-backend.h"
#include "system/blockdev.h"
#include "system/reset.h"
#include "system/runstate.h"
#include "system/system.h"
#include "chardev/char.h"
#include "exec/cpu-common.h"
#include "exec/cpu-interrupt.h"
#include "hw/core/boards.h"
#include "hw/core/cpu.h"
#include "hw/core/clock.h"
#include "hw/core/irq.h"
#include "hw/core/loader.h"
#include "hw/core/qdev-properties-system.h"
#include "hw/core/sysbus.h"
#include "hw/audio/jz4740_aic.h"
#include "hw/block/bbk9588_nand.h"
#include "hw/char/jz4740_uart.h"
#include "hw/display/bbk9588_host_bridge.h"
#include "hw/display/bbk9588_panel.h"
#include "hw/display/jz4740_lcd.h"
#include "hw/dma/bbk9588_dma_bridge.h"
#include "hw/dma/jz4740_dmac.h"
#include "hw/gpio/jz4740_gpio.h"
#include "hw/input/bbk9588_host_input.h"
#include "hw/input/jz4740_sadc.h"
#include "hw/intc/jz4740_intc.h"
#include "hw/mem/jz4740_ecc.h"
#include "hw/mem/jz4740_emc.h"
#include "hw/misc/bbk9588_diag.h"
#include "hw/misc/jz4740_cim.h"
#include "hw/misc/jz4740_cpm.h"
#include "hw/rtc/jz4740_rtc.h"
#include "hw/sd/jz4740_msc.h"
#include "hw/timer/jz4740_tcu.h"
#include "hw/usb/jz4740_udc.h"
#include "qemu/error-report.h"
#include "qemu/atomic.h"
#include "qemu/cutils.h"
#include "qemu/host-utils.h"
#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "qom/object.h"
#include "target/mips/cpu.h"

#define BBK9588_RAM_DEFAULT_SIZE   (160 * MiB)
#define BBK9588_RAM_VADDR          0x80000000u
#define BBK9588_RAM_SIZE           BBK9588_RAM_DEFAULT_SIZE
#define BBK9588_FIRMWARE_PHYS      0x00004000
#define BBK9588_FIRMWARE_VADDR     0x80004000
#define BBK9588_KSEG_TO_PHYS(addr) ((addr) & 0x1fffffff)
#define BBK9588_BOOTROM_NAND_PAGE  0u
#define BBK9588_BOOTROM_BACKUP_NAND_ADDR 0x2000u
#define BBK9588_BOOTROM_FIRST_STAGE_BYTES 0x2000u
#define BBK9588_BOOTROM_ENTRY_VADDR 0x80000004u
#define BBK9588_LCD_WIDTH          240
#define BBK9588_LCD_HEIGHT         320
#define BBK9588_HOST_KEY_POWER     11u
#define BBK9588_LCD_STRIDE         (BBK9588_LCD_WIDTH * 2)
#define BBK9588_LCD_BYTES          (BBK9588_LCD_STRIDE * BBK9588_LCD_HEIGHT)
#define BBK9588_LCD_VBLANK_PERIOD_MS 33
#define BBK9588_GUI_EVENT_OBJ_OFF  0xf0u
#define BBK9588_NAND_READ_SOURCE_RUNTIME 1u
#define BBK9588_NAND_READ_REQUEST_BLANK 8u
#define BBK9588_NAND_READ_FINAL_BLANK 16u
#define BBK9588_NAND_TARGET_BLOCK 0x2540u
#define BBK9588_NAND_TARGET_PAGE 0x256au
#define BBK9588_NAND_TARGET_EVENT_ERASE 1u
#define BBK9588_NAND_TARGET_EVENT_PROGRAM 2u
/*
 * C200 firmware accesses SoC devices through KSEG1 uncached addresses such as
 * 0xb0001000. QEMU MemoryRegions are mapped in physical address space, so the
 * board exposes those windows at kseg1 & 0x1fffffff.
 */
#define KSEG1_TO_PHYS(addr)        ((addr) & 0x1fffffff)

typedef struct Bbk9588MachineState Bbk9588MachineState;

struct Bbk9588MachineState {
    MachineState parent_obj;

    MIPSCPU *cpu;
    qemu_irq cpu_irq;
    qemu_irq aic_irq;
    qemu_irq intc_irq;
    qemu_irq dmac_irq;
    qemu_irq tcu_irq[JZ4740_TCU_NUM_OUTPUTS];
    QEMUTimer *intc_resample_timer;
    QEMUTimer *progress_trace_timer;
    bool cpu_irq_output_enabled;
    bool intc_output_level;
    uint32_t tcu_period_ms;
    uint32_t progress_trace_period_ms;
    uint32_t lcd_refresh_period_ms;
    uint32_t sadc_battery_raw;
    uint32_t sadc_sadcin_raw;
    uint32_t intc_last_cp0_status;
    uint32_t intc_last_cp0_cause;
    JZ4740AICState *aic;
    Bbk9588DiagState *diag;
    Bbk9588HostBridgeState *host_bridge;
    Bbk9588HostInputState *host_input;
    JZ4740LCDState *lcd;
    Bbk9588PanelState *panel;
    JZ4740INTCState *intc;
    JZ4740EMCState *emc;
    JZ4740CIMState *cim;
    JZ4740CPMState *cpm;
    Bbk9588DMABridgeState *dma_bridge;
    JZ4740DMACState *dmac;
    JZ4740MSCState *msc;
    JZ4740GPIOState *gpio;
    JZ4740RTCState *rtc;
    JZ4740SADCState *sadc;
    JZ4740TCUState *tcu;
    JZ4740UARTState *uart;
    JZ4740UDCState *udc;
    Bbk9588NandState *nand_dev;
    bool storage_trace_enabled;
    bool graphics_trace;
    bool touch_trace;
    bool progress_trace;
    char *input_chardev;
    char *frame_chardev;
    char *nand_image;
    bool bootrom_nand_enabled;
    bool hibernate_poweroff_enabled;
    bool hibernate_wakeup_enabled;
    uint32_t nand_id_code;
    uint32_t firmware_phys;
    uint32_t reset_pc;
    uint32_t bootrom_nand_page;
    uint32_t bootrom_size;
};

#define TYPE_BBK9588_MACHINE MACHINE_TYPE_NAME("bbk9588")
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588MachineState, BBK9588_MACHINE)

static void bbk9588_touch_set_state(Bbk9588MachineState *board,
                                     uint16_t raw_x, uint16_t raw_y,
                                     bool down);
static void bbk9588_queue_input_event(Bbk9588MachineState *board,
                                      uint32_t kind, uint32_t arg0,
                                      uint32_t arg1, uint32_t arg2);
static void bbk9588_key_apply_host_input(void *opaque, uint32_t key_code,
                                         bool down);
static void bbk9588_update_irq(Bbk9588MachineState *board);
static void bbk9588_touch_diag_record(Bbk9588MachineState *board,
                                      uint32_t reason);

static void bbk9588_wake_cpu(Bbk9588MachineState *board)
{
    if (board && board->cpu) {
        CPUState *cs = CPU(board->cpu);

        BQL_LOCK_GUARD();
        cs->halted = 0;
        cpu_interrupt(cs, CPU_INTERRUPT_WAKE);
    }
}

static void bbk9588_sync_tcu_irq_sources(Bbk9588MachineState *board)
{
    jz4740_intc_set_irq(
        board->intc, JZ4740_INTC_IRQ_TCU0,
        jz4740_tcu_irq_level(board->tcu, JZ4740_TCU_IRQ_TCU0));
    jz4740_intc_set_irq(
        board->intc, JZ4740_INTC_IRQ_TCU1,
        jz4740_tcu_irq_level(board->tcu, JZ4740_TCU_IRQ_TCU1));
    jz4740_intc_set_irq(
        board->intc, JZ4740_INTC_IRQ_TCU2,
        jz4740_tcu_irq_level(board->tcu, JZ4740_TCU_IRQ_TCU2));
}

static void bbk9588_sync_level_irq_sources(Bbk9588MachineState *board)
{
    bbk9588_sync_tcu_irq_sources(board);
}

static void bbk9588_drive_cpu_irq(Bbk9588MachineState *board)
{
    bool ip2_level = board->intc_output_level &&
                     board->cpu_irq_output_enabled;

    /* Expose the BBK INTC output as the MIPS CPU IP2 level interrupt. */
    if (board->cpu) {
        BQL_LOCK_GUARD();
        board->cpu->env.bbk9588_irq_ip2_level = ip2_level;
    }
    if (board->cpu_irq) {
        qemu_set_irq(board->cpu_irq, ip2_level);
    }
    if (board->cpu) {
        BQL_LOCK_GUARD();
        board->intc_last_cp0_status = board->cpu->env.CP0_Status;
        board->intc_last_cp0_cause = board->cpu->env.CP0_Cause;
    }
    bbk9588_touch_diag_record(board, 0x51);
    if (board->intc_resample_timer) {
        if (ip2_level) {
            timer_mod(board->intc_resample_timer,
                      qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 1);
        } else {
            timer_del(board->intc_resample_timer);
        }
    }
}

static void bbk9588_intc_output_handler(void *opaque, int n, int level)
{
    Bbk9588MachineState *board = opaque;

    board->intc_output_level = level != 0;
    bbk9588_drive_cpu_irq(board);
}

static void bbk9588_intc_refresh(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    bbk9588_sync_level_irq_sources(board);
}

static void bbk9588_update_irq(Bbk9588MachineState *board)
{
    bbk9588_intc_refresh(board);
    board->intc_output_level = jz4740_intc_output_level(board->intc);
    bbk9588_drive_cpu_irq(board);
}

static void bbk9588_cpm_update(void *opaque)
{
    bbk9588_update_irq(opaque);
}

static void bbk9588_intc_resample_timer_cb(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    bbk9588_update_irq(board);
}

static void bbk9588_nand_raise_ready(Bbk9588MachineState *board)
{
    if (!board) {
        return;
    }
    bbk9588_diag_note_nand_ready(board->diag);
    jz4740_gpio_raise_flag(board->gpio, JZ4740_GPIO_PORT_C, 0x40000000u);
    bbk9588_touch_diag_record(board, 0x52);
}

static void bbk9588_progress_trace_schedule(Bbk9588MachineState *board)
{
    if (!board->progress_trace_timer ||
        board->progress_trace_period_ms == 0) {
        return;
    }
    timer_mod(board->progress_trace_timer,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
              board->progress_trace_period_ms);
}

static void bbk9588_queue_input_event(Bbk9588MachineState *board,
                                      uint32_t kind, uint32_t arg0,
                                      uint32_t arg1, uint32_t arg2)
{
    if (board) {
        bbk9588_diag_queue_input(board->diag, kind, arg0, arg1, arg2);
    }
}

static void bbk9588_touch_apply_host_input(void *opaque, uint16_t raw_x,
                                           uint16_t raw_y, uint16_t x,
                                           uint16_t y, bool down)
{
    Bbk9588MachineState *board = opaque;

    (void)x;
    (void)y;
    bbk9588_touch_set_state(board, raw_x, raw_y, down);
}

static bool bbk9588_key_gpio_bits(uint32_t key_code, unsigned *port,
                                  uint32_t *mask)
{
    switch (key_code) {
    case 10:
        *port = JZ4740_GPIO_PORT_B;
        *mask = 0x40000000u;
        return true;
    case 5:
        *port = JZ4740_GPIO_PORT_B;
        *mask = 0x10000000u;
        return true;
    case 7:
        *port = JZ4740_GPIO_PORT_B;
        *mask = 0x08000000u;
        return true;
    case 6:
        *port = JZ4740_GPIO_PORT_B;
        *mask = 0x20000000u;
        return true;
    case 9:
        *port = JZ4740_GPIO_PORT_C;
        *mask = 0x08000000u;
        return true;
    case 4:
        *port = JZ4740_GPIO_PORT_D;
        *mask = 0x00200000u;
        return true;
    case BBK9588_HOST_KEY_POWER:
        *port = JZ4740_GPIO_PORT_D;
        *mask = 0x20000000u;
        return true;
    default:
        return false;
    }
}

static bool bbk9588_key_gpio_set_state(Bbk9588MachineState *board,
                                       uint32_t key_code, bool down)
{
    unsigned port = 0;
    uint32_t mask = 0;

    if (!bbk9588_key_gpio_bits(key_code, &port, &mask)) {
        return false;
    }
    return jz4740_gpio_set_input_level(board->gpio, port, mask,
                                       !down, true);
}

static void bbk9588_key_apply_host_input(void *opaque, uint32_t key_code,
                                         bool down)
{
    Bbk9588MachineState *board = opaque;

    if (bbk9588_key_gpio_set_state(board, key_code & 0xff, down)) {
        bbk9588_queue_input_event(board, BBK9588_DIAG_EVENT_KIND_KEY,
                                  key_code & 0xff, down ? 1 : 0, 0);
    }
}

static void bbk9588_progress_trace_timer_cb(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    bbk9588_diag_progress_record(board->diag, 2);

    bbk9588_progress_trace_schedule(board);
}

static void bbk9588_nand_event(void *opaque,
                                const Bbk9588NandEvent *event)
{
    Bbk9588MachineState *board = opaque;
    uint32_t pc = 0;
    uint32_t s0 = 0;

    if (!board || !event) {
        return;
    }
    if (board->cpu) {
        pc = board->cpu->env.active_tc.PC & 0xffffffffu;
        s0 = board->cpu->env.active_tc.gpr[16] & 0xffffffffu;
    }

    switch (event->type) {
    case BBK9588_NAND_EVENT_COMMAND:
        break;
    case BBK9588_NAND_EVENT_READ:
        bbk9588_nand_raise_ready(board);
        break;
    case BBK9588_NAND_EVENT_READ_TRACE:
        bbk9588_diag_storage_record(
            board->diag, BBK9588_DIAG_STORAGE_NAND_READ | event->page,
            ((event->flags & 0xffu) << 24) |
            (event->page & 0x00ffffffu),
            event->value);
        break;
    case BBK9588_NAND_EVENT_READ_DETAIL:
        bbk9588_diag_storage_record(
            board->diag, BBK9588_DIAG_STORAGE_NAND_READ |
            BBK9588_DIAG_STORAGE_NAND_DETAIL |
            (event->page & 0x03ffffffu),
            event->column, pc);
        break;
    case BBK9588_NAND_EVENT_PROGRAM:
        if (event->failed && board->storage_trace_enabled) {
            bbk9588_diag_storage_record(
                board->diag,
                BBK9588_DIAG_STORAGE_NAND_PROGRAM | event->page,
                event->column, 0xffffffffu);
        }
        if (board->storage_trace_enabled &&
            !event->failed && event->column < BBK9588_NAND_PAGE_SIZE) {
            bbk9588_diag_storage_record(
                board->diag,
                BBK9588_DIAG_STORAGE_NAND_PROGRAM | event->page,
                event->column, event->value);
        }
        if (!event->failed &&
            event->page == BBK9588_NAND_TARGET_PAGE &&
            event->column < BBK9588_NAND_PAGE_SIZE) {
            bbk9588_diag_nand_target_record(
                board->diag,
                BBK9588_NAND_TARGET_EVENT_PROGRAM, event->page,
                event->column, event->value, pc);
        }
        bbk9588_nand_raise_ready(board);
        break;
    case BBK9588_NAND_EVENT_ERASE:
        if (board->storage_trace_enabled) {
            bbk9588_diag_storage_record(
                board->diag,
                BBK9588_DIAG_STORAGE_NAND_ERASE | event->flags,
                event->count,
                event->failed ? 0u : event->value);
        }
        if (!event->failed &&
            event->flags == BBK9588_NAND_TARGET_BLOCK) {
            bbk9588_diag_nand_target_record(
                board->diag,
                BBK9588_NAND_TARGET_EVENT_ERASE, event->flags,
                event->page, event->count, pc);
        }
        bbk9588_nand_raise_ready(board);
        break;
    case BBK9588_NAND_EVENT_DEBUG_BYTE:
        error_report("bbk9588-nand-data page=0x%06x col=0x%03x "
                     "idx=0x%03x byte=%02x pc=0x%08x s0=0x%08x",
                     event->page, event->column, event->index,
                     event->value & 0xffu, pc, s0);
        break;
    }
}

static void bbk9588_create_nand_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_BBK9588_NAND);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);
    DriveInfo *dinfo = drive_get(IF_MTD, 0, 0);

    if (dinfo) {
        qdev_prop_set_drive_err(dev, "drive", blk_by_legacy_dinfo(dinfo),
                                &error_fatal);
    }
    if (board->nand_image) {
        qdev_prop_set_string(dev, "image-path", board->nand_image);
    }
    qdev_prop_set_uint32(dev, "id-code", board->nand_id_code);
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, KSEG1_TO_PHYS(0xb8000000u));
    board->nand_dev = BBK9588_NAND(dev);
    bbk9588_nand_set_event_callback(board->nand_dev, bbk9588_nand_event,
                                    board);
    bbk9588_nand_set_trace_enabled(board->nand_dev,
                                   board->storage_trace_enabled);
}

static void bbk9588_create_emc_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_EMC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, KSEG1_TO_PHYS(0xb3010000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_EMC));
    board->emc = JZ4740_EMC(dev);
    jz4740_emc_attach_nand(board->emc, board->nand_dev);
}


static bool bbk9588_bootrom_nand_page_valid(Bbk9588NandState *nand,
                                            uint64_t page)
{
    const uint8_t *data = bbk9588_nand_raw_data(nand);
    uint64_t size = bbk9588_nand_size(nand);
    uint32_t stride = bbk9588_nand_page_stride(nand);
    uint64_t spare_off = page * stride + BBK9588_NAND_PAGE_SIZE;

    if (!data) {
        return false;
    }
    if (stride < BBK9588_NAND_RAW_STRIDE) {
        return true;
    }
    if (spare_off + 5 > size) {
        return false;
    }

    return data[spare_off + 2] == 0 ||
           data[spare_off + 3] == 0 ||
           data[spare_off + 4] == 0;
}

static bool bbk9588_bootrom_nand_area_has_valid_page(Bbk9588NandState *nand,
                                                     uint32_t nand_addr,
                                                     uint32_t length)
{
    const uint8_t *data = bbk9588_nand_raw_data(nand);
    uint64_t size = bbk9588_nand_size(nand);
    uint32_t stride = bbk9588_nand_page_stride(nand);
    uint64_t first_page;
    uint64_t last_page;

    if (!data || length == 0) {
        return false;
    }

    first_page = nand_addr / BBK9588_NAND_PAGE_SIZE;
    last_page = ((uint64_t)nand_addr + length - 1) /
                BBK9588_NAND_PAGE_SIZE;
    for (uint64_t page = first_page; page <= last_page; page++) {
        uint64_t page_off = page * stride;

        if (page_off + BBK9588_NAND_PAGE_SIZE > size) {
            return false;
        }
        if (bbk9588_bootrom_nand_page_valid(nand, page)) {
            return true;
        }
    }
    return false;
}

static bool bbk9588_bootrom_correct_nand_page(Bbk9588NandState *nand,
                                              uint32_t page,
                                              uint8_t corrected[
                                                  BBK9588_NAND_PAGE_SIZE],
                                              uint32_t *corrected_errors)
{
    const uint8_t *data = bbk9588_nand_raw_data(nand);
    uint64_t size = bbk9588_nand_size(nand);
    uint32_t stride = bbk9588_nand_page_stride(nand);
    uint64_t page_off = (uint64_t)page * stride;
    uint64_t spare_off = page_off + BBK9588_NAND_PAGE_SIZE;
    uint32_t total_errors = 0;

    if (!data || stride < BBK9588_NAND_RAW_STRIDE ||
        spare_off + 6 + 4 * JZ4740_RS_PARITY_BYTES > size) {
        return false;
    }
    memcpy(corrected, data + page_off, BBK9588_NAND_PAGE_SIZE);
    for (unsigned chunk = 0; chunk < 4; chunk++) {
        JZ4740RSCorrection corrections[JZ4740_RS_MAX_ERRORS];
        const uint8_t *parity = data + spare_off + 6 +
                                chunk * JZ4740_RS_PARITY_BYTES;
        uint8_t *block = corrected + chunk * JZ4740_ECC_BLOCK_BYTES;
        int error_count = jz4740_rs_decode(block, parity, corrections);

        if (error_count < 0) {
            warn_report("bbk9588: BootROM NAND page 0x%08x chunk %u has "
                        "uncorrectable RS ECC", page, chunk);
            return false;
        }
        for (int error = 0; error < error_count; error++) {
            jz4740_rs_apply_correction(block, &corrections[error]);
        }
        total_errors += error_count;
    }
    if (corrected_errors) {
        *corrected_errors = total_errors;
    }
    return true;
}

static bool bbk9588_bootrom_copy_nand_data(Bbk9588NandState *nand,
                                           uint32_t nand_addr,
                                           uint32_t load_phys,
                                           uint32_t length,
                                           uint32_t *copied_out)
{
    g_autofree uint8_t *payload = g_malloc(length);
    uint32_t copied = 0;
    uint32_t corrected_errors = 0;

    if (copied_out) {
        *copied_out = 0;
    }

    while (copied < length) {
        uint8_t corrected_page[BBK9588_NAND_PAGE_SIZE];
        uint32_t data_addr = nand_addr + copied;
        uint32_t page = data_addr / BBK9588_NAND_PAGE_SIZE;
        uint32_t column = data_addr % BBK9588_NAND_PAGE_SIZE;
        uint32_t page_copy = MIN(BBK9588_NAND_PAGE_SIZE - column,
                                  length - copied);
        uint32_t page_errors = 0;

        if (!bbk9588_bootrom_nand_page_valid(nand, page)) {
            break;
        }
        if (!bbk9588_bootrom_correct_nand_page(nand, page, corrected_page,
                                               &page_errors)) {
            return false;
        }
        memcpy(payload + copied, corrected_page + column, page_copy);
        corrected_errors += page_errors;
        copied += page_copy;
    }
    if (copied == 0) {
        return false;
    }
    cpu_physical_memory_write(load_phys, payload, copied);
    if (copied_out) {
        *copied_out = copied;
    }
    if (corrected_errors) {
        info_report("bbk9588: BootROM corrected %u NAND RS symbol errors",
                    corrected_errors);
    }
    return true;
}

static bool bbk9588_bootrom_nand_range_erased(Bbk9588NandState *nand,
                                              uint32_t nand_addr,
                                              uint32_t length)
{
    const uint8_t *data = bbk9588_nand_raw_data(nand);
    uint64_t size = bbk9588_nand_size(nand);
    uint32_t stride = bbk9588_nand_page_stride(nand);

    if (!data || length == 0) {
        return true;
    }
    for (uint32_t checked = 0; checked < length; ) {
        uint32_t data_addr = nand_addr + checked;
        uint32_t page = data_addr / BBK9588_NAND_PAGE_SIZE;
        uint32_t column = data_addr % BBK9588_NAND_PAGE_SIZE;
        uint32_t page_check = MIN(BBK9588_NAND_PAGE_SIZE - column,
                                  length - checked);
        uint64_t page_off = (uint64_t)page * stride + column;

        if (page_off + page_check > size) {
            return true;
        }
        for (uint32_t index = 0; index < page_check; index++) {
            if (data[page_off + index] != 0xff) {
                return false;
            }
        }
        checked += page_check;
    }
    return true;
}

static bool bbk9588_bootrom_load_first_stage(Bbk9588MachineState *board,
                                             uint32_t nand_addr,
                                             const char *area)
{
    Bbk9588NandState *nand = board ? board->nand_dev : NULL;
    const uint8_t *data = bbk9588_nand_raw_data(nand);
    uint32_t copied = 0;

    if (!data ||
        !bbk9588_bootrom_nand_area_has_valid_page(nand, nand_addr,
                                                  board->bootrom_size) ||
        bbk9588_bootrom_nand_range_erased(nand, nand_addr,
                                          board->bootrom_size)) {
        return false;
    }
    if (!bbk9588_bootrom_copy_nand_data(nand, nand_addr,
                                        board->firmware_phys,
                                        board->bootrom_size,
                                        &copied)) {
        return false;
    }
    if (board->reset_pc == BBK9588_FIRMWARE_VADDR &&
        board->firmware_phys == 0) {
        board->reset_pc = BBK9588_BOOTROM_ENTRY_VADDR;
    }
    info_report("bbk9588: BootROM loaded %u-byte first-stage from NAND "
                "%s address 0x%08x to phys 0x%08x, reset-pc=0x%08x",
                copied, area, nand_addr, board->firmware_phys,
                board->reset_pc);
    return true;
}

static bool bbk9588_bootrom_load_raw_payload(Bbk9588MachineState *board)
{
    Bbk9588NandState *nand = board ? board->nand_dev : NULL;
    const uint8_t *data = bbk9588_nand_raw_data(nand);
    uint64_t size = bbk9588_nand_size(nand);
    uint32_t stride;
    uint32_t boot_page;
    uint32_t load_phys;
    uint32_t boot_size;

    if (!data || size == 0) {
        return false;
    }

    stride = bbk9588_nand_page_stride(nand);
    boot_page = board->bootrom_nand_page;
    load_phys = board->firmware_phys;
    boot_size = board->bootrom_size;
    if ((uint64_t)boot_page * stride + BBK9588_NAND_PAGE_SIZE > size) {
        error_report("bbk9588: BootROM page 0x%08x is outside NAND image",
                     boot_page);
        exit(1);
    }

    for (uint32_t copied = 0; copied < boot_size; ) {
        uint32_t page_copy = MIN(BBK9588_NAND_PAGE_SIZE, boot_size - copied);
        uint64_t page_off = (uint64_t)boot_page * stride;

        if (page_off + page_copy > size) {
            error_report("bbk9588: BootROM payload exceeds NAND image");
            exit(1);
        }
        cpu_physical_memory_write(load_phys + copied,
                                  data + page_off,
                                  page_copy);
        copied += page_copy;
        boot_page++;
    }

    info_report("bbk9588: diagnostic BootROM copied %u raw bytes from NAND "
                "page 0x%08x to phys 0x%08x, reset-pc=0x%08x",
                boot_size, board->bootrom_nand_page, load_phys,
                board->reset_pc);
    return true;
}

static bool bbk9588_bootrom_load_from_nand(Bbk9588MachineState *board)
{
    Bbk9588NandState *nand = board ? board->nand_dev : NULL;
    const uint8_t *data = bbk9588_nand_raw_data(nand);
    uint64_t size = bbk9588_nand_size(nand);

    if (!board || !board->bootrom_nand_enabled) {
        return false;
    }
    if (!data || size == 0) {
        error_report("bbk9588: BootROM NAND boot requested without a NAND image");
        exit(1);
    }
    if (board->bootrom_nand_page != BBK9588_BOOTROM_NAND_PAGE ||
        board->bootrom_size > BBK9588_BOOTROM_FIRST_STAGE_BYTES) {
        return bbk9588_bootrom_load_raw_payload(board);
    }

    if (bbk9588_bootrom_load_first_stage(board,
                                         board->bootrom_nand_page *
                                         BBK9588_NAND_PAGE_SIZE,
                                         "normal")) {
        return true;
    }
    if (bbk9588_bootrom_load_first_stage(board,
                                         BBK9588_BOOTROM_BACKUP_NAND_ADDR,
                                         "backup")) {
        return true;
    }

    error_report("bbk9588: BootROM could not find a first-stage loader in "
                 "normal NAND address 0x00000000 or backup address 0x%08x",
                 BBK9588_BOOTROM_BACKUP_NAND_ADDR);
    exit(1);
}

static void bbk9588_dmac_irq_handler(void *opaque, int n, int level)
{
    Bbk9588MachineState *board = opaque;

    jz4740_intc_set_irq(board->intc, JZ4740_INTC_IRQ_DMA, level != 0);
    bbk9588_update_irq(board);
}

static void bbk9588_tcu_irq_handler(void *opaque, int n, int level)
{
    Bbk9588MachineState *board = opaque;

    (void)n;
    (void)level;
    bbk9588_sync_tcu_irq_sources(board);
    bbk9588_update_irq(board);
}

static void bbk9588_tcu_event_handler(void *opaque, int n, int level)
{
    Bbk9588MachineState *board = opaque;

    (void)n;
    if (level && board->cpu_irq_output_enabled) {
        bbk9588_wake_cpu(board);
    }
}

static void bbk9588_aic_irq_handler(void *opaque, int n, int level)
{
    Bbk9588MachineState *board = opaque;

    jz4740_intc_set_irq(board->intc, JZ4740_INTC_IRQ_AIC, level != 0);
    bbk9588_update_irq(board);
}

static void bbk9588_touch_sync_latch(Bbk9588MachineState *board)
{
    (void)board;
}

static void bbk9588_touch_diag_record(Bbk9588MachineState *board,
                                      uint32_t reason)
{
    Bbk9588DiagBoardSnapshot snapshot = {
        .intc_last_cp0_status = board->intc_last_cp0_status,
        .intc_last_cp0_cause = board->intc_last_cp0_cause,
    };

    bbk9588_diag_touch_record(board->diag, reason, &snapshot);
}

static void bbk9588_sadc_trace(void *opaque, uint32_t reason)
{
    bbk9588_touch_diag_record(opaque, reason);
}

static void bbk9588_touch_set_state(Bbk9588MachineState *board,
                                    uint16_t raw_x, uint16_t raw_y,
                                    bool down)
{
    jz4740_sadc_set_touch(board->sadc, raw_x, raw_y, down);
    jz4740_gpio_set_input_level(board->gpio, JZ4740_GPIO_PORT_B,
                                0x00040000u, !down, false);
    jz4740_gpio_set_input_level(board->gpio, JZ4740_GPIO_PORT_C,
                                0x08000000u, !down, false);
}

static void bbk9588_gpio_trace(void *opaque, uint32_t reason)
{
    bbk9588_touch_diag_record(opaque, reason);
}

static uint32_t bbk9588_gpio_sample_input(void *opaque, unsigned port,
                                          uint32_t level)
{
    Bbk9588MachineState *board = opaque;

    if (port == JZ4740_GPIO_PORT_C &&
        bbk9588_nand_consume_busy_read(board->nand_dev)) {
        level &= ~0x40000000;
    }
    return level;
}


static void bbk9588_create_aic_device(MachineState *machine,
                                      Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_AIC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    if (machine->audiodev) {
        qdev_prop_set_string(dev, "audiodev", machine->audiodev);
    }
    board->aic_irq = qemu_allocate_irq(bbk9588_aic_irq_handler, board,
                                       JZ4740_AIC_IRQ);
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0020000u));
    sysbus_connect_irq(sbd, JZ4740_AIC_IRQ, board->aic_irq);
    sysbus_connect_irq(sbd, JZ4740_AIC_TX_DMA_REQUEST,
                       qdev_get_gpio_in(DEVICE(board->dmac),
                                        JZ4740_DMAC_REQUEST_AIC_TX));
    sysbus_connect_irq(sbd, JZ4740_AIC_RX_DMA_REQUEST,
                       qdev_get_gpio_in(DEVICE(board->dmac),
                                        JZ4740_DMAC_REQUEST_AIC_RX));
    board->aic = JZ4740_AIC(dev);
}

static uint64_t bbk9588_guest_insn_count(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    return board->cpu ?
           qatomic_read(&board->cpu->env.bbk9588_guest_insn_count) : 0;
}

static void bbk9588_create_diag_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_BBK9588_DIAG);

    qdev_realize(dev, NULL, &error_fatal);
    board->diag = BBK9588_DIAG(dev);
    bbk9588_diag_set_storage_enabled(board->diag,
                                     board->storage_trace_enabled);
    bbk9588_diag_set_graphics_enabled(board->diag, board->graphics_trace);
    bbk9588_diag_set_touch_enabled(board->diag, board->touch_trace);
    bbk9588_diag_set_progress_enabled(board->diag, board->progress_trace);
}

static void bbk9588_connect_diag_sources(Bbk9588MachineState *board)
{
    Bbk9588DiagSources sources = {
        .cpu = CPU(board->cpu),
        .nand = board->nand_dev,
        .panel = board->panel,
        .cpm = board->cpm,
        .dmac = board->dmac,
        .emc = board->emc,
        .gpio = board->gpio,
        .intc = board->intc,
        .lcd = board->lcd,
        .msc = board->msc,
        .sadc = board->sadc,
        .tcu = board->tcu,
    };

    bbk9588_diag_connect_sources(board->diag, &sources);
}

static void bbk9588_create_host_bridge(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_BBK9588_HOST_BRIDGE);

    qdev_realize(dev, NULL, &error_fatal);
    board->host_bridge = BBK9588_HOST_BRIDGE(dev);
    bbk9588_host_bridge_configure(
        board->host_bridge, board->frame_chardev,
        board->lcd_refresh_period_ms, bbk9588_guest_insn_count, board);
}

static void bbk9588_create_host_input(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_BBK9588_HOST_INPUT);

    qdev_realize(dev, NULL, &error_fatal);
    board->host_input = BBK9588_HOST_INPUT(dev);
    bbk9588_host_input_configure(
        board->host_input, board->input_chardev,
        bbk9588_key_apply_host_input, bbk9588_touch_apply_host_input, board);
}

static void bbk9588_create_intc_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_INTC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    board->intc_irq = qemu_allocate_irq(bbk9588_intc_output_handler, board,
                                        0);
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0001000u));
    sysbus_connect_irq(sbd, 0, board->intc_irq);
    board->intc = JZ4740_INTC(dev);
    jz4740_intc_set_refresh(board->intc, bbk9588_intc_refresh, board);
    board->intc_output_level = jz4740_intc_output_level(board->intc);
}

static void bbk9588_create_uart_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_UART);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    if (serial_hd(0)) {
        qdev_prop_set_chr(dev, "chardev", serial_hd(0));
    }
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0030000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_UART0));
    board->uart = JZ4740_UART(dev);
}

static void bbk9588_create_udc_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_UDC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb3040000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_UDC));
    board->udc = JZ4740_UDC(dev);
}

static void bbk9588_create_cim_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_CIM);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb3060000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_CIM));
    board->cim = JZ4740_CIM(dev);
}

static void bbk9588_create_cpm_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_CPM);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0000000u));
    board->cpm = JZ4740_CPM(dev);
    jz4740_cpm_set_update(board->cpm, bbk9588_cpm_update, board);
}

static void bbk9588_create_lcd_device(MachineState *machine,
                                       Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_LCD);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    qdev_prop_set_uint64(dev, "ram-size", machine->ram_size);
    qdev_prop_set_uint32(dev, "frame-bytes", BBK9588_LCD_BYTES);
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb3050000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_LCD));
    board->lcd = JZ4740_LCD(dev);
}

static void bbk9588_create_panel_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_BBK9588_PANEL);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0043000u));
    board->panel = BBK9588_PANEL(dev);
    bbk9588_panel_set_write_callback(
        board->panel, bbk9588_diag_panel_write, board->diag);
}

static void bbk9588_create_sadc_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_SADC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    qdev_prop_set_uint32(dev, "battery-raw", board->sadc_battery_raw);
    qdev_prop_set_uint32(dev, "sadcin-raw", board->sadc_sadcin_raw);
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0070000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_SADC));
    board->sadc = JZ4740_SADC(dev);
    jz4740_sadc_set_trace_callback(board->sadc, bbk9588_sadc_trace, board);
}

static void bbk9588_create_gpio_device(Bbk9588MachineState *board)
{
    static const unsigned irq_source[JZ4740_GPIO_NUM_PORTS] = {
        JZ4740_INTC_IRQ_GPIO0,
        JZ4740_INTC_IRQ_GPIO1,
        JZ4740_INTC_IRQ_GPIO2,
        JZ4740_INTC_IRQ_GPIO3,
    };
    DeviceState *dev = qdev_new(TYPE_JZ4740_GPIO);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    qdev_prop_set_uint32(dev, "input-reset-b", 0x78040000u);
    qdev_prop_set_uint32(dev, "input-reset-c", 0x48000000u);
    qdev_prop_set_uint32(dev, "input-reset-d", 0x20200000u);
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0010000u));
    for (unsigned port = 0; port < JZ4740_GPIO_NUM_PORTS; port++) {
        sysbus_connect_irq(sbd, port,
                           qdev_get_gpio_in(DEVICE(board->intc),
                                            irq_source[port]));
    }
    board->gpio = JZ4740_GPIO(dev);
    jz4740_gpio_set_input_sample_callback(
        board->gpio, bbk9588_gpio_sample_input, board);
    jz4740_gpio_set_trace_callback(board->gpio, bbk9588_gpio_trace, board);
}

static void bbk9588_rtc_power_down(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    if (board->hibernate_poweroff_enabled) {
        info_report("bbk9588: RTC HCR.PD requested guest shutdown");
        qemu_system_shutdown_request(SHUTDOWN_CAUSE_GUEST_SHUTDOWN);
    }
}

static void bbk9588_create_rtc_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_RTC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    qdev_prop_set_bit(dev, "hibernate-wakeup",
                      board->hibernate_wakeup_enabled);
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0003000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_RTC));
    board->rtc = JZ4740_RTC(dev);
    jz4740_rtc_set_power_down_callback(board->rtc,
                                       bbk9588_rtc_power_down, board);
}

static void bbk9588_create_tcu_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_TCU);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    qdev_prop_set_uint32(dev, "period-ms", board->tcu_period_ms);
    for (unsigned output = 0; output < JZ4740_TCU_NUM_OUTPUTS; output++) {
        board->tcu_irq[output] = qemu_allocate_irq(
            output == JZ4740_TCU_EVENT ? bbk9588_tcu_event_handler :
                                        bbk9588_tcu_irq_handler,
            board, output);
    }
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0002000u));
    board->tcu = JZ4740_TCU(dev);
    for (unsigned output = 0; output < JZ4740_TCU_NUM_OUTPUTS; output++) {
        sysbus_connect_irq(sbd, output, board->tcu_irq[output]);
    }
    bbk9588_sync_tcu_irq_sources(board);
}

static void bbk9588_create_dmac_device(MachineState *machine,
                                       Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_DMAC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    qdev_prop_set_uint64(dev, "ram-size", machine->ram_size);
    board->dmac_irq = qemu_allocate_irq(bbk9588_dmac_irq_handler, board, 0);
    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb3020000u));
    sysbus_connect_irq(sbd, 0, board->dmac_irq);
    board->dmac = JZ4740_DMAC(dev);
}

static void bbk9588_create_msc_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_MSC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0021000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_MSC));
    board->msc = JZ4740_MSC(dev);
}

static void bbk9588_create_dma_bridge(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_BBK9588_DMA_BRIDGE);

    qdev_realize(dev, NULL, &error_fatal);
    board->dma_bridge = BBK9588_DMA_BRIDGE(dev);
    bbk9588_dma_bridge_connect(board->dma_bridge, board->dmac, board->msc,
                               board->aic, board->diag);
}

static void bbk9588_cpu_reset(void *opaque)
{
    Bbk9588MachineState *board = opaque;
    MIPSCPU *cpu = board->cpu;
    CPUMIPSState *env = &cpu->env;

    cpu_reset(CPU(cpu));
    env->active_tc.PC = board->reset_pc;
    env->CP0_Status &= ~((1 << CP0St_BEV) | (1 << CP0St_ERL));
    env->CP0_Config7 |= 1 << CP0C7_WII;
    env->bbk9588_heap_next = 0x80960000;
    env->bbk9588_guest_insn_count_enabled = true;
    qatomic_set(&env->bbk9588_guest_insn_count, 0);
    bbk9588_host_bridge_reset_metrics(board->host_bridge);

    bbk9588_diag_reset_input(board->diag);
}

static void bbk9588_load_firmware(MachineState *machine)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(machine);
    const char *image = machine->kernel_filename ?
                        machine->kernel_filename : machine->firmware;
    ssize_t size;

    if (!image) {
        if (bbk9588_bootrom_load_from_nand(board)) {
            return;
        }
        warn_report("bbk9588: no raw firmware supplied; use -kernel C200.bin or bootrom-nand=on");
        return;
    }

    size = load_image_targphys(image, board->firmware_phys,
                               machine->ram_size - board->firmware_phys,
                               NULL);
    if (size < 0) {
        error_report("bbk9588: could not load raw firmware image '%s'", image);
        exit(1);
    }
    info_report("bbk9588: loaded '%s' at phys 0x%08x (%zd bytes)",
                image, board->firmware_phys, size);
}

static char *bbk9588_get_nand_image(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return g_strdup(board->nand_image ? board->nand_image : "");
}

static void bbk9588_set_nand_image(Object *obj, const char *value,
                                   Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    g_free(board->nand_image);
    board->nand_image = value && value[0] ? g_strdup(value) : NULL;
}

static char *bbk9588_get_input_chardev(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return g_strdup(board->input_chardev ? board->input_chardev : "");
}

static void bbk9588_set_input_chardev(Object *obj, const char *value,
                                      Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    g_free(board->input_chardev);
    board->input_chardev = value && value[0] ? g_strdup(value) : NULL;
}

static char *bbk9588_get_frame_chardev(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return g_strdup(board->frame_chardev ? board->frame_chardev : "");
}

static void bbk9588_set_frame_chardev(Object *obj, const char *value,
                                      Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    g_free(board->frame_chardev);
    board->frame_chardev = value && value[0] ? g_strdup(value) : NULL;
}

static bool bbk9588_get_cpu_irq_output(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->cpu_irq_output_enabled;
}

static void bbk9588_set_cpu_irq_output(Object *obj, bool value, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->cpu_irq_output_enabled = value;
    if (board->cpu) {
        bbk9588_drive_cpu_irq(board);
    }
}

static bool bbk9588_get_storage_trace(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->storage_trace_enabled;
}

static void bbk9588_set_storage_trace(Object *obj, bool value,
                                      Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->storage_trace_enabled = value;
    bbk9588_diag_set_storage_enabled(board->diag, value);
    bbk9588_nand_set_trace_enabled(board->nand_dev, value);
}

static bool bbk9588_get_graphics_trace(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->graphics_trace;
}

static void bbk9588_set_graphics_trace(Object *obj, bool value,
                                       Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->graphics_trace = value;
    bbk9588_diag_set_graphics_enabled(board->diag, value);
}

static bool bbk9588_get_touch_trace(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->touch_trace;
}

static void bbk9588_set_touch_trace(Object *obj, bool value,
                                    Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->touch_trace = value;
    bbk9588_diag_set_touch_enabled(board->diag, value);
}

static bool bbk9588_get_progress_trace(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->progress_trace;
}

static void bbk9588_set_progress_trace(Object *obj, bool value,
                                       Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->progress_trace = value;
    bbk9588_diag_set_progress_enabled(board->diag, value);
}

static bool bbk9588_get_bootrom_nand(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->bootrom_nand_enabled;
}

static bool bbk9588_get_hibernate_poweroff(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->hibernate_poweroff_enabled;
}

static void bbk9588_set_hibernate_poweroff(Object *obj, bool value,
                                           Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->hibernate_poweroff_enabled = value;
}

static bool bbk9588_get_hibernate_wakeup(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->hibernate_wakeup_enabled;
}

static void bbk9588_set_hibernate_wakeup(Object *obj, bool value,
                                         Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->hibernate_wakeup_enabled = value;
}

static void bbk9588_set_bootrom_nand(Object *obj, bool value, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->bootrom_nand_enabled = value;
}

static uint32_t *bbk9588_u32_field_ptr(Object *obj, void *opaque)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);
    uintptr_t offset = (uintptr_t)opaque;

    return (uint32_t *)((uint8_t *)board + offset);
}

static void bbk9588_get_u32_field(Object *obj, Visitor *v, const char *name,
                                  void *opaque, Error **errp)
{
    uint32_t value = *bbk9588_u32_field_ptr(obj, opaque);

    visit_type_uint32(v, name, &value, errp);
}

static void bbk9588_set_u32_field(Object *obj, Visitor *v, const char *name,
                                  void *opaque, Error **errp)
{
    uint32_t *field = bbk9588_u32_field_ptr(obj, opaque);
    uint32_t value;

    if (!visit_type_uint32(v, name, &value, errp)) {
        return;
    }
    *field = value;
}

static void bbk9588_add_u32_property(ObjectClass *oc,
                                     const char *name,
                                     size_t offset,
                                     uint32_t default_value,
                                     const char *description)
{
    ObjectProperty *prop;

    prop = object_class_property_add(oc, name, "uint32",
                                     bbk9588_get_u32_field,
                                     bbk9588_set_u32_field,
                                     NULL, (void *)(uintptr_t)offset);
    object_property_set_default_uint(prop, default_value);
    object_class_property_set_description(oc, name, description);
}

static void bbk9588_get_period_ms(Object *obj, Visitor *v, const char *name,
                                  void *opaque, Error **errp)
{
    uint32_t value = *bbk9588_u32_field_ptr(obj, opaque);

    visit_type_uint32(v, name, &value, errp);
}

static void bbk9588_set_period_ms(Object *obj, Visitor *v, const char *name,
                                  void *opaque, Error **errp)
{
    uint32_t *period = bbk9588_u32_field_ptr(obj, opaque);
    uint32_t value;

    if (!visit_type_uint32(v, name, &value, errp)) {
        return;
    }
    *period = value;
}

static void bbk9588_add_period_ms_property(ObjectClass *oc,
                                           const char *name,
                                           size_t offset,
                                           uint32_t default_value,
                                           const char *description)
{
    ObjectProperty *prop;

    prop = object_class_property_add(oc, name, "uint32",
                                     bbk9588_get_period_ms,
                                     bbk9588_set_period_ms,
                                     NULL, (void *)(uintptr_t)offset);
    object_property_set_default_uint(prop, default_value);
    object_class_property_set_description(oc, name, description);
}

static void bbk9588_instance_finalize(Object *obj)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    if (board->dma_bridge) {
        object_unref(OBJECT(board->dma_bridge));
    }
    if (board->aic) {
        object_unref(OBJECT(board->aic));
    }
    if (board->diag) {
        object_unref(OBJECT(board->diag));
    }
    if (board->host_bridge) {
        object_unref(OBJECT(board->host_bridge));
    }
    if (board->host_input) {
        object_unref(OBJECT(board->host_input));
    }
    if (board->aic_irq) {
        qemu_free_irq(board->aic_irq);
    }
    if (board->intc) {
        object_unref(OBJECT(board->intc));
    }
    if (board->emc) {
        object_unref(OBJECT(board->emc));
    }
    if (board->intc_irq) {
        qemu_free_irq(board->intc_irq);
    }
    if (board->cpm) {
        object_unref(OBJECT(board->cpm));
    }
    if (board->cim) {
        object_unref(OBJECT(board->cim));
    }
    if (board->dmac) {
        object_unref(OBJECT(board->dmac));
    }
    if (board->msc) {
        object_unref(OBJECT(board->msc));
    }
    if (board->dmac_irq) {
        qemu_free_irq(board->dmac_irq);
    }
    if (board->gpio) {
        object_unref(OBJECT(board->gpio));
    }
    if (board->lcd) {
        object_unref(OBJECT(board->lcd));
    }
    if (board->panel) {
        object_unref(OBJECT(board->panel));
    }
    if (board->rtc) {
        object_unref(OBJECT(board->rtc));
    }
    if (board->sadc) {
        object_unref(OBJECT(board->sadc));
    }
    if (board->tcu) {
        object_unref(OBJECT(board->tcu));
    }
    if (board->uart) {
        object_unref(OBJECT(board->uart));
    }
    if (board->udc) {
        object_unref(OBJECT(board->udc));
    }
    for (unsigned output = 0; output < JZ4740_TCU_NUM_OUTPUTS; output++) {
        if (board->tcu_irq[output]) {
            qemu_free_irq(board->tcu_irq[output]);
        }
    }
    if (board->nand_dev) {
        object_unref(OBJECT(board->nand_dev));
    }
    g_free(board->input_chardev);
    g_free(board->frame_chardev);
    g_free(board->nand_image);
}

static void bbk9588_instance_init(Object *obj)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->cpu_irq_output_enabled = true;
    board->intc_output_level = false;
    board->storage_trace_enabled = false;
    board->graphics_trace = false;
    board->touch_trace = false;
    board->progress_trace = false;
    board->bootrom_nand_enabled = false;
    board->hibernate_poweroff_enabled = true;
    board->hibernate_wakeup_enabled = false;
    board->nand_id_code = BBK9588_NAND_DEFAULT_ID_CODE;
    board->bootrom_nand_page = BBK9588_BOOTROM_NAND_PAGE;
    board->bootrom_size = BBK9588_BOOTROM_FIRST_STAGE_BYTES;
    board->tcu_period_ms = 10;
    board->progress_trace_period_ms = 0;
    board->lcd_refresh_period_ms = BBK9588_LCD_VBLANK_PERIOD_MS;
}

static void bbk9588_init(MachineState *machine)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(machine);
    MemoryRegion *system_memory = get_system_memory();
    Clock *cpuclk;
    MIPSCPU *cpu;

    if (machine->ram_size < 32 * MiB) {
        error_report("bbk9588: RAM must be at least 32 MiB");
        exit(1);
    }
    memory_region_add_subregion(system_memory, 0, machine->ram);

    cpuclk = clock_new(OBJECT(machine), "cpu-refclk");
    clock_set_hz(cpuclk, 336000000);

    cpu = mips_cpu_create_with_clock(machine->cpu_type, cpuclk,
                                     TARGET_BIG_ENDIAN);
    cpu->env.bbk9588_storage_trace = board->storage_trace_enabled;
    cpu->env.bbk9588_wait_nop = false;
    cpu->env.bbk9588_guest_insn_count_enabled = true;
    qatomic_set(&cpu->env.bbk9588_guest_insn_count, 0);
    /*
     * C200 enters WAIT through short helpers that temporarily keep CP0 IE
     * clear while board interrupts are already pending.  JZ47xx-class MIPS
     * cores wake from WAIT in that state and then let firmware restore its
     * interrupt mask; model that with Config7.WII instead of skipping WAIT.
     */
    cpu->env.CP0_Config7 |= 1 << CP0C7_WII;
    cpu->env.bbk9588_heap_next = 0x80960000;
    cpu_mips_irq_init_cpu(cpu);
    cpu_mips_clock_init(cpu);
    board->cpu = cpu;
    qemu_register_reset(bbk9588_cpu_reset, board);
    board->cpu_irq = cpu->env.irq[2];
    board->intc_resample_timer = timer_new_ms(QEMU_CLOCK_REALTIME,
                                              bbk9588_intc_resample_timer_cb,
                                              board);
    board->progress_trace_timer = timer_new_ms(
        QEMU_CLOCK_REALTIME, bbk9588_progress_trace_timer_cb, board);

    bbk9588_create_diag_device(board);
    bbk9588_create_host_input(board);
    bbk9588_create_host_bridge(board);
    bbk9588_create_intc_device(board);
    bbk9588_create_uart_device(board);
    bbk9588_create_udc_device(board);
    bbk9588_create_cim_device(board);
    bbk9588_create_sadc_device(board);
    bbk9588_create_gpio_device(board);
    bbk9588_create_rtc_device(board);
    bbk9588_create_lcd_device(machine, board);
    bbk9588_create_panel_device(board);
    bbk9588_create_tcu_device(board);
    bbk9588_create_cpm_device(board);
    bbk9588_create_dmac_device(machine, board);
    bbk9588_create_msc_device(board);
    bbk9588_create_nand_device(board);
    bbk9588_create_emc_device(board);
    bbk9588_connect_diag_sources(board);
    bbk9588_touch_sync_latch(board);
    bbk9588_touch_diag_record(board, 7u);
    bbk9588_create_aic_device(machine, board);
    bbk9588_create_dma_bridge(board);
    bbk9588_host_bridge_connect_display(board->host_bridge, board->lcd,
                                        board->panel);
    bbk9588_host_bridge_connect_audio(board->host_bridge, board->aic,
                                      board->dmac);

    bbk9588_load_firmware(machine);
    bbk9588_progress_trace_schedule(board);
    bbk9588_host_bridge_start(board->host_bridge);
}

static void bbk9588_machine_class_init(ObjectClass *oc, const void *data)
{
    MachineClass *mc = MACHINE_CLASS(oc);

    object_class_property_add_str(oc, "nand-image",
                                  bbk9588_get_nand_image,
                                  bbk9588_set_nand_image);
    object_class_property_set_description(oc, "nand-image",
                                          "Combined BBK9588 NAND image backing file");
    object_class_property_add_str(oc, "input-chardev",
                                  bbk9588_get_input_chardev,
                                  bbk9588_set_input_chardev);
    object_class_property_set_description(oc, "input-chardev",
                                          "Chardev id for BBK9588 host key/touch input events");
    object_class_property_add_str(oc, "frame-chardev",
                                  bbk9588_get_frame_chardev,
                                  bbk9588_set_frame_chardev);
    object_class_property_set_description(oc, "frame-chardev",
                                          "Chardev id for BBK9588 RGB565 framebuffer output");
    object_class_property_add_bool(oc, "cpu-irq-output",
                                   bbk9588_get_cpu_irq_output,
                                   bbk9588_set_cpu_irq_output);
    object_class_property_set_description(oc, "cpu-irq-output",
                                          "Enable BBK INTC output to MIPS CPU IP2");
    object_class_property_add_bool(oc, "storage-trace",
                                   bbk9588_get_storage_trace,
                                   bbk9588_set_storage_trace);
    object_class_property_set_description(oc, "storage-trace",
                                          "Trace bbk9588 NAND/MSC page and DMA diagnostics");
    object_class_property_add_bool(oc, "graphics-trace",
                                   bbk9588_get_graphics_trace,
                                   bbk9588_set_graphics_trace);
    object_class_property_set_description(oc, "graphics-trace",
                                          "Print bbk9588 graphics MMIO writes to stderr");
    object_class_property_add_bool(oc, "touch-trace",
                                   bbk9588_get_touch_trace,
                                   bbk9588_set_touch_trace);
    object_class_property_set_description(oc, "touch-trace",
                                          "Mirror SADC/GPIO/INTC touch diagnostics into guest diagnostic RAM");
    object_class_property_add_bool(oc, "progress-trace",
                                   bbk9588_get_progress_trace,
                                   bbk9588_set_progress_trace);
    object_class_property_set_description(oc, "progress-trace",
                                          "Trace bbk9588 CPU/IRQ/runtime progress into diagnostic guest RAM");
    object_class_property_add_bool(oc, "bootrom-nand",
                                   bbk9588_get_bootrom_nand,
                                   bbk9588_set_bootrom_nand);
    object_class_property_set_description(oc, "bootrom-nand",
                                          "Load the boot image from the NAND BootROM area when -kernel is absent");
    object_class_property_add_bool(oc, "hibernate-poweroff",
                                   bbk9588_get_hibernate_poweroff,
                                   bbk9588_set_hibernate_poweroff);
    object_class_property_set_description(
        oc, "hibernate-poweroff",
        "Exit QEMU when the guest asserts RTC HCR.PD");
    object_class_property_add_bool(oc, "hibernate-wakeup",
                                   bbk9588_get_hibernate_wakeup,
                                   bbk9588_set_hibernate_wakeup);
    object_class_property_set_description(
        oc, "hibernate-wakeup",
        "Start after a PD29 wakeup-pin hibernate reset instead of RTC power-on reset");
    bbk9588_add_u32_property(
        oc, "sadc-battery-raw",
        offsetof(Bbk9588MachineState, sadc_battery_raw),
        JZ4740_SADC_DEFAULT_BATTERY_RAW,
        "12-bit SADC PBAT sample value latched into ADBDAT when ADENA.PBATEN is written");
    bbk9588_add_u32_property(
        oc, "sadc-sadcin-raw",
        offsetof(Bbk9588MachineState, sadc_sadcin_raw),
        JZ4740_SADC_DEFAULT_SADCIN_RAW,
        "12-bit SADCIN sample value latched into ADSDAT when ADENA.SADCINEN is written");
    bbk9588_add_u32_property(
        oc, "nand-id-code",
        offsetof(Bbk9588MachineState, nand_id_code),
        BBK9588_NAND_DEFAULT_ID_CODE,
        "NAND READ ID device code returned as byte 1; 0xdc is 512 MiB, 0xda is 256 MiB");
    bbk9588_add_u32_property(
        oc, "firmware-phys",
        offsetof(Bbk9588MachineState, firmware_phys), BBK9588_FIRMWARE_PHYS,
        "Physical address where the raw -kernel firmware image is loaded");
    bbk9588_add_u32_property(
        oc, "reset-pc",
        offsetof(Bbk9588MachineState, reset_pc), BBK9588_FIRMWARE_VADDR,
        "Virtual PC used when resetting the BBK9588 CPU");
    bbk9588_add_u32_property(
        oc, "bootrom-page",
        offsetof(Bbk9588MachineState, bootrom_nand_page),
        BBK9588_BOOTROM_NAND_PAGE,
        "Raw NAND page containing the BootROM first-stage loader; nonzero pages use diagnostic raw-payload copy mode");
    bbk9588_add_u32_property(
        oc, "bootrom-size",
        offsetof(Bbk9588MachineState, bootrom_size),
        BBK9588_BOOTROM_FIRST_STAGE_BYTES,
        "BootROM first-stage bytes to copy from NAND; sizes over 8 KiB use diagnostic raw-payload copy mode");
    bbk9588_add_period_ms_property(
        oc, "tcu-period-ms",
        offsetof(Bbk9588MachineState, tcu_period_ms), 10,
        "Diagnostic/performance TCU sampling period in milliseconds; hardware correctness must not depend on changing it");
    bbk9588_add_period_ms_property(
        oc, "progress-trace-period-ms",
        offsetof(Bbk9588MachineState, progress_trace_period_ms), 0,
        "Diagnostic progress-sampling timer period in milliseconds; 0 disables it");
    bbk9588_add_period_ms_property(
        oc, "lcd-refresh-period-ms",
        offsetof(Bbk9588MachineState, lcd_refresh_period_ms), 250,
        "RGB565 panel scanout/frame chardev refresh period in milliseconds");

    mc->desc = "BBK 9588 handheld learning computer";
    mc->init = bbk9588_init;
    mc->default_cpu_type = MIPS_CPU_TYPE_NAME("24Kf");
    mc->default_ram_size = BBK9588_RAM_DEFAULT_SIZE;
    mc->default_ram_id = "bbk9588.ram";
    mc->max_cpus = 1;
    mc->no_parallel = true;
    mc->no_floppy = true;
    mc->no_cdrom = true;
    machine_add_audiodev_property(mc);
}

static const TypeInfo bbk9588_machine_typeinfo = {
    .name = TYPE_BBK9588_MACHINE,
    .parent = TYPE_MACHINE,
    .class_init = bbk9588_machine_class_init,
    .instance_init = bbk9588_instance_init,
    .instance_finalize = bbk9588_instance_finalize,
    .instance_size = sizeof(Bbk9588MachineState),
};

static void bbk9588_machine_register_types(void)
{
    type_register_static(&bbk9588_machine_typeinfo);
}

type_init(bbk9588_machine_register_types);
