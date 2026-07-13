# 固件与运行时镜像

公开仓库不发布固件、NAND 镜像或商业应用资源。用户和开发者需要自行准备本地 dump。

## 本地目录约定

从仓库根目录或 release 包根目录看，应放置：

```text
系统/
  数据/
    loader_9588_4740.bin
    C200.bin
    kj409588.bin
    u_boot_9588_4740.bin
  ...
应用/
  程序/
    *.bda
  ...
```

默认 `nand` 模式不再把外部 U-Boot、C200 或 `kj409588.bin` 作为 RAM payload 传给 QEMU。
QEMU BootROM 会从 NAND address `0` 按 JZ4740 spare valid flag 读取最多 8 KiB
`loader_9588_4740.bin`，入口为 `0x80000004`，并在 normal 区域不可用时尝试 address
`0x2000` 的 backup loader。loader 再把 NAND page `0x40`
处的 U-Boot 拉起，由 U-Boot 通过 FAT/FTL 读取 `系统/数据/kj409588.bin` 并进入系统。
`C200.bin` 只保留给 direct-boot/旧诊断场景。默认构建不再写 `BBKUBOOT` header；
需要旧镜像兼容时才给 `make_combined_nand.py` 显式传 `--legacy-uboot-header`。显式传 `--image` 时，启动器
仍会把 boot image 复制到 `build/qemu_payloads/`，避免 Windows 下 QEMU 命令行路径处理不稳定。

`make_combined_nand.py` 会同时生成 JZ4740 RS parity。标准 9588 启动区 page
`0x000..0x1ff` 使用 BootROM/first-stage 的 OOB `6+9*n` 布局，从 page `0x200` 起使用
U-Boot 常规 NAND driver 的 OOB `4+9*n` 布局。迁移已有完整 raw NAND 时使用：

```powershell
python .\tools\stamp_nand_ecc.py `
  .\runtime\bbk9588_nand_legacy.bin `
  .\runtime\bbk9588_nand.bin
```

该工具只修改 parity range，保留 page data、FTL OOB metadata 和尾部 sequence/LBA
标签；输入和输出必须是不同文件。非标准 U-Boot 起点或复制长度需要显式传
`--boot-ecc-end-page`。

## 构建 FAT 与 NAND

推荐使用 wrapper：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\build_runtime_images.ps1
```

FAT 构建默认匹配当前真机 dump 规格：

- 卷容量：`519,421,952` 字节，也就是 `0xf7ae0` 个 512 字节扇区。
- 文件系统：FAT16。
- allocation unit：16 KiB，也就是 `32` 个 512 字节扇区。

等价的手动步骤：

```powershell
python .\tools\make_fat16_image.py `
  --output .\build\bbk9588_fat_page1c40.img `
  .\系统 .\应用

python .\tools\make_combined_nand.py `
  --loader-image .\系统\数据\loader_9588_4740.bin `
  --loader-page-base 0 `
  --uboot-image .\系统\数据\u_boot_9588_4740.bin `
  --uboot-page-base 0x40 `
  --fat-image .\build\bbk9588_fat_page1c40.img `
  --output .\build\bbk9588_nand_loader0_uboot40_fat_page1c40.bin `
  --fat-page-base 0x1c40

python .\tools\stamp_ftl_oob.py `
  .\build\bbk9588_nand_loader0_uboot40_fat_page1c40.bin `
  .\runtime\bbk9588_nand.bin `
  --fat-page-base 0x1c40
```

`stamp_ftl_oob.py` 按 C200 原生格式只占用 logical tag 的低 16 位，高 16 位保持
`0xffff` 擦除态。不要把 logical id 写成高半字为 `0x0000` 的 32-bit 值；C200 后续
写入其他 page 时会产生 first/last-valid-page tail 不一致。可用以下命令按固件规则
审计镜像：

```powershell
python .\tools\audit_ftl_nand.py .\runtime\bbk9588_nand.bin --strict
```

对一份写入前 reference 和正常提交后的 raw work，可构造单 logical remap 的
pre-commit 掉电快照：

```powershell
python .\tools\audit_ftl_nand.py .\build\committed.bin `
  --compare .\runtime\bbk9588_nand.bin `
  --inject-remap-power-cut 36 `
  --output .\build\logical36-power-cut.bin
```

工具会恢复 reference 的旧 physical block、只清除新 block last-valid tail 的一个
已编程 bit，并要求扫描结果回退旧 mapping。该故障镜像预期含一个 `torn` anomaly；
固件冷启动后应擦除 torn candidate，未提交的数据可以丢失，但旧文件系统视图应可用。
普通 `--compare` JSON 报告还包含每个 `remap_transitions` 的写前/写后 physical 状态、
old/new 都有效时的 sequence 胜者和 new torn 时的回退目标。

前端默认使用最终的 `_ftloob` 镜像：

```text
runtime/bbk9588_nand.bin
```

## 运行时写入策略

QEMU 前端会把源 NAND 镜像复制到：

```text
build/qemu_nand_runs/
```

普通前端会话不会直接修改 `runtime/bbk9588_nand.bin` 基础镜像；持久 checkpoint
也位于 `runtime/`，因此清理 `build/` 不会删除用户数据。
checkpoint 压实只把 runtime 最新 logical block 的 data page 写回 canonical physical
block，并对变化页重新生成数据区 OOB `4+9*n` RS parity；canonical sequence/logical
metadata 暂时保留。旧 checkpoint 第一次打开时会原子迁移到 C200 的 16-bit logical
tag 格式，避免与后续 guest page program 混用。这仍是固件 FTL 掉电恢复完成前的
兼容层，不等同于真机提交协议。

Web 右侧“文件”标签管理的是当前持久 checkpoint。目录浏览和导出使用只读 FAT
快照；新建目录、导入、改名和删除会先正常停止 QEMU、提交 work copy，再原子更新
checkpoint 并重启 QEMU。该工具只用于离线安装和维护文件，不参与 C200 运行时的
FTL/FAT 访问。

## 发布规则

不要提交：

- `系统/`
- `应用/`
- `build/`
- `runtime/`
- `*.bin`
- `*.bda`
- `*.dba`
- `*.dlx`
- 批量截图、trace、完整反汇编和临时分析输出

`.gitignore` 已排除这些路径和扩展名。
