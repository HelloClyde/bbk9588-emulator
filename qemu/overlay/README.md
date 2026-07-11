# QEMU Source Overlay

This directory contains the full QEMU source files that differ from upstream
QEMU for the BBK9588 emulator.

It is intentionally an overlay, not a vendored full QEMU checkout:

- `hw/mips/bbk9588.c` is the custom board/machine model.
- The other files are the small QEMU MIPS/Kconfig/build-system changes needed
  by the current emulator backend and diagnostics.

The default `bbk9588` storage path is raw NAND plus modeled device behavior.
Do not add QEMU C runtime helpers that understand FAT directory entries,
clusters, or boot-sector layout; U-Boot/C200 should discover those through
modeled NAND/MSC behavior.
INTC/TCU changes should keep matching the JZ4740 register contract, including
reset values, read-only/write-only command registers, and byte/halfword MMIO
lane behavior.
The JZ4740 LCDC window lives at `0xb3050000`; keep its LCDSTATE/LCDDAx
descriptor behavior separate from the older BBK `0xb0043000` ready/status
compatibility window. LCDC SOF/EOF/disable status should raise the JZ4740 LCD
INTC source bit 30 only when the matching LCDCTRL interrupt-enable bit is set.
Frame completion should also consume `LCDCMDx.LEN` and load the next descriptor
through `LCDDAx`, matching the hardware DMA lifecycle rather than treating the
framebuffer address as a permanent global.

Install this overlay into a QEMU checkout with:

```powershell
python .\emu\qemu\scripts\install_qemu_overlay.py --qemu-source E:\qemu-src
```
