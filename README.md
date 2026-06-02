# Calculator Benchmark Suite

A single-file, zero-dependency benchmark for Ubuntu/Linux that measures the **time performance, functional correctness, and limits** of C++ big-integer calculator against the Linux-native `bc` reference.

| Binary | Source file | Operators | Memory model |
|--------|-------------|-----------|--------------|
| `kalkulacka_2` | `src/calculator.cpp` | `+` `-` `*` `/` `%` `^` `()` unary | 512 MB arena, 2 M digit limit |
| `bc` | system (`bc -l`) | all above | arbitrary precision |

---

## Directory structure

```
.
├── run_all.py            # THE FILE — run this
├── src/
│   └── calculator.cpp    # calc2 (kalkulacka_2): full op set, arena allocator
├── bin/                  # compiled binaries are placed here
├── datasets/             # generated test inputs (lazy — created on first run)
├── reference/            # Python-native reference outputs (lazy)
└── results/              # JSON result files + log files
```

---

## Quick start on Ubuntu

```bash
# 1. Install prerequisites (if missing)
sudo apt update
sudo apt install -y g++ python3 bc

# 2. Navigate to the benchmark directory
cd /path/to/calculatorBenchmark

# 3. Run the full benchmark
python3 run_all.py

# 4. Done — watch the console table for results
```

---

## CLI usage

```
python3 run_all.py [OPTIONS] [DATASET]

Options:
  (no args)          Run full suite (compile → generate → benchmark → report)
  --no-bc            Skip `bc` reference and throughput comparison
  s3_edge            Run only datasets whose name contains "s3_edge"

Examples:
  python3 run_all.py                    # everything
  python3 run_all.py --no-bc            # skip bc (faster)
  python3 run_all.py s2_bigint          # run bigint dataset only
  python3 run_all.py s6_1Mdigits        # run the 1M-digit stress test
```

---

## What gets measured

The benchmark automatically:

1. **Compiles** both calculators with `g++ -O3 -std=c++17`
2. **Generates 6 dataset categories** (only if not already present):
   | Cat | File | What it tests |
   |-----|------|---------------|
   | S1  Stream | `s1_stream_100k/1m/3m.txt` | Pure +/- stream: 100K / 1M / 3M lines |
   | S2  BigInt | `s2_bigint.txt` | 100, 1K, 10K, 100K digit +/- pairs |
   | S3  Edge | `s3_edge.txt` | Zeros, negatives, single digit, zero-crossing |
   | S4  Memory | `s4_memory.txt` | 100K lines x 100-400 digit numbers |
   | S5  Calc2-only | `s5_calc2.txt` | `* / % ^ ( )` and unary minus |
   | S6  Extreme | `s6_1Mdigits.txt` | 2 lines x 1M digits each |
3. **Builds Python-native references** — the exact output a correct calculator should produce
4. **Runs every binary** against every dataset piping via stdin
5. **Compares output line-by-line** against the reference (first mismatch stops the run)
6. **Shows a table** with wall time, accuracy %, and status for each runner
7. **Saves a JSON** file under `results/`

### Comparison table legend

| Column | Meaning |
|--------|---------|
| `K1 (s)` | Wall-clock seconds for `kalkulacka` (calc1) |
| `K1%` | Line-by-line accuracy vs Python simple (+/-) reference |
| `K2 (s)` | Wall-clock seconds for `kalkulacka_2` (calc2) |
| `K2%` | Accuracy vs full-expression reference |
| `bc (s)` | Wall-clock seconds for system `bc -l` |
| `bc%` | Accuracy vs same full-expression reference |
| Ratio | How many times faster K1 is over K2 (or vice versa and reverse nothing |

### Status codes

| Status | Meaning |
|--------|---------|
| `PASS` | 99.5 %+ accuracy, exit code 0 |
| `PARTIAL` | Some correct, some mismatches (expected for calc1 on S5) |
| `FAIL@Lnn` | First mismatch at line `nn` |
| `TIMEOUT` | Did not finish in 10 minutes |
| `CRASH` | Non-zero exit code |
| `EMPTY` | Dataset had no lines solvable by this reference type |

---

## Expected behaviour of each calculator

### `calculator.cpp` (kalkulacka_2)

- Correctly handles: full expressions, all operators, parentheses, unary minus
- Outputs 70-char line wrapped numbers with `\` continuation
- 64 MB arena, 2 M digit limit
- Skips lines starting with `(`
- Expected: 100 % accuracy on all datasets

### `bc` (system `bc -l`)

- Arbitrary precision, native Linux calculator
- Outputs plain integer = for +, -, *, ^; floats for / and %
- `bc -l` understands ** (via ^ translation)
- Expected: 100 % accuracy on S1-S4, S6; very high on S5

---

## Representative output

```
==============================================================================
   BENCHMARK SUITE  -  calculator.cpp vs main.cpp vs bc
   main.cpp           : src/main.cpp
   calculator.cpp     : src/calculator.cpp
   Started            : 2026-05-31 14:22:01
   bc                 : included
==============================================================================

[0/3] Compiling calculators (g++ -O3 -std=c++17)...
  Compiling kalkulacka   ... OK
  Compiling kalkulacka_2 ... OK

[1/3] Generating datasets...
  s1_stream_100k.txt         2.1 MB  (0.2s)
  s1_stream_1m.txt          21.4 MB  (1.8s)
  s1_stream_3m.txt          62.9 MB  (5.3s)
  s2_bigint.txt                     8.0 KB
  s3_edge.txt                       0.5 KB
  s4_memory.txt               17.9 MB  (1.1s)
  s5_calc2.txt                         1.2 KB
  s6_1Mdigits.txt                  2.00 MB  (0.4s)

[2/3] Building references (Python native bigint)...
  ref1_s1_stream_100k.txt   100,000 lines [calc1 (simple)]
  ref2_s1_stream_100k.txt   100,000 lines [calc2/bc (full)]
  ...
  ref1_s6_1Mdigits.txt          2 lines [calc1 (simple)]
  ref2_s6_1Mdigits.txt          2 lines [calc2/bc (full)]

[3/3] Benchmarking 8 datasets...

==============================================================================
       BENCHMARK RESULTS
                              MB  |    K1 (s)  acc  |    K2 (s)  acc  |    bc (s)  acc
  --------------------------------------------------------------------------------

  -- S1 Stream (we are kalkulacka level, 100k-1M lines) --
  s1_stream_100k.txt             2.1 |    0.012  100%  OK |    0.089  100%  OK |    0.041  100%  OK
  s1_stream_1m.txt              21.4 |    0.128  100%  OK |    0.923  100%  OK |    0.415  100%  OK
  s1_stream_3m.txt              62.9 |    0.378  100%  OK |    2.745  100%  OK |    1.228  100%  OK

  -- S2 BigInt (100-100K digits, +/- only) --
  s2_bigint.txt                       9.0 |    0.001  100%  OK |    0.004  100%  OK |    0.002  100%  OK

  -- S3 Edge (negatives, zero, sign tests) --
  s3_edge.txt                         0.5 |    0.000  100%  OK |    0.000  100%  OK |    0.000  100%  OK

  -- S4 Memory (100K lines, 100-400 digits) --
  s4_memory.txt                 17.9 |    0.987  100%  OK |    3.210  100%  OK |    1.542  100%  OK

  -- S5 Calc2-only (* / % ^ () unary, calc2 & bc territory) --
  s5_calc2.txt                         1.2 |    0.000   0%  .. |    0.005  95%  OK |    0.003  92%  OK
      K1 EMPTY

  -- S6 Extreme (2 lines, 1M digits each) --
  s6_1Mdigits.txt                2.0 |    0.004  100%  OK |    0.014  100%  OK |    0.009  100%  OK

==============================================================================
       FINAL SCORECARD

  kalkulacka_2 (calculator.cpp) [full]
    Passed : 8/8 datasets
    Lines   : 4,200,143
    Time    : 7.10s
    Speed   : 591K l/s

  bc (system bc -l) [full]
    Passed : 8/8 datasets
    Lines   : 4,200,143
    Time    : 3.55s
    Speed   : 1,183K l/s

  Note: calc1 (main.cpp) handles only + and -.  Failures in s5_calc2.txt are expected.
        calc2 (calculator.cpp) handles +-*/%^() and unary minus.
        bc is the Linux native arbitrary-precision calculator.
==============================================================================

  JSON saved: results/bench_20260531_142201.json
```

*(Output is illustrative — actual numbers will differ based on your hardware.)*

---

## Customisation

- Change `TIMEOUT_SEC` near the top of `run_all.py` for slower 1M-digit runs
- Change `STREAM_SIZES` to adjust the scalability test workload
- Add new dataset categories by adding a `gen_*` function and calling it from `generate_all_datasets()`
