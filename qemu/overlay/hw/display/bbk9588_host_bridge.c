/*
 * BBK 9588 host display/audio/performance bridge.
 *
 * This device keeps host transport and scanout policy out of the JZ4740 LCD,
 * AIC and DMAC devices.  It has no guest-visible registers.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "system/address-spaces.h"
#include "chardev/char.h"
#include "chardev/char-fe.h"
#include "exec/cpu-common.h"
#include "hw/audio/jz4740_aic.h"
#include "hw/display/bbk9588_host_bridge.h"
#include "hw/display/bbk9588_panel.h"
#include "hw/display/jz4740_lcd.h"
#include "hw/dma/jz4740_dmac.h"
#include "qapi/error.h"
#include "qemu/module.h"
#include "qemu/timer.h"
#include "qemu/units.h"
#include "ui/console.h"
#include "ui/surface.h"

#define BBK9588_RAM_SIZE              (160u * MiB)
#define BBK9588_KSEG_TO_PHYS(addr)    ((addr) & 0x1fffffffu)
#define BBK9588_LCD_WIDTH              240u
#define BBK9588_LCD_HEIGHT             320u
#define BBK9588_LCD_STRIDE             (BBK9588_LCD_WIDTH * 2u)
#define BBK9588_LCD_BYTES              (BBK9588_LCD_STRIDE * BBK9588_LCD_HEIGHT)
#define BBK9588_FRAME_MAGIC            0x464b4242u
#define BBK9588_PERF_MAGIC             0x504b4242u
#define BBK9588_AUDIO_MAGIC            0x414b4242u
#define BBK9588_FRAME_FORMAT_RGB565    0x00005635u
#define BBK9588_AUDIO_FORMAT_S16LE     0x36314c53u
#define BBK9588_PERF_FORMAT_GUEST_INSNS 0x00004950u
#define BBK9588_PERF_PAYLOAD_BYTES     16u
#define BBK9588_PERF_FORMAT_AIC        0x00434941u
#define BBK9588_AIC_PERF_WORDS         24u
#define BBK9588_AIC_PERF_PAYLOAD_BYTES \
    (BBK9588_AIC_PERF_WORDS * sizeof(uint64_t))
#define BBK9588_PERF_PERIOD_MS         1000u

struct Bbk9588HostBridgeState {
    DeviceState parent_obj;

    CharFrontend frame_chr;
    QEMUTimer *refresh_timer;
    QemuConsole *console;
    DisplaySurface *surface;
    uint8_t framebuffer[BBK9588_LCD_BYTES];
    uint8_t last_framebuffer[BBK9588_LCD_BYTES];
    JZ4740LCDState *lcd;
    Bbk9588PanelState *panel;
    JZ4740AICState *aic;
    JZ4740DMACState *dmac;
    Bbk9588GuestInsnCallback guest_insns;
    void *guest_insns_opaque;
    uint32_t frame_seq;
    uint32_t perf_seq;
    uint32_t audio_seq;
    uint32_t refresh_period_ms;
    int64_t scanout_not_before_ms;
    int64_t frame_stable_not_before_ms;
    int64_t perf_last_send_ms;
    bool frame_chr_initialized;
    bool frame_chardev_configured;
    bool last_frame_valid;
    bool frame_chardev_sent_valid;
};

static bool host_guest_ram_address_valid(uint32_t address, uint32_t size)
{
    uint32_t segment = address & 0xe0000000u;
    uint32_t phys = BBK9588_KSEG_TO_PHYS(address);

    return (segment == 0 || segment == 0x80000000u ||
            segment == 0xa0000000u) &&
           size <= BBK9588_RAM_SIZE && phys <= BBK9588_RAM_SIZE - size;
}

static bool host_copy_framebuffer(Bbk9588HostBridgeState *s)
{
    uint32_t fb_va;

    if (!s->lcd) {
        return false;
    }
    jz4740_lcd_refresh_frame_source(s->lcd);
    if (!jz4740_lcd_get_frame_source(s->lcd, &fb_va) ||
        !host_guest_ram_address_valid(fb_va, sizeof(s->framebuffer))) {
        return false;
    }

    cpu_physical_memory_read(BBK9588_KSEG_TO_PHYS(fb_va), s->framebuffer,
                             sizeof(s->framebuffer));
    return true;
}

static bool host_send_frame(Bbk9588HostBridgeState *s)
{
    uint32_t header[7];

    if (!qemu_chr_fe_backend_connected(&s->frame_chr)) {
        return false;
    }

    s->frame_seq++;
    header[0] = cpu_to_le32(BBK9588_FRAME_MAGIC);
    header[1] = cpu_to_le32(s->frame_seq);
    header[2] = cpu_to_le32(BBK9588_LCD_WIDTH);
    header[3] = cpu_to_le32(BBK9588_LCD_HEIGHT);
    header[4] = cpu_to_le32(BBK9588_LCD_STRIDE);
    header[5] = cpu_to_le32(BBK9588_FRAME_FORMAT_RGB565);
    header[6] = cpu_to_le32(BBK9588_LCD_BYTES);

    if (qemu_chr_fe_write_all(&s->frame_chr, (const uint8_t *)header,
                              sizeof(header)) < 0 ||
        qemu_chr_fe_write_all(&s->frame_chr, s->framebuffer,
                              sizeof(s->framebuffer)) < 0) {
        return false;
    }
    return true;
}

static bool host_send_metrics(Bbk9588HostBridgeState *s, int64_t now_ms)
{
    uint32_t header[7];
    uint64_t payload[2];
    uint64_t insns;

    if (!s->guest_insns || !qemu_chr_fe_backend_connected(&s->frame_chr)) {
        return false;
    }

    insns = s->guest_insns(s->guest_insns_opaque);
    s->perf_seq++;
    header[0] = cpu_to_le32(BBK9588_PERF_MAGIC);
    header[1] = cpu_to_le32(s->perf_seq);
    header[2] = cpu_to_le32(1);
    header[3] = cpu_to_le32(0);
    header[4] = cpu_to_le32(0);
    header[5] = cpu_to_le32(BBK9588_PERF_FORMAT_GUEST_INSNS);
    header[6] = cpu_to_le32(BBK9588_PERF_PAYLOAD_BYTES);
    payload[0] = cpu_to_le64(insns);
    payload[1] = cpu_to_le64((uint64_t)now_ms);

    if (qemu_chr_fe_write_all(&s->frame_chr, (const uint8_t *)header,
                              sizeof(header)) < 0 ||
        qemu_chr_fe_write_all(&s->frame_chr, (const uint8_t *)payload,
                              sizeof(payload)) < 0) {
        return false;
    }

    if (s->aic && s->dmac) {
        JZ4740AICDiagnostics diagnostics;
        JZ4740DMACDiagnostics dmac_diagnostics;
        uint64_t audio_payload[BBK9588_AIC_PERF_WORDS];

        jz4740_aic_get_diagnostics(s->aic, &diagnostics);
        jz4740_dmac_get_diagnostics(s->dmac, &dmac_diagnostics);
        s->perf_seq++;
        header[0] = cpu_to_le32(BBK9588_PERF_MAGIC);
        header[1] = cpu_to_le32(s->perf_seq);
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
        audio_payload[14] =
            cpu_to_le64(dmac_diagnostics.audio_completion_count);
        audio_payload[15] = cpu_to_le64(dmac_diagnostics.audio_rearm_count);
        audio_payload[16] =
            cpu_to_le64(dmac_diagnostics.audio_last_rearm_gap_ns);
        audio_payload[17] =
            cpu_to_le64(dmac_diagnostics.audio_max_rearm_gap_ns);
        audio_payload[18] =
            cpu_to_le64(dmac_diagnostics.audio_total_rearm_gap_ns);
        audio_payload[19] =
            cpu_to_le64(dmac_diagnostics.audio_last_gap_underruns);
        audio_payload[20] =
            cpu_to_le64(dmac_diagnostics.audio_total_gap_underruns);
        audio_payload[21] = cpu_to_le64(dmac_diagnostics.audio_last_units);
        audio_payload[22] = cpu_to_le64(dmac_diagnostics.audio_completion_fifo);
        audio_payload[23] = cpu_to_le64(dmac_diagnostics.audio_rearm_fifo);

        if (qemu_chr_fe_write_all(&s->frame_chr,
                                  (const uint8_t *)header,
                                  sizeof(header)) < 0 ||
            qemu_chr_fe_write_all(&s->frame_chr,
                                  (const uint8_t *)audio_payload,
                                  sizeof(audio_payload)) < 0) {
            return false;
        }
    }
    return true;
}

static void host_maybe_send_metrics(Bbk9588HostBridgeState *s,
                                    int64_t now_ms)
{
    if (s->perf_last_send_ms != 0 &&
        now_ms - s->perf_last_send_ms < BBK9588_PERF_PERIOD_MS) {
        return;
    }
    if (host_send_metrics(s, now_ms)) {
        s->perf_last_send_ms = now_ms;
    }
}

static bool host_frame_changed(Bbk9588HostBridgeState *s)
{
    if (!s->last_frame_valid ||
        memcmp(s->framebuffer, s->last_framebuffer,
               sizeof(s->framebuffer)) != 0) {
        memcpy(s->last_framebuffer, s->framebuffer,
               sizeof(s->framebuffer));
        s->last_frame_valid = true;
        return true;
    }
    return false;
}

static void host_gfx_update(void *opaque)
{
    Bbk9588HostBridgeState *s = opaque;
    bool changed;
    int64_t now = qemu_clock_get_ms(QEMU_CLOCK_REALTIME);

    if (!host_copy_framebuffer(s)) {
        host_maybe_send_metrics(s, now);
        return;
    }
    changed = host_frame_changed(s);
    if (changed) {
        bbk9588_panel_set_frame_done(s->panel);
        jz4740_lcd_signal_frame_done(s->lcd);
        s->frame_chardev_sent_valid = false;
        s->frame_stable_not_before_ms = now;
    }
    host_maybe_send_metrics(s, now);
    if (qemu_chr_fe_backend_connected(&s->frame_chr) &&
        !s->frame_chardev_sent_valid &&
        s->frame_stable_not_before_ms <= now) {
        s->frame_chardev_sent_valid = host_send_frame(s);
    }
    if (!changed || !s->console) {
        return;
    }
    if (!s->surface) {
        s->surface = qemu_create_displaysurface_from(
            BBK9588_LCD_WIDTH, BBK9588_LCD_HEIGHT, PIXMAN_r5g6b5,
            BBK9588_LCD_STRIDE, s->framebuffer);
        dpy_gfx_replace_surface(s->console, s->surface);
    }
    dpy_gfx_update(s->console, 0, 0, BBK9588_LCD_WIDTH,
                   BBK9588_LCD_HEIGHT);
}

static void host_invalidate(void *opaque)
{
    host_gfx_update(opaque);
}

static const GraphicHwOps host_lcd_ops = {
    .invalidate = host_invalidate,
    .gfx_update = host_gfx_update,
};

static void host_schedule_refresh(Bbk9588HostBridgeState *s)
{
    if (s->refresh_timer) {
        timer_mod(s->refresh_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) +
                  s->refresh_period_ms);
    }
}

static void host_schedule_vblank(Bbk9588HostBridgeState *s)
{
    if (s->refresh_timer) {
        timer_mod(s->refresh_timer,
                  qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + 1);
    }
}

static void host_refresh_timer_cb(void *opaque)
{
    Bbk9588HostBridgeState *s = opaque;
    int64_t now = qemu_clock_get_ms(QEMU_CLOCK_REALTIME);

    if (s->scanout_not_before_ms > now) {
        timer_mod(s->refresh_timer, s->scanout_not_before_ms);
        return;
    }

    host_gfx_update(s);
    host_maybe_send_metrics(s, now);
    if (s->frame_chardev_configured && !s->frame_chardev_sent_valid) {
        now = qemu_clock_get_ms(QEMU_CLOCK_REALTIME);
        if (s->frame_stable_not_before_ms > now) {
            timer_mod(s->refresh_timer, s->frame_stable_not_before_ms);
        } else {
            host_schedule_vblank(s);
        }
    } else {
        host_schedule_refresh(s);
    }
}

static void host_frame_source_changed(void *opaque)
{
    Bbk9588HostBridgeState *s = opaque;

    bbk9588_panel_set_frame_done(s->panel);
    s->frame_chardev_sent_valid = false;
    s->scanout_not_before_ms =
        qemu_clock_get_ms(QEMU_CLOCK_REALTIME) + s->refresh_period_ms;
    host_schedule_vblank(s);
}

static void host_audio_output(void *opaque, uint32_t sample_rate,
                              const int16_t *samples, size_t frames)
{
    Bbk9588HostBridgeState *s = opaque;
    uint32_t header[7];
    size_t payload_bytes = frames * 2u * sizeof(int16_t);

    if (frames == 0 || !qemu_chr_fe_backend_connected(&s->frame_chr)) {
        return;
    }
    s->audio_seq++;
    header[0] = cpu_to_le32(BBK9588_AUDIO_MAGIC);
    header[1] = cpu_to_le32(s->audio_seq);
    header[2] = cpu_to_le32(sample_rate);
    header[3] = cpu_to_le32(2u);
    header[4] = cpu_to_le32(2u * sizeof(int16_t));
    header[5] = cpu_to_le32(BBK9588_AUDIO_FORMAT_S16LE);
    header[6] = cpu_to_le32(payload_bytes);

    if (qemu_chr_fe_write_all(&s->frame_chr, (const uint8_t *)header,
                              sizeof(header)) < 0) {
        return;
    }
    qemu_chr_fe_write_all(&s->frame_chr, (const uint8_t *)samples,
                          payload_bytes);
}

void bbk9588_host_bridge_configure(
    Bbk9588HostBridgeState *s, const char *frame_chardev,
    uint32_t refresh_period_ms, Bbk9588GuestInsnCallback guest_insns,
    void *guest_insns_opaque)
{
    Chardev *chr = frame_chardev ? qemu_chr_find(frame_chardev) : NULL;

    s->refresh_period_ms = refresh_period_ms;
    s->guest_insns = guest_insns;
    s->guest_insns_opaque = guest_insns_opaque;
    s->frame_chardev_configured = frame_chardev && frame_chardev[0];
    if (chr) {
        qemu_chr_fe_init(&s->frame_chr, chr, &error_abort);
        s->frame_chr_initialized = true;
    }
    s->refresh_timer = timer_new_ms(QEMU_CLOCK_REALTIME,
                                    host_refresh_timer_cb, s);
    s->console = graphic_console_init(NULL, 0, &host_lcd_ops, s);
    qemu_console_resize(s->console, BBK9588_LCD_WIDTH, BBK9588_LCD_HEIGHT);
}

void bbk9588_host_bridge_connect_display(Bbk9588HostBridgeState *s,
                                         JZ4740LCDState *lcd,
                                         Bbk9588PanelState *panel)
{
    s->lcd = lcd;
    s->panel = panel;
    jz4740_lcd_set_frame_source_callback(lcd, host_frame_source_changed, s);
}

void bbk9588_host_bridge_connect_audio(Bbk9588HostBridgeState *s,
                                       JZ4740AICState *aic,
                                       JZ4740DMACState *dmac)
{
    s->aic = aic;
    s->dmac = dmac;
    jz4740_aic_set_output_callback(aic, host_audio_output, s);
}

void bbk9588_host_bridge_reset_metrics(Bbk9588HostBridgeState *s)
{
    s->perf_seq = 0;
    s->perf_last_send_ms = 0;
}

void bbk9588_host_bridge_start(Bbk9588HostBridgeState *s)
{
    host_schedule_refresh(s);
}

static void host_bridge_finalize(Object *obj)
{
    Bbk9588HostBridgeState *s = BBK9588_HOST_BRIDGE(obj);

    if (s->refresh_timer) {
        timer_free(s->refresh_timer);
    }
    if (s->frame_chr_initialized) {
        qemu_chr_fe_deinit(&s->frame_chr, false);
    }
}

static const TypeInfo host_bridge_type_info = {
    .name = TYPE_BBK9588_HOST_BRIDGE,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(Bbk9588HostBridgeState),
    .instance_finalize = host_bridge_finalize,
};

static void host_bridge_register_types(void)
{
    type_register_static(&host_bridge_type_info);
}

type_init(host_bridge_register_types)
