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
#include "chardev/char-fe.h"
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
#include "hw/display/jz4740_lcd.h"
#include "hw/dma/jz4740_dmac.h"
#include "hw/gpio/jz4740_gpio.h"
#include "hw/input/jz4740_sadc.h"
#include "hw/intc/jz4740_intc.h"
#include "hw/misc/jz4740_cpm.h"
#include "hw/rtc/jz4740_rtc.h"
#include "hw/timer/jz4740_tcu.h"
#include "qemu/error-report.h"
#include "qemu/atomic.h"
#include "qemu/cutils.h"
#include "qemu/host-utils.h"
#include "qemu/main-loop.h"
#include "qemu/timer.h"
#include "qom/object.h"
#include "target/mips/cpu.h"
#include "ui/console.h"
#include "ui/surface.h"

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
#define BBK9588_NAND_PAGE_SIZE     2048
#define BBK9588_NAND_SPARE_SIZE    64
#define BBK9588_NAND_STRIDE        (BBK9588_NAND_PAGE_SIZE + \
                                    BBK9588_NAND_SPARE_SIZE)
#define BBK9588_NAND_PAGES_PER_BLOCK 64u
#define BBK9588_NAND_BLOCKS        4096u
#define BBK9588_NAND_TOTAL_PAGES   (BBK9588_NAND_BLOCKS * \
                                    BBK9588_NAND_PAGES_PER_BLOCK)
#define BBK9588_NAND_TOTAL_SIZE    ((uint64_t)BBK9588_NAND_TOTAL_PAGES * \
                                    BBK9588_NAND_STRIDE)
#define BBK9588_NAND_ID_MAKER      0xecu
#define BBK9588_NAND_ID_CODE       0xdcu
#define BBK9588_NAND_ID_BYTE2      0x10u
#define BBK9588_NAND_ID_BYTE3      0x95u
#define BBK9588_NAND_ID_BYTE4      0x44u
#define BBK9588_FRAMEBUFFER_VA     0xa1f82000u
#define BBK9588_LCD_WIDTH          240
#define BBK9588_LCD_HEIGHT         320
#define BBK9588_LCD_STRIDE         (BBK9588_LCD_WIDTH * 2)
#define BBK9588_LCD_BYTES          (BBK9588_LCD_STRIDE * BBK9588_LCD_HEIGHT)
#define BBK9588_LCD_VBLANK_PERIOD_MS 33
#define BBK9588_LCD_MIRROR_CONFIG  0x804a6b88u
#define BBK9588_LCD_STATUS_FRAME_DONE 0x00000001u
#define BBK9588_LCD_STATUS_READY      0x00000080u
#define BBK9588_GUI_EVENT_OBJ_OFF  0xf0u
#define BBK9588_FRAME_MAGIC        0x464b4242u
#define BBK9588_PERF_MAGIC         0x504b4242u
#define BBK9588_AUDIO_MAGIC        0x414b4242u
#define BBK9588_FRAME_FORMAT_RGB565 0x00005635u
#define BBK9588_AUDIO_FORMAT_S16LE 0x36314c53u
#define BBK9588_PERF_FORMAT_GUEST_INSNS 0x00004950u
#define BBK9588_PERF_PAYLOAD_BYTES 16u
#define BBK9588_PERF_FORMAT_AIC  0x00434941u
#define BBK9588_AIC_PERF_WORDS  24u
#define BBK9588_AIC_PERF_PAYLOAD_BYTES \
    (BBK9588_AIC_PERF_WORDS * sizeof(uint64_t))
#define BBK9588_PERF_PERIOD_MS     1000u
#define BBK9588_UART_FIFO_SIZE     16u
#define BBK9588_UART_IRQ           JZ4740_INTC_IRQ_UART0
#define BBK9588_UART_RBR_OFF       0x00u
#define BBK9588_UART_THR_OFF       0x00u
#define BBK9588_UART_DLL_OFF       0x00u
#define BBK9588_UART_IER_OFF       0x04u
#define BBK9588_UART_DLH_OFF       0x04u
#define BBK9588_UART_IIR_OFF       0x08u
#define BBK9588_UART_FCR_OFF       0x08u
#define BBK9588_UART_LCR_OFF       0x0cu
#define BBK9588_UART_MCR_OFF       0x10u
#define BBK9588_UART_LSR_OFF       0x14u
#define BBK9588_UART_MSR_OFF       0x18u
#define BBK9588_UART_SPR_OFF       0x1cu
#define BBK9588_UART_ISR_OFF       0x20u
#define BBK9588_UART_UMR_OFF       0x24u
#define BBK9588_UART_UACR_OFF      0x28u
#define BBK9588_UART_IER_RDRIE     0x01u
#define BBK9588_UART_IER_TDRIE     0x02u
#define BBK9588_UART_IER_RLSIE     0x04u
#define BBK9588_UART_IER_MSIE      0x08u
#define BBK9588_UART_IER_RTOIE     0x10u
#define BBK9588_UART_IER_MASK      0x1fu
#define BBK9588_UART_IIR_NONE      0x01u
#define BBK9588_UART_IIR_MODEM     0x00u
#define BBK9588_UART_IIR_TDR       0x02u
#define BBK9588_UART_IIR_RDR       0x04u
#define BBK9588_UART_IIR_RLS       0x06u
#define BBK9588_UART_IIR_RTO       0x0cu
#define BBK9588_UART_IIR_FIFO      0xc0u
#define BBK9588_UART_FCR_FME       0x01u
#define BBK9588_UART_FCR_RFRT      0x02u
#define BBK9588_UART_FCR_TFRT      0x04u
#define BBK9588_UART_FCR_DME       0x08u
#define BBK9588_UART_FCR_UME       0x10u
#define BBK9588_UART_FCR_RDTR_MASK 0xc0u
#define BBK9588_UART_FCR_MASK      0xdfu
#define BBK9588_UART_LCR_DLAB      0x80u
#define BBK9588_UART_MCR_MDCE      0x80u
#define BBK9588_UART_MCR_LOOP      0x10u
#define BBK9588_UART_MCR_RTS       0x02u
#define BBK9588_UART_MCR_MASK      0x92u
#define BBK9588_UART_LSR_DRY       0x01u
#define BBK9588_UART_LSR_OVER      0x02u
#define BBK9588_UART_LSR_PARER     0x04u
#define BBK9588_UART_LSR_FMER      0x08u
#define BBK9588_UART_LSR_BI        0x10u
#define BBK9588_UART_LSR_TDRQ      0x20u
#define BBK9588_UART_LSR_TEMP      0x40u
#define BBK9588_UART_LSR_FIFOE     0x80u
#define BBK9588_UART_LSR_RESET \
    (BBK9588_UART_LSR_TDRQ | BBK9588_UART_LSR_TEMP)
#define BBK9588_UART_LSR_ERROR_MASK \
    (BBK9588_UART_LSR_OVER | BBK9588_UART_LSR_PARER | \
     BBK9588_UART_LSR_FMER | BBK9588_UART_LSR_BI | \
     BBK9588_UART_LSR_FIFOE)
#define BBK9588_UART_MSR_CCTS      0x01u
#define BBK9588_UART_MSR_CTS       0x10u
#define BBK9588_UART_ISR_MASK      0x1fu
#define BBK9588_UART_UMR_MASK      0x3fu
#define BBK9588_UART_UACR_MASK     0x0fffu
#define BBK9588_UDC_IRQ            JZ4740_INTC_IRQ_UDC
#define BBK9588_UDC_EP_COUNT       16u
#define BBK9588_UDC_FADDR_OFF      0x00u
#define BBK9588_UDC_POWER_OFF      0x01u
#define BBK9588_UDC_INTRIN_OFF     0x02u
#define BBK9588_UDC_INTROUT_OFF    0x04u
#define BBK9588_UDC_INTRINE_OFF    0x06u
#define BBK9588_UDC_INTROUTE_OFF   0x08u
#define BBK9588_UDC_INTRUSB_OFF    0x0au
#define BBK9588_UDC_INTRUSBE_OFF   0x0bu
#define BBK9588_UDC_FRAME_OFF      0x0cu
#define BBK9588_UDC_INDEX_OFF      0x0eu
#define BBK9588_UDC_TESTMODE_OFF   0x0fu
#define BBK9588_UDC_INMAXP_OFF     0x10u
#define BBK9588_UDC_CSR0_INCSR_OFF 0x12u
#define BBK9588_UDC_OUTMAXP_OFF    0x14u
#define BBK9588_UDC_OUTCSR_OFF     0x16u
#define BBK9588_UDC_COUNT_OFF      0x18u
#define BBK9588_UDC_FIFO_BASE_OFF  0x20u
#define BBK9588_UDC_FIFO_END_OFF   0x60u
#define BBK9588_UDC_EPINFO_OFF     0x78u
#define BBK9588_UDC_RAMINFO_OFF    0x79u
#define BBK9588_UDC_POWER_RESET    0x20u
#define BBK9588_UDC_POWER_RW_MASK  0xe5u
#define BBK9588_UDC_INTRINE_RESET  0xffffu
#define BBK9588_UDC_INTROUTE_RESET 0xfffeu
#define BBK9588_UDC_INTRUSBE_RESET 0x06u
#define BBK9588_UDC_INTRUSBE_MASK  0x0fu
#define BBK9588_UDC_INTRIN_ENDPOINT_MASK 0x000fu
#define BBK9588_UDC_INTROUT_ENDPOINT_MASK 0x0006u
#define BBK9588_UDC_INDEX_MASK     0x0fu
#define BBK9588_UDC_TESTMODE_MASK  0x3fu
#define BBK9588_UDC_EPINFO_VALUE   0x23u
#define BBK9588_UDC_RAMINFO_VALUE  0x00u
#define BBK9588_UDC_MAXP_MASK      0x07ffu
#define BBK9588_UDC_INCSR_RW_MASK  0xfc10u
#define BBK9588_UDC_OUTCSR_RW_MASK 0xf820u
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
#define BBK9588_MSC_STRPCL_OFF     0x0000u
#define BBK9588_MSC_STAT_OFF       0x0004u
#define BBK9588_MSC_RESTO_OFF      0x0010u
#define BBK9588_MSC_RDTO_OFF       0x0014u
#define BBK9588_MSC_IMASK_OFF      0x0024u
#define BBK9588_MSC_IREG_OFF       0x0028u
#define BBK9588_MSC_CMD_OFF        0x002cu
#define BBK9588_MSC_ARG_OFF        0x0030u
#define BBK9588_MSC_RES_OFF        0x0034u
#define BBK9588_MSC_STAT_RESET     0x00000040u
#define BBK9588_MSC_RESTO_RESET    0x00000040u
#define BBK9588_MSC_RDTO_RESET     0x0000ffffu
#define BBK9588_MSC_IMASK_RESET    0x000000ffu
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
#define BBK9588_NAND_NFCSR_OFF         0x050u
#define BBK9588_NAND_NFECCR_OFF        0x100u
#define BBK9588_NAND_NFECC_OFF         0x104u
#define BBK9588_NAND_NFINTS_OFF        0x114u
#define BBK9588_NAND_NFINTE_OFF        0x118u
#define BBK9588_NAND_NFCSR_RW_MASK     0x000000ffu
#define BBK9588_NAND_NFECCR_RW_MASK    0x0000000du
#define BBK9588_NAND_NFECCR_ECCE       0x00000001u
#define BBK9588_NAND_NFECCR_ERST       0x00000002u
#define BBK9588_NAND_NFECCR_ENCE       0x00000008u
/* NFINTS: bit2 ENCF, bit3 DECF.  ERR/UNCOR stay clear for clean data. */
#define BBK9588_BCH_STATUS_ENCODE_DONE 0x00000004u
#define BBK9588_BCH_STATUS_DECODE_DONE 0x00000008u
#define BBK9588_BCH_STATUS_W0C_MASK    0x0000001fu
#define BBK9588_BCH_CONTROL_ENCODE     BBK9588_NAND_NFECCR_ENCE
#define BBK9588_SYSCTRL_WAKE_PROXY_IRQ JZ4740_INTC_IRQ_TCU1

/*
 * C200 firmware accesses SoC devices through KSEG1 uncached addresses such as
 * 0xb0001000. QEMU MemoryRegions are mapped in physical address space, so the
 * board exposes those windows at kseg1 & 0x1fffffff.
 */
#define KSEG1_TO_PHYS(addr)        ((addr) & 0x1fffffff)

typedef enum Bbk9588MmioKind {
    BBK9588_MMIO_EXTGPIO,
    BBK9588_MMIO_GRAPHICS,
    BBK9588_MMIO_UART,
    BBK9588_MMIO_UDC,
    BBK9588_MMIO_LCD,
    BBK9588_MMIO_MISC,
} Bbk9588MmioKind;

typedef struct Bbk9588MmioWindow {
    const char *name;
    hwaddr kseg1_base;
    hwaddr size;
    Bbk9588MmioKind kind;
} Bbk9588MmioWindow;

typedef struct Bbk9588MachineState Bbk9588MachineState;
typedef struct Bbk9588NandState Bbk9588NandState;
typedef struct Bbk9588MmioState Bbk9588MmioState;

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

typedef struct Bbk9588MmioState {
    MemoryRegion mr;
    const Bbk9588MmioWindow *window;
    Bbk9588MachineState *board;
    uint32_t regs[0x20000 / sizeof(uint32_t)];
    uint8_t msc_response[16];
    uint32_t msc_response_len;
    uint32_t msc_response_index;
} Bbk9588MmioState;

struct Bbk9588MachineState {
    MachineState parent_obj;

    CharFrontend uart_chr;
    CharFrontend input_chr;
    CharFrontend frame_chr;
    uint32_t uart_status;
    bool uart_thr_irq_latched;
    uint8_t uart_ier;
    uint8_t uart_fcr;
    uint8_t uart_lcr;
    uint8_t uart_mcr;
    uint8_t uart_msr;
    uint8_t uart_spr;
    uint8_t uart_isr;
    uint8_t uart_umr;
    uint16_t uart_uacr;
    uint8_t uart_dll;
    uint8_t uart_dlh;
    uint8_t udc_faddr;
    uint8_t udc_power;
    uint16_t udc_intr_in;
    uint16_t udc_intr_out;
    uint16_t udc_intr_in_enable;
    uint16_t udc_intr_out_enable;
    uint8_t udc_intr_usb;
    uint8_t udc_intr_usb_enable;
    uint16_t udc_frame;
    uint8_t udc_index;
    uint8_t udc_testmode;
    uint16_t udc_in_maxp[BBK9588_UDC_EP_COUNT];
    uint16_t udc_in_csr[BBK9588_UDC_EP_COUNT];
    uint16_t udc_out_maxp[BBK9588_UDC_EP_COUNT];
    uint16_t udc_out_csr[BBK9588_UDC_EP_COUNT];
    uint32_t lcd_status;
    uint32_t lcd_irq_status;
    MIPSCPU *cpu;
    qemu_irq cpu_irq;
    qemu_irq aic_irq;
    qemu_irq intc_irq;
    qemu_irq dmac_irq;
    qemu_irq tcu_irq[JZ4740_TCU_NUM_OUTPUTS];
    QEMUTimer *intc_resample_timer;
    QEMUTimer *progress_trace_timer;
    QEMUTimer *lcd_refresh_timer;
    QemuConsole *lcd_console;
    DisplaySurface *lcd_surface;
    uint8_t lcd_framebuffer[BBK9588_LCD_BYTES];
    uint8_t lcd_last_framebuffer[BBK9588_LCD_BYTES];
    uint32_t lcd_frame_seq;
    uint32_t perf_seq;
    uint32_t audio_seq;
    int64_t lcd_scanout_not_before_ms;
    int64_t lcd_frame_stable_not_before_ms;
    int64_t perf_last_send_ms;
    bool lcd_last_frame_valid;
    bool lcd_frame_chardev_sent_valid;
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
    JZ4740LCDState *lcd;
    JZ4740INTCState *intc;
    JZ4740CPMState *cpm;
    JZ4740DMACState *dmac;
    JZ4740GPIOState *gpio;
    JZ4740RTCState *rtc;
    JZ4740SADCState *sadc;
    JZ4740TCUState *tcu;
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
    uint8_t uart_rx_fifo[BBK9588_UART_FIFO_SIZE];
    unsigned uart_rx_head;
    unsigned uart_rx_len;
    bool msc_read_pending;
    bool msc_write_pending;
    bool msc_data_ready;
    uint32_t msc_read_lba;
    uint32_t msc_write_lba;
    uint32_t msc_last_cmd;
    uint32_t msc_last_arg;
    uint32_t msc_last_dma_phys;
    uint32_t msc_last_dma_words;
    uint32_t msc_dma_complete_count;
    uint32_t nand_ready_raise_count;
    uint32_t nand_page_read_count;
    uint32_t nand_program_count;
    uint32_t nand_erase_count;
    uint32_t nand_last_cmd;
    uint32_t nand_last_page;
    uint32_t nand_last_column;
    uint32_t nand_last_block;
    char input_line[128];
    size_t input_line_len;
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

struct Bbk9588NandState {
    SysBusDevice parent_obj;

    MemoryRegion mmio;
    BlockBackend *blk;
    uint8_t cmd;
    uint8_t addr[5];
    unsigned addr_count;
    uint8_t read_buffer[4096];
    unsigned read_index;
    uint32_t read_page;
    uint32_t read_column;
    unsigned busy_reads;
    unsigned bch_busy_reads;
    uint32_t bch_status;
    uint32_t bch_done_status;
    uint8_t *data;
    gsize size;
    uint32_t page_stride;
    uint8_t program_buffer[BBK9588_NAND_STRIDE];
    uint32_t program_start;
    unsigned program_len;
    uint32_t program_page;
    uint32_t program_column;
    bool program_has_data;
    bool program_page_valid;
};

#define TYPE_BBK9588_NAND "bbk9588-nand"
OBJECT_DECLARE_SIMPLE_TYPE(Bbk9588NandState, BBK9588_NAND)

static uint32_t bbk9588_nand_page_stride(Bbk9588NandState *nand)
{
    if (nand && nand->page_stride) {
        return nand->page_stride;
    }
    return BBK9588_NAND_STRIDE;
}

static void bbk9588_nand_detect_geometry(Bbk9588NandState *nand)
{
    if (!nand || !nand->data || nand->size == 0) {
        return;
    }

    if (nand->size % BBK9588_NAND_STRIDE == 0) {
        nand->page_stride = BBK9588_NAND_STRIDE;
    } else if (nand->size % BBK9588_NAND_PAGE_SIZE == 0) {
        nand->page_stride = BBK9588_NAND_PAGE_SIZE;
    } else {
        nand->page_stride = BBK9588_NAND_STRIDE;
    }
}

static const Bbk9588MmioWindow bbk9588_mmio_windows[] = {
    { "bbk9588.extgpio",  0xb3010000, 0x10000, BBK9588_MMIO_EXTGPIO },
    { "bbk9588.msc",      0xb0021000, 0x1000, BBK9588_MMIO_GRAPHICS },
    { "bbk9588.uart",     0xb0030000, 0x1000, BBK9588_MMIO_UART },
    { "bbk9588.udc",      0xb3040000, 0x1000, BBK9588_MMIO_UDC },
    { "bbk9588.misc306",  0xb3060000, 0x1000, BBK9588_MMIO_MISC },
    { "bbk9588.lcd",      0xb0043000, 0x1000, BBK9588_MMIO_LCD },
};

static void bbk9588_touch_set_state(Bbk9588MachineState *board,
                                     uint16_t raw_x, uint16_t raw_y,
                                     bool down);
static void bbk9588_queue_input_event(Bbk9588MachineState *board,
                                      uint32_t kind, uint32_t arg0,
                                      uint32_t arg1, uint32_t arg2);
static void bbk9588_key_apply_host_input(Bbk9588MachineState *board,
                                         uint32_t key_code, bool down);
static void bbk9588_lcd_schedule_vblank(Bbk9588MachineState *board);

static void bbk9588_nand_realize(DeviceState *dev, Error **errp);
static void bbk9588_update_irq(Bbk9588MachineState *board);
static void bbk9588_touch_trace_update(Bbk9588MachineState *board,
                                       uint32_t reason);
static void bbk9588_progress_trace_sample(Bbk9588MachineState *board,
                                          uint32_t reason);
static void bbk9588_phys_write_le32(hwaddr addr, uint32_t value);

static const Property bbk9588_nand_properties[] = {
    DEFINE_PROP_DRIVE("drive", Bbk9588NandState, blk),
};

static void bbk9588_nand_class_init(ObjectClass *oc, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(oc);

    dc->realize = bbk9588_nand_realize;
    device_class_set_props(dc, bbk9588_nand_properties);
}

static void bbk9588_nand_instance_finalize(Object *obj)
{
    Bbk9588NandState *nand = BBK9588_NAND(obj);

    g_free(nand->data);
}

static const TypeInfo bbk9588_nand_typeinfo = {
    .name = TYPE_BBK9588_NAND,
    .parent = TYPE_SYS_BUS_DEVICE,
    .class_init = bbk9588_nand_class_init,
    .instance_finalize = bbk9588_nand_instance_finalize,
    .instance_size = sizeof(Bbk9588NandState),
};

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

static uint8_t bbk9588_phys_read_u8(hwaddr addr)
{
    uint8_t value;

    bbk9588_phys_read(addr, &value, sizeof(value));
    return value;
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

static unsigned bbk9588_uart_rx_trigger_level(Bbk9588MachineState *board)
{
    if (!(board->uart_fcr & BBK9588_UART_FCR_FME)) {
        return 1;
    }
    switch ((board->uart_fcr & BBK9588_UART_FCR_RDTR_MASK) >> 6) {
    case 0:
        return 1;
    case 1:
        return 4;
    case 2:
        return 8;
    default:
        return 15;
    }
}

static bool bbk9588_uart_rx_data_ready(Bbk9588MachineState *board)
{
    return board->uart_rx_len >= bbk9588_uart_rx_trigger_level(board);
}

static void bbk9588_uart_latch_thr_irq(Bbk9588MachineState *board)
{
    if ((board->uart_fcr & BBK9588_UART_FCR_UME) &&
        (board->uart_ier & BBK9588_UART_IER_TDRIE) &&
        (board->uart_status & BBK9588_UART_LSR_TDRQ)) {
        board->uart_thr_irq_latched = true;
    }
}

static uint8_t bbk9588_uart_iir_value(Bbk9588MachineState *board)
{
    uint8_t fifo_bits = (board->uart_fcr & BBK9588_UART_FCR_FME) ?
                        BBK9588_UART_IIR_FIFO : 0;

    if (!(board->uart_fcr & BBK9588_UART_FCR_UME)) {
        return fifo_bits | BBK9588_UART_IIR_NONE;
    }
    if ((board->uart_ier & BBK9588_UART_IER_RLSIE) &&
        (board->uart_status & BBK9588_UART_LSR_ERROR_MASK)) {
        return fifo_bits | BBK9588_UART_IIR_RLS;
    }
    if ((board->uart_ier & BBK9588_UART_IER_RDRIE) &&
        bbk9588_uart_rx_data_ready(board)) {
        return fifo_bits | BBK9588_UART_IIR_RDR;
    }
    if ((board->uart_ier & BBK9588_UART_IER_RTOIE) &&
        board->uart_rx_len != 0) {
        return fifo_bits | BBK9588_UART_IIR_RTO;
    }
    if (board->uart_thr_irq_latched &&
        (board->uart_ier & BBK9588_UART_IER_TDRIE) &&
        (board->uart_status & BBK9588_UART_LSR_TDRQ)) {
        return fifo_bits | BBK9588_UART_IIR_TDR;
    }
    if ((board->uart_ier & BBK9588_UART_IER_MSIE) &&
        (board->uart_msr & BBK9588_UART_MSR_CCTS)) {
        return fifo_bits | BBK9588_UART_IIR_MODEM;
    }
    return fifo_bits | BBK9588_UART_IIR_NONE;
}

static bool bbk9588_uart_irq_pending(Bbk9588MachineState *board)
{
    return (bbk9588_uart_iir_value(board) & BBK9588_UART_IIR_NONE) == 0;
}

static bool bbk9588_udc_irq_pending(Bbk9588MachineState *board)
{
    return ((board->udc_intr_in & board->udc_intr_in_enable &
             BBK9588_UDC_INTRIN_ENDPOINT_MASK) != 0) ||
           ((board->udc_intr_out & board->udc_intr_out_enable &
             BBK9588_UDC_INTROUT_ENDPOINT_MASK) != 0) ||
           ((board->udc_intr_usb & board->udc_intr_usb_enable &
             BBK9588_UDC_INTRUSBE_MASK) != 0);
}

static void bbk9588_sync_level_irq_sources(Bbk9588MachineState *board)
{
    bbk9588_sync_tcu_irq_sources(board);
    jz4740_intc_set_irq(board->intc, BBK9588_UART_IRQ,
                        bbk9588_uart_irq_pending(board));
    jz4740_intc_set_irq(board->intc, BBK9588_UDC_IRQ,
                        bbk9588_udc_irq_pending(board));
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

static void bbk9588_uart_sync_irq(Bbk9588MachineState *board)
{
    jz4740_intc_set_irq(board->intc, BBK9588_UART_IRQ,
                        bbk9588_uart_irq_pending(board));
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

static bool bbk9588_is_msc_window(Bbk9588MmioState *s)
{
    return s->window->kind == BBK9588_MMIO_GRAPHICS &&
           s->window->kseg1_base == 0xb0021000;
}

static void bbk9588_lcd_frame_source_changed(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    board->lcd_irq_status |= BBK9588_LCD_STATUS_FRAME_DONE;
    board->lcd_frame_chardev_sent_valid = false;
    board->lcd_scanout_not_before_ms =
        qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
        board->lcd_refresh_period_ms;
    bbk9588_lcd_schedule_vblank(board);
}

static void bbk9588_lcd_copy_framebuffer(Bbk9588MachineState *board)
{
    uint16_t width = 0;
    uint16_t height = 0;
    uint32_t fb_va = BBK9588_FRAMEBUFFER_VA;
    uint8_t enabled = 0;

    bbk9588_phys_read(BBK9588_LCD_MIRROR_CONFIG + 0x00, &width,
                      sizeof(width));
    bbk9588_phys_read(BBK9588_LCD_MIRROR_CONFIG + 0x04, &height,
                      sizeof(height));
    fb_va = bbk9588_phys_read_le32(BBK9588_LCD_MIRROR_CONFIG + 0xd8);
    enabled = bbk9588_phys_read_u8(BBK9588_LCD_MIRROR_CONFIG + 0xdc);

    if (width != BBK9588_LCD_WIDTH ||
        height != BBK9588_LCD_HEIGHT ||
        enabled == 0 ||
        !bbk9588_guest_ram_va_valid(fb_va, sizeof(board->lcd_framebuffer))) {
        jz4740_lcd_refresh_frame_source(board->lcd);
        if (!jz4740_lcd_get_frame_source(board->lcd, &fb_va) ||
            !bbk9588_guest_ram_va_valid(fb_va,
                                        sizeof(board->lcd_framebuffer))) {
            fb_va = BBK9588_FRAMEBUFFER_VA;
        }
    }
    if (!bbk9588_guest_ram_va_valid(fb_va, sizeof(board->lcd_framebuffer))) {
        fb_va = BBK9588_FRAMEBUFFER_VA;
    }

    bbk9588_phys_read(fb_va, board->lcd_framebuffer,
                      sizeof(board->lcd_framebuffer));
}

static void bbk9588_perf_maybe_send_metrics(Bbk9588MachineState *board,
                                            int64_t now_ms);

static bool bbk9588_lcd_send_frame(Bbk9588MachineState *board)
{
    uint32_t header[7];

    if (!qemu_chr_fe_backend_connected(&board->frame_chr)) {
        return false;
    }

    bbk9588_perf_maybe_send_metrics(
        board, qemu_clock_get_ms(QEMU_CLOCK_REALTIME));

    board->lcd_frame_seq++;
    header[0] = cpu_to_le32(BBK9588_FRAME_MAGIC);
    header[1] = cpu_to_le32(board->lcd_frame_seq);
    header[2] = cpu_to_le32(BBK9588_LCD_WIDTH);
    header[3] = cpu_to_le32(BBK9588_LCD_HEIGHT);
    header[4] = cpu_to_le32(BBK9588_LCD_STRIDE);
    header[5] = cpu_to_le32(BBK9588_FRAME_FORMAT_RGB565);
    header[6] = cpu_to_le32(BBK9588_LCD_BYTES);

    if (qemu_chr_fe_write_all(&board->frame_chr, (const uint8_t *)header,
                              sizeof(header)) < 0) {
        return false;
    }
    if (qemu_chr_fe_write_all(&board->frame_chr, board->lcd_framebuffer,
                              sizeof(board->lcd_framebuffer)) < 0) {
        return false;
    }
    return true;
}

static void bbk9588_audio_output(void *opaque, uint32_t sample_rate,
                                 const int16_t *samples, size_t frames)
{
    Bbk9588MachineState *board = opaque;
    uint32_t header[7];
    size_t payload_bytes = frames * 2u * sizeof(int16_t);

    if (frames == 0 || !qemu_chr_fe_backend_connected(&board->frame_chr)) {
        return;
    }
    board->audio_seq++;
    header[0] = cpu_to_le32(BBK9588_AUDIO_MAGIC);
    header[1] = cpu_to_le32(board->audio_seq);
    header[2] = cpu_to_le32(sample_rate);
    header[3] = cpu_to_le32(2u);
    header[4] = cpu_to_le32(2u * sizeof(int16_t));
    header[5] = cpu_to_le32(BBK9588_AUDIO_FORMAT_S16LE);
    header[6] = cpu_to_le32(payload_bytes);

    if (qemu_chr_fe_write_all(&board->frame_chr, (const uint8_t *)header,
                              sizeof(header)) < 0) {
        return;
    }
    qemu_chr_fe_write_all(&board->frame_chr, (const uint8_t *)samples,
                          payload_bytes);
}

static bool bbk9588_perf_send_metrics(Bbk9588MachineState *board,
                                      int64_t now_ms)
{
    uint32_t header[7];
    uint64_t payload[2];
    uint64_t insns = 0;

    if (!board->cpu || !qemu_chr_fe_backend_connected(&board->frame_chr)) {
        return false;
    }

    insns = qatomic_read(&board->cpu->env.bbk9588_guest_insn_count);
    board->perf_seq++;
    header[0] = cpu_to_le32(BBK9588_PERF_MAGIC);
    header[1] = cpu_to_le32(board->perf_seq);
    header[2] = cpu_to_le32(1);
    header[3] = cpu_to_le32(0);
    header[4] = cpu_to_le32(0);
    header[5] = cpu_to_le32(BBK9588_PERF_FORMAT_GUEST_INSNS);
    header[6] = cpu_to_le32(BBK9588_PERF_PAYLOAD_BYTES);
    payload[0] = cpu_to_le64(insns);
    payload[1] = cpu_to_le64((uint64_t)now_ms);

    if (qemu_chr_fe_write_all(&board->frame_chr, (const uint8_t *)header,
                              sizeof(header)) < 0) {
        return false;
    }
    if (qemu_chr_fe_write_all(&board->frame_chr, (const uint8_t *)payload,
                              sizeof(payload)) < 0) {
        return false;
    }

    if (board->aic) {
        JZ4740AICDiagnostics diagnostics;
        JZ4740DMACDiagnostics dmac_diagnostics;
        uint64_t audio_payload[BBK9588_AIC_PERF_WORDS];

        jz4740_aic_get_diagnostics(board->aic, &diagnostics);
        jz4740_dmac_get_diagnostics(board->dmac, &dmac_diagnostics);
        board->perf_seq++;
        header[0] = cpu_to_le32(BBK9588_PERF_MAGIC);
        header[1] = cpu_to_le32(board->perf_seq);
        header[2] = cpu_to_le32(1);
        header[3] = cpu_to_le32(0);
        header[4] = cpu_to_le32(0);
        header[5] = cpu_to_le32(BBK9588_PERF_FORMAT_AIC);
        header[6] = cpu_to_le32(BBK9588_AIC_PERF_PAYLOAD_BYTES);
        audio_payload[0] = cpu_to_le64(diagnostics.sample_rate);
        audio_payload[1] = cpu_to_le64(diagnostics.tx_fifo_level);
        audio_payload[2] = cpu_to_le64(diagnostics.rx_fifo_level);
        audio_payload[3] = cpu_to_le64(diagnostics.flags);
        audio_payload[4] = cpu_to_le64(diagnostics.aicfr);
        audio_payload[5] = cpu_to_le64(diagnostics.aiccr);
        audio_payload[6] = cpu_to_le64(diagnostics.cdccr1);
        audio_payload[7] = cpu_to_le64(diagnostics.cdccr2);
        audio_payload[8] = cpu_to_le64(diagnostics.tx_dma_samples);
        audio_payload[9] = cpu_to_le64(diagnostics.rx_dma_samples);
        audio_payload[10] = cpu_to_le64(diagnostics.output_frames);
        audio_payload[11] = cpu_to_le64(diagnostics.input_frames);
        audio_payload[12] = cpu_to_le64(diagnostics.underruns);
        audio_payload[13] = cpu_to_le64(diagnostics.overruns);
        audio_payload[14] = cpu_to_le64(dmac_diagnostics.audio_completion_count);
        audio_payload[15] = cpu_to_le64(dmac_diagnostics.audio_rearm_count);
        audio_payload[16] = cpu_to_le64(dmac_diagnostics.audio_last_rearm_gap_ns);
        audio_payload[17] = cpu_to_le64(dmac_diagnostics.audio_max_rearm_gap_ns);
        audio_payload[18] = cpu_to_le64(dmac_diagnostics.audio_total_rearm_gap_ns);
        audio_payload[19] = cpu_to_le64(dmac_diagnostics.audio_last_gap_underruns);
        audio_payload[20] = cpu_to_le64(dmac_diagnostics.audio_total_gap_underruns);
        audio_payload[21] = cpu_to_le64(dmac_diagnostics.audio_last_units);
        audio_payload[22] = cpu_to_le64(dmac_diagnostics.audio_completion_fifo);
        audio_payload[23] = cpu_to_le64(dmac_diagnostics.audio_rearm_fifo);

        if (qemu_chr_fe_write_all(&board->frame_chr,
                                  (const uint8_t *)header,
                                  sizeof(header)) < 0 ||
            qemu_chr_fe_write_all(&board->frame_chr,
                                  (const uint8_t *)audio_payload,
                                  sizeof(audio_payload)) < 0) {
            return false;
        }
    }
    return true;
}

static void bbk9588_perf_maybe_send_metrics(Bbk9588MachineState *board,
                                            int64_t now_ms)
{
    if (board->perf_last_send_ms != 0 &&
        now_ms - board->perf_last_send_ms < BBK9588_PERF_PERIOD_MS) {
        return;
    }
    if (bbk9588_perf_send_metrics(board, now_ms)) {
        board->perf_last_send_ms = now_ms;
    }
}

static bool bbk9588_lcd_frame_changed(Bbk9588MachineState *board)
{
    if (!board->lcd_last_frame_valid ||
        memcmp(board->lcd_framebuffer, board->lcd_last_framebuffer,
               sizeof(board->lcd_framebuffer)) != 0) {
        memcpy(board->lcd_last_framebuffer, board->lcd_framebuffer,
               sizeof(board->lcd_framebuffer));
        board->lcd_last_frame_valid = true;
        return true;
    }
    return false;
}

static void bbk9588_lcd_gfx_update(void *opaque)
{
    Bbk9588MachineState *board = opaque;
    bool changed;
    int64_t now = qemu_clock_get_ms(QEMU_CLOCK_REALTIME);

    bbk9588_lcd_copy_framebuffer(board);
    changed = bbk9588_lcd_frame_changed(board);
    if (changed) {
        board->lcd_irq_status |= BBK9588_LCD_STATUS_FRAME_DONE;
        jz4740_lcd_signal_frame_done(board->lcd);
        board->lcd_frame_chardev_sent_valid = false;
        board->lcd_frame_stable_not_before_ms = now;
    }
    bbk9588_perf_maybe_send_metrics(board, now);
    if (qemu_chr_fe_backend_connected(&board->frame_chr) &&
        !board->lcd_frame_chardev_sent_valid &&
        board->lcd_frame_stable_not_before_ms <= now) {
        board->lcd_frame_chardev_sent_valid = bbk9588_lcd_send_frame(board);
    }
    if (!changed) {
        return;
    }
    if (!board->lcd_console) {
        return;
    }
    if (!board->lcd_surface) {
        board->lcd_surface = qemu_create_displaysurface_from(
            BBK9588_LCD_WIDTH, BBK9588_LCD_HEIGHT, PIXMAN_r5g6b5,
            BBK9588_LCD_STRIDE, board->lcd_framebuffer);
        dpy_gfx_replace_surface(board->lcd_console, board->lcd_surface);
    }
    dpy_gfx_update(board->lcd_console, 0, 0, BBK9588_LCD_WIDTH,
                   BBK9588_LCD_HEIGHT);
}

static void bbk9588_lcd_invalidate(void *opaque)
{
    bbk9588_lcd_gfx_update(opaque);
}

static const GraphicHwOps bbk9588_lcd_ops = {
    .invalidate = bbk9588_lcd_invalidate,
    .gfx_update = bbk9588_lcd_gfx_update,
};

static void bbk9588_lcd_refresh_schedule(Bbk9588MachineState *board)
{
    if (!board->lcd_refresh_timer) {
        return;
    }
    timer_mod(board->lcd_refresh_timer,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
              board->lcd_refresh_period_ms);
}

static void bbk9588_lcd_schedule_vblank(Bbk9588MachineState *board)
{
    if (!board->lcd_refresh_timer) {
        return;
    }
    timer_mod(board->lcd_refresh_timer,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 1);
}

static void bbk9588_lcd_refresh_timer_cb(void *opaque)
{
    Bbk9588MachineState *board = opaque;
    bool wants_frame_chardev = board->frame_chardev && board->frame_chardev[0];
    int64_t now = qemu_clock_get_ms(QEMU_CLOCK_REALTIME);

    if (board->lcd_scanout_not_before_ms > now) {
        timer_mod(board->lcd_refresh_timer, board->lcd_scanout_not_before_ms);
        return;
    }

    bbk9588_lcd_gfx_update(board);
    bbk9588_perf_maybe_send_metrics(board, now);
    if (wants_frame_chardev && !board->lcd_frame_chardev_sent_valid) {
        now = qemu_clock_get_ms(QEMU_CLOCK_REALTIME);
        if (board->lcd_frame_stable_not_before_ms > now) {
            timer_mod(board->lcd_refresh_timer,
                      board->lcd_frame_stable_not_before_ms);
        } else {
            bbk9588_lcd_schedule_vblank(board);
        }
    } else {
        bbk9588_lcd_refresh_schedule(board);
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

static void bbk9588_touch_apply_host_input(Bbk9588MachineState *board,
                                           uint16_t raw_x,
                                           uint16_t raw_y,
                                           uint16_t x,
                                           uint16_t y,
                                           bool down)
{
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

static void bbk9588_key_apply_host_input(Bbk9588MachineState *board,
                                         uint32_t key_code, bool down)
{
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

static void bbk9588_lcd_write(Bbk9588MachineState *board, hwaddr offset,
                              uint32_t value)
{
    switch (offset) {
    case 0x0c:
        board->lcd_irq_status &= ~(value & BBK9588_LCD_STATUS_FRAME_DONE);
        break;
    case 0x00:
    case 0x04:
    case 0x08:
        /*
         * Keep the controller ready while firmware programs timing/control
         * registers, but do not synthesize a frame-done edge until DMA/frame
         * address registers point at guest RAM.
         */
        board->lcd_status |= 0x80u;
        break;
    default:
        break;
    }
}

static void bbk9588_nand_fill_read(Bbk9588NandState *nand,
                                   const uint8_t *data, size_t len)
{
    memset(nand->read_buffer, 0xff, sizeof(nand->read_buffer));
    memcpy(nand->read_buffer, data, MIN(len, sizeof(nand->read_buffer)));
    nand->read_index = 0;
}

static void bbk9588_nand_fill_read_id(Bbk9588NandState *nand)
{
    Bbk9588MachineState *board = bbk9588_active_board;
    uint8_t id[] = {
        BBK9588_NAND_ID_MAKER,
        BBK9588_NAND_ID_CODE,
        BBK9588_NAND_ID_BYTE2,
        BBK9588_NAND_ID_BYTE3,
        BBK9588_NAND_ID_BYTE4,
    };

    if (board) {
        id[1] = board->nand_id_code & 0xffu;
    }
    bbk9588_nand_fill_read(nand, id, sizeof(id));
}

static void bbk9588_nand_prepare_erased_page(Bbk9588NandState *nand)
{
    memset(nand->read_buffer, 0xff, sizeof(nand->read_buffer));
    nand->read_index = 0;
}

static bool bbk9588_nand_data_region_is_blank(const uint8_t *data,
                                              gsize size,
                                              uint32_t stride,
                                              uint32_t page,
                                              uint32_t column,
                                              uint32_t len)
{
    uint64_t offset = (uint64_t)page * stride + column;
    if (!data || column >= BBK9588_NAND_PAGE_SIZE ||
        offset + len > size) {
        return true;
    }

    for (uint32_t i = 0; i < len; i++) {
        if (data[offset + i] != 0xffu) {
            return false;
        }
    }
    return true;
}

static bool bbk9588_nand_page_region_is_blank(Bbk9588NandState *nand,
                                              uint32_t page,
                                              uint32_t column,
                                              uint32_t len)
{
    return bbk9588_nand_data_region_is_blank(
        nand ? nand->data : NULL,
        nand ? nand->size : 0,
        nand ? bbk9588_nand_page_stride(nand) : BBK9588_NAND_STRIDE,
        page, column, len);
}

static void bbk9588_nand_prepare_page_read(Bbk9588NandState *nand)
{
    uint32_t column;
    uint32_t page;
    uint32_t requested_page;
    uint64_t offset;
    uint32_t stride = bbk9588_nand_page_stride(nand);
    size_t copy_len = 0;
    bool request_blank = false;
    bool final_blank = true;
    uint32_t source = BBK9588_NAND_READ_SOURCE_RUNTIME;

    if (nand->addr_count < 5) {
        bbk9588_nand_prepare_erased_page(nand);
        return;
    }

    column = nand->addr[0] | (nand->addr[1] << 8);
    page = nand->addr[2] |
           (nand->addr[3] << 8) |
           (nand->addr[4] << 16);
    requested_page = page;
    nand->read_page = requested_page;
    nand->read_column = column;
    if (column < BBK9588_NAND_PAGE_SIZE) {
        request_blank = bbk9588_nand_page_region_is_blank(
            nand, page, column, BBK9588_NAND_PAGE_SIZE - column);
    }
    offset = (uint64_t)page * stride + column;

    memset(nand->read_buffer, 0xff, sizeof(nand->read_buffer));
    if (nand->data && column < stride && offset < nand->size) {
        copy_len = MIN((uint64_t)sizeof(nand->read_buffer),
                       (uint64_t)nand->size - offset);
        memcpy(nand->read_buffer, nand->data + offset, copy_len);
    }
    if (column < BBK9588_NAND_PAGE_SIZE) {
        final_blank = bbk9588_nand_data_region_is_blank(
            nand->data, nand->size, stride, page, column,
            BBK9588_NAND_PAGE_SIZE - column);
    }
    if (request_blank) {
        source |= BBK9588_NAND_READ_REQUEST_BLANK;
    }
    if (final_blank) {
        source |= BBK9588_NAND_READ_FINAL_BLANK;
    }
    if (bbk9588_active_board &&
        bbk9588_active_board->storage_trace_enabled &&
        column < stride) {
        uint32_t pc = 0;

        if (bbk9588_active_board->cpu) {
            pc = bbk9588_active_board->cpu->env.active_tc.PC & 0xffffffffu;
        }
        bbk9588_storage_trace_record(
            BBK9588_STORAGE_TRACE_NAND_READ | requested_page,
            ((source & 0xffu) << 24) | (page & 0x00ffffffu),
            copy_len >= 4 ? bbk9588_ldl_le(nand->read_buffer) : 0xffffffffu);
        bbk9588_storage_trace_record(
            BBK9588_STORAGE_TRACE_NAND_READ |
            BBK9588_STORAGE_TRACE_NAND_DETAIL |
            (requested_page & 0x03ffffffu),
            column,
            pc);
    }
    nand->read_index = 0;
}

static uint32_t bbk9588_nand_page_from_addr(Bbk9588NandState *nand)
{
    if (nand->addr_count < 5) {
        return 0;
    }
    return nand->addr[2] |
           (nand->addr[3] << 8) |
           (nand->addr[4] << 16);
}

static uint32_t bbk9588_nand_column_from_addr(Bbk9588NandState *nand)
{
    if (nand->addr_count < 2) {
        return 0;
    }
    return nand->addr[0] | (nand->addr[1] << 8);
}

static uint32_t bbk9588_nand_row_from_addr(Bbk9588NandState *nand)
{
    uint32_t row = 0;

    for (unsigned i = 0; i < MIN(nand->addr_count, 3u); i++) {
        row |= (uint32_t)nand->addr[i] << (i * 8);
    }
    return row;
}

static void bbk9588_nand_bch_start(Bbk9588NandState *nand,
                                   uint32_t done_status)
{
    if (!nand) {
        return;
    }

    nand->bch_status = 0;
    nand->bch_done_status = done_status;
    nand->bch_busy_reads = 1;
}

static uint32_t bbk9588_nand_bch_read_status(Bbk9588MachineState *board)
{
    Bbk9588NandState *nand = board ? board->nand_dev : NULL;

    if (!nand) {
        return 0;
    }
    if (nand->bch_busy_reads > 0) {
        nand->bch_busy_reads--;
        if (nand->bch_busy_reads == 0) {
            nand->bch_status = nand->bch_done_status ?
                               nand->bch_done_status :
                               BBK9588_BCH_STATUS_DECODE_DONE;
        }
        return 0;
    }
    return nand->bch_status;
}

static void bbk9588_nand_bch_ack_status(Bbk9588MachineState *board,
                                         uint32_t value)
{
    Bbk9588NandState *nand = board ? board->nand_dev : NULL;

    if (!nand) {
        return;
    }
    nand->bch_status &= value | ~BBK9588_BCH_STATUS_W0C_MASK;
}

static uint32_t bbk9588_nand_nfcsr_write_value(uint32_t value)
{
    return value & BBK9588_NAND_NFCSR_RW_MASK;
}

static uint32_t bbk9588_nand_nfeccr_write_value(Bbk9588MachineState *board,
                                                uint32_t value)
{
    Bbk9588NandState *nand = board ? board->nand_dev : NULL;

    if (value & BBK9588_NAND_NFECCR_ERST) {
        if (nand) {
            nand->bch_status = 0;
            nand->bch_done_status = 0;
            nand->bch_busy_reads = 0;
        }
        return value & BBK9588_NAND_NFECCR_RW_MASK &
               ~BBK9588_NAND_NFECCR_ERST;
    }

    return value & BBK9588_NAND_NFECCR_RW_MASK;
}

static bool bbk9588_nand_control_window(Bbk9588MmioState *s)
{
    return s->window->kind == BBK9588_MMIO_EXTGPIO &&
           s->window->kseg1_base == 0xb3010000;
}

static uint32_t bbk9588_nand_control_read(Bbk9588MachineState *board,
                                          Bbk9588MmioState *s,
                                          hwaddr offset)
{
    switch (offset) {
    case BBK9588_NAND_NFCSR_OFF:
        return s->regs[offset / sizeof(uint32_t)] &
               BBK9588_NAND_NFCSR_RW_MASK;
    case BBK9588_NAND_NFECCR_OFF:
        return s->regs[offset / sizeof(uint32_t)] &
               BBK9588_NAND_NFECCR_RW_MASK;
    case BBK9588_NAND_NFECC_OFF:
        return s->regs[offset / sizeof(uint32_t)] & 0x00ffffffu;
    case BBK9588_NAND_NFINTS_OFF:
        return bbk9588_nand_bch_read_status(board);
    default:
        return s->regs[offset / sizeof(uint32_t)];
    }
}

static void bbk9588_nand_control_write(Bbk9588MachineState *board,
                                       Bbk9588MmioState *s,
                                       hwaddr offset,
                                       uint32_t value)
{
    uint32_t index = offset / sizeof(uint32_t);

    switch (offset) {
    case BBK9588_NAND_NFCSR_OFF:
        s->regs[index] = bbk9588_nand_nfcsr_write_value(value);
        break;
    case BBK9588_NAND_NFECCR_OFF:
        s->regs[index] = bbk9588_nand_nfeccr_write_value(board, value);
        if (board && board->nand_dev &&
            (s->regs[index] & BBK9588_NAND_NFECCR_ECCE)) {
            bbk9588_nand_bch_start(
                board->nand_dev,
                (s->regs[index] & BBK9588_NAND_NFECCR_ENCE) ?
                BBK9588_BCH_STATUS_ENCODE_DONE :
                BBK9588_BCH_STATUS_DECODE_DONE);
        }
        break;
    case BBK9588_NAND_NFECC_OFF:
        break;
    case BBK9588_NAND_NFINTS_OFF:
        bbk9588_nand_bch_ack_status(board, value);
        break;
    case BBK9588_NAND_NFINTE_OFF:
        s->regs[index] = value & BBK9588_BCH_STATUS_W0C_MASK;
        break;
    default:
        s->regs[index] = value;
        break;
    }
}

static void bbk9588_nand_begin_program(Bbk9588NandState *nand)
{
    memset(nand->program_buffer, 0xff, sizeof(nand->program_buffer));
    nand->program_start = BBK9588_NAND_STRIDE;
    nand->program_len = 0;
    nand->program_page = 0;
    nand->program_column = 0;
    nand->program_has_data = false;
    nand->program_page_valid = false;
}

static void bbk9588_nand_append_program_data(Bbk9588NandState *nand,
                                             uint64_t value, unsigned size)
{
    unsigned max_size = MIN(size, 4u);

    if (nand->cmd != 0x80 && nand->cmd != 0x85) {
        return;
    }
    for (unsigned i = 0; i < max_size; i++) {
        if (nand->program_column >= sizeof(nand->program_buffer)) {
            return;
        }
        if (!nand->program_has_data) {
            nand->program_start = nand->program_column;
        }
        nand->program_buffer[nand->program_column++] =
            (value >> (i * 8)) & 0xff;
        nand->program_has_data = true;
        if (nand->program_column > nand->program_len) {
            nand->program_len = nand->program_column;
        }
    }
}

static void bbk9588_nand_backend_update(Bbk9588NandState *nand,
                                         uint64_t offset, uint64_t len)
{
    uint64_t write_start;
    uint64_t write_end;
    BlockBackend *blk = nand->blk;
    int ret;

    if (!blk || !blk_is_writable(blk) || len == 0) {
        return;
    }

    write_start = QEMU_ALIGN_DOWN(offset, BDRV_SECTOR_SIZE);
    write_end = QEMU_ALIGN_UP(offset + len, BDRV_SECTOR_SIZE);
    if (write_start >= nand->size) {
        return;
    }
    if (write_end > nand->size) {
        write_end = nand->size;
    }

    ret = blk_pwrite(blk, write_start, write_end - write_start,
                     nand->data + write_start, 0);
    if (ret < 0) {
        error_report("bbk9588: could not update NAND offset=0x%" PRIx64
                     " length=0x%" PRIx64 ": %s",
                     write_start, write_end - write_start, strerror(-ret));
    }
}

static void bbk9588_nand_commit_program(Bbk9588NandState *nand)
{
    uint64_t page_offset;
    uint32_t column;
    uint32_t page;
    uint32_t stride = bbk9588_nand_page_stride(nand);
    uint32_t write_start;
    unsigned limit;

    if (!nand->data || !nand->program_page_valid ||
        !nand->program_has_data) {
        nand->program_start = BBK9588_NAND_STRIDE;
        nand->program_len = 0;
        nand->program_has_data = false;
        return;
    }

    page = nand->program_page;
    write_start = MIN(nand->program_start, (uint32_t)nand->program_len);
    column = write_start;
    page_offset = (uint64_t)page * stride;
    if (page_offset >= nand->size || column >= stride) {
        nand->program_start = BBK9588_NAND_STRIDE;
        nand->program_len = 0;
        nand->program_has_data = false;
        return;
    }

    if (nand->program_len <= column) {
        nand->program_start = BBK9588_NAND_STRIDE;
        nand->program_len = 0;
        nand->program_has_data = false;
        return;
    }
    limit = MIN(nand->program_len - column, stride - column);
    if (page_offset + column + limit > nand->size) {
        limit = nand->size - page_offset - column;
    }
    for (unsigned i = 0; i < limit; i++) {
        nand->data[page_offset + column + i] &=
            nand->program_buffer[column + i];
    }
    bbk9588_nand_backend_update(nand, page_offset + column, limit);
    if (bbk9588_active_board) {
        bbk9588_active_board->nand_program_count++;
        bbk9588_active_board->nand_last_page = page;
        bbk9588_active_board->nand_last_column = column;
    }
    if (bbk9588_active_board &&
        bbk9588_active_board->storage_trace_enabled &&
        column < BBK9588_NAND_PAGE_SIZE) {
        uint32_t first_word = 0xffffffffu;

        if (limit >= 4) {
            first_word = (uint32_t)nand->data[page_offset + column] |
                         ((uint32_t)nand->data[page_offset + column + 1] << 8) |
                         ((uint32_t)nand->data[page_offset + column + 2] << 16) |
                         ((uint32_t)nand->data[page_offset + column + 3] << 24);
        }
        bbk9588_storage_trace_record(BBK9588_STORAGE_TRACE_NAND_PROGRAM | page,
                                     column, first_word);
    }
    if (page == BBK9588_NAND_TARGET_PAGE &&
        column < BBK9588_NAND_PAGE_SIZE) {
        uint32_t first_word = 0xffffffffu;

        if (limit >= 4) {
            first_word = (uint32_t)nand->data[page_offset + column] |
                         ((uint32_t)nand->data[page_offset + column + 1] << 8) |
                         ((uint32_t)nand->data[page_offset + column + 2] << 16) |
                         ((uint32_t)nand->data[page_offset + column + 3] << 24);
        }
        bbk9588_nand_target_trace_record(
            BBK9588_NAND_TARGET_EVENT_PROGRAM, page, column, first_word);
    }
    if (bbk9588_active_board) {
        bbk9588_nand_raise_ready(bbk9588_active_board);
    }
    nand->program_page = page;
    nand->program_column = column;
    nand->program_start = BBK9588_NAND_STRIDE;
    nand->program_len = 0;
    nand->program_has_data = false;
}

static void bbk9588_nand_commit_erase(Bbk9588NandState *nand)
{
    uint32_t row;
    uint32_t block_start;

    if (!nand->data || nand->addr_count < 2) {
        return;
    }
    row = bbk9588_nand_row_from_addr(nand);
    block_start = row & ~(BBK9588_NAND_PAGES_PER_BLOCK - 1u);
    for (unsigned page = block_start;
         page < block_start + BBK9588_NAND_PAGES_PER_BLOCK;
         page++) {
        uint32_t stride = bbk9588_nand_page_stride(nand);
        uint64_t offset = (uint64_t)page * stride;
        uint64_t len;

        if (offset >= nand->size) {
            break;
        }
        len = MIN((uint64_t)stride, (uint64_t)nand->size - offset);
        memset(nand->data + offset, 0xff, len);
        bbk9588_nand_backend_update(nand, offset, len);
    }
    if (bbk9588_active_board &&
        bbk9588_active_board->storage_trace_enabled) {
        bbk9588_storage_trace_record(
            BBK9588_STORAGE_TRACE_NAND_ERASE | block_start,
            BBK9588_NAND_PAGES_PER_BLOCK,
            0xffffffffu);
    }
    if (block_start == BBK9588_NAND_TARGET_BLOCK) {
        bbk9588_nand_target_trace_record(
            BBK9588_NAND_TARGET_EVENT_ERASE, block_start, row,
            BBK9588_NAND_PAGES_PER_BLOCK);
    }
    if (bbk9588_active_board) {
        bbk9588_active_board->nand_erase_count++;
        bbk9588_active_board->nand_last_page = row;
        bbk9588_active_board->nand_last_block = block_start;
        bbk9588_nand_raise_ready(bbk9588_active_board);
    }
}

static uint32_t bbk9588_nand_read_data(Bbk9588NandState *nand,
                                       unsigned size)
{
    uint32_t value = 0xffffffffu;
    unsigned max_size = MIN(size, 4u);

    for (unsigned i = 0; i < max_size; i++) {
        uint8_t byte = 0xff;
        unsigned index = nand->read_index;

        if (index < sizeof(nand->read_buffer)) {
            byte = nand->read_buffer[index];
        }
        if (bbk9588_active_board &&
            bbk9588_active_board->storage_trace_enabled &&
            ((nand->read_page == 0x2c30cu &&
              index >= 0x6b8u && index < 0x6c0u) ||
             (nand->read_page == 0x2c30fu &&
              index >= 0x598u && index < 0x5a0u))) {
            uint32_t pc = 0;
            uint32_t s0 = 0;
            if (bbk9588_active_board->cpu) {
                pc = bbk9588_active_board->cpu->env.active_tc.PC &
                     0xffffffffu;
                s0 = bbk9588_active_board->cpu->env.active_tc.gpr[16] &
                     0xffffffffu;
            }
            error_report("bbk9588-nand-data page=0x%06x col=0x%03x "
                         "idx=0x%03x byte=%02x pc=0x%08x s0=0x%08x",
                         nand->read_page, nand->read_column, index, byte,
                         pc, s0);
        }
        nand->read_index++;
        value = (value & ~(0xffu << (i * 8))) | ((uint32_t)byte << (i * 8));
    }
    return value;
}

static uint64_t bbk9588_nand_read(Bbk9588NandState *nand,
                                  hwaddr offset, unsigned size)
{
    switch (offset) {
    case 0x00000:
        return bbk9588_nand_read_data(nand, size);
    default:
        return 0xffffffffu;
    }
}

static void bbk9588_nand_command(Bbk9588NandState *nand, uint8_t command)
{
    static const uint8_t status[] = { 0x40, 0xff, 0xff, 0xff };
    Bbk9588MachineState *board = bbk9588_active_board;

    nand->cmd = command;
    if (board) {
        board->nand_last_cmd = command;
    }
    if (command == 0x00 || command == 0x60 || command == 0x80 ||
        command == 0x85 ||
        command == 0x90 || command == 0xff) {
        nand->addr_count = 0;
        nand->read_index = 0;
    }

    switch (command) {
    case 0x30:
        bbk9588_nand_prepare_page_read(nand);
        nand->busy_reads = 1;
        bbk9588_nand_bch_start(nand, BBK9588_BCH_STATUS_DECODE_DONE);
        if (board) {
            board->nand_page_read_count++;
            board->nand_last_page = bbk9588_nand_page_from_addr(nand);
            board->nand_last_column = bbk9588_nand_column_from_addr(nand);
            bbk9588_nand_raise_ready(board);
        }
        break;
    case 0x35:
        bbk9588_nand_prepare_page_read(nand);
        memcpy(nand->program_buffer, nand->read_buffer,
               sizeof(nand->program_buffer));
        nand->program_len = sizeof(nand->program_buffer);
        nand->program_column = 0;
        nand->program_has_data = true;
        nand->program_page_valid = false;
        nand->busy_reads = 1;
        bbk9588_nand_bch_start(nand, BBK9588_BCH_STATUS_DECODE_DONE);
        if (board) {
            board->nand_page_read_count++;
            board->nand_last_page = bbk9588_nand_page_from_addr(nand);
            board->nand_last_column = bbk9588_nand_column_from_addr(nand);
            bbk9588_nand_raise_ready(board);
        }
        break;
    case 0x80:
        bbk9588_nand_begin_program(nand);
        break;
    case 0x85:
        break;
    case 0x10:
        bbk9588_nand_commit_program(nand);
        nand->busy_reads = 1;
        bbk9588_nand_bch_start(nand, BBK9588_BCH_STATUS_DECODE_DONE);
        break;
    case 0xd0:
        bbk9588_nand_commit_erase(nand);
        nand->busy_reads = 1;
        bbk9588_nand_bch_start(nand, BBK9588_BCH_STATUS_DECODE_DONE);
        nand->addr_count = 0;
        nand->read_index = 0;
        break;
    case 0x70:
        bbk9588_nand_fill_read(nand, status, sizeof(status));
        break;
    case 0x90:
        bbk9588_nand_fill_read_id(nand);
        break;
    case 0xff:
        nand->busy_reads = 0;
        bbk9588_nand_prepare_erased_page(nand);
        break;
    default:
        break;
    }
}

static void bbk9588_nand_address(Bbk9588NandState *nand, uint8_t value)
{
    if (nand->addr_count < ARRAY_SIZE(nand->addr)) {
        nand->addr[nand->addr_count++] = value;
    }
    if (nand->cmd == 0x90 && nand->addr_count == 1 &&
        nand->addr[0] == 0) {
        bbk9588_nand_fill_read_id(nand);
    }
    if (nand->cmd == 0x80 && nand->addr_count >= 5) {
        nand->program_column = bbk9588_nand_column_from_addr(nand);
        nand->program_page = bbk9588_nand_page_from_addr(nand);
        nand->program_page_valid = true;
    }
    if (nand->cmd == 0x85 && nand->addr_count >= 5) {
        nand->program_column = bbk9588_nand_column_from_addr(nand);
        nand->program_page = bbk9588_nand_page_from_addr(nand);
        nand->program_page_valid = true;
    } else if (nand->cmd == 0x85 && nand->addr_count >= 2) {
        nand->program_column = bbk9588_nand_column_from_addr(nand);
    }
}

static void bbk9588_nand_write(Bbk9588NandState *nand, hwaddr offset,
                               uint64_t value, unsigned size)
{
    switch (offset) {
    case 0x00000:
        bbk9588_nand_append_program_data(nand, value, size);
        break;
    case 0x08000:
        if (size != 1) {
            break;
        }
        bbk9588_nand_command(nand, value & 0xff);
        break;
    case 0x10000:
        if (size != 1) {
            break;
        }
        bbk9588_nand_address(nand, value & 0xff);
        break;
    default:
        break;
    }
}

static uint64_t bbk9588_nand_mmio_read(void *opaque,
                                       hwaddr offset, unsigned size)
{
    Bbk9588NandState *nand = opaque;

    return bbk9588_nand_read(nand, offset, size);
}

static void bbk9588_nand_mmio_write(void *opaque, hwaddr offset,
                                    uint64_t value, unsigned size)
{
    Bbk9588NandState *nand = opaque;

    bbk9588_nand_write(nand, offset, value, size);
}

static const MemoryRegionOps bbk9588_nand_ops = {
    .read = bbk9588_nand_mmio_read,
    .write = bbk9588_nand_mmio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
};

static void bbk9588_nand_realize(DeviceState *dev, Error **errp)
{
    Bbk9588NandState *nand = BBK9588_NAND(dev);

    memory_region_init_io(&nand->mmio, OBJECT(dev), &bbk9588_nand_ops, nand,
                          "bbk9588.nand", 0x20000);
    sysbus_init_mmio(SYS_BUS_DEVICE(dev), &nand->mmio);
}

static void bbk9588_create_nand_device(Bbk9588MachineState *board)
{
    DeviceState *dev = qdev_new(TYPE_BBK9588_NAND);
    DriveInfo *dinfo = drive_get(IF_MTD, 0, 0);

    if (dinfo) {
        qdev_prop_set_drive_err(dev, "drive", blk_by_legacy_dinfo(dinfo),
                                &error_fatal);
    }
    sysbus_realize(SYS_BUS_DEVICE(dev), &error_fatal);
    sysbus_mmio_map(SYS_BUS_DEVICE(dev), 0, KSEG1_TO_PHYS(0xb8000000));
    board->nand_dev = BBK9588_NAND(dev);
    board->nand_dev->cmd = 0xff;
    board->nand_dev->addr_count = 0;
    board->nand_dev->busy_reads = 0;
    board->nand_dev->bch_busy_reads = 0;
    board->nand_dev->bch_status = BBK9588_BCH_STATUS_DECODE_DONE;
    board->nand_dev->bch_done_status = BBK9588_BCH_STATUS_DECODE_DONE;
    board->nand_dev->page_stride = BBK9588_NAND_STRIDE;
    board->nand_dev->program_len = 0;
    board->nand_dev->program_page = 0;
    board->nand_dev->program_column = 0;
    board->nand_dev->program_has_data = false;
    board->nand_dev->program_page_valid = false;
    bbk9588_nand_prepare_erased_page(board->nand_dev);
}

static void bbk9588_load_nand_image(Bbk9588MachineState *board)
{
    Bbk9588NandState *nand = board->nand_dev;
    GError *err = NULL;
    gchar *data = NULL;
    gsize size = 0;
    const char *image = board->nand_image;
    BlockBackend *blk = nand ? nand->blk : NULL;

    if (blk) {
        int64_t blk_len;
        uint64_t perm;
        int ret;

        perm = BLK_PERM_CONSISTENT_READ |
               (blk_supports_write_perm(blk) ? BLK_PERM_WRITE : 0);
        ret = blk_set_perm(blk, perm, BLK_PERM_ALL, &error_fatal);
        if (ret < 0) {
            exit(1);
        }

        blk_len = blk_getlength(blk);
        if (blk_len <= 0) {
            error_report("bbk9588: invalid MTD NAND image size");
            exit(1);
        }

        data = g_malloc(blk_len);
        if (blk_pread(blk, 0, blk_len, data, 0) < 0) {
            error_report("bbk9588: could not read MTD NAND image");
            g_free(data);
            exit(1);
        }
        nand->data = (uint8_t *)data;
        nand->size = blk_len;
        bbk9588_nand_detect_geometry(nand);
        info_report("bbk9588: loaded MTD NAND image (%" PRId64
                    " bytes, page_stride=%u)",
                    blk_len, nand->page_stride);
        return;
    }

    if (!image) {
        info_report("bbk9588: no nand-image supplied; CS0 returns erased data");
        return;
    }

    if (!g_file_get_contents(image, &data, &size, &err)) {
        error_report("bbk9588: could not load NAND image '%s': %s",
                     image, err ? err->message : "unknown error");
        g_clear_error(&err);
        exit(1);
    }

    nand->data = (uint8_t *)data;
    nand->size = size;
    bbk9588_nand_detect_geometry(nand);
    info_report("bbk9588: loaded NAND image '%s' (%" G_GSIZE_FORMAT
                " bytes, page_stride=%u)",
                image, size, nand->page_stride);
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
    uint32_t stride = bbk9588_nand_page_stride(nand);
    uint64_t spare_off = page * stride + BBK9588_NAND_PAGE_SIZE;

    if (!nand || !nand->data) {
        return false;
    }
    if (stride < BBK9588_NAND_STRIDE) {
        return true;
    }
    if (spare_off + 5 > nand->size) {
        return false;
    }

    return nand->data[spare_off + 2] == 0 ||
           nand->data[spare_off + 3] == 0 ||
           nand->data[spare_off + 4] == 0;
}

static bool bbk9588_bootrom_nand_area_has_valid_page(Bbk9588NandState *nand,
                                                     uint32_t nand_addr,
                                                     uint32_t length)
{
    uint32_t stride = bbk9588_nand_page_stride(nand);
    uint64_t first_page;
    uint64_t last_page;

    if (!nand || !nand->data || length == 0) {
        return false;
    }

    first_page = nand_addr / BBK9588_NAND_PAGE_SIZE;
    last_page = ((uint64_t)nand_addr + length - 1) /
                BBK9588_NAND_PAGE_SIZE;
    for (uint64_t page = first_page; page <= last_page; page++) {
        uint64_t page_off = page * stride;

        if (page_off + BBK9588_NAND_PAGE_SIZE > nand->size) {
            return false;
        }
        if (bbk9588_bootrom_nand_page_valid(nand, page)) {
            return true;
        }
    }
    return false;
}

static bool bbk9588_bootrom_copy_nand_data(Bbk9588NandState *nand,
                                           uint32_t nand_addr,
                                           uint32_t load_phys,
                                           uint32_t length,
                                           uint32_t *copied_out)
{
    uint32_t stride = bbk9588_nand_page_stride(nand);
    uint32_t copied = 0;

    while (copied < length) {
        uint32_t data_addr = nand_addr + copied;
        uint32_t page = data_addr / BBK9588_NAND_PAGE_SIZE;
        uint32_t column = data_addr % BBK9588_NAND_PAGE_SIZE;
        uint32_t page_copy = MIN(BBK9588_NAND_PAGE_SIZE - column,
                                 length - copied);
        uint64_t page_off = (uint64_t)page * stride + column;

        if (!nand->data || page_off + page_copy > nand->size) {
            return false;
        }
        if (!bbk9588_bootrom_nand_page_valid(nand, page)) {
            break;
        }
        cpu_physical_memory_write(load_phys + copied,
                                  nand->data + page_off,
                                  page_copy);
        copied += page_copy;
    }
    if (copied_out) {
        *copied_out = copied;
    }
    return copied > 0;
}

static bool bbk9588_bootrom_nand_range_erased(Bbk9588NandState *nand,
                                              uint32_t nand_addr,
                                              uint32_t length)
{
    uint32_t stride = bbk9588_nand_page_stride(nand);

    if (!nand || !nand->data || length == 0) {
        return true;
    }
    for (uint32_t checked = 0; checked < length; ) {
        uint32_t data_addr = nand_addr + checked;
        uint32_t page = data_addr / BBK9588_NAND_PAGE_SIZE;
        uint32_t column = data_addr % BBK9588_NAND_PAGE_SIZE;
        uint32_t page_check = MIN(BBK9588_NAND_PAGE_SIZE - column,
                                  length - checked);
        uint64_t page_off = (uint64_t)page * stride + column;

        if (page_off + page_check > nand->size) {
            return true;
        }
        for (uint32_t index = 0; index < page_check; index++) {
            if (nand->data[page_off + index] != 0xff) {
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
    uint32_t copied = 0;

    if (!nand || !nand->data ||
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
    uint32_t stride;
    uint32_t boot_page;
    uint32_t load_phys;
    uint32_t boot_size;

    if (!nand || !nand->data || nand->size == 0) {
        return false;
    }

    stride = bbk9588_nand_page_stride(nand);
    boot_page = board->bootrom_nand_page;
    load_phys = board->firmware_phys;
    boot_size = board->bootrom_size;
    if ((uint64_t)boot_page * stride + BBK9588_NAND_PAGE_SIZE > nand->size) {
        error_report("bbk9588: BootROM page 0x%08x is outside NAND image",
                     boot_page);
        exit(1);
    }

    for (uint32_t copied = 0; copied < boot_size; ) {
        uint32_t page_copy = MIN(BBK9588_NAND_PAGE_SIZE, boot_size - copied);
        uint64_t page_off = (uint64_t)boot_page * stride;

        if (page_off + page_copy > nand->size) {
            error_report("bbk9588: BootROM payload exceeds NAND image");
            exit(1);
        }
        cpu_physical_memory_write(load_phys + copied,
                                  nand->data + page_off,
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

    if (!board || !board->bootrom_nand_enabled) {
        return false;
    }
    if (!nand || !nand->data || nand->size == 0) {
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

static uint32_t bbk9588_mmio_extract32(uint32_t value, hwaddr offset,
                                       unsigned size)
{
    value >>= (offset & 3) * 8;
    switch (size) {
    case 1:
        return value & 0xffu;
    case 2:
        return value & 0xffffu;
    default:
        return value;
    }
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

static bool bbk9588_msc_dma_transfer(Bbk9588MachineState *board,
                                     unsigned channel, uint32_t source,
                                     uint32_t target, uint32_t words)
{
    bool read_pending = board->msc_read_pending;
    bool write_pending = board->msc_write_pending;
    uint32_t dma_phys;
    uint32_t bytes;
    uint32_t sectors;
    uint8_t *buf;
    bool ok = true;

    if ((!read_pending && !write_pending) ||
        (read_pending && channel != 0) || (write_pending && channel != 1)) {
        return false;
    }
    dma_phys = (read_pending ? target : source) & 0x1fffffff;
    bbk9588_storage_trace_record(
        BBK9588_STORAGE_TRACE_DMAC_TRANSFER |
        (read_pending ? 1u : 2u),
        words, dma_phys);
    board->msc_last_dma_phys = dma_phys;
    board->msc_last_dma_words = words;
    board->msc_dma_complete_count++;
    bytes = words * sizeof(uint32_t);
    if (bytes == 0) {
        board->msc_read_pending = false;
        board->msc_write_pending = false;
        return true;
    }
    sectors = (bytes + 511u) / 512u;
    if (sectors == 0 || sectors > 128) {
        return false;
    }

    buf = g_malloc0(sectors * 512u);
    if (read_pending) {
        /* No removable MSC medium is attached by default. */
        bbk9588_msc_trace_record(BBK9588_MSC_TRACE_READ,
                                 board->msc_read_lba, dma_phys, bytes,
                                 board->msc_last_cmd, board->msc_last_arg,
                                 ok ? bbk9588_ldl_le(buf) : 0xffffffffu);
        if (ok) {
            cpu_physical_memory_write(dma_phys, buf, MIN(bytes, sectors * 512u));
        }
    } else {
        cpu_physical_memory_read(dma_phys, buf, MIN(bytes, sectors * 512u));
        bbk9588_msc_trace_record(BBK9588_MSC_TRACE_WRITE,
                                 board->msc_write_lba, dma_phys, bytes,
                                 board->msc_last_cmd, board->msc_last_arg,
                                 bbk9588_ldl_le(buf));
    }
    board->msc_read_pending = false;
    board->msc_write_pending = false;
    if (ok) {
        board->msc_data_ready = true;
    }
    g_free(buf);
    return true;
}

static void bbk9588_msc_complete_dma(Bbk9588MmioState *s)
{
    Bbk9588MachineState *board = s->board;
    bool read_pending = board->msc_read_pending;
    bool write_pending = board->msc_write_pending;
    unsigned channel = read_pending ? 0 : 1;
    uint32_t source;
    uint32_t target;
    uint32_t words;

    if (!read_pending && !write_pending) {
        s->regs[0x10008 / sizeof(uint32_t)] = 0;
        s->regs[0x10028 / sizeof(uint32_t)] = 0;
        return;
    }
    source = write_pending ? s->regs[0x10020 / sizeof(uint32_t)] : 0;
    target = read_pending ? s->regs[0x10004 / sizeof(uint32_t)] : 0;
    words = s->regs[(read_pending ? 0x10008 : 0x10028) /
                    sizeof(uint32_t)];
    if (!bbk9588_msc_dma_transfer(board, channel, source, target, words)) {
        return;
    }
    if (read_pending) {
        s->regs[0x10008 / sizeof(uint32_t)] = 0;
        s->regs[0x10010 / sizeof(uint32_t)] =
            (s->regs[0x10010 / sizeof(uint32_t)] & ~0x00000001u) |
            0x00000008u;
    } else {
        s->regs[0x10028 / sizeof(uint32_t)] = 0;
        s->regs[0x10030 / sizeof(uint32_t)] =
            (s->regs[0x10030 / sizeof(uint32_t)] & ~0x00000001u) |
            0x00000008u;
    }
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
    JZ4740SADCDiagnostics sadc_diag;
    JZ4740TCUDiagnostics tcu_diag;

    if (!board || !board->touch_trace_enabled ||
        !bbk9588_guest_ram_va_valid(BBK9588_TOUCH_TRACE_VA, 0x154)) {
        return;
    }

    uint32_t pc = board->cpu ? board->cpu->env.active_tc.PC : 0;

    jz4740_gpio_get_diagnostics(board->gpio, &gpio_diag);
    jz4740_intc_get_diagnostics(board->intc, &intc_diag);
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
                            board->msc_read_pending ? 1u : 0u);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xcc,
                            board->msc_write_pending ? 1u : 0u);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xd0,
                            board->msc_data_ready ? 1u : 0u);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xd4,
                            board->msc_read_lba);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xd8,
                            board->msc_write_lba);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xdc,
                            board->msc_dma_complete_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xe0,
                            board->msc_last_cmd);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xe4,
                            board->msc_last_arg);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xe8,
                            board->msc_last_dma_phys);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xec,
                            board->msc_last_dma_words);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xf0,
                            board->nand_ready_raise_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xf4,
                            board->nand_page_read_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xf8,
                            board->nand_program_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0xfc,
                            board->nand_erase_count);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x100,
                            board->nand_last_cmd);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x104,
                            board->nand_last_page);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x108,
                            board->nand_last_column);
    bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x10c,
                            board->nand_last_block);
    if (board->nand_dev) {
        bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x110,
                                board->nand_dev->busy_reads);
        bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x114,
                                board->nand_dev->bch_busy_reads);
        bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x118,
                                board->nand_dev->addr_count);
        bbk9588_phys_write_le32(BBK9588_TOUCH_TRACE_VA + 0x11c,
                                gpio_diag.flag[JZ4740_GPIO_PORT_C]);
    }
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
        board->nand_dev && board->nand_dev->busy_reads > 0) {
        board->nand_dev->busy_reads--;
        level &= ~0x40000000;
    }
    return level;
}

static int bbk9588_uart_can_read(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    return BBK9588_UART_FIFO_SIZE - board->uart_rx_len;
}

static void bbk9588_uart_update_lsr(Bbk9588MachineState *board)
{
    uint32_t sticky = board->uart_status & BBK9588_UART_LSR_ERROR_MASK;

    board->uart_status = sticky | BBK9588_UART_LSR_RESET;
    if (board->uart_rx_len != 0) {
        board->uart_status |= BBK9588_UART_LSR_DRY;
    }
    if ((board->uart_fcr & BBK9588_UART_FCR_FME) &&
        (sticky & (BBK9588_UART_LSR_OVER | BBK9588_UART_LSR_PARER |
                   BBK9588_UART_LSR_FMER | BBK9588_UART_LSR_BI))) {
        board->uart_status |= BBK9588_UART_LSR_FIFOE;
    }
}

static void bbk9588_uart_push_rx(Bbk9588MachineState *board, uint8_t value)
{
    if (board->uart_rx_len < BBK9588_UART_FIFO_SIZE) {
        unsigned tail = (board->uart_rx_head + board->uart_rx_len) %
                        BBK9588_UART_FIFO_SIZE;

        board->uart_rx_fifo[tail] = value;
        board->uart_rx_len++;
    } else {
        board->uart_status |= BBK9588_UART_LSR_OVER;
    }
    bbk9588_uart_update_lsr(board);
    bbk9588_uart_sync_irq(board);
}

static void bbk9588_uart_read_backend(void *opaque, const uint8_t *buf,
                                      int size)
{
    Bbk9588MachineState *board = opaque;

    for (int i = 0; i < size; i++) {
        bbk9588_uart_push_rx(board, buf[i]);
    }
}

static uint8_t bbk9588_uart_pop_rx(Bbk9588MachineState *board)
{
    uint8_t value = 0xff;

    if (board->uart_rx_len != 0) {
        value = board->uart_rx_fifo[board->uart_rx_head];
        board->uart_rx_head = (board->uart_rx_head + 1) %
                              BBK9588_UART_FIFO_SIZE;
        board->uart_rx_len--;
    }
    bbk9588_uart_update_lsr(board);
    bbk9588_uart_sync_irq(board);
    qemu_chr_fe_accept_input(&board->uart_chr);
    return value;
}

static void bbk9588_uart_clear_rx(Bbk9588MachineState *board)
{
    board->uart_rx_head = 0;
    board->uart_rx_len = 0;
    board->uart_status &= ~BBK9588_UART_LSR_ERROR_MASK;
    bbk9588_uart_update_lsr(board);
    bbk9588_uart_sync_irq(board);
    qemu_chr_fe_accept_input(&board->uart_chr);
}

static void bbk9588_uart_update_msr(Bbk9588MachineState *board)
{
    uint8_t old_cts = board->uart_msr & BBK9588_UART_MSR_CTS;
    uint8_t cts = 0;

    if ((board->uart_mcr & (BBK9588_UART_MCR_MDCE |
                            BBK9588_UART_MCR_LOOP |
                            BBK9588_UART_MCR_RTS)) ==
        (BBK9588_UART_MCR_MDCE | BBK9588_UART_MCR_LOOP |
         BBK9588_UART_MCR_RTS)) {
        cts = BBK9588_UART_MSR_CTS;
    }
    board->uart_msr = (board->uart_msr & BBK9588_UART_MSR_CCTS) | cts;
    if (old_cts != cts) {
        board->uart_msr |= BBK9588_UART_MSR_CCTS;
    }
}

static uint32_t bbk9588_uart_read(Bbk9588MachineState *board,
                                  Bbk9588MmioState *s,
                                  hwaddr offset, unsigned size)
{
    hwaddr aligned_offset = offset & ~3;
    uint32_t index = aligned_offset / sizeof(uint32_t);
    uint8_t value;

    switch (aligned_offset) {
    case BBK9588_UART_RBR_OFF:
        value = (board->uart_lcr & BBK9588_UART_LCR_DLAB) ?
                board->uart_dll : bbk9588_uart_pop_rx(board);
        break;
    case BBK9588_UART_IER_OFF:
        value = (board->uart_lcr & BBK9588_UART_LCR_DLAB) ?
                board->uart_dlh : board->uart_ier;
        break;
    case BBK9588_UART_IIR_OFF:
        value = bbk9588_uart_iir_value(board);
        if ((value & 0x0fu) == BBK9588_UART_IIR_TDR) {
            board->uart_thr_irq_latched = false;
            bbk9588_uart_sync_irq(board);
        }
        break;
    case BBK9588_UART_LCR_OFF:
        value = board->uart_lcr;
        break;
    case BBK9588_UART_MCR_OFF:
        value = board->uart_mcr;
        break;
    case BBK9588_UART_LSR_OFF:
        bbk9588_uart_update_lsr(board);
        value = board->uart_status & 0xffu;
        board->uart_status &= ~BBK9588_UART_LSR_ERROR_MASK;
        bbk9588_uart_update_lsr(board);
        bbk9588_uart_sync_irq(board);
        break;
    case BBK9588_UART_MSR_OFF:
        bbk9588_uart_update_msr(board);
        value = board->uart_msr;
        board->uart_msr &= ~BBK9588_UART_MSR_CCTS;
        bbk9588_uart_sync_irq(board);
        break;
    case BBK9588_UART_SPR_OFF:
        value = board->uart_spr;
        break;
    case BBK9588_UART_ISR_OFF:
        value = board->uart_isr;
        break;
    case BBK9588_UART_UMR_OFF:
        value = board->uart_umr;
        break;
    case BBK9588_UART_UACR_OFF:
        value = board->uart_uacr & 0xffu;
        break;
    default:
        value = s->regs[index] & 0xffu;
        break;
    }
    s->regs[index] = value;
    return bbk9588_mmio_extract32(value, offset, size);
}

static bool bbk9588_range_intersects(hwaddr offset, unsigned size,
                                     hwaddr reg_offset, unsigned reg_size)
{
    return offset < reg_offset + reg_size &&
           reg_offset < offset + size;
}

static bool bbk9588_udc_in_ep_valid(unsigned ep)
{
    return (BBK9588_UDC_INTRIN_ENDPOINT_MASK & (1u << ep)) != 0;
}

static bool bbk9588_udc_out_ep_valid(unsigned ep)
{
    return (BBK9588_UDC_INTROUT_ENDPOINT_MASK & (1u << ep)) != 0;
}

static uint8_t bbk9588_udc_read_byte(Bbk9588MachineState *board,
                                     hwaddr offset)
{
    unsigned ep = board->udc_index & BBK9588_UDC_INDEX_MASK;

    switch (offset) {
    case BBK9588_UDC_FADDR_OFF:
        return board->udc_faddr;
    case BBK9588_UDC_POWER_OFF:
        return board->udc_power & BBK9588_UDC_POWER_RW_MASK;
    case BBK9588_UDC_INTRIN_OFF:
        return (board->udc_intr_in & BBK9588_UDC_INTRIN_ENDPOINT_MASK) & 0xffu;
    case BBK9588_UDC_INTRIN_OFF + 1:
        return (board->udc_intr_in & BBK9588_UDC_INTRIN_ENDPOINT_MASK) >> 8;
    case BBK9588_UDC_INTROUT_OFF:
        return (board->udc_intr_out & BBK9588_UDC_INTROUT_ENDPOINT_MASK) & 0xffu;
    case BBK9588_UDC_INTROUT_OFF + 1:
        return (board->udc_intr_out & BBK9588_UDC_INTROUT_ENDPOINT_MASK) >> 8;
    case BBK9588_UDC_INTRINE_OFF:
        return (board->udc_intr_in_enable &
                BBK9588_UDC_INTRIN_ENDPOINT_MASK) & 0xffu;
    case BBK9588_UDC_INTRINE_OFF + 1:
        return (board->udc_intr_in_enable &
                BBK9588_UDC_INTRIN_ENDPOINT_MASK) >> 8;
    case BBK9588_UDC_INTROUTE_OFF:
        return (board->udc_intr_out_enable &
                BBK9588_UDC_INTROUT_ENDPOINT_MASK) & 0xffu;
    case BBK9588_UDC_INTROUTE_OFF + 1:
        return (board->udc_intr_out_enable &
                BBK9588_UDC_INTROUT_ENDPOINT_MASK) >> 8;
    case BBK9588_UDC_INTRUSB_OFF:
        return board->udc_intr_usb & BBK9588_UDC_INTRUSBE_MASK;
    case BBK9588_UDC_INTRUSBE_OFF:
        return board->udc_intr_usb_enable & BBK9588_UDC_INTRUSBE_MASK;
    case BBK9588_UDC_FRAME_OFF:
        return board->udc_frame & 0xffu;
    case BBK9588_UDC_FRAME_OFF + 1:
        return (board->udc_frame >> 8) & 0x07u;
    case BBK9588_UDC_INDEX_OFF:
        return board->udc_index & BBK9588_UDC_INDEX_MASK;
    case BBK9588_UDC_TESTMODE_OFF:
        return board->udc_testmode & BBK9588_UDC_TESTMODE_MASK;
    case BBK9588_UDC_INMAXP_OFF:
        return bbk9588_udc_in_ep_valid(ep) ?
               (board->udc_in_maxp[ep] & 0xffu) : 0;
    case BBK9588_UDC_INMAXP_OFF + 1:
        return bbk9588_udc_in_ep_valid(ep) ?
               (board->udc_in_maxp[ep] >> 8) : 0;
    case BBK9588_UDC_CSR0_INCSR_OFF:
        return ep != 0 && bbk9588_udc_in_ep_valid(ep) ?
               (board->udc_in_csr[ep] & 0xffu) : 0;
    case BBK9588_UDC_CSR0_INCSR_OFF + 1:
        return ep != 0 && bbk9588_udc_in_ep_valid(ep) ?
               (board->udc_in_csr[ep] >> 8) : 0;
    case BBK9588_UDC_OUTMAXP_OFF:
        return bbk9588_udc_out_ep_valid(ep) ?
               (board->udc_out_maxp[ep] & 0xffu) : 0;
    case BBK9588_UDC_OUTMAXP_OFF + 1:
        return bbk9588_udc_out_ep_valid(ep) ?
               (board->udc_out_maxp[ep] >> 8) : 0;
    case BBK9588_UDC_OUTCSR_OFF:
        return bbk9588_udc_out_ep_valid(ep) ?
               (board->udc_out_csr[ep] & 0xffu) : 0;
    case BBK9588_UDC_OUTCSR_OFF + 1:
        return bbk9588_udc_out_ep_valid(ep) ?
               (board->udc_out_csr[ep] >> 8) : 0;
    case BBK9588_UDC_COUNT_OFF:
    case BBK9588_UDC_COUNT_OFF + 1:
        return 0;
    case BBK9588_UDC_EPINFO_OFF:
        return BBK9588_UDC_EPINFO_VALUE;
    case BBK9588_UDC_RAMINFO_OFF:
        return BBK9588_UDC_RAMINFO_VALUE;
    default:
        if (offset >= BBK9588_UDC_FIFO_BASE_OFF &&
            offset < BBK9588_UDC_FIFO_END_OFF) {
            return 0;
        }
        return 0;
    }
}

static uint32_t bbk9588_udc_read(Bbk9588MachineState *board,
                                 hwaddr offset, unsigned size)
{
    uint32_t value = 0;

    for (unsigned i = 0; i < size; i++) {
        value |= (uint32_t)bbk9588_udc_read_byte(board, offset + i) <<
                 (i * 8);
    }

    if (bbk9588_range_intersects(offset, size, BBK9588_UDC_INTRIN_OFF, 2)) {
        board->udc_intr_in = 0;
    }
    if (bbk9588_range_intersects(offset, size, BBK9588_UDC_INTROUT_OFF, 2)) {
        board->udc_intr_out = 0;
    }
    if (bbk9588_range_intersects(offset, size, BBK9588_UDC_INTRUSB_OFF, 1)) {
        board->udc_intr_usb = 0;
        board->udc_power &= ~0x02u;
    }
    bbk9588_update_irq(board);
    return value;
}

static void bbk9588_udc_write_byte(Bbk9588MachineState *board,
                                   hwaddr offset, uint8_t value)
{
    unsigned ep = board->udc_index & BBK9588_UDC_INDEX_MASK;

    switch (offset) {
    case BBK9588_UDC_FADDR_OFF:
        board->udc_faddr = (value & 0x7fu) | 0x80u;
        break;
    case BBK9588_UDC_POWER_OFF:
        board->udc_power = (value & BBK9588_UDC_POWER_RW_MASK) |
                           (board->udc_power & ~BBK9588_UDC_POWER_RW_MASK);
        board->udc_power &= ~0x1au;
        break;
    case BBK9588_UDC_INTRINE_OFF:
        board->udc_intr_in_enable =
            (board->udc_intr_in_enable & 0xff00u) | value;
        board->udc_intr_in_enable &= BBK9588_UDC_INTRIN_ENDPOINT_MASK;
        break;
    case BBK9588_UDC_INTRINE_OFF + 1:
        board->udc_intr_in_enable =
            (board->udc_intr_in_enable & 0x00ffu) | ((uint16_t)value << 8);
        board->udc_intr_in_enable &= BBK9588_UDC_INTRIN_ENDPOINT_MASK;
        break;
    case BBK9588_UDC_INTROUTE_OFF:
        board->udc_intr_out_enable =
            ((board->udc_intr_out_enable & 0xff00u) | value) &
            BBK9588_UDC_INTROUT_ENDPOINT_MASK;
        break;
    case BBK9588_UDC_INTROUTE_OFF + 1:
        board->udc_intr_out_enable =
            ((board->udc_intr_out_enable & 0x00ffu) |
             ((uint16_t)value << 8)) &
            BBK9588_UDC_INTROUT_ENDPOINT_MASK;
        break;
    case BBK9588_UDC_INTRUSBE_OFF:
        board->udc_intr_usb_enable = value & BBK9588_UDC_INTRUSBE_MASK;
        break;
    case BBK9588_UDC_INDEX_OFF:
        board->udc_index = value & BBK9588_UDC_INDEX_MASK;
        break;
    case BBK9588_UDC_TESTMODE_OFF:
        board->udc_testmode = value & BBK9588_UDC_TESTMODE_MASK;
        break;
    case BBK9588_UDC_INMAXP_OFF:
        if (bbk9588_udc_in_ep_valid(ep)) {
            board->udc_in_maxp[ep] =
                (board->udc_in_maxp[ep] & 0xff00u) | value;
            board->udc_in_maxp[ep] &= BBK9588_UDC_MAXP_MASK;
        }
        break;
    case BBK9588_UDC_INMAXP_OFF + 1:
        if (bbk9588_udc_in_ep_valid(ep)) {
            board->udc_in_maxp[ep] =
                ((board->udc_in_maxp[ep] & 0x00ffu) |
                 ((uint16_t)value << 8)) & BBK9588_UDC_MAXP_MASK;
        }
        break;
    case BBK9588_UDC_CSR0_INCSR_OFF:
        if (ep != 0 && bbk9588_udc_in_ep_valid(ep)) {
            board->udc_in_csr[ep] =
                (board->udc_in_csr[ep] & 0xff00u) | value;
            board->udc_in_csr[ep] &= BBK9588_UDC_INCSR_RW_MASK;
        }
        break;
    case BBK9588_UDC_CSR0_INCSR_OFF + 1:
        if (ep != 0 && bbk9588_udc_in_ep_valid(ep)) {
            board->udc_in_csr[ep] =
                ((board->udc_in_csr[ep] & 0x00ffu) |
                 ((uint16_t)value << 8)) & BBK9588_UDC_INCSR_RW_MASK;
        }
        break;
    case BBK9588_UDC_OUTMAXP_OFF:
        if (bbk9588_udc_out_ep_valid(ep)) {
            board->udc_out_maxp[ep] =
                (board->udc_out_maxp[ep] & 0xff00u) | value;
            board->udc_out_maxp[ep] &= BBK9588_UDC_MAXP_MASK;
        }
        break;
    case BBK9588_UDC_OUTMAXP_OFF + 1:
        if (bbk9588_udc_out_ep_valid(ep)) {
            board->udc_out_maxp[ep] =
                ((board->udc_out_maxp[ep] & 0x00ffu) |
                 ((uint16_t)value << 8)) & BBK9588_UDC_MAXP_MASK;
        }
        break;
    case BBK9588_UDC_OUTCSR_OFF:
        if (bbk9588_udc_out_ep_valid(ep)) {
            board->udc_out_csr[ep] =
                (board->udc_out_csr[ep] & 0xff00u) | value;
            board->udc_out_csr[ep] &= BBK9588_UDC_OUTCSR_RW_MASK;
        }
        break;
    case BBK9588_UDC_OUTCSR_OFF + 1:
        if (bbk9588_udc_out_ep_valid(ep)) {
            board->udc_out_csr[ep] =
                ((board->udc_out_csr[ep] & 0x00ffu) |
                 ((uint16_t)value << 8)) & BBK9588_UDC_OUTCSR_RW_MASK;
        }
        break;
    default:
        break;
    }
}

static void bbk9588_udc_write(Bbk9588MachineState *board, hwaddr offset,
                              uint64_t value, unsigned size)
{
    for (unsigned i = 0; i < size; i++) {
        bbk9588_udc_write_byte(board, offset + i, (value >> (i * 8)) & 0xffu);
    }
    bbk9588_update_irq(board);
}

static void bbk9588_msc_prepare_response(Bbk9588MmioState *s)
{
    Bbk9588MachineState *board = s->board;
    uint8_t cmd = s->regs[BBK9588_MSC_CMD_OFF / sizeof(uint32_t)] & 0xff;
    uint32_t arg = s->regs[BBK9588_MSC_ARG_OFF / sizeof(uint32_t)];

    memset(s->msc_response, 0, sizeof(s->msc_response));
    s->msc_response[0] = cmd;
    s->msc_response_len = sizeof(s->msc_response);
    s->msc_response_index = 0;
    board->msc_last_cmd = cmd;
    board->msc_last_arg = arg;
    board->msc_data_ready = false;
    s->regs[BBK9588_MSC_IREG_OFF / sizeof(uint32_t)] &= ~0x00000003u;
    bbk9588_msc_trace_record(BBK9588_MSC_TRACE_CMD, arg >> 9, 0, 0,
                             cmd, arg, 0);
    if (bbk9588_is_msc_window(s) && (cmd == 0x11 || cmd == 0x12)) {
        board->msc_read_lba = arg >> 9;
        board->msc_read_pending = true;
        board->msc_write_pending = false;
        jz4740_dmac_kick(board->dmac);
    } else if (bbk9588_is_msc_window(s) &&
               (cmd == 0x18 || cmd == 0x19)) {
        board->msc_write_lba = arg >> 9;
        board->msc_write_pending = true;
        board->msc_read_pending = false;
        jz4740_dmac_kick(board->dmac);
    }
}

static uint32_t bbk9588_msc_read_response(Bbk9588MmioState *s, unsigned size)
{
    uint32_t value = 0;

    /*
     * C200 loads halfwords from 0xb0021034 and stores the high byte first into
     * its response buffer.  Return each FIFO pair as a big-endian halfword so
     * the firmware sees the shifted response byte order.
     */
    if (size <= 1) {
        if (s->msc_response_index < s->msc_response_len) {
            value = s->msc_response[s->msc_response_index++];
        }
        return value;
    }
    for (unsigned i = 0; i < size; i += 2) {
        uint32_t hi = 0;
        uint32_t lo = 0;

        if (s->msc_response_index < s->msc_response_len) {
            hi = s->msc_response[s->msc_response_index++];
        }
        if (s->msc_response_index < s->msc_response_len) {
            lo = s->msc_response[s->msc_response_index++];
        }
        value |= ((hi << 8) | lo) << (i * 8);
    }
    return value;
}

static void bbk9588_graphics_trace_write(Bbk9588MmioState *s, hwaddr offset,
                                         uint64_t value, unsigned size)
{
    Bbk9588MachineState *board = s->board;
    JZ4740LCDDiagnostics lcd;

    if (!board || !board->graphics_trace_enabled) {
        return;
    }
    if (s->window->kind != BBK9588_MMIO_GRAPHICS) {
        return;
    }
    if (board->graphics_trace_count++ >= 4096) {
        return;
    }
    jz4740_lcd_get_diagnostics(board->lcd, &lcd);
    error_report(
        "bbk9588-mmio[%u] win=0x%08" HWADDR_PRIx
        " kind=%u off=0x%04" HWADDR_PRIx
        " size=%u value=0x%08" PRIx64
        " r0000=0x%08x r0004=0x%08x r0008=0x%08x r000c=0x%08x"
        " r0010=0x%08x r0014=0x%08x r0018=0x%08x r001c=0x%08x"
        " r0020=0x%08x r0024=0x%08x r0028=0x%08x r002c=0x%08x"
        " r0030=0x%08x r0034=0x%08x r0038=0x%08x r003c=0x%08x"
        " r0040=0x%08x r0044=0x%08x r0048=0x%08x r004c=0x%08x"
        " lcd_desc=0x%08x lcd_fb=0x%08x",
        board->graphics_trace_count - 1,
        s->window->kseg1_base,
        s->window->kind,
        offset,
        size,
        value,
        s->regs[0x0000 / sizeof(uint32_t)],
        s->regs[0x0004 / sizeof(uint32_t)],
        s->regs[0x0008 / sizeof(uint32_t)],
        s->regs[0x000c / sizeof(uint32_t)],
        s->regs[0x0010 / sizeof(uint32_t)],
        s->regs[0x0014 / sizeof(uint32_t)],
        s->regs[0x0018 / sizeof(uint32_t)],
        s->regs[0x001c / sizeof(uint32_t)],
        s->regs[0x0020 / sizeof(uint32_t)],
        s->regs[0x0024 / sizeof(uint32_t)],
        s->regs[0x0028 / sizeof(uint32_t)],
        s->regs[0x002c / sizeof(uint32_t)],
        s->regs[0x0030 / sizeof(uint32_t)],
        s->regs[0x0034 / sizeof(uint32_t)],
        s->regs[0x0038 / sizeof(uint32_t)],
        s->regs[0x003c / sizeof(uint32_t)],
        s->regs[0x0040 / sizeof(uint32_t)],
        s->regs[0x0044 / sizeof(uint32_t)],
        s->regs[0x0048 / sizeof(uint32_t)],
        s->regs[0x004c / sizeof(uint32_t)],
        lcd.descriptor_address,
        lcd.framebuffer_address);
}

static void bbk9588_uart_write(Bbk9588MmioState *s, hwaddr offset,
                               uint32_t reg)
{
    Bbk9588MachineState *board = s->board;
    hwaddr aligned_offset = offset & ~3;
    unsigned shift = (offset & 3) * 8;
    uint8_t value = (reg >> shift) & 0xffu;
    uint32_t index = aligned_offset / sizeof(uint32_t);

    if (offset & 3) {
        return;
    }

    switch (aligned_offset) {
    case BBK9588_UART_THR_OFF:
        if (board->uart_lcr & BBK9588_UART_LCR_DLAB) {
            board->uart_dll = value;
            s->regs[index] = board->uart_dll;
            break;
        }
        board->uart_thr_irq_latched = false;
        if (board->uart_mcr & BBK9588_UART_MCR_LOOP) {
            bbk9588_uart_push_rx(board, value);
        } else if (qemu_chr_fe_backend_connected(&board->uart_chr)) {
            qemu_chr_fe_write_all(&board->uart_chr, &value, 1);
        }
        bbk9588_uart_update_lsr(board);
        bbk9588_uart_latch_thr_irq(board);
        s->regs[index] = value;
        break;
    case BBK9588_UART_IER_OFF:
        if (board->uart_lcr & BBK9588_UART_LCR_DLAB) {
            board->uart_dlh = value;
            s->regs[index] = board->uart_dlh;
        } else {
            bool was_tdrie = (board->uart_ier & BBK9588_UART_IER_TDRIE) != 0;

            board->uart_ier = value & BBK9588_UART_IER_MASK;
            if (!(board->uart_ier & BBK9588_UART_IER_TDRIE)) {
                board->uart_thr_irq_latched = false;
            } else if (!was_tdrie) {
                bbk9588_uart_latch_thr_irq(board);
            }
            s->regs[index] = board->uart_ier;
        }
        break;
    case BBK9588_UART_FCR_OFF:
        if (value & BBK9588_UART_FCR_RFRT) {
            bbk9588_uart_clear_rx(board);
        }
        if (value & BBK9588_UART_FCR_TFRT) {
            board->uart_thr_irq_latched = false;
        }
        board->uart_fcr = value & (BBK9588_UART_FCR_FME |
                                   BBK9588_UART_FCR_DME |
                                   BBK9588_UART_FCR_UME |
                                   BBK9588_UART_FCR_RDTR_MASK);
        if (board->uart_fcr & BBK9588_UART_FCR_UME) {
            bbk9588_uart_latch_thr_irq(board);
        } else {
            board->uart_thr_irq_latched = false;
        }
        s->regs[index] = board->uart_fcr;
        break;
    case BBK9588_UART_LCR_OFF:
        board->uart_lcr = value;
        s->regs[index] = board->uart_lcr;
        break;
    case BBK9588_UART_MCR_OFF:
        board->uart_mcr = value & BBK9588_UART_MCR_MASK;
        bbk9588_uart_update_msr(board);
        s->regs[index] = board->uart_mcr;
        break;
    case BBK9588_UART_SPR_OFF:
        board->uart_spr = value;
        s->regs[index] = board->uart_spr;
        break;
    case BBK9588_UART_ISR_OFF:
        board->uart_isr = value & BBK9588_UART_ISR_MASK;
        s->regs[index] = board->uart_isr;
        break;
    case BBK9588_UART_UMR_OFF:
        board->uart_umr = value & BBK9588_UART_UMR_MASK;
        s->regs[index] = board->uart_umr;
        break;
    case BBK9588_UART_UACR_OFF:
        board->uart_uacr = reg & BBK9588_UART_UACR_MASK;
        s->regs[index] = board->uart_uacr;
        break;
    default:
        s->regs[index] = reg;
        break;
    }
    bbk9588_uart_update_lsr(board);
    bbk9588_uart_sync_irq(board);
}

static uint64_t bbk9588_mmio_read(void *opaque, hwaddr offset, unsigned size)
{
    Bbk9588MmioState *s = opaque;
    Bbk9588MachineState *board = s->board;
    uint32_t index = offset / sizeof(uint32_t);
    unsigned shift = (offset & 3) * 8;
    hwaddr aligned_offset = offset & ~3;
    uint32_t value;

    switch (s->window->kind) {
    case BBK9588_MMIO_EXTGPIO:
        if (s->window->kseg1_base == 0xb3010000 && offset == 0x10008) {
            bbk9588_msc_complete_dma(s);
        }
        if (s->window->kseg1_base == 0xb3010000 && offset == 0x10028) {
            bbk9588_msc_complete_dma(s);
        }
        if (bbk9588_nand_control_window(s)) {
            switch (aligned_offset) {
            case BBK9588_NAND_NFCSR_OFF:
            case BBK9588_NAND_NFECCR_OFF:
            case BBK9588_NAND_NFECC_OFF:
            case BBK9588_NAND_NFINTS_OFF:
            case BBK9588_NAND_NFINTE_OFF:
                return bbk9588_mmio_extract32(
                    bbk9588_nand_control_read(board, s, aligned_offset),
                    offset, size);
            default:
                break;
            }
        }
        break;
    case BBK9588_MMIO_MISC:
        break;
    case BBK9588_MMIO_GRAPHICS:
        if (bbk9588_is_msc_window(s) &&
            offset == BBK9588_MSC_RES_OFF) {
            return bbk9588_msc_read_response(s, size);
        }
        if (bbk9588_is_msc_window(s) &&
            offset == BBK9588_MSC_IREG_OFF) {
            return s->regs[index] | (board->msc_data_ready ? 0x00000003 : 0);
        }
        if (bbk9588_is_msc_window(s) &&
            offset == BBK9588_MSC_STAT_OFF) {
            return s->regs[index];
        }
        break;
    case BBK9588_MMIO_UART:
        return bbk9588_uart_read(board, s, offset, size);
    case BBK9588_MMIO_UDC:
        return bbk9588_udc_read(board, offset, size);
    case BBK9588_MMIO_LCD:
        if (offset == 0x0c) {
            return board->lcd_status | board->lcd_irq_status;
        }
        break;
    }

    value = s->regs[index] >> shift;
    switch (size) {
    case 1:
        return value & 0xff;
    case 2:
        return value & 0xffff;
    default:
        return value;
    }
}

static void bbk9588_mmio_write(void *opaque, hwaddr offset,
                               uint64_t value, unsigned size)
{
    Bbk9588MmioState *s = opaque;
    Bbk9588MachineState *board = s->board;
    uint32_t index = offset / sizeof(uint32_t);
    unsigned shift = (offset & 3) * 8;
    hwaddr aligned_offset = offset & ~3;
    uint32_t mask;
    uint32_t lane_value;
    uint32_t reg;

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
    reg = s->regs[index];
    lane_value = ((uint32_t)value << shift) & mask;
    reg = (reg & ~mask) | lane_value;
    s->regs[index] = reg;
    bbk9588_graphics_trace_write(s, offset, value, size);
    if (bbk9588_nand_control_window(s)) {
        switch (aligned_offset) {
        case BBK9588_NAND_NFCSR_OFF:
        case BBK9588_NAND_NFECCR_OFF:
        case BBK9588_NAND_NFECC_OFF:
        case BBK9588_NAND_NFINTS_OFF:
        case BBK9588_NAND_NFINTE_OFF:
            bbk9588_nand_control_write(board, s, aligned_offset, reg);
            reg = s->regs[index];
            break;
        default:
            break;
        }
    }
    if (s->window->kind == BBK9588_MMIO_EXTGPIO &&
        s->window->kseg1_base == 0xb3010000 &&
        offset == 0x80) {
        board->extgpio_wake_enable_80 = reg & 0x00040000u;
        bbk9588_sysctrl_sync_wake(board);
        bbk9588_update_irq(board);
    }
    if (s->window->kind == BBK9588_MMIO_LCD) {
        bbk9588_lcd_write(board, offset, value);
    }
    if (s->window->kind == BBK9588_MMIO_LCD) {
        jz4740_lcd_observe_alias_write(board->lcd, offset, reg);
    }
    if (s->window->kind == BBK9588_MMIO_UART) {
        bbk9588_uart_write(s, offset, reg);
    }
    if (s->window->kind == BBK9588_MMIO_UDC) {
        bbk9588_udc_write(board, offset, value, size);
    }
    if (s->window->kind == BBK9588_MMIO_EXTGPIO &&
        s->window->kseg1_base == 0xb3010000 &&
        (offset == 0x10010 || offset == 0x10030) &&
        (reg & 0x80000001u)) {
        bbk9588_msc_complete_dma(s);
    }
    if (bbk9588_is_msc_window(s) &&
        offset == BBK9588_MSC_STRPCL_OFF && (value & 0xffffu) == 6) {
        bbk9588_msc_prepare_response(s);
        s->regs[BBK9588_MSC_STAT_OFF / sizeof(uint32_t)] =
            (s->regs[BBK9588_MSC_STAT_OFF / sizeof(uint32_t)] & ~0x00000100u) |
            0x00000800u;
    }
    if (bbk9588_is_msc_window(s) &&
        offset == BBK9588_MSC_IREG_OFF && (value & 0x03u)) {
        s->regs[index] &= ~(value & 0x03u);
        if ((s->regs[index] & 0x03u) == 0) {
            board->msc_data_ready = false;
        }
    }
}

static const MemoryRegionOps bbk9588_mmio_ops = {
    .read = bbk9588_mmio_read,
    .write = bbk9588_mmio_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
    },
};

static void bbk9588_input_handle_line(Bbk9588MachineState *board,
                                      const char *line)
{
    unsigned x;
    unsigned y;
    unsigned raw_x;
    unsigned raw_y;
    unsigned down;
    unsigned key_code;

    if (sscanf(line, "T %u %u %u %u %u", &x, &y, &raw_x, &raw_y,
               &down) == 5) {
        bbk9588_touch_apply_host_input(
            board,
            (uint16_t)(raw_x > 0xffff ? 0xffff : raw_x),
            (uint16_t)(raw_y > 0xffff ? 0xffff : raw_y),
            (uint16_t)(x > 0xffff ? 0xffff : x),
            (uint16_t)(y > 0xffff ? 0xffff : y),
            down != 0);
        return;
    }

    if (sscanf(line, "K %u %u", &key_code, &down) == 2) {
        bbk9588_key_apply_host_input(board, key_code & 0xff, down != 0);
    }
}

static int bbk9588_input_can_read(void *opaque)
{
    Bbk9588MachineState *board = opaque;

    return (int)(sizeof(board->input_line) - board->input_line_len - 1);
}

static void bbk9588_input_read(void *opaque, const uint8_t *buf, int size)
{
    Bbk9588MachineState *board = opaque;

    for (int i = 0; i < size; i++) {
        char ch = (char)buf[i];

        if (ch == '\r') {
            continue;
        }
        if (ch == '\n') {
            board->input_line[board->input_line_len] = 0;
            if (board->input_line_len > 0) {
                bbk9588_input_handle_line(board, board->input_line);
            }
            board->input_line_len = 0;
            continue;
        }
        if (board->input_line_len + 1 < sizeof(board->input_line)) {
            board->input_line[board->input_line_len++] = ch;
        } else {
            board->input_line_len = 0;
        }
    }
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
    jz4740_aic_set_output_callback(board->aic, bbk9588_audio_output, board);
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
    jz4740_lcd_set_frame_source_callback(
        board->lcd, bbk9588_lcd_frame_source_changed, board);
    jz4740_lcd_set_trace_enabled(board->lcd,
                                  board->graphics_trace_enabled);
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

static void bbk9588_map_mmio_window(MachineState *machine,
                                    Bbk9588MachineState *board,
                                    const Bbk9588MmioWindow *window)
{
    Bbk9588MmioState *s = g_new0(Bbk9588MmioState, 1);

    s->window = window;
    s->board = board;
    if (bbk9588_is_msc_window(s)) {
        s->regs[BBK9588_MSC_STAT_OFF / sizeof(uint32_t)] =
            BBK9588_MSC_STAT_RESET;
        s->regs[BBK9588_MSC_RESTO_OFF / sizeof(uint32_t)] =
            BBK9588_MSC_RESTO_RESET;
        s->regs[BBK9588_MSC_RDTO_OFF / sizeof(uint32_t)] =
            BBK9588_MSC_RDTO_RESET;
        s->regs[BBK9588_MSC_IMASK_OFF / sizeof(uint32_t)] =
            BBK9588_MSC_IMASK_RESET;
    }
    memory_region_init_io(&s->mr, OBJECT(machine), &bbk9588_mmio_ops, s,
                          window->name, window->size);
    memory_region_add_subregion(get_system_memory(),
                                KSEG1_TO_PHYS(window->kseg1_base), &s->mr);
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
    board->perf_seq = 0;
    board->perf_last_send_ms = 0;

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
    if (board->aic_irq) {
        qemu_free_irq(board->aic_irq);
    }
    if (board->intc) {
        object_unref(OBJECT(board->intc));
    }
    if (board->intc_irq) {
        qemu_free_irq(board->intc_irq);
    }
    if (board->cpm) {
        object_unref(OBJECT(board->cpm));
    }
    if (board->dmac) {
        object_unref(OBJECT(board->dmac));
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
    if (board->rtc) {
        object_unref(OBJECT(board->rtc));
    }
    if (board->sadc) {
        object_unref(OBJECT(board->sadc));
    }
    if (board->tcu) {
        object_unref(OBJECT(board->tcu));
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

    board->uart_status = BBK9588_UART_LSR_RESET;
    board->uart_thr_irq_latched = false;
    board->uart_ier = 0;
    board->uart_fcr = 0;
    board->uart_lcr = 0;
    board->uart_mcr = 0;
    board->uart_msr = 0;
    board->uart_spr = 0;
    board->uart_isr = 0;
    board->uart_umr = 0;
    board->uart_uacr = 0;
    board->uart_dll = 0;
    board->uart_dlh = 0;
    board->uart_rx_head = 0;
    board->uart_rx_len = 0;
    board->udc_faddr = 0;
    board->udc_power = BBK9588_UDC_POWER_RESET;
    board->udc_intr_in = 0;
    board->udc_intr_out = 0;
    board->udc_intr_in_enable =
        BBK9588_UDC_INTRINE_RESET & BBK9588_UDC_INTRIN_ENDPOINT_MASK;
    board->udc_intr_out_enable =
        BBK9588_UDC_INTROUTE_RESET & BBK9588_UDC_INTROUT_ENDPOINT_MASK;
    board->udc_intr_usb = 0;
    board->udc_intr_usb_enable = BBK9588_UDC_INTRUSBE_RESET;
    board->udc_frame = 0;
    board->udc_index = 0;
    board->udc_testmode = 0;
    memset(board->udc_in_maxp, 0, sizeof(board->udc_in_maxp));
    memset(board->udc_in_csr, 0, sizeof(board->udc_in_csr));
    memset(board->udc_out_maxp, 0, sizeof(board->udc_out_maxp));
    memset(board->udc_out_csr, 0, sizeof(board->udc_out_csr));
    board->msc_read_pending = false;
    board->msc_write_pending = false;
    board->msc_data_ready = false;
    board->msc_read_lba = 0;
    board->msc_write_lba = 0;
    board->msc_last_cmd = 0;
    board->msc_last_arg = 0;
    board->msc_last_dma_phys = 0;
    board->msc_last_dma_words = 0;
    board->msc_dma_complete_count = 0;
    board->lcd_status = 0;
    board->lcd_irq_status = 0;
    board->perf_seq = 0;
    board->perf_last_send_ms = 0;
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
    board->nand_id_code = BBK9588_NAND_ID_CODE;
    board->bootrom_nand_page = BBK9588_BOOTROM_NAND_PAGE;
    board->bootrom_size = BBK9588_BOOTROM_FIRST_STAGE_BYTES;
    board->lcd_scanout_not_before_ms = 0;
    board->lcd_frame_stable_not_before_ms = 0;
    board->lcd_frame_chardev_sent_valid = false;
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
    Chardev *input_chr;
    Chardev *frame_chr;

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
    board->lcd_refresh_timer = timer_new_ms(QEMU_CLOCK_REALTIME,
                                            bbk9588_lcd_refresh_timer_cb,
                                            board);
    board->lcd_console = graphic_console_init(NULL, 0, &bbk9588_lcd_ops,
                                              board);
    qemu_console_resize(board->lcd_console, BBK9588_LCD_WIDTH,
                        BBK9588_LCD_HEIGHT);

    if (serial_hd(0)) {
        qemu_chr_fe_init(&board->uart_chr, serial_hd(0), &error_abort);
        qemu_chr_fe_set_handlers(&board->uart_chr, bbk9588_uart_can_read,
                                 bbk9588_uart_read_backend, NULL, NULL,
                                 board, NULL, true);
    }
    input_chr = board->input_chardev ? qemu_chr_find(board->input_chardev) :
                serial_hd(1);
    if (input_chr) {
        qemu_chr_fe_init(&board->input_chr, input_chr, &error_abort);
        qemu_chr_fe_set_handlers(&board->input_chr, bbk9588_input_can_read,
                                 bbk9588_input_read, NULL, NULL, board, NULL,
                                 true);
    }
    frame_chr = board->frame_chardev ? qemu_chr_find(board->frame_chardev) :
                NULL;
    if (frame_chr) {
        qemu_chr_fe_init(&board->frame_chr, frame_chr, &error_abort);
    }
    board->extgpio_wake_enable_80 = 0;
    board->sysctrl_wake_pending = false;
    board->gpio300_wake_pulse_available = false;
    board->sysctrl_wake_count = 0;
    bbk9588_create_intc_device(board);
    bbk9588_create_sadc_device(board);
    bbk9588_create_gpio_device(board);
    bbk9588_create_rtc_device(board);
    bbk9588_create_lcd_device(machine, board);
    bbk9588_create_tcu_device(board);
    bbk9588_create_cpm_device(board);
    bbk9588_create_dmac_device(machine, board);
    bbk9588_create_nand_device(board);
    bbk9588_load_nand_image(board);
    bbk9588_touch_sync_latch(board);
    bbk9588_touch_trace_update(board, 7u);
    for (size_t i = 0; i < ARRAY_SIZE(bbk9588_mmio_windows); i++) {
        bbk9588_map_mmio_window(machine, board, &bbk9588_mmio_windows[i]);
    }
    bbk9588_create_aic_device(machine, board);

    bbk9588_load_firmware(machine);
    bbk9588_progress_trace_schedule(board);
    bbk9588_lcd_refresh_schedule(board);
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
        offsetof(Bbk9588MachineState, nand_id_code), BBK9588_NAND_ID_CODE,
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
        "RGB565 LCD mirror/frame chardev refresh period in milliseconds");

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
    type_register_static(&bbk9588_nand_typeinfo);
    type_register_static(&bbk9588_machine_typeinfo);
}

type_init(bbk9588_machine_register_types);
