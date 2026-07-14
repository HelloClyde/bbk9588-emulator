# 模拟器架构

`emu/` 分为四个边界清晰的层：

```text
Web 前端        emu/web/
运行编排        emu/qemu/system.py
数据镜像工具    tools/
QEMU 设备模型   qemu/overlay/
```

## Web 前端

`emu/web/frontend.py` 启动本地 HTTP/WebSocket 服务。浏览器中的 canvas 显示 240x320
framebuffer，触摸和按键事件通过前端 API 送入后端。AIC 的 S16LE PCM 通过独立
`/audio` WebSocket 送入浏览器 AudioContext；首次用户操作用于解锁 iOS 音频策略。
Web 启动路径默认选择 QEMU `driver=none`，避免服务器声卡与浏览器重复播放，但不会
关闭 AIC 时钟、FIFO、DMA 或 PCM 推流。

`emu/web/frontend_state.py` 负责：

- 管理长生命周期 QEMU 进程。
- 接收 frame chardev 中的画面、性能和 PCM 数据，并按画面/音频通道推送给浏览器。
- 把触摸/按键转换成 QEMU input chardev 事件。
- 输出诊断状态。

QEMU 内部由无 guest MMIO 的 `bbk9588-host-input` 持有 input chardev 和 `T/K` 协议
parser；解析后的 typed 事件才进入 machine 的 GPIO/SADC 板级接线。

## QEMU 编排

`emu/qemu/system.py` 负责构建 QEMU 命令、启动进程、管理 HMP/QMP 风格的诊断通道和
frontend backend 状态。它不应承担真实设备行为的长期替代实现。

已删除的 Python/GDB storage fastpath 只保留禁用状态和诊断提示；新的兼容性工作应落到
QEMU C machine / SoC 模型中。

## 镜像工具

`tools/` 中的脚本负责把本地 dump 转为 QEMU 可写 NAND：

- `make_fat16_image.py`
- `make_combined_nand.py`
- `stamp_ftl_oob.py`
- `audit_ftl_nand.py`
- `build_runtime_images.ps1`

这些工具只消费本地 `系统/`、`应用/`，不向仓库写入可提交数据。
`emu/qemu/ftl.py` 是只读/离线共享 parser，按 U-Boot/C200 的 last-valid-page、
完整 6-byte tail 和环形 sequence 规则审计映射；它不参与 guest 运行时 FTL 决策。

## QEMU 设备模型

`qemu/overlay/` 是对 QEMU 11.0.0 的覆盖源码。release workflow 会下载官方
QEMU 源码，复制 overlay，编译 `mipsel-softmmu`，再收集 Windows runtime DLL。

当前独立 QOM 设备包括 AIC、raw NAND、EMC、MSC、UART、UDC、LCD controller、BBK
panel/status、CIM、SADC、GPIO、RTC、INTC、CPM、DMAC 和 TCU。MSC device 负责
`0xb0021000` register bank、
response FIFO、command/DMA pending、`IREG`/`IMASK`、IRQ14、reset 和 migration；machine
通过明确的 DMA API 保留 RAM 搬运、默认无介质读零/写丢弃策略和 trace。UART device
负责 `0xb0030000` register bank、RX FIFO、
serial chardev、IRQ、reset 和 migration；UDC device 负责 `0xb3040000` no-host
register、indexed endpoint config、IRQ、reset 和 migration，尚不包含 USB packet
transport。raw NAND device 负责 `0xb8000000`
command/address/data window、
backing、几何、page program、block erase、OOB 和 ready/busy；EMC device 负责
`0xb3010000` register block、NAND control、Hamming/RS ECC data path、parity/status、
error index/mask、IRQ、reset 和 migration。raw NAND 的 data window 通过 callback
向 EMC 提供实际读写字节，NAND device 本身不持有 ECC 状态。raw NAND 还提供
`fail-program-block`/`fail-erase-block` 调试属性，用于在指定
physical block 返回 NAND ready+FAIL 而不修改 backing；它们只用于恢复测试，不替代
固件自己的 bad-block/FTL 决策。LCD device 负责
`0xb3050000` register block、descriptor DMA 和 IRQ；SADC
device 负责 `0xb0070000` register block、触摸 FIFO、conversion timer 和 IRQ；GPIO
device 负责 `0xb0010000` 的 4 个 port、pin level、flag 和 4 路 IRQ；RTC device 负责
`0xb0003000` 的 seconds、alarm、hibernate、timer 和 IRQ。独立 BBK panel device 负责
`0xb0043000` 的 board register、ready/frame-done 和 W1C。独立、无 guest MMIO 的
BBK host bridge 负责 panel RGB565 scanout、frame/audio/perf chardev、QEMU console
和刷新 timer。无 guest MMIO 的 `bbk9588-diag` 持有 input event ring 以及
storage/MSC/NAND-target/DMAC trace 的 sequence 和 guest RAM recorder；machine 通过
显式参数提交 PC、INTC 和 DMAC 快照，不再依赖全局活动 board 指针。按键、pen、NAND
ready/busy 和 wake 板级接线仍由 machine 连接。
`0xb3060000` 已不再是通用 shadow window，而是独立 JZ4740 CIM idle device；9588
未连接摄像头 sensor，因此它只提供 register mask、FIFO empty、disable-done 和 IRQ17，
不合成 image stream 或 CIM descriptor DMA。
LCD scanout 只采用 JZ LCD descriptor source，不再读取固定 guest mirror config 或
framebuffer fallback。BootROM 的 normal/backup 选择也属于 machine 启动策略，只通过
raw NAND 的只读 backing API 取 first-stage 数据。

运行层只维护一个调用方明确指定的活动 raw NAND。QEMU 使用 writeback 直接写该文件；
首个脏写后 1 秒通过 block AIO 异步 flush，正常关闭时同步 flush，block erase 合并为
一次 backing write。Python 不创建 persistent/disposable work copy，也不执行 host
canonical FTL 压实。测试在自己的临时目录管理 fixture，probe 未显式传 NAND 时不挂载
介质。Windows 前端用 kill-on-close Job Object 约束 QEMU 子进程，Web 被强杀时不会
留下继续写 NAND 的孤儿 QEMU。

Web 运行层使用一把可重入 NAND lifecycle lock 串行化停止、文件修改、镜像
替换、重启、reset 和镜像切换。每个活动镜像路径还持有 OS 级跨进程独占
租约；第二个 Web 实例在启动 QEMU 或覆盖镜像前就会失败。`start-web.cmd -Nand`
的解包和导入也在持有该租约的 Web 进程内完成，不再由启动脚本
预先覆盖活动文件。

标准 BBK9588 NAND 镜像存在两种固件原生 OOB ECC 布局：BootROM 和 first-stage boot
copy 使用 `6+9*n`，U-Boot 常规 C200/FAT NAND 路径使用 `4+9*n`。默认 boot copy 从
page `0x40` 读取 `0xe0000` bytes，因此 `stamp_nand_ecc.py` 以 page `0x200` 为默认
边界；不同启动布局必须显式传入 `--boot-ecc-end-page`。

设备模型优先级：

1. 按真实 SoC/MMIO 行为补设备寄存器。
2. 让固件自然走原有逻辑。
3. 仅保留必要诊断开关，避免默认路径依赖系统级 hook。
