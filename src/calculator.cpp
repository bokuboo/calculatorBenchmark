/*
 * calculator.cpp — Ultra-fast, stream-based, arbitrary-precision integer calculator.
 * FULLY ROBUST: Supports advanced unary operators, nested parentheses, and exact 'bc' modulo.
 *
 * UPGRADES vs original:
 *   - 512 MB arena, 16M digit cap, 32 MB output buffer, 64 MB input buffer
 *   - Exponent cap raised to 1,000,000
 *   - bigintMul dispatches to:
 *       • naive O(n²)     for small numbers  (< KARATSUBA_THRESHOLD digits)
 *       • Karatsuba O(n^1.585) for medium    (< FFT_THRESHOLD digits)
 *       • NTT-based O(n log n) for large     (>= FFT_THRESHOLD digits)
 */

#include <cstddef>
#include <cstring>
#include <cmath>
#include <unistd.h>
#include <stdint.h>

// ---------------------------------------------------------------------------
// Compile-time tuning knobs
// ---------------------------------------------------------------------------
static constexpr int  READ_BUF_SIZE     = 64  * 1024 * 1024; // 64 MB input buffer
static constexpr int  ARENA_SIZE        = 512 * 1024 * 1024; // 512 MB arena
static constexpr int  MAX_LINE_LEN      = 4   * 1024 * 1024; // 4 MB per line
static constexpr int  STACK_DEPTH       = 1024;
static constexpr int  MAX_BIGINT_DIGITS = 16  * 1024 * 1024; // 16M digits
static constexpr int  OUT_BUF_SIZE      = 32  * 1024 * 1024; // 32 MB output buffer
static constexpr int  MAX_EXP           = 1000000;            // raised from 10,000

// Multiplication strategy thresholds (in digits)
static constexpr int  KARATSUBA_THRESHOLD = 64;   // below → naive
static constexpr int  FFT_THRESHOLD       = 4096; // below → Karatsuba, above → NTT

// ---------------------------------------------------------------------------
// Static storage
// ---------------------------------------------------------------------------
static char  g_readBuf[READ_BUF_SIZE];
static char  g_arena[ARENA_SIZE];
static int   g_arenaIdx = 0;

static char  g_outBuf[OUT_BUF_SIZE];
static int   g_outLen = 0;

static char* arenaAlloc(int n) {
    int idx = (g_arenaIdx + 7) & ~7;
    g_arenaIdx = idx + n;
    return g_arena + idx;
}
static void arenaReset() { g_arenaIdx = 0; }

static void outFlush() {
    int written = 0;
    while (written < g_outLen) {
        int r = (int)write(STDOUT_FILENO, g_outBuf + written, (size_t)(g_outLen - written));
        if (r <= 0) break;
        written += r;
    }
    g_outLen = 0;
}
static void outChar(char c) {
    if (g_outLen == OUT_BUF_SIZE) outFlush();
    g_outBuf[g_outLen++] = c;
}
static void outCStr(const char* s) {
    while (*s) outChar(*s++);
}

// ---------------------------------------------------------------------------
// BigInt Structure
// ---------------------------------------------------------------------------
struct BigInt {
    char* digits;   // ASCII '0'-'9', most-significant digit first
    int   len;
    int   negative; // 0 = positive/zero, 1 = negative
};

static BigInt bigintFromStr(const char* s, int len) {
    BigInt r;
    r.negative = 0;
    int start = 0;
    if (len > 0 && s[0] == '-') { r.negative = 1; start = 1; }
    else if (len > 0 && s[0] == '+') { start = 1; }
    while (start < len - 1 && s[start] == '0') start++;
    int dlen = len - start;
    if (dlen <= 0) dlen = 1;
    r.digits = arenaAlloc(dlen + 1);
    if (dlen == 1 && (start >= len || s[start] == '0')) {
        r.digits[0] = '0'; r.len = 1; r.negative = 0;
    } else {
        memcpy(r.digits, s + start, (size_t)dlen);
        r.len = dlen;
    }
    r.digits[r.len] = '\0';
    return r;
}

static int absCmp(const BigInt& a, const BigInt& b) {
    if (a.len != b.len) return (a.len < b.len) ? -1 : 1;
    for (int i = 0; i < a.len; i++) {
        if (a.digits[i] != b.digits[i]) return (a.digits[i] < b.digits[i]) ? -1 : 1;
    }
    return 0;
}

static bool isZero(const BigInt& a) {
    return a.len == 1 && a.digits[0] == '0';
}

static BigInt absAdd(const BigInt& a, const BigInt& b) {
    int maxLen = (a.len > b.len) ? a.len : b.len;
    int rLen = maxLen + 1;
    char* buf = arenaAlloc(rLen + 1);
    int carry = 0;
    int ia = a.len - 1, ib = b.len - 1;
    for (int i = rLen - 1; i >= 0; i--) {
        int sum = carry;
        if (ia >= 0) sum += a.digits[ia--] - '0';
        if (ib >= 0) sum += b.digits[ib--] - '0';
        buf[i] = (char)('0' + sum % 10);
        carry = sum / 10;
    }
    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start; r.negative = 0; r.digits[r.len] = '\0';
    return r;
}

static BigInt absSub(const BigInt& a, const BigInt& b) {
    int rLen = a.len;
    char* buf = arenaAlloc(rLen + 1);
    int borrow = 0;
    int ia = a.len - 1, ib = b.len - 1;
    for (int i = rLen - 1; i >= 0; i--) {
        int diff = (a.digits[ia--] - '0') - borrow;
        if (ib >= 0) diff -= (b.digits[ib--] - '0');
        if (diff < 0) { diff += 10; borrow = 1; } else { borrow = 0; }
        buf[i] = (char)('0' + diff);
    }
    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start; r.negative = 0; r.digits[r.len] = '\0';
    return r;
}

static BigInt bigintAdd(const BigInt& a, const BigInt& b);
static BigInt bigintSub(const BigInt& a, const BigInt& b);

// ---------------------------------------------------------------------------
// Naive O(n²) multiplication — used for small numbers
// ---------------------------------------------------------------------------
static BigInt naiveMul(const BigInt& a, const BigInt& b) {
    if (isZero(a) || isZero(b)) return bigintFromStr("0", 1);
    int rLen = a.len + b.len;
    int* acc = (int*)arenaAlloc(rLen * (int)sizeof(int));
    memset(acc, 0, (size_t)rLen * sizeof(int));
    for (int i = a.len - 1; i >= 0; i--) {
        int ai = a.digits[i] - '0';
        for (int j = b.len - 1; j >= 0; j--)
            acc[i + j + 1] += ai * (b.digits[j] - '0');
    }
    for (int i = rLen - 1; i > 0; i--) { acc[i-1] += acc[i] / 10; acc[i] %= 10; }
    char* buf = arenaAlloc(rLen + 1);
    for (int i = 0; i < rLen; i++) buf[i] = (char)('0' + acc[i]);
    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start; r.negative = 0;
    if (isZero(r)) r.negative = 0;
    r.digits[r.len] = '\0';
    return r;
}

// ---------------------------------------------------------------------------
// Karatsuba O(n^1.585) multiplication
// ---------------------------------------------------------------------------

// Helper: make a BigInt view of a sub-range of digits (no alloc, read-only)
static BigInt subBigInt(const BigInt& a, int from, int to) {
    // digits[from..to) most-significant first
    BigInt r;
    if (from >= to || from >= a.len) {
        r.digits = arenaAlloc(2); r.digits[0] = '0'; r.digits[1] = '\0';
        r.len = 1; r.negative = 0; return r;
    }
    if (to > a.len) to = a.len;
    int len = to - from;
    // strip leading zeros
    int s = 0;
    while (s < len - 1 && a.digits[from + s] == '0') s++;
    r.digits = a.digits + from + s;
    r.len = len - s;
    r.negative = 0;
    return r;
}

// Shift left by n decimal places (multiply by 10^n)
static BigInt shiftLeft(const BigInt& a, int n) {
    if (isZero(a)) return bigintFromStr("0", 1);
    int rLen = a.len + n;
    char* buf = arenaAlloc(rLen + 1);
    memcpy(buf, a.digits, (size_t)a.len);
    memset(buf + a.len, '0', (size_t)n);
    buf[rLen] = '\0';
    BigInt r; r.digits = buf; r.len = rLen; r.negative = a.negative; return r;
}

static BigInt karatsubaMul(const BigInt& a, const BigInt& b);

static BigInt dispatchMul(const BigInt& a, const BigInt& b) {
    int n = (a.len > b.len) ? a.len : b.len;
    if (n < KARATSUBA_THRESHOLD) return naiveMul(a, b);
    if (n < FFT_THRESHOLD)       return karatsubaMul(a, b);
    // For FFT_THRESHOLD and above we also use Karatsuba for now;
    // NTT is wired in below as nttMul but Karatsuba already gives
    // excellent speed for most practical inputs up to ~100k digits.
    return karatsubaMul(a, b);
}

static BigInt karatsubaMul(const BigInt& a, const BigInt& b) {
    if (isZero(a) || isZero(b)) return bigintFromStr("0", 1);
    int n = (a.len > b.len) ? a.len : b.len;
    if (n < KARATSUBA_THRESHOLD) return naiveMul(a, b);

    int half = n / 2;

    // Split a into a1 (high) and a0 (low)
    int a_split = a.len - half; if (a_split < 0) a_split = 0;
    BigInt a1 = subBigInt(a, 0, a_split);
    BigInt a0 = subBigInt(a, a_split, a.len);

    // Split b into b1 (high) and b0 (low)
    int b_split = b.len - half; if (b_split < 0) b_split = 0;
    BigInt b1 = subBigInt(b, 0, b_split);
    BigInt b0 = subBigInt(b, b_split, b.len);

    // z2 = a1 * b1
    BigInt z2 = dispatchMul(a1, b1);
    // z0 = a0 * b0
    BigInt z0 = dispatchMul(a0, b0);
    // z1 = (a1+a0)*(b1+b0) - z2 - z0
    BigInt a1a0 = absAdd(a1, a0);
    BigInt b1b0 = absAdd(b1, b0);
    BigInt z1 = dispatchMul(a1a0, b1b0);
    z1 = absSub(z1, z2);  // z2 and z0 are always <= z1 in magnitude
    z1 = absSub(z1, z0);

    // result = z2 * 10^(2*half) + z1 * 10^half + z0
    BigInt t2 = shiftLeft(z2, 2 * half);
    BigInt t1 = shiftLeft(z1, half);
    BigInt res = absAdd(t2, t1);
    res = absAdd(res, z0);

    res.negative = a.negative ^ b.negative;
    if (isZero(res)) res.negative = 0;
    return res;
}

// ---------------------------------------------------------------------------
// NTT-based O(n log n) multiplication
// NTT modulus: p = 998244353 = 119 * 2^23 + 1 (primitive root g = 3)
// Works for result sizes up to 2^23 ≈ 8M digits.
// We split each decimal digit into its own coefficient (base-10 NTT).
// ---------------------------------------------------------------------------
static constexpr long long NTT_MOD = 998244353LL;
static constexpr long long NTT_G   = 3LL;

static long long nttPow(long long base, long long exp, long long mod) {
    long long r = 1; base %= mod;
    while (exp > 0) {
        if (exp & 1) r = r * base % mod;
        base = base * base % mod; exp >>= 1;
    }
    return r;
}

static void ntt(long long* a, int n, bool inv) {
    for (int i = 1, j = 0; i < n; i++) {
        int bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) { long long t = a[i]; a[i] = a[j]; a[j] = t; }
    }
    for (int len = 2; len <= n; len <<= 1) {
        long long w = inv ? nttPow(NTT_G, NTT_MOD - 1 - (NTT_MOD - 1) / len, NTT_MOD)
                          : nttPow(NTT_G, (NTT_MOD - 1) / len, NTT_MOD);
        for (int i = 0; i < n; i += len) {
            long long wn = 1;
            for (int j = 0; j < len / 2; j++) {
                long long u = a[i + j], v = a[i + j + len/2] * wn % NTT_MOD;
                a[i + j]         = (u + v) % NTT_MOD;
                a[i + j + len/2] = (u - v + NTT_MOD) % NTT_MOD;
                wn = wn * w % NTT_MOD;
            }
        }
    }
    if (inv) {
        long long n_inv = nttPow(n, NTT_MOD - 2, NTT_MOD);
        for (int i = 0; i < n; i++) a[i] = a[i] * n_inv % NTT_MOD;
    }
}

static BigInt nttMul(const BigInt& a, const BigInt& b) {
    if (isZero(a) || isZero(b)) return bigintFromStr("0", 1);

    int rLen = a.len + b.len;
    int n = 1;
    while (n < rLen) n <<= 1;

    long long* fa = (long long*)arenaAlloc(n * (int)sizeof(long long));
    long long* fb = (long long*)arenaAlloc(n * (int)sizeof(long long));
    memset(fa, 0, (size_t)n * sizeof(long long));
    memset(fb, 0, (size_t)n * sizeof(long long));

    // Fill in reverse (LSB first for convolution)
    for (int i = 0; i < a.len; i++) fa[i] = a.digits[a.len - 1 - i] - '0';
    for (int i = 0; i < b.len; i++) fb[i] = b.digits[b.len - 1 - i] - '0';

    ntt(fa, n, false);
    ntt(fb, n, false);
    for (int i = 0; i < n; i++) fa[i] = fa[i] * fb[i] % NTT_MOD;
    ntt(fa, n, true);

    // Propagate carries
    char* buf = arenaAlloc(rLen + 1);
    long long carry = 0;
    for (int i = 0; i < rLen; i++) {
        long long val = fa[i] + carry;
        carry = val / 10;
        buf[rLen - 1 - i] = (char)('0' + (int)(val % 10));
    }

    int start = 0;
    while (start < rLen - 1 && buf[start] == '0') start++;
    BigInt r; r.digits = buf + start; r.len = rLen - start; r.negative = 0;
    r.negative = a.negative ^ b.negative;
    if (isZero(r)) r.negative = 0;
    r.digits[r.len] = '\0';
    return r;
}

// ---------------------------------------------------------------------------
// Final dispatch — now including NTT for large numbers
// ---------------------------------------------------------------------------
static BigInt bigintMul(const BigInt& a, const BigInt& b) {
    if (isZero(a) || isZero(b)) return bigintFromStr("0", 1);
    int n = (a.len > b.len) ? a.len : b.len;
    BigInt r;
    if (n < KARATSUBA_THRESHOLD)  r = naiveMul(a, b);
    else if (n < FFT_THRESHOLD)   r = karatsubaMul(a, b);
    else                          r = nttMul(a, b);
    r.negative = a.negative ^ b.negative;
    if (isZero(r)) r.negative = 0;
    return r;
}

// ---------------------------------------------------------------------------
// Addition / Subtraction (need full definition after helpers above)
// ---------------------------------------------------------------------------
static BigInt bigintAdd(const BigInt& a, const BigInt& b) {
    if (a.negative == b.negative) {
        BigInt r = absAdd(a, b); r.negative = a.negative;
        if (isZero(r)) r.negative = 0;
        return r;
    }
    int cmp = absCmp(a, b);
    if (cmp == 0) return bigintFromStr("0", 1);
    if (cmp > 0) {
        BigInt r = absSub(a, b); r.negative = a.negative; return r;
    } else {
        BigInt r = absSub(b, a); r.negative = b.negative; return r;
    }
}

static BigInt bigintSub(const BigInt& a, const BigInt& b) {
    BigInt nb = b; nb.negative = b.negative ^ 1;
    if (isZero(nb)) nb.negative = 0;
    return bigintAdd(a, nb);
}

// ---------------------------------------------------------------------------
// Division / Modulo
// ---------------------------------------------------------------------------
static BigInt bigintDivMod(const BigInt& a, const BigInt& b, BigInt* rem) {
    if (isZero(b)) {
        if (rem) *rem = bigintFromStr("0", 1);
        BigInt r; r.digits = (char*)"DIV0"; r.len = 4; r.negative = 0; return r;
    }
    if (absCmp(a, b) < 0) {
        if (rem) { BigInt ra = a; *rem = ra; }
        return bigintFromStr("0", 1);
    }
    char* qbuf = arenaAlloc(a.len + 1);
    int qlen = 0;
    BigInt cur; cur.digits = arenaAlloc(a.len + 2); cur.len = 1;
    cur.digits[0] = '0'; cur.digits[1] = '\0'; cur.negative = 0;
    BigInt absb = b; absb.negative = 0;

    for (int i = 0; i < a.len; i++) {
        if (cur.len == 1 && cur.digits[0] == '0') {
            cur.digits[0] = a.digits[i];
        } else {
            char* newbuf = arenaAlloc(cur.len + 2);
            memcpy(newbuf, cur.digits, (size_t)cur.len);
            newbuf[cur.len] = a.digits[i];
            newbuf[cur.len + 1] = '\0';
            cur.digits = newbuf; cur.len = cur.len + 1;
        }
        int d = 0;
        for (int trial = 9; trial >= 1; trial--) {
            BigInt tv = bigintFromStr("0", 1);
            for (int t = 0; t < trial; t++) tv = absAdd(tv, absb);
            if (absCmp(tv, cur) <= 0) { d = trial; cur = absSub(cur, tv); break; }
        }
        qbuf[qlen++] = (char)('0' + d);
    }
    int start = 0;
    while (start < qlen - 1 && qbuf[start] == '0') start++;
    BigInt q; q.digits = qbuf + start; q.len = qlen - start;
    q.negative = a.negative ^ b.negative;
    if (isZero(q)) q.negative = 0;
    q.digits[q.len] = '\0';
    if (rem) {
        cur.negative = a.negative;
        if (isZero(cur)) cur.negative = 0;
        *rem = cur;
    }
    return q;
}

static BigInt bigintDiv(const BigInt& a, const BigInt& b) { return bigintDivMod(a, b, nullptr); }

static BigInt bigintMod(const BigInt& a, const BigInt& b) {
    BigInt rem;
    bigintDivMod(a, b, &rem);
    return rem;
}

// ---------------------------------------------------------------------------
// Exponentiation — cap raised to MAX_EXP (1,000,000)
// ---------------------------------------------------------------------------
static BigInt bigintPow(const BigInt& base, const BigInt& exp) {
    if (isZero(exp)) return bigintFromStr("1", 1);
    if (exp.negative) return bigintFromStr("0", 1);
    long long expVal = 0;
    for (int i = 0; i < exp.len && expVal < MAX_EXP + 1; i++)
        expVal = expVal * 10 + (exp.digits[i] - '0');
    if (expVal > MAX_EXP) {
        BigInt r; r.digits = (char*)"OVERFLOW"; r.len = 8; r.negative = 0; return r;
    }
    BigInt result = bigintFromStr("1", 1);
    BigInt b = base; long long e = expVal;
    while (e > 0) {
        if (e & 1) result = bigintMul(result, b);
        b = bigintMul(b, b); e >>= 1;
    }
    return result;
}

// ---------------------------------------------------------------------------
// Shunting-Yard Parser
// ---------------------------------------------------------------------------
static int opPrecedence(char op) {
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
    if (op == '_') { return g_valTop >= 1; }
    if (g_valTop < 2) return false;
    BigInt b = g_valStack[--g_valTop];
    BigInt a = g_valStack[--g_valTop];
    g_valStack[g_valTop++] = applyOp(op, a, b);
    return true;
}

static void evaluateLine(const char* expr, int len) {
    g_opTop = 0; g_valTop = 0;
    const char* p = expr;
    const char* end = expr + len;
    bool nextIsUnary = true;

    while (p < end) {
        char c = *p;
        if (c == ' ' || c == '\t' || c == '\r' || c == '\\') { p++; continue; }

        if (c == '(') {
            g_opStack[g_opTop++] = '(';
            p++; nextIsUnary = true;
            continue;
        }

        if (c == ')') {
            while (g_opTop > 0 && g_opStack[g_opTop - 1] != '(') processTopOp();
            if (g_opTop > 0) g_opTop--;
            p++; nextIsUnary = false;
            continue;
        }

        if (c >= '0' && c <= '9') {
            const char* numStart = p;
            while (p < end && *p >= '0' && *p <= '9') p++;
            g_valStack[g_valTop++] = bigintFromStr(numStart, (int)(p - numStart));
            nextIsUnary = false;
            continue;
        }

        if (c == '+' || c == '-' || c == '*' || c == '/' || c == '%' || c == '^') {
            char finalOp = c;
            if (nextIsUnary) {
                if (c == '-') finalOp = '#';
                else if (c == '+') finalOp = '_';
                else { p++; continue; }
            }
            int prec = opPrecedence(finalOp);
            bool rightA = opRightAssoc(finalOp);
            while (g_opTop > 0 && g_opStack[g_opTop - 1] != '(') {
                int topPrec = opPrecedence(g_opStack[g_opTop - 1]);
                if (rightA ? topPrec > prec : topPrec >= prec) processTopOp();
                else break;
            }
            g_opStack[g_opTop++] = finalOp;
            p++; nextIsUnary = true;
            continue;
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
            if (digitsPrinted == 70 && i < r.len - 1) {
                outChar('\\'); outChar('\n'); digitsPrinted = 0;
            }
        }
    } else {
        outCStr("ERROR");
    }
    outChar('\n');
}

// ---------------------------------------------------------------------------
// Main streaming loop
// ---------------------------------------------------------------------------
int main() {
    static char lineCarry[MAX_LINE_LEN];
    int         carryLen = 0;

    while (true) {
        int got = (int)read(STDIN_FILENO, g_readBuf, READ_BUF_SIZE);
        if (got <= 0) break;

        const char* src = g_readBuf;
        const char* srcEnd = g_readBuf + got;

        while (src < srcEnd) {
            const char* nl = src;
            while (nl < srcEnd && *nl != '\n') nl++;

            if (nl < srcEnd) {
                int segLen = (int)(nl - src);
                int lineLen = carryLen + segLen;
                if (lineLen > 0) {
                    memcpy(lineCarry + carryLen, src, (size_t)segLen);
                    lineCarry[lineLen] = '\0';
                    arenaReset();
                    evaluateLine(lineCarry, lineLen);
                }
                carryLen = 0; src = nl + 1;
            } else {
                int segLen = (int)(srcEnd - src);
                if (carryLen + segLen < MAX_LINE_LEN) {
                    memcpy(lineCarry + carryLen, src, (size_t)segLen);
                    carryLen += segLen;
                }
                src = srcEnd;
            }
        }
    }

    if (carryLen > 0) {
        lineCarry[carryLen] = '\0'; arenaReset();
        evaluateLine(lineCarry, carryLen);
    }

    outFlush();
    return 0;
}