# U-Boot 启动与 NAND/FTL 反汇编分析

本文记录 `系统/数据/u_boot_9588_4740.bin` 的静态反汇编结论。U-Boot
装载地址按 `0x80900000` 分析。

## 入口流程

U-Boot 入口位于 `0x80900000`：

- 初始化 CP0/cache。
- 清零 BSS 区间，约为 `0x8095acd0..0x811f4168`。
- 设置 `gp` 与 `sp`。
- 跳转到主启动函数 `0x80901d20`。

主启动函数 `0x80901d20` 完成基础硬件初始化后，进入启动决策逻辑。
正常从 NAND/FAT 启动系统固件时，路径为：

```text
0x80901e64
  call 0x80900190        初始化介质与 FAT 参数
  if success:
    a0 = 0x80003fc0
    a1 = "kj409588.bin"
    call 0x809002c0      从 FAT 读取 kj409588.bin
    if success:
      jump 0x80004000
```

因此，当前这版 U-Boot 不是直接从 raw page 0 拉 C200，而是通过 FAT
文件系统读取 `kj409588.bin`，然后跳到 `0x80004000`。

## NAND ID 与几何

NAND 初始化函数位于 `0x809026c0`。它通过 NAND `READ ID` 命令 `0x90`
读取 5 个 ID 字节，并用第二个字节匹配容量表。

容量表大意如下：

```text
device code 0xf1 -> 128MB
device code 0xda -> 256MB
device code 0xdc -> 512MB
device code 0xd3 -> 1GB
device code 0xd5 -> 2GB
```

真机 FAT 可见容量约 `519,421,952` 字节，符合 512MB raw NAND 扣除
保留区和管理区后的容量，因此模拟器应暴露 `0xdc` 这类 512MB NAND。

对 512MB NAND，U-Boot 计算出的关键几何为：

```text
page_size       = 0x800    // 2048 bytes
oob_size        = 0x40     // 64 bytes
block_size      = 0x20000  // 128 KiB
pages_per_block = 0x40     // 64 pages
block_count     = 0x1000   // 4096 blocks
```

## FTL 初始化与 OOB 扫描

FTL 初始化入口为：

```text
0x80903aa0 -> 0x80903c64 -> 0x80903d1c
```

初始化会分配并清空几张 RAM 表：

```text
logical_to_physical: block_count * 2
block_state:         block_count
block_tag/status:    block_count
```

扫描函数 `0x80903d1c` 的核心逻辑如下：

```c
start_block = 0xb40000 / block_size;  // 512MB NAND 下为 0x5a

for (physical = start_block; physical < block_count; physical++) {
    if (is_bad_block(physical)) {
        state[physical] = 2;
        continue;
    }

    oob = read_oob(first_page_of_block);

    if (oob[1] != 0xff) {
        last_valid_page = oob[2];
        if (last_valid_page < pages_per_block) {
            last_oob = read_oob(first_page + last_valid_page);
            if (last_oob[-6..-1] != oob[-6..-1]) {
                mark_invalid_or_recover();
                continue;
            }
        }
    }

    tail = *(u32 *)(oob + oob_size - 4);

    if (tail == 0xffffffff) {
        state[physical] = free;
        continue;
    }

    if (tail == 0x38746262) { // "bbt8"
        update_bbt_candidate(physical);
        continue;
    }

    logical = tail & 0xffff;
    if (logical < block_count) {
        seq = *(u16 *)(oob + oob_size - 6);
        update_logical_to_physical(logical, physical, seq);
        continue;
    }

    mark_invalid();
}
```

结论：

- U-Boot 冷启动时会扫描 raw NAND block 的 OOB 元数据。
- 512MB NAND 下扫描范围约为 block `0x5a..0xfff`，共约 4006 个 block。
- 这不是扫描完整 512MB 数据区，而是主要读取每个 block 的 OOB。
- 每个 block 第一页 OOB 为 64 字节，总读取量约 256KB；即便部分 block
  额外读取 last-valid-page OOB，总量仍不大。
- 反汇编中没有看到遇到 `"bbt8"` 后直接退出整盘扫描的早退逻辑；
  `"bbt8"` 更像 BBT 候选标记，外层 block 循环仍会继续。

## 文件读取路径

`0x809002c0` 负责按文件名读取系统固件文件。它依赖 `0x80900190`
初始化出来的 FAT 参数和 FTL 映射表。

后续底层逻辑大致为：

```text
0x80904c00    读取逻辑扇区范围
  -> 0x80904ef4
       使用 logical_to_physical 表把逻辑块映射到物理 NAND block
       读取对应 NAND page 数据
```

这说明 FTL 扫描只在初始化时建表；正常文件读取不会每次从头扫描 NAND。

## 性能判断

OOB 扫描本身不应该是分钟级瓶颈：

```text
0x1000 - 0x5a = 4006 blocks
4006 * 64B ~= 256KB OOB
```

因此，如果模拟器在这里表现很慢，应重点排查：

1. NAND ready/busy 状态是否让 U-Boot 每次读 OOB 都进行过多轮询。
2. OOB/FTL 元数据是否错误，导致扫描失败后反复重新初始化。
3. 是否已经进入 `kj409588.bin` 数据搬运阶段；该文件约 4.6MB，
   如果每个 byte 都通过一次 MMIO data-port read，会比 OOB 扫描慢得多。
4. NAND data port 是否只有 byte 粒度 MMIO 回调，导致 QEMU TCG 性能被放大。

## 镜像构建要求

构建可启动 NAND 镜像时必须同时满足两层结构：

- FAT 层：
  - FAT 文件系统参数要匹配真机。
  - 真机可见容量约 `519,421,952` 字节。
  - 分配单元大小为 16KB，即 `sectors_per_cluster = 32`。
  - `系统/数据/kj409588.bin` 必须能通过 FAT 路径读取。

- raw NAND/OOB 层：
  - FAT 逻辑数据需要铺到 raw NAND 物理页。
  - 每个参与映射的物理 block 第一页 OOB 需要写入固件能识别的
    逻辑块号和序号。
  - OOB 尾部至少需要满足 U-Boot 的判断：

```text
spare[-6..-5] = sequence
spare[-4..-1] = logical block id, 或 0xffffffff, 或 "bbt8"
```

OOB 映射写正确后，U-Boot 扫描一次即可建立正确的
`logical -> physical` 表。它不一定能完全跳过扫描，但不应反复失败或重扫。
