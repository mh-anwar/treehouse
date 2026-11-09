"""Tiny stdlib QR code encoder (byte mode, error correction level M) that renders an SVG.

Used by sharedbox-web for "add a device" links, so a phone can scan the login link off a laptop screen.
Follows ISO/IEC 18004 the same way Project Nayuki's reference encoder does (MIT); versions 1-40.
"""

# per version (index 0 unused), error correction level M
_ECC_PER_BLOCK = [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26, 30, 22, 22, 24, 24, 28, 28, 26, 26, 26,
                  26, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28, 28]
_NUM_BLOCKS = [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5, 5, 8, 9, 9, 10, 10, 11, 13, 14, 16,
               17, 17, 18, 20, 21, 23, 25, 26, 28, 29, 31, 33, 35, 37, 38, 40, 43, 45, 47, 49]
_ECL_BITS = 0  # M


def _raw_modules(ver):
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        na = ver // 7 + 2
        n -= (25 * na - 10) * na - 55
        if ver >= 7:
            n -= 36
    return n


def _data_codewords(ver):
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _NUM_BLOCKS[ver]


def _gf_mul(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree):
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 2)
    return res


def _rs_remainder(data, div):
    res = [0] * len(div)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, c in enumerate(div):
            res[i] ^= _gf_mul(c, factor)
    return res


def _bit(x, i):
    return (x >> i) & 1 != 0


class _Qr:
    def __init__(self, ver, codewords, mask=None):
        self.ver, self.size = ver, ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fn = [[False] * self.size for _ in range(self.size)]
        self._function_patterns()
        self._draw_codewords(self._add_ecc(codewords))
        if mask is None:
            best = None
            for m in range(8):
                self._apply_mask(m)
                self._format_bits(m)
                p = self._penalty()
                if best is None or p < best[0]:
                    best = (p, m)
                self._apply_mask(m)  # xor again = undo
            mask = best[1]
        self._apply_mask(mask)
        self._format_bits(mask)
        self.mask = mask

    def _set(self, x, y, dark):
        self.mod[y][x] = dark
        self.fn[y][x] = True

    def _function_patterns(self):
        s = self.size
        for i in range(s):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self._set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = self._align_positions()
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self._format_bits(0)  # reserve the area; overwritten later
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                b, a = _bit(bits, i), s - 11 + i % 3
                self._set(a, i // 3, b)
                self._set(i // 3, a, b)

    def _align_positions(self):
        if self.ver == 1:
            return []
        n = self.ver // 7 + 2
        step = (self.ver * 8 + n * 3 + 5) // (n * 4 - 4) * 2
        return list(reversed([self.size - 7 - i * step for i in range(n - 1)] + [6]))

    def _format_bits(self, mask):
        data = _ECL_BITS << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        s = self.size
        for i in range(6):
            self._set(8, i, _bit(bits, i))
        self._set(8, 7, _bit(bits, 6))
        self._set(8, 8, _bit(bits, 7))
        self._set(7, 8, _bit(bits, 8))
        for i in range(9, 15):
            self._set(14 - i, 8, _bit(bits, i))
        for i in range(8):
            self._set(s - 1 - i, 8, _bit(bits, i))
        for i in range(8, 15):
            self._set(8, s - 15 + i, _bit(bits, i))
        self._set(8, s - 8, True)

    def _add_ecc(self, data):
        ver = self.ver
        nblocks, ecclen = _NUM_BLOCKS[ver], _ECC_PER_BLOCK[ver]
        raw = _raw_modules(ver) // 8
        nshort = nblocks - raw % nblocks
        shortlen = raw // nblocks
        div = _rs_divisor(ecclen)
        blocks, k = [], 0
        for i in range(nblocks):
            dat = data[k:k + shortlen - ecclen + (0 if i < nshort else 1)]
            k += len(dat)
            ecc = _rs_remainder(dat, div)
            if i < nshort:
                dat = dat + [0]
            blocks.append(dat + ecc)
        out = []
        for i in range(len(blocks[0])):
            for j, blk in enumerate(blocks):
                if i != shortlen - ecclen or j >= nshort:
                    out.append(blk[i])
        return out

    def _draw_codewords(self, data):
        s, i, total = self.size, 0, len(data) * 8
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = s - 1 - vert if upward else vert
                    if not self.fn[y][x] and i < total:
                        self.mod[y][x] = _bit(data[i >> 3], 7 - (i & 7))
                        i += 1
            right -= 2

    _MASKS = (
        lambda x, y: (x + y) % 2 == 0,
        lambda x, y: y % 2 == 0,
        lambda x, y: x % 3 == 0,
        lambda x, y: (x + y) % 3 == 0,
        lambda x, y: (x // 3 + y // 2) % 2 == 0,
        lambda x, y: x * y % 2 + x * y % 3 == 0,
        lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
        lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
    )

    def _apply_mask(self, m):
        f = self._MASKS[m]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fn[y][x] and f(x, y):
                    self.mod[y][x] = not self.mod[y][x]

    def _penalty(self):
        """Simplified mask score (runs, 2x2 blocks, finder look-alikes, balance). Any mask decodes;
        this only picks a scanner-friendly one."""
        s, m, p = self.size, self.mod, 0
        lines = m + [[m[y][x] for y in range(s)] for x in range(s)]
        finder = ([True, False, True, True, True, False, True, False, False, False, False],
                  [False, False, False, False, True, False, True, True, True, False, True])
        for line in lines:
            run, prev = 0, None
            for v in line:
                if v == prev:
                    run += 1
                    if run == 5:
                        p += 3
                    elif run > 5:
                        p += 1
                else:
                    run, prev = 1, v
            for i in range(s - 10):
                if line[i:i + 11] in finder:
                    p += 40
        for y in range(s - 1):
            for x in range(s - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    p += 3
        dark = sum(sum(r) for r in m)
        p += (abs(dark * 20 - s * s * 10) + s * s - 1) // (s * s) * 10
        return p


def encode(text, mask=None):
    """The QR module matrix (list of rows of bools) for `text`, smallest version that fits."""
    data = text.encode("utf-8")
    for ver in range(1, 41):
        cap = _data_codewords(ver) * 8
        cc_bits = 8 if ver < 10 else 16
        need = 4 + cc_bits + len(data) * 8
        if need <= cap:
            break
    else:
        raise ValueError("text too long for a QR code")
    bits = []

    def put(val, n):
        bits.extend((val >> i) & 1 for i in reversed(range(n)))

    put(0b0100, 4)
    put(len(data), cc_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, cap - len(bits)))
    put(0, -len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < cap // 8:
        words.append(pad)
        pad ^= 0xEC ^ 0x11
    return _Qr(ver, words, mask).mod


def svg(text, scale=6, border=4):
    """An SVG string (dark modules on white) for `text`."""
    m = encode(text)
    n = len(m) + 2 * border
    path = "".join(f"M{x + border},{y + border}h1v1h-1z" for y, row in enumerate(m)
                   for x, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n * scale}" '
            f'height="{n * scale}" shape-rendering="crispEdges" role="img" aria-label="QR code">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
