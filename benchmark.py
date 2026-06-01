#!/usr/bin/env python3
"""
benchmark.py — Diagnostický benchmark pre kalkulačky (main.cpp a calculator.cpp)

Štruktúra priečinkov:
  bin/          — skompilované binárky (kalkulacka, kalkulacka_2)
  datasets/     — vstupné datasety
  reference/    — referenčné výstupy z `bc`
  logs/         — výstupy kalkulačiek (generované automaticky)

Použitie:
  python3 benchmark.py [--compile] [--generate] [--dataset 1|2|3|all]

Flags:
  --compile     Skompiluje main.cpp a calculator.cpp do bin/
  --generate    Vygeneruje datasety do datasets/
  --dataset N   Spustí benchmark iba pre dataset N (1, 2, 3, alebo all)
"""

import subprocess
import os
import sys
import time
import argparse
import random

# ─────────────────────────────────────────────
# Konfigurácia ciest
# ─────────────────────────────────────────────
DIRS = {
    "bin":       "bin",
    "datasets":  "datasets",
    "reference": "reference",
    "logs":      "logs",
}

BINARIES = {
    "kalkulacka":   os.path.join("bin", "kalkulacka"),
    "kalkulacka_2": os.path.join("bin", "kalkulacka_2"),
}

SOURCES = {
    "kalkulacka":   "main.cpp",
    "kalkulacka_2": "calculator.cpp",
}

DATASETS = {
    1: {
        "input":     os.path.join("datasets", "dataset1_stream.txt"),
        "reference": os.path.join("reference", "bc_vystup1.txt"),
        "label":     "Dataset 1 — Stream (základné operátory, zátvorky)",
    },
    2: {
        "input":     os.path.join("datasets", "dataset2_bigint.txt"),
        "reference": os.path.join("reference", "bc_vystup2.txt"),
        "label":     "Dataset 2 — BigInt (1 000 000-ciferné čísla)",
    },
    3: {
        "input":     os.path.join("datasets", "dataset3_power.txt"),
        "reference": os.path.join("reference", "bc_vystup3.txt"),
        "label":     "Dataset 3 — Power (mocniny, komplexné výrazy)",
    },
}


# ─────────────────────────────────────────────
# Pomocné funkcie
# ─────────────────────────────────────────────
def ensure_dirs():
    for d in DIRS.values():
        os.makedirs(d, exist_ok=True)


def log(msg, indent=0):
    prefix = "   " * indent
    print(f"{prefix}{msg}")


def ok(msg, indent=1):  log(f"✅ {msg}", indent)
def err(msg, indent=1): log(f"❌ {msg}", indent)
def info(msg, indent=1): log(f"ℹ️  {msg}", indent)
def warn(msg, indent=1): log(f"⚠️  {msg}", indent)


# ─────────────────────────────────────────────
# Kompilácia
# ─────────────────────────────────────────────
def compile_all():
    log("\n══════════════════════════════════════════")
    log("🔨 KOMPILÁCIA ZDROJOVÝCH SÚBOROV")
    log("══════════════════════════════════════════")
    ensure_dirs()

    results = {}
    for name, src in SOURCES.items():
        out = BINARIES[name]
        if not os.path.exists(src):
            err(f"{src} nenájdený — preskočené.")
            results[name] = False
            continue

        cmd = ["g++", "-O2", "-std=c++17", "-o", out, src]
        log(f"\n  Kompilujem {src} → {out}")
        log(f"  Príkaz: {' '.join(cmd)}", 1)

        t0 = time.time()
        proc = subprocess.run(cmd, capture_output=True, text=True)
        elapsed = time.time() - t0

        if proc.returncode == 0:
            ok(f"Skompilovanie úspešné ({elapsed:.2f}s)")
            results[name] = True
        else:
            err(f"Chyba kompilácie!")
            log(proc.stderr.strip(), 2)
            results[name] = False

    return results


# ─────────────────────────────────────────────
# Generovanie datasetov
# ─────────────────────────────────────────────
def generate_dataset_1(path, target_mb=10):
    """Generuje stream dataset (zmenšený — 10 MB namiesto 1 GB pre rýchly test)."""
    log(f"\n  Generujem {path} ({target_mb} MB)...")
    target_bytes = target_mb * 1024 * 1024
    bytes_written = 0
    with open(path, "w") as f:
        while bytes_written < target_bytes:
            a = random.randint(1, 1_000_000)
            b = random.randint(1, 1_000_000)
            c = random.randint(1, 100)
            lines = [
                f"({a} + {b}) / {c}\n",
                f"-{a} - (+{b}) % {c}\n",
                f"{a} + {b} - {a} + {b}\n",
            ]
            line = random.choice(lines)
            f.write(line)
            bytes_written += len(line.encode("utf-8"))
    ok(f"Vygenerovaný: {bytes_written / 1024 / 1024:.1f} MB")


def generate_dataset_2(path):
    """Generuje 2 riadky s 1 000 000-cifernými číslami."""
    log(f"\n  Generujem {path} (BigInt stres-test)...")
    num_1 = "9" + "".join(str(random.randint(0, 9)) for _ in range(999_999))
    num_2 = "8" + "".join(str(random.randint(0, 9)) for _ in range(999_999))
    with open(path, "w") as f:
        f.write(f"{num_1} + {num_2}\n")
        f.write(f"{num_1} - {num_2}\n")
    ok("Vygenerovaný: 2 riadky × 1 000 000 číslic")


def generate_dataset_3(path):
    """Generuje mocninové výrazy."""
    log(f"\n  Generujem {path} (Power test)...")
    base_1 = "2" + "".join(str(random.randint(0, 9)) for _ in range(9_999))
    with open(path, "w") as f:
        f.write(f"{base_1} ^ 500\n")
        f.write(f"7 ^ 100000\n")
        f.write(f"(99 ^ 1000) + (2 ^ 200000)\n")
    ok("Vygenerovaný: 3 riadky (mocniny)")


def generate_all():
    log("\n══════════════════════════════════════════")
    log("📦 GENEROVANIE DATASETOV")
    log("══════════════════════════════════════════")
    ensure_dirs()

    generate_dataset_1(DATASETS[1]["input"])
    generate_dataset_2(DATASETS[2]["input"])
    generate_dataset_3(DATASETS[3]["input"])

    log("\n  Generujem referenčné výstupy cez `bc` ...")
    for ds_id, ds in DATASETS.items():
        ref = ds["reference"]
        inp = ds["input"]
        if not os.path.exists(inp):
            warn(f"Vstupný súbor {inp} chýba — preskočené.")
            continue
        log(f"  bc < {inp} > {ref}")
        t0 = time.time()
        with open(inp, "r") as fin, open(ref, "w") as fout:
            proc = subprocess.run(["bc"], stdin=fin, stdout=fout, stderr=subprocess.DEVNULL)
        elapsed = time.time() - t0
        if proc.returncode == 0:
            ok(f"Referencia pre Dataset {ds_id} hotová ({elapsed:.1f}s)")
        else:
            err(f"bc zlyhalo pre Dataset {ds_id}!")


# ─────────────────────────────────────────────
# Spustenie kalkulačky
# ─────────────────────────────────────────────
def run_calculator(binary_name, dataset_path, log_path):
    """Spustí binárku so vstupom z datasetu a zapíše výstup do logu."""
    binary = BINARIES[binary_name]
    if not os.path.exists(binary):
        err(f"Binárka {binary} nenájdená!")
        return False, 0.0

    t0 = time.time()
    with open(dataset_path, "r") as fin, open(log_path, "w") as fout:
        proc = subprocess.run(
            [f"./{binary}"],
            stdin=fin,
            stdout=fout,
            stderr=subprocess.DEVNULL,
            timeout=300,
        )
    elapsed = time.time() - t0

    if proc.returncode != 0:
        warn(f"Binárka skončila s návratovým kódom {proc.returncode}")

    return True, elapsed


# ─────────────────────────────────────────────
# Porovnávanie výstupov
# ─────────────────────────────────────────────
def normalize_bc_line(line: str) -> str:
    """Odstráni zalamovanie riadkov 'bc' (spätné lomítko + newline)."""
    return line.replace("\\\n", "").replace("\\\r\n", "").strip()


def compare_outputs(reference_path, actual_path, input_path, label: str):
    """Riadok po riadku porovná výstup kalkulačky s referenciou."""
    if not os.path.exists(actual_path):
        return 0, 0, f"Súbor s výstupom neexistuje: {actual_path}"

    if not os.path.exists(reference_path):
        return 0, 0, f"Referenčný súbor neexistuje. Spusti benchmark s --generate."

    correct = 0
    total = 0
    stop_reason = "Dosiahnutý koniec súboru — všetky riadky správne"

    with open(reference_path, "r") as f_ref, \
         open(actual_path, "r") as f_act, \
         open(input_path, "r") as f_in:

        for line_idx, (ref_line, in_line) in enumerate(zip(f_ref, f_in), start=1):
            ref_clean = normalize_bc_line(ref_line)
            if not ref_clean:
                continue

            act_line = f_act.readline()
            act_clean = act_line.strip() if act_line else ""
            total += 1

            if act_clean == ref_clean:
                correct += 1
            else:
                zadanie = in_line.strip()[:70]
                if not act_line:
                    stop_reason = (
                        f"STOP na riadku {line_idx}: Program prestal generovať výstup "
                        f"(crash alebo prázdny výstup).\n"
                        f"     Zadanie:    {zadanie}"
                    )
                else:
                    stop_reason = (
                        f"LIMIT na riadku {line_idx}: Nesúhlas výstupu.\n"
                        f"     Zadanie:    {zadanie}\n"
                        f"     Očakávané:  {ref_clean[:60]}\n"
                        f"     Vrátilo:    {act_clean[:60]}"
                    )
                break

    return correct, total, stop_reason


# ─────────────────────────────────────────────
# Benchmark jedného datasetu
# ─────────────────────────────────────────────
def benchmark_dataset(ds_id: int):
    ds = DATASETS[ds_id]
    label = ds["label"]
    inp = ds["input"]
    ref = ds["reference"]

    log(f"\n{'═' * 70}")
    log(f"🔍 {label}")
    log(f"{'═' * 70}")

    if not os.path.exists(inp):
        err(f"Vstupný súbor {inp} chýba. Spusti --generate.")
        return

    results = {}
    calcs = {
        "kalkulacka":   ("main.cpp", f"logs/vystup_k1_ds{ds_id}.txt"),
        "kalkulacka_2": ("calculator.cpp", f"logs/vystup_k2_ds{ds_id}.txt"),
    }

    # Spustenie oboch kalkulačiek
    for name, (src_label, log_path) in calcs.items():
        log(f"\n  Spúšťam {name} ({src_label}) na {inp} ...")
        ok_run, elapsed = run_calculator(name, inp, log_path)
        results[name] = {"elapsed": elapsed, "log": log_path, "src": src_label, "ran": ok_run}
        if ok_run:
            log(f"  Beh dokončený za {elapsed:.2f}s", 1)

    # Porovnanie výstupov
    log(f"\n  Porovnávam výstupy s referenciou ({ref}) ...")
    for name, meta in results.items():
        correct, total, reason = compare_outputs(ref, meta["log"], inp, name)

        log(f"\n  📊 {name} ({meta['src']}):")
        log(f"     Správnych riadkov: {correct} / {total}", 1)
        log(f"     Čas behu:          {meta['elapsed']:.2f}s", 1)
        log(f"     Výsledok:          {reason}", 1)

    log("")


# ─────────────────────────────────────────────
# Hlavný vstupný bod
# ─────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Diagnostický benchmark pre main.cpp a calculator.cpp"
    )
    parser.add_argument("--compile",  action="store_true", help="Skompiluje zdrojové súbory")
    parser.add_argument("--generate", action="store_true", help="Vygeneruje datasety a referencie")
    parser.add_argument("--dataset",  default="all",       help="Dataset na testovanie: 1, 2, 3, all")
    args = parser.parse_args()

    ensure_dirs()

    if args.compile:
        compile_all()

    if args.generate:
        generate_all()

    log("\n🚀 SPÚŠŤAM BENCHMARK")
    log(f"   Binárky: {BINARIES}")
    log(f"   Dataset: {args.dataset}")

    if args.dataset == "all":
        for ds_id in DATASETS:
            benchmark_dataset(ds_id)
    else:
        try:
            ds_id = int(args.dataset)
            if ds_id not in DATASETS:
                raise ValueError
            benchmark_dataset(ds_id)
        except ValueError:
            err(f"Neplatné --dataset '{args.dataset}'. Použi 1, 2, 3 alebo all.")
            sys.exit(1)

    log("\n✅ Benchmark dokončený.\n")


if __name__ == "__main__":
    main()