# JZ4740 手册驱动的 QEMU 改造计划

本文基于当前 `bbk9588` QEMU 实现、`u_boot_9588_4740.bin` 反汇编结论，以及
[JZ4740 Programming Manual](https://opennoah.github.io/datasheet/JZ4740_pm.pdf)
整理后续硬件模型改造范围。

本页于 2026-07-13 按当前分支和 JZ4740 Programming Manual 重新核对。勾选含义如下：

- [x] 默认 `bbk9588` 路径已经实现，并有代码测试或实际启动结果支持。
- [ ] 尚未完成；标注“部分完成”的条目不能视为最终硬件模型。

## 结论摘要

原计划的方向仍然正确，但“当前状态”已明显过时。当前默认路径已经能从 raw NAND
冷启动进入系统，并且不再依赖 Python/GDB 固件 hook、QEMU C FAT16 解析或默认固件
patch。当前版本已经具备可用的启动、显示、输入和存储路径，后续优先级改为：

1. 优先让 Web/QEMU 使用可复用的持久运行 NAND，应用创建或修改的文件必须在停止、
   Web 重启和 QEMU 冷启动后保留；测试自行创建临时 NAND fixture，probe 默认只读，
   QEMU 运行层不再提供 disposable copy。
2. 继续补齐 AIC/I2S、板级 codec 和系统提示音验收；游戏、音乐和 host audio 基线
   已经可用。
3. 音频基线稳定后，将 `bbk9588.c` 按设备拆分，降低继续补硬件模型时
   的回归风险。
4. 再处理固件 FTL、NAND ECC、PM 和 USB 等准确性与完整性工作。

这只是执行顺序调整，不表示 FTL、ECC、PM 或 USB 已经完成。当前可用路径继续保留，
以“不破坏现有可用版本”为阶段约束。

| 模块 | 当前结论 |
| --- | --- |
| BootROM/NAND first-stage | 固定 2 KiB NAND 的 RS 校验、1~4 symbol 纠错和 normal/backup 回退已完成；完整 boot-select 与 NOR fallback 仍缺失 |
| raw NAND/EMC | EMC 和 raw NAND 已拆为独立 QOM device，Hamming/RS、bad-marker cold-scan、确定性 program/erase FAIL、C200 FTL 正常 remap raw 冷启动及单 block pre-commit 回退已验证；Web/QEMU 已改为唯一活动 raw NAND，完整物理故障矩阵仍是非阻塞研究项 |
| INTC/TCU/WAIT | INTC、TCU 已拆为独立 QOM device；板级 wake proxy 仍需核实 |
| LCD | controller、descriptor DMA、IRQ 和 BBK panel/status 窗口已拆为独立 QOM device；固定 guest mirror 与 alias scanout 已删除，完整 panel command 细节留作非阻塞研究项 |
| SADC/Touch/GPIO | SADC、GPIO 已拆为独立 QOM device，当前交互路径基本完成；板级接线真机确认仍未完成 |
| AIC/I2S/audio codec | 独立 AIC、internal codec、audio DMA、host/Web output 已实现并经用户实际验收；外部板级 route trace 属于非阻塞研究项 |
| DMAC/MSC/UART/UDC/CIM/RTC/PM | CPM、DMAC、MSC、RTC、UART、UDC、CIM 已独立，MSC/DMAC/AIC transport 已进入无 MMIO board bridge；MSC 已与 raw NAND 解耦，USB packet transport、独立可选介质、完整 PM 和剩余 DMA request 仍是缺口 |
| Python/Web 收敛 | 默认路径已完成，旧诊断代码仍可继续删除 |
| QEMU 文件结构 | 部分完成，主要 SoC 设备、host output/input、DMA bridge 和全部 diagnostic recorder 已独立；板级 wake、touch GPIO/SADC 接线等 glue 仍在 `bbk9588.c` |

## 当前阶段优先级

| 优先级 | 范围 | 阶段目标 |
| --- | --- | --- |
| 已完成 | Web/QEMU 运行时 NAND 持久化 | 应用文件跨停止、Web 重启和 QEMU 冷启动保留；显式恢复时才重建持久副本 |
| 已完成 | AIC/I2S、audio DMA、codec、主机/Web 音频输出 | 音频已由用户在实际 Web 环境验收，不再阻塞后续结构改造 |
| P1 | QEMU 设备结构拆分 | `bbk9588.c` 只保留 machine 和板级连线，各设备拥有独立 state/MMIO/IRQ/reset |
| P2 | FTL、NAND Hamming/RS ECC | 提高存储和启动的硬件真实性，保持现有镜像兼容 |
| P3 | PM、USB、剩余 DMA corner case、旧 Python 诊断清理 | 补齐非核心场景和维护性工作 |

NAND 持久化和音频已完成当前用户验收，不再作为后续阻塞项。当前从 P1 结构拆分
继续，设备迁移期间保持现有 MMIO 和板级行为；外部 codec/功放 trace 留作研究资料，
不会阻止 RTC、EMC 和显示收敛。

## 手册核对后的修正

- BootROM 章节明确：NAND normal area 从 address `0` 开始，backup area 从
  `0x2000` 开始，每个区域最多读取 8 KiB，成功后从 `0x80000004` 执行。当前实现
  的核心地址和大小与此一致。
- JZ4740 NAND ECC 是 Hamming 或 Reed-Solomon。旧计划中的“ECC/BCH”表述不准确，
  后续不应把当前 `bch_status` 兼容命名当作真实 JZ4740 BCH 硬件。
- INTC 的 `ICSR/ICMR/ICMSR/ICMCR/ICPR` 和 source 编号与手册第 7 章一致；
  `ICPR` 是未屏蔽后的 pending 视图，不应由软件直接清除。
- JZ4740 TCU 有 8 个 counter channel，旧文档中的“6 个 channel”已经删除。
- LCD `EOF/SOF/OUF/IFU/LDD/QD`、descriptor 的 `SOFINT/EOFINT/LEN` 语义与手册
  第 10 章一致。
- `ADTCH` 是 2x32-bit FIFO 项，含两个 12-bit 数据和 type bit；当前 SADC FIFO
  建模方向正确。
- 手册第 24 章开头写 internal Boot ROM 为 8 KiB，但 memory map 又标为 4 KiB；
  这是手册自身不一致。NAND first-stage 的“最多复制 8 KiB”在 boot sequence 和
  NAND boot specification 中是一致的，不应把两者混为一谈。

## 目标与当前边界

长期目标保持不变：

1. QEMU 只模拟 JZ4740 SoC、板级外设和 raw NAND 行为。
2. loader/U-Boot/C200 自己完成 FTL、FAT、资源缓存和 UI 逻辑。
3. Python 只负责启动、Web 前端、镜像打包、只读诊断和 QEMU 停止后的离线 NAND
   文件维护；guest 运行时的 FTL/FAT 仍全部由 loader/U-Boot/C200 完成。

主要代码位置：

- `qemu/overlay/hw/mips/bbk9588.c`：machine、BootROM 策略、板级 IRQ/wake/touch
  连线、progress timer 和诊断所需的板级 wake 快照。
- `qemu/overlay/hw/display/bbk9588_host_bridge.c`：独立无 MMIO 的 host bridge，负责
  LCD RGB565 scanout、frame/audio/perf chardev、控制台和刷新定时器。
- `qemu/overlay/hw/input/bbk9588_host_input.c`：独立无 MMIO 的 host input bridge，
  负责 input chardev 生命周期和 `T/K` 文本协议解析，再通过 typed callback 接入
  machine 的 SADC/GPIO 板级连线。
- `qemu/overlay/hw/dma/bbk9588_dma_bridge.c`：独立无 guest MMIO 的 board transport，
  连接 MSC command/data、DMAC bulk/AIC endpoint 和 diagnostic sample；machine 不再
  实现 peripheral ops。DMAC IRQ 保留很薄的 board adapter，以触发 INTC/TCU level
  重采样，不能机械改成直接 GPIO 接线。
- `qemu/overlay/hw/misc/bbk9588_diag.c`：独立无 guest MMIO 的 diagnostic recorder，
  持有 input、storage、MSC、NAND-target、DMAC、touch、progress 和 graphics trace
  状态；通过显式连接的设备源和 machine 提供的板级 wake 快照写入保留的 guest
  diagnostic RAM 或输出 panel trace。
- `qemu/overlay/hw/sd/jz4740_msc.c`：独立 MSC register/response FIFO、command/DMA
  pending、`IREG` 写一清零、`IMASK`/IRQ14、reset、migration 和诊断接口。
- `qemu/overlay/hw/display/bbk9588_panel.c`：独立 BBK `0xb0043000` panel/status
  register、ready/frame-done、W1C、reset 和 migration；host scanout 由独立 bridge
  连接，不进入 JZ4740 LCD register device。
- `qemu/overlay/hw/misc/jz4740_cim.c`：独立 JZ4740 `0xb3060000` idle CIM；实现
  register mask、FIFO empty、disable-done、status W0C、IRQ17、reset 和 migration，
  无 camera sensor/image/DMA backend。
- `qemu/overlay/hw/char/jz4740_uart.c`：独立 UART0 register bank、16-byte RX FIFO、
  DLAB/divisor、loopback、serial chardev、IRQ、reset 和 migration。
- `qemu/overlay/hw/usb/jz4740_udc.c`：独立 UDC no-host register path、indexed
  endpoint config、interrupt enable/status、IRQ、reset 和 migration；尚无 USB packet
  transport 和 endpoint FIFO backend。
- `qemu/overlay/hw/block/bbk9588_nand.c`：独立 BBK raw NAND backing、几何、
  command/address/data、program/erase、OOB、ready/busy、reset 和 migration。
- `qemu/overlay/hw/mem/jz4740_emc.c`：独立 JZ4740 EMC register window、NAND
  control/ECC completion 状态、IRQ、reset 和 migration。
- `qemu/overlay/hw/misc/jz4740_cpm.c`：独立 JZ4740 CPM/sysctrl
  寄存器、reset、migration 和板级 wake 配置通知。
- `qemu/overlay/hw/dma/jz4740_dmac.c`：独立 JZ4740 DMAC channel、
  descriptor、request、IRQ、RAM/外设搬运和 migration。
- `qemu/overlay/hw/timer/jz4740_tcu.c`：独立 JZ4740 TCU 8-channel
  counter、full/half compare、三组 parent IRQ、reset、migration 和诊断状态。
- `qemu/overlay/hw/display/jz4740_lcd.c`：独立 JZ4740 LCD register、
  descriptor DMA、SOF/EOF state、IRQ、reset 和 migration；BBK panel scanout 与
  frame chardev 板级连接位于 `bbk9588_host_bridge.c`。
- `qemu/overlay/hw/input/jz4740_sadc.c`：独立 JZ4740 SADC register、2-entry
  touch FIFO、conversion timer、PBAT/SADCIN、IRQ、reset 和 migration；触摸坐标与
  GPIO pen/key 接线由 machine 提供。
- `qemu/overlay/hw/gpio/jz4740_gpio.c`：独立 JZ4740 4-port register banks、外部
  pin level、FLG latch、4 路 IRQ、reset 和 migration；BBK 键位、pen、NAND/wake
  接线由 machine 提供。
- `qemu/overlay/hw/rtc/jz4740_rtc.c`：独立 JZ4740 seconds counter、1 Hz/alarm、
  hibernate registers、RTC timer、IRQ、reset 和 migration。
- `emu/qemu/system.py`：命令构造、进程管理和只读诊断；旧 hook 代码仍保留但
  `bbk9588` 默认路径禁用。
- `tools/make_combined_nand.py`：构建 raw boot 区和 FAT 数据区。
- `tools/stamp_ftl_oob.py`：离线写入当前固件能识别的 OOB FTL 标签。
- `emu/web/frontend_state.py`：frame/input chardev、Web 状态和测试用自动校准。

手册能定义 SoC 寄存器和时序契约，但不能定义 BBK 私有 FTL 标签、C200 资源格式、
板级 GPIO 接线、触摸校准参数或 LCD panel 细节。这些仍要靠真机 dump、反汇编和
运行 trace 确认。

## 设备状态与待办

### 1. BootROM 与 NAND 启动路径：基本完成

已完成：

- [x] 默认 `nand` 模式不预加载 `C200.bin`、U-Boot 或 `kj409588.bin` 到 RAM。
- [x] BootROM 从 NAND address `0` 复制最多 8 KiB first-stage 到 physical `0`，
  reset PC 为 `0x80000004`。
- [x] normal area 擦除或 OOB valid bytes 无效时，会尝试 address `0x2000` 的
  backup area。
- [x] QEMU C 已删除 `bootrom-fat-kernel`、`BBKUBOOT` 解析和 FAT 内核查找。
- [x] `make_combined_nand.py` 默认同时写 normal/backup loader；旧 `BBKUBOOT`
  header 仅由显式 `--legacy-uboot-header` 生成。
- [x] 默认启动链已实际进入 C200 主菜单，说明 loader/U-Boot/FAT 路径能够工作。
- [x] 固定 2 KiB page 启动路径会按每 512-byte chunk 读取 OOB `6+9*n` 的
  JZ4740 RS parity；1~4 个 symbol 错误先在临时页内纠正，整段 first-stage 验证成功后
  才写入 RAM，任一 chunk 不可纠正时放弃 normal area 并尝试 backup。
- [x] 按手册“up to 8KB”语义读取连续 valid pages；遇到第一张 invalid page 时把它
  视为 first-stage 结束，已经验证的前缀写入 SRAM。第一张 page 就 invalid 仍判定该
  area 不可启动。
- [x] normal clean、normal 4-error correction、normal 5-error backup fallback 和
  normal OOB invalid backup fallback 均有 sidecar 运行回归；单页 2 KiB first-stage
  后接 invalid page 的提前结束也有回归。

仍需完成：

- [ ] 当前 board 固定为 2 KiB page 的 BBK NAND；没有完整实现 `boot_sel[1:0]`、
  512-byte page、8/16-bit bus width 和 2/3 address cycle 探测。
- [ ] normal/backup 都失败时当前直接报错退出，没有实现手册中的 CS4 NOR fallback。
- [ ] `bootrom-page`/`bootrom-size` 诊断 raw-copy 模式可以保留，但发布文档要持续明确
  它不是 JZ4740 BootROM 行为。

验收：

- [x] 只提供 NAND 镜像即可完成 BootROM -> loader/U-Boot -> C200 冷启动。
- [x] normal area 被擦除或 valid OOB 无效时，集成测试可观察到 backup area 启动。
- [x] 注入 1~4 个 RS symbol 错误会纠正原始 first-stage；注入 5 个错误会判定
  uncorrectable 并进入 backup area。
- [ ] normal/backup 都不可纠正时，验证并实现 CS4 NOR fallback。

### 2. NAND/EMC 与 raw NAND 后端：部分完成

已完成：

- [x] `Bbk9588NandState` 已是独立 QOM NAND device，支持 READ ID、page read、
  program、erase、ready/busy 和 raw OOB。
- [x] 默认几何为 2048-byte page、64-byte OOB、64 pages/block、4096 blocks。
- [x] backing 只按 `2048+64` raw stride 或显式 legacy page-only 格式识别，不再
  通过 FAT boot sector 猜偏移。
- [x] NAND program 使用 `old & new`，erase 恢复 `0xff`，并写回运行时 NAND copy。
- [x] QEMU C 已移除 FAT16 boot-sector scan、目录项、cluster/resource fastpath、
  logical FAT sector helper 和 FAT page protect。
- [x] NFCSR、NFECCR、NFPAR0..2、NFINTS、NFINTE、NFERR0..3 和 NFECC 已有
  reset、RW mask、W0C status、完成状态和 EMC IRQ 语义。
- [x] `stamp_ftl_oob.py` 和 `make_combined_nand.py` 已固定/显式配置真实 NAND 几何。
- [x] 反汇编已定位 C200 FTL 初始化 `0x8017d8e0`、冷扫 `0x8017db6c` 和 OOB read
  helper `0x80184300`；运行期应用启动观察到 1950 次 read、192 次 program 和 3 次 erase，
  同期 MSC 事件为 0，固件自管 FTL 边界已经找到。
- [x] 已删除 `bbk9588_msc_build_oob_lba_map()` 和全部 `msc_oob_lba_*` 状态；MSC
  默认未挂载介质，不再读取或写入 raw NAND backing。移除后冷启动、主菜单、应用
  资源读取和 C200 raw NAND program/erase 实测通过。
- [x] raw NAND 只负责 command/address/data/backing，通过 data callback 将每次 NAND
  data window 访问送入 EMC；旧 `bch_status`、fake busy/done 和 NAND 内 ECC 状态已删除。
- [x] JZ4740 ECC core 实现 512-byte Hamming encode 及固定参数
  RS(511,503)/GF(2^9) encode/decode；RS 最多报告 4 组 index/mask，5-error 返回
  uncorrectable。按手册参数计算的全 `0xff` 已知回归向量为
  `cd 9d 90 58 f4 8b ff b7 6f`。
- [x] qtest 经真实 NAND data window 验证 parity registers、NFINTE mask、EMC IRQ、
  4-error 的 `ERRC/NFERR/DECF/ERR`、5-error 的 `UNCOR` 以及 NFINTS W0C。
- [x] 镜像工具按固件真实布局区分 ECC：BootROM/first-stage boot copy pages 使用 OOB
  `6..41`，U-Boot C200/FAT 数据区使用 `4..39`。标准布局边界为 page `0x200`；私有
  sidecar Web 冷启动已进入主菜单并响应触摸，证明 U-Boot 能加载 C200 内核。

仍需完成：

- [x] 实现 JZ4740 Hamming/Reed-Solomon ECC data/parity/status/error index/mask，
  ECC data path、寄存器和 IRQ 均由独立 EMC device 持有。
- [x] EMC controller 已迁移到独立 `hw/mem/jz4740_emc.c`，raw NAND backing、几何、
  command/address/data、program/erase 和 OOB 已迁移到独立
  `hw/block/bbk9588_nand.c`；machine 只保留 BootROM 策略、diagnostic trace、GPIO
  ready/busy 和 wake 接线。Windows 对象编译与 sidecar 链接通过，独立 GDB/MMIO
  回归验证 NFCSR mask、NFINTS busy/done、erase/program/readback 及 backing 写回。
- [x] 共享 FTL parser 已覆盖最后一页 bad-block marker；raw NAND device 新增按 physical
  block 注入 program/erase failure 的属性，失败返回 ready+FAIL `0x41`、不修改内存或
  backing，后续成功命令恢复 ready `0x40`。qtest 已覆盖状态、读回及进程退出后的文件。
- [ ] 非阻塞研究项：补 guest FTL 面对物理 bad block、program/erase failure 和 OOB
  各提交边界的恢复测试，并与真机 trace 对齐。虚拟 NAND 默认无坏块且不注入失败，
  该矩阵不再阻塞单一活动 NAND。
- [x] 正常 remap raw 冷启动和 single-block torn-tail 回退证据已足够支持普通虚拟介质；
  host canonical checkpoint 和 persistent/disposable work-copy 路径已经删除。更完整的
  sequence、回收和物理故障矩阵继续作为非阻塞研究项。
- [x] 从 U-Boot `0x80903d1c` 和 C200 `0x8017db6c` 还原 cold-scan：bad marker、
  `u16` last-valid-page、first/last 6-byte commit 比较、`bbt8`、低 16-bit logical id
  及 16-bit 环形 sequence 候选替换均已进入共享只读 parser 和单元测试。
- [x] 对当前 8012 work image 与 canonical checkpoint 做只读审计：观察到 61 个 logical
  block 重映射；严格固件规则识别出 5 个 first/last tail 不一致的 torn block，其中
  包含 logical block 0。根因是旧构造 tag 高 16 位为 `0x0000`，而 C200 page program
  只写低 16 位并保持高 16 位 `0xffff`。
- [x] `stamp_ftl_oob.py` 已改为 C200 的 `0xffff:logical16` 格式；旧 checkpoint 会先
  原子迁移所有 legacy page tag，checkpoint 重建也固定使用新格式。新增
  `audit_ftl_nand.py` 可输出映射/anomaly/对比报告并注入只清位的 commit-tail 掉电故障。
- [x] 新格式私有 NAND 首次冷启动产生 `bbt8` 写入后仍通过严格审计；保留 raw work
  不经 canonical checkpoint 再次冷启动，2.695 秒输出 2 帧非空画面。该证据覆盖基础
  启动维护写入。
- [x] 名片应用创建记录并正常退出后，raw work 观察到 10 个 logical remap（包括
  logical 0/36/37）、21 个 physical block 和约 2.54 MB data 变化；严格审计无
  torn/invalid/legacy tag。该 raw work 不经 checkpoint 在 2.727 秒冷启动，重新打开
  名片应用仍显示保存的记录。
- [x] `audit_ftl_nand.py --inject-remap-power-cut` 可从 reference/committed 镜像构造
  “旧 physical block 保留、新 block last-valid tail 撕裂”的 pre-commit 快照，并验证
  mapping 回退旧块。logical 36 实际快照在 2.78 秒冷启动，未提交名片记录回退；固件
  擦除 torn candidate 后 raw work 再次通过严格审计。
- [x] comparison 对全部 10 个 remap 输出候选阶段矩阵：new physical 写前均为 free，
  old physical 提交后均为 free；9 个 `seq 1 -> 2` 在 old/new 都有效时选择 new，
  logical 0 的 `seq 1 -> 0` 选择 old，形成提交完成前的安全回滚窗口；new tail torn
  时 10 项均回退 reference mapping。
- [ ] 上述证据仍未覆盖多 logical transaction 的每个提交边界、free-block 回收、
  sequence wrap 实机样本、guest 对 bad-block/program/erase failure 的恢复和任意
  指令点断电。
- [x] 阶段兼容层曾使用稳定 checkpoint、隔离 work copy 和正常停止 logical-view
  压实，并验证跨冷启动持久化；该运行路径现已由第 23 项完全删除。
- [x] 阶段兼容层曾为压实后变化的 data page 重算 OOB `4+9*n` RS parity；当前文件
  管理器直接修改活动 NAND 时只重算实际变化页，不再执行 canonical FTL 压实。
- [x] 阶段兼容层曾展示 persistent/disposable、checkpoint、work image 和提交错误；
  这些状态和提交动作现已删除，状态统一显示活动 NAND 的 `direct` write mode。
- [x] checkpoint 阶段曾提供 Web “↺ 恢复”操作；单一活动 NAND 不再维护隐藏基础副本，
  该按钮和 `restore-nand-image` 已删除。恢复/更换镜像改为显式运行
  `start-web.cmd -Nand <镜像或ZIP>` 替换活动文件。
- [x] 活动镜像固定为 `runtime/bbk9588_nand.bin`；旧 `runtime/qemu_nand_persistent/`
  只用于一次性升级迁移，`build/qemu_nand_runs/` 不再由代码创建。FAT 只读快照缓存仍
  可留在 `build/`，它不作为 QEMU backing。
- [x] Web 右侧增加“状态/文件”标签；文件页支持新建目录、导入/导出、改名和递归
  删除。写操作先停止 QEMU，再原子修改活动 NAND、重算变化页 ECC 并重启；该离线工具
  不进入 guest 运行路径，也不改变 QEMU C 不解析 FAT 的边界。
- [x] Web persistent 模式已改为直接打开唯一活动 raw NAND；已有正常 remap raw
  冷启动和单 block torn-tail 回退作为迁移基线。虚拟 NAND 默认无坏块、无故障注入，
  page program/erase 实时写入该文件，正常停止、QEMU 崩溃、Web 强退和下次冷启动都不再
  创建、提交或删除 persistent work copy。
- [x] 删除 `ensure_runtime_nand_checkpoint()`、`commit_runtime_nand_checkpoint()` 及
  persistent checkpoint 状态，同时删除 `prepare_runtime_nand_image()`、
  `build/qemu_nand_runs/` 和 persistent/disposable 分支。Release/用户导入的镜像只作为
  恢复来源，启动器显式导入时才替换活动 NAND。
- [x] 单元测试和集成测试在各自临时目录创建 NAND fixture 并把该路径直接传给 QEMU；
  probe 默认只读，确需写入时也必须由调用方显式提供临时镜像。后端不复制、不猜测、
  不删除调用者传入的 NAND。
- [x] NAND 文件管理器在 QEMU 停止后直接操作同一活动 NAND，并完成导入、导出、改名、
  删除后原地冷启动回归；不得再通过 canonical checkpoint 隐式重排 FTL physical block。
- [x] Web 的停止、修改、替换、重启、reset 和镜像切换已统一进入 NAND lifecycle
  lock；活动路径持有 OS 级跨进程独占租约，第二实例不能共用同一 NAND。
  `start-web.cmd -Nand` 也先取得该租约，再执行导入和替换。
- [x] Web 文件导入已改为最大 128 MiB 的分块临时上传，拒绝 chunked、超限和短读；
  上传临时文件在写入 FAT 前和候选 NAND 中均校验大小与 SHA256。
- [x] 离线写入先构造同目录候选 NAND，检查 FTL 映射元数据不变、FAT 逻辑镜像与
  请求结果一致，并从候选镜像重新读取目标文件验证大小和 SHA256。全部通过后
  才 `os.replace()`；任一校验失败时原 NAND 字节不变。

验收：

- [x] QEMU 不认识 FAT boot sector、目录项、文件名或资源对象，主菜单仍可进入。
- [x] raw NAND program/erase 不再保护构造镜像中的 FAT page range。
- [x] 运行时 NAND drive 已从逐次 `writethrough` 改为 `writeback`；首个脏写后 1 秒通过
  block AIO 异步 flush，正常关闭时同步 flush。block erase 的 64 个 page backing write
  已合并为一次完整 block write，避免固件 FTL 回收时放大主循环卡顿。
- [x] 删除 MSC OOB FTL 翻译后，固件仍能扫描 OOB、读取资源并执行 raw NAND 写入。
- [x] RS clean、1~4-error correction、5-error uncorrectable、status/IRQ 和 BootROM
  backup 行为已有手册参数下的已知向量、纯 C、qtest 与运行时交叉回归。
- [ ] 非阻塞研究项：bad-block marker、program/erase failure 和细粒度掉电中断行为与
  真机 trace 一致。

### 3. INTC/TCU/CPU WAIT：基本完成

已完成：

- [x] INTC 实现 `ICSR/ICMR/ICMSR/ICMCR/ICPR`、reset mask 和手册 source 编号。
- [x] INTC 已迁移到独立 `hw/intc/jz4740_intc.c`，拥有独立 state、MMIO、32 路 IRQ
  input、IP2 汇总 output、reset、migration 和诊断接口；`bbk9588.c` 只保留 source
  连线以及板级 sysctrl wake 合并。
- [x] LCD、GPIO、UDC、TCU、DMA、RTC、MSC、SADC、UART 等 source 统一汇入 INTC。
- [x] TCU 实现 8 channel 的 enable/stop、full/half compare、counter、flag、mask、
  set/clear 和三组 parent IRQ。
- [x] TCU 已迁移到独立 `hw/timer/jz4740_tcu.c`，拥有独立 state、MMIO、QEMU
  virtual-clock timer、三路 parent IRQ、事件唤醒 output、reset、migration 和诊断接口；
  machine 只保留 INTC 接线及 TCU1/wake proxy 合并。
- [x] TCU 周期由寄存器和时钟选择推导；`tcu-period-ms` 只保留为诊断/性能采样属性。
- [x] 默认 `bbk9588` firmware patch 列表为空，不再应用 `c200-cp0-*` 或
  `c200-wait-noop`。
- [x] MIPS WAIT 不再被 NOP；QEMU 使用真实 IP2 pending，并通过 Config7.WII 处理
  C200 在 IE 清除窗口进入 WAIT 的情况。

仍需完成：

- [ ] `BBK9588_SYSCTRL_WAKE_PROXY_IRQ` 仍借用 TCU1 parent 表达板级 wake，需要根据
  真机 PM/GPIO 接线确认后替换为真实 source 或明确为板级逻辑。
- [ ] 给所有已建模外设增加“raise -> mask -> ICPR -> clear -> lower”的运行时测试，
  目前很多测试仍是源码契约检查。
- [ ] 继续核对 TCU 时钟门控、PWM、watchdog/OST 差异和极短 compare 的精度。

验收：

- [x] 默认启动不需要 CP0 IRQ、status restore 或 WAIT firmware patch。
- [x] GUI tick、触摸、LCD、DMA、RTC 等设备 pending 能通过 INTC 唤醒 CPU。
- [x] 2026-07-11 独立 INTC 版本通过 Windows QEMU 全量构建和 195 项 Python 测试；
  默认 NAND 冷启动进入主菜单，方向键和触摸事件均被 guest 接收，且只有一个 QEMU
  进程运行。
- [x] 2026-07-11 独立 TCU 版本通过 Windows QEMU 构建、195 项 Python 测试和源树
  检查；默认 NAND 约 46 秒自动校准进入主菜单，主菜单时钟正常递增，方向键产生新帧，
  Web 保持一个 frontend 和一个 QEMU 实例。雷霆战机实际战斗 12 秒新增 233 帧，
  采样约 20.48 fps、1.56 MIPS，随后可正常退出回主菜单。
- [x] 2026-07-14 Web 默认启用 QEMU 自适应 icount，并在该模式下使用 single-thread
  TCG。飞天影音的 MPEG-4/MP3 视频在解码负载下连续 40 秒从 93 帧增长到 968 帧，
  audio DMA completion/rearm 从 219/219 增长到 469/469，underrun 保持 0；不再因
  guest tick 越过播放器判等目标而永久忙等。TCU counter/flag/IRQ 硬件语义未改。
- [ ] 去掉 wake proxy 后，待机、按键唤醒和所有应用计时仍稳定。

### 4. LCD/SLCD 输出：部分完成

已完成：

- [x] 普通 LCD controller 已覆盖 config/timing/control/state/IID/DA/SA/FID/CMD。
- [x] descriptor DMA 会读取 source、frame id、command 和 length，并维护 SOF/EOF、
  disable done、quick disable、underflow mask 和 LCD IRQ。
- [x] 默认路径不再注入 `graphics-done`、LCD ready magic 或相关 machine property。
- [x] frame chardev 只输出 RGB565 帧；WebSocket/PNG 共享同一 QEMU 帧源。
- [x] 前端已有发送 FPS、QEMU frame FPS、guest IPS、丢帧和延迟指标。

仍需完成：

- [x] 删除 `0x804a6b88` guest mirror config、`0xa1f82000` 固定 fallback 和宽松的
  alias framebuffer observer。scanout 只接受 JZ LCD controller 解析出的 descriptor
  source，并正确接受 DMA 使用的 physical/KSEG0/KSEG1 RAM 地址别名。
- [x] trace 确认固件会配置 `0xb3050000` timing/control，写入
  `DA0=0x00477d10`，再由 descriptor 得到 framebuffer `0x01f82000`；
  `0xb0043000` 同期承载 panel command，但不负责 framebuffer 建源。当前 scanout
  不需要另造 SLCD FIFO，详细 panel command/status 语义留作非阻塞板级研究项。
- [x] Web smoke 连续采样 raw RGB565 像素 hash 和逐像素差异；descriptor-only 冷启动
  的 4 个主菜单样本一致，3 组 changed-pixel 均为 `0/76800`。旋转回归同时断言 raw
  WS framebuffer 不变，触摸坐标按可见方向映射。
- [x] LCD controller 已迁移到独立 `hw/display/jz4740_lcd.c`，拥有独立 state、
  MMIO、descriptor DMA、IRQ output、reset、migration 和诊断接口；IRQ 直接接入
  INTC source 30。Windows QEMU C 编译和旁路链接通过，默认 NAND 冷启动实际输出
  240x320 主菜单画面。固定 guest mirror 与 alias observer 已删除。
- [x] BBK `0xb0043000` panel/status 窗口已迁移到独立
  `hw/display/bbk9588_panel.c`，拥有 register、ready/frame-done、W1C、reset 和
  migration。descriptor qtest 覆盖 ready `0x80`、frame-done `0x81` 和清零恢复；
  JZ4740 LCDC 与 host scanout 的职责边界保持不变。
- [x] RGB565 scanout、frame/audio/perf chardev、QEMU console、帧缓存和刷新 timer
  已迁移到无 guest MMIO 的独立 `hw/display/bbk9588_host_bridge.c`。私有 sidecar 从
  528 MiB raw NAND 冷启动收到 2 个非空帧，panel/LCD frame-done qtest 通过。

验收：

- [x] 主菜单、应用和校准界面都能经同一个 frame chardev 显示。
- [x] Web 旋转只改变输出和触摸坐标映射，不修改 guest framebuffer。
- [x] 不读取固定 guest mirror 配置时，主菜单和应用画面仍完整稳定。
- [x] 自动 raw-frame 回归会比较连续帧 hash 和 changed-pixel ratio，当前主菜单样本
  没有随机半图标、噪点或局部补绘。

### 5. SADC/Touch/GPIO 输入：基本完成

已完成：

- [x] Web touch/key 通过 input chardev 进入 QEMU C，不再通过 GDB 写 firmware global。
- [x] SADC 实现 `ADENA/ADCFG/ADCTRL/ADSTATE/ADSAME/ADWAIT/ADTCH/ADBDAT/ADSDAT`。
- [x] `ADTCH` 使用 2-entry FIFO，支持 X/Y、Z 模式、type bit、DTCH/PEND/PENU 和
  FIFO 未读时的背压。
- [x] ADSAME/ADWAIT 延迟按手册 `12 MHz / 128` counter clock 换算。
- [x] GPIO 实现 4 个 port 的 PIN/DAT/IM/PE/FUN/SEL/DIR/TRG/FLG set/clear 语义和
  parent IRQ。
- [x] 触摸与 6 个实体键只通过板级 GPIO/SADC 接线进入固件；无 chardev 时拒绝回退
  到 guest RAM mailbox/global 写入。
- [x] 自动校准只存在于 Web `FrontendState` 测试 harness，不再是 machine property。
- [x] 实际 Web 操作已验证点击持续可用；手动输入和画板连续触摸可工作。
- [x] SADC 已迁移到独立 `hw/input/jz4740_sadc.c`，拥有独立 state、MMIO、
  2-entry FIFO、conversion timer、IRQ output、reset、migration 和诊断接口；IRQ
  直接接入 INTC source 12，machine 只保留 BBK 9588 触摸/GPIO 板级连线。
- [x] GPIO 已迁移到独立 `hw/gpio/jz4740_gpio.c`，拥有独立 4-port register state、
  外部 pin input、FLG latch、4 路 IRQ output、reset、migration 和诊断接口；machine
  只保留 6 个实体键、触摸 pen、NAND ready/busy 和 wake pulse 接线。Windows QEMU
  编译、sidecar 链接及 Web 冷启动、按键、触摸、GUI ring 和帧更新回归通过。

仍需完成：

- [ ] 当前 GPIO pin、active level、触摸 raw calibration、Z1/Z2 和 battery raw 默认值
  来自反汇编/实测近似，需要真机 trace 固化板级说明。
- [ ] 增加 SADC FIFO overrun、DMA mode、不同 XYZ/SPZZ/EX_IN 组合的运行时测试。

验收：

- [x] 校准、菜单点击、按键和触摸 release 通过 SADC/GPIO/INTC 完成。
- [x] 不存在默认 `touch-firmware-globals` 或等价 guest global 写入路径。
- [ ] 用真机采样数据校验 raw X/Y/Z、电池值和 pen IRQ 时序。

### 6. DMAC/MSC/UART/UDC/CIM/RTC/PM：部分完成

已完成：

- [x] DMAC 有 6 channel register、descriptor fetch/link、RAM auto transfer、terminal
  count、doorbell 和 IRQ，覆盖当前 MSC 以及 AIC DRT24/25 FIFO 搬运路径。
- [x] DMAC 已迁移到独立 `hw/dma/jz4740_dmac.c`，拥有独立 state/MMIO、32 路
  request GPIO input、汇总 IRQ output、descriptor/RAM copy、reset 和 migration；
  AIC request 直接连入 DMAC，machine 只保留 MSC bulk、AIC FIFO 和 trace 适配回调。
- [x] MSC 有 reset state、command/argument/response、interrupt 和 DMAC completion 基础模型。
- [x] MSC 与 raw NAND backing 已解耦；默认未挂载 removable medium，不再由 QEMU
  解释 NAND OOB logical block/sequence。
- [x] MSC 已迁移到独立 `hw/sd/jz4740_msc.c` QOM device；device 拥有 register、
  response FIFO、command/DMA pending、`IREG` 写一清零、`IMASK`/IRQ14、reset、
  migration 和诊断状态。machine 只通过 `begin_dma/finish_dma` API 保留 RAM 搬运、
  默认无介质读零/写丢弃策略和 trace。qtest 覆盖 reset/lane access、CMD17/CMD24、
  channel 0/1 completion、response byte order、data-ready、INTC 14 和 IRQ clear。
- [x] UART0 有 16-byte FIFO、DLAB/divisor、IER/IIR/FCR/LCR/LSR、TX/RX chardev 和 IRQ，
  不再依赖 `c200-uart-ready`。
- [x] UART0 已迁移到独立 `hw/char/jz4740_uart.c` QOM device；machine 只连接 serial0、
  `0xb0030000` MMIO 和 INTC source 9。qtest 覆盖 reset LSR、FIFO loopback、RX IRQ、
  DLAB DLL/DLH 和 THR IRQ latch/ack，device 同时拥有独立 reset 与 migration state。
- [x] UDC 在无 USB host 时提供 endpoint、power、enable/pending 和 idle register 语义，
  不再用虚拟时间伪造 frame counter 推动服务循环。
- [x] UDC 已迁移到独立 `hw/usb/jz4740_udc.c` QOM device；machine 只连接
  `0xb3040000` MMIO 和 INTC source 24。qtest 覆盖手册 reset 值、FADDR UPDATE、
  POWER/interrupt mask、`EPINFO=0x23` 以及 EP1/EP3/无效 endpoint indexed bank，
  device 拥有独立 reset 与 migration state。
- [x] 手册确认原匿名 `0xb3060000` 窗口是 JZ4740 CIM，不是 BBK misc register。
  已迁移到独立 `hw/misc/jz4740_cim.c`，实现 9 个 32-bit register、CFG/CR mask、
  RX FIFO empty、disable-done、status W0C、IRQ17、reset 和 migration。9588 无摄像头
  sensor，因此 image sampling、RX FIFO data 和 descriptor DMA 保持未连接 idle 状态；
  qtest 覆盖 reset、mask、RO、DA alignment、VDD/W0C 和 INTC 17。
- [x] RTC 使用 host/QEMU RTC clock，支持 seconds、1 Hz、alarm、IRQ 和 hibernate/reset
  status，不再返回固定日期 magic。
- [x] RTC 已迁移到独立 `hw/rtc/jz4740_rtc.c`，MMIO 映射到 `0xb0003000`，IRQ 直接
  接入 INTC source 15；Windows QEMU 编译、sidecar 链接和默认 NAND 主菜单时间、
  按键、触摸回归通过。
- [x] battery sample 由 SADC PBAT conversion 给出，并可通过 machine property 配置 raw 值。
- [x] graphics/UART/RTC 的固定 ready override machine properties 已移除。

仍需完成：

- [ ] MSC 尚未实现独立可选 SD/MMC block backend、card detect 和真实响应/error 语义。
- [ ] DMAC 尚未覆盖所有外设 request、传输宽度、stride、错误和 descriptor corner case；
  audio request 作为 P0 优先补齐，其余 request 放到 P3。
- [ ] PM/clock gating 只有独立 CPM register、WAIT wake 和 RTC hibernate 基础行为，尚未
  完整建模 suspend、reset cause、module clock gating 和低电压关机。
- [ ] USB device attach/data transfer 未实现；当前 UDC 只满足 no-host idle 路径。

验收：

- [x] 主菜单日期时间来自 RTC 路径，UART 和 UDC 服务循环无需 ready patch。
- [x] 运行期资源读取、文件写入和块回收不依赖 QEMU OOB map。
- [x] 写后 raw NAND 冷启动不再依赖 host canonical checkpoint；QEMU 直接复用同一活动
  raw 文件。细粒度物理掉电语义仍是非阻塞研究项。
- [ ] USB attach、低电压提示和 suspend/resume 都有可重复的运行时测试。

### 7. AIC/I2S/audio codec：P0，部分完成

当前已新增独立 `hw/audio/jz4740_aic.c`，实现 AIC MMIO、internal codec、FIFO、IRQ、
DMAC request 和 QEMU host audio。旧的 audio DMA immediate completion 已删除。C200
反汇编确认播放使用 DMAC channel 3 / DRT 24、16-bit AIC target；2026-07-11 Web 运行
雷霆战机并开启音效后，模型稳定进入 mono `8000 Hz` 播放，TX DMA samples 与 output
frames 按约 8k/s 持续增长，FIFO 稳定在 31。DirectSound 首次打开时 underrun 增长到
约 310 samples 后稳定；WAV backend 同场景从开始播放起保持 underrun/overrun `0/0`。

同次 WAV 端到端采集得到 77.4955 秒、16-bit、双声道 PCM；采样率 8000 Hz，peak
6426、RMS 1025.87、非零样本 95.38%、绝对值不小于 32 的样本 91.58%、clipping 0，
左右声道逐帧相同，符合当前 mono-to-stereo 输出。QEMU WAV backend 在 Windows 退出后
可能保留为 0 的 RIFF/data 长度由 `tests.qemu_audio_wav` 在验证前按实际文件大小修复；
它不修改 PCM payload。该 WAV 证据只覆盖一个游戏场景；音乐和无声后端由下面的
运行时基线覆盖，但系统提示音仍缺独立采集。

音乐应用使用测试 NAND 中的 `A:\飞天音乐\1.mp3` 实测为 stereo `22050 Hz`、
DMAC channel 3 / DRT 24、16-bit sample、每块 1152 个 DMA unit。最初每秒约产生
700~1000 个 underrun；边界诊断显示约 94% 发生在 terminal count 到固件重装 DMA
之间。根因不是 32-entry FIFO 或 mono/stereo 解释错误，而是 QEMU 在单次 1ms audio
timer callback 内跨过 DMA terminal count 后继续批量消费，CPU 无法在同一回调中处理
完成中断。AIC 现在在 TX DMA 块边界结束当前批次，并将剩余帧债务留给下次 timer；
这不扩大 FIFO、不自动重装 DMA，也不丢弃虚拟时间。修复后 DirectSound 连续 20 秒
新增 underrun 为 0；`driver=none` 连续 30 秒输出 645524 帧、完成 1121 个 DMA 块，
新增 underrun 为 0。播放暂停和退出时固件只 mute codec、未清 `AICCR.ERPL`，所以硬件
语义下 `TUR/underrun` 仍会累计；恢复播放稳定后 10 秒没有新增 underrun。
音乐 WAV 回归得到 40.5298 秒、22050 Hz、16-bit 双声道 PCM，peak 14967、RMS
1401.60、有效样本比例 32.78%、clipping 0；35572 帧左右声道不同，符合 stereo 文件
而不是 mono copy。对应 20 秒连续播放窗口新增 underrun 为 0。

2026-07-12 雷霆战机 Web lifecycle smoke 已能从发布 NAND 自动打开游戏音效，并按
截图状态完成设置关闭、首次战斗、Game Over、返回游戏菜单和第二次战斗。绿色回归中
首次 5.005 秒稳定窗口的 DMA/output 各增长 40492 帧，completion/rearm 各增长 40；
第二次战斗 5 秒窗口各增长 40106 帧和 40 个 DMA block。两个窗口新增
underrun/overrun 都为 `0/0`，第二轮没有遗留 DMA completion、rearm 或 IRQ 卡死。

实施：

- [ ] 对系统提示音和外部 codec/功放路径采集 AIC/I2S/DMAC trace，继续确认 BBK
  板级 route；这是硬件真实性研究项，不再阻塞已由用户验收的 Web 音频路径。
- [x] 实现独立 AIC device，包括 config/control/status、TX/RX FIFO、FIFO threshold、
  underrun/overrun、enable/flush 和 AIC IRQ 18。
- [x] 将 audio TX/RX FIFO 接入 DMAC 外设 request，不再在启动 channel 时直接伪造
  terminal count；DMA 节拍应受 FIFO 水位和采样时钟约束。
- [x] internal codec 路径按 `CDCCR2.SMPR` 生成虚拟采样时钟，支持 mono/stereo 和
  8/16/18/20/24-bit sample width；虚拟 FIFO 时序不受 host callback 阻塞。
- [ ] 外部 codec/AC-link 所需的 `I2SCDR`、AIC divider 和外部时钟组合尚未验证。
- [x] 按 C200 实际使用的 `CDCCR1/CDCCR2` 实现 internal codec mute、音量和最小
  headphone output route。
- [ ] 外部功放使能、引脚复用和 BBK 私有板级 route 仍需真机 trace，不能凭空固化。
- [x] 接入 QEMU host audio backend，支持无声/无音频设备环境，不因 host backend
  不可用阻塞或改变 guest 时序。
- [x] AIC 输出按约 20 ms 聚合为 S16LE PCM，经 frame chardev 和独立 WebSocket 推送到
  浏览器；前端使用 AudioContext 低延迟播放，并通过首次触摸/按键解锁 iOS 音频。
- [x] 增加寄存器、FIFO、IRQ、DMA 源码契约测试，并通过 frame-info 包在 Web 展示
  sample rate、FIFO level、DMA samples、output frames、underrun/overrun，以及 DMA
  completion/rearm 次数、间隙、块大小和边界 FIFO 水位。
- [x] 增加 deterministic WAV 端到端音频回归：运行时校验 8k/s DMA/output 增长与
  xrun，采集后校验 WAV sample rate、时长、有效样本、削波和 mono 声道一致性。

验收：

- [x] 音频已由用户在实际 Web 环境验证可用，包含游戏声音和浏览器输出；后续不再
  重复执行音频功能测试，自动回归缺口单独作为可选加固项记录。
- [x] 雷霆战机开启音效后可连续驱动 8000 Hz AIC TX DMA 与 host output，guest 和 CPU
  未卡住；该单项通过不代表上一条综合验收完成。
- [x] 雷霆战机首次战斗结束后可返回游戏菜单并再次进入战斗；两次稳定播放窗口的
  DMA completion/rearm 成对增长，新增 xrun 为 0，guest 未卡住。
- [ ] 音乐暂停/恢复、切换应用和系统提示音重复播放的统一自动回归可继续加固，但不
  作为后续设备拆分的验收门槛。
- [x] 关闭 host audio 或没有音频设备时系统仍能正常启动和操作；`driver=none` 已完成
  主菜单、音乐应用和连续播放 30 秒验收，guest 未卡住且播放期新增 underrun 为 0。
- [x] 删除临时 audio DMA completion 后，音频 DMA/IRQ 契约、游戏运行和现有启动回归
  全部通过（214 项 Python 测试通过；31 项按环境跳过）。

## 结构重构

当前 `bbk9588.c` 约 1560 行。AIC、raw NAND、EMC、MSC、LCD controller、BBK
panel/status、CIM、SADC、GPIO、RTC、INTC、CPM、DMAC、TCU、UART 和 UDC 已迁移到
独立文件；host scanout、frame/audio/perf chardev 也已迁移到独立 host bridge，
host input chardev/parser 已迁移到独立 input bridge，storage/input recorder 已迁移到
独立 diagnostic device，MSC/DMAC/AIC transport 已迁移到独立 DMA bridge。machine
仍持有 IRQ/wake、touch GPIO/SADC 和 BootROM 等真正的板级策略。

- [x] 设备寄存器常量和 helper 已按模块分区，raw NAND 已有独立
  state/MMIO/backing/reset/migration/ops。
- [x] P0 新增的 AIC 直接放入独立设备文件，不再扩大 `bbk9588.c`。
- [ ] 拆分 `hw/mips/bbk9588.c`，只保留 machine、RAM、CPU 和 board wiring。
- [x] 新建 `hw/intc/jz4740_intc.c`，从 machine 迁移 INTC 寄存器、mask/pending、
  IRQ 汇总、reset、migration 和诊断状态。
- [x] 新建 `hw/misc/jz4740_cpm.c`，从通用 MMIO window 迁移 CPCCR/LCR/CPPCR、
  CLKGR/SCR 和各外设 divider；保留未知寄存器存储、访问宽度约束、reset/migration，
  并通过回调将板级 wake 配置变化通知 machine。
- [x] 新建 `hw/misc/jz4740_cim.c`，替换最后一个匿名 generic MMIO window；包含
  idle camera register、FIFO empty、disable-done、IRQ、reset 和 migration。
- [x] 新建 `hw/timer/jz4740_tcu.c`，包含 8-channel counter、full/half compare、
  clock/prescale、flag/mask、parent IRQ、reset、migration 和诊断状态。
- [x] 新建 `hw/mem/jz4740_emc.c` 和 `hw/block/bbk9588_nand.c`，EMC 与 raw NAND
  各自拥有独立 state/MMIO/reset/migration，并通过明确 API 连接 ECC 状态。
- [x] 新建 `hw/display/jz4740_lcd.c`，包含独立 register state、descriptor DMA、
  SOF/EOF IRQ、reset、migration 和 frame-source callback；固定 guest mirror 与
  alias observer 已删除。
- [x] 新建 `hw/display/bbk9588_panel.c`，包含独立 board register、ready/frame-done、
  W1C、reset 和 migration。
- [x] 新建 `hw/input/jz4740_sadc.c`，包含独立 register state、touch FIFO、
  conversion timer、PBAT/SADCIN、IRQ、reset、migration 和 board callback。
- [x] 新建 `hw/gpio/jz4740_gpio.c`，包含独立 4-port register、pin input、flag、
  4 路 IRQ、reset、migration、诊断和 board callback。
- [x] 新建 `hw/audio/jz4740_aic.c`，包含独立 state/MMIO/IRQ/reset/migration。
- [x] 新建 `hw/dma/jz4740_dmac.c`，包含独立 channel/global register、descriptor、
  RAM/AIC/MSC transfer、request input、IRQ output、reset、migration 和音频 DMA 诊断。
- [x] 新建 `hw/rtc/jz4740_rtc.c`，包含独立 seconds/alarm/hibernate register state、
  timer、IRQ、reset、migration 和诊断接口。
- [x] 新建 `hw/char/jz4740_uart.c`，包含独立 register/FIFO/chardev/IRQ/reset/migration。
- [x] 新建 `hw/usb/jz4740_udc.c`，包含独立 no-host register、indexed endpoint、
  interrupt status/enable、IRQ、reset 和 migration；packet transport 仍属 USB 功能缺口。
- [x] 新建 `hw/sd/jz4740_msc.c`，包含独立 register/response FIFO、command/DMA
  pending、IRQ、reset、migration 和诊断接口；可选介质 backend 仍属 MSC 功能缺口。
- [x] 新建 `hw/display/bbk9588_host_bridge.c`，迁移 host scanout、frame/audio/perf
  chardev、QEMU console、帧缓存和刷新 timer；bridge 无 guest MMIO，不混入 LCD/AIC
  register state。
- [x] 新建 `hw/input/bbk9588_host_input.c`，迁移 input chardev、行缓冲和 `T/K`
  协议解析；bridge 只输出 typed key/touch callback，GPIO/SADC 板级接线仍由 machine
  决定。
- [x] 新建 `hw/misc/bbk9588_diag.c`，迁移 input event ring 和
  storage/MSC/NAND-target/DMAC trace sequence/guest RAM recorder；删除全局活动 board
  指针，PC/INTC/DMAC 状态改由 recorder 的显式连接设备源采样。
- [x] 将 touch/progress/graphics trace 状态、设备诊断采样、guest RAM writer 和 panel
  日志 recorder 迁移到 `bbk9588_diag.c`；machine 只保留属性配置、progress timer 和
  板级 wake 快照。
- [x] 新建 `hw/dma/bbk9588_dma_bridge.c`，迁移 MSC kick/command/data、DMAC bulk/AIC
  endpoint 和 diagnostics/trace callback；machine 只创建并连接 bridge。DMAC IRQ
  仍经 board adapter 更新 INTC 并执行 level 重采样，避免 guest 唤醒后中断忙循环。
- [ ] 每个设备使用独立 state、MemoryRegion、IRQ input/output、reset 和迁移状态。

拆文件时不要顺手改变行为；先给当前 MMIO 契约加运行时测试，再做机械迁移。

## Python/Web 收敛

- [x] `default_firmware_patches_for_machine("bbk9588")` 返回空列表；已知 patch 只保留
  给 legacy/malta 诊断路径。
- [x] `bbk9588` 默认路径禁用 Python/GDB storage/resource hook 和 guest global seed。
- [x] frame/input 使用 chardev；Python 负责进程、转发、性能统计和只读状态。
- [x] 自动校准属于 Web smoke test/harness，不进入 SoC machine property。
- [x] 自动校准在 PC/RA 尚未可用时先等待 8 秒启动窗口，避免 QEMU/HMP 连接前提前
  消耗四个校准点；无 PC 环境保留超时 fallback。
- [x] `KNOWN_STALL_REGIONS` 只用作逆向分类和诊断，不参与默认硬件修复。
- [ ] `system.py` 仍保留大量 legacy hook、GDB helper 和旧 patch 定义；确认无发布用户后
  可继续删除，而不是只改成 `legacy_*` 名称。
- [ ] 把关键 C machine runtime probe 从“读源码字符串”升级为真实 MMIO/IRQ 测试。

## 更新后的实施顺序

1. [x] **阶段兼容层：实现 Web/QEMU 持久运行 NAND。** 曾使用 canonical checkpoint
   和隔离 work copy；该兼容层已在第 23 项由唯一活动 raw NAND 取代。
2. [x] 固化冷启动、backup BootROM、主菜单、应用、触摸和帧传输基线。
3. [x] 默认路径移除 firmware patch、Python storage/resource hook 和 QEMU C FAT16。
4. [x] 实现 raw NAND first-stage/backup boot。
5. [x] 实现 INTC/8-channel TCU/WAIT 基础硬件路径。
6. [x] 实现普通 LCD descriptor/state/IRQ 和 frame chardev。
7. [x] 实现 SADC FIFO、GPIO edge/flag/IRQ 和 input chardev。
8. [x] 实现 DMAC/UART/UDC/RTC/MSC 的当前基础路径。
9. [x] 实现独立 AIC、audio DMA request、最小 internal codec 和 host audio 输出，
   删除临时 audio DMA completion，并完成雷霆战机 DirectSound/WAV 端到端回归。
10. [x] 完成当前音频验收：独立 AIC、DMA、host/Web 输出和无音频后端已实现，
    雷霆战机重复进入自动回归通过，用户已在实际 Web 环境确认声音正常。系统提示音
    和外部 codec/功放 trace 留作非阻塞研究项，不再重复执行音频功能测试。
11. [x] 将 INTC 迁移为独立 QOM device，并保持默认 NAND、输入和音频路径可用。
12. [x] 将 CPM/sysctrl 迁移为独立 QOM device；Windows QEMU 构建、195 项测试、
    默认 NAND 主菜单及按键/触摸回归均通过。
13. [x] 将 DMAC 迁移为独立 QOM device；Windows QEMU 构建和 195 项测试通过。
    默认 NAND 可进入主菜单；雷霆战机 8000 Hz 播放稳定窗口 10 秒内 DMA/output
    各增长 80835 帧、completion/rearm 各增长 81、新增 underrun 为 0。
14. [x] 将 TCU 迁移为独立 QOM device；Windows QEMU 构建、195 项测试、默认 NAND
    冷启动、主菜单时钟、方向键帧更新和雷霆战机实际战斗均通过。
15. [x] 继续按设备拆分 `bbk9588.c`；LCD controller、SADC、GPIO、RTC、EMC 和
    raw NAND 已迁移。EMC/NAND 版本通过 Windows 编译、sidecar 链接、默认 NAND
    冷启动、主菜单画面、按键/触摸以及独立 erase/program/readback/backing 写回回归。
    UART、UDC 和 MSC 随后也已迁入独立 QOM device 并通过 MMIO qtest；下一轮结构拆分
    处理 LCD panel/frame chardev。UDC packet transport 仍按 USB 功能项处理。
16. [x] 用运行期 trace 找到 C200 raw NAND FTL 边界并删除 MSC OOB LBA map；移除后
    主菜单、应用资源以及 1950 read / 192 program / 3 erase 的固件写入路径通过。
17. [x] 删除固定 guest LCD mirror、固定 framebuffer fallback 和 alias observer；
    修正 descriptor DMA physical address 被 machine 错拒的问题。trace 证明固件原生
    配置 JZ LCD `DA0=0x00477d10` 并解析到 framebuffer `0x01f82000`，无需为 scanout
    另造 SLCD FIFO；descriptor-only 冷启动、主菜单、按键、触摸、返回和 4 帧稳定性
    Web smoke 通过。
18. [x] 将 MSC register/response/pending/IRQ/reset/migration 迁移到独立 QOM device，
    machine 只保留 DMAC RAM transport、默认无介质策略和 trace。Windows 对象编译、
    sidecar 链接、CMD17/CMD24 qtest 以及 raw NAND 私有冷启动非空帧回归通过。
19. [x] 将 BBK `0xb0043000` panel/status register、ready/frame-done、W1C、reset 和
    migration 迁移到独立 QOM device。descriptor MMIO qtest、Windows sidecar 链接和
    raw NAND 私有冷启动非空帧回归通过；host frame bridge 继续保留为板级输出。
20. [x] 按手册将匿名 `0xb3060000` window 识别并替换为独立 JZ4740 CIM idle device，
    删除 machine 中最后一套 generic MMIO state。Windows 编译、sidecar、CIM MMIO/IRQ
    qtest 和 raw NAND 私有冷启动非空帧回归通过。
21. [x] 实现 NAND Hamming/RS ECC data path、parity/status/error index/mask 和 EMC IRQ；
    BootROM 对 2 KiB page 执行真实 RS 纠错及 normal/backup fallback。镜像生成同时适配
    boot OOB `+6` 与 U-Boot 数据 OOB `+4`，私有 Web 冷启动进入主菜单。
22. [x] checkpoint 压实在保留 canonical FTL tag 的同时，为变化 data page 重新生成
    `4+9*n` RS parity；还原固件 cold-scan/环形 sequence 规则，修正 C200 16-bit logical
    tag 的高半字，并加入旧 checkpoint 原子迁移、严格审计和 tail 掉电注入工具。
23. [x] 将 Web persistent NAND 收敛为唯一活动 raw NAND：删除 canonical checkpoint、
    persistent work copy 和正常停止压实流程；异常退出后直接复用同一文件，文件管理和
    显式导入也围绕该文件工作。测试 fixture 强杀 QEMU 后用同一 528 MiB 文件再次冷启动，
    两次均收到非空帧；强杀 Web 后 Job Object 自动结束对应 QEMU，再次启动同一 NAND
    同样收到非空帧。严格审计为 3963 mapped、0 anomaly。代码已完全删除 disposable
    work-copy 路径，测试自行管理临时 fixture。
24. [x] 将 host scanout、frame/audio/perf chardev、console 和 refresh timer 从
    machine 迁移到独立无 MMIO host bridge；Windows 对象编译、旁路链接、panel qtest
    和 raw NAND 冷启动 2 帧回归通过。
25. [x] 将 input chardev、行缓冲和 `T/K` 协议解析迁移到独立无 MMIO host input
    bridge。sidecar 冷启动后 key/touch down/up 共 4 次均成功进入 chardev，QEMU
    保持运行并新增帧，input/frame error 均为空。
26. [x] 将 input ring 和 storage/MSC/NAND-target/DMAC recorder 迁移到独立无 guest
    MMIO 的 `bbk9588-diag`，删除全局活动 board 指针。Windows 对象编译、sidecar 链接、
    panel qtest 和 224 项非音频回归通过；标准 raw NAND 默认 trace 关闭时 3.5 秒输出
    3 帧，key/touch 4 次输入均成功。开启 trace 后诊断 RAM 实测 storage sequence
    `0x7f6`、input ring count 2，证明 recorder 实际写入。
27. [x] 将 touch/progress/graphics trace 的状态、序号、设备诊断采样和 guest RAM/log
    recorder 收敛到 `bbk9588-diag`；machine 只传入显式设备源和板级 wake 快照，默认
    trace 关闭时 guest 行为不变。
28. [x] 将 MSC command/data、DMAC bulk/AIC endpoint 和 diagnostics callback 迁入独立
    无 guest MMIO 的 `bbk9588-dma-bridge`，machine 不再持有 peripheral ops。DMAC IRQ
    保留 board adapter；直接连接 INTC GPIO 会在输入唤醒后留下持续 pending，实测造成
    单核 100% 中断忙循环。Windows QEMU 构建、226 项系统测试和单一活动 raw NAND Web
    冷启动、按键、触摸回归通过。
29. [ ] 非阻塞研究项：继续复现 FTL sequence/valid-page/回收/提交顺序和完整故障矩阵。
    正常 10-block remap raw 重启、单-block pre-commit 回退及 raw NAND FAIL qtest 已通过；
    仍缺多-block 提交边界、垃圾回收、sequence wrap 和物理故障下的 guest 恢复。
30. [ ] 完成 PM、USB、剩余 DMA request/corner case 和旧诊断代码清理。

## 关键验收清单

- [x] 冷启动：只提供 NAND 镜像即可 BootROM -> loader/U-Boot -> C200。
- [x] BootROM：normal area 失效后会尝试 `0x2000` backup area。
- [x] 存储层：QEMU C 不解析 FAT boot sector、目录项、cluster 或资源文件。
- [x] raw 写入：page program/erase/OOB 修改会写入调用方显式提供的 raw NAND fixture。
- [x] raw 故障：可按 physical block 注入 program/erase FAIL；状态为 `0x41`，失败操作
  不改变 raw backing，后续成功操作回到 `0x40`。
- [x] 持久写入：早期 checkpoint 路径和当前唯一活动 NAND 路径均完成跨冷启动验证；
  当前正常停止、QEMU 崩溃和 Web 强杀都不执行 host 提交或镜像替换。
- [x] FTL：资源读取和文件写入不再依赖 `msc_oob_lba_*`；C200 直接执行 raw NAND
  data/OOB read、page program 和 block erase。
- [x] FTL 正常写入恢复：名片文件写入触发 10 个 logical remap，未经 checkpoint 的
  raw work 冷启动后文件仍存在。
- [x] ECC：EMC Hamming/RS、NFINTS/NFERR/IRQ、BootROM 纠错/backup fallback 和
  boot/data 双 OOB parity 布局均有自动或私有运行回归。
- [ ] FTL 掉电恢复：单 logical remap 的“旧块保留、新尾标签 torn”已验证回退并由
  固件清理；仍需覆盖多 logical transaction、垃圾回收及 program/erase 各提交阶段。
- [x] 单一活动 NAND：persistent Web/QEMU 不再创建 work copy/checkpoint；应用写入后
  确认保存并回到空闲状态，再强杀 QEMU 或 Web；再次启动同一 raw NAND 仍能进入系统、
  保留文件并通过严格 FTL/ECC 审计。若在写命令中途退出，最低要求是镜像仍可启动，
  最近一次文件修改允许按固件提交边界保留或回滚。
- [x] 性能：默认 OOB scan 可在可用时间内进入主菜单，前端可观察 FPS/IPS/延迟。
- [x] 显示：有自动 raw-frame 稳定回归，并且不依赖固定 guest mirror/alias 地址。
- [x] 输入：触摸、按键和 release 通过 SADC/GPIO/INTC，不卡在 guest global hook。
- [x] 时间：主菜单时间来自 RTC host clock 路径。
- [x] 音频：真实 AIC FIFO、DMA request 和 host/Web audio 输出已由用户实际验收；
  外部板级 route trace 不作为当前发布阻塞项。
- [ ] 电源：低电压、hibernate、wake 有端到端测试。
- [x] 发布：release 工具只打包运行必需文件，不再携带旧 QEMU patch 安装路径。
- [ ] 结构：主要 JZ4740 设备已从 `bbk9588.c` 拆分。

## 风险与注意点

- JZ4740 手册是 SoC 级参考，BBK 9588 的板级接线和私有 FTL 仍要靠反汇编、
  真机 dump 和 trace 确认。
- 手册对 internal Boot ROM 物理容量有 8 KiB/4 KiB 的自相矛盾描述；NAND loader
  copy limit 则应按 BootROM 章节的 8 KiB 实现。
- BootROM/first-stage 与 U-Boot 常规 NAND driver 使用不同 OOB ECC 起点，分别是
  `6+9*n` 和 `4+9*n`。镜像迁移不能再无差别覆盖全盘同一 OOB range；标准 9588 镜像
  以 page `0x200` 为布局边界，其他 boot layout 必须显式提供边界。
- C200 raw NAND trace 已证明运行期 FTL 由固件持有，因此 MSC OOB map 已删除。单一活动
  NAND 可先按默认无坏块的虚拟介质落地；不能据此宣称私有 FTL 完整，sequence/valid-page
  提交、坏块、垃圾回收和细粒度掉电恢复仍应继续用反汇编、真机 dump/trace 与无
  checkpoint 冷启动回归确认，但不阻塞普通模拟器持久化。
- 音频 codec、功放使能、mute 和引脚复用属于板级信息；JZ4740 手册只能确定 AIC/DMAC
  契约，最终接线和初始化序列仍需结合固件反汇编与运行 trace。
- LCD 噪点要同时检查 descriptor、cache flush、DMA、资源加载和 frame push，不能只
  在 Web 前端丢帧或延时掩盖。
- 诊断 trace 可以保留，但关闭 trace 后 guest 行为必须不变。
