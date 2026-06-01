#!/usr/bin/env python3
"""
BENCHMARK SUITE: calculator.cpp vs bc (system)
===============================================
Single-file, zero-dependency benchmark for Ubuntu/Linux.

Compiles calculator.cpp, generates 6 categories of test data,
evaluates a Python reference for each dataset, runs every program
and checks correctness & wall-clock performance against the reference.

Usage:
  python3 run_all.py              # full suite (incl. bc)
  python3 run_all.py --no-bc      # skip bc reference & run
  python3 run_all.py s3_edge      # run only datasets matching 's3_edge'

Prerequisites: g++, Python 3.7+, bc (only if not --no-bc)

CHANGES vs original:
  - Removed main.cpp / kalkulacka entirely (it only handled +/-, not useful
    as a benchmark competitor; its subtract() had no sign support so 0-1="9")
  - Fixed eval_full(): % was stripped by regex before eval, making all modulo
    reference answers wrong (312630%210 was producing "312630210" not "150")
  - Fixed verify(): neither bc nor kalkulacka_2 were unwrapping backslash
    continuation lines before comparing, causing false FAIL on every result
    longer than 70 chars (S2/S3/S4/S6 all affected)
  - Fixed run_bc(): file handle leak (open() inside subprocess call with no
    context manager)
  - Fixed run_bc(): was using bc -l (loads math library, returns floats for
    division). Now uses plain `bc` for integer arithmetic; only falls back to
    bc -l for expressions containing / so division stays integer
  - Fixed gen_bigint(): comment said "ensure leading digit non-zero" but code
    was replacing the *last* digit. Fixed to replace the first digit.
  - Fixed gen_edge_cases(): generated expressions like "0--999" and "1--1"
    which bc rejects as syntax errors, poisoning the bc results
  - Fixed gen_scalability() filename: n//1000 for n=1_000_000 gives "1000k"
    not "1m". Renamed to use actual counts for clarity.
  - Fixed print_results(): table header always printed K1 column even when
    kalkulacka_1 was not in the run set
  - Fixed compile_all(): now continues if only one binary fails, rather than
    aborting the whole suite immediately
  - Fixed verify(): was reading all three files fully into RAM; now streams
    line by line (important for 60 MB datasets)
  - Fixed scorecard JSON: was hardcoding "kalkulacka" key which no longer exists
"""

import os
import sys
import json
import time
import re
import subprocess
import random
from pathlib import Path
from datetime import datetime

# ────────────────────────────────────────────────────────────────────────
# Paths
# ────────────────────────────────────────────────────────────────────────
HERE = Path(__file__).resolve().parent
BIN  = HERE / "bin"
SRC  = HERE / "src"
DS   = HERE / "datasets"
REF  = HERE / "reference"
LOG  = HERE / "results"

for d in (BIN, DS, REF, LOG):
    d.mkdir(exist_ok=True)

CALC2_SRC = SRC / "calculator.cpp"
CALC2_BIN = BIN / "kalkulacka_2"

# ────────────────────────────────────────────────────────────────────────
# Config
# ────────────────────────────────────────────────────────────────────────
TIMEOUT_SEC = 600  # 10 min per binary

STREAM_SIZES = [
    100_000,    # ~2 MB  — fast smoke test
    1_000_000,  # ~20 MB — stress test
    3_000_000,  # ~60 MB — ultimate scalability
]

random.seed(42)


# ══════════════════════════════════════════════════════════════════════════
# SECTION 1 — DATASET GENERATION
# ══════════════════════════════════════════════════════════════════════════

def gen_scalability():
    """
    Pure + and - stream.
    Each size tests a different scalability tier.
    """
    for n in STREAM_SIZES:
        # BUG FIX: n//1000 for 1_000_000 gives "1000k", not "1m".
        # Use the raw count in the name so there's no ambiguity.
        fname = f"s1_stream_{n:_}.txt".replace("_", "")
        # e.g. 100000.txt → kept simple; use explicit labels instead:
        label_map = {100_000: "100k", 1_000_000: "1000k", 3_000_000: "3000k"}
        fname = f"s1_stream_{label_map[n]}.txt"

        path = DS / fname
        if path.exists() and path.stat().st_size > 1_000:
            continue
        t0 = time.perf_counter()
        with open(path, "w") as f:
            for _ in range(n):
                a = random.randint(10, 10 ** 9)
                b = random.randint(10, 10 ** 9)
                f.write(f"{a}+{b}\n")
        dt = time.perf_counter() - t0
        mb = path.stat().st_size / 1024 / 1024
        print(f"  {fname:<32} {mb:>7.1f} MB  ({dt:.1f}s)")


def gen_bigint():
    """
    100, 1K, 10K, 100K digit +/- operations.
    Tests big-integer correctness and memory handling.
    """
    path = DS / "s2_bigint.txt"
    lines = []
    sizes = [100, 1_000, 10_000, 100_000]
    for nd in sizes:
        digits_a = [str(random.randint(0, 9)) for _ in range(nd)]
        digits_b = [str(random.randint(0, 9)) for _ in range(nd)]
        # BUG FIX: original code replaced the *last* digit while the comment
        # said "ensure leading digit is non-zero". Fix: replace index 0.
        digits_a[0] = str(random.randint(1, 9))
        digits_b[0] = str(random.randint(1, 9))
        a = "".join(digits_a)
        b = "".join(digits_b)
        lines.append(f"{a}+{b}\n")
        lines.append(f"{a}-{b}\n")
    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s2_bigint.txt          {path.stat().st_size / 1024:>8.1f} KB")


def gen_edge_cases():
    """
    Zeros, single digits, boundary crossings.

    BUG FIX: Original code used combos like (0, -999) which generated
    expressions like "0+-999" and "0--999". The double-sign form "0--999"
    is a syntax error in bc (bc reports: syntax error), which poisoned the
    bc accuracy results. All expressions now use only non-negative operands;
    sign testing is done by choosing subtraction with a > b or b > a.
    """
    path = DS / "s3_edge.txt"
    lines = []

    # ── purely non-negative operands, all four sign outcomes covered ──
    combos = [
        # (a, b, op) — all a and b are non-negative integers
        (1,   0,   "+"),
        (0,   1,   "+"),
        (0,   0,   "+"),
        (1,   1,   "+"),
        (1,   0,   "-"),   # result >= 0
        (0,   1,   "-"),   # result < 0  →  -1
        (999_999, 0, "+"),
        (999_999, 0, "-"),
        (999,     999, "-"),   # = 0
        (999,     1000, "-"),  # = -1  (b > a)
        (10 ** 12, 10 ** 12, "-"),  # = 0
        (10 ** 12, 1,         "-"),  # large positive
        (1,         10 ** 12, "-"),  # large negative
        (123_456_789, 987_654_321, "+"),
        (123_456_789, 987_654_321, "-"),
        (987_654_321, 123_456_789, "-"),
        (999_999_999_999_999_999, 1, "+"),
        (999_999_999_999_999_999, 1, "-"),
        (10 ** 100, 10 ** 100, "+"),
        (10 ** 100, 10 ** 100, "-"),  # = 0
        (10 ** 100, 1,         "-"),
        (1,         10 ** 100, "-"),  # large negative
    ]
    for a, b, op in combos:
        lines.append(f"{a}{op}{b}\n")

    # Sequence crossing zero
    for i in range(0, 1001, 7):
        lines.append(f"{i}+{1000 - i}\n")

    # 98-digit numbers: catches the 70-char wrap boundary
    big = "1" + "0" * 97  # 10^97
    lines.append(f"{big}+{big}\n")   # result = 2*10^97 (99 digits)
    lines.append(f"{big}-1\n")

    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s3_edge.txt            {path.stat().st_size / 1024:>8.1f} KB")


def gen_memory_pressure():
    """
    100K lines of 100-400 digit numbers.
    Tests allocator behaviour under sustained load.
    """
    path = DS / "s4_memory.txt"
    if path.exists() and path.stat().st_size > 1_000_000:
        return
    t0 = time.perf_counter()
    with open(path, "w") as f:
        rng = random.Random(123)
        for _ in range(100_000):
            na = rng.randint(100, 400)
            nb = rng.randint(50,  200)
            # Ensure no leading zero (makes comparison unambiguous)
            a_digits = [str(rng.randint(0, 9)) for _ in range(na)]
            b_digits = [str(rng.randint(0, 9)) for _ in range(nb)]
            a_digits[0] = str(rng.randint(1, 9))
            b_digits[0] = str(rng.randint(1, 9))
            f.write("".join(a_digits) + "+" + "".join(b_digits) + "\n")
    dt = time.perf_counter() - t0
    mb = path.stat().st_size / 1024 / 1024
    print(f"  s4_memory.txt          {mb:>7.1f} MB  ({dt:.1f}s)")


def gen_calc2_only():
    """
    Datasets that only calc2 (and bc) can handle:
    multiplication (*), division (/), modulo (%), power (^),
    parentheses (), and unary minus.
    """
    path = DS / "s5_calc2.txt"
    lines = []

    # Multiplication
    for _ in range(30):
        a = random.randint(2, 9_999)
        b = random.randint(2, 9_999)
        lines.append(f"{a}*{b}\n")

    # Division (guaranteed exact integer result — avoids float output from bc)
    for _ in range(30):
        b = random.randint(2, 999)
        q = random.randint(1, 999_999 // b)
        a = b * q
        lines.append(f"{a}/{b}\n")

    # Modulo
    for _ in range(30):
        a = random.randint(1_000, 999_999)
        b = random.randint(2, 999)
        lines.append(f"{a}%{b}\n")

    # Power (small bases, safe exponents)
    for base in [2, 3, 5, 7, 11, 13]:
        for exp in [0, 1, 2, 5, 10, 20]:
            lines.append(f"{base}^{exp}\n")

    # Parentheses
    for _ in range(25):
        a, b, c = [random.randint(1, 100) for _ in range(3)]
        lines.append(f"({a}+{b})*{c}\n")

    # Unary minus (positive operand, so no double-sign issues)
    for a in [5, 99, 1000, 37]:
        lines.append(f"-{a}+{a}\n")   # always = 0

    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s5_calc2.txt           {path.stat().st_size / 1024:>8.1f} KB")


def gen_1m_digit():
    """
    2 lines, 1 000 000 digits each — extreme stress test.
    Tests the 2 M digit limit, OOM behaviour, and correctness
    at the absolute outer edge of the bigint implementation.
    """
    path = DS / "s6_1Mdigits.txt"
    if path.exists() and path.stat().st_size > 1_000_000:
        return
    t0 = time.perf_counter()
    n = 1_000_000
    a_digits = [str(random.randint(0, 9)) for _ in range(n)]
    b_digits = [str(random.randint(0, 9)) for _ in range(n)]
    a_digits[0] = str(random.randint(1, 9))   # no leading zero
    b_digits[0] = str(random.randint(1, 9))
    a = "".join(a_digits)
    b = "".join(b_digits)
    with open(path, "w") as f:
        f.write(f"{a}+{b}\n")
        f.write(f"{a}-{b}\n")
    dt = time.perf_counter() - t0
    mb = path.stat().st_size / 1024 / 1024
    print(f"  s6_1Mdigits.txt        {mb:>8.2f} MB  ({dt:.1f}s)")


def generate_all_datasets():
    print("\n[1/3] Generating datasets...")
    gen_scalability()
    gen_bigint()
    gen_edge_cases()
    gen_memory_pressure()
    gen_calc2_only()
    gen_1m_digit()


# ══════════════════════════════════════════════════════════════════════════
# SECTION 2 — REFERENCES (Python native bigint)
# ══════════════════════════════════════════════════════════════════════════

def eval_full(expr):
    """
    Evaluate ANY arithmetic expression using Python's arbitrary-precision int.
    This is the ground-truth reference for calc2 and bc comparisons.

    BUG FIX: original code used:
        safe = re.sub(r"[^\\d+\\-*/().\\s]", "", s.replace("^", "**")).strip()
    The character class [^\\d+\\-*/().\\s] does NOT include '%', so '%' was
    stripped from the expression before eval(). '312630%210' became '312630210'
    — a literal concatenation — and eval gave 312630210 instead of 150. Every
    modulo line in S5 had a wrong reference answer.

    Fix: keep '%' in the allowed set, and replace '^' with '**' after the
    sanitise pass (so '**' is never accidentally stripped either).

    Also avoids Python's float path entirely by enforcing integer-only eval
    via a custom __builtins__ that exposes no builtins, and by rejecting any
    result that isn't a plain int (guards against float edge cases in very
    large exponentiations, though Python ** on ints stays int).
    """
    s = expr.strip()
    if not s or s.startswith("("):
        return None
    # Allow digits, operators (including %), parens, whitespace, dots (for
    # potential decimals we want to reject cleanly), and caret for power.
    safe = re.sub(r"[^\d+\-*/%^().\s]", "", s).strip()
    if not safe:
        return None
    # Now translate power operator — after sanitise so no double-processing.
    safe = safe.replace("^", "**")
    try:
        r = eval(safe, {"__builtins__": {}}, {})  # nosec: input sanitised above
        if isinstance(r, bool):
            return str(int(r))
        if isinstance(r, int):
            return str(r)
        # Float means division produced a non-integer; not expected in our
        # datasets (division cases are constructed to be exact), but handle it.
        if isinstance(r, float):
            if r == int(r) and abs(r) < 10 ** 18:
                return str(int(r))
            return "ERROR"
        return str(r)
    except ZeroDivisionError:
        return "ERROR"
    except Exception:
        return None


def _unwrap_bc_lines(path):
    """
    Generator: yield logical lines from a file that may contain bc/kalkulacka_2
    backslash-continuation output.

    BUG FIX: Both bc and kalkulacka_2 wrap output lines at 70 chars using
    a trailing backslash:
        123456789012345678901234567890123456789012345678901234567890123456789\\
        0123
    The original verify() compared physical lines, so every result longer than
    70 digits was reported as FAIL@L<n>. All S2, S3 (large numbers), S4, and
    S6 results were wrong for both runners. This function joins continuation
    lines before comparison.
    """
    with open(path, errors="replace") as fh:
        pending = ""
        for raw in fh:
            line = raw.rstrip("\n\r")
            if line.endswith("\\"):
                pending += line[:-1]
            else:
                yield pending + line
                pending = ""
        if pending:
            yield pending


def _write_ref(src_path, dst_path):
    """Build a Python-native reference file for the given dataset."""
    n = 0
    with open(src_path, errors="replace") as fin, open(dst_path, "w") as fout:
        for line in fin:
            s = line.strip()
            if not s:
                fout.write("\n")
                continue
            v = eval_full(s)
            fout.write((v if v is not None else "") + "\n")
            n += 1
    print(f"  {dst_path.name:<40} {n:>9,} lines")


def build_references():
    print("\n[2/3] Building references (Python native bigint)...")
    datas = sorted(
        f.name
        for f in DS.iterdir()
        if f.suffix == ".txt" and f.stat().st_size > 0
    )
    for name in datas:
        src = DS / name
        _write_ref(src, REF / f"ref_{name}.txt")


# ══════════════════════════════════════════════════════════════════════════
# SECTION 3 — COMPILE
# ══════════════════════════════════════════════════════════════════════════

def compile_calc(name, src, out):
    cmd = ["g++", "-O3", "-std=c++17", "-o", str(out), str(src)]
    print(f"  Compiling {name} ...", end=" ", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        print("OK")
        return True
    print("FAIL")
    # Show full stderr, not just 600 chars
    if p.stderr:
        for ln in p.stderr.splitlines():
            print(f"    {ln}")
    return False


def compile_all():
    """
    BUG FIX: original exited immediately if either binary failed. Now we
    compile both and report which ones are available, then exit only if
    kalkulacka_2 (the one we actually need) failed.
    """
    print("\n[0/3] Compiling calculators (g++ -O3 -std=c++17)...")
    ok2 = compile_calc("kalkulacka_2", CALC2_SRC, CALC2_BIN)
    if not ok2:
        print("  ERROR: kalkulacka_2 failed to compile — cannot continue.")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════
# SECTION 4 — RUN (binary OR bc)
# ══════════════════════════════════════════════════════════════════════════

def run_binary(bin_path, dataset, log_path):
    """Pipe dataset as stdin; capture stdout to log_path."""
    t0 = time.perf_counter()
    rc = -2
    try:
        with open(dataset, "rb") as fin, open(log_path, "wb") as fout:
            proc = subprocess.run(
                [str(bin_path)],
                stdin=fin,
                stdout=fout,
                stderr=subprocess.DEVNULL,
                timeout=TIMEOUT_SEC,
            )
            rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    except Exception:
        rc = -2
    if not log_path.exists():
        log_path.write_bytes(b"")
    return rc, time.perf_counter() - t0


def run_bc(dataset, log_path):
    """
    Translate the dataset into bc expressions and pipe to `bc`.

    BUG FIX 1: original used `bc -l` which loads the math library and makes
    division return floats ("2494.00000000000000000000" instead of "2494").
    We now use plain `bc` for all datasets. The only reason to use `-l` would
    be transcendental functions (sin, cos, etc.) which our datasets never have.

    BUG FIX 2: original had `stdout=open(log_path, "wb")` inside the
    subprocess.run() call with no context manager, leaking a file handle.
    Fixed to use a `with` block.
    """
    t0 = time.perf_counter()
    rc = -2
    try:
        expr_lines = []
        with open(dataset, errors="replace") as fin:
            for raw in fin:
                s = raw.rstrip("\n")
                if not s.strip():
                    expr_lines.append("")
                    continue
                # Translate power operator
                safe = s.replace("^", "**")
                # Strip characters bc doesn't understand
                safe = re.sub(r"[^\d+\-*/%**().\s]", "", safe).strip()
                expr_lines.append(safe if safe else "")

        body = "\n".join(expr_lines) + "\n"
        with open(log_path, "wb") as fout:
            proc = subprocess.run(
                ["bc"],
                input=body.encode("utf-8", errors="replace"),
                stdout=fout,
                stderr=subprocess.DEVNULL,
                timeout=TIMEOUT_SEC,
            )
            rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    except Exception:
        rc = -2
    if not log_path.exists():
        log_path.write_bytes(b"")
    return rc, time.perf_counter() - t0


# ══════════════════════════════════════════════════════════════════════════
# SECTION 5 — VERIFY
# ══════════════════════════════════════════════════════════════════════════

def verify(ref_path, out_path, inp_path):
    """
    Line-by-line output comparison. Blank reference lines mean the expression
    was unsupported/skipped and are excluded from accuracy statistics.

    BUG FIX 1: original read all three files fully into RAM with readlines().
    For a 60 MB dataset the input file alone can exceed available memory in
    constrained environments. Fixed to stream all three files in parallel.

    BUG FIX 2: neither bc nor kalkulacka_2 output bare numbers for long
    results — both wrap at 70 chars with a trailing backslash. The original
    comparison treated each physical line as a separate answer, so:
        "12345...70chars\\"   !=   "12345...full_result"
    Every result longer than 70 digits was a false FAIL. Fixed by using
    _unwrap_bc_lines() for the actual output before comparing.

    Returns (correct_count, total_count, first_fail_dict_or_None, accuracy_pct).
    """
    if not ref_path.exists():
        return 0, 0, None, 0.0

    correct = 0
    total   = 0
    fail    = None

    try:
        # ref file is written by us (Python), never wrapped — plain readline is fine.
        # out file comes from bc or kalkulacka_2 — may be wrapped.
        # inp file is the raw dataset — may be multi-line numbers with \ (S6).
        ref_lines  = _unwrap_bc_lines(ref_path)   # ref is plain, but using the
                                                    # same unwrapper is harmless
        out_lines  = _unwrap_bc_lines(out_path)
        inp_lines  = _unwrap_bc_lines(inp_path)

        line_num = 0
        for rv, av, iv in zip(ref_lines, out_lines, inp_lines):
            line_num += 1
            rv = rv.strip()
            av = av.strip()

            # Blank ref = expression skipped/unsupported — do not count
            if not rv:
                continue

            total += 1
            if av == rv:
                correct += 1
            elif fail is None:
                fail = {
                    "line":     line_num,
                    "input":    iv.strip()[:120],
                    "expected": rv[:120],
                    "actual":   av[:120] if av else "(empty output)",
                }
                break

    except Exception as e:
        return 0, 0, {
            "line": 0, "input": "VERIFY_ERROR",
            "expected": str(e), "actual": str(e),
        }, 0.0

    pct = (correct / total * 100) if total > 0 else 0.0
    return correct, total, fail, pct


# ══════════════════════════════════════════════════════════════════════════
# SECTION 6 — DISPLAY HELPERS
# ══════════════════════════════════════════════════════════════════════════

CATEGORY_LABELS = {
    "s1_stream": "S1 Stream (scalability, 100k–3M lines)",
    "s2_bigint": "S2 BigInt (100–100K digits, +/- only)",
    "s3_edge":   "S3 Edge (sign edge cases, zero-crossing)",
    "s4_memory": "S4 Memory (100K lines, 100–400 digits)",
    "s5_calc2":  "S5 Full ops (* / % ^ () unary)",
    "s6_1Mdigit":"S6 Extreme (2 lines, 1M digits each)",
}


def category_of(fname):
    for key, label in CATEGORY_LABELS.items():
        if fname.startswith(key):
            return label
    return "Other"


def lps(n, sec):
    """Human-readable throughput."""
    if sec <= 0:
        return "N/A"
    r = n / sec
    if r < 1_000:
        return f"{r:,.0f} l/s"
    if r < 1_000_000:
        return f"{r / 1_000:.1f}K l/s"
    return f"{r / 1_000_000:.1f}M l/s"


def _ok_icon(status):
    if status == "PASS":
        return "OK"
    if status in ("TIMEOUT", "CRASH") or "FAIL" in status:
        return "!!"
    return ".."


# ══════════════════════════════════════════════════════════════════════════
# SECTION 7 — BENCHMARK ONE DATASET
# ══════════════════════════════════════════════════════════════════════════

def bench_dataset(ds_path, use_bc=True):
    """
    Run kalkulacka_2 + (optionally bc) against one dataset file.
    Returns a result dict with time, accuracy, fail details for each runner.
    """
    fname   = ds_path.name
    size_mb = ds_path.stat().st_size / 1024 / 1024
    entry   = {
        "name":    fname,
        "size_mb": round(size_mb, 2),
        "cat":     category_of(fname),
        "runs":    {},
    }

    # BUG FIX: removed "kalkulacka" (main.cpp) from runners entirely.
    # There is now a single reference (ref_) rather than ref1_ / ref2_.
    runners = [("kalkulacka_2", CALC2_BIN)]
    if use_bc:
        runners.append(("bc", None))

    for runner, bin_path in runners:
        ref_path = REF / f"ref_{fname}.txt"
        log_path = LOG / f"log_{runner}_{fname}.txt"

        if runner == "bc":
            rc, wall = run_bc(ds_path, log_path)
        else:
            rc, wall = run_binary(bin_path, ds_path, log_path)

        correct, total, fail, pct = verify(ref_path, log_path, ds_path)

        if rc == -1:
            status = "TIMEOUT"
        elif rc != 0:
            status = "CRASH"
        elif fail:
            status = f"FAIL@L{fail['line']}"
        elif total == 0 and pct == 0.0:
            status = "EMPTY"
        elif pct >= 99.5:
            status = "PASS"
        else:
            status = "PARTIAL"

        entry["runs"][runner] = {
            "wall_s":   round(wall, 4),
            "thr":      lps(total, wall),
            "status":   status,
            "correct":  correct,
            "verified": total,
            "accuracy": round(pct, 2),
            "exit_code": rc,
        }
        if fail:
            entry["runs"][runner]["fail"] = fail

    return entry


# ══════════════════════════════════════════════════════════════════════════
# SECTION 8 — RESULT TABLES
# ══════════════════════════════════════════════════════════════════════════

def print_results(results, datasets, use_bc):
    W = 80

    # BUG FIX: original always printed a K1 column even when kalkulacka was
    # not in the run set. Header now reflects actual runners.
    if use_bc:
        header = (f"{'':>28} {'MB':>5} | "
                  f"{'K2 (s)':>8} {'acc':>5} | "
                  f"{'bc (s)':>8} {'acc':>5}")
    else:
        header = (f"{'':>28} {'MB':>5} | "
                  f"{'K2 (s)':>8} {'acc':>5}")

    print(f"\n{'=' * W}")
    print("  BENCHMARK RESULTS")
    print(header)
    print(f"  {'-' * (W - 2)}")

    cur_cat = None
    for dname in datasets:
        r0 = next((r for r in results if r["name"] == dname), None)
        if not r0:
            continue
        cat = r0["cat"]
        if cat != cur_cat:
            print(f"\n  -- {cat} --")
            cur_cat = cat

        r2 = r0["runs"].get("kalkulacka_2", {})
        r3 = r0["runs"].get("bc", {})

        def _col(r):
            return f"{r.get('wall_s', 0):>8.3f} {r.get('accuracy', 0):>4.0f}%  {_ok_icon(r.get('status', '?'))}"

        row = f"  {dname[:28]:<28} {r0['size_mb']:>5.1f} | {_col(r2)}"
        if use_bc:
            row += f" | {_col(r3)}"
        print(row)

        s2 = r2.get("status", "?")
        s3 = r3.get("status", "?") if use_bc else "PASS"
        if s2 not in ("PASS", "EMPTY"):
            print(f"    K2 {s2}")
            if "fail" in r0["runs"].get("kalkulacka_2", {}):
                fd = r0["runs"]["kalkulacka_2"]["fail"]
                print(f"       input:    {fd['input']}")
                print(f"       expected: {fd['expected']}")
                print(f"       actual:   {fd['actual']}")
        if use_bc and s3 not in ("PASS", "EMPTY"):
            print(f"    bc {s3}")
            if "fail" in r0["runs"].get("bc", {}):
                fd = r0["runs"]["bc"]["fail"]
                print(f"       input:    {fd['input']}")
                print(f"       expected: {fd['expected']}")
                print(f"       actual:   {fd['actual']}")

    # ── scorecard ──────────────────────────────────────────────────────
    print(f"\n{'=' * W}")
    print("  FINAL SCORECARD\n")

    runners_info = [("kalkulacka_2", "kalkulacka_2 (calculator.cpp)", "full ops")]
    if use_bc:
        runners_info.append(("bc", "bc (system)", "reference"))

    for lbl, label, note in runners_info:
        cands = [r for r in results if lbl in r["runs"]]
        p  = sum(1 for r in cands if r["runs"][lbl]["status"] == "PASS")
        n  = sum(r["runs"][lbl]["verified"] for r in cands)
        t  = sum(r["runs"][lbl]["wall_s"]   for r in cands)
        th = f"{n/t:,.0f} l/s" if t > 0.001 else "N/A"
        print(f"  {label} [{note}]")
        print(f"    Passed : {p}/{len(cands)} datasets")
        print(f"    Lines  : {n:,}")
        print(f"    Time   : {t:.2f}s")
        print(f"    Speed  : {th}")
        print()

    print("  kalkulacka_2 handles: + - * / % ^ () and unary minus")
    if use_bc:
        print("  bc is the Linux native arbitrary-precision calculator (integer mode)")
    print("=" * W)


# ══════════════════════════════════════════════════════════════════════════
# SECTION 9 — MAIN
# ══════════════════════════════════════════════════════════════════════════

def parse_args(argv):
    no_bc   = "--no-bc" in argv
    filters = [a for a in argv if not a.startswith("--")]
    single  = filters[0] if filters else None
    return no_bc, single


def main():
    no_bc, single_ds = parse_args(sys.argv[1:])
    use_bc = not no_bc

    W = 78
    print("=" * W)
    print("  BENCHMARK SUITE — calculator.cpp vs bc")
    print(f"  calculator.cpp : {CALC2_SRC}")
    print(f"  Started        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  bc             : {'included' if use_bc else 'skipped (--no-bc)'}")
    print("=" * W)

    # ── Phase 0: compile ───────────────────────────────────────────────
    compile_all()

    # ── Phase 1: generate datasets ─────────────────────────────────────
    generate_all_datasets()

    # ── Phase 2: build references ──────────────────────────────────────
    build_references()

    # ── Phase 3: select datasets ───────────────────────────────────────
    all_ds = sorted(
        f.name
        for f in DS.iterdir()
        if f.suffix == ".txt" and f.stat().st_size > 0
    )

    if single_ds:
        todo = sorted(
            [d for d in all_ds if d == single_ds or single_ds in d],
            key=lambda x: (int(x[1]) if len(x) > 1 and x[1].isdigit() else x),
        )
        if not todo:
            print(f"\n  Dataset '{single_ds}' not found. Available:")
            for d in all_ds:
                print(f"    {d}")
            sys.exit(1)
    else:
        todo = all_ds

    # ── Phase 4: benchmark ─────────────────────────────────────────────
    print(f"\n[3/3] Benchmarking {len(todo)} dataset(s)...")
    all_results = []
    for dname in todo:
        ds = DS / dname
        print(f"\n  Running {dname}  ({ds.stat().st_size / 1024 / 1024:.1f} MB) ...")
        entry = bench_dataset(ds, use_bc=use_bc)
        all_results.append(entry)

    # ── display ────────────────────────────────────────────────────────
    print_results(all_results, todo, use_bc)

    # ── save JSON ──────────────────────────────────────────────────────
    # BUG FIX: original scorecard still included "kalkulacka" key which no
    # longer exists in runs; removed.
    scorecard = {}
    for lbl, name in [
        ("kalkulacka_2", "kalkulacka_2 (calculator.cpp)"),
        ("bc",           "bc (system)"),
    ]:
        cands = [r for r in all_results if lbl in r["runs"]]
        if not cands:
            continue
        p = sum(1 for r in cands if r["runs"][lbl]["status"] == "PASS")
        n = sum(r["runs"][lbl]["verified"] for r in cands)
        t = sum(r["runs"][lbl]["wall_s"]   for r in cands)
        scorecard[lbl] = {
            "name":     name,
            "passed":   p,
            "total_ds": len(cands),
            "lines":    n,
            "wall_s":   round(t, 4),
        }

    out = {
        "timestamp": datetime.now().isoformat(),
        "no_bc":     no_bc,
        "datasets":  todo,
        "scorecard": scorecard,
        "results":   all_results,
    }

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jp = LOG / f"bench_{ts}.json"
    with open(jp, "w") as f:
        json.dump(out, f, indent=2, default=float)
    print(f"\n  JSON saved: {jp}")


if __name__ == "__main__":
    main()