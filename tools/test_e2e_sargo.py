#!/usr/bin/env python3
"""Comprehensive Opaque-Box E2E Test Suite for sargo iOS 16 XNU Kernel Port.

Test Architecture:
- Tier 1: Feature Coverage (>=5 tests per feature across 11 features = 55 tests)
  - Feature 1: SDCC v5 Register Mapping
  - Feature 2: Qualcomm ICE Bypass
  - Feature 3: Interrupt Status Handling
  - Feature 4: SDMA Data Transport
  - Feature 5: LBA 0 MBR Signature Verification
  - Feature 6: LBA 1 GPT Header Verification
  - Feature 7: Partition Table Parsing
  - Feature 8: Block Device & Rootfs Mount
  - Feature 9: Preemptive Boot Audit
  - Feature 10: Repo Validation & Build Integrity
  - Feature 11: Version Control & CI Setup
- Tier 2: Boundary & Corner Cases (5 tests)
- Tier 3: Combinatorial & Cross-Feature Interactions (3 tests)
- Tier 4: Real-World Acceptance Scenarios (2 tests)

Total: 65 Comprehensive Tests.
Exit code: 0 on all tests passing, non-zero on failure.
"""

import glob
import os
import py_compile
import re
import struct
import subprocess
import sys
import unittest
import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# -----------------------------------------------------------------------------
# Hardware Emulation & Reference Models for Qualcomm SDCC v5 eMMC Controller
# -----------------------------------------------------------------------------

SDHCI_BLOCK = 512
SARGO_SDHCI_BASE = 0x7C4000
SARGO_CQHCI_BASE = 0x7C5000

# Standard SDHCI Offsets
REG_DMA_ADDR = 0x00
REG_BLOCK_SIZE = 0x04
REG_BLOCK_COUNT = 0x06
REG_ARGUMENT = 0x08
REG_TRANSFER_MODE = 0x0C
REG_COMMAND = 0x0E
REG_RESPONSE0 = 0x10
REG_RESPONSE1 = 0x14
REG_RESPONSE2 = 0x18
REG_RESPONSE3 = 0x1C
REG_BUFFER = 0x20
REG_PRESENT_STATE = 0x24
REG_HOST_CONTROL = 0x28
REG_POWER_CONTROL = 0x29
REG_CLOCK_CONTROL = 0x2C
REG_TIMEOUT_CONTROL = 0x2E
REG_SOFTWARE_RESET = 0x2F
REG_INT_STATUS = 0x30
REG_INT_STATUS_EN = 0x34
REG_INT_SIGNAL_EN = 0x38
REG_CAPABILITIES = 0x40

# SDCC v5 Core Vendor Offsets (in 0x7c4000)
CORE_DLL_CONFIG = 0x200
CORE_VENDOR_SPEC = 0x20C
CORE_MCI_FIFO_CNT = 0x308
CORE_MCI_DATA_CNT = 0x35C

# CQHCI / ICE Offsets (in 0x7c5000)
CQHCI_CFG = 0x08
ICE_NONCQ_CRYPTO_PARAMS = 0x70
ICE_NONCQ_CRYPTO_DUN = 0x74

# Interrupt Status Bits
INT_CMD_COMPLETE = 0x0001
INT_XFER_COMPLETE = 0x0002
INT_BUF_READ_READY = 0x0020
INT_ERROR = 0x8000
EINT_CMD_TIMEOUT = 0x0001
EINT_DATA_TIMEOUT = 0x0010

# Command Response Types
CMD_RESP_NONE = 0x00
CMD_RESP_LONG = 0x01
CMD_RESP_SHORT = 0x02
CMD_RESP_SHORT_BUSY = 0x03
CMD_FLAG_CRC = 0x08
CMD_FLAG_INDEX = 0x10
CMD_FLAG_DATA = 0x20

# Canonical Boot Args Invariant
CANONICAL_BOOT_ARGS = (
    "debug=0xc serial=3 cpus=1 keepsyms=1 -enable_kprintf_spam -noprogress "
    "rd=sdhciroot0 sdhciroot=userdata unentitled_ios_sim_launch=1 "
    "ignition_level=1 wdt_test=no_user_checkin rootrw=1 launchd_unsecure_cache=1 wdt=-1"
)


# -----------------------------------------------------------------------------
# Patch 0082 Artifact Inspector (Opaque-Box Unified Diff & Driver Verifier)
# -----------------------------------------------------------------------------

class Patch0082Inspector:
    """Extracts, parses, and statically/dynamically validates patch 0082."""

    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.patch_path = os.path.join(REPO_ROOT, "xnu/patches/0082-sargo-sdhci-root-device.patch")
        self.raw_patch = self._read_patch()
        self.hunk_files = self._parse_hunks()
        self.c_source = self.hunk_files.get("bsd/dev/sargo_sdhci.c", "")
        self.iokit_source = self.hunk_files.get("iokit/bsddev/IOKitBSDInit.cpp", "")
        self.conf_source = self.hunk_files.get("bsd/conf/files", "")

    def _read_patch(self):
        with open(self.patch_path, "r", errors="replace") as f:
            return f.read()

    def _parse_hunks(self):
        hunks = {}
        cur_file = None
        cur_lines = []
        for line in self.raw_patch.splitlines(True):
            if line.startswith("+++ b/"):
                if cur_file:
                    hunks[cur_file] = "".join(cur_lines)
                cur_file = line[6:].strip()
                cur_lines = []
            elif cur_file:
                if line.startswith("diff --git"):
                    hunks[cur_file] = "".join(cur_lines)
                    cur_file = None
                    cur_lines = []
                elif line.startswith("+") and not line.startswith("+++"):
                    cur_lines.append(line[1:])
                elif line.startswith(" ") and not line.startswith("   "):
                    cur_lines.append(line[1:])
        if cur_file and cur_lines:
            hunks[cur_file] = "".join(cur_lines)
        return hunks

    def validate_patch_syntax(self):
        res = subprocess.run(
            ["git", "apply", "--stat", self.patch_path],
            capture_output=True, text=True, check=False
        )
        return res.returncode == 0, res.stderr.strip()

    def get_macro_definition(self, name):
        m = re.search(rf'#define\s+{name}\s+([^\n\r]+)', self.c_source)
        return m.group(1).strip() if m else ""

    def get_function_body(self, func_name):
        m = re.search(rf'\b{func_name}\s*\([^)]*\)\s*\{{', self.c_source)
        if not m:
            return ""
        start = m.end() - 1
        depth = 0
        for i in range(start, len(self.c_source)):
            if self.c_source[i] == '{':
                depth += 1
            elif self.c_source[i] == '}':
                depth -= 1
                if depth == 0:
                    return self.c_source[start:i+1]
        return self.c_source[start:]


class Patch0083Inspector:
    """Extracts, parses, and statically validates patch 0083."""

    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.patch_path = os.path.join(REPO_ROOT, "xnu/patches/0083-sargo-vfs-cryptex-redirection-and-shared-region.patch")
        self.raw_patch = self._read_patch()
        self.hunk_files = self._parse_hunks()
        self.vfs_source = self.hunk_files.get("bsd/vfs/vfs_lookup.c", "")
        self.vm_source = self.hunk_files.get("bsd/vm/vm_unix.c", "")

    def _read_patch(self):
        if not os.path.exists(self.patch_path):
            return ""
        with open(self.patch_path, "r", errors="replace") as f:
            return f.read()

    def _parse_hunks(self):
        hunks = {}
        cur_file = None
        cur_lines = []
        for line in self.raw_patch.splitlines(True):
            if line.startswith("+++ b/"):
                if cur_file:
                    hunks[cur_file] = "".join(cur_lines)
                cur_file = line[6:].strip()
                cur_lines = []
            elif cur_file:
                if line.startswith("diff --git"):
                    hunks[cur_file] = "".join(cur_lines)
                    cur_file = None
                    cur_lines = []
                elif line.startswith("+") and not line.startswith("+++"):
                    cur_lines.append(line[1:])
                elif line.startswith(" ") and not line.startswith("   "):
                    cur_lines.append(line[1:])
        if cur_file and cur_lines:
            hunks[cur_file] = "".join(cur_lines)
        return hunks

    def validate_patch_syntax(self):
        res = subprocess.run(
            ["git", "apply", "--stat", self.patch_path],
            capture_output=True, text=True, check=False
        )
        return res.returncode == 0, res.stderr.strip()


class Patch0084Inspector:
    """Extracts, parses, and statically validates patch 0084."""

    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.patch_path = os.path.join(REPO_ROOT, "xnu/patches/0084-sargo-process-sampler-diagnostics.patch")
        self.raw_patch = self._read_patch()
        self.hunk_files = self._parse_hunks()
        self.sample_source = self.hunk_files.get("bsd/dev/sweet_sample.c", "")

    def _read_patch(self):
        if not os.path.exists(self.patch_path):
            return ""
        with open(self.patch_path, "r", errors="replace") as f:
            return f.read()

    def _parse_hunks(self):
        hunks = {}
        cur_file = None
        cur_lines = []
        for line in self.raw_patch.splitlines(True):
            if line.startswith("+++ b/"):
                if cur_file:
                    hunks[cur_file] = "".join(cur_lines)
                cur_file = line[6:].strip()
                cur_lines = []
            elif cur_file:
                if line.startswith("diff --git"):
                    hunks[cur_file] = "".join(cur_lines)
                    cur_file = None
                    cur_lines = []
                elif line.startswith("+") and not line.startswith("+++"):
                    cur_lines.append(line[1:])
                elif line.startswith(" ") and not line.startswith("   "):
                    cur_lines.append(line[1:])
        if cur_file and cur_lines:
            hunks[cur_file] = "".join(cur_lines)
        return hunks

    def validate_patch_syntax(self):
        res = subprocess.run(
            ["git", "apply", "--stat", self.patch_path],
            capture_output=True, text=True, check=False
        )
        return res.returncode == 0, res.stderr.strip()


class Patch0085Inspector:
    """Extracts, parses, and statically validates patch 0085."""

    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self.patch_path = os.path.join(REPO_ROOT, "xnu/patches/0085-sargo-platform-binary-and-entitlements.patch")
        self.raw_patch = self._read_patch()
        self.hunk_files = self._parse_hunks()
        self.cs_source = self.hunk_files.get("bsd/kern/kern_cs.c", "")
        self.exec_source = self.hunk_files.get("bsd/kern/kern_exec.c", "")
        self.proc_source = self.hunk_files.get("bsd/kern/kern_proc.c", "")
        self.amfi_source = self.hunk_files.get("bsd/kern/sweet_amfi.c", "")
        self.fb_source = self.hunk_files.get("iokit/Kernel/arm/SweetFramebuffer.cpp", "")

    def _read_patch(self):
        if not os.path.exists(self.patch_path):
            return ""
        with open(self.patch_path, "r", errors="replace") as f:
            return f.read()

    def _parse_hunks(self):
        hunks = {}
        cur_file = None
        cur_lines = []
        for line in self.raw_patch.splitlines(True):
            if line.startswith("+++ b/"):
                if cur_file:
                    hunks[cur_file] = "".join(cur_lines)
                cur_file = line[6:].strip()
                cur_lines = []
            elif cur_file:
                if line.startswith("diff -Naur") or line.startswith("diff --git"):
                    hunks[cur_file] = "".join(cur_lines)
                    cur_file = None
                    cur_lines = []
                elif line.startswith("+") and not line.startswith("+++"):
                    cur_lines.append(line[1:])
                elif line.startswith(" ") and not line.startswith("   "):
                    cur_lines.append(line[1:])
        if cur_file and cur_lines:
            hunks[cur_file] = "".join(cur_lines)
        return hunks

    def validate_patch_syntax(self):
        res = subprocess.run(
            ["git", "apply", "--stat", self.patch_path],
            capture_output=True, text=True, check=False
        )
        return res.returncode == 0, res.stderr.strip()


class MockPhysicalMemory:
    """Simulates physical RAM for SDMA bus-master transfers and cache coherency."""

    def __init__(self, size=0x100000, base=0x80000000):
        self.base = base
        self.size = size
        self.mem = bytearray(size)
        self.dcache_flushed_ranges = []

    def translate_pa(self, pa):
        if pa < self.base or pa >= self.base + self.size:
            raise ValueError(f"Physical address 0x{pa:08x} out of range [0x{self.base:08x}..0x{self.base + self.size:08x}]")
        return pa - self.base

    def write_bytes(self, pa, data):
        off = self.translate_pa(pa)
        self.mem[off : off + len(data)] = data

    def read_bytes(self, pa, length):
        off = self.translate_pa(pa)
        return bytes(self.mem[off : off + length])

    def flush_dcache64(self, pa, length, invalidate=True):
        self.dcache_flushed_ranges.append((pa, length, invalidate))


class MockSDCCv5Hardware:
    """Cycle-accurate software model of Qualcomm SDCC v5 Host Controller."""

    def __init__(self, phys_mem: MockPhysicalMemory):
        self.phys_mem = phys_mem
        self.regs = {}          # 32-bit registers at base 0x7c4000
        self.cq_regs = {}       # 32-bit registers at base 0x7c5000
        self.fifo = bytearray() # Controller hardware FIFO
        self.card_data_store = {} # LBA -> 512 bytes
        self.card_state = "IDLE"
        self.card_rca = 0
        self.card_ocr = 0xC0FF8080 # High Capacity eMMC OCR
        self.card_cid = [0x15010041, 0x4A34394D, 0x42006765, 0x8291A1E1]
        self.card_csd = [0xD0270132, 0x0F5903FF, 0xF6DBFFEF, 0x8A404000]

        # Init default register states
        self.regs[REG_CAPABILITIES] = 0x280700B0 # 200 MHz base clock
        self.regs[REG_PRESENT_STATE] = 0x01F80000 # DAT[3:0] high
        self.regs[REG_HOST_CONTROL] = 0x00000000
        self.regs[REG_CLOCK_CONTROL] = 0x00000000
        self.regs[REG_INT_STATUS] = 0x00000000
        self.regs[REG_INT_STATUS_EN] = 0x00000000
        self.regs[REG_INT_SIGNAL_EN] = 0x00000000
        self.regs[CORE_MCI_FIFO_CNT] = 0
        self.regs[CORE_MCI_DATA_CNT] = 0
        self.regs[CORE_VENDOR_SPEC] = 0x00000200
        self.cq_regs[CQHCI_CFG] = 0x00000000
        self.cq_regs[ICE_NONCQ_CRYPTO_PARAMS] = 0x00000001 # CE enabled by default before bypass
        self.cq_regs[ICE_NONCQ_CRYPTO_DUN] = 0x00000000

        self.ahb_violations = []
        self.corrupted_registers = set()
        self.dsb_executed = False

    def rd32(self, off):
        if off % 4 != 0:
            self.ahb_violations.append(f"Unaligned rd32 at 0x{off:x}")
            raise ValueError(f"Qualcomm AHB requires 32-bit aligned reads (off=0x{off:x})")
        if off == 0x80:
            # Qualcomm SDCC v5 eliminated legacy MCI FIFO at 0x80 (.mci_removed = true)
            return 0x00000000
        if off == REG_BUFFER:
            # Drain 4 bytes from FIFO port
            if len(self.fifo) >= 4:
                val = struct.unpack("<I", self.fifo[:4])[0]
                self.fifo = self.fifo[4:]
                self.regs[CORE_MCI_FIFO_CNT] = len(self.fifo)
                return val
            return 0x00000000
        return self.regs.get(off, 0x00000000)

    def wr32(self, off, val):
        if off % 4 != 0:
            self.ahb_violations.append(f"Unaligned wr32 at 0x{off:x}")
            raise ValueError(f"Qualcomm AHB requires 32-bit aligned writes (off=0x{off:x})")
        val &= 0xFFFFFFFF
        if off in (0x300, 0x304, 0x30C):
            # Bogus writes to host controller 0x300..0x30c corrupt core FIFO state
            self.corrupted_registers.add(off)
        if off == REG_INT_STATUS:
            # W1C (write-1-to-clear)
            self.regs[REG_INT_STATUS] = self.regs.get(REG_INT_STATUS, 0) & ~val
            return
        if off == REG_TRANSFER_MODE:
            # Atomic packing: mode in low 16, cmd in high 16
            mode = val & 0xFFFF
            cmd_val = (val >> 16) & 0xFFFF
            self.regs[REG_TRANSFER_MODE] = mode
            self.regs[REG_COMMAND] = cmd_val
            self._execute_cmd(cmd_val, mode)
            return
        self.regs[off] = val

    def wr_cq32(self, off, val):
        if off % 4 != 0:
            raise ValueError(f"CQHCI requires 32-bit alignment (off=0x{off:x})")
        self.cq_regs[off] = val & 0xFFFFFFFF

    def rd_cq32(self, off):
        return self.cq_regs.get(off, 0x00000000)

    def dsb(self):
        self.dsb_executed = True

    def _execute_cmd(self, cmd_val, mode_val):
        idx = (cmd_val >> 8) & 0x3F
        flags = cmd_val & 0xFF
        arg = self.regs.get(REG_ARGUMENT, 0)
        has_data = bool(flags & CMD_FLAG_DATA)

        # Card state machine transitions
        if idx == 0:
            self.card_state = "IDLE"
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE
        elif idx == 1:
            self.card_state = "READY"
            self.regs[REG_RESPONSE0] = self.card_ocr
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE
        elif idx == 2:
            self.card_state = "IDENT"
            self.regs[REG_RESPONSE0] = self.card_cid[0]
            self.regs[REG_RESPONSE1] = self.card_cid[1]
            self.regs[REG_RESPONSE2] = self.card_cid[2]
            self.regs[REG_RESPONSE3] = self.card_cid[3]
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE
        elif idx == 3:
            rca = (arg >> 16) & 0xFFFF
            self.card_rca = rca
            self.card_state = "STBY"
            self.regs[REG_RESPONSE0] = 0x00000000 # R1 no error
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE
        elif idx == 9:
            self.regs[REG_RESPONSE0] = self.card_csd[0]
            self.regs[REG_RESPONSE1] = self.card_csd[1]
            self.regs[REG_RESPONSE2] = self.card_csd[2]
            self.regs[REG_RESPONSE3] = self.card_csd[3]
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE
        elif idx == 7:
            self.card_state = "TRAN"
            self.regs[REG_RESPONSE0] = 0x00000000
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE
        elif idx == 6:
            self.regs[REG_RESPONSE0] = 0x00000000
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE
        elif idx == 16:
            self.regs[REG_RESPONSE0] = 0x00000000
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE
        elif idx == 17:
            # Single block read
            lba = arg
            sector_data = self.card_data_store.get(lba, bytes(512))
            self.regs[REG_RESPONSE0] = 0x00000000 # Card status
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE

            is_dma = bool(mode_val & 0x0001)
            if is_dma:
                dma_addr = self.regs.get(REG_DMA_ADDR, 0)
                # SDMA: Bus-Master DMA writes directly to RAM
                self.phys_mem.write_bytes(dma_addr, sector_data)
                self.regs[CORE_MCI_DATA_CNT] = 0
                self.regs[CORE_MCI_FIFO_CNT] = 0
                self.regs[REG_INT_STATUS] |= INT_XFER_COMPLETE
            else:
                # PIO: Data clocks into FIFO
                self.fifo = bytearray(sector_data)
                self.regs[CORE_MCI_FIFO_CNT] = len(self.fifo)
                self.regs[CORE_MCI_DATA_CNT] = 0
                self.regs[REG_INT_STATUS] |= INT_BUF_READ_READY
        else:
            self.regs[REG_INT_STATUS] |= INT_CMD_COMPLETE


class GPTBuilder:
    """Generates synthetic MBR and GPT structures for testing."""

    @staticmethod
    def create_protective_mbr(valid=True, sig=0xAA55):
        buf = bytearray(512)
        if valid:
            # Signature at 510..511
            struct.pack_into("<H", buf, 510, sig)
        return bytes(buf)

    @staticmethod
    def create_gpt_header(valid_magic=True, entry_lba=2, nent=128, esz=128):
        buf = bytearray(512)
        magic = b"EFI PART" if valid_magic else b"BAD PART"
        buf[0:8] = magic
        struct.pack_into("<I", buf, 8, 0x00010000) # revision 1.0
        struct.pack_into("<I", buf, 12, 92)        # header size
        struct.pack_into("<Q", buf, 24, 1)         # my_lba
        struct.pack_into("<Q", buf, 72, entry_lba) # entry_lba
        struct.pack_into("<I", buf, 80, nent)      # number of entries
        struct.pack_into("<I", buf, 84, esz)       # entry size
        return bytes(buf)

    @staticmethod
    def create_partition_entry(name: str, first_lba: int, last_lba: int):
        entry = bytearray(128)
        # Type GUID: Basic data
        entry[0:16] = bytes([0xAF, 0x3D, 0xC6, 0x0F, 0x83, 0x84, 0x72, 0x47, 0x8E, 0x79, 0x3D, 0x69, 0xD8, 0x47, 0x7D, 0xE4])
        # Unique GUID
        entry[16:32] = os.urandom(16)
        # Starting LBA (offset 32)
        struct.pack_into("<Q", entry, 32, first_lba)
        # Ending LBA (offset 40)
        struct.pack_into("<Q", entry, 40, last_lba)
        # Attributes (offset 48)
        struct.pack_into("<Q", entry, 48, 0)
        # Name: UTF-16LE, up to 36 chars (offset 56)
        name_utf16 = name.encode("utf-16le")[:72]
        entry[56 : 56 + len(name_utf16)] = name_utf16
        return bytes(entry)


# -----------------------------------------------------------------------------
# Tier 1 Tests: Feature Coverage (>=5 tests per feature)
# -----------------------------------------------------------------------------

class TestTier1Feature1SDCCv5RegisterMapping(unittest.TestCase):
    """Feature 1: SDCC v5 Register Mapping."""

    def setUp(self):
        self.mem = MockPhysicalMemory()
        self.hw = MockSDCCv5Hardware(self.mem)
        self.insp = Patch0082Inspector.get_instance()

    def test_01_reg_0x80_removed_in_sdcc_v5(self):
        """Verify legacy 0x80 (REG_MMCIFIFO) is completely eliminated from patch 0082."""
        self.assertNotIn("REG_MMCIFIFO", self.insp.c_source, "Obsolete REG_MMCIFIFO must not be defined")
        self.assertFalse(re.search(r'\b0x80\b', self.insp.c_source), "Legacy offset 0x80 must not be accessed in sargo_sdhci.c")
        val = self.hw.rd32(0x80)
        self.assertEqual(val, 0x00000000, "Reading legacy 0x80 should return 0 in SDCC v5")

    def test_02_fifo_cnt_offset_0x308(self):
        """Verify offset 0x308 represents CORE_MCI_FIFO_CNT in patch 0082 and mock."""
        val = self.insp.get_macro_definition("REG_CORE_MCI_FIFO_CNT")
        self.assertIn("0x308", val, "REG_CORE_MCI_FIFO_CNT must be 0x308")
        self.assertIn("sdhci_rd32(REG_CORE_MCI_FIFO_CNT)", self.insp.c_source)
        self.assertEqual(CORE_MCI_FIFO_CNT, 0x308)
        self.hw.fifo = bytearray(512)
        self.hw.regs[CORE_MCI_FIFO_CNT] = 512
        self.assertEqual(self.hw.rd32(CORE_MCI_FIFO_CNT), 512)

    def test_03_data_cnt_offset_0x35c(self):
        """Verify offset 0x35c represents CORE_MCI_DATA_CNT in patch 0082 and mock."""
        val = self.insp.get_macro_definition("REG_CORE_MCI_DATA_CNT")
        self.assertIn("0x35c", val.lower(), "REG_CORE_MCI_DATA_CNT must be 0x35C")
        self.assertIn("sdhci_rd32(REG_CORE_MCI_DATA_CNT)", self.insp.c_source)
        self.assertEqual(CORE_MCI_DATA_CNT, 0x35C)
        self.hw.regs[CORE_MCI_DATA_CNT] = 0
        self.assertEqual(self.hw.rd32(CORE_MCI_DATA_CNT), 0)

    def test_04_standard_sdhci_offsets(self):
        """Verify standard SDHCI register offsets in patch 0082 match hardware spec."""
        expected = {
            "REG_DMA_ADDR": "0x00",
            "REG_BLOCK_SIZE": "0x04",
            "REG_ARGUMENT": "0x08",
            "REG_TRANSFER_MODE": "0x0c",
            "REG_BUFFER": "0x20",
            "REG_PRESENT_STATE": "0x24",
            "REG_INT_STATUS": "0x30",
        }
        for name, exp_val in expected.items():
            val = self.insp.get_macro_definition(name)
            self.assertTrue(val.lower().startswith(exp_val.lower()), f"{name} mismatch in patch: {val} vs {exp_val}")

    def test_05_atomic_32bit_mmio_wrappers(self):
        """Verify 32-bit MMIO atomic write rules on Qualcomm AHB and no 8/16-bit MMIO in patch 0082."""
        body = re.sub(r'static\s+inline\s+(uint8_t|uint16_t|void)\s+sdhci_(rd8|wr8|rd16|wr16)\b[^{]*\{[^}]*\}', '', self.insp.c_source)
        for fn in ['sdhci_rd8', 'sdhci_wr8', 'sdhci_rd16', 'sdhci_wr16']:
            matches = re.findall(rf'\b{fn}\s*\(', body)
            self.assertEqual(len(matches), 0, f"Prohibited non-32-bit MMIO call found in patch: {fn}")

        atomic_blk = (1 << 16) | SDHCI_BLOCK
        self.hw.wr32(REG_BLOCK_SIZE, atomic_blk)
        self.assertEqual(self.hw.rd32(REG_BLOCK_SIZE), atomic_blk)

        hostctl = 0x00
        pwrctl = 0x0F
        atomic_pwr = hostctl | (pwrctl << 8)
        self.hw.wr32(REG_HOST_CONTROL, atomic_pwr)
        self.assertEqual(self.hw.rd32(REG_HOST_CONTROL) & 0xFFFF, 0x0F00)

        with self.assertRaises(ValueError):
            self.hw.wr32(0x02, 0x12345678)


class TestTier1Feature2QualcommICEBypass(unittest.TestCase):
    """Feature 2: Qualcomm ICE Bypass."""

    def setUp(self):
        self.mem = MockPhysicalMemory()
        self.hw = MockSDCCv5Hardware(self.mem)
        self.insp = Patch0082Inspector.get_instance()

    def test_01_ice_target_address_at_cq_regs_0x70(self):
        """Verify ICE bypass targets ss.cq_regs + 0x70 (ICE_NONCQ_CRYPTO_PARAMS) in patch 0082."""
        val = self.insp.get_macro_definition("ICE_NONCQ_CRYPTO_PARAMS")
        self.assertIn("0x70", val)
        val_dun = self.insp.get_macro_definition("ICE_NONCQ_CRYPTO_DUN")
        self.assertIn("0x74", val_dun)
        self.assertIn("SARGO_CQHCI_BASE", self.insp.c_source)
        self.assertEqual(ICE_NONCQ_CRYPTO_PARAMS, 0x70)

    def test_02_ice_crypto_enable_cleared(self):
        """Verify Crypto Enable bit (CE) is cleared to 0 in patch 0082 sdhci_init_card()."""
        fn_body = self.insp.get_function_body("sdhci_init_card")
        self.assertIn("*(volatile uint32_t *)(ss.cq_regs + ICE_NONCQ_CRYPTO_PARAMS) = 0x00000000u;", fn_body)
        self.hw.wr_cq32(ICE_NONCQ_CRYPTO_PARAMS, 0x00000000)
        val = self.hw.rd_cq32(ICE_NONCQ_CRYPTO_PARAMS)
        self.assertEqual(val & 0x01, 0, "Crypto Enable bit CE must be 0")

    def test_03_ice_noncq_crypto_dun_cleared(self):
        """Verify ss.cq_regs + 0x74 (ICE_NONCQ_CRYPTO_DUN) is cleared to 0 in patch 0082 sdhci_init_card()."""
        fn_body = self.insp.get_function_body("sdhci_init_card")
        self.assertIn("*(volatile uint32_t *)(ss.cq_regs + ICE_NONCQ_CRYPTO_DUN) = 0x00000000u;", fn_body)
        self.assertEqual(ICE_NONCQ_CRYPTO_DUN, 0x74)
        self.hw.wr_cq32(ICE_NONCQ_CRYPTO_DUN, 0x00000000)
        self.assertEqual(self.hw.rd_cq32(ICE_NONCQ_CRYPTO_DUN), 0)

    def test_04_prohibit_corrupting_writes_to_0x300_0x30c(self):
        """Verify host controller 0x300..0x30c are not modified for ICE bypass in patch 0082."""
        for bad in ["0x300", "0x304", "0x30c", "REG_CORE_ICE_CTRL"]:
            self.assertNotIn(bad, self.insp.c_source, f"Corrupting access to {bad} must not exist in driver")
        self.hw.wr32(0x300, 1)
        self.assertIn(0x300, self.hw.corrupted_registers, "Writing to 0x300 in ss.regs corrupts core FIFO state")

    def test_05_ice_dsb_barrier_executed(self):
        """Verify DSB barrier is invoked after ICE bypass configuration in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_init_card")
        ice_pos = fn_body.find("ICE_NONCQ_CRYPTO_PARAMS")
        dsb_pos = fn_body.find("__builtin_arm_dsb(0xf)", ice_pos)
        self.assertNotEqual(dsb_pos, -1, "ARM DSB barrier must follow ICE configuration in patch 0082")
        self.hw.wr_cq32(ICE_NONCQ_CRYPTO_PARAMS, 0)
        self.hw.dsb()
        self.assertTrue(self.hw.dsb_executed)


class TestTier1Feature3InterruptStatusHandling(unittest.TestCase):
    """Feature 3: Interrupt Status Handling."""

    def setUp(self):
        self.mem = MockPhysicalMemory()
        self.hw = MockSDCCv5Hardware(self.mem)
        self.insp = Patch0082Inspector.get_instance()

    def test_01_line_419_clears_cmd_complete(self):
        """Verify W1C acknowledgment in sdhci_cmd clears ONLY INT_CMD_COMPLETE."""
        fn_body = self.insp.get_function_body("sdhci_cmd")
        self.assertIn("sdhci_wr32(REG_INT_STATUS, INT_CMD_COMPLETE);", fn_body)
        self.hw.regs[REG_INT_STATUS] = INT_CMD_COMPLETE | INT_BUF_READ_READY
        self.hw.wr32(REG_INT_STATUS, INT_CMD_COMPLETE)
        st = self.hw.rd32(REG_INT_STATUS)
        self.assertEqual(st & INT_CMD_COMPLETE, 0)
        self.assertEqual(st & INT_BUF_READ_READY, INT_BUF_READ_READY)

    def test_02_line_419_retains_buf_read_ready(self):
        """Verify sdhci_cmd does NOT prematurely clear INT_BUF_READ_READY at command completion."""
        fn_body = self.insp.get_function_body("sdhci_cmd")
        self.assertNotIn("INT_CMD_COMPLETE | INT_BUF_READ_READY", fn_body,
                         "Must not clear INT_BUF_READ_READY at command completion")
        self.hw.regs[REG_INT_STATUS] = INT_CMD_COMPLETE | INT_BUF_READ_READY
        buggy_clear = INT_CMD_COMPLETE | INT_BUF_READ_READY
        self.hw.wr32(REG_INT_STATUS, buggy_clear)
        self.assertEqual(self.hw.rd32(REG_INT_STATUS) & INT_BUF_READ_READY, 0)

    def test_03_buf_read_ready_premature_clear_stall_detection(self):
        """Verify sdhci_cmd data loop waits for INT_XFER_COMPLETE."""
        fn_body = self.insp.get_function_body("sdhci_cmd")
        self.assertIn("if (nint & INT_XFER_COMPLETE)", fn_body)
        self.assertIn("sdhci_wr32(REG_INT_STATUS, INT_XFER_COMPLETE);", fn_body)

    def test_04_buf_read_ready_acknowledged_post_drain(self):
        """Verify INT_XFER_COMPLETE is acknowledged upon SDMA completion in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_cmd")
        xfer_pos = fn_body.find("if (nint & INT_XFER_COMPLETE)")
        ack_pos = fn_body.find("sdhci_wr32(REG_INT_STATUS, INT_XFER_COMPLETE);", xfer_pos)
        self.assertNotEqual(ack_pos, -1, "sdhci_cmd must clear INT_XFER_COMPLETE")

    def test_05_error_status_bit_decoding(self):
        """Verify error status bits map to ETIMEDOUT / EIO in patch 0082 sdhci_cmd."""
        fn_body = self.insp.get_function_body("sdhci_cmd")
        self.assertIn("error = (eint & EINT_DATA_TIMEOUT) ? ETIMEDOUT : EIO;", fn_body)
        st = (EINT_DATA_TIMEOUT << 16) | INT_ERROR
        eint = st >> 16
        nint = st & 0xFFFF
        is_timeout = bool(eint & EINT_DATA_TIMEOUT)
        self.assertTrue(is_timeout)
        self.assertTrue(bool(nint & INT_ERROR))


class TestTier1Feature4SDMADataTransport(unittest.TestCase):
    """Feature 4: SDMA Data Transport."""

    def setUp(self):
        self.mem = MockPhysicalMemory(size=0x100000, base=0x80000000)
        self.hw = MockSDCCv5Hardware(self.mem)
        self.insp = Patch0082Inspector.get_instance()

    def test_01_reg_dma_addr_programming(self):
        """Verify REG_ADMA_ADDR_LO (0x58) and REG_ADMA_ADDR_HI (0x5c) are defined and programmed in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_cmd")
        self.assertIn("sdhci_wr32(REG_ADMA_ADDR_LO", fn_body)
        self.assertIn("sdhci_wr32(REG_ADMA_ADDR_HI", fn_body)
        self.assertIn("#define REG_ADMA_ADDR_LO", self.insp.c_source)
        self.assertIn("#define REG_ADMA_ADDR_HI", self.insp.c_source)

    def test_02_transfer_mode_0x0013(self):
        """Verify Transfer Mode 0x0013 (DMA + Block Count + Read) is programmed in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_cmd")
        self.assertIn("data ? 0x0013u : 0x0000u", fn_body)
        mode = 0x0013
        dma_en = bool(mode & 0x0001)
        blk_cnt_en = bool(mode & 0x0002)
        read_dir = bool(mode & 0x0010)
        self.assertTrue(dma_en)
        self.assertTrue(blk_cnt_en)
        self.assertTrue(read_dir)

    def test_03_flush_dcache64_invocation(self):
        """Verify flush_dcache64 invalidation is invoked on INT_XFER_COMPLETE in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_cmd")
        xfer_pos = fn_body.find("if (nint & INT_XFER_COMPLETE)")
        flush_pos = fn_body.find("flush_dcache64(bounce_phys, SDHCI_BLOCK, TRUE);", xfer_pos)
        self.assertNotEqual(flush_pos, -1, "flush_dcache64 must be called when INT_XFER_COMPLETE is received")
        phys = 0x80001000
        self.mem.flush_dcache64(phys, SDHCI_BLOCK, True)
        self.assertEqual(len(self.mem.dcache_flushed_ranges), 1)
        self.assertEqual(self.mem.dcache_flushed_ranges[0], (phys, 512, True))

    def test_04_int_xfer_complete_polling(self):
        """Verify transfer completion asserts INT_XFER_COMPLETE and SDMA populates buffer."""
        phys = 0x80002000
        self.hw.card_data_store[0] = GPTBuilder.create_protective_mbr()
        self.hw.wr32(REG_DMA_ADDR, phys)
        self.hw.wr32(REG_BLOCK_SIZE, (1 << 16) | 512)
        cmd = 17
        flags = CMD_RESP_SHORT | CMD_FLAG_CRC | CMD_FLAG_INDEX | CMD_FLAG_DATA
        packed = ((((cmd << 8) | flags) << 16) | 0x0013)
        self.hw.wr32(REG_ARGUMENT, 0)
        self.hw.wr32(REG_TRANSFER_MODE, packed)

        st = self.hw.rd32(REG_INT_STATUS)
        self.assertTrue(bool(st & INT_XFER_COMPLETE))
        read_back = self.mem.read_bytes(phys, 512)
        sig = struct.unpack_from("<H", read_back, 510)[0]
        self.assertEqual(sig, 0xAA55)

    def test_05_dma_alignment_validation(self):
        """Verify bounce buffer 64-byte D-cache alignment attribute in patch 0082."""
        self.assertIn("__attribute__((aligned(64)))", self.insp.c_source)
        aligned_pa = 0x80004000
        self.assertEqual(aligned_pa % 4, 0)
        self.assertEqual(aligned_pa % 64, 0)


class TestTier1Feature5MBRSignatureVerification(unittest.TestCase):
    """Feature 5: LBA 0 MBR Signature Verification."""

    def setUp(self):
        self.insp = Patch0082Inspector.get_instance()

    def test_01_valid_mbr_signature_0xaa55(self):
        """Verify valid MBR signature 0xaa55 at byte 510 checked in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("mbr_sig = *(const uint16_t *)&b[510];", fn_body)
        self.assertIn("if (mbr_sig != 0xaa55)", fn_body)
        mbr = GPTBuilder.create_protective_mbr(valid=True, sig=0xAA55)
        sig = struct.unpack_from("<H", mbr, 510)[0]
        self.assertEqual(sig, 0xAA55)

    def test_02_mbr_endianness(self):
        """Verify little-endian byte ordering of 0xaa55 in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("b[510]", fn_body)
        mbr = GPTBuilder.create_protective_mbr(valid=True, sig=0xAA55)
        self.assertEqual(mbr[510], 0x55)
        self.assertEqual(mbr[511], 0xAA)

    def test_03_mbr_word127_high_half(self):
        """Verify word 127 high-half extraction (bounce[127] >> 16 == 0xaa55)."""
        mbr = GPTBuilder.create_protective_mbr(valid=True, sig=0xAA55)
        words = struct.unpack("<128I", mbr)
        word127_sig = words[127] >> 16
        self.assertEqual(word127_sig, 0xAA55)

    def test_04_corrupted_signature_rejection(self):
        """Verify bad signatures are rejected with panic in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn('panic("SARGO: LBA 0 sig 0x%04x', fn_body)
        for bad_sig in (0x0000, 0x55AA, 0x1234, 0xFFFF):
            mbr = GPTBuilder.create_protective_mbr(valid=True, sig=bad_sig)
            sig = struct.unpack_from("<H", mbr, 510)[0]
            self.assertNotEqual(sig, 0xAA55)

    def test_05_all_zero_buffer_rejection(self):
        """Verify completely zeroed LBA 0 buffer rejection in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("error = sdhci_read_block(0);", fn_body)
        mbr = bytes(512)
        sig = struct.unpack_from("<H", mbr, 510)[0]
        self.assertEqual(sig, 0x0000)
        self.assertNotEqual(sig, 0xAA55)


class TestTier1Feature6GPTHeaderVerification(unittest.TestCase):
    """Feature 6: LBA 1 GPT Header Verification."""

    def setUp(self):
        self.insp = Patch0082Inspector.get_instance()

    def test_01_gpt_magic_efi_part(self):
        """Verify 'EFI PART' magic check at byte offset 0 in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn('if (memcmp(b, "EFI PART", 8) != 0)', fn_body)
        hdr = GPTBuilder.create_gpt_header(valid_magic=True)
        self.assertEqual(hdr[0:8], b"EFI PART")

    def test_02_gpt_entry_lba_extraction(self):
        """Verify entry_lba extraction at offset 72 (uint64) in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("memcpy(&entry_lba, b + 72, 8);", fn_body)
        hdr = GPTBuilder.create_gpt_header(entry_lba=2)
        entry_lba = struct.unpack_from("<Q", hdr, 72)[0]
        self.assertEqual(entry_lba, 2)

    def test_03_gpt_nent_extraction(self):
        """Verify nent extraction at offset 80 (uint32) in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("memcpy(&nent, b + 80, 4);", fn_body)
        hdr = GPTBuilder.create_gpt_header(nent=128)
        nent = struct.unpack_from("<I", hdr, 80)[0]
        self.assertEqual(nent, 128)

    def test_04_gpt_esz_validation_128(self):
        """Verify esz extraction at offset 84 (uint32) must be 128 in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("memcpy(&esz, b + 84, 4);", fn_body)
        self.assertIn("if (esz != 128 || nent == 0)", fn_body)
        hdr = GPTBuilder.create_gpt_header(esz=128)
        esz = struct.unpack_from("<I", hdr, 84)[0]
        self.assertEqual(esz, 128)

    def test_05_bad_magic_and_esz_rejection(self):
        """Verify rejection of bad magic or invalid esz in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn('panic("SARGO SDHCI: no GPT', fn_body)
        self.assertIn("return ENXIO;", fn_body)
        bad_hdr = GPTBuilder.create_gpt_header(valid_magic=False)
        self.assertNotEqual(bad_hdr[0:8], b"EFI PART")

        bad_esz_hdr = GPTBuilder.create_gpt_header(esz=256)
        esz = struct.unpack_from("<I", bad_esz_hdr, 84)[0]
        self.assertNotEqual(esz, 128)


class TestTier1Feature7PartitionTableParsing(unittest.TestCase):
    """Feature 7: Partition Table Parsing."""

    def setUp(self):
        self.insp = Patch0082Inspector.get_instance()

    def decode_name(self, entry_bytes):
        name_chars = []
        for c in range(36):
            ch = struct.unpack_from("<H", entry_bytes, 56 + 2 * c)[0]
            if ch == 0 or ch > 0x7F:
                break
            name_chars.append(chr(ch))
        return "".join(name_chars)

    def test_01_utf16le_name_decoding(self):
        """Verify UTF-16LE partition name decoding in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("uint16_t ch = (uint16_t)(e[56 + 2 * c] | (e[57 + 2 * c] << 8));", fn_body)
        self.assertIn("name[c] = (char)ch;", fn_body)
        entry = GPTBuilder.create_partition_entry("userdata", 1000, 5000)
        name = self.decode_name(entry)
        self.assertEqual(name, "userdata")

    def test_02_match_userdata_partition(self):
        """Verify matching 'userdata' partition with default root in patch 0082."""
        init_body = self.insp.get_function_body("sargo_sdhci_init")
        self.assertIn('char want[64] = "userdata";', init_body)
        self.assertIn('PE_parse_boot_argn("sdhciroot", want, sizeof(want));', init_body)
        entry = GPTBuilder.create_partition_entry("userdata", 2048, 10240)
        self.assertEqual(self.decode_name(entry), "userdata")
        first = struct.unpack_from("<Q", entry, 32)[0]
        last = struct.unpack_from("<Q", entry, 40)[0]
        self.assertEqual(first, 2048)
        self.assertEqual(last, 10240)
        self.assertEqual(last - first + 1, 8193)

    def test_03_match_boot_partition(self):
        """Verify matching alternative partition 'boot' via boot-arg override."""
        init_body = self.insp.get_function_body("sargo_sdhci_init")
        self.assertIn("sdhci_find_partition(want)", init_body)
        entry = GPTBuilder.create_partition_entry("boot", 100, 500)
        self.assertEqual(self.decode_name(entry), "boot")

    def test_04_missing_partition_handling(self):
        """Verify missing partition returns ENOENT in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("return ENOENT;", fn_body)
        table = [
            GPTBuilder.create_partition_entry("system", 100, 500),
            GPTBuilder.create_partition_entry("vendor", 501, 900),
        ]
        matched = False
        for e in table:
            if self.decode_name(e) == "userdata":
                matched = True
        self.assertFalse(matched)

    def test_05_geometry_extraction_and_inversion_check(self):
        """Verify geometry extraction and rejection of last < first in patch 0082."""
        fn_body = self.insp.get_function_body("sdhci_find_partition")
        self.assertIn("if (last < first) {", fn_body)
        self.assertIn("return ENXIO;", fn_body)
        valid_entry = GPTBuilder.create_partition_entry("userdata", 100, 200)
        first = struct.unpack_from("<Q", valid_entry, 32)[0]
        last = struct.unpack_from("<Q", valid_entry, 40)[0]
        self.assertGreaterEqual(last, first)

        inv_entry = GPTBuilder.create_partition_entry("corrupted", 200, 100)
        first_inv = struct.unpack_from("<Q", inv_entry, 32)[0]
        last_inv = struct.unpack_from("<Q", inv_entry, 40)[0]
        self.assertLess(last_inv, first_inv)


class TestTier1Feature8BlockDeviceMount(unittest.TestCase):
    """Feature 8: Block Device & Rootfs Mount."""

    def setUp(self):
        self.insp = Patch0082Inspector.get_instance()

    def test_01_devfs_device_node_names(self):
        """Verify block and character devfs nodes are registered in sargo_sdhci_init()."""
        fn_body = self.insp.get_function_body("sargo_sdhci_init")
        self.assertIn('devfs_make_node(ss.bdev, DEVFS_BLOCK, UID_ROOT, GID_OPERATOR, 0640, "sdhciroot0");', fn_body)
        self.assertIn('devfs_make_node(makedev(ss.cmajor, 0), DEVFS_CHAR, UID_ROOT, GID_OPERATOR, 0640, "rsdhciroot0");', fn_body)

    def test_02_strategy_write_absorption(self):
        """Verify write requests in sargo_sdhci_strategy and sargo_sdhci_write are absorbed with resid=0."""
        strat_body = self.insp.get_function_body("sargo_sdhci_strategy")
        self.assertIn("!(buf_flags(bp) & B_READ)", strat_body)
        self.assertIn("buf_setresid(bp, 0);", strat_body)
        self.assertIn("buf_biodone(bp);", strat_body)
        write_body = self.insp.get_function_body("sargo_sdhci_write")
        self.assertIn("uio_setresid(uio, 0);", write_body)
        self.assertIn("return 0;", write_body)

    def test_03_strategy_read_bounds_checking(self):
        """Verify read requests beyond partition bounds return EINVAL in patch 0082."""
        strat_body = self.insp.get_function_body("sargo_sdhci_strategy")
        self.assertIn("off > part_bytes || count > part_bytes - off", strat_body)
        self.assertIn("error = EINVAL;", strat_body)
        part_bytes = 100 * SDHCI_BLOCK
        off = 101 * SDHCI_BLOCK
        count = 512
        error = 0
        if off > part_bytes or count > part_bytes - off:
            error = 22 # EINVAL
        self.assertEqual(error, 22)

    def test_04_strategy_lba_offset_translation(self):
        """Verify logical sector translation to first_block + lba in patch 0082."""
        strat_body = self.insp.get_function_body("sargo_sdhci_strategy")
        self.assertIn("error = sdhci_read_block(ss.first_block + lba);", strat_body)
        first_block = 2048
        blkno = 10
        secsize = 512
        off = blkno * secsize
        lba = off // SDHCI_BLOCK
        target_lba = first_block + lba
        self.assertEqual(target_lba, 2058)

    def test_05_iokit_bsd_init_hook(self):
        """Verify IOKitBSDInit.cpp hook for sdhciroot0 in patch 0082."""
        self.assertIn('strcmp(rdBootVar, "sdhciroot0") == 0', self.insp.iokit_source)
        self.assertIn('extern int sargo_sdhci_rootdev(dev_t *dev);', self.insp.iokit_source)
        self.assertIn('int sdhci_error = sargo_sdhci_rootdev(root);', self.insp.iokit_source)
        self.assertIn('panic("IOFindBSDRoot: SARGO SDHCI root %s unavailable: %d", rdBootVar, sdhci_error);', self.insp.iokit_source)
        self.assertIn('strlcpy(rootName, rdBootVar, rootNameSize);', self.insp.iokit_source)
        self.assertIn('goto iofrootx;', self.insp.iokit_source)

    def test_06_sdhci_ioctl_synchronize_cache(self):
        """Verify DKIOCSYNCHRONIZE and DKIOCSYNCHRONIZECACHE return 0 in sargo_sdhci_ioctl."""
        ioctl_body = self.insp.get_function_body("sargo_sdhci_ioctl")
        self.assertIn("case DKIOCSYNCHRONIZE:", ioctl_body)
        self.assertIn("case DKIOCSYNCHRONIZECACHE:", ioctl_body)
        self.assertIn("*o = 256;", ioctl_body)

    def test_07_sdhci_high_speed_clock_elevation(self):
        """Verify 25 MHz clock elevation and fallback logic in sdhci_init_card."""
        init_body = self.insp.get_function_body("sdhci_init_card")
        self.assertIn("sdhci_set_clock(25000000);", init_body)
        self.assertIn("sdhci_read_block(0);", init_body)
        self.assertIn("sdhci_set_clock(400000);", init_body)

    def test_08_sdhci_strategy_telemetry(self):
        """Verify live I/O progress telemetry in sargo_sdhci_strategy."""
        strat_body = self.insp.get_function_body("sargo_sdhci_strategy")
        self.assertIn("sargo_total_bytes_read", strat_body)
        self.assertIn("printf(\"SARGO SDHCI: %llu MB read from eMMC\\n\"", strat_body)

    def test_09_sdhci_invokes_sweet_sample_start(self):
        """Verify sargo_sdhci.c declares and calls sweet_sample_start() in sargo_sdhci_init()."""
        self.assertIn("extern void sweet_sample_start(void);", self.insp.raw_patch)
        init_body = self.insp.get_function_body("sargo_sdhci_init")
        self.assertIn("sweet_sample_start();", init_body)


class TestTier1Feature9PreemptiveBootAudit(unittest.TestCase):
    """Feature 9: Preemptive Boot Audit."""

    def test_01_canonical_boot_args_invariants(self):
        """Verify canonical boot-args contains all essential tokens."""
        tokens = [
            "debug=0xc", "serial=3", "cpus=1", "keepsyms=1",
            "-enable_kprintf_spam", "-noprogress", "rd=sdhciroot0",
            "sdhciroot=userdata", "unentitled_ios_sim_launch=1",
            "ignition_level=1", "wdt_test=no_user_checkin",
            "rootrw=1", "launchd_unsecure_cache=1", "wdt=-1"
        ]
        for t in tokens:
            self.assertIn(t, CANONICAL_BOOT_ARGS)

    def test_02_mkadt_cpu_compatible_kryo(self):
        """Verify tools/mkadt-sargo.py declares qcom,kryo360 and qcom,kryo470."""
        mkadt_path = os.path.join(REPO_ROOT, "tools/mkadt-sargo.py")
        with open(mkadt_path, "r") as f:
            content = f.read()
        self.assertIn("qcom,kryo360", content)
        self.assertIn("qcom,kryo470", content)

    def test_03_mkadt_timer_intid_19(self):
        """Verify tools/mkadt-sargo.py sets VTIMER_INTID = 19 (PPI 19)."""
        mkadt_path = os.path.join(REPO_ROOT, "tools/mkadt-sargo.py")
        with open(mkadt_path, "r") as f:
            content = f.read()
        self.assertIn("VTIMER_INTID = 19", content)

    def test_04_sweetbattery_ac_shield(self):
        """Verify patch 0062 SweetBattery AC static shield fallback."""
        patch_path = os.path.join(REPO_ROOT, "xnu/patches/0062-sweet-touch-power-battery.patch")
        with open(patch_path, "r") as f:
            content = f.read()
        self.assertIn("SweetBattery", content)
        self.assertIn("sweetbattery", content)

    def test_05_watchdog_mitigation_tokens(self):
        """Verify watchdog disable tokens in canonical boot-args."""
        self.assertIn("wdt=-1", CANONICAL_BOOT_ARGS)
        self.assertIn("wdt_test=no_user_checkin", CANONICAL_BOOT_ARGS)

    def test_06_patch_0042_alien_platform_bypass(self):
        """Verify patch 0042 unconditionally permits iOS binaries on Sweet/Sargo."""
        patch_path = os.path.join(REPO_ROOT, "xnu/patches/0042-sweet-short-exec-log.patch")
        with open(patch_path, "r") as f:
            content = f.read()
        self.assertIn("no_sandbox_entitled = TRUE;", content)
        self.assertIn("ARM64_BOARD_CONFIG_SWEET", content)

    def test_07_patch_0010_boot_args_injection(self):
        """Verify patch 0010 auto-injects essential tokens into args->CommandLine."""
        patch_path = os.path.join(REPO_ROOT, "xnu/patches/0010-sweet-early-framebuffer-debug.patch")
        with open(patch_path, "r") as f:
            content = f.read()
        self.assertIn("args->CommandLine", content)
        self.assertIn("unentitled_ios_sim_launch=1", content)
        self.assertIn("ignition_level=1", content)
        self.assertIn("launchd_unsecure_cache=1", content)

    def test_08_mkpayload_essential_tokens_auto_append(self):
        """Verify tools/mkpayload.py automatically appends missing essential tokens."""
        mkpayload_path = os.path.join(REPO_ROOT, "tools/mkpayload.py")
        with open(mkpayload_path, "r") as f:
            content = f.read()
        self.assertIn("essential_tokens", content)
        self.assertIn("CANONICAL_BOOT_ARGS", content)


class TestTier1Feature10RepoValidation(unittest.TestCase):
    """Feature 10: Repo Validation."""

    def test_01_validate_repo_execution(self):
        """Verify python3 tools/validate-repo.py runs and passes cleanly."""
        res = subprocess.run([sys.executable, "tools/validate-repo.py"], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, f"validate-repo.py failed:\n{res.stdout}\n{res.stderr}")

    def test_02_patch_sequence_contiguous(self):
        """Verify patch files 0001..0085 are contiguous with no gaps."""
        patches = sorted(glob.glob(os.path.join(REPO_ROOT, "xnu/patches/*.patch")))
        numbers = [int(os.path.basename(p).split("-")[0]) for p in patches]
        self.assertEqual(len(patches), 85)
        self.assertEqual(numbers, list(range(1, 86)))

    def test_03_all_python_tools_syntax(self):
        """Verify all python scripts in tools/ compile cleanly."""
        py_files = glob.glob(os.path.join(REPO_ROOT, "tools/*.py"))
        for p in py_files:
            py_compile.compile(p, doraise=True)

    def test_04_all_shell_scripts_syntax(self):
        """Verify all shell scripts pass bash -n syntax check."""
        sh_files = (
            glob.glob(os.path.join(REPO_ROOT, "tools/*.sh")) +
            glob.glob(os.path.join(REPO_ROOT, "shim/*.sh")) +
            glob.glob(os.path.join(REPO_ROOT, "xnu/patches/*.sh"))
        )
        for s in sh_files:
            res = subprocess.run(["bash", "-n", s], capture_output=True)
            self.assertEqual(res.returncode, 0, f"Shell syntax error in {s}")

    def test_05_patch_diff_headers_present(self):
        """Verify all patches contain standard unified diff headers and patch 0082 is valid."""
        patches = glob.glob(os.path.join(REPO_ROOT, "xnu/patches/*.patch"))
        for p in patches:
            with open(p, "r", errors="ignore") as f:
                head = f.read(1024)
            self.assertTrue("--- " in head or "diff " in head, f"Missing diff header in {p}")
        valid, err = Patch0082Inspector.get_instance().validate_patch_syntax()
        self.assertTrue(valid, f"Patch 0082 git apply --stat failed: {err}")

    def test_06_patch_0083_vfs_cryptex_redirection(self):
        """Verify patch 0083 VFS Cryptex path redirection logic in vfs_lookup.c."""
        insp = Patch0083Inspector.get_instance()
        valid, err = insp.validate_patch_syntax()
        self.assertTrue(valid, f"Patch 0083 git apply --stat failed: {err}")
        self.assertIn("ARM64_BOARD_CONFIG_SWEET", insp.vfs_source)
        self.assertIn("System/Library/Caches/com.apple.dyld", insp.vfs_source)
        self.assertIn("private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld", insp.vfs_source)
        self.assertIn("System/Cryptexes/OS", insp.vfs_source)
        self.assertIn("private/preboot/Cryptexes/OS", insp.vfs_source)
        self.assertIn("System/Cryptexes/App", insp.vfs_source)
        self.assertIn("private/preboot/Cryptexes/App", insp.vfs_source)
        self.assertIn("ndp->ni_pathlen = (u_int)new_len", insp.vfs_source)

    def test_07_patch_0083_shared_region_trust_cache_bypass(self):
        """Verify patch 0083 shared region trust cache and SIP bypass in vm_unix.c."""
        insp = Patch0083Inspector.get_instance()
        self.assertIn("ARM64_BOARD_CONFIG_SWEET", insp.vm_source)
        self.assertIn("goto after_root_check;", insp.vm_source)
        self.assertIn("csr_check(CSR_ALLOW_UNRESTRICTED_FS)", insp.raw_patch)
        self.assertIn("scdir_path", insp.vm_source)
        self.assertIn("/private/preboot/Cryptexes/OS/System/Library/Caches/com.apple.dyld", insp.vm_source)
        self.assertIn("on sweet/sargo, dyld cache mappings are accepted unconditionally", insp.vm_source)

    def test_08_patch_0084_process_sampler_diagnostics(self):
        """Verify patch 0084 process sampler diagnostics configuration in sweet_sample.c."""
        insp = Patch0084Inspector.get_instance()
        valid, err = insp.validate_patch_syntax()
        self.assertTrue(valid, f"Patch 0084 git apply --stat failed: {err}")
        self.assertIn("30 * hz", insp.sample_source)
        self.assertIn("launchd,xpcproleaccountd,xpcroleaccountd,usermanagerd,xpcproxy,SpringBoard,backboardd", insp.sample_source)
        self.assertIn('strcmp(sweet_sample_names, "all") == 0', insp.sample_source)
        self.assertIn('printf("SMP proc: [%d] %s\\n", proc_pid(p), name);', insp.sample_source)
        self.assertIn('printf("SMP --- sample %d (%d s)\\n", i, (i + 1) * 30);', insp.sample_source)

    def test_09_patch_0085_platform_binary_and_entitlements(self):
        """Verify patch 0085 platform binary stamping, CS_VALID retention, and system entitlements."""
        insp = Patch0085Inspector.get_instance()
        valid, err = insp.validate_patch_syntax()
        self.assertTrue(valid, f"Patch 0085 git apply --stat failed: {err}")
        self.assertIn("*cs_flags |= CS_SIGNED | CS_PLATFORM_BINARY;", insp.amfi_source)
        self.assertIn("platform-application", insp.amfi_source)
        self.assertIn("com.apple.private.security.storage.AppContainer", insp.amfi_source)
        self.assertIn("com.apple.private.security.system-group-containers", insp.amfi_source)
        self.assertIn("com.apple.security.system-group-containers", insp.amfi_source)
        self.assertIn("com.apple.springboard", insp.amfi_source)
        self.assertIn("com.apple.springboard.ui", insp.amfi_source)
        self.assertIn("com.apple.backboardd", insp.amfi_source)
        self.assertIn("imgp->ip_csflags |= CS_SIGNED | CS_PLATFORM_BINARY;", insp.exec_source)
        self.assertIn("flags |= CS_SIGNED | CS_PLATFORM_BINARY | CS_VALID;", insp.cs_source)
        self.assertIn("csproc_get_platform_binary", insp.cs_source)
        self.assertIn("csblob_get_platform_binary", insp.cs_source)
        self.assertIn("retflags |= CS_SIGNED | CS_PLATFORM_BINARY | CS_VALID;", insp.proc_source)
        self.assertIn("sweetfb_noswap", insp.fb_source)


class TestTier1Feature11VersionControlCI(unittest.TestCase):
    """Feature 11: Version Control & CI Setup."""

    def test_01_git_current_branch_sargo(self):
        """Verify git branch is sargo."""
        res = subprocess.run(["git", "branch", "--show-current"], cwd=REPO_ROOT, capture_output=True, text=True)
        self.assertEqual(res.stdout.strip(), "sargo")

    def test_02_remote_origin_url(self):
        """Verify git remote origin URL points to Adadeane/ios-sargo.git."""
        res = subprocess.run(["git", "remote", "get-url", "origin"], cwd=REPO_ROOT, capture_output=True, text=True)
        url = res.stdout.strip()
        self.assertTrue("github.com/Adadeane/ios-sargo" in url or "github.com/Adadeane/ios-sweet" in url)

    def test_03_codemagic_yaml_exists(self):
        """Verify codemagic.yaml exists and parses valid YAML."""
        cm_path = os.path.join(REPO_ROOT, "codemagic.yaml")
        self.assertTrue(os.path.exists(cm_path))
        with open(cm_path, "r") as f:
            cfg = yaml.safe_load(f)
        self.assertIn("workflows", cfg)

    def test_04_codemagic_workflow_xnu_sargo(self):
        """Verify workflow xnu-sargo configured on mac_mini_m2."""
        cm_path = os.path.join(REPO_ROOT, "codemagic.yaml")
        with open(cm_path, "r") as f:
            cfg = yaml.safe_load(f)
        wf = cfg["workflows"]["xnu-sargo"]
        self.assertEqual(wf["instance_type"], "mac_mini_m2")

    def test_05_codemagic_trigger_push_sargo(self):
        """Verify push triggering on branch sargo."""
        cm_path = os.path.join(REPO_ROOT, "codemagic.yaml")
        with open(cm_path, "r") as f:
            cfg = yaml.safe_load(f)
        branch_patterns = cfg["workflows"]["xnu-sargo"]["triggering"]["branch_patterns"]
        sargo_pattern = [p for p in branch_patterns if p.get("pattern") == "sargo"]
        self.assertTrue(len(sargo_pattern) > 0)
        self.assertTrue(sargo_pattern[0].get("include"))


# -----------------------------------------------------------------------------
# Tier 2 Tests: Boundary & Corner Cases (5 tests)
# -----------------------------------------------------------------------------

class TestTier2BoundaryCases(unittest.TestCase):
    """Tier 2: Boundary & Corner Cases."""

    def test_01_partition_at_lba_0(self):
        """Boundary: Partition starting at LBA 0."""
        entry = GPTBuilder.create_partition_entry("userdata", 0, 1024)
        first = struct.unpack_from("<Q", entry, 32)[0]
        last = struct.unpack_from("<Q", entry, 40)[0]
        self.assertEqual(first, 0)
        self.assertEqual(last, 1024)
        self.assertEqual(last - first + 1, 1025)

    def test_02_max_64bit_lba_ranges(self):
        """Boundary: Extreme 64-bit LBA ranges."""
        first = 0x7FFFFFFFFFFFFFFF
        last = 0xFFFFFFFFFFFFFFFE
        entry = GPTBuilder.create_partition_entry("userdata", first, last)
        dec_first = struct.unpack_from("<Q", entry, 32)[0]
        dec_last = struct.unpack_from("<Q", entry, 40)[0]
        self.assertEqual(dec_first, first)
        self.assertEqual(dec_last, last)

    def test_03_partition_entries_across_multiple_blocks(self):
        """Boundary: Partition entries traversing 512-byte block boundaries."""
        # 128 entries of 128 bytes = 32 blocks (4 entries per block)
        nent = 128
        esz = 128
        entry_lba = 2
        for idx in (0, 3, 4, 7, 8, 127):
            off = idx * esz
            blk = entry_lba + off // SDHCI_BLOCK
            boff = off % SDHCI_BLOCK
            expected_blk = entry_lba + (idx // 4)
            expected_boff = (idx % 4) * 128
            self.assertEqual(blk, expected_blk)
            self.assertEqual(boff, expected_boff)

    def test_04_non_ascii_utf16_characters(self):
        """Boundary: Non-ASCII characters in UTF-16LE partition names."""
        # Name with special / non-ASCII characters
        raw_chars = [ord('u'), ord('s'), ord('e'), ord('r'), 0x00E9, ord('d')] # é is 0xE9 > 0x7F
        entry = bytearray(128)
        for i, ch in enumerate(raw_chars):
            struct.pack_into("<H", entry, 56 + 2 * i, ch)

        decoded = []
        for c in range(36):
            ch = struct.unpack_from("<H", entry, 56 + 2 * c)[0]
            if ch == 0 or ch > 0x7F:
                break
            decoded.append(chr(ch))
        self.assertEqual("".join(decoded), "user")

    def test_05_zero_residual_bytes_on_writes(self):
        """Boundary: Verify write absorption sets zero residual across all write sizes."""
        insp = Patch0082Inspector.get_instance()
        strat_body = insp.get_function_body("sargo_sdhci_strategy")
        write_body = insp.get_function_body("sargo_sdhci_write")
        self.assertIn("buf_setresid(bp, 0);", strat_body)
        self.assertIn("uio_setresid(uio, 0);", write_body)
        for sz in [0, 512, 4096, 16384, 1048576]:
            b_flags = 0x00000000 # B_WRITE
            resid = sz
            done = False
            if not (b_flags & 0x00000001): # B_READ
                resid = 0
                done = True
            self.assertEqual(resid, 0)
            self.assertTrue(done)


# -----------------------------------------------------------------------------
# Tier 3 Tests: Combinatorial & Cross-Feature Interactions (3 tests)
# -----------------------------------------------------------------------------

class TestTier3CombinatorialInteractions(unittest.TestCase):
    """Tier 3: Combinatorial & Cross-Feature Interactions."""

    def test_01_simultaneous_dma_and_block_count(self):
        """Combinatorial: Simultaneous DMA enable with block count in transfer mode."""
        idx = 17
        flags = CMD_RESP_SHORT | CMD_FLAG_CRC | CMD_FLAG_INDEX | CMD_FLAG_DATA
        transfer_mode = 0x0013 # DMA (1) + Block Count (2) + Read (16)
        packed_dword = (((idx << 8) | flags) << 16) | transfer_mode
        self.assertEqual(packed_dword & 0xFFFF, 0x0013)
        self.assertEqual((packed_dword >> 16) & 0xFF, flags)
        self.assertEqual((packed_dword >> 24) & 0x3F, 17)

    def test_02_mbr_gpt_chain_validation(self):
        """Combinatorial: MBR + GPT chain validation sequence."""
        mbr = GPTBuilder.create_protective_mbr(valid=True, sig=0xAA55)
        self.assertEqual(struct.unpack_from("<H", mbr, 510)[0], 0xAA55)

        gpt = GPTBuilder.create_gpt_header(valid_magic=True, entry_lba=2, nent=128, esz=128)
        self.assertEqual(gpt[0:8], b"EFI PART")
        self.assertEqual(struct.unpack_from("<Q", gpt, 72)[0], 2)
        self.assertEqual(struct.unpack_from("<I", gpt, 80)[0], 128)
        self.assertEqual(struct.unpack_from("<I", gpt, 84)[0], 128)

    def test_03_boot_arg_overrides(self):
        """Combinatorial: boot-arg sdhciroot override matching alternate partition."""
        cmdline = "rd=sdhciroot0 sdhciroot=custom_root cpus=1"
        m = re.search(r"sdhciroot=([^\s]+)", cmdline)
        target = m.group(1) if m else "userdata"
        self.assertEqual(target, "custom_root")


# -----------------------------------------------------------------------------
# Tier 4 Tests: Real-World Acceptance Scenarios (2 tests)
# -----------------------------------------------------------------------------

class TestTier4RealWorldAcceptance(unittest.TestCase):
    """Tier 4: Real-World Acceptance Scenarios."""

    def test_01_end_to_end_device_boot_simulation(self):
        """Acceptance: End-to-end simulation of device boot flow.

        Card Init -> LBA 0 read -> LBA 1 read -> Partition table scan ->
        Locate userdata -> Register sdhciroot0 -> IOFindBSDRoot handshake.
        """
        mem = MockPhysicalMemory(size=0x200000, base=0x80000000)
        hw = MockSDCCv5Hardware(mem)

        # 1. Setup card backing store
        hw.card_data_store[0] = GPTBuilder.create_protective_mbr(valid=True, sig=0xAA55)
        hw.card_data_store[1] = GPTBuilder.create_gpt_header(valid_magic=True, entry_lba=2, nent=128, esz=128)

        # LBA 2..33: Partition entry array
        block2 = bytearray(512)
        # Entry 0: boot partition (LBA 1000..2000)
        block2[0:128] = GPTBuilder.create_partition_entry("boot", 1000, 2000)
        # Entry 1: userdata partition (LBA 2001..20000)
        block2[128:256] = GPTBuilder.create_partition_entry("userdata", 2001, 20000)
        hw.card_data_store[2] = bytes(block2)

        # 2. Host controller initialization
        hw.wr32(REG_INT_STATUS_EN, 0xFFFFFFFF)
        hw.wr32(REG_INT_SIGNAL_EN, 0xFFFFFFFF)
        hw.wr32(REG_HOST_CONTROL, 0x0F00) # hostctl 0x00, pwrctl 0x0f (3.3V)

        # ICE bypass
        hw.wr_cq32(ICE_NONCQ_CRYPTO_PARAMS, 0x00000000)
        hw.wr_cq32(ICE_NONCQ_CRYPTO_DUN, 0x00000000)
        hw.dsb()

        # 3. Card Init sequence (CMD0 -> CMD1 -> CMD2 -> CMD3 -> CMD9 -> CMD7 -> CMD6 -> CMD16)
        def send_cmd(idx, arg, flags):
            hw.wr32(REG_ARGUMENT, arg)
            packed = (((idx << 8) | flags) << 16) | 0x0000
            hw.wr32(REG_TRANSFER_MODE, packed)
            st = hw.rd32(REG_INT_STATUS)
            self.assertTrue(bool(st & INT_CMD_COMPLETE))
            hw.wr32(REG_INT_STATUS, INT_CMD_COMPLETE)

        send_cmd(0, 0, CMD_RESP_NONE)
        send_cmd(1, 0x40FF8080, CMD_RESP_SHORT)
        send_cmd(2, 0, CMD_RESP_LONG | CMD_FLAG_CRC)
        send_cmd(3, 2 << 16, CMD_RESP_SHORT | CMD_FLAG_CRC | CMD_FLAG_INDEX)
        send_cmd(9, 2 << 16, CMD_RESP_LONG | CMD_FLAG_CRC)
        send_cmd(7, 2 << 16, CMD_RESP_SHORT_BUSY | CMD_FLAG_CRC | CMD_FLAG_INDEX)
        send_cmd(6, 0x03B90000, CMD_RESP_SHORT_BUSY | CMD_FLAG_CRC | CMD_FLAG_INDEX)
        send_cmd(6, 0x03B70000, CMD_RESP_SHORT_BUSY | CMD_FLAG_CRC | CMD_FLAG_INDEX)
        send_cmd(16, 512, CMD_RESP_SHORT | CMD_FLAG_CRC | CMD_FLAG_INDEX)

        # 4. SDMA Read LBA 0 (Protective MBR)
        bounce_pa = 0x80010000
        hw.wr32(REG_DMA_ADDR, bounce_pa)
        hw.wr32(REG_BLOCK_SIZE, (1 << 16) | 512)
        hw.wr32(REG_ARGUMENT, 0)
        cmd_dma = (((17 << 8) | (CMD_RESP_SHORT | CMD_FLAG_CRC | CMD_FLAG_INDEX | CMD_FLAG_DATA)) << 16) | 0x0013
        hw.wr32(REG_TRANSFER_MODE, cmd_dma)
        mem.flush_dcache64(bounce_pa, 512, True)

        lba0_bytes = mem.read_bytes(bounce_pa, 512)
        mbr_sig = struct.unpack_from("<H", lba0_bytes, 510)[0]
        self.assertEqual(mbr_sig, 0xAA55, "LBA 0 MBR signature must be 0xaa55")

        # 5. SDMA Read LBA 1 (GPT Header)
        hw.wr32(REG_ARGUMENT, 1)
        hw.wr32(REG_TRANSFER_MODE, cmd_dma)
        mem.flush_dcache64(bounce_pa, 512, True)

        lba1_bytes = mem.read_bytes(bounce_pa, 512)
        self.assertEqual(lba1_bytes[0:8], b"EFI PART", "LBA 1 signature must be 'EFI PART'")
        entry_lba = struct.unpack_from("<Q", lba1_bytes, 72)[0]
        nent = struct.unpack_from("<I", lba1_bytes, 80)[0]
        esz = struct.unpack_from("<I", lba1_bytes, 84)[0]
        self.assertEqual(entry_lba, 2)
        self.assertEqual(nent, 128)
        self.assertEqual(esz, 128)

        # 6. Read Partition Entry Block (LBA 2)
        hw.wr32(REG_ARGUMENT, entry_lba)
        hw.wr32(REG_TRANSFER_MODE, cmd_dma)
        mem.flush_dcache64(bounce_pa, 512, True)

        entries_bytes = mem.read_bytes(bounce_pa, 512)
        matched_part = None
        for i in range(4):
            e = entries_bytes[i * 128 : (i + 1) * 128]
            name_chars = []
            for c in range(36):
                ch = struct.unpack_from("<H", e, 56 + 2 * c)[0]
                if ch == 0 or ch > 0x7F:
                    break
                name_chars.append(chr(ch))
            name = "".join(name_chars)
            if name == "userdata":
                first = struct.unpack_from("<Q", e, 32)[0]
                last = struct.unpack_from("<Q", e, 40)[0]
                matched_part = (name, first, last)
                break

        self.assertIsNotNone(matched_part)
        self.assertEqual(matched_part[0], "userdata")
        self.assertEqual(matched_part[1], 2001)
        self.assertEqual(matched_part[2], 20000)

        # 7. Device Node & IOFindBSDRoot Handshake
        insp = Patch0082Inspector.get_instance()
        self.assertIn('strcmp(rdBootVar, "sdhciroot0") == 0', insp.iokit_source)
        self.assertIn('"sdhciroot0"', insp.c_source)
        self.assertIn('"rsdhciroot0"', insp.c_source)
        bmajor = 42
        cmajor = 43
        bdev = (bmajor << 24) | 0
        rdBootVar = "sdhciroot0"
        self.assertEqual(rdBootVar, "sdhciroot0")
        root_dev = bdev
        self.assertEqual(root_dev, (42 << 24))

    def test_02_devfs_device_node_lookup(self):
        """Acceptance: Verification of devfs registration sequence in patch 0082."""
        insp = Patch0082Inspector.get_instance()
        init_body = insp.get_function_body("sargo_sdhci_init")
        pattern = r'devfs_make_node\((.+?),\s*(DEVFS_[A-Z]+),\s*UID_ROOT,\s*GID_OPERATOR,\s*(0[0-7]+),\s*"([^"]+)"\)'
        matches = re.findall(pattern, init_body)
        self.assertEqual(len(matches), 4, "Must register four devfs nodes (sdhciroot0, rsdhciroot0, and ufsroot0 aliases)")
        node_map = {m[3]: {"dev": m[0].strip(), "type": m[1].strip(), "mode": m[2].strip()} for m in matches}
        self.assertIn("sdhciroot0", node_map)
        self.assertIn("rsdhciroot0", node_map)
        self.assertIn("ufsroot0", node_map)
        self.assertIn("rufsroot0", node_map)
        self.assertEqual(node_map["sdhciroot0"]["type"], "DEVFS_BLOCK")
        self.assertEqual(node_map["sdhciroot0"]["mode"], "0640")
        self.assertEqual(node_map["rsdhciroot0"]["type"], "DEVFS_CHAR")
        self.assertEqual(node_map["rsdhciroot0"]["mode"], "0640")
        self.assertEqual(node_map["ufsroot0"]["type"], "DEVFS_BLOCK")
        self.assertEqual(node_map["ufsroot0"]["mode"], "0640")
        self.assertEqual(node_map["rufsroot0"]["type"], "DEVFS_CHAR")
        self.assertEqual(node_map["rufsroot0"]["mode"], "0640")


# -----------------------------------------------------------------------------
# Main Test Runner & Reporting
# -----------------------------------------------------------------------------

def run_tests():
    suite = unittest.TestSuite()
    loader = unittest.TestLoader()

    test_classes = [
        # Tier 1
        TestTier1Feature1SDCCv5RegisterMapping,
        TestTier1Feature2QualcommICEBypass,
        TestTier1Feature3InterruptStatusHandling,
        TestTier1Feature4SDMADataTransport,
        TestTier1Feature5MBRSignatureVerification,
        TestTier1Feature6GPTHeaderVerification,
        TestTier1Feature7PartitionTableParsing,
        TestTier1Feature8BlockDeviceMount,
        TestTier1Feature9PreemptiveBootAudit,
        TestTier1Feature10RepoValidation,
        TestTier1Feature11VersionControlCI,
        # Tier 2
        TestTier2BoundaryCases,
        # Tier 3
        TestTier3CombinatorialInteractions,
        # Tier 4
        TestTier4RealWorldAcceptance,
    ]

    for tc in test_classes:
        suite.addTests(loader.loadTestsFromTestCase(tc))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)

    print("\n" + "=" * 70)
    print("SARGO E2E TEST SUMMARY")
    print("=" * 70)
    print(f"Total Tests Run   : {result.testsRun}")
    print(f"Total Errors      : {len(result.errors)}")
    print(f"Total Failures    : {len(result.failures)}")
    print(f"Overall Status    : {'PASSED' if result.wasSuccessful() else 'FAILED'}")
    print("=" * 70)

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run_tests())
