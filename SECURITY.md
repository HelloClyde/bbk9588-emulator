# Security Policy

## Reporting

请通过 GitHub Security Advisory 的私有报告入口提交安全问题。不要在公开 issue 中
上传 NAND、固件、设备 dump、访问令牌或其它私有数据。

报告应尽量包含受影响版本、复现步骤、预期影响和最小化日志。维护者确认问题后会在
修复可用时发布说明。

## Scope

本项目仅在本机启动 HTTP/WebSocket 服务，默认绑定 `127.0.0.1`。不要把 Web 端口直接
暴露到不可信网络；当前前端不是面向公网部署的多用户服务。
