# User Manual — BigInt Calculator Benchmark Suite

## Table of Contents

1. [Installation](#1-installation)
2. [Running the Benchmark](#2-running-the-benchmark)
3. [Command-Line Options](#3-command-line-options)
4. [Understanding the Output](#4-understanding-the-output)
5. [Dataset Details](#5-dataset-details)
6. [Troubleshooting](#6-troubleshooting)

---

## 1. Installation

### System Requirements

| Requirement | Minimum version |
|-------------|----------------|
| Linux       | Kernel 4.x+    |
| Python      | 3.8+           |
| GCC         | 7+ (`-std=c++17`) |
| RAM         | 512 MB         |

> **Note:** `calculator.cpp` uses `mmap` and `sysconf` — it runs on Linux only.

### Steps

1. Place `calculator.cpp` in a `src/` subdirectory next to `run_all.py`:

   ```
   my_project/
   ├── run_all.py
   └── src/
       └── calculator.cpp
   ```

2. Make sure `g++` is installed:

   ```bash
   g++ --version
   ```

3. (Optional) Install `bc` for reference comparisons:

   ```bash
   sudo apt install bc      # Debian/Ubuntu
   sudo dnf install bc      # Fedora/RHEL
   ```

No Python packages beyond the standard library are required.

---

## 2. Running the Benchmark

### Full run (recommended first time)

```bash
python3 run_all.py
```

This will:

1. **Compile** `calculator.cpp` with `-O3 -march=native` (skipped if binary is already up to date).
2. **Generate** all 12 datasets (skipped if they already exist and are unchanged).
3. **Build reference answers** using Python's native big integers (cached per dataset).
4. **Benchmark** `kalkulacka_2` (and optionally `bc`) on every dataset.
5. **Print** a result table and save a JSON report in `results/`.

The first run can take several minutes due to dataset generation (especially S1 at 10M lines and S11 at 1M × 500-digit lines). Subsequent runs are much faster because all data is cached.

---

## 3. Command-Line Options

### `--no-bc`

Skip the `bc` comparison. Use this if `bc` is not installed or if you only care about `kalkulacka_2` performance.

```bash
python3 run_all.py --no-bc
```

### `--no-gen`

Skip dataset generation and reference building. Assumes all datasets and reference files already exist in `datasets/` and `reference/`.

```bash
python3 run_all.py --no-gen
```

### `--max-power`

Run the max-power probe after benchmarking. This binary-searches for the largest exponent `N` such that `base^N` completes correctly within a 10-second time budget for several bases (2, 3, 10, 100, 999).

```bash
python3 run_all.py --max-power
```

### Filter by dataset name

Pass a partial dataset name to run only matching datasets:

```bash
python3 run_all.py s3_edge       # runs only s3_edge.txt
python3 run_all.py s1            # runs all s1_stream_* files
python3 run_all.py bigint        # runs s2_bigint.txt
```

### Combining options

Options can be combined freely:

```bash
python3 run_all.py --no-bc --no-gen s7
python3 run_all.py --max-power --no-bc
```

---

## 4. Understanding the Output

### Progress log

While running you will see messages like:

```
[0/3] Compiling...
  kalkulacka_2: binary up to date, skipping compile

[1/3] Generating datasets...
  s1_stream_100k.txt               (cached)
  ...

[2/3] Building references (Python native bigint)...
  ref_s1_stream_100k.txt           (cached)
  ...

[3/3] Benchmarking 12 dataset(s)...
```

### Result table

```
========================================================================================
  BENCHMARK RESULTS — ABSOLUTE LOAD EDITION
                                   MB | K2 (s)  acc | bc (s)  acc
  --------------------------------------------------------------------------------------

  -- S1  Stream scalability (100k–10M lines) --
  s1_stream_100k.txt             8.1 |    0.041 100%  OK |    0.214 100%  OK
  s1_stream_1000k.txt           81.3 |    0.398 100%  OK |    2.143 100%  OK
  s1_stream_5000k.txt          406.3 |    1.983 100%   .. |              (k2 only)
  s1_stream_10000k.txt         812.6 |    3.961 100%   .. |              (k2 only)
```

Column meanings:

| Column     | Description |
|------------|-------------|
| `MB`       | Dataset file size in megabytes |
| `K2 (s)`   | Wall-clock time for `kalkulacka_2` |
| `acc`      | Accuracy: percentage of lines matching the Python reference |
| `bc (s)`   | Wall-clock time for `bc` (only on small/medium datasets) |
| Status icon `OK` | All verified lines passed |
| Status icon `!!` | One or more failures or a crash/timeout |
| Status icon `..` | Dataset not run for this tool (too large for bc) |

### Failure details

If a dataset fails verification, the first mismatch is printed:

```
    K2 FAIL@L42
       input:    123456789^99
       expected: 1234567890...
       actual:   (empty)
```

### Final scorecard

```
  FINAL SCORECARD

  kalkulacka_2 (calculator.cpp) [full ops]
    Passed : 12/12 datasets
    Lines  : 12,345,678
    Time   : 8.42s
    Speed  : 1.46M l/s
```

### JSON report

A machine-readable report is written to `results/bench_YYYYMMDD_HHMMSS.json` containing all timing, accuracy, and failure data for programmatic analysis.

---

## 5. Dataset Details

### S1 — Stream scalability

Four file sizes: 100k, 1M, 5M, 10M lines. Each line is `A+B` or `A-B` with operands in `[10, 10⁹)`. Tests raw throughput. `bc` is only run on the two smaller sizes; the 5M and 10M files are `kalkulacka_2`-only.

### S2 — BigInt correctness

15 lines with operand sizes 100, 1 000, 10 000, 100 000, and 1 000 000 digits. Each size has three cases: `a+b`, `a-b`, `b-a`. Verifies correct carry propagation for very large numbers.

### S3 — Edge cases

Manually crafted expressions: zeros, near-zero differences, large power-of-10 boundaries, sign crossings, and 100-digit operands. Every correctness edge case in one file.

### S4 — Memory pressure

100k lines, each with operands of 100–400 digits. Stresses both memory bandwidth and the BigInt allocator.

### S5 — Full operator set

Uses all supported operators: `*`, `/`, `%`, `^`, `()`, and unary minus. Small numbers to keep the reference build fast.

### S6 — Extreme single line

Two lines, each with 1M-digit operands (`a+b` and `a-b`). Tests parser and addition/subtraction at maximum scale.

### S7 — Multiplication stress

Covers all three multiplication tiers: naive O(n²) (≤63 digits), Karatsuba O(n^1.585) (64–threshold digits), and NTT O(n log n) (above threshold). Operands range from 1 digit to 200 000 digits.

### S8 — Division & modulo deep

Division and modulo with multi-thousand digit divisors. Tests the binary-search + multiply division algorithm.

### S9 — Power stress

`base^exp` for bases 2, 3, 7, 10, 99 and exponents up to 100 000. Exercises the binary exponentiation loop and NTT squaring path.

### S10 — Mixed expression depth

Deeply nested parentheses (up to depth 500), mixed-operator chains, and double unary minus. Tests the Shunting-Yard parser under stress.

### S11 — Sustained BigInt stream

1M lines, each with two 500-digit operands (`a+b`). ~500 MB dataset. Tests sustained throughput and memory reuse under a realistic long-running workload.

### S12 — Adversarial

All-nines numbers (9...9 + 1), alternating 90/10 patterns, ones-squared, and near-midpoint subtractions. Designed to maximise carry chains and expose off-by-one errors.

---

## 6. Troubleshooting

### "ERROR: kalkulacka_2 failed to compile"

Check that `g++` supports C++17:

```bash
g++ --version        # needs 7+
g++ -std=c++17 -x c++ /dev/null -o /dev/null
```

If compiling manually fails, inspect the full error:

```bash
g++ -O3 -march=native -std=c++17 -o bin/kalkulacka_2 src/calculator.cpp
```

### Datasets not being regenerated

Datasets are skipped if the file already exists and has at least 1 000 bytes. To force regeneration, delete the relevant file:

```bash
rm datasets/s3_edge.txt
python3 run_all.py s3_edge
```

### Reference files seem wrong

Reference files are regenerated whenever the dataset file is newer. To force a rebuild:

```bash
rm reference/ref_s3_edge.txt.txt
python3 run_all.py s3_edge
```

### `bc` is missing

Install it or use `--no-bc`. The benchmark runs fine without `bc`; it is only used as a reference comparison on small/medium datasets.

### Out-of-memory crash in `kalkulacka_2`

`calculator.cpp` auto-detects available RAM and scales its arena. On machines with less than 512 MB free RAM, it may fail on S11 or large S7/S9 cases. Try running with `--no-gen` after removing the largest datasets.

### Very slow reference build

S1-10M and S11 reference builds use Python multiprocessing. On a single-core machine this can take 10+ minutes. Use `--no-gen` on subsequent runs or filter to a single small dataset while developing.
