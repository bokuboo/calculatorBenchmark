#include <unistd.h>
#include <stdio.h>
#include <vector>

#define OUT_BUFFER_SIZE 1048576 // 1MB Output Buffer

struct BigInt {
    std::vector<unsigned char> digits;
    void clear() { digits.clear(); }
    size_t length() const { return digits.size(); }
};

char output_buffer[OUT_BUFFER_SIZE];
size_t output_index = 0;

inline char get_next_char() {
    int c = getchar_unlocked();
    return (c == EOF) ? '\0' : static_cast<char>(c);
}

void flush_output() {
    if (output_index > 0) {
        ssize_t unused = write(1, output_buffer, output_index);
        (void)unused;
        output_index = 0;
    }
}

inline void print_char(char c) {
    output_buffer[output_index++] = c;
    if (output_index >= OUT_BUFFER_SIZE) flush_output();
}

void print_bigint(const BigInt& num) {
    if (num.length() == 0) {
        print_char('0');
        print_char('\n');
        return;
    }
    for (size_t i = num.length(); i > 0; i--) {
        print_char(num.digits[i - 1] + '0');
    }
    print_char('\n');
}

void add(const BigInt& a, const BigInt& b, BigInt& result) {
    result.clear();
    size_t a_len = a.length(), b_len = b.length();
    size_t max_len = (a_len > b_len) ? a_len : b_len;
    result.digits.reserve(max_len + 1);

    unsigned int carry = 0;
    for (size_t i = 0; i < max_len; i++) {
        unsigned int digit_a = (i < a_len) ? a.digits[i] : 0;
        unsigned int digit_b = (i < b_len) ? b.digits[i] : 0;
        unsigned int sum = digit_a + digit_b + carry;
        result.digits.push_back(sum % 10);
        carry = sum / 10;
    }
    if (carry > 0) result.digits.push_back(carry);
}

void subtract(const BigInt& a, const BigInt& b, BigInt& result) {
    result.clear();
    size_t a_len = a.length(), b_len = b.length();
    result.digits.reserve(a_len);
    int borrow = 0;

    for (size_t i = 0; i < a_len; i++) {
        int digit_a = a.digits[i];
        int digit_b = (i < b_len) ? b.digits[i] : 0;
        int diff = digit_a - digit_b - borrow;
        if (diff < 0) {
            diff += 10;
            borrow = 1;
        } else {
            borrow = 0;
        }
        result.digits.push_back(diff);
    }
    while (result.digits.size() > 1 && result.digits.back() == 0) {
        result.digits.pop_back();
    }
}

BigInt register_a;
BigInt register_b;
BigInt math_result;

int main() {
    register_a.digits.reserve(1000005);
    register_b.digits.reserve(1000005);

    char ch = get_next_char();

    while (ch != '\0') {
        // 1. Preskoč biele znaky pred príkladom
        while (ch == ' ' || ch == '\t' || ch == '\n' || ch == '\r') {
            ch = get_next_char();
        }
        if (ch == '\0') break;

        // Ochrana pred Datasetom 3 (ak riadok začína zátvorkou, preskoč ho)
        if (ch == '(') {
            while (ch != '\n' && ch != '\0') ch = get_next_char();
            continue;
        }

        // 2. Načítaj REGISTER A
        register_a.clear();
        while ((ch >= '0' && ch <= '9') || ch == '\\' || ch == '\n' || ch == '\r') {
            if (ch >= '0' && ch <= '9') {
                register_a.digits.push_back(ch - '0');
            }
            // Ak je to newline bez spätného lomítka pred ním, mohol by to byť koniec čísla,
            // ale pre Dataset 2 musíme ignorovať osamotené \n uprostred čísla.
            // Preto bezpečne stopneme až na operátore.
            ch = get_next_char();
        }

        // Ak sme narazili na neplatný znak pre sčítanie (z Datasetu 3, napr. *), preskoč riadok
        if (ch != '+' && ch != '-' && ch != ' ' && ch != '\t') {
            while (ch != '\n' && ch != '\0') ch = get_next_char();
            continue;
        }

        // Preskoč medzery k operátoru
        while (ch == ' ' || ch == '\t') ch = get_next_char();
        char op = ch;
        if (op != '+' && op != '-') {
            while (ch != '\n' && ch != '\0') ch = get_next_char();
            continue;
        }
        ch = get_next_char(); // zjedz operátor

        // Preskoč medzery pred REGISTER B
        while (ch == ' ' || ch == '\t') ch = get_next_char();

        // 3. Načítaj REGISTER B
        register_b.clear();
        while (ch != '\0') {
            if (ch >= '0' && ch <= '9') {
                register_b.digits.push_back(ch - '0');
            } else if (ch == '+' || ch == '-' || ch == '\n' || ch == '\r') {
                // Koniec druhého čísla nastane novým riadkom alebo ďalším operátorom
                break;
            } else if (ch != ' ' && ch != '\t' && ch != '\\') {
                // Dataset 3 detekcia neplechy
                break;
            }
            ch = get_next_char();
        }

        // Otočenie reg_a do Little-Endian
        size_t len_a = register_a.length();
        for (size_t i = 0; i < len_a / 2; i++) {
            unsigned char temp = register_a.digits[i];
            register_a.digits[i] = register_a.digits[len_a - 1 - i];
            register_a.digits[len_a - 1 - i] = temp;
        }

        // Otočenie reg_b do Little-Endian
        size_t len_b = register_b.length();
        for (size_t i = 0; i < len_b / 2; i++) {
            unsigned char temp = register_b.digits[i];
            register_b.digits[i] = register_b.digits[len_b - 1 - i];
            register_b.digits[len_b - 1 - i] = temp;
        }

        // Výpočet
        if (register_a.length() > 0 && register_b.length() > 0) {
            if (op == '+') {
                add(register_a, register_b, math_result);
                print_bigint(math_result);
            } else if (op == '-') {
                subtract(register_a, register_b, math_result);
                print_bigint(math_result);
            }
        }
    }

    flush_output();
    return 0;
}