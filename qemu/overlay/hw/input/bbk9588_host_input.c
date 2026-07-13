/*
 * BBK 9588 host key/touch input bridge.
 *
 * This device parses the private host transport protocol and forwards typed
 * input events to board wiring callbacks.  It has no guest-visible registers.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "system/system.h"
#include "chardev/char.h"
#include "chardev/char-fe.h"
#include "hw/input/bbk9588_host_input.h"
#include "qapi/error.h"
#include "qemu/module.h"

struct Bbk9588HostInputState {
    DeviceState parent_obj;

    CharFrontend chr;
    Bbk9588HostKeyCallback key_callback;
    Bbk9588HostTouchCallback touch_callback;
    void *callback_opaque;
    char line[128];
    size_t line_len;
    bool chr_initialized;
};

static uint16_t host_input_u16(unsigned value)
{
    return value > UINT16_MAX ? UINT16_MAX : value;
}

static void host_input_handle_line(Bbk9588HostInputState *s,
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
        if (s->touch_callback) {
            s->touch_callback(s->callback_opaque,
                              host_input_u16(raw_x), host_input_u16(raw_y),
                              host_input_u16(x), host_input_u16(y),
                              down != 0);
        }
        return;
    }

    if (sscanf(line, "K %u %u", &key_code, &down) == 2 &&
        s->key_callback) {
        s->key_callback(s->callback_opaque, key_code & 0xffu, down != 0);
    }
}

static int host_input_can_read(void *opaque)
{
    Bbk9588HostInputState *s = opaque;

    return (int)(sizeof(s->line) - s->line_len - 1);
}

static void host_input_read(void *opaque, const uint8_t *buf, int size)
{
    Bbk9588HostInputState *s = opaque;

    for (int i = 0; i < size; i++) {
        char ch = (char)buf[i];

        if (ch == '\r') {
            continue;
        }
        if (ch == '\n') {
            s->line[s->line_len] = 0;
            if (s->line_len > 0) {
                host_input_handle_line(s, s->line);
            }
            s->line_len = 0;
            continue;
        }
        if (s->line_len + 1 < sizeof(s->line)) {
            s->line[s->line_len++] = ch;
        } else {
            s->line_len = 0;
        }
    }
}

void bbk9588_host_input_configure(Bbk9588HostInputState *s,
                                  const char *input_chardev,
                                  Bbk9588HostKeyCallback key_callback,
                                  Bbk9588HostTouchCallback touch_callback,
                                  void *opaque)
{
    Chardev *chr = input_chardev ? qemu_chr_find(input_chardev) : serial_hd(1);

    s->key_callback = key_callback;
    s->touch_callback = touch_callback;
    s->callback_opaque = opaque;
    if (chr) {
        qemu_chr_fe_init(&s->chr, chr, &error_abort);
        qemu_chr_fe_set_handlers(&s->chr, host_input_can_read,
                                 host_input_read, NULL, NULL, s, NULL, true);
        s->chr_initialized = true;
    }
}

static void host_input_finalize(Object *obj)
{
    Bbk9588HostInputState *s = BBK9588_HOST_INPUT(obj);

    if (s->chr_initialized) {
        qemu_chr_fe_deinit(&s->chr, false);
    }
}

static const TypeInfo host_input_type_info = {
    .name = TYPE_BBK9588_HOST_INPUT,
    .parent = TYPE_DEVICE,
    .instance_size = sizeof(Bbk9588HostInputState),
    .instance_finalize = host_input_finalize,
};

static void host_input_register_types(void)
{
    type_register_static(&host_input_type_info);
}

type_init(host_input_register_types)
