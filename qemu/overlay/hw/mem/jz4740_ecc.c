/*
 * Ingenic JZ4740 NAND Hamming and Reed-Solomon helpers.
 *
 * The RS decoder follows the fixed-parameter form of the Linux generic
 * Reed-Solomon implementation by Phil Karn and Thomas Gleixner.  JZ4740 uses
 * RS(511, 503) over GF(2^9), polynomial 0x211, first root 1, primitive step 1.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#include "qemu/osdep.h"
#include "hw/mem/jz4740_ecc.h"
#include "qemu/bswap.h"

#define JZ4740_RS_SYMBOL_BITS 9
#define JZ4740_RS_NN          511
#define JZ4740_RS_NROOTS      8
#define JZ4740_RS_DATA_SYMBOLS 503
#define JZ4740_RS_GF_POLY     0x211
#define JZ4740_RS_FCR         1
#define JZ4740_RS_PRIM        1
#define JZ4740_RS_IPRIM       1

typedef struct JZ4740RSCodec {
    uint16_t alpha_to[JZ4740_RS_NN + 1];
    uint16_t index_of[JZ4740_RS_NN + 1];
    uint16_t genpoly[JZ4740_RS_NROOTS + 1];
} JZ4740RSCodec;

static unsigned rs_modnn(unsigned value)
{
    while (value >= JZ4740_RS_NN) {
        value -= JZ4740_RS_NN;
    }
    return value;
}

static void rs_init(JZ4740RSCodec *rs)
{
    unsigned sr = 1;
    unsigned root = JZ4740_RS_FCR * JZ4740_RS_PRIM;

    memset(rs, 0, sizeof(*rs));
    rs->index_of[0] = JZ4740_RS_NN;
    rs->alpha_to[JZ4740_RS_NN] = 0;
    for (unsigned i = 0; i < JZ4740_RS_NN; i++) {
        rs->index_of[sr] = i;
        rs->alpha_to[i] = sr;
        sr <<= 1;
        if (sr & (1u << JZ4740_RS_SYMBOL_BITS)) {
            sr ^= JZ4740_RS_GF_POLY;
        }
        sr &= JZ4740_RS_NN;
    }

    rs->genpoly[0] = 1;
    for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++, root++) {
        rs->genpoly[i + 1] = 1;
        for (unsigned j = i; j > 0; j--) {
            if (rs->genpoly[j] != 0) {
                rs->genpoly[j] = rs->genpoly[j - 1] ^
                    rs->alpha_to[rs_modnn(
                        rs->index_of[rs->genpoly[j]] + root)];
            } else {
                rs->genpoly[j] = rs->genpoly[j - 1];
            }
        }
        rs->genpoly[0] = rs->alpha_to[rs_modnn(
            rs->index_of[rs->genpoly[0]] + root)];
    }
    for (unsigned i = 0; i <= JZ4740_RS_NROOTS; i++) {
        rs->genpoly[i] = rs->index_of[rs->genpoly[i]];
    }
}

static void rs_data_symbols(const uint8_t data[JZ4740_ECC_BLOCK_BYTES],
                            uint16_t symbols[JZ4740_RS_DATA_SYMBOLS])
{
    memset(symbols, 0, JZ4740_RS_DATA_SYMBOLS * sizeof(*symbols));
    for (unsigned i = 0; i < 456; i++) {
        unsigned bit = i * JZ4740_RS_SYMBOL_BITS;
        unsigned byte = bit >> 3;
        unsigned shift = bit & 7u;
        uint16_t value = data[byte] >> shift;

        if (byte + 1 < JZ4740_ECC_BLOCK_BYTES) {
            value |= (uint16_t)data[byte + 1] << (8 - shift);
        }
        symbols[i] = value & JZ4740_RS_NN;
    }
}

static void rs_pack_parity(const uint16_t parity[JZ4740_RS_NROOTS],
                           uint8_t packed[JZ4740_RS_PARITY_BYTES])
{
    uint16_t par[JZ4740_RS_NROOTS];
    uint32_t par0;
    uint32_t par1;

    for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++) {
        par[i] = parity[JZ4740_RS_NROOTS - 1 - i] & JZ4740_RS_NN;
    }
    par0 = par[7] | (par[6] << 9) | (par[5] << 18) |
           ((par[4] & 0x1fu) << 27);
    par1 = ((par[4] >> 5) & 0x0fu) | (par[3] << 4) | (par[2] << 13) |
           (par[1] << 22) | ((par[0] & 1u) << 31);
    stl_le_p(packed, par0);
    stl_le_p(packed + 4, par1);
    packed[8] = par[0] >> 1;
}

static void rs_unpack_parity(const uint8_t packed[JZ4740_RS_PARITY_BYTES],
                             uint16_t parity[JZ4740_RS_NROOTS])
{
    uint16_t par[JZ4740_RS_NROOTS];
    uint32_t par0 = ldl_le_p(packed);
    uint32_t par1 = ldl_le_p(packed + 4);

    par[7] = par0 & JZ4740_RS_NN;
    par[6] = (par0 >> 9) & JZ4740_RS_NN;
    par[5] = (par0 >> 18) & JZ4740_RS_NN;
    par[4] = ((par0 >> 27) & 0x1fu) | ((par1 & 0x0fu) << 5);
    par[3] = (par1 >> 4) & JZ4740_RS_NN;
    par[2] = (par1 >> 13) & JZ4740_RS_NN;
    par[1] = (par1 >> 22) & JZ4740_RS_NN;
    par[0] = ((par1 >> 31) & 1u) | ((uint16_t)packed[8] << 1);
    for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++) {
        parity[i] = par[JZ4740_RS_NROOTS - 1 - i];
    }
}

uint32_t jz4740_hamming_encode(const uint8_t *data, size_t length)
{
    static const unsigned parity_number[] = {
        1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048,
    };
    uint8_t parity[ARRAY_SIZE(parity_number)][2] = { 0 };
    uint32_t ecc = 0;

    length = MIN(length, (size_t)JZ4740_ECC_BLOCK_BYTES);
    for (size_t byte = 0; byte < length; byte++) {
        for (unsigned bit = 0; bit < 8; bit++) {
            if (!(data[byte] & (1u << bit))) {
                continue;
            }
            for (unsigned axis = 0; axis < 3; axis++) {
                parity[axis][(bit >> axis) & 1u] ^= 1;
            }
            for (unsigned axis = 0; axis < 9; axis++) {
                parity[axis + 3][(byte >> axis) & 1u] ^= 1;
            }
        }
    }

#define ECC_BIT(position, number, prime) \
    ((uint32_t)(parity[number][prime] ^ 1u) << (position))
    ecc |= ECC_BIT(7, 6, 1) | ECC_BIT(6, 6, 0) |
           ECC_BIT(5, 5, 1) | ECC_BIT(4, 5, 0) |
           ECC_BIT(3, 4, 1) | ECC_BIT(2, 4, 0) |
           ECC_BIT(1, 3, 1) | ECC_BIT(0, 3, 0);
    ecc |= (ECC_BIT(15, 10, 1) | ECC_BIT(14, 10, 0) |
            ECC_BIT(13, 9, 1) | ECC_BIT(12, 9, 0) |
            ECC_BIT(11, 8, 1) | ECC_BIT(10, 8, 0) |
            ECC_BIT(9, 7, 1) | ECC_BIT(8, 7, 0));
    ecc |= (ECC_BIT(23, 2, 1) | ECC_BIT(22, 2, 0) |
            ECC_BIT(21, 1, 1) | ECC_BIT(20, 1, 0) |
            ECC_BIT(19, 0, 1) | ECC_BIT(18, 0, 0) |
            ECC_BIT(17, 11, 1) | ECC_BIT(16, 11, 0));
#undef ECC_BIT
    return ecc;
}

void jz4740_rs_encode(const uint8_t data[JZ4740_ECC_BLOCK_BYTES],
                      uint8_t packed[JZ4740_RS_PARITY_BYTES])
{
    JZ4740RSCodec rs;
    uint16_t symbols[JZ4740_RS_DATA_SYMBOLS];
    uint16_t parity[JZ4740_RS_NROOTS] = { 0 };

    rs_init(&rs);
    rs_data_symbols(data, symbols);
    for (unsigned i = 0; i < JZ4740_RS_DATA_SYMBOLS; i++) {
        uint16_t feedback = rs.index_of[symbols[i] ^ parity[0]];

        if (feedback != JZ4740_RS_NN) {
            for (unsigned j = 1; j < JZ4740_RS_NROOTS; j++) {
                parity[j] ^= rs.alpha_to[rs_modnn(
                    feedback + rs.genpoly[JZ4740_RS_NROOTS - j])];
            }
        }
        memmove(parity, parity + 1,
                (JZ4740_RS_NROOTS - 1) * sizeof(*parity));
        parity[JZ4740_RS_NROOTS - 1] =
            feedback == JZ4740_RS_NN ? 0 :
            rs.alpha_to[rs_modnn(feedback + rs.genpoly[0])];
    }
    rs_pack_parity(parity, packed);
}

int jz4740_rs_decode(const uint8_t data[JZ4740_ECC_BLOCK_BYTES],
                     const uint8_t packed[JZ4740_RS_PARITY_BYTES],
                     JZ4740RSCorrection corrections[JZ4740_RS_MAX_ERRORS])
{
    JZ4740RSCodec rs;
    uint16_t data_symbols[JZ4740_RS_DATA_SYMBOLS];
    uint16_t parity[JZ4740_RS_NROOTS];
    uint16_t lambda[9] = { 0 }, syndrome[9] = { 0 }, b[9] = { 0 };
    uint16_t t[9] = { 0 }, omega[9] = { 0 }, root[9] = { 0 };
    uint16_t reg[9] = { 0 }, location[9] = { 0 }, correction[9] = { 0 };
    unsigned degree_lambda = 0;
    unsigned degree_omega;
    unsigned count = 0;
    unsigned el = 0;
    bool syndrome_error = false;

    rs_init(&rs);
    rs_data_symbols(data, data_symbols);
    rs_unpack_parity(packed, parity);
    for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++) {
        syndrome[i] = data_symbols[0];
    }
    for (unsigned j = 1; j < JZ4740_RS_DATA_SYMBOLS; j++) {
        for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++) {
            syndrome[i] = data_symbols[j] ^
                (syndrome[i] == 0 ? 0 : rs.alpha_to[rs_modnn(
                    rs.index_of[syndrome[i]] + JZ4740_RS_FCR + i)]);
        }
    }
    for (unsigned j = 0; j < JZ4740_RS_NROOTS; j++) {
        for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++) {
            syndrome[i] = parity[j] ^
                (syndrome[i] == 0 ? 0 : rs.alpha_to[rs_modnn(
                    rs.index_of[syndrome[i]] + JZ4740_RS_FCR + i)]);
        }
    }
    for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++) {
        syndrome_error |= syndrome[i] != 0;
        syndrome[i] = rs.index_of[syndrome[i]];
    }
    if (!syndrome_error) {
        return 0;
    }

    lambda[0] = 1;
    for (unsigned i = 0; i <= JZ4740_RS_NROOTS; i++) {
        b[i] = rs.index_of[lambda[i]];
    }
    for (unsigned step = 1; step <= JZ4740_RS_NROOTS; step++) {
        uint16_t discrepancy = 0;

        for (unsigned i = 0; i < step; i++) {
            if (lambda[i] != 0 &&
                syndrome[step - i - 1] != JZ4740_RS_NN) {
                discrepancy ^= rs.alpha_to[rs_modnn(
                    rs.index_of[lambda[i]] + syndrome[step - i - 1])];
            }
        }
        discrepancy = rs.index_of[discrepancy];
        if (discrepancy == JZ4740_RS_NN) {
            memmove(b + 1, b, JZ4740_RS_NROOTS * sizeof(*b));
            b[0] = JZ4740_RS_NN;
            continue;
        }
        t[0] = lambda[0];
        for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++) {
            t[i + 1] = lambda[i + 1] ^
                (b[i] == JZ4740_RS_NN ? 0 : rs.alpha_to[rs_modnn(
                    discrepancy + b[i])]);
        }
        if (2 * el <= step - 1) {
            el = step - el;
            for (unsigned i = 0; i <= JZ4740_RS_NROOTS; i++) {
                b[i] = lambda[i] == 0 ? JZ4740_RS_NN :
                    rs_modnn(rs.index_of[lambda[i]] - discrepancy +
                             JZ4740_RS_NN);
            }
        } else {
            memmove(b + 1, b, JZ4740_RS_NROOTS * sizeof(*b));
            b[0] = JZ4740_RS_NN;
        }
        memcpy(lambda, t, sizeof(lambda));
    }

    for (unsigned i = 0; i <= JZ4740_RS_NROOTS; i++) {
        lambda[i] = rs.index_of[lambda[i]];
        if (lambda[i] != JZ4740_RS_NN) {
            degree_lambda = i;
        }
    }
    if (degree_lambda == 0 || degree_lambda > JZ4740_RS_MAX_ERRORS) {
        return -1;
    }

    memcpy(reg + 1, lambda + 1, JZ4740_RS_NROOTS * sizeof(*reg));
    for (unsigned i = 1, k = JZ4740_RS_IPRIM - 1;
         i <= JZ4740_RS_NN; i++, k = rs_modnn(k + JZ4740_RS_IPRIM)) {
        uint16_t q = 1;

        for (unsigned j = degree_lambda; j > 0; j--) {
            if (reg[j] != JZ4740_RS_NN) {
                reg[j] = rs_modnn(reg[j] + j);
                q ^= rs.alpha_to[reg[j]];
            }
        }
        if (q == 0) {
            root[count] = i;
            location[count] = k;
            if (++count == degree_lambda) {
                break;
            }
        }
    }
    if (count != degree_lambda) {
        return -1;
    }

    degree_omega = degree_lambda - 1;
    for (unsigned i = 0; i <= degree_omega; i++) {
        uint16_t value = 0;

        for (unsigned j = 0; j <= i; j++) {
            if (syndrome[i - j] != JZ4740_RS_NN &&
                lambda[j] != JZ4740_RS_NN) {
                value ^= rs.alpha_to[rs_modnn(
                    syndrome[i - j] + lambda[j])];
            }
        }
        omega[i] = rs.index_of[value];
    }

    for (int j = count - 1; j >= 0; j--) {
        uint16_t numerator = 0;
        uint16_t denominator = 0;

        for (int i = degree_omega; i >= 0; i--) {
            if (omega[i] != JZ4740_RS_NN) {
                numerator ^= rs.alpha_to[rs_modnn(
                    omega[i] + i * root[j])];
            }
        }
        for (int i = MIN(degree_lambda, JZ4740_RS_NROOTS - 1) & ~1;
             i >= 0; i -= 2) {
            if (lambda[i + 1] != JZ4740_RS_NN) {
                denominator ^= rs.alpha_to[rs_modnn(
                    lambda[i + 1] + i * root[j])];
            }
        }
        if (numerator != 0 && denominator != 0) {
            correction[j] = rs.alpha_to[rs_modnn(
                rs.index_of[numerator] + JZ4740_RS_NN -
                rs.index_of[denominator])];
        }
    }

    for (unsigned i = 0; i < JZ4740_RS_NROOTS; i++) {
        uint16_t expected = 0;

        for (unsigned j = 0; j < count; j++) {
            if (correction[j] != 0) {
                unsigned exponent = (JZ4740_RS_FCR + i) *
                    (JZ4740_RS_NN - location[j] - 1);
                expected ^= rs.alpha_to[rs_modnn(
                    rs.index_of[correction[j]] + exponent)];
            }
        }
        if (expected != rs.alpha_to[syndrome[i]]) {
            return -1;
        }
    }

    memset(corrections, 0,
           JZ4740_RS_MAX_ERRORS * sizeof(*corrections));
    for (unsigned i = 0; i < count; i++) {
        corrections[i].index = location[i] + 1;
        corrections[i].mask = correction[i];
    }
    return count;
}

void jz4740_rs_apply_correction(
    uint8_t data[JZ4740_ECC_BLOCK_BYTES],
    const JZ4740RSCorrection *correction)
{
    unsigned symbol;
    unsigned byte;
    unsigned shift;
    uint16_t mask;

    if (!correction || correction->index == 0) {
        return;
    }
    symbol = correction->index - 1;
    byte = symbol + (symbol >> 3);
    if (byte >= JZ4740_ECC_BLOCK_BYTES) {
        return;
    }
    shift = symbol & 7u;
    mask = (correction->mask & JZ4740_RS_NN) << shift;
    data[byte] ^= mask & 0xffu;
    if (byte + 1 < JZ4740_ECC_BLOCK_BYTES) {
        data[byte + 1] ^= mask >> 8;
    }
}
