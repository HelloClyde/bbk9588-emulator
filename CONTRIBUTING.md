# 贡献说明

这个仓库混合了硬件实测、静态反汇编、自动探针和仍在验证的推断。提交内容时请把证据等级写清楚：

- `confirmed`：真实硬件上观察到的行为。
- `static`：来自反汇编、字符串、表结构或二进制格式分析。
- `probe`：有生成的测试 BDA 或自动探针，但边界仍需复核。
- `guess`：临时假设，不应作为公开 API 名称或稳定结论。

## 代码要求

- Python 代码使用 3.11+，保持类型标注和明确的错误信息。
- 避免把硬件行为写成系统级固件 hook；模拟器默认路径应推进 QEMU SoC/设备模型。
- 不要恢复旧 Unicorn/Python/GDB fastpath 服务作为默认能力。
- 修改 `qemu/overlay/` 后，同步更新 patch 或说明其来源。
- 发布包入口应保持在根目录 `start-web.cmd` / `start-web.ps1`，下载用户不需要理解内部目录。

## 文档要求

- 用户文档可以使用中文。
- 反汇编结论要注明地址、调用链或报告来源。
- 面向 release 用户的文档要区分“公开包包含什么”和“用户需要自行提供什么 dump”。

## 禁止提交的数据

不要把原始固件、NAND 镜像、应用 BDA、DBA、DLX、字典库、音频、图片资源、生成的
BDA/DLX、工具链压缩包或本地构建目录提交到 Git 历史。详见 [DATA_NOTICE.md](DATA_NOTICE.md)。

## 提交前检查

```powershell
python -m unittest tests.test_qemu_system
git diff --check
git status --short
```

如果改了 release 打包逻辑，还需要运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\tools\package_emulator.ps1 -Version emu-local
python .\tools\validate_release_package.py .\build\dist\bbk9588-emulator-emu-local.zip --runtime
```
