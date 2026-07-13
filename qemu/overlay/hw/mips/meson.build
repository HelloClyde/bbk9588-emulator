mips_ss = ss.source_set()
mips_ss.add(files('bootloader.c', 'mips_int.c'))
common_ss.add(when: 'CONFIG_FW_CFG_MIPS', if_true: files('fw_cfg.c'))
common_ss.add(when: 'CONFIG_LOONGSON3V', if_true: files('loongson3_bootp.c'))
mips_ss.add(when: 'CONFIG_LOONGSON3V', if_true: files('loongson3_virt.c'))
mips_ss.add(when: 'CONFIG_MALTA', if_true: files('malta.c'))
mips_ss.add(when: 'CONFIG_BBK9588', if_true: files(
  'bbk9588.c',
  '../block/bbk9588_nand.c',
  '../char/jz4740_uart.c',
  '../display/bbk9588_host_bridge.c',
  '../display/bbk9588_panel.c',
  '../display/jz4740_lcd.c',
  '../dma/jz4740_dmac.c',
  '../gpio/jz4740_gpio.c',
  '../input/bbk9588_host_input.c',
  '../input/jz4740_sadc.c',
  '../intc/jz4740_intc.c',
  '../mem/jz4740_ecc.c',
  '../mem/jz4740_emc.c',
  '../misc/jz4740_cim.c',
  '../misc/jz4740_cpm.c',
  '../rtc/jz4740_rtc.c',
  '../sd/jz4740_msc.c',
  '../timer/jz4740_tcu.c',
  '../usb/jz4740_udc.c',
))
mips_ss.add(when: 'CONFIG_MIPS_CPS', if_true: files('cps.c'))

if 'CONFIG_TCG' in config_all_accel
mips_ss.add(when: 'CONFIG_JAZZ', if_true: files('jazz.c'))
mips_ss.add(when: 'CONFIG_FULOONG', if_true: files('fuloong2e.c'))
mips_ss.add(when: 'CONFIG_MIPS_BOSTON', if_true: files('boston.c'))
endif

hw_arch += {'mips': mips_ss}
