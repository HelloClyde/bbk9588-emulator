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
#include "hw/dma/jz4740_dmac.h"
#include "hw/gpio/jz4740_gpio.h"
#include "hw/input/bbk9588_host_input.h"
#include "hw/input/jz4740_sadc.h"
#include "hw/intc/jz4740_intc.h"
#include "hw/mem/jz4740_ecc.h"
#include "hw/mem/jz4740_emc.h"
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
#define BBK9588_LCD_STRIDE         (BBK9588_LCD_WIDTH * 2)
#define BBK9588_LCD_BYTES          (BBK9588_LCD_STRIDE * BBK9588_LCD_HEIGHT)
#define BBK9588_LCD_VBLANK_PERIOD_MS 33
#define BBK9588_GUI_EVENT_OBJ_OFF  0xf0u
#define BBK9588_AIC_DATA_PHYS      0x10020034u
#define BBK9588_DIAG_VA            0x89f00000u
#define BBK9588_EVENT_SCRATCH_VA   (BBK9588_DIAG_VA + 0x0000u)
#define BBK9588_EVENT_SCRATCH_MAGIC 0x45564b42u
#define BBK9588_EVENT_QUEUE_VA     (BBK9588_DIAG_VA + 0x0040u)
#define BBK9588_EVENT_QUEUE_MAGIC  0x514b4242u
#define BBK9588_EVENT_QUEUE_SLOTS  8u
#define BBK9588_EVENT_QUEUE_WORDS  5u
#define BBK9588_EVENT_QUEUE_HEADER_WORDS 4u
#define BBK9588_EVENT_CODE_INPUT   3u
#define BBK9588_EVENT_KIND_KEY     1u
#define BBK9588_EVENT_KIND_TOUCH   2u
#define BBK9588_TOUCH_TRACE_VA     (BBK9588_DIAG_VA + 0x0100u)
#define BBK9588_TOUCH_TRACE_MAGIC  0x54434b42u
#define BBK9588_NAND_READ_SOURCE_RUNTIME 1u
#define BBK9588_NAND_READ_REQUEST_BLANK 8u
#define BBK9588_NAND_READ_FINAL_BLANK 16u
#define BBK9588_STORAGE_TRACE_VA   (BBK9588_DIAG_VA + 0x2000u)
#define BBK9588_STORAGE_TRACE_MAGIC 0x53544b42u
#define BBK9588_STORAGE_TRACE_SLOTS 4096u
#define BBK9588_STORAGE_TRACE_WORDS 4u
#define BBK9588_STORAGE_TRACE_HEADER_WORDS 4u
#define BBK9588_STORAGE_TRACE_NAND_READ 0x80000000u
#define BBK9588_STORAGE_TRACE_NAND_PROGRAM 0x20000000u
#define BBK9588_STORAGE_TRACE_NAND_ERASE 0x10000000u
#define BBK9588_STORAGE_TRACE_DMAC_TRANSFER 0x08000000u
#define BBK9588_STORAGE_TRACE_NAND_DETAIL 0x04000000u
#define BBK9588_MSC_TRACE_VA       (BBK9588_DIAG_VA + 0x1000u)
#define BBK9588_MSC_TRACE_MAGIC    0x4d534b42u
#define BBK9588_MSC_TRACE_SLOTS    113u
#define BBK9588_MSC_TRACE_WORDS    9u
#define BBK9588_MSC_TRACE_HEADER_WORDS 4u
#define BBK9588_MSC_TRACE_READ     1u
#define BBK9588_MSC_TRACE_WRITE    2u
#define BBK9588_MSC_TRACE_CMD      3u
#define BBK9588_CLUSTER_TRACE_DETAIL_VA (BBK9588_DIAG_VA + 0x0420u)
#define BBK9588_PROGRESS_TRACE_VA  (BBK9588_DIAG_VA + 0x0500u)
#define BBK9588_PROGRESS_TRACE_MAGIC 0x50544b42u
#define BBK9588_PROGRESS_TRACE_SLOTS 8u
#define BBK9588_PROGRESS_TRACE_WORDS 12u
#define BBK9588_PROGRESS_TRACE_HEADER_WORDS 4u
#define BBK9588_DMAC_TRACE_VA      (BBK9588_DIAG_VA + 0x0300u)
#define BBK9588_DMAC_TRACE_MAGIC   0x444d4b42u
#define BBK9588_DMAC_TRACE_WORDS   16u
#define BBK9588_NAND_TARGET_TRACE_VA (BBK9588_DIAG_VA + 0x0600u)
#define BBK9588_NAND_TARGET_TRACE_MAGIC 0x4e544b42u
#define BBK9588_NAND_TARGET_TRACE_SLOTS 8u
#define BBK9588_NAND_TARGET_TRACE_WORDS 6u
#define BBK9588_NAND_TARGET_BLOCK 0x2540u
#define BBK9588_NAND_TARGET_PAGE 0x256au
#define BBK9588_NAND_TARGET_EVENT_ERASE 1u
#define BBK9588_NAND_TARGET_EVENT_PROGRAM 2u
#define BBK9588_SYSCTRL_WAKE_PROXY_IRQ JZ4740_INTC_IRQ_TCU1

/*
 * C200 firmware accesses SoC devices through KSEG1 uncached addresses such as
 * 0xb0001000. QEMU MemoryRegions are mapped in physical address space, so the
 * board exposes those windows at kseg1 & 0x1fffffff.
 */
#define KSEG1_TO_PHYS(addr)        ((addr) & 0x1fffffff)

typedef struct Bbk9588MachineState Bbk9588MachineState;

static void bbk9588_storage_trace_record(uint32_t logical,
                                         uint32_t absolute,
                                         uint32_t first_word);
static void bbk9588_nand_target_trace_record(uint32_t event,
                                             uint32_t a,
                                             uint32_t b,
                                             uint32_t c);
static void bbk9588_dmac_trace_sample(void *opaque, uint32_t event,
                                      unsigned channel, hwaddr offset,
                                      uint32_t value);
static uint32_t bbk9588_ldl_le(const uint8_t *p);

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
    Bbk9588HostBridgeState *host_bridge;
    Bbk9588HostInputState *host_input;
    JZ4740LCDState *lcd;
    Bbk9588PanelState *panel;
    JZ4740INTCState *intc;
    JZ4740EMCState *emc;
    JZ4740CIMState *cim;
    JZ4740CPMState *cpm;
    JZ4740DMACState *dmac;
    JZ4740MSCState *msc;
    JZ4740GPIOState *gpio;
    JZ4740RTCState *rtc;
    JZ4740SADCState *sadc;
    JZ4740TCUState *tcu;
    JZ4740UARTState *uart;
    JZ4740UDCState *udc;
    uint32_t extgpio_wake_enable_80;
    bool sysctrl_wake_pending;
    bool gpio300_wake_pulse_available;
    uint32_t sysctrl_wake_count;
    Bbk9588NandState *nand_dev;
    bool storage_trace_enabled;
    bool graphics_trace_enabled;
    bool touch_trace_enabled;
    uint32_t graphics_trace_count;
    uint32_t storage_trace_seq;
    uint32_t msc_trace_seq;
    uint32_t nand_target_trace_seq;
    bool progress_trace_enabled;
    uint32_t progress_trace_seq;
    uint32_t dmac_trace_seq;
    uint32_t dmac_last_event;
    uint32_t dmac_last_channel;
    uint32_t dmac_last_offset;
    uint32_t dmac_last_value;
    uint32_t input_event_read_idx;
    uint32_t input_event_write_idx;
    uint32_t input_event_count;
    uint32_t input_event_words[BBK9588_EVENT_QUEUE_SLOTS]
                              [BBK9588_EVENT_QUEUE_WORDS];
    uint32_t nand_ready_raise_count;
    char *input_chardev;
    char *frame_chardev;
    char *nand_image;
    bool bootrom_nand_enabled;
    uint32_t nand_id_code;
    uint32_t firmware_phys;
    uint32_t reset_pc;
    uint32_t bootrom_nand_page;
    uint32_t bootrom_size;
};

#define TYPE_BBK9588_MACHINE MACHINE_TYPE_NAME("bbk9588")
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588MachineState, BBK9588_MACHINE)

static Bbk9588MachineState *bbk9588_active_board;

static void bbk9588_touch_set_state(Bbk9588MachineState *board,
                                     uint16_t raw_x, uint16_t raw_y,
                                     bool down);
static void bbk9588_queue_input_event(Bbk9588MachineState *board,
                                      uint32_t kind, uint32_t arg0,
                                      uint32_t arg1, uint32_t arg2);
static void bbk9588_key_apply_host_input(void *opaque, uint32_t key_code,
                                         bool down);
static void bbk9588_update_irq(Bbk9588MachineState *board);
static void bbk9588_touch_trace_update(Bbk9588MachineState *board,
                                       uint32_t reason);
static void bbk9588_progress_trace_sample(Bbk9588MachineState *board,
                                          uint32_t reason);
static void bbk9588_phys_write_le32(hwaddr addr, uint32_t value);

static void bbk9588_phys_read(hwaddr addr, void *buf, hwaddr len)
{
    cpu_physical_memory_read(BBK9588_KSEG_TO_PHYS(addr), buf, len);
}

static uint32_t bbk9588_phys_read_le32(hwaddr addr)
{
    uint8_t buf[4];

    bbk9588_phys_read(addr, buf, sizeof(buf));
    return buf[0] | (buf[1] << 8) | (buf[2] << 16) | (buf[3] << 24);
}

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
        jz4740_tcu_irq_level(board->tcu, JZ4740_TCU_IRQ_TCU1) ||
        board->sysctrl_wake_pending);
    jz4740_intc_set_irq(
        board->intc, JZ4740_INTC_IRQ_TCU2,
        jz4740_tcu_irq_level(board->tcu, JZ4740_TCU_IRQ_TCU2));
}

static void bbk9588_sync_level_irq_sources(Bbk9588MachineState *board)
{
    bbk9588_sync_tcu_irq_sources(board);
}

static bool bbk9588_sysctrl_wake_enabled(Bbk9588MachineState *board)
{
    return jz4740_cpm_wake_enabled(board->cpm) ||
           board->extgpio_wake_enable_80 != 0;
}

static void bbk9588_sysctrl_sync_wake(Bbk9588MachineState *board)
{
    if (!bbk9588_sysctrl_wake_enabled(board)) {
        board->sysctrl_wake_pending = false;
    }
}

static void bbk9588_drive_cpu_irq(Bbk9588MachineState *board)
{
    bool level = board->intc_output_level || board->sysctrl_wake_pending;
    bool ip2_level;

    ip2_level = level && board->cpu_irq_output_enabled;

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
    bbk9588_touch_trace_update(board, 0x51);
    if (board->intc_resample_timer) {
        if (level && board->cpu_irq_output_enabled) {
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

    bbk9588_sysctrl_sync_wake(board);
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
    board->nand_ready_raise_count++;
    jz4740_gpio_raise_flag(board->gpio, JZ4740_GPIO_PORT_C, 0x40000000u);
    bbk9588_touch_trace_update(board, 0x52);
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

static bool bbk9588_guest_ram_va_valid(uint32_t va, uint32_t size)
{
    uint32_t phys = BBK9588_KSEG_TO_PHYS(va);

    return (va & 0xe0000000u) == 0x80000000u &&
           size <= BBK9588_RAM_SIZE &&
           phys <= BBK9588_RAM_SIZE - size;
}

static void bbk9588_progress_trace_sample(Bbk9588MachineState *board,
                                          uint32_t reason)
{
    JZ4740TCUDiagnostics tcu_diag;
    uint32_t base = BBK9588_PROGRESS_TRACE_VA;
    uint32_t total_size = (BBK9588_PROGRESS_TRACE_HEADER_WORDS +
                           BBK9588_PROGRESS_TRACE_SLOTS *
                           BBK9588_PROGRESS_TRACE_WORDS) * 4;
    uint32_t seq;
    uint32_t slot;
    uint32_t entry;
    uint32_t pc = 0;
    uint32_t cause = 0;
    uint32_t status = 0;

    if (!board || !board->progress_trace_enabled ||
        !bbk9588_guest_ram_va_valid(base, total_size)) {
        return;
    }

    if (board->cpu) {
        CPUMIPSState *env = &board->cpu->env;

        pc = env->active_tc.PC & 0xffffffffu;
        cause = env->CP0_Cause;
        status = env->CP0_Status;
    }
    jz4740_tcu_get_diagnostics(board->tcu, &tcu_diag);

    seq = ++board->progress_trace_seq;
    slot = (seq - 1) % BBK9588_PROGRESS_TRACE_SLOTS;
    entry = base + (BBK9588_PROGRESS_TRACE_HEADER_WORDS +
                    slot * BBK9588_PROGRESS_TRACE_WORDS) * 4;

    bbk9588_phys_write_le32(base + 0x00, BBK9588_PROGRESS_TRACE_MAGIC);
    bbk9588_phys_write_le32(base + 0x04, seq);
    bbk9588_phys_write_le32(base + 0x08, slot);
    bbk9588_phys_write_le32(base + 0x0c, BBK9588_PROGRESS_TRACE_SLOTS);

    bbk9588_phys_write_le32(entry + 0x00, seq);
    bbk9588_phys_write_le32(entry + 0x04, reason);
    bbk9588_phys_write_le32(entry + 0x08, pc);
    bbk9588_phys_write_le32(entry + 0x0c,
                            jz4740_intc_pending(board->intc));
    bbk9588_phys_write_le32(entry + 0x10,
                            jz4740_intc_mask(board->intc));
    bbk9588_phys_write_le32(entry + 0x14, tcu_diag.pending_mask);
    bbk9588_phys_write_le32(entry + 0x18, bbk9588_phys_read_le32(0x804bf440));
    bbk9588_phys_write_le32(entry + 0x1c, bbk9588_phys_read_le32(0x804bf444));
    bbk9588_phys_write_le32(entry + 0x20, bbk9588_phys_read_le32(0x80473f08));
    bbk9588_phys_write_le32(entry + 0x24, bbk9588_phys_read_le32(0x80473f38));
    bbk9588_phys_write_le32(entry + 0x28, cause);
    bbk9588_phys_write_le32(entry + 0x2c, status);
}

static void bbk9588_event_queue_mirror_header(Bbk9588MachineState *board)
{
    uint32_t queue = BBK9588_EVENT_QUEUE_VA;
    uint32_t total_size = (BBK9588_EVENT_QUEUE_HEADER_WORDS +
                           BBK9588_EVENT_QUEUE_SLOTS *
                           BBK9588_EVENT_QUEUE_WORDS) * 4;

    if (!bbk9588_guest_ram_va_valid(queue, total_size)) {
        return;
    }

    bbk9588_phys_write_le32(queue + 0x00, BBK9588_EVENT_QUEUE_MAGIC);
    bbk9588_phys_write_le32(queue + 0x04, board->input_event_read_idx);
    bbk9588_phys_write_le32(queue + 0x08, board->input_event_write_idx);
    bbk9588_phys_write_le32(queue + 0x0c, board->input_event_count);
}

static void bbk9588_event_queue_mirror_slot(Bbk9588MachineState *board,
                                            uint32_t slot)
{
    uint32_t queue = BBK9588_EVENT_QUEUE_VA;
    uint32_t total_size = (BBK9588_EVENT_QUEUE_HEADER_WORDS +
                           BBK9588_EVENT_QUEUE_SLOTS *
                           BBK9588_EVENT_QUEUE_WORDS) * 4;
    uint32_t base;

    if (slot >= BBK9588_EVENT_QUEUE_SLOTS ||
        !bbk9588_guest_ram_va_valid(queue, total_size)) {
        return;
    }

    base = queue + BBK9588_EVENT_QUEUE_HEADER_WORDS * 4 +
           slot * BBK9588_EVENT_QUEUE_WORDS * 4;
    for (uint32_t word = 0; word < BBK9588_EVENT_QUEUE_WORDS; word++) {
        bbk9588_phys_write_le32(base + word * 4,
                                board->input_event_words[slot][word]);
    }
}

static void bbk9588_event_queue_mirror_all(Bbk9588MachineState *board)
{
    bbk9588_event_queue_mirror_header(board);
    for (uint32_t slot = 0; slot < BBK9588_EVENT_QUEUE_SLOTS; slot++) {
        bbk9588_event_queue_mirror_slot(board, slot);
    }
}

static void bbk9588_queue_input_event(Bbk9588MachineState *board,
                                      uint32_t kind, uint32_t arg0,
                                      uint32_t arg1, uint32_t arg2)
{
    uint32_t read_idx;
    uint32_t write_idx;
    uint32_t count;

    if (!board) {
        return;
    }

    read_idx = board->input_event_read_idx;
    write_idx = board->input_event_write_idx;
    count = board->input_event_count;
    if (read_idx >= BBK9588_EVENT_QUEUE_SLOTS ||
        write_idx >= BBK9588_EVENT_QUEUE_SLOTS ||
        count > BBK9588_EVENT_QUEUE_SLOTS) {
        read_idx = 0;
        write_idx = 0;
        count = 0;
    }

    if (count == BBK9588_EVENT_QUEUE_SLOTS) {
        read_idx = (read_idx + 1) % BBK9588_EVENT_QUEUE_SLOTS;
        count--;
    }

    board->input_event_words[write_idx][0] = BBK9588_EVENT_CODE_INPUT;
    board->input_event_words[write_idx][1] = kind;
    board->input_event_words[write_idx][2] = arg0;
    board->input_event_words[write_idx][3] = arg1;
    board->input_event_words[write_idx][4] = arg2;
    write_idx = (write_idx + 1) % BBK9588_EVENT_QUEUE_SLOTS;
    count++;
    board->input_event_read_idx = read_idx;
    board->input_event_write_idx = write_idx;
    board->input_event_count = count;
    bbk9588_event_queue_mirror_all(board);

    bbk9588_phys_write_le32(BBK9588_EVENT_SCRATCH_VA + 0x18,
                            BBK9588_EVENT_SCRATCH_MAGIC);
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
        bbk9588_queue_input_event(board, BBK9588_EVENT_KIND_KEY,
                                  key_code & 0xff, down ? 1 : 0, 0);
    }
}

static void bbk9588_progress_trace_timer_cb(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    bbk9588_progress_trace_sample(board, 2);

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
        bbk9588_storage_trace_record(
            BBK9588_STORAGE_TRACE_NAND_READ | event->page,
            ((event->flags & 0xffu) << 24) |
            (event->page & 0x00ffffffu),
            event->value);
        break;
    case BBK9588_NAND_EVENT_READ_DETAIL:
        bbk9588_storage_trace_record(
            BBK9588_STORAGE_TRACE_NAND_READ |
            BBK9588_STORAGE_TRACE_NAND_DETAIL |
            (event->page & 0x03ffffffu),
            event->column, pc);
        break;
    case BBK9588_NAND_EVENT_PROGRAM:
        if (event->failed && board->storage_trace_enabled) {
            bbk9588_storage_trace_record(
                BBK9588_STORAGE_TRACE_NAND_PROGRAM | event->page,
                event->column, 0xffffffffu);
        }
        if (board->storage_trace_enabled &&
            !event->failed && event->column < BBK9588_NAND_PAGE_SIZE) {
            bbk9588_storage_trace_record(
                BBK9588_STORAGE_TRACE_NAND_PROGRAM | event->page,
                event->column, event->value);
        }
        if (!event->failed &&
            event->page == BBK9588_NAND_TARGET_PAGE &&
            event->column < BBK9588_NAND_PAGE_SIZE) {
            bbk9588_nand_target_trace_record(
                BBK9588_NAND_TARGET_EVENT_PROGRAM, event->page,
                event->column, event->value);
        }
        bbk9588_nand_raise_ready(board);
        break;
    case BBK9588_NAND_EVENT_ERASE:
        if (board->storage_trace_enabled) {
            bbk9588_storage_trace_record(
                BBK9588_STORAGE_TRACE_NAND_ERASE | event->flags,
                event->count,
                event->failed ? 0u : event->value);
        }
        if (!event->failed &&
            event->flags == BBK9588_NAND_TARGET_BLOCK) {
            bbk9588_nand_target_trace_record(
                BBK9588_NAND_TARGET_EVENT_ERASE, event->flags,
                event->page, event->count);
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

static void bbk9588_emc_board_write(void *opaque, hwaddr offset,
                                    uint32_t value)
{
    Bbk9588MachineState *board = opaque;

    if (!board || offset != 0x80u) {
        return;
    }
    board->extgpio_wake_enable_80 = value & 0x00040000u;
    bbk9588_sysctrl_sync_wake(board);
    bbk9588_update_irq(board);
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
    jz4740_emc_set_board_write_callback(
        board->emc, bbk9588_emc_board_write, board);
}


static void bbk9588_phys_write(hwaddr addr, const void *buf, hwaddr len)
{
    cpu_physical_memory_write(BBK9588_KSEG_TO_PHYS(addr), buf, len);
}

static void bbk9588_phys_write_le32(hwaddr addr, uint32_t value)
{
    uint8_t buf[4] = {
        value & 0xff,
        (value >> 8) & 0xff,
        (value >> 16) & 0xff,
        (value >> 24) & 0xff,
    };

    bbk9588_phys_write(addr, buf, sizeof(buf));
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

static uint32_t bbk9588_ldl_le(const uint8_t *p)
{
    return p[0] | (p[1] << 8) | (p[2] << 16) | (p[3] << 24);
}

static void bbk9588_storage_trace_record(uint32_t logical,
                                         uint32_t absolute,
                                         uint32_t first_word)
{
    Bbk9588MachineState *board = bbk9588_active_board;
    uint32_t seq;
    uint32_t slot;
    uint32_t base;

    if (!board || !board->storage_trace_enabled) {
        return;
    }

    seq = ++board->storage_trace_seq;
    slot = (seq - 1) % BBK9588_STORAGE_TRACE_SLOTS;
    base = BBK9588_STORAGE_TRACE_VA +
           BBK9588_STORAGE_TRACE_HEADER_WORDS * 4u +
           slot * BBK9588_STORAGE_TRACE_WORDS * 4u;
    bbk9588_phys_write_le32(BBK9588_STORAGE_TRACE_VA,
                            BBK9588_STORAGE_TRACE_MAGIC);
    bbk9588_phys_write_le32(BBK9588_STORAGE_TRACE_VA + 4, seq);
    bbk9588_phys_write_le32(BBK9588_STORAGE_TRACE_VA + 8, slot);
    bbk9588_phys_write_le32(BBK9588_STORAGE_TRACE_VA + 12,
                            BBK9588_STORAGE_TRACE_SLOTS);
    bbk9588_phys_write_le32(base + 0, seq);
    bbk9588_phys_write_le32(base + 4, logical);
    bbk9588_phys_write_le32(base + 8, absolute);
    bbk9588_phys_write_le32(base + 12, first_word);
}

static void bbk9588_msc_trace_record(uint32_t event,
                                     uint32_t lba,
                                     uint32_t dma_phys,
                                     uint32_t bytes,
                                     uint32_t cmd,
                                     uint32_t arg,
                                     uint32_t first_word)
{
    Bbk9588MachineState *board = bbk9588_active_board;
    uint32_t seq;
    uint32_t slot;
    uint32_t base;
    uint32_t pc = 0;

    if (!board || !board->storage_trace_enabled) {
        return;
    }
    if (board->cpu) {
        pc = board->cpu->env.active_tc.PC & 0xffffffffu;
    }

    seq = ++board->msc_trace_seq;
    slot = (seq - 1) % BBK9588_MSC_TRACE_SLOTS;
    base = BBK9588_MSC_TRACE_VA +
           BBK9588_MSC_TRACE_HEADER_WORDS * 4u +
           slot * BBK9588_MSC_TRACE_WORDS * 4u;
    bbk9588_phys_write_le32(BBK9588_MSC_TRACE_VA,
                            BBK9588_MSC_TRACE_MAGIC);
    bbk9588_phys_write_le32(BBK9588_MSC_TRACE_VA + 4, seq);
    bbk9588_phys_write_le32(BBK9588_MSC_TRACE_VA + 8, slot);
    bbk9588_phys_write_le32(BBK9588_MSC_TRACE_VA + 12,
                            BBK9588_MSC_TRACE_SLOTS);
    bbk9588_phys_write_le32(base + 0, seq);
    bbk9588_phys_write_le32(base + 4, event);
    bbk9588_phys_write_le32(base + 8, lba);
    bbk9588_phys_write_le32(base + 12, dma_phys);
    bbk9588_phys_write_le32(base + 16, bytes);
    bbk9588_phys_write_le32(base + 20, cmd);
    bbk9588_phys_write_le32(base + 24, arg);
    bbk9588_phys_write_le32(base + 28, first_word);
    bbk9588_phys_write_le32(base + 32, pc);
}

static void bbk9588_nand_target_trace_record(uint32_t event,
                                             uint32_t a,
                                             uint32_t b,
                                             uint32_t c)
{
    Bbk9588MachineState *board = bbk9588_active_board;
    uint32_t seq;
    uint32_t slot;
    uint32_t base;
    uint32_t pc = 0;

    if (!board || !board->storage_trace_enabled) {
        return;
    }
    if (board->cpu) {
        pc = board->cpu->env.active_tc.PC & 0xffffffffu;
    }

    seq = ++board->nand_target_trace_seq;
    slot = (seq - 1) % BBK9588_NAND_TARGET_TRACE_SLOTS;
    base = BBK9588_NAND_TARGET_TRACE_VA +
           BBK9588_STORAGE_TRACE_HEADER_WORDS * 4u +
           slot * BBK9588_NAND_TARGET_TRACE_WORDS * 4u;
    bbk9588_phys_write_le32(BBK9588_NAND_TARGET_TRACE_VA,
                            BBK9588_NAND_TARGET_TRACE_MAGIC);
    bbk9588_phys_write_le32(BBK9588_NAND_TARGET_TRACE_VA + 4, seq);
    bbk9588_phys_write_le32(BBK9588_NAND_TARGET_TRACE_VA + 8, slot);
    bbk9588_phys_write_le32(BBK9588_NAND_TARGET_TRACE_VA + 12,
                            BBK9588_NAND_TARGET_TRACE_SLOTS);
    bbk9588_phys_write_le32(base + 0, seq);
    bbk9588_phys_write_le32(base + 4, event);
    bbk9588_phys_write_le32(base + 8, a);
    bbk9588_phys_write_le32(base + 12, b);
    bbk9588_phys_write_le32(base + 16, c);
    bbk9588_phys_write_le32(base + 20, pc);
}

static void bbk9588_msc_kick_dmac(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    if (board->dmac) {
        jz4740_dmac_kick(board->dmac);
    }
}

static void bbk9588_msc_command(void *opaque, uint32_t command,
                                uint32_t argument)
{
    (void)opaque;
    bbk9588_msc_trace_record(BBK9588_MSC_TRACE_CMD, argument >> 9,
                             0, 0, command, argument, 0);
}

static bool bbk9588_msc_dma_transfer(Bbk9588MachineState *board,
                                     unsigned channel, uint32_t source,
                                     uint32_t target, uint32_t words)
{
    JZ4740MSCDMATransfer transfer;
    uint8_t *buf;
    bool ok = true;

    if (!board->msc ||
        !jz4740_msc_begin_dma(board->msc, channel, source, target, words,
                              &transfer)) {
        return false;
    }
    bbk9588_storage_trace_record(
        BBK9588_STORAGE_TRACE_DMAC_TRANSFER |
        (transfer.read ? 1u : 2u),
        transfer.words, transfer.dma_phys);
    if (transfer.bytes == 0) {
        jz4740_msc_finish_dma(board->msc, false);
        return true;
    }
    if (transfer.sectors == 0 || transfer.sectors > 128) {
        return false;
    }

    buf = g_malloc0(transfer.sectors * 512u);
    if (transfer.read) {
        /* No removable MSC medium is attached by default. */
        bbk9588_msc_trace_record(BBK9588_MSC_TRACE_READ,
                                 transfer.lba, transfer.dma_phys,
                                 transfer.bytes, transfer.command,
                                 transfer.argument,
                                 ok ? bbk9588_ldl_le(buf) : 0xffffffffu);
        if (ok) {
            cpu_physical_memory_write(
                transfer.dma_phys, buf,
                MIN(transfer.bytes, transfer.sectors * 512u));
        }
    } else {
        cpu_physical_memory_read(
            transfer.dma_phys, buf,
            MIN(transfer.bytes, transfer.sectors * 512u));
        bbk9588_msc_trace_record(BBK9588_MSC_TRACE_WRITE,
                                 transfer.lba, transfer.dma_phys,
                                 transfer.bytes, transfer.command,
                                 transfer.argument,
                                 bbk9588_ldl_le(buf));
    }
    jz4740_msc_finish_dma(board->msc, ok);
    g_free(buf);
    return true;
}

static void bbk9588_dmac_trace_sample(void *opaque, uint32_t event,
                                      unsigned channel, hwaddr offset,
                                      uint32_t value)
{
    Bbk9588MachineState *board = opaque;
    uint32_t base = BBK9588_DMAC_TRACE_VA;
    uint32_t pc = 0;

    if (!board || !board->storage_trace_enabled ||
        !bbk9588_guest_ram_va_valid(base, BBK9588_DMAC_TRACE_WORDS * 4)) {
        return;
    }

    if (board->cpu) {
        pc = board->cpu->env.active_tc.PC & 0xffffffffu;
    }

    board->dmac_trace_seq++;
    board->dmac_last_event = event;
    board->dmac_last_channel = channel;
    board->dmac_last_offset = offset;
    board->dmac_last_value = value;

    bbk9588_phys_write_le32(base + 0x00, BBK9588_DMAC_TRACE_MAGIC);
    bbk9588_phys_write_le32(base + 0x04, board->dmac_trace_seq);
    bbk9588_phys_write_le32(base + 0x08, event);
    bbk9588_phys_write_le32(base + 0x0c, channel);
    bbk9588_phys_write_le32(base + 0x10, offset);
    bbk9588_phys_write_le32(base + 0x14, value);
    bbk9588_phys_write_le32(base + 0x18, pc);
    bbk9588_phys_write_le32(base + 0x1c,
                            jz4740_intc_pending(board->intc));
    bbk9588_phys_write_le32(base + 0x20,
                            jz4740_intc_mask(board->intc));
    bbk9588_phys_write_le32(base + 0x24,
                            jz4740_dmac_get_reg(board->dmac, 0x304));
    bbk9588_phys_write_le32(base + 0x28,
                            jz4740_dmac_get_reg(board->dmac, 0x50));
    bbk9588_phys_write_le32(base + 0x2c,
                            jz4740_dmac_get_reg(board->dmac, 0x54));
    bbk9588_phys_write_le32(base + 0x30,
                            jz4740_dmac_get_reg(board->dmac, 0x48));
    bbk9588_phys_write_le32(base + 0x34,
                            jz4740_dmac_get_reg(board->dmac, 0x70));
    bbk9588_phys_write_le32(base + 0x38,
                            jz4740_dmac_get_reg(board->dmac, 0x74));
    bbk9588_phys_write_le32(base + 0x3c,
                            jz4740_dmac_get_reg(board->dmac, 0x68));
}

static bool bbk9588_dmac_bulk_transfer(void *opaque, unsigned channel,
                                       uint32_t request, uint32_t source,
                                       uint32_t target, uint32_t count,
                                       uint32_t command)
{
    return bbk9588_msc_dma_transfer(opaque, channel, source, target, count);
}

static bool bbk9588_dmac_endpoint_address_valid(void *opaque,
                                                unsigned request,
                                                uint32_t address)
{
    return (request == JZ4740_DMAC_REQUEST_AIC_TX ||
            request == JZ4740_DMAC_REQUEST_AIC_RX) &&
           BBK9588_KSEG_TO_PHYS(address) == BBK9588_AIC_DATA_PHYS;
}

static size_t bbk9588_dmac_endpoint_write(void *opaque, unsigned request,
                                          const uint8_t *buf, size_t bytes,
                                          unsigned width)
{
    Bbk9588MachineState *board = opaque;

    if (!board->aic || request != JZ4740_DMAC_REQUEST_AIC_TX) {
        return 0;
    }
    return jz4740_aic_dma_write_tx(board->aic, buf, bytes, width);
}

static size_t bbk9588_dmac_endpoint_read(void *opaque, unsigned request,
                                         uint8_t *buf, size_t bytes,
                                         unsigned width)
{
    Bbk9588MachineState *board = opaque;

    if (!board->aic || request != JZ4740_DMAC_REQUEST_AIC_RX) {
        return 0;
    }
    return jz4740_aic_dma_read_rx(board->aic, buf, bytes, width);
}

static void bbk9588_dmac_endpoint_complete(void *opaque, unsigned request)
{
    Bbk9588MachineState *board = opaque;

    if (board->aic && request == JZ4740_DMAC_REQUEST_AIC_TX) {
        jz4740_aic_notify_tx_dma_boundary(board->aic);
    }
}

static void bbk9588_dmac_endpoint_diagnostics(
    void *opaque, unsigned request,
    JZ4740DMACEndpointDiagnostics *diagnostics)
{
    Bbk9588MachineState *board = opaque;
    JZ4740AICDiagnostics aic;

    if (!board->aic) {
        return;
    }
    jz4740_aic_get_diagnostics(board->aic, &aic);
    diagnostics->underruns = aic.underruns;
    diagnostics->fifo_level = request == JZ4740_DMAC_REQUEST_AIC_RX ?
                              aic.rx_fifo_level : aic.tx_fifo_level;
}

static const JZ4740DMACPeripheralOps bbk9588_dmac_peripheral_ops = {
    .bulk_transfer = bbk9588_dmac_bulk_transfer,
    .address_valid = bbk9588_dmac_endpoint_address_valid,
    .write = bbk9588_dmac_endpoint_write,
    .read = bbk9588_dmac_endpoint_read,
    .complete = bbk9588_dmac_endpoint_complete,
    .get_diagnostics = bbk9588_dmac_endpoint_diagnostics,
    .trace = bbk9588_dmac_trace_sample,
};

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

static void bbk9588_touch_trace_update(Bbk9588MachineState *board,
                                        uint32_t reason)
{
    JZ4740GPIODiagnostics gpio_diag;
    JZ4740INTCDiagnostics intc_diag;
    JZ4740EMCDiagnostics emc_diag;
    Bbk9588NandDiagnostics nand_diag;
    JZ4740MSCDiagnostics msc_diag;
    JZ4740SADCDiagnostics sadc_diag;
    JZ4740TCUDiagnostics tcu_diag;

    if (!board || !board->touch_trace_enabled ||
        !bbk9588_guest_ram_va_valid(BBK9588_TOUCH_TRACE_VA, 0x154)) {
        return;
    }

    uint32_t pc = board->cpu ? board->cpu->env.active_tc.PC : 0;

    jz4740_gpio_get_diagnostics(board->gpio, &gpio_diag);
    jz4740_intc_get_diagnostics(board->intc, &intc_diag);
    jz4740_emc_get_diagnostics(board->emc, &emc_diag);
    bbk9588_nand_get_diagnostics(board->nand_dev, &nand_diag);
    jz4740_msc_get_diagnostics(board->msc, &msc_diag);
    jz4740_sadc_get_diagnostics(board->sadc, &sadc_diag);
    jz4740_tcu_get_diagnostics(board->tcu, &tcu_diag);

    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x00,
                            BBK9588_TOUCH_TRACE_MAGIC);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x04, 0);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x08, 0);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x0c, 0);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x10,
                            sadc_diag.touch_down ? 1u : 0u);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x14,
                            sadc_diag.touch_raw_x);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x18,
                            sadc_diag.touch_raw_y);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x1c,
                            sadc_diag.status);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x20,
                            sadc_diag.next_axis);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x24,
                            intc_diag.pending);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x28, pc);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x2c, reason);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x30,
                            sadc_diag.conversion_events_remaining);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x34,
                            sadc_diag.control);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x38,
                            sadc_diag.last_read_offset);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x3c,
                            sadc_diag.last_read_value);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x40,
                            sadc_diag.last_write_offset);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x44,
                            sadc_diag.last_write_value);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x48,
                            gpio_diag.last_read_offset);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x4c,
                            gpio_diag.last_read_value);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x50,
                            gpio_diag.last_flag_offset);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x54,
                            gpio_diag.last_flag_value);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x58,
                            intc_diag.mask);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x5c,
                            tcu_diag.enabled_mask);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x60,
                            tcu_diag.pending_mask);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x64,
                            tcu_diag.compare[0]);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x68,
                            tcu_diag.compare[1]);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x6c,
                            tcu_diag.period_ms[0]);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x70,
                            tcu_diag.period_ms[1]);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x74,
                            (uint32_t)(tcu_diag.deadline_ns[0] / SCALE_MS));
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x78,
                            (uint32_t)(tcu_diag.deadline_ns[1] / SCALE_MS));
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x7c,
                            intc_diag.unmasked_pending);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x80,
                            intc_diag.output_level);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x84,
                            intc_diag.update_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x88,
                            board->intc_last_cp0_status);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x8c,
                            board->intc_last_cp0_cause);
    if (board->cpu) {
        CPUState *cs = CPU(board->cpu);

        bbk9588_phys_write_le32(
            BBK9588_TOUCH_TRACE_VA + 0x90,
            board->cpu->env.bbk9588_irq_ip2_level ? 1u : 0u);
        bbk9588_phys_write_le32(
            BBK9588_TOUCH_TRACE_VA + 0x94,
            (uint32_t)cs->interrupt_request);
    }
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x98,
                            intc_diag.last_read_offset);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x9c,
                            intc_diag.last_read_value);
    if (board->dmac) {
        bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x144,
                                jz4740_dmac_get_reg(board->dmac, 0x304));
        bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x148,
                                jz4740_dmac_get_reg(board->dmac, 0x70));
        bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x14c,
                                jz4740_dmac_get_reg(board->dmac, 0x74));
        bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x150,
                                jz4740_dmac_get_reg(board->dmac, 0x68));
    }
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xa0,
                            intc_diag.last_write_offset);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xa4,
                            intc_diag.last_write_value);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xa8, 0);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xac, 0);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xb0,
                            tcu_diag.last_read_offset);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xb4,
                            tcu_diag.last_read_value);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xb8,
                            tcu_diag.last_write_offset);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xbc,
                            tcu_diag.last_write_value);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xc0,
                            tcu_diag.irq_raise_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xc4, 0);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xc8,
                            msc_diag.read_pending ? 1u : 0u);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xcc,
                            msc_diag.write_pending ? 1u : 0u);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xd0,
                            msc_diag.data_ready ? 1u : 0u);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xd4,
                            msc_diag.read_lba);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xd8,
                            msc_diag.write_lba);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xdc,
                            msc_diag.dma_complete_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xe0,
                            msc_diag.last_command);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xe4,
                            msc_diag.last_argument);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xe8,
                            msc_diag.last_dma_phys);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xec,
                            msc_diag.last_dma_words);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xf0,
                            board->nand_ready_raise_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xf4,
                            nand_diag.page_read_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xf8,
                            nand_diag.program_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xfc,
                            nand_diag.erase_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x100,
                            nand_diag.command);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x104,
                            nand_diag.last_page);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x108,
                            nand_diag.last_column);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x10c,
                            nand_diag.last_block);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x110,
                            nand_diag.busy_reads);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x114,
                            emc_diag.nfints);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x118,
                            nand_diag.address_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x11c,
                            gpio_diag.flag[JZ4740_GPIO_PORT_C]);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x120,
                            jz4740_cpm_clkgr_wake_mask(board->cpm));
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x124,
                            jz4740_cpm_scr_wake_mask(board->cpm));
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x128,
                            board->extgpio_wake_enable_80);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x12c,
                            board->sysctrl_wake_pending ? 1u : 0u);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x130,
                            board->sysctrl_wake_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x134,
                            tcu_diag.irq_mask);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x138,
                            tcu_diag.compare[4]);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x13c,
                            tcu_diag.period_ms[4]);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x140,
                            (uint32_t)(tcu_diag.deadline_ns[4] / SCALE_MS));
}

static void bbk9588_sadc_trace(void *opaque, uint32_t reason)
{
    bbk9588_touch_trace_update(opaque, reason);
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
    bbk9588_touch_trace_update(opaque, reason);
}

static uint32_t bbk9588_gpio_sample_input(void *opaque, unsigned port,
                                          uint32_t level)
{
    Bbk9588MachineState *board = opaque;

    if (port == JZ4740_GPIO_PORT_D) {
        if (board->gpio300_wake_pulse_available) {
            level |= 0x20000000u;
            board->gpio300_wake_pulse_available = false;
            board->sysctrl_wake_pending = false;
            bbk9588_update_irq(board);
        } else {
            level &= ~0x20000000u;
        }
    }
    if (port == JZ4740_GPIO_PORT_C &&
        bbk9588_nand_consume_busy_read(board->nand_dev)) {
        level &= ~0x40000000;
    }
    return level;
}

static void bbk9588_panel_trace_write(void *opaque, hwaddr offset,
                                      uint64_t value, unsigned size)
{
    Bbk9588MachineState *board = opaque;
    JZ4740LCDDiagnostics lcd;

    if (!board || !board->graphics_trace_enabled) {
        return;
    }
    if (board->graphics_trace_count++ >= 4096) {
        return;
    }
    jz4740_lcd_get_diagnostics(board->lcd, &lcd);
    error_report(
        "bbk9588-panel[%u] off=0x%04" HWADDR_PRIx
        " size=%u value=0x%08" PRIx64
        " r0000=0x%08x r0004=0x%08x r0008=0x%08x r000c=0x%08x"
        " r0010=0x%08x r0014=0x%08x r0018=0x%08x r001c=0x%08x"
        " r0020=0x%08x r0024=0x%08x r0028=0x%08x r002c=0x%08x"
        " r0030=0x%08x r0034=0x%08x r0038=0x%08x r003c=0x%08x"
        " r0040=0x%08x r0044=0x%08x r0048=0x%08x r004c=0x%08x"
        " lcd_desc=0x%08x lcd_fb=0x%08x lcd_source=%u",
        board->graphics_trace_count - 1,
        offset,
        size,
        value,
        bbk9588_panel_get_reg(board->panel, 0x0000),
        bbk9588_panel_get_reg(board->panel, 0x0004),
        bbk9588_panel_get_reg(board->panel, 0x0008),
        bbk9588_panel_get_reg(board->panel, 0x000c),
        bbk9588_panel_get_reg(board->panel, 0x0010),
        bbk9588_panel_get_reg(board->panel, 0x0014),
        bbk9588_panel_get_reg(board->panel, 0x0018),
        bbk9588_panel_get_reg(board->panel, 0x001c),
        bbk9588_panel_get_reg(board->panel, 0x0020),
        bbk9588_panel_get_reg(board->panel, 0x0024),
        bbk9588_panel_get_reg(board->panel, 0x0028),
        bbk9588_panel_get_reg(board->panel, 0x002c),
        bbk9588_panel_get_reg(board->panel, 0x0030),
        bbk9588_panel_get_reg(board->panel, 0x0034),
        bbk9588_panel_get_reg(board->panel, 0x0038),
        bbk9588_panel_get_reg(board->panel, 0x003c),
        bbk9588_panel_get_reg(board->panel, 0x0040),
        bbk9588_panel_get_reg(board->panel, 0x0044),
        bbk9588_panel_get_reg(board->panel, 0x0048),
        bbk9588_panel_get_reg(board->panel, 0x004c),
        lcd.descriptor_address,
        lcd.framebuffer_address,
        lcd.frame_source_kind);
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
    jz4740_lcd_set_trace_enabled(board->lcd,
                                  board->graphics_trace_enabled);
}

static void bbk9588_create_panel_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_BBK9588_PANEL);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0043000u));
    board->panel = BBK9588_PANEL(dev);
    bbk9588_panel_set_write_callback(
        board->panel, bbk9588_panel_trace_write, board);
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
    qdev_prop_set_uint32(dev, "input-reset-d", 0x00200000u);
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

static void bbk9588_create_rtc_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_JZ4740_RTC);
    SysBusDevice *sbd = SYS_BUS_DEVICE(dev);

    sysbus_realize(sbd, &error_fatal);
    sysbus_mmio_map(sbd, 0, BBK9588_KSEG_TO_PHYS(0xb0003000u));
    sysbus_connect_irq(sbd, 0,
                       qdev_get_gpio_in(DEVICE(board->intc),
                                        JZ4740_INTC_IRQ_RTC));
    board->rtc = JZ4740_RTC(dev);
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
    jz4740_dmac_set_peripheral_ops(board->dmac,
                                   &bbk9588_dmac_peripheral_ops, board);
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
    jz4740_msc_set_kick_callback(board->msc, bbk9588_msc_kick_dmac, board);
    jz4740_msc_set_command_callback(board->msc, bbk9588_msc_command, board);
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

    board->input_event_read_idx = 0;
    board->input_event_write_idx = 0;
    board->input_event_count = 0;
    memset(board->input_event_words, 0, sizeof(board->input_event_words));
    bbk9588_event_queue_mirror_all(board);
    if (bbk9588_guest_ram_va_valid(BBK9588_EVENT_SCRATCH_VA, 0x1c)) {
        bbk9588_phys_write_le32(BBK9588_EVENT_SCRATCH_VA + 0x18, 0);
    }
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
    bbk9588_nand_set_trace_enabled(board->nand_dev, value);
}

static bool bbk9588_get_graphics_trace(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->graphics_trace_enabled;
}

static void bbk9588_set_graphics_trace(Object *obj, bool value,
                                       Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->graphics_trace_enabled = value;
    jz4740_lcd_set_trace_enabled(board->lcd, value);
}

static bool bbk9588_get_touch_trace(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->touch_trace_enabled;
}

static void bbk9588_set_touch_trace(Object *obj, bool value,
                                    Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->touch_trace_enabled = value;
}

static bool bbk9588_get_progress_trace(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->progress_trace_enabled;
}

static void bbk9588_set_progress_trace(Object *obj, bool value,
                                       Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    board->progress_trace_enabled = value;
}

static bool bbk9588_get_bootrom_nand(Object *obj, Error **errp)
{
    Bbk9588MachineState *board = BBK9588_MACHINE(obj);

    return board->bootrom_nand_enabled;
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

    if (board->aic) {
        object_unref(OBJECT(board->aic));
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
    board->graphics_trace_enabled = false;
    board->touch_trace_enabled = false;
    board->graphics_trace_count = 0;
    board->storage_trace_seq = 0;
    board->msc_trace_seq = 0;
    board->progress_trace_enabled = false;
    board->progress_trace_seq = 0;
    board->bootrom_nand_enabled = false;
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
    bbk9588_active_board = board;

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
    board->input_event_read_idx = 0;
    board->input_event_write_idx = 0;
    board->input_event_count = 0;
    memset(board->input_event_words, 0, sizeof(board->input_event_words));
    qemu_register_reset(bbk9588_cpu_reset, board);
    board->cpu_irq = cpu->env.irq[2];
    board->intc_resample_timer = timer_new_ms(QEMU_CLOCK_REALTIME,
                                              bbk9588_intc_resample_timer_cb,
                                              board);
    board->progress_trace_timer = timer_new_ms(
        QEMU_CLOCK_REALTIME, bbk9588_progress_trace_timer_cb, board);

    bbk9588_create_host_input(board);
    bbk9588_create_host_bridge(board);
    board->extgpio_wake_enable_80 = 0;
    board->sysctrl_wake_pending = false;
    board->gpio300_wake_pulse_available = false;
    board->sysctrl_wake_count = 0;
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
    bbk9588_touch_sync_latch(board);
    bbk9588_touch_trace_update(board, 7u);
    bbk9588_create_aic_device(machine, board);
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
