/*
 * Ingenic JZ4740 NAND ECC helpers.
 *
 * SPDX-License-Identifier: GPL-2.0-or-later
 */

#ifndef HW_MEM_JZ4740_ECC_H
#define HW_MEM_JZ4740_ECC_H

#define JZ4740_ECC_BLOCK_BYTES 512u
#define JZ4740_RS_PARITY_BYTES 9u
#define JZ4740_RS_MAX_ERRORS   4u

typedef struct JZ4740RSCorrection {
    uint16_t index;
    uint16_t mask;
} JZ4740RSCorrection;

uint32_t jz4740_hamming_encode(const uint8_t *data, size_t length);
void jz4740_rs_encode(const uint8_t data[JZ4740_ECC_BLOCK_BYTES],
                      uint8_t parity[JZ4740_RS_PARITY_BYTES]);
int jz4740_rs_decode(const uint8_t data[JZ4740_ECC_BLOCK_BYTES],
                     const uint8_t parity[JZ4740_RS_PARITY_BYTES],
                     JZ4740RSCorrection corrections[JZ4740_RS_MAX_ERRORS]);
void jz4740_rs_apply_correction(
    uint8_t data[JZ4740_ECC_BLOCK_BYTES],
    const JZ4740RSCorrection *correction);

#endif
