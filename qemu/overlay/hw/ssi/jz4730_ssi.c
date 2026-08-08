/*
 * Ingenic JZ4730 synchronous serial interface and BBK TSC2100 endpoint.
 *
 * The BBK JZ4730 firmware uses two-word SPI frames: a TSC2100 command and
 * one data word.  The controller completes transfers synchronously, which
 * matches the firmware's polling driver while preserving the documented
 * FIFO and status-register interface.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/core/irq.h"
#include "hw/ssi/jz4730_ssi.h"
#include "migration/vmstate.h"
#include "qemu/module.h"
#include "qemu/timer.h"

#define JZ4730_SSI_MMIO_SIZE 0x1000u
#define SSI_DR                0x00u
#define SSI_CR0               0x04u
#define SSI_CR1               0x08u
#define SSI_SR                0x0cu
#define SSI_ITR               0x10u
#define SSI_ICR               0x14u
#define SSI_GR                0x18u

#define SSI_CR0_TIE           (1u << 14)
#define SSI_CR0_RIE           (1u << 13)
#define SSI_CR0_TEIE          (1u << 12)
#define SSI_CR0_REIE          (1u << 11)
#define SSI_CR0_TFLUSH        (1u << 2)
#define SSI_CR0_RFLUSH        (1u << 1)
#define SSI_CR0_DISREV        (1u << 0)

#define SSI_SR_END            (1u << 7)
#define SSI_SR_TFF            (1u << 5)
#define SSI_SR_RFE            (1u << 4)
#define SSI_SR_TFHE           (1u << 3)
#define SSI_SR_RFHF           (1u << 2)
#define SSI_SR_UNDR           (1u << 1)
#define SSI_SR_OVER           (1u << 0)

#define SSI_FIFO_DEPTH        16u
#define TSC2100_PAGE_COUNT    3u
#define TSC2100_REG_COUNT     32u

#define TSC2100_PAGE_DATA     0u
#define TSC2100_PAGE_TOUCH    1u
#define TSC2100_PAGE_CODEC    2u
#define TSC2100_REG_X         0u
#define TSC2100_REG_Y         1u
#define TSC2100_REG_Z1        2u
#define TSC2100_REG_Z2        3u
#define TSC2100_REG_BAT1      5u
#define TSC2100_REG_BAT2      6u
#define TSC2100_REG_AUX       7u
#define TSC2100_REG_TEMP1     9u
#define TSC2100_REG_TEMP2     10u
#define TSC2100_REG_ADC       0u
#define TSC2100_REG_STATUS    1u
#define TSC2100_REG_RESET     4u
#define TSC2100_REG_AUDIO_CTRL1 0u
#define TSC2100_REG_DAC_GAIN    2u
#define TSC2100_REG_CODEC_POWER 5u
#define TSC2100_REG_AUDIO_CTRL3 6u

#define TSC2100_ADC_PSTCM     (1u << 15)
#define TSC2100_ADC_ADST      (1u << 14)
#define TSC2100_ADC_ADSCM_SHIFT 10u
#define TSC2100_ADC_ADSCM_MASK  0x0fu
#define TSC2100_STATUS_PEN_DATA_MODE (2u << 14)
#define TSC2100_STATUS_DAVAIL (1u << 11)
#define TSC2100_STATUS_X      (1u << 10)
#define TSC2100_STATUS_Y      (1u << 9)
#define TSC2100_STATUS_Z1     (1u << 8)
#define TSC2100_STATUS_Z2     (1u << 7)
#define TSC2100_STATUS_BAT1   (1u << 6)
#define TSC2100_STATUS_BAT2   (1u << 5)
#define TSC2100_STATUS_AUX    (1u << 4)
#define TSC2100_STATUS_TEMP1  (1u << 2)
#define TSC2100_STATUS_TEMP2  (1u << 1)
#define TSC2100_STATUS_DATA_MASK \
    (TSC2100_STATUS_X | TSC2100_STATUS_Y | TSC2100_STATUS_Z1 | \
     TSC2100_STATUS_Z2 | TSC2100_STATUS_BAT1 | TSC2100_STATUS_BAT2 | \
     TSC2100_STATUS_AUX | TSC2100_STATUS_TEMP1 | TSC2100_STATUS_TEMP2)
#define TSC2100_STATUS_PINTDAV_SHIFT 14u
#define TSC2100_STATUS_PINTDAV_MASK  3u

#define TSC2100_AUDIO_DACFS_SHIFT 3u
#define TSC2100_AUDIO_DACFS_MASK  7u
#define TSC2100_AUDIO_WLEN_SHIFT  10u
#define TSC2100_AUDIO_WLEN_MASK   3u
#define TSC2100_DAC_LEFT_MUTE     (1u << 15)
#define TSC2100_DAC_RIGHT_MUTE    (1u << 7)
#define TSC2100_DAC_VOLUME_MASK   0x7fu
#define TSC2100_CODEC_POWER_DOWN  (1u << 15)
#define TSC2100_CODEC_AST_POWER_DOWN (1u << 13)
#define TSC2100_CODEC_AST_POWER_FLAG (1u << 11)
#define TSC2100_CODEC_DAC_POWER_DOWN (1u << 10)
#define TSC2100_CODEC_ADC_POWER_DOWN (1u << 9)
#define TSC2100_CODEC_ADC_POWER_FLAG (1u << 7)
#define TSC2100_CODEC_DAC_POWER_FLAG (1u << 6)
#define TSC2100_CODEC_POWER_RO_MASK  0x48c0u
#define TSC2100_CODEC_POWER_RESET    0xafc0u
#define TSC2100_AUDIO_REFFS          (1u << 13)
#define TSC2100_MIN_CONVERSION_DELAY_MS 1u
#define TSC2100_SCAN_INTERVAL_MS     16u
#define TSC2100_TOUCH_SAMPLE_STEP    0x00c0u
#define TSC2100_RELEASE_SCAN_COUNT   1u

struct JZ4730SSIState {
    SysBusDevice parent_obj;

    MemoryRegion iomem;
    qemu_irq irq;
    QEMUTimer *touch_timer;
    uint16_t control0;
    uint32_t control1;
    uint16_t interval;
    uint8_t interval_chars;
    uint16_t divider;
    uint8_t error_status;
    uint16_t rx_fifo[SSI_FIFO_DEPTH];
    uint8_t rx_head;
    uint8_t rx_count;

    uint16_t tsc_regs[TSC2100_PAGE_COUNT][TSC2100_REG_COUNT];
    uint8_t tsc_page;
    uint8_t tsc_addr;
    bool tsc_read;
    bool tsc_expect_command;
    bool touch_down;
    bool touch_conversion_pending;
    bool touch_rescan_scheduled;
    uint8_t touch_release_scans_remaining;
    uint16_t touch_raw_x;
    uint16_t touch_raw_y;
    uint16_t touch_sample_x;
    uint16_t touch_sample_y;

    JZ4730TSC2100AudioConfigCallback audio_config_callback;
    void *audio_config_opaque;
    JZ4730TSC2100PintdavCallback pintdav_callback;
    void *pintdav_opaque;
    bool pintdav_high;
};

static void jz4730_tsc2100_set_pintdav(JZ4730SSIState *s, bool high)
{
    if (s->pintdav_high == high) {
        return;
    }
    s->pintdav_high = high;
    if (s->pintdav_callback) {
        s->pintdav_callback(s->pintdav_opaque, high);
    }
}

static unsigned jz4730_tsc2100_pintdav_mode(JZ4730SSIState *s)
{
    uint16_t status =
        s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS];

    return (status >> TSC2100_STATUS_PINTDAV_SHIFT) &
           TSC2100_STATUS_PINTDAV_MASK;
}

static unsigned jz4730_tsc2100_scan_mode(JZ4730SSIState *s)
{
    uint16_t adc = s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_ADC];

    return (adc >> TSC2100_ADC_ADSCM_SHIFT) & TSC2100_ADC_ADSCM_MASK;
}

static bool jz4730_tsc2100_is_touch_scan(JZ4730SSIState *s)
{
    unsigned scan_mode = jz4730_tsc2100_scan_mode(s);

    return scan_mode >= 1 && scan_mode <= 5;
}

static bool jz4730_tsc2100_is_adc_scan(JZ4730SSIState *s)
{
    unsigned scan_mode = jz4730_tsc2100_scan_mode(s);

    return scan_mode >= 1 && scan_mode <= 12;
}

static unsigned jz4730_tsc2100_conversion_delay_ms(JZ4730SSIState *s)
{
    static const uint8_t stabilization_ms[] = {
        0, 1, 1, 1, 5, 10, 50, 100,
    };
    uint16_t adc = s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_ADC];
    unsigned pvstc = (adc >> 1) & 7u;

    if (!jz4730_tsc2100_is_touch_scan(s)) {
        return TSC2100_MIN_CONVERSION_DELAY_MS;
    }
    return TSC2100_MIN_CONVERSION_DELAY_MS + stabilization_ms[pvstc];
}

static void jz4730_tsc2100_update_pintdav(JZ4730SSIState *s)
{
    uint16_t status =
        s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS];

    if (!s->touch_down && s->touch_conversion_pending &&
        s->touch_release_scans_remaining != 0) {
        jz4730_tsc2100_set_pintdav(s, false);
        return;
    }
    switch (jz4730_tsc2100_pintdav_mode(s)) {
    case 0:
        jz4730_tsc2100_set_pintdav(s, !s->touch_down);
        break;
    case 1:
        jz4730_tsc2100_set_pintdav(
            s, !(status & TSC2100_STATUS_DAVAIL));
        break;
    default:
        jz4730_tsc2100_set_pintdav(s,
                                  !s->touch_conversion_pending);
        break;
    }
}

static uint16_t jz4730_tsc2100_conversion_status_mask(JZ4730SSIState *s)
{
    unsigned scan_mode = jz4730_tsc2100_scan_mode(s);

    switch (scan_mode) {
    case 1:
        return TSC2100_STATUS_X | TSC2100_STATUS_Y;
    case 2:
        return TSC2100_STATUS_X | TSC2100_STATUS_Y |
               TSC2100_STATUS_Z1 | TSC2100_STATUS_Z2;
    case 3:
        return TSC2100_STATUS_X;
    case 4:
        return TSC2100_STATUS_Y;
    case 5:
        return TSC2100_STATUS_Z1 | TSC2100_STATUS_Z2;
    case 6:
        return TSC2100_STATUS_BAT1;
    case 7:
        return TSC2100_STATUS_BAT2;
    case 8:
    case 9:
        return TSC2100_STATUS_AUX;
    case 10:
        return TSC2100_STATUS_TEMP1;
    case 11:
        return TSC2100_STATUS_BAT1 | TSC2100_STATUS_BAT2 |
               TSC2100_STATUS_AUX;
    case 12:
        return TSC2100_STATUS_TEMP2;
    default:
        return 0;
    }
}

static uint16_t jz4730_tsc2100_data_status_mask(unsigned addr)
{
    switch (addr) {
    case TSC2100_REG_X:
        return TSC2100_STATUS_X;
    case TSC2100_REG_Y:
        return TSC2100_STATUS_Y;
    case TSC2100_REG_Z1:
        return TSC2100_STATUS_Z1;
    case TSC2100_REG_Z2:
        return TSC2100_STATUS_Z2;
    case TSC2100_REG_BAT1:
        return TSC2100_STATUS_BAT1;
    case TSC2100_REG_BAT2:
        return TSC2100_STATUS_BAT2;
    case TSC2100_REG_AUX:
        return TSC2100_STATUS_AUX;
    case TSC2100_REG_TEMP1:
        return TSC2100_STATUS_TEMP1;
    case TSC2100_REG_TEMP2:
        return TSC2100_STATUS_TEMP2;
    default:
        return 0;
    }
}

static void jz4730_tsc2100_schedule_touch_conversion(JZ4730SSIState *s);

static uint16_t jz4730_tsc2100_approach_sample(uint16_t sample,
                                               uint16_t target)
{
    if (sample + TSC2100_TOUCH_SAMPLE_STEP < target) {
        return sample + TSC2100_TOUCH_SAMPLE_STEP;
    }
    if (target + TSC2100_TOUCH_SAMPLE_STEP < sample) {
        return sample - TSC2100_TOUCH_SAMPLE_STEP;
    }
    return target;
}

static void jz4730_tsc2100_complete_touch_conversion(void *opaque)
{
    JZ4730SSIState *s = opaque;
    uint16_t ready_mask;
    uint16_t status;

    if (s->touch_rescan_scheduled) {
        s->touch_rescan_scheduled = false;
        if (s->touch_down && jz4730_tsc2100_is_touch_scan(s)) {
            jz4730_tsc2100_schedule_touch_conversion(s);
        } else if (jz4730_tsc2100_scan_mode(s) == 9) {
            jz4730_tsc2100_schedule_touch_conversion(s);
        } else if (s->touch_release_scans_remaining != 0) {
            jz4730_tsc2100_set_pintdav(s, true);
            s->touch_conversion_pending = true;
            jz4730_tsc2100_update_pintdav(s);
            timer_mod(s->touch_timer,
                      qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
                      jz4730_tsc2100_conversion_delay_ms(s));
        }
        return;
    }
    if (!s->touch_conversion_pending) {
        return;
    }
    s->touch_conversion_pending = false;
    if (jz4730_tsc2100_is_touch_scan(s) && !s->touch_down) {
        jz4730_tsc2100_update_pintdav(s);
        if (s->touch_release_scans_remaining != 0) {
            s->touch_release_scans_remaining--;
            if (s->touch_release_scans_remaining != 0) {
                s->touch_rescan_scheduled = true;
                timer_mod(s->touch_timer,
                          qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
                          TSC2100_SCAN_INTERVAL_MS);
            }
        }
        return;
    }
    ready_mask = jz4730_tsc2100_conversion_status_mask(s);
    if (ready_mask == 0) {
        jz4730_tsc2100_update_pintdav(s);
        return;
    }
    if (!s->touch_down && s->touch_release_scans_remaining != 0) {
        s->touch_release_scans_remaining = 0;
    }
    status = s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS];
    status |= TSC2100_STATUS_DAVAIL | ready_mask;
    s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS] = status;
    jz4730_tsc2100_update_pintdav(s);
    if ((jz4730_tsc2100_is_touch_scan(s) && s->touch_down &&
         jz4730_tsc2100_pintdav_mode(s) != 0) ||
        jz4730_tsc2100_scan_mode(s) == 9) {
        s->touch_rescan_scheduled = true;
        timer_mod(s->touch_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
                  TSC2100_SCAN_INTERVAL_MS);
    }
}

static void jz4730_tsc2100_schedule_touch_conversion(JZ4730SSIState *s)
{
    uint16_t status;

    if (!jz4730_tsc2100_is_adc_scan(s) ||
        (jz4730_tsc2100_is_touch_scan(s) && !s->touch_down)) {
        s->touch_conversion_pending = false;
        s->touch_rescan_scheduled = false;
        timer_del(s->touch_timer);
        jz4730_tsc2100_update_pintdav(s);
        return;
    }

    status = s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS];
    status &= ~jz4730_tsc2100_conversion_status_mask(s);
    if (!(status & TSC2100_STATUS_DATA_MASK)) {
        status &= ~TSC2100_STATUS_DAVAIL;
    }
    s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS] = status;
    jz4730_tsc2100_update_pintdav(s);

    if (jz4730_tsc2100_is_touch_scan(s)) {
        s->touch_sample_x = jz4730_tsc2100_approach_sample(
            s->touch_sample_x, s->touch_raw_x);
        s->touch_sample_y = jz4730_tsc2100_approach_sample(
            s->touch_sample_y, s->touch_raw_y);
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_X] = s->touch_sample_x;
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_Y] = s->touch_sample_y;
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_Z1] = 0x0600;
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_Z2] = 0x0c00;
    } else {
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_BAT1] = 0x0880;
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_BAT2] = 0;
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_AUX] = 0;
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_TEMP1] = 0x0800;
        s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_TEMP2] = 0x0800;
    }
    s->touch_rescan_scheduled = false;
    s->touch_conversion_pending = true;
    jz4730_tsc2100_update_pintdav(s);
    timer_mod(s->touch_timer,
              qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
              jz4730_tsc2100_conversion_delay_ms(s));
}

static uint32_t jz4730_tsc2100_dac_sample_rate(JZ4730SSIState *s)
{
    static const uint32_t rates_48000[] = {
        48000, 32000, 24000, 16000, 12000, 9600, 8727, 8000,
    };
    static const uint32_t rates_44100[] = {
        44100, 29400, 22050, 14700, 11025, 8820, 8018, 7350,
    };
    uint16_t control1 =
        s->tsc_regs[TSC2100_PAGE_CODEC][TSC2100_REG_AUDIO_CTRL1];
    uint16_t control3 =
        s->tsc_regs[TSC2100_PAGE_CODEC][TSC2100_REG_AUDIO_CTRL3];
    unsigned index = (control1 >> TSC2100_AUDIO_DACFS_SHIFT) &
                     TSC2100_AUDIO_DACFS_MASK;

    return (control3 & TSC2100_AUDIO_REFFS) ?
           rates_44100[index] : rates_48000[index];
}

static uint8_t jz4730_tsc2100_sample_bits(JZ4730SSIState *s)
{
    static const uint8_t sample_bits[] = { 16, 20, 24, 32 };
    uint16_t control1 =
        s->tsc_regs[TSC2100_PAGE_CODEC][TSC2100_REG_AUDIO_CTRL1];
    unsigned index = (control1 >> TSC2100_AUDIO_WLEN_SHIFT) &
                     TSC2100_AUDIO_WLEN_MASK;

    return sample_bits[index];
}

static void jz4730_tsc2100_notify_audio_config(JZ4730SSIState *s)
{
    uint16_t gain;
    uint16_t power;
    bool left_muted;
    bool right_muted;
    bool muted;
    uint8_t left_attenuation;
    uint8_t right_attenuation;

    if (!s->audio_config_callback) {
        return;
    }
    gain = s->tsc_regs[TSC2100_PAGE_CODEC][TSC2100_REG_DAC_GAIN];
    power = s->tsc_regs[TSC2100_PAGE_CODEC][TSC2100_REG_CODEC_POWER];
    left_muted = (gain & TSC2100_DAC_LEFT_MUTE) != 0;
    right_muted = (gain & TSC2100_DAC_RIGHT_MUTE) != 0;
    muted = (power & (TSC2100_CODEC_POWER_DOWN |
                      TSC2100_CODEC_DAC_POWER_DOWN)) != 0 ||
            (left_muted && right_muted);
    left_attenuation = left_muted ? 127 :
        (gain >> 8) & TSC2100_DAC_VOLUME_MASK;
    right_attenuation = right_muted ? 127 :
        gain & TSC2100_DAC_VOLUME_MASK;
    s->audio_config_callback(
        s->audio_config_opaque, jz4730_tsc2100_dac_sample_rate(s),
        jz4730_tsc2100_sample_bits(s), muted,
        left_attenuation, right_attenuation);
}

static unsigned jz4730_ssi_rx_threshold(JZ4730SSIState *s)
{
    static const unsigned thresholds[] = { 1, 4, 8, 14 };

    return thresholds[(s->control1 >> 8) & 3u];
}

static uint32_t jz4730_ssi_status(JZ4730SSIState *s)
{
    uint32_t status = SSI_SR_END | SSI_SR_TFHE | s->error_status;

    status |= (uint32_t)s->rx_count << 8;
    if (s->rx_count == 0) {
        status |= SSI_SR_RFE;
    }
    if (s->rx_count >= jz4730_ssi_rx_threshold(s)) {
        status |= SSI_SR_RFHF;
    }
    return status;
}

static void jz4730_ssi_update_irq(JZ4730SSIState *s)
{
    uint32_t status = jz4730_ssi_status(s);
    bool level = ((s->control0 & SSI_CR0_TIE) &&
                  (status & SSI_SR_TFHE)) ||
                 ((s->control0 & SSI_CR0_RIE) &&
                  (status & SSI_SR_RFHF)) ||
                 ((s->control0 & SSI_CR0_TEIE) &&
                  (status & SSI_SR_UNDR)) ||
                 ((s->control0 & SSI_CR0_REIE) &&
                  (status & SSI_SR_OVER));

    qemu_set_irq(s->irq, level);
}

static void jz4730_ssi_flush_rx(JZ4730SSIState *s)
{
    s->rx_head = 0;
    s->rx_count = 0;
    s->tsc_expect_command = true;
}

static void jz4730_ssi_push_rx(JZ4730SSIState *s, uint16_t value)
{
    unsigned tail;

    if (s->rx_count == SSI_FIFO_DEPTH) {
        s->error_status |= SSI_SR_OVER;
        return;
    }
    tail = (s->rx_head + s->rx_count) % SSI_FIFO_DEPTH;
    s->rx_fifo[tail] = value;
    s->rx_count++;
}

static uint16_t jz4730_ssi_pop_rx(JZ4730SSIState *s)
{
    uint16_t value = 0;

    if (s->rx_count != 0) {
        value = s->rx_fifo[s->rx_head];
        s->rx_head = (s->rx_head + 1) % SSI_FIFO_DEPTH;
        s->rx_count--;
    }
    return value;
}

static void jz4730_tsc2100_reset(JZ4730SSIState *s)
{
    memset(s->tsc_regs, 0, sizeof(s->tsc_regs));
    s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS] =
        TSC2100_STATUS_PEN_DATA_MODE;
    s->tsc_regs[TSC2100_PAGE_TOUCH][3] = 0x0002;
    s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_RESET] = 0xffff;
    s->tsc_regs[TSC2100_PAGE_CODEC][TSC2100_REG_DAC_GAIN] = 0xffff;
    s->tsc_regs[TSC2100_PAGE_CODEC][TSC2100_REG_CODEC_POWER] =
        TSC2100_CODEC_POWER_RESET;
    s->tsc_page = 0;
    s->tsc_addr = 0;
    s->tsc_read = false;
    s->tsc_expect_command = true;
    s->touch_down = false;
    s->touch_conversion_pending = false;
    s->touch_rescan_scheduled = false;
    s->touch_release_scans_remaining = 0;
    s->touch_raw_x = 0;
    s->touch_raw_y = 0;
    s->touch_sample_x = 0;
    s->touch_sample_y = 0;
    timer_del(s->touch_timer);
    jz4730_tsc2100_set_pintdav(s, true);
    jz4730_tsc2100_notify_audio_config(s);
    jz4730_tsc2100_update_pintdav(s);
}

static uint16_t jz4730_tsc2100_read(JZ4730SSIState *s, unsigned page,
                                    unsigned addr)
{
    uint16_t data_status;
    uint16_t value;

    if (page >= TSC2100_PAGE_COUNT || addr >= TSC2100_REG_COUNT) {
        return 0xffff;
    }
    if (page == TSC2100_PAGE_TOUCH && addr == TSC2100_REG_ADC) {
        value = s->tsc_regs[page][addr] & 0x3fffu;
        if (!s->touch_conversion_pending) {
            value |= TSC2100_ADC_ADST;
        }
        if (s->touch_down) {
            value |= TSC2100_ADC_PSTCM;
        }
        return value;
    }
    value = s->tsc_regs[page][addr];
    data_status = page == TSC2100_PAGE_DATA ?
        jz4730_tsc2100_data_status_mask(addr) : 0;
    if (data_status != 0) {
        s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS] &=
            ~data_status;
        if (!(s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS] &
              TSC2100_STATUS_DATA_MASK)) {
            s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS] &=
                ~TSC2100_STATUS_DAVAIL;
        }
        jz4730_tsc2100_update_pintdav(s);
    }
    return value;
}

static void jz4730_tsc2100_write(JZ4730SSIState *s, unsigned page,
                                 unsigned addr, uint16_t value)
{
    if (page >= TSC2100_PAGE_COUNT || addr >= TSC2100_REG_COUNT ||
        page == TSC2100_PAGE_DATA) {
        return;
    }
    if (page == TSC2100_PAGE_TOUCH && addr == TSC2100_REG_RESET &&
        value == 0xbb00) {
        jz4730_tsc2100_reset(s);
        return;
    }
    if (page == TSC2100_PAGE_TOUCH && addr == TSC2100_REG_STATUS) {
        s->tsc_regs[page][addr] =
            (s->tsc_regs[page][addr] & 0x3fffu) | (value & 0xc000u);
        jz4730_tsc2100_update_pintdav(s);
        if (s->touch_down && jz4730_tsc2100_is_touch_scan(s) &&
            jz4730_tsc2100_pintdav_mode(s) == 1) {
            timer_del(s->touch_timer);
            jz4730_tsc2100_schedule_touch_conversion(s);
        }
        return;
    }
    if (page == TSC2100_PAGE_TOUCH && addr == TSC2100_REG_ADC) {
        s->tsc_regs[page][addr] = value;
        if (value & TSC2100_ADC_ADST) {
            s->touch_conversion_pending = false;
            s->touch_rescan_scheduled = false;
            timer_del(s->touch_timer);
            jz4730_tsc2100_update_pintdav(s);
        } else if (jz4730_tsc2100_is_adc_scan(s) &&
                   (!jz4730_tsc2100_is_touch_scan(s) || s->touch_down)) {
            timer_del(s->touch_timer);
            jz4730_tsc2100_schedule_touch_conversion(s);
            if (!jz4730_tsc2100_is_touch_scan(s)) {
                /* These conversions complete in microseconds on the chip. */
                timer_del(s->touch_timer);
                jz4730_tsc2100_complete_touch_conversion(s);
            }
        } else {
            jz4730_tsc2100_update_pintdav(s);
        }
        return;
    }
    if (page == TSC2100_PAGE_CODEC &&
        addr == TSC2100_REG_CODEC_POWER) {
        uint16_t power = value & ~TSC2100_CODEC_POWER_RO_MASK;

        if (value & TSC2100_CODEC_AST_POWER_DOWN) {
            power |= TSC2100_CODEC_AST_POWER_FLAG;
        }
        if (value & (TSC2100_CODEC_POWER_DOWN |
                     TSC2100_CODEC_ADC_POWER_DOWN)) {
            power |= TSC2100_CODEC_ADC_POWER_FLAG;
        }
        if (value & (TSC2100_CODEC_POWER_DOWN |
                     TSC2100_CODEC_DAC_POWER_DOWN)) {
            power |= TSC2100_CODEC_DAC_POWER_FLAG;
        }
        s->tsc_regs[page][addr] = power;
        jz4730_tsc2100_notify_audio_config(s);
        return;
    }
    s->tsc_regs[page][addr] = value;
    if (page == TSC2100_PAGE_CODEC &&
        (addr == TSC2100_REG_AUDIO_CTRL1 ||
         addr == TSC2100_REG_DAC_GAIN ||
         addr == TSC2100_REG_AUDIO_CTRL3)) {
        jz4730_tsc2100_notify_audio_config(s);
    }
}

static uint16_t jz4730_tsc2100_transfer(JZ4730SSIState *s, uint16_t word)
{
    uint16_t response = 0;

    if (s->tsc_expect_command) {
        s->tsc_read = (word & 0x8000u) != 0;
        s->tsc_page = (word >> 11) & 0x0fu;
        s->tsc_addr = (word >> 5) & 0x3fu;
        s->tsc_expect_command = false;
        return 0;
    }
    if (s->tsc_read) {
        response = jz4730_tsc2100_read(s, s->tsc_page, s->tsc_addr);
    } else {
        jz4730_tsc2100_write(s, s->tsc_page, s->tsc_addr, word);
    }
    s->tsc_addr++;
    s->tsc_expect_command = true;
    return response;
}

void jz4730_ssi_set_touch(JZ4730SSIState *s, uint16_t raw_x,
                          uint16_t raw_y, bool down)
{
    bool was_down;
    uint16_t status;

    if (!s) {
        return;
    }
    was_down = s->touch_down;
    s->touch_down = down;
    s->touch_raw_x = raw_x & 0x0fffu;
    s->touch_raw_y = raw_y & 0x0fffu;
    if (down) {
        s->touch_release_scans_remaining = 0;
        if (!was_down) {
            s->touch_sample_x = s->touch_raw_x;
            s->touch_sample_y = s->touch_raw_y;
        }
        if (jz4730_tsc2100_is_touch_scan(s)) {
            timer_del(s->touch_timer);
            jz4730_tsc2100_schedule_touch_conversion(s);
        } else {
            jz4730_tsc2100_update_pintdav(s);
        }
        return;
    }

    if (jz4730_tsc2100_is_touch_scan(s)) {
        s->touch_conversion_pending = false;
        s->touch_rescan_scheduled = false;
        timer_del(s->touch_timer);
    }
    s->touch_release_scans_remaining = 0;
    s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_Z1] = 0;
    s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_Z2] = 0;
    status = s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS];
    status &= ~(TSC2100_STATUS_X | TSC2100_STATUS_Y |
                TSC2100_STATUS_Z1 | TSC2100_STATUS_Z2);
    if (!(status & TSC2100_STATUS_DATA_MASK)) {
        status &= ~TSC2100_STATUS_DAVAIL;
    }
    s->tsc_regs[TSC2100_PAGE_TOUCH][TSC2100_REG_STATUS] = status;
    if (was_down && jz4730_tsc2100_pintdav_mode(s) != 1) {
        /* Preserve a final status edge for non-data-only configurations. */
        s->touch_release_scans_remaining = TSC2100_RELEASE_SCAN_COUNT;
        jz4730_tsc2100_set_pintdav(s, true);
        s->touch_conversion_pending = true;
        jz4730_tsc2100_update_pintdav(s);
        timer_mod(s->touch_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
                  jz4730_tsc2100_conversion_delay_ms(s));
        return;
    }
    jz4730_tsc2100_update_pintdav(s);
}

void jz4730_ssi_set_audio_config_callback(
    JZ4730SSIState *s, JZ4730TSC2100AudioConfigCallback callback,
    void *opaque)
{
    if (!s) {
        return;
    }
    s->audio_config_callback = callback;
    s->audio_config_opaque = opaque;
    jz4730_tsc2100_notify_audio_config(s);
}

void jz4730_ssi_set_pintdav_callback(
    JZ4730SSIState *s, JZ4730TSC2100PintdavCallback callback, void *opaque)
{
    if (!s) {
        return;
    }
    s->pintdav_callback = callback;
    s->pintdav_opaque = opaque;
    if (callback) {
        callback(opaque, s->pintdav_high);
    }
}

static uint64_t jz4730_ssi_read(void *opaque, hwaddr offset, unsigned size)
{
    JZ4730SSIState *s = JZ4730_SSI(opaque);
    uint64_t value = 0;

    switch (offset) {
    case SSI_DR:
        value = jz4730_ssi_pop_rx(s);
        break;
    case SSI_CR0:
        value = s->control0;
        break;
    case SSI_CR1:
        value = s->control1;
        break;
    case SSI_SR:
        value = jz4730_ssi_status(s);
        break;
    case SSI_ITR:
        value = s->interval;
        break;
    case SSI_ICR:
        value = s->interval_chars;
        break;
    case SSI_GR:
        value = s->divider;
        break;
    default:
        break;
    }
    jz4730_ssi_update_irq(s);
    return value;
}

static void jz4730_ssi_write(void *opaque, hwaddr offset, uint64_t value,
                             unsigned size)
{
    JZ4730SSIState *s = JZ4730_SSI(opaque);

    switch (offset) {
    case SSI_DR:
        value = jz4730_tsc2100_transfer(s, value & 0xffffu);
        if (!(s->control0 & SSI_CR0_DISREV)) {
            jz4730_ssi_push_rx(s, value);
        }
        break;
    case SSI_CR0:
        if (value & SSI_CR0_RFLUSH) {
            jz4730_ssi_flush_rx(s);
        }
        s->control0 = value & ~(SSI_CR0_TFLUSH | SSI_CR0_RFLUSH);
        break;
    case SSI_CR1:
        s->control1 = value & 0x007fffffu;
        break;
    case SSI_SR:
        s->error_status &= value & (SSI_SR_UNDR | SSI_SR_OVER);
        break;
    case SSI_ITR:
        s->interval = value & 0xffffu;
        break;
    case SSI_ICR:
        s->interval_chars = value & 0x07u;
        break;
    case SSI_GR:
        s->divider = value & 0xffffu;
        break;
    default:
        break;
    }
    jz4730_ssi_update_irq(s);
}

static const MemoryRegionOps jz4730_ssi_ops = {
    .read = jz4730_ssi_read,
    .write = jz4730_ssi_write,
    .endianness = DEVICE_LITTLE_ENDIAN,
    .valid = {
        .min_access_size = 1,
        .max_access_size = 4,
        .unaligned = false,
    },
};

static void jz4730_ssi_reset_hold(Object *obj, ResetType type)
{
    JZ4730SSIState *s = JZ4730_SSI(obj);

    s->control0 = 0;
    s->control1 = 0x00007060u;
    s->interval = 0;
    s->interval_chars = 0;
    s->divider = 0;
    s->error_status = 0;
    jz4730_ssi_flush_rx(s);
    jz4730_tsc2100_reset(s);
    qemu_set_irq(s->irq, 0);
}

static int jz4730_ssi_post_load(void *opaque, int version_id)
{
    JZ4730SSIState *s = opaque;

    if (version_id < 2) {
        s->pintdav_high = true;
    }
    if (version_id < 3) {
        s->touch_conversion_pending = false;
        s->touch_raw_x = s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_X];
        s->touch_raw_y = s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_Y];
    }
    if (version_id < 4) {
        s->touch_rescan_scheduled = false;
    }
    if (version_id < 5) {
        s->touch_sample_x =
            s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_X];
        s->touch_sample_y =
            s->tsc_regs[TSC2100_PAGE_DATA][TSC2100_REG_Y];
    }
    if (version_id < 6) {
        s->touch_release_scans_remaining = 0;
    }
    if (s->touch_conversion_pending || s->touch_rescan_scheduled) {
        timer_mod(s->touch_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
                  jz4730_tsc2100_conversion_delay_ms(s));
    }
    jz4730_tsc2100_update_pintdav(s);
    jz4730_ssi_update_irq(s);
    jz4730_tsc2100_notify_audio_config(s);
    if (s->pintdav_callback) {
        s->pintdav_callback(s->pintdav_opaque, s->pintdav_high);
    }
    return 0;
}

static const VMStateDescription vmstate_jz4730_ssi = {
    .name = TYPE_JZ4730_SSI,
    .version_id = 6,
    .minimum_version_id = 1,
    .post_load = jz4730_ssi_post_load,
    .fields = (const VMStateField[]) {
        VMSTATE_UINT16(control0, JZ4730SSIState),
        VMSTATE_UINT32(control1, JZ4730SSIState),
        VMSTATE_UINT16(interval, JZ4730SSIState),
        VMSTATE_UINT8(interval_chars, JZ4730SSIState),
        VMSTATE_UINT16(divider, JZ4730SSIState),
        VMSTATE_UINT8(error_status, JZ4730SSIState),
        VMSTATE_UINT16_ARRAY(rx_fifo, JZ4730SSIState, SSI_FIFO_DEPTH),
        VMSTATE_UINT8(rx_head, JZ4730SSIState),
        VMSTATE_UINT8(rx_count, JZ4730SSIState),
        VMSTATE_UINT16_2DARRAY(tsc_regs, JZ4730SSIState,
                              TSC2100_PAGE_COUNT, TSC2100_REG_COUNT),
        VMSTATE_UINT8(tsc_page, JZ4730SSIState),
        VMSTATE_UINT8(tsc_addr, JZ4730SSIState),
        VMSTATE_BOOL(tsc_read, JZ4730SSIState),
        VMSTATE_BOOL(tsc_expect_command, JZ4730SSIState),
        VMSTATE_BOOL(touch_down, JZ4730SSIState),
        VMSTATE_BOOL_V(pintdav_high, JZ4730SSIState, 2),
        VMSTATE_BOOL_V(touch_conversion_pending, JZ4730SSIState, 3),
        VMSTATE_UINT16_V(touch_raw_x, JZ4730SSIState, 3),
        VMSTATE_UINT16_V(touch_raw_y, JZ4730SSIState, 3),
        VMSTATE_BOOL_V(touch_rescan_scheduled, JZ4730SSIState, 4),
        VMSTATE_UINT16_V(touch_sample_x, JZ4730SSIState, 5),
        VMSTATE_UINT16_V(touch_sample_y, JZ4730SSIState, 5),
        VMSTATE_UINT8_V(touch_release_scans_remaining,
                        JZ4730SSIState, 6),
        VMSTATE_END_OF_LIST(),
    },
};

static void jz4730_ssi_init(Object *obj)
{
    JZ4730SSIState *s = JZ4730_SSI(obj);
    SysBusDevice *sbd = SYS_BUS_DEVICE(obj);

    memory_region_init_io(&s->iomem, obj, &jz4730_ssi_ops, s,
                          TYPE_JZ4730_SSI, JZ4730_SSI_MMIO_SIZE);
    sysbus_init_mmio(sbd, &s->iomem);
    sysbus_init_irq(sbd, &s->irq);
    s->touch_timer = timer_new_ms(QEMU_CLOCK_REALTIME,
                                  jz4730_tsc2100_complete_touch_conversion,
                                  s);
}

static void jz4730_ssi_finalize(Object *obj)
{
    JZ4730SSIState *s = JZ4730_SSI(obj);

    timer_free(s->touch_timer);
}

static void jz4730_ssi_class_init(ObjectClass *klass, const void *data)
{
    DeviceClass *dc = DEVICE_CLASS(klass);
    ResettableClass *rc = RESETTABLE_CLASS(klass);

    dc->vmsd = &vmstate_jz4730_ssi;
    rc->phases.hold = jz4730_ssi_reset_hold;
}

static const TypeInfo jz4730_ssi_info = {
    .name = TYPE_JZ4730_SSI,
    .parent = TYPE_SYS_BUS_DEVICE,
    .instance_size = sizeof(JZ4730SSIState),
    .instance_init = jz4730_ssi_init,
    .instance_finalize = jz4730_ssi_finalize,
    .class_init = jz4730_ssi_class_init,
};

static void jz4730_ssi_register_types(void)
{
    type_register_static(&jz4730_ssi_info);
}

type_init(jz4730_ssi_register_types)
