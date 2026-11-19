"""NB-IoT downlink receiver library (3GPP TS 36.211 / 36.212 / 36.213 / 36.331, Rel-13/14).

Used in GRC as a Python Module block with id `nbiot`. Pure numpy; pycrate is optional
(needed only to print SIB contents and to find the SI-message schedule).

Scope: standalone and guard-band anchor carriers (full chain), in-band (sync + MIB-NB only).
Signal processing that GNU Radio has stock blocks for (tuning, filtering, FFT, sub-carrier
selection, displays) is NOT here; this file holds only what stock blocks cannot do.
"""
import numpy as np

# ----------------------------------------------------------------------------------------
# Numerology at 1.92 MS/s: 128-point FFT, 15 kHz sub-carriers, normal CP
# ----------------------------------------------------------------------------------------
FS = 1.92e6
NFFT = 128
SF_LEN = 1920
FRAME_LEN = 19200
CP = [10, 9, 9, 9, 9, 9, 9] * 2                       # per symbol in a subframe
SYM_START = np.concatenate(([0], np.cumsum([c + NFFT for c in CP])[:-1])).astype(int)
WIN_START = SYM_START + np.array(CP)                   # FFT window start (no back-off)
BACKOFF = 2                                            # samples of CP used to dodge ISI
NPSS_OFFSET = 5 * SF_LEN + SYM_START[3]                # 10012: NPSS start inside a frame
SHIFT_VEC = np.exp(1j * np.pi * np.arange(NFFT) / NFFT)  # half-subcarrier shift, per window
CONT_DEROT = np.exp(-1j * np.pi * ((np.arange(140) // 14) * SF_LEN + WIN_START[np.arange(140) % 14]) / NFFT)
SC_OFFSET = 59                                         # first NB-IoT bin after fftshift
SI_RNTI = 0xFFFF


# ----------------------------------------------------------------------------------------
# Generic helpers: Gold sequence, CRC
# ----------------------------------------------------------------------------------------
_gold_cache = {}


def gold(c_init, n):
    """LTE pseudo-random sequence c(0..n-1), 36.211 7.2."""
    key = (int(c_init), int(n))
    if key in _gold_cache:
        return _gold_cache[key]
    nc = 1600
    tot = n + nc
    x1 = np.zeros(tot + 31, np.uint8)
    x2 = np.zeros(tot + 31, np.uint8)
    x1[0] = 1
    x2[:31] = [(int(c_init) >> i) & 1 for i in range(31)]
    for i in range(tot):
        x1[i + 31] = x1[i + 3] ^ x1[i]
        x2[i + 31] = x2[i + 3] ^ x2[i + 2] ^ x2[i + 1] ^ x2[i]
    c = (x1[nc:nc + n] ^ x2[nc:nc + n]).astype(np.uint8)
    if len(_gold_cache) > 4000:
        _gold_cache.clear()
    _gold_cache[key] = c
    return c


def crc_remainder(bits, poly, L):
    """Remainder of bits(x) * x^L / g(x); returns L bits, MSB first. bits: 2-D (H, n) uint8."""
    bits = np.atleast_2d(bits).astype(np.int64)
    reg = np.zeros(bits.shape[0], np.int64)
    top = 1 << (L - 1)
    mask = (1 << L) - 1
    for i in range(bits.shape[1]):
        fb = ((reg & top) > 0).astype(np.int64) ^ bits[:, i]
        reg = ((reg << 1) & mask) ^ (fb * poly)
    return ((reg[:, None] >> np.arange(L - 1, -1, -1)) & 1).astype(np.uint8)


CRC16 = 0x1021      # D^16 + D^12 + D^5 + 1
CRC24A = 0x864CFB


def bits_to_int(b):
    v = 0
    for x in b:
        v = (v << 1) | int(x)
    return v


def bits_to_bytes(b):
    b = list(b) + [0] * ((-len(b)) % 8)
    return bytes(bits_to_int(b[i:i + 8]) for i in range(0, len(b), 8))


# ----------------------------------------------------------------------------------------
# NPSS (36.211 10.2.7.1) and NSSS (10.2.7.2)
# ----------------------------------------------------------------------------------------
NPSS_COVER = np.array([1, 1, 1, 1, -1, -1, 1, 1, 1, -1, 1])


def npss_freq():
    n = np.arange(11)
    return np.exp(-1j * np.pi * 5 * n * (n + 1) / 11.0)


def npss_template(fs=FS, continuous=False):
    """NPSS time waveform (symbols 3..13 of subframe 5 incl. CPs), unit energy.
    Standalone/guard-band sub-carrier layout: (k - 5.5) * 15 kHz.
    continuous=False: phase of the 7.5 kHz half-subcarrier offset restarts at every FFT window
    (srsRAN). continuous=True: the offset runs continuously (an LTE-grid OFDM modulator followed
    by a +7.5 kHz shift), which adds exp(j*pi*n_win/128) per symbol."""
    ts = 1.0 / 30.72e6
    starts, cps, t = [], [], 0.0
    for l in range(14):
        ncp = 160 if l % 7 == 0 else 144
        starts.append(t)
        cps.append(ncp * ts)
        t += (ncp + 2048) * ts
    t0, t_end = starts[3], starts[13] + cps[13] + 2048 * ts
    tt = t0 + np.arange(int(round((t_end - t0) * fs))) / fs
    f = (np.arange(11) - 5.5) * 15e3
    d = npss_freq()
    x = np.zeros(len(tt), complex)
    for i, l in enumerate(range(3, 14)):
        m = (tt >= starts[l] - 1e-12) & (tt < starts[l] + cps[l] + 2048 * ts - 1e-12)
        tau = tt[m] - starts[l] - cps[l]
        x[m] = NPSS_COVER[i] * (np.exp(2j * np.pi * np.outer(tau, f)) @ d)
        if continuous:
            x[m] *= np.exp(1j * np.pi * 15e3 * (5e-3 + starts[l] + cps[l]))
    return x / np.sqrt(np.sum(np.abs(x) ** 2))


def npss_matched_taps(fs, continuous=False):
    """FIR taps that turn a stock FIR/FFT filter into an NPSS correlator."""
    return list(np.conj(npss_template(fs, continuous)[::-1]))


def _hadamard_row(r):
    c = np.arange(128)
    pc = np.array([bin(r & int(x)).count('1') for x in c])
    return 1 - 2 * (pc & 1)


_BQ = np.array([_hadamard_row(r) for r in (0, 31, 63, 127)])


def nsss_seq(pci, theta_idx):
    """NSSS d(n), n = 0..131. theta_idx = (nf/2) mod 4  ->  theta_f = theta_idx/4."""
    n = np.arange(132)
    u = pci % 126 + 3
    q = pci // 126
    npr = n % 131
    return (_BQ[q][n % 128] * np.exp(-2j * np.pi * (theta_idx / 4.0) * n)
            * np.exp(-1j * np.pi * u * npr * (npr + 1) / 131.0))


_NSSS_BANK = None


def nsss_bank():
    """(504*4, 11, 12) conjugated NSSS sequences laid out on the grid (k first, then l)."""
    global _NSSS_BANK
    if _NSSS_BANK is None:
        bank = np.zeros((504 * 4, 11, 12), complex)
        for p in range(504):
            for th in range(4):
                bank[p * 4 + th] = np.conj(nsss_seq(p, th)).reshape(11, 12)
        _NSSS_BANK = bank
    return _NSSS_BANK


# ----------------------------------------------------------------------------------------
# NRS (36.211 10.2.6) and channel estimation
# ----------------------------------------------------------------------------------------
def nrs_k(pci, port, lsym):
    """Sub-carriers of NRS for port 0/1 (2000/2001) in slot-symbol lsym (5 or 6)."""
    v = (0 if lsym == 5 else 3) if port == 0 else (3 if lsym == 5 else 0)
    return [6 * m + (v + pci % 6) % 6 for m in range(2)]


_nrs_cache = {}


def nrs_table(pci):
    """{(sf, port): [(l_in_sf, k, value), ...]} for the 10 subframes of a frame."""
    if pci in _nrs_cache:
        return _nrs_cache[pci]
    tab = {}
    for sf in range(10):
        for port in range(2):
            lst = []
            for slot in range(2):
                ns = 2 * sf + slot
                for lsym in (5, 6):
                    c_init = 1024 * (7 * (ns + 1) + lsym + 1) * (2 * pci + 1) + 2 * pci + 1
                    c = gold(c_init, 2 * 111 + 2)
                    for m, k in enumerate(nrs_k(pci, port, lsym)):
                        mp = m + 109
                        val = ((1 - 2.0 * c[2 * mp]) + 1j * (1 - 2.0 * c[2 * mp + 1])) / np.sqrt(2)
                        lst.append((slot * 7 + lsym, k, val))
            tab[(sf, port)] = lst
    _nrs_cache[pci] = tab
    return tab


def chest(Y, pci, sf, port):
    """Channel estimate for one subframe Y (14, 12) from its NRS.
    Model per slot: H(l, k) = a(l) * exp(j*beta*k), a(l) linear in time. Returns (H, noise_var, pilot_power)."""
    pil = nrs_table(pci)[(sf, port)]
    l = np.array([p[0] for p in pil])
    k = np.array([p[1] for p in pil])
    h = Y[l, k] * np.conj(np.array([p[2] for p in pil]))
    # frequency slope from the k / k+6 pairs (same symbol)
    acc = 0j
    for i in range(0, len(h), 2):
        acc += h[i + 1] * np.conj(h[i]) * (1 if k[i + 1] > k[i] else -1)
    beta = np.angle(acc) / 6.0
    hd = h * np.exp(-1j * beta * k)
    a0 = hd[l < 7].mean()
    a1 = hd[l >= 7].mean()
    res = np.concatenate((hd[l < 7] - a0, hd[l >= 7] - a1))
    nvar = max(np.mean(np.abs(res) ** 2) * 4.0 / 3.0, 1e-12)
    ll = np.arange(14)[:, None]
    a = a0 + (a1 - a0) * (ll - 5.5) / 7.0
    H = a * np.exp(1j * beta * np.arange(12)[None, :])
    return H, nvar, np.mean(np.abs(hd) ** 2)


def chest_frame(G, pci, port, sfs):
    """Frequency-selective channel estimate for a whole frame from the NRS in subframes `sfs`.
    NRS of one port sit on 4 sub-carriers spaced by 3. Per frame (fixed receiver):
      1. common phase drift w (residual CFO) from pilots 7 symbols apart on the same sub-carrier,
      2. per-pilot-sub-carrier average (after removing the drift) -> H at 4 frequencies,
      3. interpolation across the 12 sub-carriers after removing the mean delay slope,
      4. a common complex linear trend in time.
    Handles multipath (frequency-selective fading) that a flat/linear-phase model cannot.
    Returns (H (140, 12), noise variance)."""
    tab = nrs_table(pci)
    t, k, h = [], [], []
    for sf in sfs:
        for (l, kk, v) in tab[(sf, port)]:
            t.append(sf * 14 + l)
            k.append(kk)
            h.append(G[sf * 14 + l, kk] * np.conj(v))
    t, k, h = np.array(t, float), np.array(k), np.array(h)
    hb = h.reshape(-1, 8)                      # per subframe: s0 l5 (m0,m1), s0 l6, s1 l5, s1 l6
    w = np.angle(np.sum(hb[:, 4:] * np.conj(hb[:, :4]))) / 7.0
    hd = h * np.exp(-1j * w * t)
    kp = np.unique(k)                          # 4 pilot sub-carriers
    Hk = np.array([hd[k == q].mean() for q in kp])
    # mean delay slope between neighbouring pilot sub-carriers (spacing 3)
    beta = np.angle(np.sum(Hk[1:] * np.conj(Hk[:-1]))) / 3.0
    Rk = Hk * np.exp(-1j * beta * kp)
    kk = np.arange(12)
    re = np.interp(kk, kp, Rk.real, left=np.nan, right=np.nan)
    im = np.interp(kk, kp, Rk.imag, left=np.nan, right=np.nan)
    R = re + 1j * im
    for side in (kk < kp[0], kk > kp[-1]):     # linear extrapolation at the band edges
        if np.any(side):
            a, b = (0, 1) if side[0] else (-2, -1)
            slope = (Rk[b] - Rk[a]) / (kp[b] - kp[a])
            R[side] = Rk[a] + slope * (kk[side] - kp[a])
    Hf = R * np.exp(1j * beta * kk)
    # common linear trend in time (amplitude / slow phase), relative to the frame average
    ratio = hd / Hk[np.searchsorted(kp, k)]
    tm = t.mean()
    if len(sfs) >= 3:
        A = np.vstack((np.ones(len(t)), t - tm)).T
        coef = np.linalg.lstsq(A, ratio, rcond=None)[0]
    else:
        coef = np.array([1.0 + 0j, 0j])
    fit = (coef[0] + coef[1] * (t - tm)) * Hk[np.searchsorted(kp, k)]
    dof = max(len(h) - len(kp) - 2, 1)
    nvar = max(np.sum(np.abs(hd - fit) ** 2) / dof, 1e-12)
    tt = np.arange(140)[:, None]
    H = (coef[0] + coef[1] * (tt - tm)) * Hf[None, :] * np.exp(1j * w * tt)
    return H, nvar


# ----------------------------------------------------------------------------------------
# Equalisation / QPSK soft bits
# ----------------------------------------------------------------------------------------
def equalize(y, h0, h1, nvar, ports):
    """y, h0, h1: RE vectors in mapping order. Returns (soft symbols z scaled as LLR source, display symbols)."""
    if ports == 1:
        z = y * np.conj(h0)
        g = np.abs(h0) ** 2
    else:
        # SFBC pair: ya = h0a x0 - h1a x1*,  yb* = h1b* x0 + h0b* x1*  -> exact 2x2 solve per pair
        # (no "same channel on both REs" assumption, so it holds under multipath)
        ya, yb = y[0::2], y[1::2]
        h0a, h0b, h1a, h1b = h0[0::2], h0[1::2], h1[0::2], h1[1::2]
        a, b, c, d = h0a, -h1a, np.conj(h1b), np.conj(h0b)
        det = a * d - b * c
        det = np.where(np.abs(det) < 1e-15, 1e-15, det)
        x0 = (d * ya - b * np.conj(yb)) / det
        x1 = np.conj((-c * ya + a * np.conj(yb)) / det)
        v0 = (np.abs(d) ** 2 + np.abs(b) ** 2) / np.abs(det) ** 2       # noise enhancement
        v1 = (np.abs(c) ** 2 + np.abs(a) ** 2) / np.abs(det) ** 2
        disp = np.empty(len(y), complex)
        disp[0::2], disp[1::2] = x0, x1
        v = np.empty(len(y))
        v[0::2], v[1::2] = v0, v1
        return disp / (v * nvar), disp
    disp = z / np.maximum(g, 1e-15)
    return z / nvar, disp


def qpsk_llr(z):
    """LLR = log P(0)/P(1); bit order b(2i) on I, b(2i+1) on Q."""
    out = np.empty(2 * len(z))
    out[0::2] = z.real
    out[1::2] = z.imag
    return out


def snr_db(z):
    """Blind M2M4 SNR estimate of equalised QPSK symbols (dB); about -20 for pure noise."""
    z = np.asarray(z)
    m2 = np.mean(np.abs(z) ** 2)
    m4 = np.mean(np.abs(z) ** 4)
    sig = np.sqrt(max(2 * m2 * m2 - m4, 0.0))
    return 10 * np.log10(sig / max(m2 - sig, 1e-12 * m2) + 1e-2)


# ----------------------------------------------------------------------------------------
# Tail-biting convolutional code (36.212 5.1.3.1) and rate matching (5.1.4.2)
# ----------------------------------------------------------------------------------------
_POLY = (0x6D, 0x4F, 0x57)        # 133, 171, 165 octal, newest bit = LSB


def _parity(x):
    return bin(x).count('1') & 1


def conv_encode(bits):
    bits = list(bits)
    sr = 0
    for b in bits[-6:]:
        sr = (sr << 1) | b
    out = []
    for b in bits:
        sr = ((sr << 1) | b) & 0x7F
        out += [_parity(sr & p) for p in _POLY]
    return np.array(out, np.uint8)


# trellis: next state ns = ((s << 1) | b) & 63; predecessors p0 = ns >> 1, p1 = p0 | 32
_NS = np.arange(64)
_P0 = _NS >> 1
_P1 = _P0 | 32
_S0 = np.array([[1 - 2 * _parity((((p << 1) | (ns & 1)) & 0x7F) & q) for q in _POLY]
                for ns, p in zip(_NS, _P0)], float)
_S1 = np.array([[1 - 2 * _parity((((p << 1) | (ns & 1)) & 0x7F) & q) for q in _POLY]
                for ns, p in zip(_NS, _P1)], float)


def viterbi_tb(llr):
    """Batched wrap-around Viterbi for the LTE tail-biting code.
    llr: (H, 3*L) with LLR>0 meaning bit 0. Returns (H, L) uint8."""
    llr = np.atleast_2d(llr)
    H, n3 = llr.shape
    L = n3 // 3
    r = llr.reshape(H, L, 3)
    W = min(L, 96)
    ext = np.concatenate((r[:, L - W:], r, r[:, :W]), axis=1)
    T = ext.shape[1]
    M = np.zeros((H, 64))
    dec = np.zeros((T, H, 64), bool)
    for t in range(T):
        bm0 = ext[:, t] @ _S0.T
        bm1 = ext[:, t] @ _S1.T
        m0 = M[:, _P0] + bm0
        m1 = M[:, _P1] + bm1
        d = m1 > m0
        dec[t] = d
        M = np.where(d, m1, m0)
        M -= M.max(axis=1, keepdims=True)
    st = np.argmax(M, axis=1)
    bits = np.zeros((H, T), np.uint8)
    hidx = np.arange(H)
    for t in range(T - 1, -1, -1):
        bits[:, t] = st & 1
        st = (st >> 1) | (dec[t, hidx, st].astype(int) << 5)
    return bits[:, W:W + L]


_PERM_CC = [1, 17, 9, 25, 5, 21, 13, 29, 3, 19, 11, 27, 7, 23, 15, 31,
            0, 16, 8, 24, 4, 20, 12, 28, 2, 18, 10, 26, 6, 22, 14, 30]
_rm_cache = {}


def rm_conv_map(D, E):
    """Index (into the 3*D interleaved coded bits d[3i+s]) of each of the E rate-matched bits."""
    key = (D, E)
    if key in _rm_cache:
        return _rm_cache[key]
    nrows = (D - 1) // 32 + 1
    kp = nrows * 32
    nd = kp - D
    w = []
    for s in range(3):
        for j in range(32):
            for i in range(nrows):
                pos = i * 32 + _PERM_CC[j]
                w.append(-1 if pos < nd else (pos - nd) * 3 + s)
    w = [x for x in w if x >= 0]
    idx = np.array([w[k % len(w)] for k in range(E)])
    _rm_cache[key] = idx
    return idx


def rm_conv_rx(llr_e, D):
    """Soft de-rate-matching: combine all repetitions. llr_e (..., E) -> (..., 3D)."""
    llr_e = np.atleast_2d(llr_e)
    idx = rm_conv_map(D, llr_e.shape[1])
    out = np.zeros((llr_e.shape[0], 3 * D))
    for h in range(llr_e.shape[0]):
        np.add.at(out[h], idx, llr_e[h])
    return out


# ----------------------------------------------------------------------------------------
# NPBCH / MIB-NB (36.211 10.2.4, 36.212 6.4.1, 36.331 MasterInformationBlock-NB)
# ----------------------------------------------------------------------------------------
def npbch_re(pci):
    """(l, k) of the 100 NPBCH REs in subframe 0, in mapping order (k first, then l).
    Always skips 4-port LTE CRS and 2-port NRS positions."""
    out = []
    for l in range(3, 14):
        for k in range(12):
            if l in (3, 9, 10) or k % 3 != pci % 3:
                out.append((l, k))
    return out


def npbch_rotation(pci, nf8):
    """Rel-14 per-frame symbol rotation theta_f(i), i = 0..99."""
    c = gold((pci + 1) * (nf8 + 1) ** 3 * 512 + pci, 200)
    lut = np.array([1, -1, 1j, -1j])
    return lut[2 * c[0::2] + c[1::2]]


OPMODE = ['inband-SamePCI', 'inband-DifferentPCI', 'guardband', 'standalone']


def parse_mib(b):
    b = [int(x) for x in b]
    m = {'sfn_msb': bits_to_int(b[0:4]), 'hsfn_lsb': bits_to_int(b[4:6]),
         'sched_sib1': bits_to_int(b[6:10]), 'value_tag': bits_to_int(b[10:15]),
         'ab_enabled': b[15], 'opmode': OPMODE[bits_to_int(b[16:18])],
         'opmode_bits': b[18:23], 'add_tx_sib1': b[23], 'spare': b[24:34]}
    if m['opmode'] == 'inband-SamePCI':
        m['eutra_crs_seq'] = bits_to_int(b[18:23])
    elif m['opmode'] == 'inband-DifferentPCI':
        m['eutra_num_crs_ports'] = 'four' if b[18] else 'same'
        m['raster_offset'] = ['khz-7dot5', 'khz-2dot5', 'khz2dot5', 'khz7dot5'][bits_to_int(b[19:21])]
    elif m['opmode'] == 'guardband':
        m['raster_offset'] = ['khz-7dot5', 'khz-2dot5', 'khz2dot5', 'khz7dot5'][bits_to_int(b[18:20])]
    return m


SIB1_REP = [4, 8, 16] * 4
SIB1_TBS = [208] * 3 + [328] * 3 + [440] * 3 + [680] * 3


def sib1_start_frame(pci, nrep):
    if nrep == 4:
        return [0, 16, 32, 48][pci % 4]
    if nrep == 8:
        return [0, 16][pci % 2]
    return [0, 1][pci % 2]


def sib1_frames(pci, sched):
    """{sfn mod 256: (repetition r, subframe-in-codeword j)} carrying SIB1-NB (in subframe 4)."""
    nrep = SIB1_REP[sched]
    s0 = sib1_start_frame(pci, nrep)
    out = {}
    for r in range(nrep):
        for j in range(8):
            out[(s0 + r * 256 // nrep + 2 * j) % 256] = (r, j)
    return out


# ----------------------------------------------------------------------------------------
# NPDSCH (36.211 10.2.3, 36.212 6.4.2)
# ----------------------------------------------------------------------------------------
def npdsch_re(pci, ports, l_start=0):
    out = []
    for l in range(l_start, 14):
        skip = set()
        if l % 7 in (5, 6):
            for p in range(ports):
                skip.update(nrs_k(pci, p, l % 7))
        out += [(l, k) for k in range(12) if k not in skip]
    return out


def bcch_cinit(variant, pci, nf, sf):
    if variant == 'r14':
        return (SI_RNTI << 15) + (pci + 1) * ((nf % 61) + 1)
    return (SI_RNTI << 14) + ((nf % 2) << 13) + (sf << 9) + pci     # floor(ns/2) = subframe


def tbcc_decode(llr_e, tbs, crc_poly=CRC24A, crc_len=24):
    """De-rate-match, Viterbi, CRC. Returns (ok, payload bits)."""
    D = tbs + crc_len
    soft = rm_conv_rx(llr_e, D)
    bits = viterbi_tb(soft)[0]
    rem = crc_remainder(bits[None, :tbs], crc_poly, crc_len)[0]
    return bool(np.all(rem == bits[tbs:])), bits[:tbs]


# ----------------------------------------------------------------------------------------
# ASN.1 (optional pycrate)
# ----------------------------------------------------------------------------------------
_asn = None


def asn():
    global _asn
    if _asn is None:
        try:
            from pycrate_asn1dir import RRCLTE
            _asn = RRCLTE.NBIOT_RRC_Definitions.BCCH_DL_SCH_Message_NB
        except Exception:
            _asn = False
    return _asn


def decode_bcch(payload_bytes):
    """Returns (text, value) or (None, None) if pycrate is missing / decoding fails."""
    M = asn()
    if not M:
        return None, None
    try:
        M.from_uper(payload_bytes)
        return M.to_asn1(), M.get_val()
    except Exception as e:
        return 'ASN.1 decode error: %s' % e, None


SI_PERIOD = [64, 128, 256, 512, 1024, 2048, 4096]
SI_REP = [2, 4, 8, 16]
SI_TB = [56, 120, 208, 256, 328, 440, 552, 680]
SI_WIN = [16, 32, 48, 64, 96, 128, 160]   # frames


def _enum_idx(v, prefix_list):
    return prefix_list.index(v)


def sib1_schedule(val):
    """Extract what the SI-message scheduler needs from a decoded SIB1-NB value."""
    sib = val['message'][1][1]
    per = ['rf64', 'rf128', 'rf256', 'rf512', 'rf1024', 'rf2048', 'rf4096']
    rep = ['every2ndRF', 'every4thRF', 'every8thRF', 'every16thRF']
    tb = ['b56', 'b120', 'b208', 'b256', 'b328', 'b440', 'b552', 'b680']
    win = ['ms160', 'ms320', 'ms480', 'ms640', 'ms960', 'ms1280', 'ms1600']
    entries = []
    for e in sib['schedulingInfoList-r13']:
        entries.append({'T': SI_PERIOD[per.index(e['si-Periodicity-r13'])],
                        'rep': SI_REP[rep.index(e['si-RepetitionPattern-r13'])],
                        'tbs': SI_TB[tb.index(e['si-TB-r13'])],
                        'sibs': list(e.get('sib-MappingInfo-r13', []))})
    bitmap = None
    if 'downlinkBitmap-r13' in sib:
        kind, bs = sib['downlinkBitmap-r13']
        nb = 10 if kind == 'subframePattern10-r13' else 40
        v, ln = bs
        bitmap = [(v >> (ln - 1 - i)) & 1 for i in range(nb)]
    hs = sib['hyperSFN-MSB-r13']
    return {'entries': entries, 'win': SI_WIN[win.index(sib['si-WindowLength-r13'])],
            'offset': sib.get('si-RadioFrameOffset-r13', 0), 'bitmap': bitmap,
            'hsfn_msb': hs[0] >> (hs[1] - 8) if hs[1] >= 8 else hs[0]}


# ----------------------------------------------------------------------------------------
# Front end: NPSS acquisition / tracking, CFO, CP removal  ->  140 x 128 per frame
# ----------------------------------------------------------------------------------------
class Sync(object):
    MODES = ('per-symbol', 'continuous')

    def __init__(self, cfo_span=12000.0, cfo_step=250.0, acq_frames=4, log=print):
        self.log = log
        self.tpl = {m: (npss_template(240e3, m == 'continuous'), npss_template(FS, m == 'continuous'))
                    for m in self.MODES}
        self.mode = 'per-symbol'
        self.t240, self.t1920 = self.tpl[self.mode]
        self.cfo_hyp = np.arange(-cfo_span, cfo_span + 1, cfo_step)
        self.acq_frames = acq_frames
        # per-symbol segments of the full-rate template (for CFO from phase slope)
        ts = 1.0 / 30.72e6
        b = [int(round((SYM_START[l] - SYM_START[3]))) for l in range(3, 14)] + [len(self.t1920)]
        self.seg = [(b[i], b[i + 1]) for i in range(11)]
        self.seg_t = np.array([(s + e) / 2.0 for s, e in self.seg]) / FS
        self.reset()

    def reset(self):
        self.buf = np.zeros(0, np.complex64)
        self.buf0 = 0                 # absolute index of buf[0]
        self.locked = False
        self.cfo = 0.0
        self.phase = 0.0
        self.next_start = 0
        self.tacc = 0.0
        self.misses = 0
        self.seq = 0
        self.thr = 0.0
        self.last = None              # (mode, cfo) of the previous lock
        self.fails = 0
        self.epoch = 0

    # --- helpers
    def _npss_measure(self, x, nom):
        """x: CFO-corrected samples; nom: nominal NPSS start in x. Returns (metric, delta, cfo_res)."""
        L = len(self.t1920)
        best = (-1, 0)
        cs = {}
        for d in range(-3, 4):
            seg = x[nom + d: nom + d + L]
            if len(seg) < L:
                continue
            c = np.vdot(self.t1920, seg)
            e = np.vdot(seg, seg).real + 1e-20
            cs[d] = abs(c) ** 2 / e
            if cs[d] > best[0]:
                best = (cs[d], d)
        m, d = best
        frac = 0.0
        if d - 1 in cs and d + 1 in cs:
            a, b0, c = cs[d - 1], cs[d], cs[d + 1]
            den = a - 2 * b0 + c
            if den < 0:
                frac = 0.5 * (a - c) / den
        seg = x[nom + d: nom + d + L]
        r = np.array([np.vdot(self.t1920[s:e], seg[s:e]) for s, e in self.seg])
        acc = np.sum(r[1:] * np.conj(r[:-1]))
        dt = np.mean(np.diff(self.seg_t))
        cfo_res = np.angle(acc) / (2 * np.pi * dt)
        return m, d + frac, cfo_res

    def _acquire(self):
        need = self.acq_frames * FRAME_LEN + 2 * len(self.t1920)
        if len(self.buf) < need:
            return False
        x = self.buf[:need]
        y = x[::8]                                  # 240 kS/s (input is already low-passed)
        P = 2400
        # After a first lock, re-acquire cheaply around the last CFO with the known model.
        narrow = self.last is not None and self.fails < 3
        if narrow:
            modes = (self.last[0],)
            span = (self.last[1] - 1500.0, self.last[1] + 1500.0)
        else:
            modes = self.MODES
            span = (self.cfo_hyp[0], self.cfo_hyp[-1])
        best = (0, 0, 0, 0, modes[0])
        L = len(self.t240)
        nfft = 1 << int(np.ceil(np.log2(len(y) + L)))
        df = 240e3 / nfft
        Y = np.fft.fft(y, nfft)
        e = np.convolve(np.abs(y) ** 2, np.ones(L))[L - 1:len(y)] + 1e-20
        step = max(1, int(round(250.0 / df)))
        ks = np.arange(int(np.ceil(span[0] / df)), int(np.floor(span[1] / df)) + 1, step)
        base = np.arange(nfft)
        for mode in modes:
            Tf = np.fft.fft(np.conj(self.tpl[mode][0][::-1]), nfft)
            for c0 in range(0, len(ks), 24):                      # CFO = cyclic bin shift, batched
                kk = ks[c0:c0 + 24]
                C = np.fft.ifft(Y[(base[None, :] + kk[:, None]) % nfft] * Tf[None, :], axis=1)[:, L - 1:len(y)]
                M = np.abs(C) ** 2 / e[None, :]
                nf = M.shape[1] // P
                F = M[:, :nf * P].reshape(len(kk), nf, P).mean(1)
                h, i = np.unravel_index(int(np.argmax(F)), F.shape)
                if F[h, i] > best[0]:
                    best = (F[h, i], i, kk[h] * df, F[h, i] / (np.median(F[h]) + 1e-20), mode)
        pk, i, f, ratio, mode = best
        if ratio < 12 or pk < 0.02:
            self.fails += 1
            self.log('[sync] searching... best NPSS metric %.3f (ratio %.1f) at CFO %+.0f Hz, %s model'
                     % (pk, ratio, f, mode))
            drop = self.acq_frames * FRAME_LEN // 2
            self.buf = self.buf[drop:]
            self.buf0 += drop
            return False
        # fine timing / CFO at full rate around the coarse position (first occurrence)
        self.mode = mode
        self.t240, self.t1920 = self.tpl[mode]
        nom = 8 * i
        nfull = np.arange(len(x))
        xc = x * np.exp(-2j * np.pi * f * nfull / FS)
        best2 = (-1, 0)
        L1 = len(self.t1920)
        for d in range(-12, 13):
            s = nom + d
            if s < 0 or s + L1 > len(xc):
                continue
            c = abs(np.vdot(self.t1920, xc[s:s + L1])) ** 2 / (np.vdot(xc[s:s + L1], xc[s:s + L1]).real + 1e-20)
            if c > best2[0]:
                best2 = (c, s)
        m1, s = best2
        _, _, cfo_res = self._npss_measure(xc, s)
        self.cfo = f + cfo_res
        start = s - NPSS_OFFSET
        while start < 0:
            start += FRAME_LEN
        self.next_start = self.buf0 + start
        self.phase = -2 * np.pi * self.cfo * (self.next_start - self.buf0) / FS
        self.phase_abs = self.next_start
        self.thr = max(0.25 * m1, 0.02)
        self.locked = True
        self.misses = 0
        self.log('[sync] NPSS found: metric %.2f, CFO %+.1f Hz (%+.2f ppm at 935 MHz), half-subcarrier %s, '
                 'frame start at sample %d' % (m1, self.cfo, self.cfo / 935.0, self.mode, self.next_start))
        self.mstat = []
        self.last = (self.mode, self.cfo)
        self.fails = 0
        self.epoch += 1
        self.seq = self.epoch * 1000000            # decoder resets its state when the epoch changes
        return True

    def feed(self, samples):
        """Append samples; return list of (frame (140,128) complex64, info dict)."""
        self.buf = np.concatenate((self.buf, np.asarray(samples, np.complex64)))
        out = []
        while True:
            if not self.locked:
                if not self._acquire():
                    break
                continue
            rel = self.next_start - self.buf0
            need = rel + FRAME_LEN + NFFT
            if len(self.buf) < need or rel < BACKOFF:
                if rel < BACKOFF:          # timing moved before buffer start: skip a frame
                    self.next_start += FRAME_LEN
                    continue
                break
            seg = self.buf[rel - BACKOFF: rel + FRAME_LEN + 8]
            ph = self.phase - 2 * np.pi * self.cfo * (np.arange(len(seg)) - BACKOFF) / FS
            x = seg * np.exp(1j * ph)
            m, delta, cfo_res = self._npss_measure(x, BACKOFF + NPSS_OFFSET)
            idx = (np.arange(140) // 14) * SF_LEN + WIN_START[np.arange(140) % 14]
            frame = np.stack([x[i: i + NFFT] for i in idx])
            if self.mode == 'continuous':
                frame = frame * CONT_DEROT[:, None]
            frame = frame.astype(np.complex64)
            info = {'seq': self.seq, 'cfo': self.cfo, 'npss': m, 'abs': self.next_start}
            self.mstat = (getattr(self, 'mstat', []) + [m])[-500:]
            if self.seq % 500 == 499:
                self.log('[sync] locked: NPSS metric avg %.2f (min %.2f), CFO %+.1f Hz, %s'
                         % (np.mean(self.mstat), np.min(self.mstat), self.cfo, self.mode))
            out.append((frame, info))
            self.seq += 1
            # loops
            step = 0
            if m > self.thr:
                self.misses = 0
                self.cfo += 0.3 * cfo_res
                self.tacc += 0.4 * delta
                step = int(np.round(self.tacc))
                self.tacc -= step
            else:
                self.misses += 1
            adv = FRAME_LEN + step
            self.phase = (self.phase - 2 * np.pi * self.cfo * adv / FS) % (2 * np.pi)
            self.next_start += adv
            if self.misses > 10:
                self.log('[sync] NPSS lost, re-acquiring')
                keep = self.next_start - self.buf0
                self.buf = self.buf[max(keep, 0):]
                self.buf0 += max(keep, 0)
                self.locked = False
                continue
            drop = self.next_start - self.buf0 - 4 * NFFT
            if drop > 0:
                self.buf = self.buf[drop:]
                self.buf0 += drop
        return out


# ----------------------------------------------------------------------------------------
# Decoder: NSSS -> PCI, NPBCH -> MIB-NB, NPDSCH -> SIB1-NB -> SI messages
# ----------------------------------------------------------------------------------------
class Decoder(object):
    def __init__(self, log=print, msg=None, const=None, vec=None, split_db=None):
        """split_db=None: one 'npdsch' constellation (original flowgraph).
        split_db=x: per-subframe SINR analysis; NPDSCH symbols go to 'npdsch_clean' (SINR >= x dB)
        or 'npdsch_dirty', and a 10-value per-subframe SINR vector is sent as vec('sinr', ...)."""
        self.log = log
        self.msg = msg or (lambda s: None)        # human-readable output lines
        self.const = const or (lambda name, z: None)
        self.vec = vec or (lambda name, v: None)
        self.split_db = split_db
        self.sf_sinr = np.full(10, np.nan)
        self.sinr_hist = []
        self.reset()

    def reset(self):
        self.pci = None
        self.cands = []
        self.pci_frames = 0
        self.nsss_acc = {0: None, 1: None}
        self.nsss_cnt = {0: 0, 1: 0}
        self.par = None                 # seq parity of even frames
        self.theta_hist = []
        self.pbch_buf = []              # (seq, y, h0, h1, nvar)
        self.mib = None
        self.sfn_off = None             # nf = (seq + sfn_off) mod 1024
        self.hsfn = None
        self.variant = None
        self.ports = None
        self.sib1_acc = {}
        self.sib1_done_period = None
        self.sib1_text = None
        self.si = None                  # schedule from SIB1
        self.si_acc = {}
        self.si_done = set()
        self.si_seen = {}
        self.last_mib_bits = None
        self.frames = 0

    # ---------------------------------------------------------------- NSSS / PCI
    def _nsss(self, G, seq):
        Y = G[9 * 14 + 3: 9 * 14 + 14]                        # (11, 12)
        B = nsss_bank()
        col = np.einsum('hlk,lk->hk', B, Y)                   # coherent along time per sub-carrier
        met = np.sum(np.abs(col) ** 2, axis=1) / (np.sum(np.abs(Y) ** 2) * 11 + 1e-20)
        p = seq & 1
        self.nsss_acc[p] = met if self.nsss_acc[p] is None else self.nsss_acc[p] + met
        self.nsss_cnt[p] += 1
        if self.nsss_cnt[0] + self.nsss_cnt[1] < 4:
            return
        best = None
        for par in (0, 1):
            a = self.nsss_acc[par]
            if a is None or self.nsss_cnt[par] < 2:
                continue
            pm = a.reshape(504, 4).max(axis=1) / self.nsss_cnt[par]
            floor = np.median(pm) + 1e-20
            order = np.argsort(pm)[::-1]
            score = pm[order[0]] / floor
            if best is None or score > best[0]:
                best = (score, par, pm, floor, order)
        if best is None:
            return
        score, par, pm, floor, order = best
        # Every cell ID clearly above the noise floor is a candidate. Several sectors of one site
        # often share the carrier at similar levels, so "best vs 2nd best" is not a usable test;
        # the NPBCH CRC (scrambled with the cell ID) decides which candidate is real.
        cands = [int(q) for q in order[:3] if pm[q] / floor > self.NSSS_THR and pm[q] >= 0.5 * pm[order[0]]]
        if cands:
            prev = getattr(self, 'prev_pci', None)
            if prev in cands:
                cands.remove(prev)
                cands.insert(0, prev)
            self.cands, self.par = cands, par
            self._use_pci(cands[0])
            others = ', '.join('PCI %d (%.0f%%)' % (q, 100 * pm[q] / pm[cands[0]]) for q in cands[1:])
            self.log('[nsss] candidate cells: PCI %d (%.1fx noise floor)%s'
                     % (cands[0], pm[cands[0]] / floor, ('; co-channel: ' + others) if others else ''))
        elif sum(self.nsss_cnt.values()) % 50 == 0:
            self.log('[nsss] no cell yet (best %.1fx noise floor, need %.1fx)' % (score, self.NSSS_THR))
            for q in (0, 1):                       # exponential forgetting, keeps long integration
                if self.nsss_acc[q] is not None:
                    self.nsss_acc[q] *= 0.5
                    self.nsss_cnt[q] = max(1, self.nsss_cnt[q] // 2)

    NSSS_THR = 2.4           # pure noise peaks at ~1.6x the median
    MIB_TIMEOUT = 96          # frames to wait for a MIB-NB before trying the next candidate cell

    def _use_pci(self, pci):
        self.pci = pci
        self.pci_frames = 0
        self.pbch_buf = []
        if pci != getattr(self, 'prev_pci', None) or not getattr(self, '_announced', False):
            self.msg('Cell found: PCI (N_ID_cell) = %d' % pci
                     + ('   [co-channel candidates: %s]' % ', '.join(map(str, self.cands[1:]))
                        if len(self.cands) > 1 else ''))
            self._announced = True
        self.prev_pci = pci

    # ---------------------------------------------------------------- subframe helpers
    def _nrs_sfs(self, seq):
        """Subframes the UE may assume carry NRS (36.211 10.2.6), for the current stage."""
        odd = (seq & 1) != self.par
        if self.mib is None or self.mib['opmode'].startswith('inband'):
            sfs = [0, 4]
        else:
            sfs = [0, 1, 3, 4]
            if self.si is not None:
                bm = self.si['bitmap']
                nf = (seq + self.sfn_off) % 1024
                a = (self.hsfn or 0) * 1024 + nf
                for sf in (2, 6, 7, 8):
                    if bm is None or bm[(10 * a + sf) % len(bm)]:
                        sfs.append(sf)
        if odd:
            sfs.append(9)
        return sorted(sfs)

    def _frame_chest(self, G, seq):
        sfs = self._nrs_sfs(seq)
        self.H0, self.n0 = chest_frame(G, self.pci, 0, sfs)
        self.H1, self.n1 = chest_frame(G, self.pci, 1, sfs)

    def _sinr_frame(self, G, seq):
        """Per-subframe SINR (dB) of the serving cell: model power / residual power at its NRS.
        Interference that lands on the NRS in one subframe (a co-channel cell's data burst) shows up
        here even though the frame-wide channel model is unaffected."""
        sinr = np.full(10, np.nan)
        tab = nrs_table(self.pci)
        ports = self.ports or 1
        for sf in self._nrs_sfs(seq):
            sig, err = 0.0, 0.0
            for p, H in ((0, self.H0), (1, self.H1))[:ports]:
                for (l, k, v) in tab[(sf, p)]:
                    hm = G[sf * 14 + l, k] * np.conj(v)
                    hp = H[sf * 14 + l, k]
                    sig += abs(hp) ** 2
                    err += abs(hm - hp) ** 2
            sinr[sf] = 10 * np.log10(sig / max(err, 1e-12 * sig) + 1e-12)
        self.sf_sinr = sinr
        if self.split_db is None:
            return
        self.vec('sinr', np.where(np.isnan(sinr), -10.0, sinr))
        self.sinr_hist = (self.sinr_hist + [sinr])[-500:]
        if len(self.sinr_hist) == 500 and seq % 500 == 0:
            A = np.array(self.sinr_hist)
            cells = []
            for sf in range(10):
                col = A[:, sf][~np.isnan(A[:, sf])]
                if len(col) < 20:
                    cells.append('sf%d   --   ' % sf)
                    continue
                cells.append('sf%d %4.1f/%4.1f %2d%%' % (sf, np.median(col), np.percentile(col, 10),
                                                       100 * np.mean(col < self.split_db)))
            self.log('[sinr] last 5 s, per subframe median/10th-pct dB, %% below %.0f dB:  %s'
                     % (self.split_db, '  '.join(cells)))

    def _show_npdsch(self, parts, label):
        """parts: list of (display symbols, subframe SINR dB)."""
        if self.split_db is None:
            self.const('npdsch', np.concatenate([d for d, _ in parts]))
            return
        clean = [d for d, q in parts if q >= self.split_db]
        dirty = [d for d, q in parts if not q >= self.split_db]
        if clean:
            self.const('npdsch_clean', np.concatenate(clean))
        if dirty:
            self.const('npdsch_dirty', np.concatenate(dirty))
        self.log('[sinr] %s: subframe SINR %s dB -> %d clean / %d interfered; QPSK SNR clean %s, interfered %s'
                 % (label, ' '.join('%.0f' % q for _, q in parts), len(clean), len(dirty),
                    '%.1f' % snr_db(np.concatenate(clean)) if clean else '--',
                    '%.1f' % snr_db(np.concatenate(dirty)) if dirty else '--'))

    def _sf_est(self, G, sf, ports):
        Y = G[sf * 14:(sf + 1) * 14]
        H0 = self.H0[sf * 14:(sf + 1) * 14]
        H1 = self.H1[sf * 14:(sf + 1) * 14]
        return Y, H0, H1, (0.5 * (self.n0 + self.n1) if ports == 2 else self.n0)

    # ---------------------------------------------------------------- NPBCH
    def _pbch_collect(self, G, seq):
        re = npbch_re(self.pci)
        ls = np.array([r[0] for r in re])
        ks = np.array([r[1] for r in re])
        Y = G[0:14]
        H0, H1 = self.H0[0:14], self.H1[0:14]
        self.pbch_buf.append((seq, Y[ls, ks], H0[ls, ks], H1[ls, ks], self.n0, self.n1, 0.0))
        self.pbch_buf = self.pbch_buf[-64:]

    def _pbch_llr(self, entry, variant, ports, nf8):
        seq, y, h0, h1, n0, n1, _ = entry
        if variant == 'r14':
            y = y * np.conj(npbch_rotation(self.pci, nf8))
        nv = n0 if ports == 1 else 0.5 * (n0 + n1)
        z, disp = equalize(y, h0, h1, nv, ports)
        return qpsk_llr(z), disp

    def _pbch_try(self, hyps, min_frames=1):
        """hyps: list of (variant, ports, o) with nf mod 64 = (seq + o) mod 64. Returns decoded or None."""
        c = gold(self.pci, 1600)
        sgn = 1.0 - 2.0 * c
        rows, meta, accs = [], [], []
        cache = {}
        last = self.pbch_buf[-1][0]
        for (variant, ports, o) in hyps:
            acc = np.zeros(1600)
            tti = (last + o) // 64
            used = 0
            for e in self.pbch_buf:
                if (e[0] + o) // 64 != tti:
                    continue
                nf64 = (e[0] + o) % 64
                key = (e[0], variant, ports, nf64 % 8 if variant == 'r14' else 0)
                if key not in cache:
                    cache[key] = self._pbch_llr(e, variant, ports, nf64 % 8)[0]
                b = nf64 // 8
                acc[200 * b:200 * b + 200] += cache[key] * sgn[200 * b:200 * b + 200]
                used += 1
            if used == 0:
                continue
            if used < min_frames:
                continue
            rows.append(rm_conv_rx(acc[None, :], 50)[0])
            accs.append(acc)
            meta.append((variant, ports, o, used))
        if not rows:
            return None
        bits = viterbi_tb(np.array(rows))
        rem = crc_remainder(bits[:, :34], CRC16, 16)
        best = None
        for h, (variant, ports, o, used) in enumerate(meta):
            mask = np.zeros(16, np.uint8) if ports == 1 else np.ones(16, np.uint8)
            if np.all((rem[h] ^ mask) == bits[h, 34:]) and not np.any(bits[h, 24:34]):
                m = parse_mib(bits[h, :34])
                if m['sched_sib1'] > 11:
                    continue
                # Several alignments can pass CRC (Rel-13 repeats each block for 8 frames).
                # Keep the one whose re-encoded MIB agrees best with the combined soft bits.
                cw = conv_encode(bits[h])
                e = cw[rm_conv_map(50, 1600)]
                score = float(np.sum(accs[h] * (1.0 - 2.0 * e)))
                if best is None or score > best[0]:
                    best = (score, (variant, ports, o, used, bits[h, :34], m))
        return best[1] if best else None

    def _pbch(self, seq):
        if self.mib is None and len(self.pbch_buf) and seq % 8 == 0:
            # show equalised NPBCH while searching (QPSK even with Rel-14 rotation); guess the
            # port count from the power seen on the port-1 NRS positions
            p_guess = 2 if np.mean(np.abs(self.H1) ** 2) > 0.25 * np.mean(np.abs(self.H0) ** 2) else 1
            _, disp = self._pbch_llr(self.pbch_buf[-1], 'r13', p_guess, 0)
            self.const('npbch', disp)
            if seq % 200 == 0:
                self.log('[npbch] searching for MIB-NB: frames %d, NPBCH SNR %.1f dB per frame '
                         '(about -20 = noise, > 0 = clean QPSK)' % (len(self.pbch_buf), snr_db(disp)))
        if len(self.pbch_buf) < 8:
            return
        if self.mib is None:
            if (seq + 1) % 8:
                return
            hyps = [(v, p, o) for v in (('r13', 'r14') if self.variant is None else (self.variant,))
                    for p in (1, 2) for o in range(64) if (o + self.par) % 2 == 0]
        else:
            # verify once per TTI using the known alignment
            if (seq + self.o + 1) % 64:
                return
            hyps = [(self.variant, self.ports, self.o)]
        # first acquisition: >= 9 frames so the combined set always straddles a block boundary
        r = self._pbch_try(hyps, min_frames=9 if self.mib is None else 1)
        if r is None:
            if self.mib is not None:
                self.mib_fail = getattr(self, 'mib_fail', 0) + 1
                if self.mib_fail >= 4:
                    self.log('[npbch] MIB-NB lost; restarting frame-number search')
                    self.mib = None
            return
        variant, ports, o, used, bits, m = r
        self.mib_fail = 0
        nf64 = (seq + o) % 64
        sfn = (m['sfn_msb'] << 6) | nf64
        self.sfn_off = (sfn - seq) % 1024
        first = self.mib is None
        self.variant, self.ports, self.o, self.mib = variant, ports, o, m
        self._sib1set = set(sib1_frames(self.pci, m['sched_sib1']).keys())
        hl = m['hsfn_lsb']
        if self.hsfn is None or (self.hsfn & 3) != hl:
            self.hsfn = (self.hsfn & ~3 | hl) if self.hsfn is not None else hl
        # display: last frame's equalised NPBCH symbols
        _, disp = self._pbch_llr(self.pbch_buf[-1], variant, ports, nf64 % 8)
        self.const('npbch', disp)
        self.pbch_snr = float(np.median([snr_db(self._pbch_llr(e, variant, ports, (e[0] + o) % 8)[1])
                                         for e in self.pbch_buf[-8:]]))
        key = tuple(int(x) for x in bits[6:34])
        if key != self.last_mib_bits:
            self.last_mib_bits = key
            sib1rep = SIB1_REP[m['sched_sib1']]
            txt = ['MIB-NB  (%s, %d NRS port%s, combined %d frames, NPBCH SNR %.1f dB)' % (
                       'Rel-14 rotation' if variant == 'r14' else 'Rel-13', ports, 's' if ports > 1 else '', used,
                       self.pbch_snr),
                   '  SFN = %d   (systemFrameNumber-MSB %d, H-SFN LSBs %d)' % (sfn, m['sfn_msb'], m['hsfn_lsb']),
                   '  schedulingInfoSIB1 = %d  ->  SIB1-NB TBS %d bits, %d repetitions, start frame %d'
                   % (m['sched_sib1'], SIB1_TBS[m['sched_sib1']], sib1rep, sib1_start_frame(self.pci, sib1rep)),
                   '  systemInfoValueTag = %d   ab-Enabled = %s   additionalTransmissionSIB1 = %s'
                   % (m['value_tag'], bool(m['ab_enabled']), bool(m['add_tx_sib1'])),
                   '  operationModeInfo = %s %s' % (m['opmode'], {k: v for k, v in m.items()
                                                    if k in ('eutra_crs_seq', 'eutra_num_crs_ports', 'raster_offset')})]
            self.msg('\n'.join(txt))
            self.log('[npbch] ' + ' | '.join(t.strip() for t in txt))

    # ---------------------------------------------------------------- NPDSCH (BCCH)
    def _pdsch_soft(self, G, sf, nf):
        """Soft bits of one NPDSCH subframe (BCCH). Returns (llr, disp)."""
        l0 = 3 if self.mib['opmode'].startswith('inband') else 0
        re = npdsch_re(self.pci, self.ports, l0)
        ls = np.array([r[0] for r in re])
        ks = np.array([r[1] for r in re])
        Y, H0, H1, nv = self._sf_est(G, sf, self.ports)
        z, disp = equalize(Y[ls, ks], H0[ls, ks], H1[ls, ks], nv, self.ports)
        return qpsk_llr(z), disp

    def _descramble_try(self, parts, tbs):
        """parts: list of (llr_concat, first_nf, first_sf). Combine with each scrambling variant."""
        order = [self.variant, 'r13' if self.variant == 'r14' else 'r14']
        for v in order:
            acc = None
            for llr, nf, sf in parts:
                c = gold(bcch_cinit(v, self.pci, nf, sf), len(llr))
                x = llr * (1.0 - 2.0 * c)
                acc = x if acc is None else acc + x
            ok, bits = tbcc_decode(acc[None, :], tbs)
            if ok:
                return v, bits
        return None, None

    def _sib1(self, G, nf):
        if self.mib['opmode'].startswith('inband'):
            if not getattr(self, '_inband_note', False):
                self._inband_note = True
                self.msg('In-band cell: this decoder stops at MIB-NB (SIB decoding needs the LTE CRS '
                         'puncturing, not implemented). Standalone and guard-band cells are fully decoded.')
            return
        sched = self.mib['sched_sib1']
        tab = sib1_frames(self.pci, sched)
        key = nf % 256
        if key not in tab:
            return
        r, j = tab[key]
        period = ((self.hsfn or 0) * 1024 + nf - key) // 256
        llr, disp = self._pdsch_soft(G, 4, nf)
        ent = self.sib1_acc.setdefault((period, r), {'parts': {}, 'nf0': nf - 2 * j})
        ent['parts'][j] = (llr, disp, self.sf_sinr[4])
        if len(ent['parts']) < 8:
            return
        if self.sib1_done_period == period:
            return
        # all repetitions of this period that are complete
        parts = []
        for (pp, rr), e in sorted(self.sib1_acc.items()):
            if period - 4 < pp <= period and len(e['parts']) == 8 and pp != self.sib1_done_period:
                parts.append((np.concatenate([e['parts'][i][0] for i in range(8)]), e['nf0'] % 1024, 4))
        self._show_npdsch([(ent['parts'][i][1], ent['parts'][i][2]) for i in range(8)],
                          'SIB1-NB repetition @SFN %d' % (ent['nf0'] % 1024))
        v, bits = self._descramble_try(parts, SIB1_TBS[sched])
        self.sib1_acc = {k: e for k, e in self.sib1_acc.items() if k[0] > period - 4}
        if v is None:
            self.log('[sib1] period %d: CRC failed after %d repetition(s)' % (period, len(parts)))
            return
        self.sib1_done_period = period
        self.sib1_acc = {k: e for k, e in self.sib1_acc.items() if k[0] > period}
        pb = bits_to_bytes(bits)
        text, val = decode_bcch(pb)
        hdr = 'SIB1-NB  (%d repetition%s combined, %s scrambling)  payload %s' % (
            len(parts), 's' if len(parts) > 1 else '', v, pb.hex())
        if text is None:
            if pb != getattr(self, 'sib1_hex', None):
                self.sib1_hex = pb
                self.msg(hdr + '\n  (install pycrate to decode the ASN.1 and the SI messages:  '
                         'radioconda\\python.exe -m pip install pycrate)')
            return
        if text != self.sib1_text:
            self.sib1_text = text
            self.msg(hdr + '\n' + text)
        if val is not None:
            try:
                sch = sib1_schedule(val)
                old = self.hsfn
                self.hsfn = (sch['hsfn_msb'] << 2) | (self.mib['hsfn_lsb'])
                if self.si is None or sch != self.si:
                    self.si = sch
                    self.log('[sib1] SI schedule: %s' % sch)
            except Exception as e:
                self.log('[sib1] could not read SI schedule: %s' % e)

    # ---------------------------------------------------------------- SI messages
    def _valid_sf(self, a, sf):
        """a = absolute frame count (H-SFN*1024 + SFN)."""
        nf = a % 1024
        if sf == 0 or sf == 5 or (sf == 9 and nf % 2 == 0):
            return False
        if sf in (3, 4) and (nf % 256) in self._sib1set:
            if sf == 4 or self.mib.get('add_tx_sib1'):       # Rel-15 additional SIB1 copies use subframe 3
                return False
        bm = self.si['bitmap'] if self.si else None
        if bm is not None:
            return bool(bm[(10 * a + sf) % len(bm)])
        return True

    def _si_map(self, a):
        """{sf: (entry n, window start w0, tx j, index in codeword)} for absolute frame a."""
        res = {}
        for n, e in enumerate(self.si['entries']):
            nsf = 2 if e['tbs'] <= 120 else 8
            base = n * self.si['win'] + self.si['offset']      # floor(x/10) + offset, x = (n-1)*w
            T, rep, win = e['T'], e['rep'], self.si['win']
            w0 = a - ((a - base) % T)
            if a - w0 >= win + 4:
                continue
            for s in range(max(w0, a - 4), a + 1):           # a codeword spans <= 4 frames
                if (s - w0) % rep or s >= w0 + win:
                    continue
                cnt, aa, sf = 0, s, 0
                while cnt < nsf and aa <= a:
                    if self._valid_sf(aa, sf):
                        if aa == a:
                            res[sf] = (n, w0, (s - w0) // rep, cnt)
                        cnt += 1
                    sf += 1
                    if sf == 10:
                        sf, aa = 0, aa + 1
        return res

    def _si(self, G, nf):
        if self.si is None or self.hsfn is None:
            return
        a = self.hsfn * 1024 + nf
        for sf, (n, w0, j, idx) in sorted(self._si_map(a).items()):
            e = self.si['entries'][n]
            if (n, w0) in self.si_done:
                continue
            llr, disp = self._pdsch_soft(G, sf, nf)
            ent = self.si_acc.setdefault((n, w0, j), {'parts': {}, 'start': None})
            if idx == 0:
                ent['start'] = (nf, sf)
            ent['parts'][idx] = (llr, disp, self.sf_sinr[sf])
            nsf = 2 if e['tbs'] <= 120 else 8
            if len(ent['parts']) == nsf and ent['start'] is not None:
                parts = []
                for (nn, ww, jj), ee in sorted(self.si_acc.items()):
                    if nn == n and ww == w0 and len(ee['parts']) == nsf and ee['start'] is not None:
                        parts.append((np.concatenate([ee['parts'][i][0] for i in range(nsf)]),
                                      ee['start'][0], ee['start'][1]))
                self._show_npdsch([(ent['parts'][i][1], ent['parts'][i][2]) for i in range(nsf)],
                                  'SI-message %d @SFN %d' % (n + 1, ent['start'][0]))
                v, bits = self._descramble_try(parts, e['tbs'])
                if v is None:
                    if len(parts) in (1, 4, 16):
                        self.log('[si] SI-message %d (window @%d): CRC failed, %d transmission(s) combined'
                                 % (n + 1, w0, len(parts)))
                    continue
                self.si_done.add((n, w0))
                self.si_acc = {k: x for k, x in self.si_acc.items() if not (k[0] == n and k[1] == w0)}
                pb = bits_to_bytes(bits)
                if self.si_seen.get(n) == pb:
                    continue
                self.si_seen[n] = pb
                text, _ = decode_bcch(pb)
                self.msg('SI-message %d %s (%d transmission%s combined)  payload %s\n%s' % (
                    n + 1, e['sibs'] if e['sibs'] else '[SIB2-NB]', len(parts), 's' if len(parts) > 1 else '',
                    pb.hex(), text or ''))
        # forget stale accumulations
        self.si_acc = {k: x for k, x in self.si_acc.items() if a - k[1] < self.si['win'] + 4}

    # ---------------------------------------------------------------- main entry
    def feed_frame(self, G, seq):
        """G: (140, 12) resource grid of one frame (subframe 0 first); seq: frame counter from Sync
        (epoch * 1e6 + frame index; a new epoch = new frame grid after re-acquisition)."""
        ep = seq // 1000000
        if ep != getattr(self, 'epoch', ep):
            self.log('[decoder] sync re-acquired: restarting cell search')
            keep = (self.si_seen, self.sib1_text, self.last_mib_bits, self.pci or getattr(self, 'prev_pci', None),
                    getattr(self, 'sib1_hex', None))
            self.reset()
            self.si_seen, self.sib1_text, self.last_mib_bits, self.prev_pci, self.sib1_hex = keep
            self._announced = True
        self.epoch = ep
        self.frames += 1
        if self.pci is None:
            self._nsss(G, seq)
            return
        self.pci_frames += 1
        if self.mib is None and self.pci_frames > self.MIB_TIMEOUT:
            i = self.cands.index(self.pci) if self.pci in self.cands else -1
            if i + 1 < len(self.cands):
                self.log('[npbch] no MIB-NB with PCI %d; trying co-channel PCI %d' % (self.pci, self.cands[i + 1]))
                self._use_pci(self.cands[i + 1])
            else:
                self.log('[npbch] no MIB-NB with any candidate; restarting cell search')
                keep = (self.si_seen, self.sib1_text, self.last_mib_bits, getattr(self, 'sib1_hex', None))
                self.reset()
                self.si_seen, self.sib1_text, self.last_mib_bits, self.sib1_hex = keep
                return
        self._frame_chest(G, seq)
        self._sinr_frame(G, seq)
        self._pbch_collect(G, seq)
        self._pbch(seq)
        if self.mib is None or self.sfn_off is None:
            return
        nf = (seq + self.sfn_off) % 1024
        if nf == 0 and self.hsfn is not None and self.frames > 1:
            self.hsfn = (self.hsfn + 1) % 1024
        self._sib1(G, nf)
        self._si(G, nf)
