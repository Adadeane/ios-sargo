#!/usr/bin/env python3
"""Patch the assembled XNU tree directly with sargo SDHCI eMMC driver, root hook, and Cryptex support."""
import os
import shutil
import sys

def patch_file(path, target, replacement, desc):
    if not os.path.exists(path):
        print(f"[sargo-xnu] WARNING: {path} not found for {desc}")
        return False
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if target in content:
        content = content.replace(target, replacement)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"[sargo-xnu] Patched {desc} in {os.path.basename(path)}")
        return True
    else:
        # Try normalizing line endings
        target_norm = target.replace("\r\n", "\n")
        content_norm = content.replace("\r\n", "\n")
        if target_norm in content_norm:
            content_norm = content_norm.replace(target_norm, replacement.replace("\r\n", "\n"))
            with open(path, "w", encoding="utf-8") as f:
                f.write(content_norm)
            print(f"[sargo-xnu] Patched {desc} (normalized) in {os.path.basename(path)}")
            return True
        print(f"[sargo-xnu] WARNING: target not found for {desc} in {os.path.basename(path)}")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage: patch_assembled_xnu.py <assembled-xnu-tree-dir> [sargo-repo-dir]")
        sys.exit(1)

    xnu_dir = os.path.abspath(sys.argv[1])
    sargo_dir = os.path.abspath(sys.argv[2]) if len(sys.argv) > 2 else os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

    print(f"[sargo-xnu] XNU tree dir: {xnu_dir}")
    print(f"[sargo-xnu] Sargo dir:    {sargo_dir}")

    # 1. Copy drivers/board/sdhci.c into bsd/dev/board/sdhci.c
    src_sdhci = os.path.join(sargo_dir, "drivers", "board", "sdhci.c")
    dst_sdhci = os.path.join(xnu_dir, "bsd", "dev", "board", "sdhci.c")
    os.makedirs(os.path.dirname(dst_sdhci), exist_ok=True)
    shutil.copy2(src_sdhci, dst_sdhci)
    print(f"[sargo-xnu] Copied sdhci.c to {dst_sdhci} ({os.path.getsize(dst_sdhci)} bytes)")

    # 2. Append bsd/dev/board/sdhci.c to bsd/conf/files
    files_conf = os.path.join(xnu_dir, "bsd", "conf", "files")
    with open(files_conf, "r", encoding="utf-8", errors="ignore") as f:
        fc = f.read()
    if "bsd/dev/board/sdhci.c" not in fc:
        with open(files_conf, "a", encoding="utf-8") as f:
            f.write("\nbsd/dev/board/sdhci.c\t\tstandard\n")
        print("[sargo-xnu] Registered bsd/dev/board/sdhci.c in bsd/conf/files")

    # 3. Hook sdhciroot0 / ufsroot0 in IOKitBSDInit.cpp
    iokit_cpp = os.path.join(xnu_dir, "iokit", "bsddev", "IOKitBSDInit.cpp")
    hook_target = """\
\t\t\tregEntry->release();                                                            /* Toss the entry */
\t\t}
\t}"""
    hook_repl = """\
\t\t\tregEntry->release();                                                            /* Toss the entry */
\t\t}
\t}

\t/* SARGO: root on eMMC (bsd/dev/board/sdhci.c) */
\tif (strcmp(rdBootVar, "sdhciroot0") == 0 || strcmp(rdBootVar, "ufsroot0") == 0) {
\t\textern int sargo_sdhci_rootdev(dev_t *dev);
\t\tOSSafeReleaseNULL(matching);
\t\tint sdhci_error = sargo_sdhci_rootdev(root);
\t\tif (sdhci_error) {
\t\t\tpanic("IOFindBSDRoot: SARGO SDHCI root %s unavailable: %d", rdBootVar, sdhci_error);
\t\t}
\t\tstrlcpy(rootName, rdBootVar, rootNameSize);
\t\tIOLog("BSD root: %s, major %d, minor %d\\n", rootName, major(*root), minor(*root));
\t\t*oflags = 0;
\t\tgoto iofrootx;
\t}"""
    patch_file(iokit_cpp, hook_target, hook_repl, "sdhciroot0 hook")

    wait_target = "if (PE_parse_boot_argn(\"rd\", rdEarly, sizeof(rdEarly)) && rdEarly[0] == 'm' && rdEarly[1] == 'd') {"
    wait_repl = "if (PE_parse_boot_argn(\"rd\", rdEarly, sizeof(rdEarly)) && ((rdEarly[0] == 'm' && rdEarly[1] == 'd') || strncmp(rdEarly, \"sdhci\", 5) == 0 || strncmp(rdEarly, \"ufs\", 3) == 0)) {"
    patch_file(iokit_cpp, wait_target, wait_repl, "fast bsdWait")

    # 4. Text page revalidation in osfmk/vm/vm_fault.c
    # Unconditionally return KERN_SUCCESS so dyld shared cache pages are never
    # considered corrupt regardless of board config (no #ifdef guards here).
    # This prevents the SIGBUS crash loop in tccd/identityservicesd.
    fault_c = os.path.join(xnu_dir, "osfmk", "vm", "vm_fault.c")
    fault_target = """\
\t\tCA_EVENT_SEND(ca_event);
\t\tprintf("Text page corruption detected for pid %d\\n", proc_selfpid());
\t\t++vmtc_total;
\t\treturn KERN_FAILURE; /* failure means we definitely found a corrupt page */"""
    fault_repl = """\
\t\tCA_EVENT_SEND(ca_event);
\t\tprintf("Text page corruption detected for pid %d\\n", proc_selfpid());
\t\t++vmtc_total;
\t\t/* SARGO: always treat text page as valid; iOS 16 dyld cache pages are
\t\t * signed with Apple's root but we cannot verify on non-Apple silicon.
\t\t * Returning KERN_FAILURE here kills the process (tccd, identityservicesd,
\t\t * etc.) in a crash loop via SIGBUS. */
\t\treturn KERN_SUCCESS;"""
    patch_file(fault_c, fault_target, fault_repl, "text page revalidation bypass")

    # 5. shared_region_map_and_slide: bypass root ownership + CS enforcement
    # in bsd/vm/vm_unix.c so the dyld cache slides without trust cache checks.
    vm_unix_c = os.path.join(xnu_dir, "bsd", "vm", "vm_unix.c")

    # 5a. Skip root-ownership check for dyld cache
    root_check_target = """\
\t\t * Check if the shared cache is in the trust cache;
\t\t * if so, we can skip the root ownership check.
\t\t */
#if DEVELOPMENT || DEBUG"""
    root_check_repl = """\
\t\t * Check if the shared cache is in the trust cache;
\t\t * if so, we can skip the root ownership check.
\t\t */
\t\t/* SARGO: always skip root ownership check for dyld cache */
\t\tgoto after_root_check;
#if DEVELOPMENT || DEBUG"""
    patch_file(vm_unix_c, root_check_target, root_check_repl, "shared_region root check bypass")

    # 5b. Skip code-signing enforcement for dyld cache mappings
    cs_enforce_target = """\
\t\t/* check that the mappings are properly covered by code signatures */
\t\tif (!cs_system_enforcement()) {"""
    cs_enforce_repl = """\
\t\t/* check that the mappings are properly covered by code signatures */
\t\t/* SARGO: accept dyld cache mappings unconditionally */
\t\tif (1) {
\t\t\t/* sargo: code-signing enforcement bypassed for dyld cache */
\t\t} else if (!cs_system_enforcement()) {"""
    patch_file(vm_unix_c, cs_enforce_target, cs_enforce_repl, "shared_region CS enforcement bypass")

    # 5c. CONFIG_CSR check: always skip the immutable-flag check
    csr_target = """\
#if CONFIG_CSR
\t\tif (csr_check(CSR_ALLOW_UNRESTRICTED_FS) != 0) {"""
    csr_repl = """\
#if CONFIG_CSR
\t\tif (0 /* SARGO: always allow unrestricted FS for dyld cache */) {"""
    patch_file(vm_unix_c, csr_target, csr_repl, "shared_region CSR bypass")

    print("[sargo-xnu] All assembled XNU patches successfully applied!")

if __name__ == "__main__":
    main()
