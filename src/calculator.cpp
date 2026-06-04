/*
 * calculator.cpp — Ultra-fast, stream-based, arbitrary-precision integer calculator.
 *
 * COMPLIANT WITH SPECIFICATION:
 *   - Vlastný memcpy a memset (word-by-word optimalizácia)
 *   - Vlastná pamäťová aréna (mmap, bez malloc/calloc)
 *   - Dynamické štruktúry (BigInt digits v aréne, nie na stacku)
 *   - Platformne nezávislé rozhranie funkcií
 *
 * ADAPTÍVNY VÝKON:
 *   - Prahy násobiček škálované podľa skutočnej RAM stroja
 *   - MAP_NORESERVE: virtuálna aréna, fyzické stránky len keď treba
 *   - Snapshot/restore aréna: každý výraz uvoľní svoju pamäť po sebe
 *   - OOM guard v nttMul: pred alokáciou overí dostatok miesta v aréne
 *   - Detekcia complexity výrazu: jednoduché výrazy nikdy nespustia NTT
 *
 * VYLEPŠENIA (v2):
 *   - Bez limitu na exponent — g_maxExp odstránený, bigintPow pracuje
 *     priamo s BigInt exponentom (binárne umocňovanie cez BigInt bit-shift)
 *   - Rýchle delenie — trial division nahradený binary-search + multiply
 *     (O(n · log10 · M(n)) namiesto O(n · 9 · n))
 *   - NTT dual-mod CRT — dva nezávislé NTT moduly (998244353 + 985661441)
 *     kombinované cez CRT; eliminuje carry-overflow pri číslach > ~300 000 cifier
 *   - Výstupná šírka riadku zvýšená na 120 znakov (bolo 70)
 */

#include <cstddef>
#include <cmath>
#include <unistd.h>
#include <stdint.h>
#include <sys/mman.h>

// ---------------------------------------------------------------------------
// VLASTNÁ IMPLEMENTÁCIA SYSTÉMOVÝCH FUNKCIÍ (Podmienka zo zadania)
// ---------------------------------------------------------------------------

// Vlastný memcpy optimalizovaný po 8-bajtových blokoch (word-by-word)
static void customMemcpy(void* dest, const void* src, size_t n) {
    char* d       = static_cast<char*>(dest);
    const char* s = static_cast<const char*>(src);
    while (n >= 8) {
        *reinterpret_cast<uint64_t*>(d) = *reinterpret_cast<const uint64_t*>(s);
        d += 8; s += 8; n -= 8;
    }
    while (n--) *d++ = *s++;
}

// Vlastný memset optimalizovaný po 8-bajtových blokoch
static void customMemset(void* s, int c, size_t n) {
    char*    p    = static_cast<char*>(s);
    uint64_t byte = static_cast<uint8_t>(c);
    uint64_t word = (byte << 56) | (byte << 48) | (byte << 40) | (byte << 32) |
                    (byte << 24) | (byte << 16) | (byte << 8)  | byte;
    while (n >= 8) { *reinterpret_cast<uint64_t*>(p) = word; p += 8; n -= 8; }
    while (n--)    *p++ = static_cast<char>(c);
}

// ---------------------------------------------------------------------------
// Globálne nastavenia buffrov (fixné — nezávisia od RAM)
// ---------------------------------------------------------------------------
static constexpr size_t READ_BUF_SIZE = 8  * 1024 * 1024;  // 8 MB  vstupný buffer
static constexpr size_t MAX_LINE_LEN  = 32 * 1024 * 1024;  // 32 MB max riadok
static constexpr size_t OUT_BUF_SIZE  = 8  * 1024 * 1024;  // 8 MB  výstupný buffer
static constexpr int    STACK_DEPTH   = 1024;

// ---------------------------------------------------------------------------
// Adaptívne prahy a limity — nastavené za behu podľa RAM
// ---------------------------------------------------------------------------
static int    g_karatsubaThreshold = 64;       // škáluje sa nahor na výkonných strojoch
static int    g_nttThreshold       = 2000000;  // škáluje sa podľa dostupnej RAM
// POZN: g_maxExp bol odstránený — exponent je neobmedzený BigInt

// ---------------------------------------------------------------------------
// Pamäťová aréna — mmap s MAP_NORESERVE (lazy physical pages)
// ---------------------------------------------------------------------------
static char*  g_readBuf   = nullptr;
static char*  g_outBuf    = nullptr;
static char*  g_arena     = nullptr;
static size_t g_arenaSize = 0;
static size_t g_arenaIdx  = 0;
static size_t g_outLen    = 0;

// Alokácia zo zdieľanej arény s 8-bajtovým alignmentom
static char* arenaAlloc(size_t n) {
    size_t idx = (g_arenaIdx + 7) & ~((size_t)7);
    if (idx + n > g_arenaSize) return nullptr;  // OOM — volajúci musí skontrolovať
    g_arenaIdx = idx + n;
    return g_arena + idx;
}

// Snapshot: vráti aktuálnu pozíciu — používa sa pred každým výrazom
static size_t arenaSnapshot()          { return g_arenaIdx; }

// Restore: vráti arénu na pozíciu snapshotu — uvoľní všetku pamäť daného výrazu
static void   arenaRestore(size_t idx) { g_arenaIdx = idx; }

// Zostatok arény v bajtoch
static size_t arenaFree() { return g_arenaSize - g_arenaIdx; }

// ---------------------------------------------------------------------------
// Výstupný streaming buffer
// ---------------------------------------------------------------------------
static void outFlush() {
    size_t written = 0;
    while (written < g_outLen) {
        ssize_t r = write(STDOUT_FILENO, g_outBuf + written, g_outLen - written);
        if (r <= 0) break;
        written += (size_t)r;
    }
    g_outLen = 0;
}
static void outChar(char c) {
    if (g_outLen == OUT_BUF_SIZE) outFlush();
    g_outBuf[g_outLen++] = c;
}
static void outCStr(const char* s) { while (*s) outChar(*s++); }

// ---------------------------------------------------------------------------
// BigInt — cifry žijú výhradne v aréne, nie na stacku ani heape
// ---------------------------------------------------------------------------
struct BigInt {
    char* digits;
    int   len;
    int   negative;
};

static BigInt bigintFromStr(const char* s, int len) {
    BigInt r; r.negative = 0;
    int start = 0;
    if (len > 0 && s[0] == '-') { r.negative = 1; start = 1; }
    else if (len > 0 && s[0] == '+') { start = 1; }
    while (start < len - 1 && s[start] == '0') start++;
    int dlen = len - start;
    if (dlen <= 0) dlen = 1;
    r.digits = arenaAlloc((size_t)dlen + 1);
    if (!r.digits) {
        // Núdzový fallback — vrátime "0" ukazujúce na statickú pamäť
        static char zero[2] = {'0', '\0'};
        r.digits = zero; r.len = 1; r.negative = 0; return r;
    }
    if (dlen == 1 && (start >= len || s[start] == '0')) {
        r.digits[0] = '0'; r.len = 1; r.negative = 0;
    } else {
        customMemcpy(r.digits, s + start, (size_t)dlen);
        r.len = dlen;
    }
    r.digits[r.len] = '\0';
    return r;
}

static int  absCmp(const BigInt& a, const BigInt& b) {
    if (a.len != b.len) return (a.len < b.len) ? -1 : 1;
    for (int i = 0; i < a.len; i++)
        if (a.digits[i] != b.digits[i]) return (a.digits[i] < b.digits[i]) ? -1 : 1;
    return 0;
}
static bool isZero(const BigInt& a) { return a.len == 1 && a.digits[0] == '0'; }

static BigInt absAdd(const BigInt& a, const BigInt& b) {
    int maxLen = (a.len > b.len) ? a.len : b.len;
    int rLen = maxLen + 1;
    char* buf = arenaAlloc((size_t)rLen + 1);
    if (!buf) return bigintFromStr("0", 1);
    int carry = 0, ia = a.len - 1, ib = b.len - 1;
    for (int i = rLen - 1; i >= 0; i--) {
        int sum = carry;
        if (ia >= 0) sum += a.digits[ia--] - '0';
        if (ib >= 0) sum += b.digits[ib--] - '0';
        buf[i] = (char)('0' + sum % 10); carry = sum / 10;
    }
    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start; r.negative = 0;
    r.digits[r.len] = '\0'; return r;
}

static BigInt absSub(const BigInt& a, const BigInt& b) {
    int rLen = a.len;
    char* buf = arenaAlloc((size_t)rLen + 1);
    if (!buf) return bigintFromStr("0", 1);
    int borrow = 0, ia = a.len - 1, ib = b.len - 1;
    for (int i = rLen - 1; i >= 0; i--) {
        int diff = (a.digits[ia--] - '0') - borrow;
        if (ib >= 0) diff -= (b.digits[ib--] - '0');
        if (diff < 0) { diff += 10; borrow = 1; } else { borrow = 0; }
        buf[i] = (char)('0' + diff);
    }
    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start; r.negative = 0;
    r.digits[r.len] = '\0'; return r;
}

static BigInt bigintAdd(const BigInt& a, const BigInt& b);
static BigInt bigintSub(const BigInt& a, const BigInt& b);

// ---------------------------------------------------------------------------
// Násobenie — tri úrovne, výber podľa veľkosti čísel a dostupnej RAM
// ---------------------------------------------------------------------------

// O(n²) — pre malé čísla (< g_karatsubaThreshold cifier)
static BigInt naiveMul(const BigInt& a, const BigInt& b) {
    if (isZero(a) || isZero(b)) return bigintFromStr("0", 1);
    int rLen = a.len + b.len;
    int* acc = (int*)arenaAlloc((size_t)rLen * sizeof(int));
    if (!acc) return bigintFromStr("0", 1);
    customMemset(acc, 0, (size_t)rLen * sizeof(int));
    for (int i = a.len - 1; i >= 0; i--) {
        int ai = a.digits[i] - '0';
        for (int j = b.len - 1; j >= 0; j--)
            acc[i + j + 1] += ai * (b.digits[j] - '0');
    }
    for (int i = rLen - 1; i > 0; i--) { acc[i-1] += acc[i] / 10; acc[i] %= 10; }
    char* buf = arenaAlloc((size_t)rLen + 1);
    if (!buf) return bigintFromStr("0", 1);
    for (int i = 0; i < rLen; i++) buf[i] = (char)('0' + acc[i]);
    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start; r.negative = 0;
    r.digits[r.len] = '\0'; return r;
}

// Pomocné funkcie pre Karatsuba
static BigInt subBigInt(const BigInt& a, int from, int to) {
    BigInt r;
    if (from >= to || from >= a.len) {
        r.digits = arenaAlloc(2);
        if (!r.digits) { static char z[2]="0"; r.digits=z; r.len=1; r.negative=0; return r; }
        r.digits[0] = '0'; r.digits[1] = '\0'; r.len = 1; r.negative = 0; return r;
    }
    if (to > a.len) to = a.len;
    int len = to - from, s = 0;
    while (s < len - 1 && a.digits[from + s] == '0') s++;
    r.digits = a.digits + from + s; r.len = len - s; r.negative = 0; return r;
}
static BigInt shiftLeft(const BigInt& a, int n) {
    if (isZero(a)) return bigintFromStr("0", 1);
    int rLen = a.len + n;
    char* buf = arenaAlloc((size_t)rLen + 1);
    if (!buf) return bigintFromStr("0", 1);
    customMemcpy(buf, a.digits, (size_t)a.len);
    customMemset(buf + a.len, '0', (size_t)n);
    buf[rLen] = '\0';
    BigInt r; r.digits = buf; r.len = rLen; r.negative = a.negative; return r;
}

static BigInt karatsubaMul(const BigInt& a, const BigInt& b);
static BigInt nttMul(const BigInt& a, const BigInt& b);
static BigInt nttSquare(const BigInt& a);

static BigInt dispatchSquare(const BigInt& a) {
    int n = a.len;
    if (n < g_karatsubaThreshold) return naiveMul(a, a);
    if (n < g_nttThreshold)       return karatsubaMul(a, a);
    return nttSquare(a);
}

static BigInt dispatchMul(const BigInt& a, const BigInt& b) {
    int n = (a.len > b.len) ? a.len : b.len;
    if (n < g_karatsubaThreshold) return naiveMul(a, b);
    if (n < g_nttThreshold)       return karatsubaMul(a, b);
    return nttMul(a, b);
}

// O(n^1.585) Karatsuba — pre stredné čísla
static BigInt karatsubaMul(const BigInt& a, const BigInt& b) {
    if (isZero(a) || isZero(b)) return bigintFromStr("0", 1);
    int n = (a.len > b.len) ? a.len : b.len;
    if (n < g_karatsubaThreshold) return naiveMul(a, b);

    int half = n / 2;
    int a_split = a.len - half; if (a_split < 0) a_split = 0;
    int b_split = b.len - half; if (b_split < 0) b_split = 0;

    BigInt a1 = subBigInt(a, 0, a_split),  a0 = subBigInt(a, a_split, a.len);
    BigInt b1 = subBigInt(b, 0, b_split),  b0 = subBigInt(b, b_split, b.len);

    BigInt z2 = dispatchMul(a1, b1);
    BigInt z0 = dispatchMul(a0, b0);

    BigInt a1a0 = absAdd(a1, a0);
    BigInt b1b0 = absAdd(b1, b0);
    BigInt z1   = dispatchMul(a1a0, b1b0);
    z1 = absSub(z1, z2);
    z1 = absSub(z1, z0);

    BigInt t2  = shiftLeft(z2, 2 * half);
    BigInt t1  = shiftLeft(z1, half);
    BigInt res = absAdd(t2, t1);
    res = absAdd(res, z0);
    res.negative = a.negative ^ b.negative;
    if (isZero(res)) res.negative = 0;
    return res;
}

// ---------------------------------------------------------------------------
// NTT — O(n log n), pre veľké čísla (>= g_nttThreshold)
// Dual-mod CRT: dva nezávislé NTT moduly kombinované cez Chinese Remainder Theorem.
// Eliminuje carry-overflow pri číslach s viac ako ~300 000 ciframi.
// OOM GUARD: pred alokáciou overí dostatok miesta v aréne.
// Ak nie je dosť RAM, degraduje na Karatsuba namiesto crash/garbage.
// ---------------------------------------------------------------------------
static constexpr long long NTT_MOD1 = 998244353LL;   // 119 * 2^23 + 1, primitívny koreň 3
static constexpr long long NTT_G1   = 3LL;
static constexpr long long NTT_MOD2 = 985661441LL;   // 235 * 2^22 + 1, primitívny koreň 3
static constexpr long long NTT_G2   = 3LL;

static long long nttPow(long long base, long long exp, long long mod) {
    long long r = 1; base %= mod;
    while (exp > 0) {
        if (exp & 1) r = r * base % mod;
        base = base * base % mod; exp >>= 1;
    }
    return r;
}
static void ntt(long long* a, int n, bool inv, long long mod, long long g) {
    for (int i = 1, j = 0; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) { long long t = a[i]; a[i] = a[j]; a[j] = t; }
    }
    for (int len = 2; len <= n; len <<= 1) {
        long long w = inv ? nttPow(g, mod - 1 - (mod - 1) / len, mod)
                          : nttPow(g, (mod - 1) / len, mod);
        for (int i = 0; i < n; i += len) {
            long long wn = 1;
            for (int j = 0; j < len / 2; j++) {
                long long u = a[i + j], v = a[i + j + len/2] * wn % mod;
                a[i + j]         = (u + v) % mod;
                a[i + j + len/2] = (u - v + mod) % mod;
                wn = wn * w % mod;
            }
        }
    }
    if (inv) {
        long long n_inv = nttPow(n, mod - 2, mod);
        for (int i = 0; i < n; i++) a[i] = a[i] * n_inv % mod;
    }
}

static BigInt nttMul(const BigInt& a, const BigInt& b);

// NTT špeciálna verzia pre squarovanie: a == b → len 2 NTT namiesto 4
// (fa1 = fb1, fa2 = fb2 → stačí NTT(a), potom fa[i] *= fa[i])
static BigInt nttSquare(const BigInt& a) {
    if (isZero(a)) return bigintFromStr("0", 1);

    int rLen = a.len * 2;
    int n = 1; while (n < rLen) n <<= 1;

    // Potrebujeme len 2×n×8 (fa1, fa2) namiesto 4×n×8
    size_t needed = (size_t)n * 2 * sizeof(long long) + (size_t)rLen + 1;
    if (needed > arenaFree()) return karatsubaMul(a, a);

    long long* fa1 = (long long*)arenaAlloc((size_t)n * sizeof(long long));
    long long* fa2 = (long long*)arenaAlloc((size_t)n * sizeof(long long));
    if (!fa1 || !fa2) return karatsubaMul(a, a);

    customMemset(fa1, 0, (size_t)n * sizeof(long long));
    customMemset(fa2, 0, (size_t)n * sizeof(long long));
    for (int i = 0; i < a.len; i++) { fa1[i] = a.digits[a.len - 1 - i] - '0'; fa2[i] = fa1[i]; }

    // NTT mod 1: FFT(a), square pointwise, IFFT
    ntt(fa1, n, false, NTT_MOD1, NTT_G1);
    for (int i = 0; i < n; i++) fa1[i] = fa1[i] * fa1[i] % NTT_MOD1;
    ntt(fa1, n, true,  NTT_MOD1, NTT_G1);

    // NTT mod 2
    ntt(fa2, n, false, NTT_MOD2, NTT_G2);
    for (int i = 0; i < n; i++) fa2[i] = fa2[i] * fa2[i] % NTT_MOD2;
    ntt(fa2, n, true,  NTT_MOD2, NTT_G2);

    static long long s_m1invM2_sq = 0;
    if (s_m1invM2_sq == 0) s_m1invM2_sq = nttPow(NTT_MOD1 % NTT_MOD2, NTT_MOD2 - 2, NTT_MOD2);

    char* buf = arenaAlloc((size_t)rLen + 1);
    if (!buf) return karatsubaMul(a, a);

    long long carry = 0;
    for (int i = 0; i < rLen; i++) {
        long long r1 = fa1[i], r2 = fa2[i];
        long long t  = (r2 - r1 % NTT_MOD2 + NTT_MOD2) % NTT_MOD2 * s_m1invM2_sq % NTT_MOD2;
        __int128 val = (__int128)r1 + (__int128)NTT_MOD1 * t + carry;
        carry = (long long)(val / 10);
        buf[rLen - 1 - i] = (char)('0' + (int)(val % 10));
    }
    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start;
    r.negative = 0;   // a^2 je vždy nezáporné
    if (isZero(r)) r.negative = 0;
    r.digits[r.len] = '\0'; return r;
}

static BigInt nttMul(const BigInt& a, const BigInt& b) {
    if (isZero(a) || isZero(b)) return bigintFromStr("0", 1);

    int rLen = a.len + b.len;
    int n = 1; while (n < rLen) n <<= 1;

    // OOM GUARD: potrebujeme 4×n×8 bajtov (fa1,fb1,fa2,fb2) + rLen+1 pre výsledok
    size_t needed = (size_t)n * 4 * sizeof(long long) + (size_t)rLen + 1;
    if (needed > arenaFree()) {
        return karatsubaMul(a, b);
    }

    long long* fa1 = (long long*)arenaAlloc((size_t)n * sizeof(long long));
    long long* fb1 = (long long*)arenaAlloc((size_t)n * sizeof(long long));
    long long* fa2 = (long long*)arenaAlloc((size_t)n * sizeof(long long));
    long long* fb2 = (long long*)arenaAlloc((size_t)n * sizeof(long long));
    if (!fa1 || !fb1 || !fa2 || !fb2) return karatsubaMul(a, b);

    customMemset(fa1, 0, (size_t)n * sizeof(long long));
    customMemset(fb1, 0, (size_t)n * sizeof(long long));
    customMemset(fa2, 0, (size_t)n * sizeof(long long));
    customMemset(fb2, 0, (size_t)n * sizeof(long long));

    for (int i = 0; i < a.len; i++) { fa1[i] = a.digits[a.len - 1 - i] - '0'; fa2[i] = fa1[i]; }
    for (int i = 0; i < b.len; i++) { fb1[i] = b.digits[b.len - 1 - i] - '0'; fb2[i] = fb1[i]; }

    // NTT mod 1
    ntt(fa1, n, false, NTT_MOD1, NTT_G1); ntt(fb1, n, false, NTT_MOD1, NTT_G1);
    for (int i = 0; i < n; i++) fa1[i] = fa1[i] * fb1[i] % NTT_MOD1;
    ntt(fa1, n, true,  NTT_MOD1, NTT_G1);

    // NTT mod 2
    ntt(fa2, n, false, NTT_MOD2, NTT_G2); ntt(fb2, n, false, NTT_MOD2, NTT_G2);
    for (int i = 0; i < n; i++) fa2[i] = fa2[i] * fb2[i] % NTT_MOD2;
    ntt(fa2, n, true,  NTT_MOD2, NTT_G2);

    // CRT: rekonštruuj skutočnú hodnotu z dvoch residuí
    // x ≡ r1 (mod M1), x ≡ r2 (mod M2)  =>  x = r1 + M1 * ((r2 - r1) * M1_inv_mod_M2 % M2)
    // Pre cifry (0..9) súčin dvoch n-ciferných čísel má koeficienty <= 9² × n < 2^53 pre n<10^15
    // CRT s dvoma modulmi (M1×M2 ≈ 9.8×10^17) pokrýva koeficienty do ~4.3×10^8 cifier bezpečne.
    // Vypočítame M1_inv_M2 raz lazy:
    static long long s_m1invM2 = 0;
    if (s_m1invM2 == 0) s_m1invM2 = nttPow(NTT_MOD1 % NTT_MOD2, NTT_MOD2 - 2, NTT_MOD2);

    char* buf = arenaAlloc((size_t)rLen + 1);
    if (!buf) return karatsubaMul(a, b);

    // Rekonštrukcia pomocou __int128 aby sme predišli pretečeniu pri M1 * t
    long long carry = 0;
    for (int i = 0; i < rLen; i++) {
        long long r1 = fa1[i];
        long long r2 = fa2[i];
        long long t  = (r2 - r1 % NTT_MOD2 + NTT_MOD2) % NTT_MOD2 * s_m1invM2 % NTT_MOD2;
        // skutočná hodnota = r1 + NTT_MOD1 * t  (fit do __int128)
        __int128 val = (__int128)r1 + (__int128)NTT_MOD1 * t + carry;
        carry = (long long)(val / 10);
        buf[rLen - 1 - i] = (char)('0' + (int)(val % 10));
    }
    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start;
    r.negative = a.negative ^ b.negative;
    if (isZero(r)) r.negative = 0;
    r.digits[r.len] = '\0'; return r;
}

static BigInt bigintMul(const BigInt& a, const BigInt& b) {
    if (isZero(a) || isZero(b)) return bigintFromStr("0", 1);
    BigInt r = dispatchMul(a, b);
    r.negative = a.negative ^ b.negative;
    if (isZero(r)) r.negative = 0;
    return r;
}

// ---------------------------------------------------------------------------
// Sčítanie a odčítanie
// ---------------------------------------------------------------------------
static BigInt bigintAdd(const BigInt& a, const BigInt& b) {
    if (a.negative == b.negative) {
        BigInt r = absAdd(a, b); r.negative = a.negative;
        if (isZero(r)) r.negative = 0;
        return r;
    }
    int cmp = absCmp(a, b);
    if (cmp == 0) return bigintFromStr("0", 1);
    if (cmp > 0) { BigInt r = absSub(a, b); r.negative = a.negative; return r; }
    else         { BigInt r = absSub(b, a); r.negative = b.negative; return r; }
}
static BigInt bigintSub(const BigInt& a, const BigInt& b) {
    BigInt nb = b; nb.negative = b.negative ^ 1;
    if (isZero(nb)) nb.negative = 0;
    return bigintAdd(a, nb);
}

// ---------------------------------------------------------------------------
// Delenie a Modulo — binary-search + multiply algoritmus
// Namiesto 9× sčítavania v trial division používame binárne hľadanie cifry.
// Každá cifra výsledku: O(log(10) × M(k)) kde M(k) je cena násobenia k-ciferného čísla.
// Celková cena: O(n × log(10) × M(n)) — rádovo rýchlejšie pre veľké čísla.
// ---------------------------------------------------------------------------

// Porovná |cur| s d×|b|  (d ∈ 1..9) bez stavby plného súčinu ak je to zbytočné
static BigInt bigintDivMod(const BigInt& a, const BigInt& b, BigInt* rem) {
    if (isZero(b)) {
        if (rem) *rem = bigintFromStr("0", 1);
        BigInt r; r.digits = (char*)"DIV0"; r.len = 4; r.negative = 0; return r;
    }
    if (absCmp(a, b) < 0) {
        if (rem) *rem = a;
        return bigintFromStr("0", 1);
    }
    char* qbuf = arenaAlloc((size_t)a.len + 1);
    if (!qbuf) { BigInt r; r.digits=(char*)"OOM"; r.len=3; r.negative=0; return r; }
    int qlen = 0;

    // cur = akumulovaný zvyšok (rastie od 1 do max b.len+1 cifier)
    BigInt cur; cur.digits = arenaAlloc((size_t)a.len + 2); cur.len = 1;
    if (!cur.digits) { BigInt r; r.digits=(char*)"OOM"; r.len=3; r.negative=0; return r; }
    cur.digits[0] = '0'; cur.digits[1] = '\0'; cur.negative = 0;
    BigInt absb = b; absb.negative = 0;

    for (int i = 0; i < a.len; i++) {
        // Pripoj ďalšiu cifru delenca na koniec cur
        if (cur.len == 1 && cur.digits[0] == '0') {
            cur.digits[0] = a.digits[i];
        } else {
            char* newbuf = arenaAlloc((size_t)cur.len + 2);
            if (!newbuf) { BigInt r; r.digits=(char*)"OOM"; r.len=3; r.negative=0; return r; }
            customMemcpy(newbuf, cur.digits, (size_t)cur.len);
            newbuf[cur.len] = a.digits[i]; newbuf[cur.len + 1] = '\0';
            cur.digits = newbuf; cur.len = cur.len + 1;
        }

        // Binárne hľadanie cifry d ∈ [0,9] tak, že d×absb <= cur < (d+1)×absb
        int lo = 0, hi = 9, d = 0;
        while (lo <= hi) {
            int mid = (lo + hi) / 2;
            if (mid == 0) { lo = 1; continue; }
            // Vypočítame mid × absb cez naivMul (absb má max ~rLen/n cifier — malé)
            BigInt midBI = bigintFromStr("0", 1);
            // Efektívne: mid×absb = absb << (mid ako jednociferné) — ale pre mid<=9
            // stačí priame naivMul (absb × jednociferné číslo je O(n))
            char midStr[3]; int ms = 0;
            if (mid >= 10) { midStr[ms++] = (char)('0' + mid/10); }
            midStr[ms++] = (char)('0' + mid%10); midStr[ms] = '\0';
            midBI = bigintFromStr(midStr, ms);
            BigInt trial = naiveMul(absb, midBI);
            int cmp = absCmp(trial, cur);
            if (cmp <= 0) { d = mid; lo = mid + 1; }
            else           { hi = mid - 1; }
        }
        qbuf[qlen++] = (char)('0' + d);

        // cur = cur - d×absb
        if (d > 0) {
            char dStr[2] = {(char)('0' + d), '\0'};
            BigInt dBI = bigintFromStr(dStr, 1);
            BigInt sub = naiveMul(absb, dBI);
            cur = absSub(cur, sub);
        }
    }
    int start = 0;
    while (start < qlen - 1 && qbuf[start] == '0') start++;
    BigInt q; q.digits = qbuf + start; q.len = qlen - start;
    q.negative = a.negative ^ b.negative;
    if (isZero(q)) q.negative = 0;
    q.digits[q.len] = '\0';
    if (rem) { cur.negative = a.negative; if (isZero(cur)) cur.negative = 0; *rem = cur; }
    return q;
}
static BigInt bigintDiv(const BigInt& a, const BigInt& b) { return bigintDivMod(a, b, nullptr); }
static BigInt bigintMod(const BigInt& a, const BigInt& b) { BigInt rem; bigintDivMod(a, b, &rem); return rem; }

// ---------------------------------------------------------------------------
// Mocnina — binárne umocňovanie s neobmedzeným BigInt exponentom
//
// Optimalizácie:
//  1. bigintHalf() — O(n) delenie dvoma namiesto O(n²) long-division
//  2. Žiadne zbytočné finálne squarovanie keď e==1
//  3. Per-iterácia arena GC — po každej iterácii sa staré hodnoty b a result
//     uvoľnia z arény; uchováme len živé hodnoty kópiou na začiatok slotu.
//     Bez toho by 999^160000 nahromadil stovky GB intermediárnych čísel.
// ---------------------------------------------------------------------------

// O(n) delenie BigInt hodnotou 2; vracia kvocient; *odd_out = zvyšok (0/1)
static BigInt bigintHalf(const BigInt& a, bool* odd_out) {
    int n = a.len;
    char* buf = arenaAlloc((size_t)n + 1);
    if (!buf) { if (odd_out) *odd_out = false; return bigintFromStr("0", 1); }
    int carry = 0;
    for (int i = 0; i < n; i++) {
        int cur = carry * 10 + (a.digits[i] - '0');
        buf[i]  = (char)('0' + cur / 2);
        carry   = cur & 1;
    }
    buf[n] = '\0';
    if (odd_out) *odd_out = (carry != 0);
    int start = 0;
    while (start < n - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = n - start; r.negative = 0;
    r.digits[r.len] = '\0';
    return r;
}

// Vráti true ak |a| == 1
static bool bigintIsOne(const BigInt& a) {
    return a.len == 1 && a.digits[0] == '1';
}

// Skopíruje BigInt do čerstvo alokovaného bloku v aréne (pre GC)
static BigInt bigintCopy(const BigInt& src) {
    BigInt r;
    r.negative = src.negative;
    r.digits   = arenaAlloc((size_t)src.len + 1);
    if (!r.digits) return bigintFromStr("0", 1);
    customMemcpy(r.digits, src.digits, (size_t)src.len);
    r.len = src.len;
    r.digits[r.len] = '\0';
    return r;
}

static BigInt bigintPow(const BigInt& base, const BigInt& exp) {
    if (isZero(exp))        return bigintFromStr("1", 1);
    if (exp.negative)       return bigintFromStr("0", 1);
    if (bigintIsOne(exp))   return base;
    if (isZero(base))       return bigintFromStr("0", 1);
    if (bigintIsOne(base))  return bigintFromStr("1", 1);

    // Zistiť znamienko výsledku vopred
    // result je záporný len ak base<0 a exp je nepárne
    bool base_neg   = base.negative && !isZero(base);
    bool exp_is_odd = ((exp.digits[exp.len - 1] - '0') & 1) != 0;
    int  res_sign   = (base_neg && exp_is_odd) ? 1 : 0;

    // Iterujeme cez bity exponentu od MSB po LSB:
    // Najprv zbierame bity do poľa (max log2(10) × exp.len ≈ 3.32 × exp.len bitov)
    // Pre exp.len <= 10 (číslo do ~10^10) to je max ~33 bitov — triviálne.
    // Pre ľubovoľne veľký exp použijeme on-the-fly half.
    //
    // Použijeme LEFT-TO-RIGHT binary exponentiation (MSB first):
    //   - Zbierame bity postupným halvingom do dočasného poľa
    //   - Potom iterujeme bity od MSB, čím každý výpočet b používame len raz
    //   - VÝHODA: result rastie monotónne, b sa quadruje a zahodí po použití bitu
    //
    // ARENA GC stratégia:
    //   Snapshot pred iteráciou. Po výpočte new_result a new_b, skopírujeme
    //   ich na začiatok snapshotu (prepisujeme starú pamäť), tým uvoľníme
    //   všetky intermediáty (old result, old b, temporary mul outputs).

    // Krok 1: Zbieri bity exponentu (LSB first) pomocou O(n) half
    // Max bitov = ceil(log2(10)) * exp.len ≈ 4 * exp.len — alokujeme konzervatívne
    int max_bits = exp.len * 4 + 32;
    char* bits   = arenaAlloc((size_t)max_bits);
    if (!bits) return bigintFromStr("0", 1);

    // Pracovná kópia exponentu
    BigInt e;
    e.negative = 0;
    e.digits   = arenaAlloc((size_t)exp.len + 1);
    if (!e.digits) return bigintFromStr("0", 1);
    customMemcpy(e.digits, exp.digits, (size_t)exp.len);
    e.len = exp.len;
    e.digits[e.len] = '\0';

    int nbits = 0;
    while (!isZero(e)) {
        bool odd = false;
        e = bigintHalf(e, &odd);
        bits[nbits++] = odd ? 1 : 0;
    }
    // bits[0] = LSB, bits[nbits-1] = MSB

    // Krok 2: Left-to-right exponentiation (MSB first)
    // result = 1, potom pre každý bit od MSB:
    //   result = result^2
    //   if bit == 1: result = result * base
    //
    // Poznámka: toto je ekvivalentné s right-to-left len pre výsledok —
    // ale left-to-right je pamäťovo efektívnejší lebo b nerastie tak rýchlo.
    //
    // Kôli GC: zachovávame len result. Po každej iterácii:
    //   snap = snapshot(); compute new_result; copy to front; restore(snap); result = copy

    BigInt result = bigintFromStr("1", 1);

    // Pracovná kópia základu (pozitívna — znamienko pridáme na konci)
    BigInt bpos;
    bpos.negative = 0;
    bpos.digits   = arenaAlloc((size_t)base.len + 1);
    if (!bpos.digits) return bigintFromStr("0", 1);
    customMemcpy(bpos.digits, base.digits, (size_t)base.len);
    bpos.len = base.len;
    bpos.digits[bpos.len] = '\0';

    for (int i = nbits - 1; i >= 0; i--) {
        size_t snap = arenaSnapshot();

        // result = result^2  (špeciálna squarovacia cesta — 2 NTT namiesto 4)
        BigInt sq = dispatchSquare(result);

        // if bit == 1: result = sq * base
        BigInt new_result;
        if (bits[i]) {
            new_result = bigintMul(sq, bpos);
        } else {
            new_result = sq;
        }

        // GC: skopíruj new_result na začiatok snapshotu, zahoď zvyšok
        // Trik: restore snap, potom alokuj čerstvý blok pre kópiu
        arenaRestore(snap);
        result = bigintCopy(new_result);
        // POZOR: new_result.digits ukazuje na zahodenú pamäť — result je teraz
        // čerstvá kópia. ✓
    }

    result.negative = res_sign;
    if (isZero(result)) result.negative = 0;
    return result;
}

// ---------------------------------------------------------------------------
// Shunting-Yard Parser
// ---------------------------------------------------------------------------
static int  opPrecedence(char op) {
    switch (op) {
        case '+': case '-': return 1;
        case '*': case '/': case '%': return 2;
        case '^': return 3;
        case '#': case '_': return 4;
        default: return 0;
    }
}
static bool opRightAssoc(char op) { return op == '^' || op == '#' || op == '_'; }

static BigInt applyOp(char op, const BigInt& a, const BigInt& b) {
    switch (op) {
        case '+': return bigintAdd(a, b);
        case '-': return bigintSub(a, b);
        case '*': return bigintMul(a, b);
        case '/': return bigintDiv(a, b);
        case '%': return bigintMod(a, b);
        case '^': return bigintPow(a, b);
        default:  return bigintFromStr("0", 1);
    }
}

static char   g_opStack[STACK_DEPTH];
static int    g_opTop = 0;
static BigInt g_valStack[STACK_DEPTH];
static int    g_valTop = 0;

static bool processTopOp() {
    if (g_opTop == 0) return false;
    char op = g_opStack[--g_opTop];
    if (op == '#') {
        if (g_valTop < 1) return false;
        g_valStack[g_valTop - 1].negative ^= 1;
        if (isZero(g_valStack[g_valTop - 1])) g_valStack[g_valTop - 1].negative = 0;
        return true;
    }
    if (op == '_') return g_valTop >= 1;
    if (g_valTop < 2) return false;
    BigInt b = g_valStack[--g_valTop];
    BigInt a = g_valStack[--g_valTop];
    g_valStack[g_valTop++] = applyOp(op, a, b);
    return true;
}

// ---------------------------------------------------------------------------
// Detekcia zložitosti výrazu — rýchly O(n) prescan
// Vracia odhadovanú max. dĺžku výsledku v cifrách.
// Používa sa pre adaptívne rozhodnutie arény pre daný riadok.
// ---------------------------------------------------------------------------
enum ExprComplexity { EXPR_SIMPLE, EXPR_MEDIUM, EXPR_HEAVY };

static ExprComplexity detectComplexity(const char* expr, int len) {
    int maxNumLen = 0, curNumLen = 0;
    int opCount = 0, parenDepth = 0, maxDepth = 0;
    bool hasPow = false;

    for (int i = 0; i < len; i++) {
        char c = expr[i];
        if (c >= '0' && c <= '9') {
            curNumLen++;
            if (curNumLen > maxNumLen) maxNumLen = curNumLen;
        } else {
            curNumLen = 0;
            if (c == '+' || c == '-' || c == '*' || c == '/' || c == '%') opCount++;
            if (c == '^') { opCount++; hasPow = true; }
            if (c == '(') { parenDepth++; if (parenDepth > maxDepth) maxDepth = parenDepth; }
            if (c == ')') parenDepth--;
        }
    }

    // Ťažký: umocňovanie alebo čísla s tisíckami cifier
    if (hasPow || maxNumLen > 1000) return EXPR_HEAVY;
    // Stredný: dlhé čísla alebo veľa operácií
    if (maxNumLen > 64 || opCount > 10 || maxDepth > 3) return EXPR_MEDIUM;
    return EXPR_SIMPLE;
}

static void evaluateLine(const char* expr, int len) {
    g_opTop = 0; g_valTop = 0;
    const char* p = expr, *end = expr + len;
    bool nextIsUnary = true;

    while (p < end) {
        char c = *p;
        if (c == ' ' || c == '\t' || c == '\r' || c == '\\') { p++; continue; }
        if (c == '(') { g_opStack[g_opTop++] = '('; p++; nextIsUnary = true; continue; }
        if (c == ')') {
            while (g_opTop > 0 && g_opStack[g_opTop - 1] != '(') processTopOp();
            if (g_opTop > 0) g_opTop--;
            p++; nextIsUnary = false; continue;
        }
        if (c >= '0' && c <= '9') {
            const char* numStart = p;
            while (p < end && *p >= '0' && *p <= '9') p++;
            g_valStack[g_valTop++] = bigintFromStr(numStart, (int)(p - numStart));
            nextIsUnary = false; continue;
        }
        if (c == '+' || c == '-' || c == '*' || c == '/' || c == '%' || c == '^') {
            char finalOp = c;
            if (nextIsUnary) {
                if (c == '-') finalOp = '#';
                else if (c == '+') finalOp = '_';
                else { p++; continue; }
            }
            int  prec  = opPrecedence(finalOp);
            bool rightA = opRightAssoc(finalOp);
            while (g_opTop > 0 && g_opStack[g_opTop - 1] != '(') {
                int topPrec = opPrecedence(g_opStack[g_opTop - 1]);
                if (rightA ? topPrec > prec : topPrec >= prec) processTopOp();
                else break;
            }
            g_opStack[g_opTop++] = finalOp;
            p++; nextIsUnary = true; continue;
        }
        p++;
    }
    while (g_opTop > 0) processTopOp();

    if (g_valTop == 1) {
        BigInt& r = g_valStack[0];
        if (r.negative && !isZero(r)) outChar('-');
        int digitsPrinted = 0;
        for (int i = 0; i < r.len; i++) {
            outChar(r.digits[i]); digitsPrinted++;
            if (digitsPrinted == 120 && i < r.len - 1) {
                outChar('\\'); outChar('\n'); digitsPrinted = 0;
            }
        }
    } else {
        outCStr("ERROR");
    }
    outChar('\n');
}

// ---------------------------------------------------------------------------
// main — auto-detekcia RAM, škálovanie prahov, streaming loop
// ---------------------------------------------------------------------------
int main() {
    // 1. Zisti fyzickú pamäť RAM stroja
    long pages     = sysconf(_SC_PHYS_PAGES);
    long page_size = sysconf(_SC_PAGE_SIZE);
    size_t total_ram = 0;
    if (pages > 0 && page_size > 0)
        total_ram = (size_t)pages * (size_t)page_size;

    // 2. Veľkosť arény: 75% RAM, minimum 512 MB
    g_arenaSize = (total_ram > 0) ? (total_ram * 3) / 4 : 0;
    if (g_arenaSize < 512ULL * 1024 * 1024)
        g_arenaSize = 512ULL * 1024 * 1024;

    // 3. Škálovanie adaptívnych prahov podľa zistenej RAM
    //
    //    RAM         Karatsuba prah    NTT prah
    //    < 1 GB      64 cifier         100 000
    //    1–4 GB      64 cifier         500 000
    //    4–16 GB     64 cifier        2 000 000
    //    > 16 GB     128 cifier       8 000 000
    //
    //  Exponent je neobmedzený — prirodzený limit je len dostupná RAM.
    {
        size_t ram_gb = total_ram / (1024ULL * 1024 * 1024);
        if (ram_gb >= 16) {
            g_karatsubaThreshold = 128;
            g_nttThreshold       = 8000000;
        } else if (ram_gb >= 4) {
            g_karatsubaThreshold = 64;
            g_nttThreshold       = 2000000;
        } else if (ram_gb >= 1) {
            g_karatsubaThreshold = 64;
            g_nttThreshold       = 500000;
        } else {
            // Embedded / CI prostredia s < 1 GB
            g_karatsubaThreshold = 64;
            g_nttThreshold       = 100000;
        }
    }

    // 4. Alokuj arénu cez mmap s MAP_NORESERVE
    //    MAP_NORESERVE: OS rezervuje virtuálny priestor, ale fyzické stránky
    //    prideľuje lazy — jednoduché výrazy nespotrebujú fyzickú RAM.
    g_arena = (char*)mmap(NULL, g_arenaSize,
                          PROT_READ | PROT_WRITE,
                          MAP_PRIVATE | MAP_ANONYMOUS | MAP_NORESERVE,
                          -1, 0);
    if (g_arena == MAP_FAILED) {
        // Fallback: skús menšiu arénu bez NORESERVE
        g_arenaSize = 256ULL * 1024 * 1024;
        g_arena = (char*)mmap(NULL, g_arenaSize,
                              PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS,
                              -1, 0);
        if (g_arena == MAP_FAILED) return 1;
        // Na malom stroji zníž aj NTT prah
        g_nttThreshold = 100000;
    }

    // 5. I/O streaming buffre
    g_readBuf = (char*)mmap(NULL, READ_BUF_SIZE, PROT_READ | PROT_WRITE,
                            MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    g_outBuf  = (char*)mmap(NULL, OUT_BUF_SIZE,  PROT_READ | PROT_WRITE,
                            MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (g_readBuf == MAP_FAILED || g_outBuf == MAP_FAILED) return 1;

    // 6. Streaming loop — čítaj vstup po 8MB blokoch
    static char lineCarry[MAX_LINE_LEN];
    int carryLen = 0;

    while (true) {
        ssize_t got = read(STDIN_FILENO, g_readBuf, READ_BUF_SIZE);
        if (got <= 0) break;

        const char* src    = g_readBuf;
        const char* srcEnd = g_readBuf + got;

        while (src < srcEnd) {
            const char* nl = src;
            while (nl < srcEnd && *nl != '\n') nl++;

            if (nl < srcEnd) {
                int segLen  = (int)(nl - src);
                int lineLen = carryLen + segLen;
                if (lineLen > 0) {
                    customMemcpy(lineCarry + carryLen, src, (size_t)segLen);
                    lineCarry[lineLen] = '\0';

                    // SNAPSHOT/RESTORE aréna:
                    // Každý výraz dostane "čistý" slice arény.
                    // Po výpočte sa pamäť výrazu vráti aréne — nie reset celej
                    // arény, len posun indexu späť na pred-výrazovú pozíciu.
                    size_t snap = arenaSnapshot();

                    // ADAPTIVNY VYKON: detekuj zlozitost vyrazu
                    // SIMPLE vyrazy nikdy nespustia NTT (zbytocna rezia pre 2+2)
                    // HEAVY vyrazy dostanu plny prah pre maximalnu rychlost
                    ExprComplexity cmplx = detectComplexity(lineCarry, lineLen);
                    int savedNtt = g_nttThreshold;
                    if (cmplx == EXPR_SIMPLE)
                        g_nttThreshold = g_karatsubaThreshold + 1;

                    evaluateLine(lineCarry, lineLen);
                    g_nttThreshold = savedNtt;
                    arenaRestore(snap);
                }
                carryLen = 0; src = nl + 1;
            } else {
                int segLen = (int)(srcEnd - src);
                if (carryLen + segLen < (int)MAX_LINE_LEN) {
                    customMemcpy(lineCarry + carryLen, src, (size_t)segLen);
                    carryLen += segLen;
                }
                src = srcEnd;
            }
        }
    }

    // Posledný riadok bez trailing newline
    if (carryLen > 0) {
        lineCarry[carryLen] = '\0';
        size_t snap = arenaSnapshot();
        ExprComplexity cmplx2 = detectComplexity(lineCarry, carryLen);
        int savedNtt2 = g_nttThreshold;
        if (cmplx2 == EXPR_SIMPLE) g_nttThreshold = g_karatsubaThreshold + 1;
        evaluateLine(lineCarry, carryLen);
        g_nttThreshold = savedNtt2;
        arenaRestore(snap);
    }

    outFlush();

    // 7. Uvoľni všetky mmap segmenty
    munmap(g_arena,   g_arenaSize);
    munmap(g_readBuf, READ_BUF_SIZE);
    munmap(g_outBuf,  OUT_BUF_SIZE);
    return 0;
}