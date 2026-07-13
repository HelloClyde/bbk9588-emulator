# QEMU `bbk9588` Machine

模拟器使用自定义 QEMU `bbk9588` MIPS system machine。仓库不包含完整 QEMU 源码，只保存：

```text
qemu/overlay/              完整修改后的覆盖文件
```

目标 QEMU 版本：

```text
QEMU 11.0.0
```

## 安装 Overlay

把 overlay 复制进一个干净 QEMU checkout：

```powershell
python .\qemu\scripts\install_qemu_overlay.py --qemu-source E:\qemu-src
```

检查 checkout 是否已经匹配 overlay：

```powershell
python .\qemu\scripts\install_qemu_overlay.py --qemu-source E:\qemu-src --check
```

## Windows 构建

安装 MSYS2 UCRT64 依赖后运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\qemu\scripts\build_qemu_windows.ps1 `
  -QemuSource E:\qemu-src `
  -BuildDir E:\qemu-src\build-bbk9588-win `
  -UseOverlay
```

脚本默认先安装 overlay；`-UseOverlay` 只是兼容旧命令。脚本会在缺少 `build.ninja`
时运行 QEMU configure，然后构建：

```text
E:\qemu-src\build-bbk9588-win\qemu-system-mipsel.exe
```

Release workflow 会把它复制并改名为：

```text
bin/bbk9588-qemu-system-mipsel.exe
```

## 前端集成

开发环境启动：

```powershell
python -m emu.web.frontend `
  --boot-mode nand `
  --nand-image .\runtime\bbk9588_nand.bin `
  --qemu E:\qemu-src\build-bbk9588-win\qemu-system-mipsel.exe
```

release 包启动脚本会显式传入：

```text
bin/bbk9588-qemu-system-mipsel.exe
```

## 模型范围

`hw/mips/bbk9588.c` 当前负责：

- RAM 与 BootROM/NAND boot image 加载。默认 `nand` 路径由 QEMU BootROM 从 NAND
  address `0` 按 spare valid flag 读取最多 8 KiB first-stage loader，并跳到
  `0x80000004`；loader/U-Boot 再通过 FAT/FTL 读取
  `系统/数据/kj409588.bin`。无镜像的 `uboot` 启动模式也走同一 raw
  first-stage 路径；从 NAND page `0x40` 直接复制 U-Boot 只保留为显式
  `bootrom-page`/`bootrom-size`/load-address 诊断 raw copy 模式，并且不再解析
  `BBKUBOOT` 模拟器私有 header。BootROM 不再提供从 NAND FAT 直接加载 C200
  的 `bootrom-fat-kernel` 兼容入口。
- JZ4740 LCDC 寄存器窗口 `0xb3050000` 的基础状态/descriptor DMA
  模型，`LCDCMDx.LEN` 完成消耗、下一 descriptor 装载，`LCDSTATE`
  SOF/EOF/disable 状态到 INTC bit 30 的连线，以及 RGB565 frame chardev
  输出。`0xb0043000` 仍保留为当前 C200 路径使用的 BBK status 窗口；
  该窗口已迁移到独立 `hw/display/bbk9588_panel.c`，提供 board register、
  ready/frame-done、W1C、reset 和 migration。默认启动不再注入
  graphics-done/LCD-ready magic，也不再暴露对应的 machine ready override。
- input chardev。
- raw NAND data/OOB 访问和独立 MSC DMA 控制器行为；C200 自己扫描 raw NAND OOB、
  建立 FTL map 并执行 page program/block erase。`hw/sd/jz4740_msc.c` 负责
  `0xb0021000` register、response FIFO、command/DMA pending、`IREG` 写一清零、
  `IMASK`/IRQ14、reset 和 migration；machine 只保留 DMAC RAM 搬运和 storage trace。
  MSC 已与 NAND backing 解耦，默认表示未挂载独立 removable medium，不再按 OOB tag
  把 MSC LBA 翻译到 NAND page。
  NAND backing 的 page stride 只按 2048B data +
  64B OOB raw geometry 或 legacy 2048B page-only 兼容格式识别，raw NAND
  program/erase 不再带构造镜像 FAT 页范围保护。旧的 QEMU C FAT16
  boot-sector 扫描和 FAT/cluster bridge 已移除；FAT/资源逻辑由
  U-Boot/C200 经 modeled raw NAND 路径执行。后续若需要模拟 SD/MMC，必须给 MSC
  接独立 block backend，不能复用 NAND OOB FTL。
  测试可用 `-global bbk9588-nand.fail-program-block=N` 或
  `-global bbk9588-nand.fail-erase-block=N` 让指定 physical block 的操作返回
  ready+FAIL `0x41` 且保持 backing 不变；默认值禁用故障注入。
- Web/QEMU 直接读写调用方传入的唯一活动 raw NAND，drive 使用 writethrough；正常停止、
  QEMU 崩溃或 Web 重启都不触发 host FTL 压实，也不创建或删除 work copy/checkpoint。
  测试自行创建临时 NAND fixture，direct-boot probe 默认不挂载 NAND。构造 OOB logical
  tag 只写低 16 位，高 16 位保持 `0xffff`，与 C200 page program 一致。旧版本 checkpoint
  仅在升级后的首次启动原子迁移到活动 NAND，迁移后删除。Windows 前端通过
  kill-on-close Job Object 保证 Web 被强杀时同步结束 QEMU，避免孤儿进程继续写活动镜像。
- Web 文件管理在 QEMU 停止时直接修改活动 NAND，并对变化 data page 重新生成数据区
  OOB `4+9*n` RS parity。恢复/更换镜像通过启动器显式导入 `.bin` 或单镜像 ZIP，不保留
  隐藏基础副本。
- DMAC 基础 channel 模型：`0xb3020000` 按 JZ4740
  `DSA/DTA/DTC/DRT/DCS/DCM/DDA/DMAC/DIRQP/DDR` 组织 channel
  register，MSC 读写通过 channel enable + global `DMAE` 完成并置
  terminal count / `DIRQP`，不再靠读取 `DTC` 触发 DMA 完成。
- 独立 JZ4740 AIC/internal codec 模型：`0xb0020000` 提供 AIC config/control/status、
  32-sample TX/RX FIFO、threshold/underrun/overrun、IRQ18，并通过 DMAC DRT24/25
  搬运真实样本；internal codec sample rate、mute/volume/route 接入 QEMU audio
  backend。音频虚拟时钟不依赖 host callback，frame-info 诊断包输出采样率、FIFO、
  DMA samples、output frames 和 xrun。MSC 独立位于 `0xb0021000`，不再与 AIC 重叠。
- 独立 UART0 基础 16550/JZ4740 模型 `hw/char/jz4740_uart.c`：`0xb0030000` 按
  `URBR/UTHR/UDLLR/UDLHR/UIER/UIIR/UFCR/ULCR/UMCR/ULSR/UMSR/USPR/ISR`
  组织 8-bit register slot，支持 `DLAB`、16 字节 RX FIFO、FIFO reset、
  `UIIR` pending、`ULSR` line status 和 serial chardev 输出。
- 独立 UDC no-host idle 模型 `hw/usb/jz4740_udc.c`：`0xb3040000` 按
  JZ4740 common register reset
  value 提供 `FAddr/Power/IntrIn/IntrOut/IntrInE/IntrOutE/IntrUSB/IntrUSBE`
  等寄存器、indexed endpoint 配置寄存器和 IRQ24 连线；无 USB host 时
  FIFO/count/status 保持空闲，不回显 guest shadow register；不再提供
  `irq24-period-ms` 合成中断源。
- 独立 JZ4740 CIM idle 模型 `hw/misc/jz4740_cim.c`：将原匿名 `0xb3060000`
  shadow window 按手册还原为 `CIMCFG/CIMCR/CIMST/CIMIID/CIMRXFIFO/CIMDA/`
  `CIMFA/CIMFID/CIMCMD`，支持 RW mask、FIFO empty、disable-done、status W0C、
  IRQ17、reset 和 migration。9588 未连接 camera sensor，不合成图像或 CIM DMA。
- INTC/TCU 基本寄存器模型：JZ4740 `ICSR/ICMR/ICMSR/ICMCR/ICPR`
  与 TCU enable/flag/mask set-clear 语义、reset mask，以及 byte/halfword
  MMIO lane 访问；`tcu-period-ms` 仅作为显式诊断/调速 machine property，
  默认启动命令不再注入该选项。
- SYSCTRL 基础 reset state：`0xb0000000` 的 clock control register 通过
  寄存器 reset 值提供 C200 延迟校准所需的 divider，不再在读路径临时补
  固定值。
- GPIO/SADC 输入模型：GPIO 板级电平/flag 仍保留，SADC 已按 JZ4740
  `ADENA/ADCFG/ADCTRL/ADSTATE/ADSAME/ADWAIT/ADTCH/ADBDAT/ADSDAT`
  寄存器和 ADTCH FIFO 提供触摸/电池采样事件；`ADENA.PBATEN/SADCINEN`
  会装载 `sadc-battery-raw`/`sadc-sadcin-raw` 到 12-bit 数据寄存器、
  置 `DRDY/SRDY` 并按硬件语义自动清 enable 位。
- RTC 基础寄存器模型：`0xb0003000` 已按 JZ4740
  `RTCCR/RTCSR/RTCSAR/RTCGR/HCR/HWFCR/HRCR/HWCR/HWRSR/HSPR`
  提供秒计数、闹钟、hibernate wake/status 和 scratch pattern；`HCR.PD`
  置位后除 `RTCCR.1HZ/1HZIE` 外 RTC/hibernate 写入保持冻结；`1HZIE/AIE`
  通过 INTC bit 15 输出，`HWCR.EALM` alarm 命中会置 `HWRSR.ALM` 并退出
  hibernate 状态。
- 兼容性诊断寄存器和 machine property。

overlay 还包含少量 `target/mips` 侧 instrumentation/helper，用于当前 machine model
和诊断；filesystem/resource probe 写入诊断 RAM 前受 `storage-trace=on` 门控，
默认启动不会因此改写 guest 内存。周期性 progress 采样使用
`progress-trace-period-ms` 显式诊断 property，不再以旧的资源泵命名。
Python/GDB 侧遗留的 resource hook、filesystem scan、file-open probe 和 GUI
dispatcher 诊断服务在 `bbk9588` machine 上统一返回 disabled；默认路径不再通过
Python 写 guest RAM、调用 firmware helper，或把 backing FAT 结果回填到 guest。

## 音频 WAV 回归

需要不受声卡和 DirectSound 调度影响地检查 guest 音频时，可让 Web 直接启动 QEMU
WAV backend：

```powershell
python -m emu.web.frontend `
  --host 127.0.0.1 --port 8001 --boot-mode nand `
  --frontend-input-calibration `
  --qemu E:\qemu-src\build-bbk9588-win\qemu-system-mipsel.exe `
  --qemu-machine-option audiodev=wavcap `
  --qemu-extra-arg=-audiodev `
  --qemu-extra-arg=driver=wav,id=wavcap,path=build/audio-regression/capture.wav,out.frequency=8000,out.channels=2,out.format=s16
```

在浏览器中进入待测应用并播放声音，随后通过 Web API 停止 QEMU，使退出和音频后端
关闭按正常顺序执行：

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8001/api/stop
```

最后验证采集内容：

```powershell
python -m tests.qemu_audio_wav `
  .\build\audio-regression\capture.wav `
  --expected-rate 8000 --min-duration 10
```

QEMU WAV backend 在 Windows 上可能留下值为 0 的 RIFF/data 长度。验证工具只按文件
实际大小补齐这两个字段，然后检查 PCM 采样率、时长、峰值、有效样本比例、削波和
mono-to-stereo 一致性；不会修改音频样本。
