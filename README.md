# Google Pixel 3a (`sargo`)

| | |
| --- | --- |
| Device | Google Pixel 3a (`sargo`) |
| SoC | Qualcomm Snapdragon 670 (`sdm670`); [`soc.dtsi`](soc.dtsi) |
| State | Device package (`board.dts`, `soc.dtsi`) and U-Boot/UEFI profile drafted and schema-validated against `hakios-xnu-kernel` (`tools/boards.py check`). Kernel/U-Boot compilation and on-device boot require Linux/macOS (`clang`, `lld`, `aarch64-linux-gnu-gcc`, iPhoneOS SDK). |

Nothing in the procedure writes to the phone's storage. U-Boot + HakiOS `loader.efi` + kernel run from RAM via `fastboot boot`; a reboot brings Android back. An unlocked bootloader is required.

## Hardware Mapping & HakiOS Driver Compatibility

Verified against the live Pixel 3a FDT (`/sys/firmware/fdt`) and mainline `sdm670-google-sargo.dts`:

| Subsystem | Pixel 3a (`sargo`) Hardware | HakiOS Status | Notes |
| --- | --- | --- | --- |
| Boot / ABL | Google ABL (`boot.img` v2, 4K pages, `os_version=15.0.0`, `os_patch_level=2025-11`, `text_offset=0x80000`, LZ4 kernel) | Profile in [`uefi-profiles/google/sargo/`](uefi-profiles/google/sargo/) | Standard `hakios-uefi/u-boot/pack.py` emits v1 gzip; use [`tools/pack_sargo_boot.py`](tools/pack_sargo_boot.py) for Pixel 3a ABL. |
| Memory | 2 GiB @ `0x80000000` + ~1.97 GiB @ `0x100000000` (`0x7e2c0000`) | Compatible | `PAYLOAD_BASE=0xF0000000` (64 MiB) sits in clear low RAM above `0xa1c11000`. |
| CPU / Timer / GIC | 6x Kryo 360 Silver + 2x Kryo 360 Gold, PSCI (`smc`), GICv3 (`0x17a00000`/`0x17a60000`), 19.2 MHz, PPI 19 | Compatible | `cpufreq { block = "osm"; }` matches `qcom,clk-cpu-osm-sdm670` (`0x17d43000`, `0x17d45800`). |
| Display / Backlight | Samsung S6E3FA7 1080×2220 command-mode AMOLED (`0x9c000000` fb), DSI0 `0xae94000`, INTF_1 `0xae6b800` | Ready (`display-scan`, `backlight`) | `dispcc-sdm845` offsets (`gdscr=0x3000`, `ahb=0x4004`, `gcc_disp_ahb=0xb00c`). |
| Buttons / RTC | PM660 PON (`0x008`: Power + Vol-Down), PM660L GPIO7 (`0x2c6`: Vol-Up), PM660 RTC (`0x060`) | Ready (`buttons-pmic`, `rtc`) | Uses SPMI arbiter `0x0c440000`. |
| Thermal / RPMh | TSENS v2 (`0x0c263000`, `0x0c265000`), `cmd-db` (`0x85fe0000`), APPS RSC (`0x179e0000`) | Ready (`thermal`, `ddr-rpmh`) | `thermal=1` / `ddr=1` look-only first. |
| Storage | eMMC 5.1 (`sdhci@7c4000`, `ufshc@1d84000` disabled) | Gap (RAM-only works; native eMMC missing) | HakiOS works from RAM out of the box. Persistent storage via `ufs=2` works if U-Boot exposes eMMC via UEFI `BlockIo` (`CONFIG_MMC_SDHCI_MSM`); HakiOS's native `ufshc.c` is UFS-only. |
| Touch | Synaptics DSX/RMI4 I2C (`synaptics_dsx@20` on `i2c9@a84000`, IRQ GPIO 125, reset GPIO 99) | **Needs new driver** | HakiOS currently has `goodix`, `fts` (FT3658U SPI), `ft5x06` (I2C), and `synaptics_report.h` (TCM). Also note SDM670 TLMM uses 3 split tiles (WEST/NORTH/SOUTH) rather than a single linear base. |
| GPU | Adreno 615 (`kgsl-3d0@5000000`, chipid `0x6010500`) | **Needs driver work** (software render works without it) | HakiOS `adreno.c` targets A660-family (`642`/`642L`/`643`); Darwin boots and draws in software (`gpu=0`). |
| Audio / Haptics / Wi-Fi / BT | Dual Cirrus `cs35l36`, TI `drv2624`, `wcn3990` | **Needs new drivers** | HakiOS currently targets `aw88261`, `aw86927`, `wcn6750`. |

## Next Steps (on Linux or macOS)

1. Validate this package against `hakios-xnu-kernel`:
   ```sh
   tools/device.py add --from /path/to/hakios-sargo
   tools/boards.py check
   tools/boards.py status sargo
   ```
2. Build `loader.efi` (`arch/arm64/boot/build.sh`) and the kernel (`kernel/build.sh`) on Linux/macOS with the iPhoneOS SDK.
3. Build U-Boot for `google/sargo` using [`uefi-profiles/google/sargo/`](uefi-profiles/google/sargo/) and pack with [`tools/pack_sargo_boot.py`](tools/pack_sargo_boot.py), then `fastboot boot` to verify `platform sargo` and the framebuffer console.
4. Enable and verify `display-scan`, `buttons-pmic`, `cpufreq-epss`, `ddr-rpmh`, and `thermal` one at a time via boot-args.
5. Implement SDM670 multi-tile TLMM support + a `touch-synaptics-rmi4` driver (I2C9 `0xa84000`, addr `0x20`) to enable touch input.

## Reference Notes from the `sargo` Branch

- [notes/sargo-bringup-findings.md](notes/sargo-bringup-findings.md) — Google ABL `boot.img` v2 requirements, Pixel 3a memory map / XPU carveouts, and SDCC v5 eMMC init sequence verified across 43 builds.
- [notes/sargo_sdhci_ref.c](notes/sargo_sdhci_ref.c) — Reference SDCC v5 eMMC driver (`0x7c4000` + CQHCI `0x7c5000`, 64-bit ADMA2, ICE bypass) extracted from `sargo` patch `0082` for future native HakiOS eMMC support.
