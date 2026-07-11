# 模拟器开发规范

## 代码边界

- `emu/web/`：前端服务、WebSocket、状态聚合。
- `emu/qemu/system.py`：QEMU 进程编排和诊断读取。
- `tools/`：离线镜像、打包和校验工具。
- `qemu/overlay/`：真实 QEMU C 设备模型。

不要把硬件行为长期放在 Python/GDB hook 里。默认运行路径应通过 QEMU system emulation
模拟硬件，让固件原始逻辑自己执行。

## 命名约定

- 发布包中的 QEMU 可执行文件使用 `bbk9588-qemu-system-mipsel.exe`。
- 根启动脚本固定为 `start-web.cmd` 和 `start-web.ps1`。
- 本地 dump 目录固定为 `系统/` 与 `应用/`。
- 生成物统一进入 `build/`。

## 本地检查

```powershell
python -m py_compile (Get-ChildItem emu -Filter *.py -Recurse).FullName
git diff --check
```

如果安装了 ruff：

```powershell
python -m ruff check emu reverse scripts
```

## Release 包检查

本地结构检查可以用 fake runtime 或真实 workflow 产物运行：

```powershell
python .\tools\validate_release_package.py .\build\dist\bbk9588-emulator-版本.zip --runtime
```

校验内容包括：

- 根目录启动脚本存在。
- `emu/web/` 前端存在。
- QEMU 可执行文件使用 bbk9588 专用名称。
- 固件、BDA、DLX、NAND、完整 QEMU overlay、测试目录不进入 runtime 包。

## 文档要求

用户文档优先说明可操作步骤。涉及逆向结论时，需要标明来源：硬件实测、反汇编、探针
或临时假设。
