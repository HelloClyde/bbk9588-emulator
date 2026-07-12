# Changelog

本项目的重要版本变更记录在此。模拟器版本遵循语义化版本号；NAND 镜像使用独立的
`nand-v*` 版本线。

## [Unreleased]

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
