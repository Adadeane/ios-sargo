#!/usr/bin/env python3
"""Validate repository integrity, patch sequences, and script syntax.

  tools/validate-repo.py [--verbose]

Checks:
  1. Patch sequence in xnu/patches/:
     - Sequential numbering starting at 0001 with no duplicates or gaps.
     - Patch files are non-empty and have valid unified diff markers.
  2. Python script syntax:
     - All tools/*.py compile cleanly via py_compile.
  3. Shell script syntax:
     - All tools/*.sh, shim/*.sh, and xnu/patches/*.sh pass bash -n check.
"""
import argparse, glob, os, py_compile, re, subprocess, sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def green(text): return f"\033[92m{text}\033[0m"
def red(text): return f"\033[91m{text}\033[0m"
def bold(text): return f"\033[1m{text}\033[0m"


def validate_patch_structure(path):
    """Deep structural validation of a unified diff patch."""
    errors = []
    fname = os.path.basename(path)

    # 1. Native Git unified diff syntax validation (git apply --stat)
    try:
        res = subprocess.run(
            ['git', 'apply', '--stat', path],
            capture_output=True, text=True, check=False
        )
        if res.returncode != 0:
            errors.append(f"{fname}: git apply --stat rejected patch (code {res.returncode}): {res.stderr.strip()}")
    except FileNotFoundError:
        pass  # git not in PATH; fall back to internal python parser

    # 2. Comprehensive Python-level unified diff validation
    hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
    with open(path, 'r', errors='replace') as f:
        lines = f.readlines()

    in_hunk = False
    is_new_file = False
    old_cnt = new_cnt = cur_old = cur_new = 0
    hunk_line = 0

    for idx, line in enumerate(lines, 1):
        if line.startswith('--- /dev/null'):
            is_new_file = True
        elif line.startswith('--- ') and not line.startswith('--- /dev/null'):
            is_new_file = False

        if line.startswith('@@ '):
            if in_hunk:
                if cur_old != old_cnt or cur_new != new_cnt:
                    errors.append(f"{fname}:{hunk_line}: hunk line count mismatch: expected -{old_cnt}/+{new_cnt}, found -{cur_old}/+{cur_new}")
            m = hunk_re.match(line)
            if not m:
                errors.append(f"{fname}:{idx}: malformed hunk header: {line.strip()}")
                in_hunk = False
                continue
            in_hunk = True
            hunk_line = idx
            old_cnt = int(m.group(2)) if m.group(2) is not None else 1
            new_cnt = int(m.group(4)) if m.group(4) is not None else 1
            cur_old = 0
            cur_new = 0
        elif in_hunk:
            if line.startswith('diff ') or line.startswith('--- ') or line.startswith('Index:'):
                if cur_old != old_cnt or cur_new != new_cnt:
                    errors.append(f"{fname}:{hunk_line}: hunk line count mismatch: expected -{old_cnt}/+{new_cnt}, found -{cur_old}/+{cur_new}")
                in_hunk = False
            elif line.startswith('+'):
                cur_new += 1
            elif line.startswith('-'):
                if is_new_file:
                    errors.append(f"{fname}:{idx}: deletion line found in newly created file (/dev/null)")
                cur_old += 1
            elif line.startswith(' '):
                if is_new_file:
                    errors.append(f"{fname}:{idx}: context line found in newly created file (/dev/null)")
                cur_old += 1
                cur_new += 1
            elif line.startswith('\\ No newline at end of file'):
                pass
            else:
                errors.append(f"{fname}:{idx}: invalid diff line prefix {repr(line[:20])}")

    if in_hunk:
        if cur_old != old_cnt or cur_new != new_cnt:
            errors.append(f"{fname}:{hunk_line}: hunk line count mismatch: expected -{old_cnt}/+{new_cnt}, found -{cur_old}/+{cur_new}")

    return errors


def validate_patches(verbose=False):
    patch_dir = os.path.join(REPO_ROOT, 'xnu/patches')
    patch_files = sorted(glob.glob(os.path.join(patch_dir, '*.patch')))
    if not patch_files:
        print(red("[-] Error: No patch files found in xnu/patches/"))
        return False

    errors = []
    seen_numbers = {}

    for path in patch_files:
        fname = os.path.basename(path)
        m = re.match(r'^(\d{4})-(.+)\.patch$', fname)
        if not m:
            errors.append(f"Invalid patch filename format: {fname}")
            continue

        num = int(m.group(1))
        if num in seen_numbers:
            errors.append(f"Duplicate patch number {num:04d}: {fname} and {seen_numbers[num]}")
        seen_numbers[num] = fname

        size = os.path.getsize(path)
        if size == 0:
            errors.append(f"Patch is empty: {fname}")
            continue

        struct_errors = validate_patch_structure(path)
        if struct_errors:
            errors.extend(struct_errors)
        elif verbose:
            print(f"    [OK] {fname}")

    # Check contiguous sequence
    if seen_numbers:
        sorted_nums = sorted(seen_numbers.keys())
        expected = 1
        for num in sorted_nums:
            if num != expected:
                errors.append(f"Gap in patch sequence: expected {expected:04d}, found {num:04d} ({seen_numbers[num]})")
                expected = num + 1
            else:
                expected += 1

    if errors:
        print(red(f"[-] Patch validation failed with {len(errors)} error(s):"))
        for err in errors:
            print(f"    - {err}")
        return False

    print(green(f"[✓] Validated {len(patch_files)} patches (0001-{len(patch_files):04d} contiguous, format OK)"))
    return True


def validate_python_scripts(verbose=False):
    tools_dir = os.path.join(REPO_ROOT, 'tools')
    py_files = sorted(glob.glob(os.path.join(tools_dir, '*.py')))
    errors = []

    for path in py_files:
        fname = os.path.basename(path)
        try:
            py_compile.compile(path, doraise=True)
            if verbose:
                print(f"    [OK] {fname}")
        except py_compile.PyCompileError as e:
            errors.append(f"{fname}: {e}")

    if errors:
        print(red(f"[-] Python syntax validation failed with {len(errors)} error(s):"))
        for err in errors:
            print(f"    - {err}")
        return False

    print(green(f"[✓] Validated {len(py_files)} Python tools (syntax OK)"))
    return True


def validate_shell_scripts(verbose=False):
    sh_files = sorted(
        glob.glob(os.path.join(REPO_ROOT, 'tools/*.sh')) +
        glob.glob(os.path.join(REPO_ROOT, 'shim/*.sh')) +
        glob.glob(os.path.join(REPO_ROOT, 'xnu/patches/*.sh'))
    )
    errors = []

    for path in sh_files:
        rel = os.path.relpath(path, REPO_ROOT)
        res = subprocess.run(['bash', '-n', path], capture_output=True, text=True)
        if res.returncode != 0:
            errors.append(f"{rel}: {res.stderr.strip()}")
        elif verbose:
            print(f"    [OK] {rel}")

    if errors:
        print(red(f"[-] Shell syntax validation failed with {len(errors)} error(s):"))
        for err in errors:
            print(f"    - {err}")
        return False

    print(green(f"[✓] Validated {len(sh_files)} Shell scripts (syntax OK)"))
    return True


def main():
    ap = argparse.ArgumentParser(description="Validate repository integrity, patches, and scripts.")
    ap.add_argument('-v', '--verbose', action='store_true', help='Show detailed per-file output')
    a = ap.parse_args()

    print(bold("=== Running ios-sweet Repository Validation ==="))
    ok_patches = validate_patches(a.verbose)
    ok_python = validate_python_scripts(a.verbose)
    ok_shell = validate_shell_scripts(a.verbose)

    if ok_patches and ok_python and ok_shell:
        print(bold(green("\nAll repository integrity checks PASSED successfully!")))
        return 0
    else:
        print(bold(red("\nRepository integrity check FAILED.")))
        return 1


if __name__ == '__main__':
    sys.exit(main())
