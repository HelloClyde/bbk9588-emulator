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
framebuffer，触摸和按键事件通过前端 API 送入后端。

`emu/web/frontend_state.py` 负责：

- 管理长生命周期 QEMU 进程。
- 接收 frame chardev 数据并推送给浏览器。
- 把触摸/按键转换成 QEMU input chardev 事件。
- 输出诊断状态。

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
- `build_runtime_images.ps1`

这些工具只消费本地 `系统/`、`应用/`，不向仓库写入可提交数据。

## QEMU 设备模型

`qemu/overlay/` 是对 QEMU 11.0.0 的覆盖源码。release workflow 会下载官方
QEMU 源码，复制 overlay，编译 `mipsel-softmmu`，再收集 Windows runtime DLL。

设备模型优先级：

1. 按真实 SoC/MMIO 行为补设备寄存器。
2. 让固件自然走原有逻辑。
3. 仅保留必要诊断开关，避免默认路径依赖系统级 hook。
