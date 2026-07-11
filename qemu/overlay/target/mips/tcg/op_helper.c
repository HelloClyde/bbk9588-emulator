/*
 *  MIPS emulation helpers for qemu.
 *
 *  Copyright (c) 2004-2005 Jocelyn Mayer
 *
 * This library is free software; you can redistribute it and/or
 * modify it under the terms of the GNU Lesser General Public
 * License as published by the Free Software Foundation; either
 * version 2.1 of the License, or (at your option) any later version.
 *
 * This library is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
 * Lesser General Public License for more details.
 *
 * You should have received a copy of the GNU Lesser General Public
 * License along with this library; if not, see <http://www.gnu.org/licenses/>.
 *
 */

#include "qemu/osdep.h"
#include "cpu.h"
#include "internal.h"
#include "exec/helper-proto.h"
#include "exec/cpu-common.h"
#include "exec/memop.h"
#include "fpu_helper.h"
#include "qemu/crc32c.h"
#include <zlib.h>

static inline target_ulong bitswap(target_ulong v)
{
    v = ((v >> 1) & (target_ulong)0x5555555555555555ULL) |
              ((v & (target_ulong)0x5555555555555555ULL) << 1);
    v = ((v >> 2) & (target_ulong)0x3333333333333333ULL) |
              ((v & (target_ulong)0x3333333333333333ULL) << 2);
    v = ((v >> 4) & (target_ulong)0x0F0F0F0F0F0F0F0FULL) |
              ((v & (target_ulong)0x0F0F0F0F0F0F0F0FULL) << 4);
    return v;
}

#ifdef TARGET_MIPS64
target_ulong helper_dbitswap(target_ulong rt)
{
    return bitswap(rt);
}
#endif

target_ulong helper_bitswap(target_ulong rt)
{
    return (int32_t)bitswap(rt);
}

#ifndef CONFIG_USER_ONLY
#define BBK9588_FS_PROBE_VA 0x89f20000u
#define BBK9588_FS_PROBE_MAGIC 0x46534b42u
#define BBK9588_FS_PROBE_SLOTS 96u
#define BBK9588_FS_PROBE_WORDS 96u
#define BBK9588_FS_PROBE_HEADER_WORDS 4u
#define BBK9588_KSEG_TO_PHYS(addr) ((addr) & 0x1fffffffu)

static bool bbk9588_probe_va_valid(uint32_t va, uint32_t size)
{
    uint32_t phys = BBK9588_KSEG_TO_PHYS(va);

    return (va & 0xe0000000u) == 0x80000000u &&
           size <= (160u * 1024u * 1024u) &&
           phys <= (160u * 1024u * 1024u) - size;
}

static uint32_t bbk9588_probe_read_u32(uint32_t va)
{
    uint32_t value = 0;

    if (bbk9588_probe_va_valid(va, sizeof(value))) {
        cpu_physical_memory_read(BBK9588_KSEG_TO_PHYS(va), &value,
                                 sizeof(value));
    }
    return value;
}

static uint16_t bbk9588_probe_read_u16(uint32_t va)
{
    uint16_t value = 0;

    if (bbk9588_probe_va_valid(va, sizeof(value))) {
        cpu_physical_memory_read(BBK9588_KSEG_TO_PHYS(va), &value,
                                 sizeof(value));
    }
    return value;
}

static uint8_t bbk9588_probe_read_u8(uint32_t va)
{
    uint8_t value = 0;

    if (bbk9588_probe_va_valid(va, sizeof(value))) {
        cpu_physical_memory_read(BBK9588_KSEG_TO_PHYS(va), &value,
                                 sizeof(value));
    }
    return value;
}

static void bbk9588_probe_write_u32(uint32_t va, uint32_t value)
{
    if (bbk9588_probe_va_valid(va, sizeof(value))) {
        cpu_physical_memory_write(BBK9588_KSEG_TO_PHYS(va), &value,
                                  sizeof(value));
    }
}

void helper_bbk9588_fs_probe(CPUMIPSState *env, target_ulong pc)
{
    uint32_t total_size = (BBK9588_FS_PROBE_HEADER_WORDS +
                           BBK9588_FS_PROBE_SLOTS *
                           BBK9588_FS_PROBE_WORDS) * 4;
    uint32_t seq;
    uint32_t slot;
    uint32_t entry;
    uint32_t a0;
    uint32_t a1;
    uint32_t a2;
    uint32_t a3;
    uint32_t obj;
    uint32_t dirent;
    uint32_t cache_desc = 0;
    uint32_t cache_flags = 0;
    uint32_t cache_data = 0;
    uint32_t cache_sector = 0;
    uint32_t cache_page_index = 0;
    uint32_t cache_sector_in_page = 0;
    uint32_t cache_flag_va = 0;
    uint32_t cache_data_va = 0;
    uint32_t i;
    uint32_t pc32;

    if (!env || !env->bbk9588_storage_trace ||
        !bbk9588_probe_va_valid(BBK9588_FS_PROBE_VA, total_size)) {
        return;
    }

    seq = bbk9588_probe_read_u32(BBK9588_FS_PROBE_VA + 4) + 1;
    slot = (seq - 1) % BBK9588_FS_PROBE_SLOTS;
    entry = BBK9588_FS_PROBE_VA +
            (BBK9588_FS_PROBE_HEADER_WORDS +
             slot * BBK9588_FS_PROBE_WORDS) * 4;

    a0 = env->active_tc.gpr[4] & 0xffffffffu;
    a1 = env->active_tc.gpr[5] & 0xffffffffu;
    a2 = env->active_tc.gpr[6] & 0xffffffffu;
    a3 = env->active_tc.gpr[7] & 0xffffffffu;
    pc32 = pc & 0xffffffffu;
    obj = a0;
    if ((uint32_t)pc == 0x8017a978u || (uint32_t)pc == 0x8017aa3cu ||
        (uint32_t)pc == 0x8017ab2cu || (uint32_t)pc == 0x8017a9d0u) {
        obj = a3;
    } else if ((uint32_t)pc == 0x801708c4u ||
               (uint32_t)pc == 0x80170980u) {
        obj = env->active_tc.gpr[16] & 0xffffffffu;
    } else if ((uint32_t)pc == 0x80173e84u ||
               (uint32_t)pc == 0x80173ea0u) {
        obj = env->active_tc.gpr[30] & 0xffffffffu;
    } else if ((uint32_t)pc == 0x800e1810u ||
               (uint32_t)pc == 0x800e1830u ||
               (uint32_t)pc == 0x800e1874u ||
               (uint32_t)pc == 0x800e1878u) {
        obj = env->active_tc.gpr[16] & 0xffffffffu;
    }
    dirent = a2;
    if ((uint32_t)pc == 0x80173e84u ||
        (uint32_t)pc == 0x80173ea0u) {
        dirent = env->active_tc.gpr[17] & 0xffffffffu;
    } else if ((uint32_t)pc == 0x800e1830u ||
               (uint32_t)pc == 0x800e1874u) {
        dirent = (env->active_tc.gpr[29] & 0xffffffffu) + 0x10u;
    } else if ((uint32_t)pc == 0x800e16f0u ||
               (uint32_t)pc == 0x800e1808u ||
               (uint32_t)pc == 0x800e194cu ||
               (uint32_t)pc == 0x800e1bf0u ||
               (uint32_t)pc == 0x800e1cd8u ||
               (uint32_t)pc == 0x800e1d00u ||
               (uint32_t)pc == 0x800e1d58u ||
               (uint32_t)pc == 0x800e1db0u) {
        dirent = a0;
    } else if ((uint32_t)pc == 0x80173504u ||
               (uint32_t)pc == 0x801708bcu) {
        dirent = a1;
    } else if ((uint32_t)pc == 0x801708c4u ||
               (uint32_t)pc == 0x80170980u) {
        dirent = obj + 4;
    }

    if (pc32 == 0x80182e10u) {
        cache_desc = env->active_tc.gpr[2] & 0xffffffffu; /* v0 */
        cache_sector = env->active_tc.gpr[23] & 0xffffffffu; /* s7 */
    } else if (pc32 == 0x80182d6cu || pc32 == 0x80182dc0u ||
        pc32 == 0x80182e64u || pc32 == 0x80182e78u ||
        pc32 == 0x80182f74u || pc32 == 0x80183068u ||
        pc32 == 0x80182fb8u) {
        cache_desc = env->active_tc.gpr[20] & 0xffffffffu; /* s4 */
        cache_sector = env->active_tc.gpr[17] & 0xffffffffu; /* s1 page index */
    }
    if (cache_desc != 0) {
        cache_flags = bbk9588_probe_read_u32(cache_desc + 0x0c);
        cache_data = bbk9588_probe_read_u32(cache_desc + 0x10);
        cache_page_index = cache_sector;
        cache_sector_in_page = env->active_tc.gpr[22] & 3u; /* original s6 */
        cache_flag_va = cache_flags + cache_page_index;
        cache_data_va = cache_data + cache_page_index * 0x840u +
                        cache_sector_in_page * 0x200u;
        dirent = cache_data_va;
    }

    bbk9588_probe_write_u32(BBK9588_FS_PROBE_VA + 0x00,
                            BBK9588_FS_PROBE_MAGIC);
    bbk9588_probe_write_u32(BBK9588_FS_PROBE_VA + 0x04, seq);
    bbk9588_probe_write_u32(BBK9588_FS_PROBE_VA + 0x08, slot);
    bbk9588_probe_write_u32(BBK9588_FS_PROBE_VA + 0x0c,
                            BBK9588_FS_PROBE_SLOTS);

    bbk9588_probe_write_u32(entry + 0x00, seq);
    bbk9588_probe_write_u32(entry + 0x04, pc & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x08, env->active_tc.gpr[2] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x0c, a0);
    bbk9588_probe_write_u32(entry + 0x10, a1);
    bbk9588_probe_write_u32(entry + 0x14, a2);
    bbk9588_probe_write_u32(entry + 0x18, a3);
    bbk9588_probe_write_u32(entry + 0x1c, env->active_tc.gpr[29] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x20, env->active_tc.gpr[31] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x24, obj);
    bbk9588_probe_write_u32(entry + 0x28, bbk9588_probe_read_u32(obj + 0x18));
    bbk9588_probe_write_u32(entry + 0x2c, bbk9588_probe_read_u32(obj + 0x20));
    bbk9588_probe_write_u32(entry + 0x30, bbk9588_probe_read_u32(obj + 0x30));
    bbk9588_probe_write_u32(entry + 0x34, bbk9588_probe_read_u32(obj + 0x34));
    bbk9588_probe_write_u32(entry + 0x38, bbk9588_probe_read_u16(obj + 0x48));
    bbk9588_probe_write_u32(entry + 0x3c, bbk9588_probe_read_u16(obj + 0x4a));
    bbk9588_probe_write_u32(entry + 0x40, bbk9588_probe_read_u32(obj + 0x24));
    bbk9588_probe_write_u32(entry + 0x44, bbk9588_probe_read_u32(obj + 0x38));
    bbk9588_probe_write_u32(entry + 0x48, bbk9588_probe_read_u32(obj + 0x44));
    bbk9588_probe_write_u32(entry + 0x4c, bbk9588_probe_read_u32(obj + 0x50));
    bbk9588_probe_write_u32(entry + 0x50, env->active_tc.gpr[16] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x54, env->active_tc.gpr[17] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x58, env->active_tc.gpr[18] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x5c, env->active_tc.gpr[19] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x60, env->active_tc.gpr[20] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x64, env->active_tc.gpr[21] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x68, env->active_tc.gpr[22] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x6c, env->active_tc.gpr[23] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x70, env->active_tc.gpr[30] & 0xffffffffu);
    bbk9588_probe_write_u32(entry + 0x74, dirent);
    for (i = 0; i < 8; i++) {
        bbk9588_probe_write_u32(entry + 0x78 + i * 4,
                                bbk9588_probe_read_u32(dirent + i * 4));
    }
    bbk9588_probe_write_u32(entry + 0x98, cache_desc);
    bbk9588_probe_write_u32(entry + 0x9c, bbk9588_probe_read_u32(cache_desc));
    bbk9588_probe_write_u32(entry + 0xa0, bbk9588_probe_read_u32(cache_desc + 4));
    bbk9588_probe_write_u32(entry + 0xa4, bbk9588_probe_read_u32(cache_desc + 8));
    bbk9588_probe_write_u32(entry + 0xa8, cache_flags);
    bbk9588_probe_write_u32(entry + 0xac, cache_data);
    bbk9588_probe_write_u32(entry + 0xb0, cache_sector);
    bbk9588_probe_write_u32(entry + 0xb4, cache_page_index);
    bbk9588_probe_write_u32(entry + 0xb8, cache_sector_in_page);
    bbk9588_probe_write_u32(entry + 0xbc, cache_flag_va);
    bbk9588_probe_write_u32(entry + 0xc0, bbk9588_probe_read_u8(cache_flag_va));
    bbk9588_probe_write_u32(entry + 0xc4, cache_data_va);
    for (i = 0; i < 4; i++) {
        bbk9588_probe_write_u32(entry + 0xc8 + i * 4,
                                bbk9588_probe_read_u32(cache_data_va + i * 4));
    }
    for (i = 0; i < 6; i++) {
        uint32_t slot_va = 0x804bf4c0u + i * 0x14u;
        bbk9588_probe_write_u32(entry + 0xd8 + i * 16u,
                                bbk9588_probe_read_u32(slot_va));
        bbk9588_probe_write_u32(entry + 0xdc + i * 16u,
                                bbk9588_probe_read_u32(slot_va + 4));
        bbk9588_probe_write_u32(entry + 0xe0 + i * 16u,
                                bbk9588_probe_read_u32(slot_va + 8));
        bbk9588_probe_write_u32(entry + 0xe4 + i * 16u,
                                bbk9588_probe_read_u32(slot_va + 0x10));
    }
    {
        uint32_t free_map = bbk9588_probe_read_u32(0x804bf46cu);
        bbk9588_probe_write_u32(entry + 0x138, free_map);
        bbk9588_probe_write_u32(entry + 0x13c, bbk9588_probe_read_u32(0x804bf470u));
        bbk9588_probe_write_u32(entry + 0x140, bbk9588_probe_read_u32(0x804bf474u));
        bbk9588_probe_write_u32(entry + 0x144, bbk9588_probe_read_u32(0x804bf478u));
        bbk9588_probe_write_u32(entry + 0x148, bbk9588_probe_read_u32(0x804bf47cu));
        bbk9588_probe_write_u32(entry + 0x14c, bbk9588_probe_read_u32(0x804bf480u));
        bbk9588_probe_write_u32(entry + 0x150, bbk9588_probe_read_u32(0x804bf48cu));
        bbk9588_probe_write_u32(entry + 0x154, bbk9588_probe_read_u32(free_map + 0x000u));
        bbk9588_probe_write_u32(entry + 0x158, bbk9588_probe_read_u32(free_map + 0x3ccu));
        bbk9588_probe_write_u32(entry + 0x15c, bbk9588_probe_read_u32(free_map + 0xa2bu));
        bbk9588_probe_write_u32(entry + 0x160, bbk9588_probe_read_u32(free_map + 0xa30u));
        bbk9588_probe_write_u32(entry + 0x164, bbk9588_probe_read_u32(free_map + 0xb20u));
        bbk9588_probe_write_u32(entry + 0x168, bbk9588_probe_read_u32(0x804bf4b8u));
        bbk9588_probe_write_u32(entry + 0x16c, bbk9588_probe_read_u32(0x804bf4acu));
        bbk9588_probe_write_u32(entry + 0x170, bbk9588_probe_read_u32(0x804bf4b0u));
        bbk9588_probe_write_u32(entry + 0x174, bbk9588_probe_read_u32(0x804bf4bcu));
    }
}
#endif

target_ulong helper_rotx(target_ulong rs, uint32_t shift, uint32_t shiftx,
                        uint32_t stripe)
{
    int i;
    uint64_t tmp0 = ((uint64_t)rs) << 32 | ((uint64_t)rs & 0xffffffff);
    uint64_t tmp1 = tmp0;
    for (i = 0; i <= 46; i++) {
        int s;
        if (i & 0x8) {
            s = shift;
        } else {
            s = shiftx;
        }

        if (stripe != 0 && !(i & 0x4)) {
            s = ~s;
        }
        if (s & 0x10) {
            if (tmp0 & (1LL << (i + 16))) {
                tmp1 |= 1LL << i;
            } else {
                tmp1 &= ~(1LL << i);
            }
        }
    }

    uint64_t tmp2 = tmp1;
    for (i = 0; i <= 38; i++) {
        int s;
        if (i & 0x4) {
            s = shift;
        } else {
            s = shiftx;
        }

        if (s & 0x8) {
            if (tmp1 & (1LL << (i + 8))) {
                tmp2 |= 1LL << i;
            } else {
                tmp2 &= ~(1LL << i);
            }
        }
    }

    uint64_t tmp3 = tmp2;
    for (i = 0; i <= 34; i++) {
        int s;
        if (i & 0x2) {
            s = shift;
        } else {
            s = shiftx;
        }
        if (s & 0x4) {
            if (tmp2 & (1LL << (i + 4))) {
                tmp3 |= 1LL << i;
            } else {
                tmp3 &= ~(1LL << i);
            }
        }
    }

    uint64_t tmp4 = tmp3;
    for (i = 0; i <= 32; i++) {
        int s;
        if (i & 0x1) {
            s = shift;
        } else {
            s = shiftx;
        }
        if (s & 0x2) {
            if (tmp3 & (1LL << (i + 2))) {
                tmp4 |= 1LL << i;
            } else {
                tmp4 &= ~(1LL << i);
            }
        }
    }

    uint64_t tmp5 = tmp4;
    for (i = 0; i <= 31; i++) {
        int s;
        s = shift;
        if (s & 0x1) {
            if (tmp4 & (1LL << (i + 1))) {
                tmp5 |= 1LL << i;
            } else {
                tmp5 &= ~(1LL << i);
            }
        }
    }

    return (int64_t)(int32_t)(uint32_t)tmp5;
}

/* these crc32 functions are based on target/loongarch/tcg/op_helper.c */
target_ulong helper_crc32(target_ulong val, target_ulong m, uint32_t sz)
{
    uint8_t buf[8];
    target_ulong mask = ((sz * 8) == 64) ?
                        (target_ulong) -1ULL :
                        ((1ULL << (sz * 8)) - 1);

    m &= mask;
    stq_le_p(buf, m);
    return (int32_t) (crc32(val ^ 0xffffffff, buf, sz) ^ 0xffffffff);
}

target_ulong helper_crc32c(target_ulong val, target_ulong m, uint32_t sz)
{
    uint8_t buf[8];
    target_ulong mask = ((sz * 8) == 64) ?
                        (target_ulong) -1ULL :
                        ((1ULL << (sz * 8)) - 1);
    m &= mask;
    stq_le_p(buf, m);
    return (int32_t) (crc32c(val, buf, sz) ^ 0xffffffff);
}

void helper_fork(target_ulong arg1, target_ulong arg2)
{
    /*
     * arg1 = rt, arg2 = rs
     * TODO: store to TC register
     */
}

target_ulong helper_yield(CPUMIPSState *env, target_ulong arg)
{
    target_long arg1 = arg;

    if (arg1 < 0) {
        /* No scheduling policy implemented. */
        if (arg1 != -2) {
            if (env->CP0_VPEControl & (1 << CP0VPECo_YSI) &&
                env->active_tc.CP0_TCStatus & (1 << CP0TCSt_DT)) {
                env->CP0_VPEControl &= ~(0x7 << CP0VPECo_EXCPT);
                env->CP0_VPEControl |= 4 << CP0VPECo_EXCPT;
                do_raise_exception(env, EXCP_THREAD, GETPC());
            }
        }
    } else if (arg1 == 0) {
        if (0) {
            /* TODO: TC underflow */
            env->CP0_VPEControl &= ~(0x7 << CP0VPECo_EXCPT);
            do_raise_exception(env, EXCP_THREAD, GETPC());
        } else {
            /* TODO: Deallocate TC */
        }
    } else if (arg1 > 0) {
        /* Yield qualifier inputs not implemented. */
        env->CP0_VPEControl &= ~(0x7 << CP0VPECo_EXCPT);
        env->CP0_VPEControl |= 2 << CP0VPECo_EXCPT;
        do_raise_exception(env, EXCP_THREAD, GETPC());
    }
    return env->CP0_YQMask;
}

static inline void check_hwrena(CPUMIPSState *env, int reg, uintptr_t pc)
{
    if ((env->hflags & MIPS_HFLAG_CP0) || (env->CP0_HWREna & (1 << reg))) {
        return;
    }
    do_raise_exception(env, EXCP_RI, pc);
}

target_ulong helper_rdhwr_cpunum(CPUMIPSState *env)
{
    check_hwrena(env, 0, GETPC());
    return env->CP0_EBase & 0x3ff;
}

target_ulong helper_rdhwr_synci_step(CPUMIPSState *env)
{
    check_hwrena(env, 1, GETPC());
    return env->SYNCI_Step;
}

target_ulong helper_rdhwr_cc(CPUMIPSState *env)
{
    check_hwrena(env, 2, GETPC());
#ifdef CONFIG_USER_ONLY
    return env->CP0_Count;
#else
    return (int32_t)cpu_mips_get_count(env);
#endif
}

target_ulong helper_rdhwr_ccres(CPUMIPSState *env)
{
    check_hwrena(env, 3, GETPC());
    return env->CCRes;
}

target_ulong helper_rdhwr_performance(CPUMIPSState *env)
{
    check_hwrena(env, 4, GETPC());
    return env->CP0_Performance0;
}

target_ulong helper_rdhwr_xnp(CPUMIPSState *env)
{
    check_hwrena(env, 5, GETPC());
    return (env->CP0_Config5 >> CP0C5_XNP) & 1;
}

void helper_pmon(CPUMIPSState *env, int function)
{
    function /= 2;
    switch (function) {
    case 2: /* TODO: char inbyte(int waitflag); */
        if (env->active_tc.gpr[4] == 0) {
            env->active_tc.gpr[2] = -1;
        }
        /* Fall through */
    case 11: /* TODO: char inbyte (void); */
        env->active_tc.gpr[2] = -1;
        break;
    case 3:
    case 12:
        printf("%c", (char)(env->active_tc.gpr[4] & 0xFF));
        break;
    case 17:
        break;
    case 158:
        {
            unsigned char *fmt = (void *)(uintptr_t)env->active_tc.gpr[4];
            printf("%s", fmt);
        }
        break;
    }
}

#ifdef TARGET_MIPS64
target_ulong helper_lcsr_cpucfg(CPUMIPSState *env, target_ulong rs)
{
    switch (rs) {
    case 0:
        return env->CP0_PRid;
    case 1:
        return env->lcsr_cpucfg1;
    case 2:
        return env->lcsr_cpucfg2;
    default:
        return 0;
    }
}
#endif

#if !defined(CONFIG_USER_ONLY)

void mips_cpu_do_unaligned_access(CPUState *cs, vaddr addr,
                                  MMUAccessType access_type,
                                  int mmu_idx, uintptr_t retaddr)
{
    CPUMIPSState *env = cpu_env(cs);
    int error_code = 0;
    int excp;

    if (!(env->hflags & MIPS_HFLAG_DM)) {
        env->CP0_BadVAddr = addr;
    }

    if (access_type == MMU_DATA_STORE) {
        excp = EXCP_AdES;
    } else {
        excp = EXCP_AdEL;
        if (access_type == MMU_INST_FETCH) {
            error_code |= EXCP_INST_NOTAVAIL;
        }
    }

    do_raise_exception_err(env, excp, error_code, retaddr);
}

void mips_cpu_do_transaction_failed(CPUState *cs, hwaddr physaddr,
                                    vaddr addr, unsigned size,
                                    MMUAccessType access_type,
                                    int mmu_idx, MemTxAttrs attrs,
                                    MemTxResult response, uintptr_t retaddr)
{
    MIPSCPUClass *mcc = MIPS_CPU_GET_CLASS(cs);
    CPUMIPSState *env = cpu_env(cs);

    if (access_type == MMU_INST_FETCH) {
        do_raise_exception(env, EXCP_IBE, retaddr);
    } else if (!mcc->no_data_aborts) {
        do_raise_exception(env, EXCP_DBE, retaddr);
    }
}
#endif /* !CONFIG_USER_ONLY */
