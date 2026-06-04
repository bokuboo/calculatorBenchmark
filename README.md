# 📊 Advanced BigInt Load, Stress & Performance Benchmarking Suite

This repository contains a comprehensive, automated framework designed for **differential correctness testing**, boundary condition validation, and execution throughput benchmarking of arbitrary-precision (**BigInt**) calculation engines.

The core of this suite is an intelligent orchestration harness (`run_all.py`). It dynamically synthesizes highly adversarial payloads, multi-directionally verifies output correctness against a trusted reference model (Python's native BigInt), and evaluates processing bandwidth against standard system tools like POSIX `bc`.

---

## 🛠 Test Matrix & QA Coverage

The benchmark orchestrator segregates verification into **12 specialized test suites (S1 – S12)**. These suites are mapped to evaluate distinct operational boundaries, syntax parsing depths, and memory sub-allocation mechanics:

| Suite ID | Dataset Name | QA Verification Focus | Test Load / Scale |
| :--- | :--- | :--- | :--- |
| **S1** | Stream Scalability | High-frequency throughput; validation of long-running stability and memory leak detection. | 100k to 10M stream lines |
| **S2** | BigInt Correctness | Basic arithmetic correctness (`+`, `-`) across wide bit-widths and digit spans. | 100 to 1M digits |
| **S3** | Edge Cases | Strict validation of structural anomalies: signed zeros, sign-crossing subtraction, and wrap-around boundaries. | Target-specific token streams |
| **S4** | Memory Pressure | Evaluates the allocator's ability to instantly reuse, clear, and realign intermediate heap segments. | 100k lines / 400-digit operands |
| **S5** | Full Operator Set | Evaluates complete operator precedence execution (`*`, `/`, `%`, `^`), unary states, and bracket hierarchy. | High-complexity syntax trees |
| **S6** | Extreme Single Line | Peak algorithmic stress test targeting maximum multiplication scales (Karatsuba and NTT limits). | Single-line million-digit operands |
| **S7** | Multiplication Stress | Validates correct branch dispatch and dynamic crossover thresholds (**Naive** $\to$ **Karatsuba** $\to$ **NTT**). | Linearly scaled digit lengths |
| **S8** | Division & Modulo Deep | Stability and speed check of the division/modulo algorithm under deep multi-thousand digit divisors. | High-width dividend processing |
| **S9** | Power Stress | Validates binary exponentiation structures, maximum-exponent safeguards, and `OVERFLOW` handling. | Massive bases with large powers |
| **S10** | Mixed Expression Depth| Stress tests the structural limits of the internal lexer/parser against deeply nested parentheses. | High-density infix expressions |
| **S11** | Sustained BigInt Stream | Combined high-stress profile: High volume of stream lines processing massive individual operands concurrently. | 1M lines / 500-digit numbers |
| **S12** | Adversarial Patterns | Explicit attack vectors targeting algorithmic weaknesses: all-9s processing (`999...`), alternating ciphers (`10101...`). | Anti-recurrence sequences |

---

## ⚙️ Command-Line Interface (CLI) Reference

The `run_all.py` test harness offers robust CLI controls to enable targeted validation, continuous integration filtering, and rapid debugging cycles.

### 1. Complete Execution (Default Mode)
Compiles the core engine, generates all analytical datasets, and runs the entire matrix across the native binary and system `bc` while asserting absolute verification.
```bash
python3 run_all.py
```

### 2. Fast Track — Bypass `bc` (`--no-bc`)
The native system utility `bc` utilizes standard $O(n^2)$ schoolbook multiplication algorithms, making it extremely sluggish on multi-million digit inputs. To isolate and accelerate validation of your custom binary, bypass it:
```bash
python3 run_all.py --no-bc
```

### 3. Test Isolation & Substring Filtering
If debugging a localized defect (e.g., within division or specific edge conditions), you can bypass the entire matrix and execute only matching test groups by appending a search string:
```bash
python3 run_all.py s3_edge

# Or target all multiplication profiles
python3 run_all.py multiplication
```

### 4. Skip Data Synthesis (`--no-gen`)
Generating multi-gigabyte textual payloads can stress disk storage and eat up valuable time. If the payload files have already been generated and you are purely modifying compiler configurations or logic bugs, recycle the current sets:
```bash
python3 run_all.py --no-gen
```

### 5. Maximum Capacity Power Probe (`--max-power`)
A dedicated extreme-load mode that circumvents standard datasets to run an isolated, high-ceiling performance sweep designed to measure the absolute theoretical processing peak of the NTT pipeline.
```bash
python3 run_all.py --max-power
```

---

## 🚀 Step-by-Step QA Execution Workflow

### Step 1: Environment Preparation
Ensure your target Linux runtime environment contains a functional C++ toolchain and a Python 3 distribution. On Debian/Ubuntu environments, execute:
```bash
sudo apt update && sudo apt install build-essential python3 bc
```

### Step 2: Binary Deployment
The orchestration harness expects the target engine binary to reside in the active execution workspace under the exact filename `calculator`. Compile your source using aggressive microarchitectural vectorization overrides:
```bash
g++ -O3 -march=native calculator.cpp -o calculator
```

### Step 3: Triggering Validation
Initialize the test framework in quick validation mode to verify runtime compatibility:
```bash
python3 run_all.py --no-bc
```

---

## 📊 Interpreting the Scorecard Report

Upon final execution, the framework prints a deterministic text-based validation scorecard directly into the terminal standard output.

```text
=========================================================
                  BENCHMARK SCORECARD
=========================================================
Target Binary (calculator.cpp):
  -> STATUS: PASS (12/12 test suites successful)
  -> Lines Evaluated: 12,450,000
  -> Cumulative Compute Time: 14.2345 s
  -> Average Processing Throughput: 84.2 MB/s
---------------------------------------------------------
bc (System Reference):
  -> STATUS: TIMEOUT / FAIL (Suite S6 exceeded time ceiling)
  -> Cumulative Compute Time: > 300.00 s
```

### Key Metrics to Monitor for QA Analysis:
* **Differential STATUS (`PASS` / `FAIL`):** The system validates outcomes character-by-character. If even a single digit deviates from the Python evaluation model anywhere in a multi-million-digit string, the suite instantly raises a `FAIL` flag and logs the faulty stream index.
* **Data Throughput (MB/s):** Reflects the raw volume of mathematical text the calculator effectively parses, computes, and flushes per second. A higher throughput score correlates with an optimized low-level memory arena and custom I/O buffering.
* **Cross-Engine Comparison:** By juxtaposing the custom binary's metrics against system `bc` under high scaling thresholds (S6, S7), you can observe the exact inflection point where advanced algorithms (Karatsuba, NTT) outpace traditional implementations.

---

## 🛠 Advanced Profiling Tips (Multi-Binary and Core Locking)

* **Excluding OS Scheduler Interrupts:** To achieve pure, deterministic, and noise-free performance metrics during scientific benchmarks, clamp the orchestration sequence to an exclusive physical execution core:
  ```bash
  taskset -c 0 python3 run_all.py --no-bc
  ```
* **Testing Alternative Builds:** To run comparison benchmarks between alternative compiler outputs (e.g., comparing a GCC compilation profile versus a Clang profile), update the `CALC2_BIN` variable path located at the header section of the `run_all.py` script to point to your respective test binaries.

---
> ⚠️ **Platform Enforcement Warning:** This benchmarking ecosystem targets low-level architecture hooks bound natively to POSIX paradigms. Attempting to deploy this environment on non-POSIX compliant hosts (such as raw Windows systems without WSL2 or MSYS2 wrappers) will cause shell execution and file mapping failures.
```