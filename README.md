# BigInt Calculator Benchmark Suite

A comprehensive benchmark and correctness-verification suite for **`calculator.cpp`** — an ultra-fast, stream-based, arbitrary-precision integer calculator written in C++.

## Overview

This project benchmarks `calculator.cpp` (compiled as `kalkulacka_2`) against Python's native big-integer evaluator and optionally the system `bc` tool, across 12 purpose-built datasets covering everything from basic stream throughput to adversarial edge cases.

## Project Structure

```
.
├── run_all.py          # Main benchmark runner (Python 3)
├── src/
│   └── calculator.cpp  # C++ calculator source
├── bin/
│   └── kalkulacka_2    # Compiled binary (auto-generated)
├── datasets/           # Generated test datasets (auto-generated)
├── reference/          # Python-computed reference answers (auto-generated)
└── results/            # Benchmark logs and JSON reports (auto-generated)
```

## Quick Start

### Prerequisites

- Python 3.8+
- GCC with C++17 support (`g++`)
- Linux (uses `mmap`, `sysconf`)
- Optional: `bc` (system arbitrary-precision calculator)

### Run the full benchmark

```bash
python3 run_all.py
```

### Skip `bc` comparison (faster)

```bash
python3 run_all.py --no-bc
```

### Run a single dataset

```bash
python3 run_all.py s3_edge
```

### Run the max-power probe

```bash
python3 run_all.py --max-power
```

### Reuse existing datasets (skip generation)

```bash
python3 run_all.py --no-gen
```

## Datasets

| ID  | Name                     | Description                                      |
|-----|--------------------------|--------------------------------------------------|
| S1  | Stream scalability       | 100k / 1M / 5M / 10M lines of `+`/`-`           |
| S2  | BigInt correctness       | 100 – 1M digit operands                          |
| S3  | Edge cases               | Zeros, sign crossings, wrap boundary             |
| S4  | Memory pressure          | 100k lines, 100–400 digit operands               |
| S5  | Full operator set        | `* / % ^ ()` and unary minus                    |
| S6  | Extreme single line      | 1M-digit operands                                |
| S7  | Multiplication stress    | Naive / Karatsuba / NTT tiers                    |
| S8  | Division & modulo deep   | Multi-thousand digit divisors                    |
| S9  | Power stress             | Large bases, large exponents                     |
| S10 | Mixed expression depth   | Deeply nested parentheses                        |
| S11 | Sustained BigInt stream  | 1M lines of 500-digit numbers                    |
| S12 | Adversarial              | All-9s, alternating, near-overflow patterns      |

## Output

Results are printed to stdout as a formatted table and also saved as a timestamped JSON file in `results/bench_YYYYMMDD_HHMMSS.json`.

## License

See LICENSE for details.
