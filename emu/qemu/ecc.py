"""JZ4740 NAND Reed-Solomon parity generation."""

from __future__ import annotations

from functools import lru_cache

ECC_BLOCK_BYTES = 512
RS_PARITY_BYTES = 9
RS_SYMBOL_BITS = 9
RS_NN = 511
RS_NROOTS = 8
RS_DATA_SYMBOLS = 503
RS_GF_POLY = 0x211
RS_FCR = 1


def _modnn(value: int) -> int:
    while value >= RS_NN:
        value -= RS_NN
    return value


@lru_cache(maxsize=1)
def _codec_tables() -> tuple[tuple[int, ...], tuple[int, ...], tuple[int, ...]]:
    alpha_to = [0] * (RS_NN + 1)
    index_of = [0] * (RS_NN + 1)
    sr = 1
    index_of[0] = RS_NN
    for index in range(RS_NN):
        index_of[sr] = index
        alpha_to[index] = sr
        sr <<= 1
        if sr & (1 << RS_SYMBOL_BITS):
            sr ^= RS_GF_POLY
        sr &= RS_NN

    genpoly = [0] * (RS_NROOTS + 1)
    genpoly[0] = 1
    root = RS_FCR
    for degree in range(RS_NROOTS):
        genpoly[degree + 1] = 1
        for index in range(degree, 0, -1):
            if genpoly[index]:
                genpoly[index] = genpoly[index - 1] ^ alpha_to[
                    _modnn(index_of[genpoly[index]] + root)
                ]
            else:
                genpoly[index] = genpoly[index - 1]
        genpoly[0] = alpha_to[_modnn(index_of[genpoly[0]] + root)]
        root += 1
    genpoly = [index_of[value] for value in genpoly]
    return tuple(alpha_to), tuple(index_of), tuple(genpoly)


@lru_cache(maxsize=1)
def _encoding_terms() -> tuple[tuple[int, ...], tuple[int, ...]]:
    alpha_to, index_of, genpoly = _codec_tables()
    terms: list[tuple[int, ...]] = []
    for feedback in range(RS_NN + 1):
        if feedback == RS_NN:
            terms.append((0,) * RS_NROOTS)
            continue
        terms.append(
            tuple(
                alpha_to[_modnn(feedback + genpoly[RS_NROOTS - index])]
                for index in range(1, RS_NROOTS)
            )
            + (alpha_to[_modnn(feedback + genpoly[0])],)
        )
    return index_of, tuple(terms)


def _pack_parity(parity: list[int]) -> bytes:
    par = [parity[RS_NROOTS - 1 - index] & RS_NN for index in range(RS_NROOTS)]
    par0 = par[7] | (par[6] << 9) | (par[5] << 18) | ((par[4] & 0x1F) << 27)
    par1 = (
        ((par[4] >> 5) & 0x0F)
        | (par[3] << 4)
        | (par[2] << 13)
        | (par[1] << 22)
        | ((par[0] & 1) << 31)
    )
    return par0.to_bytes(4, "little") + par1.to_bytes(4, "little") + bytes([par[0] >> 1])


def jz4740_rs_encode(data: bytes) -> bytes:
    """Return the nine JZ4740 RS parity bytes for one 512-byte block."""

    if len(data) != ECC_BLOCK_BYTES:
        raise ValueError(f"JZ4740 RS input must be {ECC_BLOCK_BYTES} bytes")
    index_of, terms = _encoding_terms()
    p0 = p1 = p2 = p3 = p4 = p5 = p6 = p7 = 0
    packed_data = int.from_bytes(data, "little")
    for symbol_index in range(RS_DATA_SYMBOLS):
        if symbol_index < 456:
            symbol = packed_data & RS_NN
            packed_data >>= RS_SYMBOL_BITS
        else:
            symbol = 0
        term = terms[index_of[symbol ^ p0]]
        p0, p1, p2, p3, p4, p5, p6, p7 = (
            p1 ^ term[0],
            p2 ^ term[1],
            p3 ^ term[2],
            p4 ^ term[3],
            p5 ^ term[4],
            p6 ^ term[5],
            p7 ^ term[6],
            term[7],
        )
    return _pack_parity([p0, p1, p2, p3, p4, p5, p6, p7])


def jz4740_page_oob_ecc(page_data: bytes, *, offset: int = 6) -> bytes:
    """Build an OOB prefix containing parity for every 512-byte page chunk."""

    if len(page_data) == 0 or len(page_data) % ECC_BLOCK_BYTES:
        raise ValueError("NAND page size must be a non-zero multiple of 512 bytes")
    parity = bytearray(b"\xff" * offset)
    for start in range(0, len(page_data), ECC_BLOCK_BYTES):
        parity.extend(jz4740_rs_encode(page_data[start : start + ECC_BLOCK_BYTES]))
    return bytes(parity)
