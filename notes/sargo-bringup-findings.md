# Pixel 3a (`sargo`) Hardware & Boot Findings (from `sargo` branch)

Hardware behavior and bootloader requirements verified directly on the Pixel 3a (`sargo`) in `Adadeane/ios-sargo` (branch `sargo`).

## 1. Google ABL Boot Requirements

- **Kernel compression**: Google's ABL rejects uncompressed ARM64 `Image` binaries (`"Error verifying the received boot.img: Unsupported"`). Ship the U-Boot/shim `Image` compressed with `lz4 -9` (block max 256 KB, content checksum enabled, no size field).
- **`text_offset`**: Google's ABL rejects `text_offset = 0` in the 64-byte ARM64 Linux Image header; use `0x80000` (with `CONFIG_POSITION_INDEPENDENT=y` in U-Boot). `tools/pack_sargo_boot.py` patches `text_offset == 0` to `0x80000` automatically.
- **Boot image header**: Header version `2`, `pagesize = 4096`, `os_version = 15.0.0` / `os_patch_level = 2025-11` (`0x1e00019b`), separate `--dtb` (`dtb_offset = 0x1f00000`).
- **Recovery / Fastboot notes**:
  - `max-download-size` is `256 MiB` (`-S 256M` required for large flashes).
  - After a panic, hold Power until dark, then Power + Volume Down for bootloader; if the phone lands in `fastbootd`, run `fastboot reboot bootloader` before `fastboot boot`.

## 2. Memory Map, XPU Carveouts, Display & Clocks

- **DDR banks** (live FDT):
  - Low bank: `2 GiB` at `0x80000000` (`0x80000000..0xFFFFFFFF`).
  - High bank: `0x7e2c0000` (~1.97 GiB) at `0x100000000`.
- **Reserved / XPU carveouts**: Live FDT `reserved-memory` spans `0x85700000..0xa1c11000` (including `hyp` `0x85700000`, `cmd-db` `0x85fe0000`, `smem` `0x86000000`, `modem` `0x8b000000..0x94800000`, `adsp` `0x95800000..0x97a00000`, `cont_splash` `0x9c000000..0x9e400000`, `qseecom` `0x9e400000..0x9f800000`, `ramoops` `0xa1800000..0xa1c11000`).
  - Qualcomm XPUs fault on speculative cacheable reads into protected carveouts; keep MMU mappings Device-nGnRnE outside active code/RAM and Normal-NC for the `0x9c000000` framebuffer.
  - HakiOS's `PAYLOAD_BASE = 0xF0000000` (`64 MiB`: `0xF0000000..0xF4000000`) sits cleanly in unreserved low RAM above `0xa1c11000`.
- **Framebuffer**: `0x9c000000`, `1080x2220`, stride `4320` (`32 bpp`). Panel is `qcom,mdss_dsi_s6e3fa7_sdc_1080p_cmd` (command mode, DCS `0x51` 2-byte brightness `3..1023`).
- **Timer / GIC / PSCI / IMEM**:
  - `cntfrq_el0 = 19.2 MHz`, virtual timer PPI `19`, GICv3 (`GICD 0x17a00000`, `GICR 0x17a60000`).
  - PSCI `1.0` via `smc` across all 8 cores (`MPIDR` `0x000..0x700`, `core-shift = 8`).
  - IMEM restart reason: `0x146bf65c` (`RESTART_BOOTLOADER = 0x77665500`).

## 3. eMMC Storage (`sdhci@7c4000`) Bring-up Findings

Pixel 3a has **eMMC 5.1** (`sdhci@7c4000`, `cqhci@7c5000`), not UFS (`ufshc@1d84000` is disabled). From 43 builds on the `sargo` branch (see [`notes/sargo_sdhci_ref.c`](sargo_sdhci_ref.c) extracted from `0082-sargo-sdhci-root-device.patch`):

1. **Do not full-reset the host controller**: ABL leaves clocks and power configured; a full host reset drops them. Only reset `CMD` (`0x02`) / `DAT` (`0x04`) lines via `SOFTWARE_RESET`.
2. **Disable CQHCI & bypass Qualcomm ICE**:
   - Clear `CQHCI_ENABLE` (`0x01`) at `0x7c5000 + 0x08`.
   - Zero `ICE_NONCQ_CRYPTO_PARAMS` (`0x7c5000 + 0x70`) and `ICE_NONCQ_CRYPTO_DUN` (`0x7c5000 + 0x74`), and set `DISABLE_CRYPTO` (`1 << 15`) at `0x7c4000 + 0x260`.
3. **Clear clock power-save & DLL**:
   - In `CORE_VENDOR_SPEC` (`0x7c4000 + 0x20c`), clear `CORE_CLK_PWRSAVE` (`1 << 1`) and `CORE_HC_SELECT_IN_*`, and set `CORE_HC_MCLK_SEL_DFLT` (`2 << 8`).
   - Write `CORE_DLL_RST | CORE_DLL_PDN` (`(1 << 30) | (1 << 29)`) to `CORE_DLL_CONFIG` (`0x7c4000 + 0x200`) for SDR operation.
4. **Use atomic 32-bit MMIO writes**: On Qualcomm's AHB bus, 16-bit writes to `REG_BLOCK_SIZE`/`REG_BLOCK_COUNT` (`0x04`) or `REG_TRANSFER_MODE`/`REG_COMMAND` (`0x0c`) can clobber the adjacent half; always write the full 32-bit dword.
5. **Exit ABL's HS400 mode cleanly**: After `CMD0 -> CMD1 -> CMD2 -> CMD3 (eMMC SET_RELATIVE_ADDR, RCA=2, R1 status) -> CMD9 -> CMD7`:
   - `CMD6` `0x03B90000` (`EXT_CSD[185] HS_TIMING = 0`, exit HS400)
   - `CMD6` `0x03B70000` (`EXT_CSD[183] BUS_WIDTH = 0`, 1-bit SDR)
   - `CMD16` (`512` bytes), then raise `SDCLK` to `25 MHz` (`200 MHz` base clock / divider `4`) and run 64-bit ADMA2 (`HOST_CONTROL = 0x18`, 12-byte descriptors at `0x58`/`0x5c`) with directional `dc civac` / `dc ivac` cache maintenance.

In HakiOS, U-Boot (`CONFIG_MMC_SDHCI_MSM=y`) can expose eMMC via UEFI `BlockIo` for `ufs=2`, or `notes/sargo_sdhci_ref.c` can be adapted into a native HakiOS `drivers/board/` eMMC block driver.
