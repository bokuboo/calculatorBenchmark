# Developer Documentation — BigInt Calculator Benchmark Suite

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [calculator.cpp Internals](#2-calculatorcpp-internals)
3. [run_all.py Internals](#3-run_allpy-internals)
4. [Performance Design Decisions](#4-performance-design-decisions)
5. [Adding New Datasets](#5-adding-new-datasets)
6. [Extending the Calculator](#6-extending-the-calculator)

---

## 1. Architecture Overview

The project has two independent components that share only the filesystem:

```
run_all.py  ──generates──►  datasets/*.txt
            ──evaluates──►  reference/ref_*.txt   (Python bigint)
            ──compiles──►   bin/kalkulacka_2       (from calculator.cpp)
            ──runs──►       results/log_*.txt      (binary output)
            ──verifies──►   log vs reference       (line-by-line diff)
            ──writes──►     results/bench_*.json   (structured results)
```

`calculator.cpp` is a pure stdin→stdout filter: one expression per line in, one result per line out. It has no knowledge of the benchmark harness.

---

## 2. calculator.cpp Internals

### 2.1 Memory Model — Custom Arena

All dynamic allocations go through a single `mmap`-backed arena:

```cpp
static char*  g_arena     = nullptr;
static size_t g_arenaSize = 0;   // 75% of physical RAM, min 512 MB
static size_t g_arenaIdx  = 0;
```

`arenaAlloc(n)` bumps `g_arenaIdx` with 8-byte alignment. It never frees individual objects. Instead, a **snapshot/restore** pattern reclaims all memory used by one expression after it has been evaluated:

```cpp
size_t snap = arenaSnapshot();     // before parsing the line
evaluateLine(lineCarry, lineLen);
arenaRestore(snap);                // reset index — O(1) "free everything"
```

The arena uses `MAP_NORESERVE`: the OS commits physical pages lazily, so a simple `2+2` query does not touch gigabytes of address space even though the arena is allocated at startup.

### 2.2 BigInt Representation

```cpp
struct BigInt {
    char* digits;    // pointer into arena — decimal digits as ASCII chars
    int   len;       // number of significant digits
    int   negative;  // 1 if negative, 0 otherwise
};
```

Digits are stored in **big-endian** (most-significant first), matching the string representation directly and avoiding reversal overhead in I/O.

### 2.3 Multiplication Dispatch

Three algorithms are chosen based on operand length and available RAM:

```
operand length           algorithm
─────────────────────────────────────────────────────
< g_karatsubaThreshold   naiveMul      O(n²)
< g_nttThreshold         karatsubaMul  O(n^1.585)
≥ g_nttThreshold         nttMul        O(n log n)
```

`g_karatsubaThreshold` (default 64) and `g_nttThreshold` (default 500k–8M depending on RAM) are set at startup by `main()` based on `sysconf(_SC_PHYS_PAGES)`.

**naiveMul** — standard O(n²) school multiplication using an `int[]` accumulator in the arena. Fastest for short numbers due to zero overhead.

**karatsubaMul** — recursive divide-and-conquer. Splits operands at the midpoint, computes three sub-products (`z0`, `z1`, `z2`), reconstructs with `shiftLeft` + `absAdd`. Falls back to `naiveMul` once both operands fit in the naive tier.

**nttMul** — Number Theoretic Transform convolution using **dual-mod CRT**:
- Two independent NTT passes with distinct prime moduli (`NTT_MOD1 = 998244353`, `NTT_MOD2 = 985661441`).
- Results are combined via the Chinese Remainder Theorem using `__int128` arithmetic to avoid 64-bit overflow.
- OOM guard: checks `arenaFree()` before allocating the four `long long` arrays; degrades to Karatsuba if insufficient space.

**nttSquare** — specialised squaring path (used by `bigintPow`): only two NTT forward passes instead of four (exploiting `fa == fb` when `a == b`), cutting NTT cost by half.

### 2.4 Division and Modulo

`bigintDivMod` implements long division with a **binary-search digit selection** instead of trial subtraction:

- For each digit position, binary-search `d ∈ [0, 9]` such that `d × divisor ≤ current_remainder < (d+1) × divisor`.
- Each step uses `naiveMul(divisor, single_digit)` — O(n) — rather than up to 9 subtractions.
- Total cost: O(n × log(10) × M(n)) where M(n) is the cost of multiplying an n-digit number by a single digit.

### 2.5 Exponentiation

`bigintPow` uses **left-to-right binary exponentiation** with an unlimited BigInt exponent:

1. Extract bits of the exponent via repeated `bigintHalf()` (O(n) per bit).
2. Iterate bits from MSB to LSB: `result = result² [× base if bit=1]`.
3. **Per-iteration arena GC**: after each squaring/multiply, snapshot the arena, compute the new result, copy it to the front of the snapshot slot, then `arenaRestore`. This discards all intermediate values and prevents arena exhaustion for cases like `999^160000`.

### 2.6 Parser — Shunting-Yard

`evaluateLine` implements the classic [Shunting-Yard algorithm](https://en.wikipedia.org/wiki/Shunting-yard_algorithm):

- Operator precedence: `+`/`-` (1) < `*`/`/`/`%` (2) < `^` (3) < unary `#`/`_` (4).
- `^` and unary operators are **right-associative**.
- Unary minus is rewritten to operator token `#`; unary plus to `_` (no-op).
- All stacks (`g_opStack`, `g_valStack`) are fixed-size arrays of depth 1024 — no heap allocation needed.

### 2.7 Adaptive Complexity Detection

Before parsing each line, `detectComplexity()` does a single O(n) scan to classify the expression:

```
EXPR_SIMPLE  → no `^`, max digit run ≤ 64, ≤ 10 operators, depth ≤ 3
EXPR_MEDIUM  → longer numbers or more operators
EXPR_HEAVY   → contains `^` or operands > 1000 digits
```

For `EXPR_SIMPLE`, `g_nttThreshold` is temporarily lowered to `g_karatsubaThreshold + 1`, ensuring simple expressions never pay NTT dispatch overhead.

### 2.8 I/O

All I/O is manual to avoid `stdio` overhead:

- **Input**: `read()` in 8 MB chunks into `g_readBuf`; lines are assembled across chunk boundaries into `lineCarry` (max 32 MB).
- **Output**: characters are written to `g_outBuf` (8 MB); `outFlush()` calls `write()` when full or at end.
- Both buffers are `mmap`-allocated.
- Long results are wrapped at 120 characters with `\` + newline (compatible with `bc` output format).

---

## 3. run_all.py Internals

### 3.1 Caching Strategy

Every expensive operation is cached using file modification times (`os.stat().st_mtime`):

| What | Cache key | Invalidation |
|------|-----------|--------------|
| Dataset file | `_is_fresh(path, min_bytes)` | File absent or too small |
| Reference file | `_ref_is_fresh(ds, ref)` | Ref older than dataset |
| Compiled binary | `mtime(bin) >= mtime(src)` | Source newer than binary |

This means repeated runs are near-instant: dataset generation is skipped (O(stat) check), reference files are reused, and `g++` is not invoked unless `calculator.cpp` changed.

### 3.2 Parallel Reference Build

For datasets with more than `PARALLEL_REF_THRESHOLD = 50_000` lines, references are computed in parallel using `multiprocessing.Pool`:

```python
with multiprocessing.Pool(_NCPUS) as pool:
    results = list(pool.imap(eval_full, exprs, chunksize=POOL_CHUNKSIZE))
```

`eval_full` is defined at module level so it is picklable. `POOL_CHUNKSIZE = 10_000` batches IPC to avoid per-line overhead.

`eval_full` uses Python's `eval()` on a sanitised expression (all non-arithmetic characters stripped) with an empty `__builtins__` dict. `^` is replaced with `**` before evaluation.

### 3.3 Dataset Generators — Fast Integer Generation

Several generators use tricks to avoid Python's slow `random.randint()` loop:

**`struct.unpack` on `os.urandom`** (S1): generates all random `uint32` values in one syscall, then unpacks them in bulk — roughly 10× faster than calling `randint()` per line.

**`bytes.translate(TABLE)`** (S4, S11): converts raw random bytes to ASCII digit characters using a 256-byte lookup table — roughly 80× faster than `[str(randint(0,9)) for _ in range(n)]`.

**S11 multiprocessing generation**: the 500 MB S11 dataset is split across all CPUs using `multiprocessing.Pool.map` for generation, then written sequentially.

### 3.4 Verification — `verify()`

Streams three files in parallel using Python generators:

- Reference file (Python-computed answers)
- Output log (binary output)
- Input dataset (for failure reporting)

The inner loop handles `bc`'s line-continuation format (`\` + newline) via `_unwrap()`. It exits on the **first mismatch** to avoid scanning gigabyte logs when a bug is found early.

### 3.5 `bc` Integration

`bc` does not accept `^` for exponentiation — it uses `^` only in some versions. `run_bc()` pre-processes expressions by stripping all non-arithmetic characters and feeds them to `bc` via stdin. `bc` is only run on datasets in `BC_ALLOWED_DATASETS` (S1-100k/1M, S2–S5) to avoid multi-minute runs on large datasets.

### 3.6 Max-Power Probe

`probe_max_power()` binary-searches for the largest exponent `N` such that `base^N` completes within `MAX_POW_SEC = 10.0` seconds for a set of bases. Correctness is checked by comparing the number of output digits against `floor(N × log10(base)) + 1` (exact result digit count is known analytically).

---

## 4. Performance Design Decisions

### Why a custom arena instead of `new`/`malloc`?

Standard allocators have per-allocation metadata and fragmentation. Since BigInt lifetime is strictly scoped to one expression, a bump allocator with snapshot/restore is both faster and eliminates fragmentation entirely.

### Why dual-mod NTT instead of single-mod?

A single NTT modulus with `M ≈ 10⁹` can represent convolution coefficients up to `M - 1`. For two n-digit numbers, convolution coefficients reach up to `9² × n`. With a single mod at 998244353, this is safe only up to `n ≈ 998244353/81 ≈ 12.3M` digits. Dual-mod CRT (product `M1 × M2 ≈ 9.8 × 10¹⁷`) is safe up to `~4.3 × 10⁸` digits, covering all practical inputs.

### Why left-to-right exponentiation instead of right-to-left?

Right-to-left exponentiation keeps a running `b = base^(2^k)` that grows to the full result size by the last bit. Left-to-right exponentiation keeps `result` growing monotonically and discards intermediate `b` values early (via arena GC), which is critical for large exponents where `b` would otherwise accumulate gigabytes of intermediates.

### Why `--no-bc` on large datasets?

`bc` is a serial, non-buffered process that is several orders of magnitude slower than `kalkulacka_2` on large inputs. Running `bc` on S1-10M would take ~20 minutes; on S11 it would exceed any reasonable timeout. The `BC_ALLOWED_DATASETS` whitelist ensures `bc` is only used where it provides useful baseline data.

---

## 5. Adding New Datasets

1. Write a generator function following the pattern of existing ones (e.g. `gen_edge_cases`). Use `_is_fresh(path)` to skip regeneration.
2. Call it from `generate_all_datasets()`.
3. Add the filename to `BC_ALLOWED_DATASETS` if `bc` comparison is meaningful (small/medium dataset only).
4. Add a label to `CATEGORY_LABELS` in `run_all.py` for clean table output.
5. No changes to `calculator.cpp` or the verify/bench pipeline are needed.

---

## 6. Extending the Calculator

### Adding a new operator

1. Assign a precedence in `opPrecedence(char op)`.
2. Set right-associativity in `opRightAssoc(char op)` if needed.
3. Implement the BigInt operation and add a case to `applyOp`.
4. Handle the new character in the `evaluateLine` parser loop.
5. Add a corresponding dataset (S5 covers the existing full operator set; extend it or add S13).

### Changing multiplication thresholds

Thresholds are set in `main()` based on detected RAM. To override them for testing:

```cpp
// At the top of main(), after the threshold-setting block:
g_karatsubaThreshold = 32;    // force Karatsuba earlier
g_nttThreshold       = 500;   // force NTT earlier
```

This lets you benchmark the impact of each algorithm on your hardware.

### Porting to non-Linux

The calculator uses three Linux-specific APIs:
- `mmap` / `munmap` — replace with `malloc` / `free` and remove `MAP_NORESERVE`.
- `sysconf(_SC_PHYS_PAGES)` / `sysconf(_SC_PAGE_SIZE)` — replace with platform-specific RAM detection or hardcode the arena size.
- `read(STDIN_FILENO, ...)` / `write(STDOUT_FILENO, ...)` — replace with `fread` / `fwrite`.

All BigInt algorithms are platform-independent. `__int128` (used in NTT CRT reconstruction) is supported by GCC and Clang on 64-bit platforms; for MSVC, replace with a two-step `__mul128` / manual carry pattern.
