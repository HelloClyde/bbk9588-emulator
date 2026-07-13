# Changelog

本项目的重要版本变更记录在此。模拟器版本遵循语义化版本号；NAND 镜像使用独立的
`nand-v*` 版本线。

## [Unreleased]

- Web/QEMU 改为 writethrough 直接读写唯一活动 raw NAND，删除 persistent/disposable
  work copy、canonical checkpoint、正常停止压实流程和相关状态/CLI；测试改为显式管理
  临时 fixture，probe 默认不挂载 NAND。旧 checkpoint 首次启动会原子迁入活动镜像。
- Windows QEMU 子进程加入 kill-on-close Job Object；Web 被强制终止时由内核同步结束
  QEMU，避免孤儿进程继续占用或并发写入唯一活动 NAND。
- NAND 文件管理器改为停机后直接更新活动 NAND，并为变化 data page 重算 U-Boot/C200
  数据区 `4+9*n` RS parity；恢复镜像改由启动器显式重新导入，不保留隐藏基础副本。
- 将 RGB565 scanout、frame/audio/perf chardev、QEMU console、帧缓存和刷新 timer
  从 machine 迁移到独立、无 guest MMIO 的 `bbk9588_host_bridge`；JZ4740 LCD/AIC/DMAC
  继续只持有 guest 可见硬件状态。
- 还原 U-Boot/C200 FTL cold-scan 的 last-valid-page、完整 6-byte tail 和 16-bit 环形
  sequence 规则，新增共享 parser、严格镜像审计和 commit-tail 掉电注入工具。修正构造
  镜像 logical tag 高半字：C200 只写低 16 位，高半字必须保持 `0xffff`；旧 checkpoint
  会原子迁移，避免运行写入后出现 torn mapping。名片文件写入触发的 10 个 logical
  remap 已通过不经 checkpoint 的 raw 冷启动恢复；新增 reference/committed remap
  pre-commit 故障快照，验证 torn 新块回退旧块及固件启动清理。多 block/垃圾回收和
  guest 故障恢复矩阵仍待完成。
- raw NAND device 新增按 physical block 注入 page-program/block-erase failure 的属性；
  失败命令返回 ready+FAIL `0x41` 且不改 backing，成功命令恢复 ready `0x40`。qtest
  同时校验进程内读回和 QEMU 退出后的 raw 文件。
- 将匿名 `0xb3060000` shadow MMIO 替换为独立 JZ4740 CIM idle device：实现手册
  CFG/CR mask、RX FIFO empty、disable-done、status W0C、IRQ17、reset 和 migration；
  BBK 9588 无摄像头 sensor，因此不合成图像数据或 descriptor DMA。
- 将 BBK 板级 `0xb0043000` panel/status register、ready/frame-done、W1C、reset 和
  migration 从 machine 迁移到独立 `hw/display/bbk9588_panel.c`；JZ4740 LCDC 仍只负责
  `0xb3050000` controller/descriptor/IRQ，host scanout 和 frame chardev 保留为板级 bridge。
- 将 MSC register bank、response FIFO、command/DMA pending、`IREG` 写一清零、
  `IMASK`/IRQ、reset、migration 和诊断状态迁移到独立 `hw/sd/jz4740_msc.c`；machine
  只保留 DMAC RAM 搬运、无介质读零/写丢弃策略和 storage trace，并连接 INTC source 14。
- 将 UDC no-host register、indexed endpoint config、interrupt status/enable、IRQ、
  reset 和 migration 从 machine 迁移到独立 `hw/usb/jz4740_udc.c`；USB attach、packet
  transport 和 endpoint FIFO backend 仍明确保留为后续功能缺口。
- 将 UART0 register bank、16-byte RX FIFO、DLAB/divisor、loopback、serial chardev、
  IRQ、reset 和 migration 从 machine 迁移到独立 `hw/char/jz4740_uart.c`；machine
  只连接 serial0、MMIO 和 INTC source 9。
- 实现 JZ4740 NAND ECC core 和 EMC data path：支持 512-byte Hamming encode、
  RS(511,503) encode/decode、NFPAR/NFERR/NFINTS/NFINTE、W0C status 与 EMC IRQ；
  删除 raw NAND 内旧的 fake BCH busy/done 状态。
- 修复持久 checkpoint 压实后的 OOB parity：只要 runtime data page 发生变化，就按
  U-Boot/C200 数据区 `4+9*n` 重新生成 RS parity，同时保留 canonical FTL metadata。
- BootROM 现在按 OOB `6+9*n` 校验并纠正 first-stage，1~4 symbol 错误可恢复，
  uncorrectable normal area 会回退到 backup，并按手册在第一张 invalid page 处结束
  “最多 8 KiB”的加载。镜像工具区分 boot OOB `+6` 与 U-Boot 数据区 OOB `+4`，
  修复启用真实 ECC 后 U-Boot 无法加载 C200 内核的问题。
- 删除 `0x804a6b88` guest LCD mirror、`0xa1f82000` 固定 framebuffer fallback 和
  `0xb0043000` alias observer，修正 JZ LCD descriptor DMA 的 physical RAM 地址被
  machine 拒绝的问题。新增连续 raw RGB565 hash/逐像素差异回归；descriptor-only
  冷启动、主菜单、输入和 4 帧稳定性 smoke 通过。
- 将 JZ4740 EMC register block、IRQ、reset 和 migration 迁移到独立
  `hw/mem/jz4740_emc.c`，并将 raw NAND backing、几何、命令状态、program/erase、
  OOB 和 ready/busy 迁移到独立 `hw/block/bbk9588_nand.c`。machine 仅保留 BootROM
  策略、诊断 trace 以及 GPIO/wake 板级接线。
- 将 JZ4740 RTC 的 seconds、1 Hz、alarm、hibernate registers、timer、IRQ、reset
  和 migration 从 `bbk9588.c` 迁移到独立 `hw/rtc/jz4740_rtc.c` QOM device。
- 将 JZ4740 GPIO 的 4-port register banks、外部 pin level、FLG latch、4 路 IRQ、
  reset 和 migration 从 `bbk9588.c` 迁移到独立
  `hw/gpio/jz4740_gpio.c` QOM device；9588 键位、pen、NAND 和 wake 接线保留在
  machine。
- 修正 Web smoke 对低配色主菜单和应用启动画面的误判，同时继续要求非黑像素、
  颜色数、GUI active 和输入队列消费达到有效阈值。
- 将 JZ4740 SADC register、2-entry touch FIFO、conversion timer、PBAT/SADCIN、
  IRQ、reset 和 migration 从 `bbk9588.c` 迁移到独立
  `hw/input/jz4740_sadc.c` QOM device。
- 将 JZ4740 LCD register、descriptor DMA、SOF/EOF IRQ、reset 和 migration 从
  `bbk9588.c` 迁移到独立 `hw/display/jz4740_lcd.c` QOM device。
- 修复独立仓库中雷霆战机 Web smoke 的 NAND 工具导入路径。
- 雷霆战机 Web smoke 会自动打开游戏音效，并校验 8000 Hz AIC 播放、DMA/output
  增长、xrun、Game Over 返回菜单以及第二次战斗的重复播放生命周期。
- 修复 Web smoke 在 C200 主界面已经激活时重复执行触摸校准的问题。

## [v0.1.2] - 2026-07-12

- 将 JZ4740 AIC 的 S16LE PCM 通过 chardev 和独立 WebSocket 推送到浏览器。
- 增加浏览器音频开关、低延迟缓冲、断线恢复和过期音频丢弃。
- 兼容 iOS Safari 音频激活、`interrupted` 恢复和 AudioContext 时钟卡死重建。
- Web 模式默认关闭 QEMU 主机重复音频输出，仍保留 AIC、FIFO 和 DMA 时序。
- 修复移动端设备按键双击缩放、长按选中文字和长按菜单干扰。
- 补充历史版本记录，并让 Release workflow 自动生成提交列表和版本比较链接。

## [v0.1.1] - 2026-07-12

- 增加可配置的键盘和手柄映射，并修复手柄捕获焦点与诊断状态。
- 降低连续触摸绘图时的输入延迟和背压等待。
- 增加模拟器画面全屏显示。
- 增加移动端响应式布局，左右面板在窄屏下改为抽屉。
- 更新 README 顶图，并按真机照片修正设备外观和按键。

## [v0.1.0] - 2026-07-11

- 首次发布 BBK 9588/JZ4740 QEMU 硬件模型和 Windows 运行包。
- 从 BBK 9588 BDA SDK 仓库拆分为独立模拟器项目，并提供 QEMU 11.0.0 overlay。
- 支持 BootROM、NAND loader、U-Boot 和 C200 系统冷启动。
- 提供 Web 屏幕、触摸、六键输入、运行状态和性能指标。
- 提供可写 NAND、持久 checkpoint 和 NAND 文件管理。
- 支持用户导入 raw NAND 或只包含一个 NAND 镜像的 ZIP。
- 集成 AIC、DMAC、INTC、TCU、CPM、SADC、GPIO、LCD 等设备模型。

[Unreleased]: https://github.com/HelloClyde/bbk9588-emulator/compare/v0.1.2...HEAD
[v0.1.2]: https://github.com/HelloClyde/bbk9588-emulator/compare/v0.1.1...v0.1.2
[v0.1.1]: https://github.com/HelloClyde/bbk9588-emulator/compare/v0.1.0...v0.1.1
[v0.1.0]: https://github.com/HelloClyde/bbk9588-emulator/releases/tag/v0.1.0
