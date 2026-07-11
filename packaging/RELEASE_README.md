# BBK 9588 模拟器 Runtime 包

这是 Windows 版 BBK 9588 QEMU system emulator 的下载运行包。

## 包内包含

- `bin/bbk9588-qemu-system-mipsel.exe` 和运行所需 DLL。
- 根目录启动脚本：`start-web.cmd`、`start-web.ps1`。
- Web 前端：`emu/web/`。
- 镜像构建与运行工具：`tools/`。
- GitHub Actions 构建时会附带 `python/` runtime。

## 启动

双击：

```text
start-web.cmd
```

或在 PowerShell 中运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-web.ps1
```

浏览器入口：

```text
http://127.0.0.1:8000/
```

Web 固定读取默认 NAND 镜像；界面只提供恢复基础镜像操作，不再切换本机路径。
右侧“文件”标签支持新建目录、导入、导出、改名和删除，可用于安装 `.bda` 应用。
写操作期间模拟器会自动停止并重启。

## 必需的本地数据

标准模拟器 ZIP 不内嵌固件、NAND 镜像、BDA 应用或商业资源。项目可以把经过授权的
NAND 作为同仓库的独立 Release asset 发布。

默认启动路径由 QEMU BootROM 从 NAND address `0` 按 JZ4740 spare valid flag 读取
`loader_9588_4740.bin`，入口为 `0x80000004`，
再由 loader/U-Boot 通过 FAT/FTL 读取 `系统/数据/kj409588.bin` 并进入系统固件。
运行时只需要：

- `runtime/bbk9588_nand.bin`

如果还没有 runtime NAND 镜像，可以把 `bbk9588_nand.bin` 或只包含一个 `.bin` 的
`bbk9588_nand*.zip` 放到 `start-web.cmd` 同级目录。首次启动会自动导入；也可以在弹出的
文件选择窗口中选择镜像，或显式运行：

```powershell
.\start-web.cmd -Nand D:\dumps\bbk9588_nand.bin
```

如果只有拆出的系统资源，也可以放到包根目录，由启动脚本构建 NAND：

```text
系统/
  数据/
    loader_9588_4740.bin
    u_boot_9588_4740.bin
    kj409588.bin
应用/
```

`C200.bin` 只用于 `-BootMode c200` direct-boot 兼容诊断模式。

生成的 FAT 卷匹配真机规格：495 MB、FAT16、16 KiB allocation unit。

生成结果：

```text
runtime/bbk9588_nand.bin
```

强制重建镜像：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-web.ps1 -RebuildImages
```

修改端口或追加前端参数：

```powershell
powershell -ExecutionPolicy Bypass -File .\start-web.ps1 -Port 8010 --orientation rot180
```

前端输入校准 helper 默认关闭；它只用于 Web smoke test。需要复现实验校准流程时，
可追加 `--frontend-input-calibration`。

## 故障排查

- 提示缺少 NAND 镜像：确认 `系统/数据/loader_9588_4740.bin`、`系统/数据/u_boot_9588_4740.bin`、`系统/数据/kj409588.bin` 和 `应用/` 已放在包根目录，或手动放入预构建 NAND。
- 端口被占用：使用 `-Port 8010` 换端口。
- 不想自动打开浏览器：加 `-NoOpenBrowser`。
