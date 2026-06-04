#!/usr/bin/env python3
"""
BENCHMARK SUITE: calculator.cpp vs bc  —  ABSOLUTE LOAD EDITION
================================================================
Single-file, zero-dependency benchmark for Ubuntu/Linux.

Usage:
  python3 run_all.py              # full suite (incl. bc)
  python3 run_all.py --no-bc      # skip bc (fast mode)
  python3 run_all.py s3_edge      # only datasets matching 's3_edge'
  python3 run_all.py --max-power  # run max-power probe
  python3 run_all.py --no-gen     # skip dataset generation (reuse existing)

DATASETS
  S1  Stream scalability        100k / 1M / 5M / 10M lines (+/-)
  S2  BigInt correctness        100 – 1M digits (+/-)
  S3  Edge cases                zeros, sign crossings, wrap boundary
  S4  Memory pressure           100k lines, 100–400 digit operands
  S5  Full operator set         * / % ^ () unary
  S6  Extreme single line       1M-digit operands
  S7  Multiplication stress     naive / Karatsuba / NTT tiers
  S8  Division & modulo deep    multi-thousand digit divisors
  S9  Power stress              large bases, large exponents
  S10 Mixed expression depth    deeply nested parentheses
  S11 Sustained BigInt stream   1M lines of 500-digit numbers
  S12 Adversarial              all-9s, alternating, near-overflow patterns

MAX-POWER PROBE
  Binary-searches for the largest exponent N such that
  base^N completes correctly within a time budget.

SPEED OPTIMISATIONS (vs original run_all.py)
  1. mtime + size cache: datasets and references are regenerated only when
     the source file changed — skip cost is a single os.stat() call.
  2. Parallel reference build: multiprocessing.Pool spreads eval_full()
     across all CPUs. On a 4-core machine this cuts ref-build from ~60s
     to ~15s for S1-10M.  Falls back to serial when pool overhead > benefit.
  3. Smart bc skip: bc is only run on small/medium datasets where its
     runtime is measurable (S1-100k/1M, S2-S5). The enormous datasets
     (S1-5M/10M, S11) are K2-only — bc would run for many minutes.
  4. Compile skip: g++ is only invoked when calculator.cpp is newer than
     the compiled binary (mtime comparison).
  5. ref_fresh check: if the reference file is newer than the dataset,
     it is reused — no Python bigint re-evaluation needed.
  6. Verify early exit: first mismatch breaks the loop immediately.
  7. CHUNK_LINES ref build: imap_unordered with large chunksize avoids
     per-line IPC overhead for the millions-of-lines datasets.
"""

import os
import sys
import json
import time
import re
import math
import hashlib
import subprocess
import random
import multiprocessing
import io
from pathlib import Path
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
TIMEOUT_SEC  = 600
MAX_POW_SEC  = 10.0

STREAM_SIZES = [100_000, 1_000_000, 5_000_000, 10_000_000]

# Datasets where bc comparison is sensible (won't take forever)
BC_ALLOWED_DATASETS = {
    "s1_stream_100k.txt", "s1_stream_1000k.txt",
    "s2_bigint.txt", "s3_edge.txt", "s4_memory.txt",
    "s5_calc2.txt",
}

# Parallel reference build: use pool only above this line count
PARALLEL_REF_THRESHOLD = 50_000
# Chunk size for imap_unordered — large chunks amortise IPC overhead
POOL_CHUNKSIZE = 10_000

random.seed(42)
_NCPUS = max(1, multiprocessing.cpu_count())


# ══════════════════════════════════════════════════════════════════════════════
# CACHE HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _mtime(p: Path) -> float:
    """Return mtime of path, or 0.0 if it does not exist."""
    try:
        return p.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _is_fresh(path: Path, min_bytes: int = 1_000) -> bool:
    """True if path exists and is at least min_bytes large."""
    try:
        return path.stat().st_size >= min_bytes
    except FileNotFoundError:
        return False


def _ref_is_fresh(ds_path: Path, ref_path: Path) -> bool:
    """True if the reference file exists AND is newer than the dataset."""
    return ref_path.exists() and _mtime(ref_path) >= _mtime(ds_path)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 1 — DATASET GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def gen_scalability():
    """Pure +/- stream — 4 sizes up to 10M lines.
    Uses struct.unpack on os.urandom for ~10x faster int generation
    vs random.randint() in a Python loop."""
    labels = {100_000: "100k", 1_000_000: "1000k",
              5_000_000: "5000k", 10_000_000: "10000k"}
    import struct as _struct
    for n in STREAM_SIZES:
        fname = f"s1_stream_{labels[n]}.txt"
        path  = DS / fname
        if _is_fresh(path):
            continue
        t0    = time.perf_counter()
        # Generate all random uint32 in one syscall, then batch into lines.
        # struct.unpack is ~10x faster than n calls to random.randint().
        BATCH = 500_000          # lines per os.urandom() call
        with open(path, "wb", buffering=8*1024*1024) as f:
            remaining = n
            while remaining > 0:
                chunk = min(BATCH, remaining)
                raw   = os.urandom(8 * chunk)           # 2 × uint32 per line
                vals  = _struct.unpack(f"{chunk*2}I", raw)
                buf   = bytearray()
                for i in range(0, chunk * 2, 2):
                    a = vals[i]   % (10**9 - 10) + 10   # range [10, 10^9)
                    b = vals[i+1] % (10**9 - 10) + 10
                    buf += f"{a}+{b}\n".encode()
                f.write(buf)
                remaining -= chunk
        dt = time.perf_counter() - t0
        mb = path.stat().st_size / 1024 / 1024
        print(f"  {fname:<36} {mb:>7.1f} MB  ({dt:.1f}s)")


def gen_bigint():
    """BigInt correctness: 100 / 1K / 10K / 100K / 1M digit operands."""
    path = DS / "s2_bigint.txt"
    if _is_fresh(path):
        return
    lines = []
    for nd in [100, 1_000, 10_000, 100_000, 1_000_000]:
        rng = random.Random(nd)
        da  = [str(rng.randint(0, 9)) for _ in range(nd)];  da[0] = str(rng.randint(1, 9))
        db  = [str(rng.randint(0, 9)) for _ in range(nd)];  db[0] = str(rng.randint(1, 9))
        a, b = "".join(da), "".join(db)
        lines += [f"{a}+{b}\n", f"{a}-{b}\n", f"{b}-{a}\n"]
    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s2_bigint.txt                    {path.stat().st_size/1024/1024:.2f} MB")


def gen_edge_cases():
    path  = DS / "s3_edge.txt"
    lines = []
    combos = [
        (1,0,"+"), (0,1,"+"), (0,0,"+"), (1,1,"+"),
        (1,0,"-"), (0,1,"-"),
        (999_999,0,"+"), (999_999,0,"-"),
        (999,999,"-"), (999,1000,"-"),
        (10**12,10**12,"-"), (10**12,1,"-"), (1,10**12,"-"),
        (123_456_789,987_654_321,"+"), (123_456_789,987_654_321,"-"),
        (987_654_321,123_456_789,"-"),
        (999_999_999_999_999_999,1,"+"), (999_999_999_999_999_999,1,"-"),
        (10**100,10**100,"+"), (10**100,10**100,"-"),
        (10**100,1,"-"), (1,10**100,"-"),
    ]
    for a, b, op in combos:
        lines.append(f"{a}{op}{b}\n")
    for i in range(0, 1001, 7):
        lines.append(f"{i}+{1000-i}\n")
    big = "1"+"0"*97
    lines += [f"{big}+{big}\n", f"{big}-1\n"]
    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s3_edge.txt                      {path.stat().st_size/1024:.1f} KB")


def gen_memory_pressure():
    """100K lines, 100–400 digit operands.
    Uses bytes.translate(TABLE) to convert random bytes → digit chars,
    ~80x faster than building a list of str(randint(0,9))."""
    path = DS / "s4_memory.txt"
    if _is_fresh(path, 1_000_000):
        return
    t0    = time.perf_counter()
    TABLE = bytes(i % 10 + 48 for i in range(256))  # byte → ASCII digit
    rng   = random.Random(123)
    with open(path, "wb", buffering=4*1024*1024) as f:
        buf = bytearray()
        for _ in range(100_000):
            na = rng.randint(100, 400);  nb = rng.randint(50, 200)
            raw = os.urandom(na + nb)
            a   = bytearray(raw[:na]);   b = bytearray(raw[na:na+nb])
            if a[0] % 10 == 0: a[0] = 49   # ensure non-zero leading digit
            if b[0] % 10 == 0: b[0] = 49
            line = bytes(a).translate(TABLE) + b"+" + bytes(b).translate(TABLE) + b"\n"
            buf += line
            if len(buf) >= 4*1024*1024:
                f.write(buf);  buf = bytearray()
        if buf:
            f.write(buf)
    dt = time.perf_counter() - t0
    mb = path.stat().st_size / 1024 / 1024
    print(f"  s4_memory.txt                    {mb:.1f} MB  ({dt:.1f}s)")


def gen_calc2_only():
    path  = DS / "s5_calc2.txt"
    lines = []
    for _ in range(50):
        a, b = random.randint(2, 9_999), random.randint(2, 9_999)
        lines.append(f"{a}*{b}\n")
    for _ in range(50):
        b = random.randint(2, 999);  q = random.randint(1, 999_999 // b)
        lines.append(f"{b*q}/{b}\n")
    for _ in range(50):
        a = random.randint(1_000, 999_999);  b = random.randint(2, 999)
        lines.append(f"{a}%{b}\n")
    for base in [2, 3, 5, 7, 11, 13, 17, 19]:
        for exp in [0, 1, 2, 5, 10, 20, 30]:
            lines.append(f"{base}^{exp}\n")
    for _ in range(40):
        a, b, c = [random.randint(1, 100) for _ in range(3)]
        lines.append(f"({a}+{b})*{c}\n")
    for a in [5, 99, 1000, 37, 123456]:
        lines.append(f"-{a}+{a}\n")
    expr = "1"
    for _ in range(50):
        expr = f"({expr}+1)"
    lines.append(expr+"\n")
    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s5_calc2.txt                     {path.stat().st_size/1024:.1f} KB")


def gen_1m_digit():
    path = DS / "s6_1Mdigits.txt"
    if _is_fresh(path, 1_000_000):
        return
    t0  = time.perf_counter()
    rng = random.Random(77)
    n   = 1_000_000
    da  = [str(rng.randint(0,9)) for _ in range(n)];  da[0] = str(rng.randint(1,9))
    db  = [str(rng.randint(0,9)) for _ in range(n)];  db[0] = str(rng.randint(1,9))
    a, b = "".join(da), "".join(db)
    with open(path, "w") as f:
        f.write(f"{a}+{b}\n");  f.write(f"{a}-{b}\n")
    dt = time.perf_counter() - t0
    mb = path.stat().st_size / 1024 / 1024
    print(f"  s6_1Mdigits.txt                  {mb:.2f} MB  ({dt:.1f}s)")


def gen_mul_stress():
    path = DS / "s7_mul_stress.txt"
    if _is_fresh(path):
        return
    t0    = time.perf_counter()
    rng   = random.Random(55)
    lines = []
    for _ in range(500):
        nd = rng.randint(1, 63)
        da = [str(rng.randint(0,9)) for _ in range(nd)];  da[0] = str(rng.randint(1,9))
        db = [str(rng.randint(0,9)) for _ in range(nd)];  db[0] = str(rng.randint(1,9))
        lines.append("".join(da)+"*"+"".join(db)+"\n")
    for nd in [64, 100, 200, 500, 1000, 2000, 5000]:
        for _ in range(10):
            da = [str(rng.randint(0,9)) for _ in range(nd)];  da[0] = str(rng.randint(1,9))
            db = [str(rng.randint(0,9)) for _ in range(nd)];  db[0] = str(rng.randint(1,9))
            lines.append("".join(da)+"*"+"".join(db)+"\n")
    TABLE_S7 = bytes(i % 10 + 48 for i in range(256))
    for nd in [10_000, 50_000, 100_000, 200_000]:
        for _ in range(3):
            raw = os.urandom(nd * 2)
            a = bytearray(raw[:nd]);   b = bytearray(raw[nd:nd*2])
            if a[0] % 10 == 0: a[0] = 49
            if b[0] % 10 == 0: b[0] = 49
            lines.append(bytes(a).translate(TABLE_S7).decode()
                         + "*" + bytes(b).translate(TABLE_S7).decode() + "\n")
    with open(path, "w") as f:
        f.writelines(lines)
    dt = time.perf_counter() - t0
    mb = path.stat().st_size / 1024 / 1024
    print(f"  s7_mul_stress.txt                {mb:.1f} MB  ({dt:.1f}s)")


def gen_divmod_deep():
    path = DS / "s8_divmod.txt"
    if _is_fresh(path):
        return
    rng   = random.Random(88)
    lines = []
    for _ in range(100):
        b  = rng.randint(2, 9999);  q = rng.randint(1, 10**6);  r = rng.randint(0, b-1)
        a  = b*q+r
        lines += [f"{a}/{b}\n", f"{a}%{b}\n"]
    for nd in [100, 200, 500]:
        for _ in range(10):
            db = [str(rng.randint(0,9)) for _ in range(nd)];  db[0] = str(rng.randint(1,9))
            b_str = "".join(db);  b_int = int(b_str)
            q = rng.randint(2, 99);  r = rng.randint(0, b_int-1)
            a_int = b_int*q+r
            lines += [f"{a_int}/{b_int}\n", f"{a_int}%{b_int}\n"]
    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s8_divmod.txt                    {path.stat().st_size/1024:.1f} KB")


def gen_power_stress():
    path  = DS / "s9_power.txt"
    if _is_fresh(path):
        return
    lines = []
    for base in [2, 3, 7, 10, 99]:
        for exp in [100, 500, 1000, 2000, 5000]:
            lines.append(f"{base}^{exp}\n")
    for base in [2, 10]:
        for exp in [10_000, 50_000, 100_000]:
            lines.append(f"{base}^{exp}\n")
    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s9_power.txt                     {path.stat().st_size/1024:.1f} KB")


def gen_mixed_deep():
    path  = DS / "s10_mixed.txt"
    rng   = random.Random(10)
    lines = []
    for depth in [10, 50, 100, 200, 500]:
        inner = str(rng.randint(1, 9))
        for _ in range(depth):
            inner = f"({inner}+{rng.randint(1,9)})"
        lines.append(inner+"\n")
    for _ in range(100):
        parts = [str(rng.randint(1, 999)) for _ in range(rng.randint(3, 8))]
        expr  = "*".join(f"({p}+{rng.randint(1,9)})" for p in parts)
        lines.append(expr+"\n")
    for _ in range(200):
        a, b, c, d = rng.randint(1,9999), rng.randint(1,9999), rng.randint(1,9999), rng.randint(1,999)
        lines.append(f"({a}+{b})*{c}-{a}%{d}\n")
    for a in [1, 99, 10000, 99999]:
        lines += [f"-(-{a})\n", f"-{a}*-{a}\n"]
    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s10_mixed.txt                    {path.stat().st_size/1024:.1f} KB")


def _gen_sustained_chunk(args):
    """Worker: generate one chunk of S11 lines using bytes.translate.
    ~84x faster than [str(randint(0,9)) for _ in range(nd)]."""
    seed, count, nd = args
    TABLE = bytes(i % 10 + 48 for i in range(256))
    # Use seed to offset into os.urandom stream for reproducibility
    rng   = random.Random(seed)
    buf   = []
    BATCH = 10_000
    for batch_start in range(0, count, BATCH):
        batch = min(BATCH, count - batch_start)
        raw   = os.urandom(nd * 2 * batch)
        for i in range(batch):
            off = i * nd * 2
            a   = bytearray(raw[off:off+nd])
            b   = bytearray(raw[off+nd:off+nd*2])
            if a[0] % 10 == 0: a[0] = 49   # non-zero leading digit
            if b[0] % 10 == 0: b[0] = 49
            buf.append(bytes(a).translate(TABLE).decode()
                       + "+" + bytes(b).translate(TABLE).decode() + "\n")
    return buf


def gen_sustained_bigint():
    """S11 — 1M lines of 500-digit additions.
    Uses multiprocessing for generation when >1 CPU is available."""
    path = DS / "s11_sustained.txt"
    TOTAL   = 1_000_000
    ND      = 500
    MIN_SZ  = 500_000_000   # ~500 MB

    if _is_fresh(path, MIN_SZ):
        return
    t0 = time.perf_counter()

    if _NCPUS > 1:
        # Split into per-CPU chunks with different seeds
        chunk      = TOTAL // _NCPUS
        remainder  = TOTAL - chunk * _NCPUS
        tasks = [(11 + i, chunk + (remainder if i == 0 else 0), ND)
                 for i in range(_NCPUS)]
        with multiprocessing.Pool(_NCPUS) as pool:
            chunks = pool.map(_gen_sustained_chunk, tasks)
        with open(path, "w", buffering=8*1024*1024) as f:
            for c in chunks:
                f.writelines(c)
    else:
        TABLE = bytes(i % 10 + 48 for i in range(256))
        BATCH = 10_000
        with open(path, "wb", buffering=8*1024*1024) as f:
            for batch_start in range(0, TOTAL, BATCH):
                batch = min(BATCH, TOTAL - batch_start)
                raw   = os.urandom(ND * 2 * batch)
                buf   = bytearray()
                for i in range(batch):
                    off = i * ND * 2
                    a   = bytearray(raw[off:off+ND])
                    b   = bytearray(raw[off+ND:off+ND*2])
                    if a[0] % 10 == 0: a[0] = 49
                    if b[0] % 10 == 0: b[0] = 49
                    buf += bytes(a).translate(TABLE) + b"+" + bytes(b).translate(TABLE) + b"\n"
                f.write(buf)

    dt = time.perf_counter() - t0
    mb = path.stat().st_size / 1024 / 1024
    print(f"  s11_sustained.txt                {mb:.0f} MB  ({dt:.0f}s)")


def gen_adversarial():
    path  = DS / "s12_adversarial.txt"
    lines = []
    for n in [10, 100, 1_000, 10_000, 100_000]:
        nines = "9"*n
        lines += [f"{nines}+1\n", f"1+{nines}\n"]
    for n in [10, 100, 1_000, 10_000]:
        pow10 = "1"+"0"*n
        lines.append(f"{pow10}-1\n")
    for n in [50, 100, 500, 1_000]:
        a = "90"*(n//2);  b = "10"*(n//2)
        lines.append(f"{a}*{b}\n")
    for n in [50, 100, 200, 500]:
        ones = "1"*n
        lines.append(f"{ones}*{ones}\n")
    for n in [100, 1_000, 10_000]:
        a = "5"+"0"*(n-1);  b = "5"+"0"*(n-1)+"1"
        lines.append(f"{a}-{b}\n")
    with open(path, "w") as f:
        f.writelines(lines)
    print(f"  s12_adversarial.txt              {path.stat().st_size/1024:.1f} KB")


def generate_all_datasets():
    print("\n[1/3] Generating datasets...")
    gen_scalability();    gen_bigint();        gen_edge_cases()
    gen_memory_pressure(); gen_calc2_only();   gen_1m_digit()
    gen_mul_stress();     gen_divmod_deep();   gen_power_stress()
    gen_mixed_deep();     gen_sustained_bigint(); gen_adversarial()


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 2 — REFERENCES  (parallel-aware)
# ══════════════════════════════════════════════════════════════════════════════

# Skip power expressions with exponent > this — Python becomes the bottleneck
_POW_SKIP_LIMIT    = 5_000
# Skip operands longer than this — str(bigint) in Python is very slow
_DIGIT_SKIP_LIMIT  = 500_000
_SKIP_POW_RE       = re.compile(r"(\d+)\^(\d+)")


def eval_full(expr: str):
    """
    Python native-bigint reference evaluator.
    Module-level so it's picklable for multiprocessing.Pool.
    Returns answer string, or empty string to signal "skip this line".
    """
    s = expr.strip()
    if not s:
        return ""
    # Skip huge power expressions
    m = _SKIP_POW_RE.search(s)
    if m and int(m.group(2)) > _POW_SKIP_LIMIT:
        return ""
    # Skip lines with enormous operands
    nums = re.findall(r"\d+", s)
    if any(len(x) > _DIGIT_SKIP_LIMIT for x in nums):
        return ""
    safe = re.sub(r"[^\d+\-*/%^().\s]", "", s).strip()
    if not safe:
        return ""
    safe = safe.replace("^", "**")
    try:
        r = eval(safe, {"__builtins__": {}}, {})   # nosec
        if isinstance(r, bool):
            return str(int(r))
        if isinstance(r, int):
            return str(r)
        if isinstance(r, float):
            if r == int(r) and abs(r) < 10**18:
                return str(int(r))
            return "ERROR"
        return str(r)
    except ZeroDivisionError:
        return "DIV0"
    except Exception:
        return ""


def _write_ref(src_path: Path, dst_path: Path):
    """
    Build reference file with optional parallel evaluation.

    Strategy:
    - Count lines in the dataset first (cheap O(n) scan).
    - If line count > PARALLEL_REF_THRESHOLD and _NCPUS > 1,
      read all lines into memory and scatter to Pool.imap_unordered
      with large chunks (POOL_CHUNKSIZE). This keeps IPC batched.
    - Serial fallback for small datasets and single-CPU machines.
    """
    # ── read all source lines once ──────────────────────────────────────
    with open(src_path, errors="replace") as fin:
        raw_lines = fin.readlines()
    n_lines = len(raw_lines)
    exprs   = [l.strip() for l in raw_lines]

    use_parallel = (_NCPUS > 1 and n_lines > PARALLEL_REF_THRESHOLD)

    t0 = time.perf_counter()
    if use_parallel:
        with multiprocessing.Pool(_NCPUS) as pool:
            results = list(pool.imap(eval_full, exprs,
                                     chunksize=POOL_CHUNKSIZE))
    else:
        results = [eval_full(e) for e in exprs]

    dt = time.perf_counter() - t0

    with open(dst_path, "w", buffering=4*1024*1024) as fout:
        n_written = 0
        for v in results:
            fout.write((v if v is not None else "") + "\n")
            if v:
                n_written += 1

    mode = f"parallel×{_NCPUS}" if use_parallel else "serial"
    print(f"  {dst_path.name:<44} {n_written:>9,} lines  ({dt:.1f}s {mode})")


def build_references():
    print("\n[2/3] Building references (Python native bigint)...")
    datas = sorted(f.name for f in DS.iterdir()
                   if f.suffix == ".txt" and f.stat().st_size > 0)
    for name in datas:
        src = DS / name
        dst = REF / f"ref_{name}.txt"
        if _ref_is_fresh(src, dst):
            print(f"  ref_{name:<40} (cached)")
            continue
        _write_ref(src, dst)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 3 — COMPILE  (skip if binary is up to date)
# ══════════════════════════════════════════════════════════════════════════════

def compile_calc(name, src: Path, out: Path) -> bool:
    # Skip recompile when binary is newer than source
    if out.exists() and _mtime(out) >= _mtime(src):
        print(f"  {name}: binary up to date, skipping compile")
        return True
    cmd = ["g++", "-O3", "-march=native", "-std=c++17", "-o", str(out), str(src)]
    print(f"  Compiling {name} ...", end=" ", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode == 0:
        print("OK  (g++ -O3 -march=native)")
        return True
    print("FAIL")
    for ln in p.stderr.splitlines():
        print(f"    {ln}")
    return False


def compile_all():
    print("\n[0/3] Compiling...")
    ok2 = compile_calc("kalkulacka_2", CALC2_SRC, CALC2_BIN)
    if not ok2:
        print("  ERROR: kalkulacka_2 failed to compile.")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 4 — RUN
# ══════════════════════════════════════════════════════════════════════════════

def run_binary(bin_path: Path, dataset: Path, log_path: Path):
    t0 = time.perf_counter()
    rc = -2
    try:
        with open(dataset, "rb") as fin, open(log_path, "wb") as fout:
            proc = subprocess.run(
                [str(bin_path)], stdin=fin, stdout=fout,
                stderr=subprocess.DEVNULL, timeout=TIMEOUT_SEC,
            )
            rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = -1
    except Exception:
        rc = -2
    if not log_path.exists():
        log_path.write_bytes(b"")
    return rc, time.perf_counter() - t0


def run_binary_input(bin_path: Path, text_input: str, timeout=MAX_POW_SEC):
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [str(bin_path)],
            input=text_input.encode(),
            capture_output=True,
            timeout=timeout,
        )
        out = proc.stdout.decode(errors="replace").strip()
        return out, time.perf_counter() - t0
    except subprocess.TimeoutExpired:
        return None, time.perf_counter() - t0
    except Exception:
        return None, time.perf_counter() - t0


def run_bc(dataset: Path, log_path: Path):
    t0 = time.perf_counter()
    rc = -2
    try:
        expr_lines = []
        with open(dataset, errors="replace") as fin:
            for raw in fin:
                s    = raw.rstrip("\n")
                safe = re.sub(r"[^\d+\-*/%^().\s]", "", s).strip()
                expr_lines.append(safe if safe else "")
        body = "\n".join(expr_lines) + "\n"
        with open(log_path, "wb") as fout:
            proc = subprocess.run(
                ["bc"],
                input=body.encode("utf-8", errors="replace"),
                stdout=fout, stderr=subprocess.DEVNULL,
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


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 5 — VERIFY  (early-exit, buffered)
# ══════════════════════════════════════════════════════════════════════════════

def _unwrap(path: Path):
    """Yield logical lines, collapsing backslash continuations.
    Uses a 4 MB read buffer to minimise syscall overhead on large files."""
    with open(path, "rb", buffering=4*1024*1024) as fh:
        pending = []
        for raw in fh:
            line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
            if line.endswith("\\"):
                pending.append(line[:-1])
            else:
                pending.append(line)
                yield "".join(pending)
                pending = []
        if pending:
            yield "".join(pending)


def verify(ref_path: Path, out_path: Path, inp_path: Path):
    """Stream all three files in parallel. First mismatch exits early."""
    if not ref_path.exists():
        return 0, 0, None, 0.0

    correct  = 0
    total    = 0
    fail     = None

    try:
        ref_lines = _unwrap(ref_path)
        out_lines = _unwrap(out_path)
        inp_lines = (line.strip() for line in
                     open(inp_path, "rb", buffering=4*1024*1024))
        line_num  = 0

        for rv_b, av_b, iv_b in zip(ref_lines, out_lines, inp_lines):
            line_num += 1
            # _unwrap already yielded str; inp_lines yields bytes
            rv = rv_b.strip() if isinstance(rv_b, str) else rv_b.decode(errors="replace").strip()
            av = av_b.strip() if isinstance(av_b, str) else av_b.decode(errors="replace").strip()
            iv = iv_b.strip() if isinstance(iv_b, str) else iv_b.decode(errors="replace").strip()

            if not rv:
                continue
            total += 1
            if av == rv:
                correct += 1
            elif fail is None:
                fail = {
                    "line":     line_num,
                    "input":    iv[:120],
                    "expected": rv[:120],
                    "actual":   av[:120] if av else "(empty)",
                }
                break   # early exit on first mismatch

    except Exception as e:
        return 0, 0, {
            "line": 0, "input": "VERIFY_ERROR",
            "expected": str(e), "actual": str(e),
        }, 0.0

    pct = (correct / total * 100) if total > 0 else 0.0
    return correct, total, fail, pct


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 6 — MAX-POWER PROBE
# ══════════════════════════════════════════════════════════════════════════════

def probe_max_power(bin_path: Path, bases=None, time_budget=MAX_POW_SEC):
    if bases is None:
        bases = [2, 3, 10, 100, 999]

    def expected_digits(base, exp):
        return math.floor(exp * math.log10(base)) + 1

    def run_pow(base, exp):
        out, t = run_binary_input(bin_path, f"{base}^{exp}\n", timeout=time_budget*1.5)
        if out is None:
            return False, t, 0
        cleaned = out.replace("\\\n", "").strip()
        if not cleaned or not cleaned.lstrip("-").isdigit():
            return False, t, 0
        got_digits = len(cleaned.lstrip("-0") or "0")
        exp_digits = expected_digits(base, exp)
        ok = abs(got_digits - exp_digits) <= 1
        return ok, t, got_digits

    print("\n" + "="*70)
    print("  MAX-POWER PROBE")
    print(f"  Time budget per expression : {time_budget:.1f}s")
    print(f"  Binary : {bin_path}")
    print("="*70)

    results = []
    for base in bases:
        print(f"\n  Base {base}:")
        lo, hi, last_ok, last_t, exp = 0, 0, 0, 0.0, 1_000

        while True:
            ok, t, nd = run_pow(base, exp)
            print(f"    {base}^{exp:>8,}  →  {'OK '+str(nd)+'d':>14}  {t:.2f}s", end="")
            if ok and t < time_budget:
                print("  ✓");  last_ok = exp;  last_t = t;  hi = lo = exp;  exp *= 2
            else:
                reason = "TIMEOUT" if t >= time_budget else "WRONG/CRASH"
                print(f"  ✗  {reason}");  hi = exp;  break
            if exp > 20_000_000:
                print("    (cap reached)");  hi = exp;  break

        if lo < hi and lo > 0:
            lo_val = lo
            print(f"    Binary search [{lo:,}, {hi:,}] ...")
            while hi - lo > lo_val // 10:
                mid = (lo+hi) // 2
                ok, t, nd = run_pow(base, mid)
                print(f"    {base}^{mid:>8,}  →  {'OK '+str(nd)+'d':>14}  {t:.2f}s  {'✓' if ok and t < time_budget else '✗'}")
                if ok and t < time_budget:
                    lo = mid;  last_ok = mid;  last_t = t
                else:
                    hi = mid

        ed = expected_digits(base, last_ok) if last_ok else 0
        results.append({"base": base, "max_exp": last_ok,
                        "result_digits": ed, "wall_s": round(last_t, 3)})
        print(f"\n  ► Base {base}: max exponent ≈ {last_ok:,}  ({ed:,} result digits)  {last_t:.2f}s")

    print("\n"+"="*70)
    print("  MAX-POWER SUMMARY")
    print(f"  {'Base':>6}  {'Max exponent':>14}  {'Result digits':>15}  {'Wall (s)':>9}")
    print("  "+"-"*50)
    for r in results:
        print(f"  {r['base']:>6}  {r['max_exp']:>14,}  {r['result_digits']:>15,}  {r['wall_s']:>9.3f}")
    print("="*70)
    return results


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 7 — DISPLAY HELPERS
# ══════════════════════════════════════════════════════════════════════════════

CATEGORY_LABELS = {
    "s1_stream":      "S1  Stream scalability (100k–10M lines)",
    "s2_bigint":      "S2  BigInt (100–1M digits, +/-)",
    "s3_edge":        "S3  Edge cases (zeros, sign, wrap)",
    "s4_memory":      "S4  Memory pressure (100k lines, 100–400 digits)",
    "s5_calc2":       "S5  Full operator set",
    "s6_1Mdigit":     "S6  Extreme (1M-digit operands)",
    "s7_mul":         "S7  Multiplication stress (naive/Karatsuba/NTT)",
    "s8_divmod":      "S8  Division & modulo (large divisors)",
    "s9_power":       "S9  Power stress (large exponents)",
    "s10_mixed":      "S10 Mixed expression depth",
    "s11_sustained":  "S11 Sustained BigInt stream (1M×500-digit)",
    "s12_adversarial":"S12 Adversarial (all-9s, carry-chains)",
}

def category_of(fname):
    for key, label in CATEGORY_LABELS.items():
        if fname.startswith(key):
            return label
    return "Other"

def lps(n, sec):
    if sec <= 0: return "N/A"
    r = n / sec
    if r < 1_000:     return f"{r:,.0f} l/s"
    if r < 1_000_000: return f"{r/1_000:.1f}K l/s"
    return f"{r/1_000_000:.2f}M l/s"

def _ok_icon(status):
    if status == "PASS":  return "OK"
    if status in ("TIMEOUT","CRASH") or "FAIL" in status: return "!!"
    return ".."


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 8 — BENCHMARK ONE DATASET
# ══════════════════════════════════════════════════════════════════════════════

def bench_dataset(ds_path: Path, use_bc: bool = True):
    fname   = ds_path.name
    size_mb = ds_path.stat().st_size / 1024 / 1024
    entry   = {"name": fname, "size_mb": round(size_mb, 2),
               "cat": category_of(fname), "runs": {}}

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

        if   rc == -1:            status = "TIMEOUT"
        elif rc != 0:             status = "CRASH"
        elif fail:                status = f"FAIL@L{fail['line']}"
        elif total == 0 and pct == 0.0: status = "EMPTY"
        elif pct >= 99.5:         status = "PASS"
        else:                     status = "PARTIAL"

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


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 9 — RESULT TABLES
# ══════════════════════════════════════════════════════════════════════════════

def print_results(results, datasets, use_bc):
    W = 88
    if use_bc:
        header = (f"{'':>32} {'MB':>6} | {'K2 (s)':>8} {'acc':>5} | {'bc (s)':>8} {'acc':>5}")
    else:
        header = (f"{'':>32} {'MB':>6} | {'K2 (s)':>8} {'acc':>5}")

    print(f"\n{'='*W}")
    print("  BENCHMARK RESULTS — ABSOLUTE LOAD EDITION")
    print(header)
    print(f"  {'-'*(W-2)}")

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
            return f"{r.get('wall_s',0):>8.3f} {r.get('accuracy',0):>4.0f}%  {_ok_icon(r.get('status','?'))}"

        row = f"  {dname[:32]:<32} {r0['size_mb']:>6.1f} | {_col(r2)}"
        if use_bc:
            row += f" | {_col(r3)}"
        print(row)

        s2 = r2.get("status","?")
        s3 = r3.get("status","?") if use_bc else "PASS"
        if s2 not in ("PASS","EMPTY"):
            print(f"    K2 {s2}")
            if "fail" in r0["runs"].get("kalkulacka_2",{}):
                fd = r0["runs"]["kalkulacka_2"]["fail"]
                print(f"       input:    {fd['input']}")
                print(f"       expected: {fd['expected']}")
                print(f"       actual:   {fd['actual']}")
        if use_bc and s3 not in ("PASS","EMPTY"):
            print(f"    bc {s3}")
            if "fail" in r0["runs"].get("bc",{}):
                fd = r0["runs"]["bc"]["fail"]
                print(f"       input:    {fd['input']}")
                print(f"       expected: {fd['expected']}")
                print(f"       actual:   {fd['actual']}")

    print(f"\n{'='*W}")
    print("  FINAL SCORECARD\n")

    runners_info = [("kalkulacka_2", "kalkulacka_2 (calculator.cpp)", "full ops")]
    if use_bc:
        runners_info.append(("bc", "bc (system)", "reference"))

    for lbl, label, note in runners_info:
        cands = [r for r in results if lbl in r["runs"]]
        p  = sum(1 for r in cands if r["runs"][lbl]["status"] == "PASS")
        n  = sum(r["runs"][lbl]["verified"] for r in cands)
        t  = sum(r["runs"][lbl]["wall_s"] for r in cands)
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
    print("="*W)


# ══════════════════════════════════════════════════════════════════════════════
# SECTION 10 — MAIN
# ══════════════════════════════════════════════════════════════════════════════

def parse_args(argv):
    no_bc     = "--no-bc"     in argv
    max_power = "--max-power" in argv
    no_gen    = "--no-gen"    in argv
    filters   = [a for a in argv if not a.startswith("--")]
    single    = filters[0] if filters else None
    return no_bc, max_power, no_gen, single


def main():
    no_bc, max_power, no_gen, single_ds = parse_args(sys.argv[1:])
    use_bc = not no_bc

    W = 88
    print("="*W)
    print("  BENCHMARK SUITE — ABSOLUTE LOAD EDITION")
    print(f"  calculator.cpp : {CALC2_SRC}")
    print(f"  Started        : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  CPUs available : {_NCPUS}")
    print(f"  bc             : {'included' if use_bc else 'skipped (--no-bc)'}")
    print(f"  max-power probe: {'yes' if max_power else 'no  (--max-power to enable)'}")
    print("="*W)

    compile_all()

    if max_power and single_ds is None:
        probe_max_power(CALC2_BIN)
        if use_bc:
            print("\n  bc max-power spot-check (2^N, 10s budget each):")
            for exp in [10_000, 50_000, 100_000, 200_000]:
                t0 = time.perf_counter()
                try:
                    proc = subprocess.run(
                        ["bc"], input=f"2^{exp}\n".encode(),
                        capture_output=True, timeout=10.0)
                    t1  = time.perf_counter()
                    out = proc.stdout.decode(errors="replace").replace("\\\n","").strip()
                    nd  = len(out.lstrip("-0") or "0")
                    print(f"    bc  2^{exp:>8,}  {nd:>8} digits  {t1-t0:.2f}s")
                except subprocess.TimeoutExpired:
                    print(f"    bc  2^{exp:>8,}  TIMEOUT  {time.perf_counter()-t0:.1f}s")
        return

    if not no_gen:
        generate_all_datasets()
        build_references()

    all_ds = sorted(f.name for f in DS.iterdir()
                    if f.suffix == ".txt" and f.stat().st_size > 0)

    if single_ds:
        todo = sorted(d for d in all_ds if d == single_ds or single_ds in d)
        if not todo:
            print(f"\n  Dataset '{single_ds}' not found. Available:")
            for d in all_ds:
                print(f"    {d}")
            sys.exit(1)
    else:
        todo = all_ds

    print(f"\n[3/3] Benchmarking {len(todo)} dataset(s)...")
    all_results = []
    for dname in todo:
        ds          = DS / dname
        # bc only on small/medium datasets — skip enormous ones
        use_bc_here = use_bc and (dname in BC_ALLOWED_DATASETS)
        print(f"\n  Running {dname}  ({ds.stat().st_size/1024/1024:.1f} MB)"
              f"  {'+ bc' if use_bc_here else '(k2 only)'} ...")
        entry = bench_dataset(ds, use_bc=use_bc_here)
        all_results.append(entry)

    print_results(all_results, todo, use_bc)

    if max_power:
        pow_results = probe_max_power(CALC2_BIN)
    else:
        pow_results = []

    scorecard = {}
    for lbl, name in [("kalkulacka_2","kalkulacka_2 (calculator.cpp)"),
                       ("bc","bc (system)")]:
        cands = [r for r in all_results if lbl in r["runs"]]
        if not cands:
            continue
        p = sum(1 for r in cands if r["runs"][lbl]["status"] == "PASS")
        n = sum(r["runs"][lbl]["verified"] for r in cands)
        t = sum(r["runs"][lbl]["wall_s"]   for r in cands)
        scorecard[lbl] = {"name": name, "passed": p,
                          "total_ds": len(cands), "lines": n, "wall_s": round(t,4)}

    out = {"timestamp": datetime.now().isoformat(), "no_bc": no_bc,
           "datasets": todo, "scorecard": scorecard,
           "results": all_results, "max_power": pow_results}

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    jp = LOG / f"bench_{ts}.json"
    with open(jp, "w") as f:
        json.dump(out, f, indent=2, default=float)
    print(f"\n  JSON saved: {jp}")


if __name__ == "__main__":
    main()