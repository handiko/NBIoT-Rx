"""
NB-IoT front end -- Embedded Python Block (GNU Radio 3.10)

Input : complex @ 1.92 MS/s, NB-IoT anchor carrier centred at 0 Hz
        (output of the stock Frequency Xlating FFT Filter).
Output: complex vectors of 128 = one OFDM symbol with its cyclic prefix removed and the
        carrier-frequency offset corrected; 140 vectors per 10 ms radio frame. The first
        vector of every frame carries the tag 'frame' (value = frame counter).

Why an EPB: NPSS acquisition over a +-7 kHz CFO search, per-frame NPSS tracking of timing
and frequency (RTL clock drift), and stripping the alternating 10/9-sample cyclic prefix on
a frame grid have no stock equivalents (Keep M in N needs a constant period, and no stock
block re-times on a correlation peak). The FFT and sub-carrier selection that follow are
stock blocks. All maths lives in the Python Module `nbiot` (passed in as `lib`).
"""
import numpy as np
import pmt
from gnuradio import gr


class blk(gr.basic_block):
    def __init__(self, lib=None, verbose=True):
        gr.basic_block.__init__(self, name='NB-IoT Sync (NPSS, CFO, CP removal)',
                                in_sig=[np.complex64], out_sig=[(np.complex64, 128)])
        self.lib = lib
        self.verbose = verbose
        self.set_tag_propagation_policy(gr.TPP_DONT)
        self.sync = lib.Sync(log=self._log) if lib is not None else None
        self.pending = []          # [(frame (140, 128), info)]
        self.pos = 0               # vectors of pending[0] already written

    def _log(self, s):
        if self.verbose:
            print(s, flush=True)

    def forecast(self, noutput_items, ninputs):
        return [1] * ninputs

    def general_work(self, input_items, output_items):
        inp = input_items[0]
        out = output_items[0]
        if self.sync is None:
            self.consume(0, len(inp))
            return 0
        backlog = sum(len(f) for f, _ in self.pending) - self.pos
        if backlog < 280:                       # take input only while the backlog is small
            self.pending += self.sync.feed(inp.copy())
            self.consume(0, len(inp))
            lag = len(self.sync.buf) / 1.92e6
            if lag > 1.0 and self.nitems_read(0) > getattr(self, '_warn_at', 0):
                self._warn_at = self.nitems_read(0) + int(10 * 1.92e6)
                self._log('[sync] WARNING: %.1f s of samples queued - CPU is not keeping up' % lag)
        n = 0
        while self.pending and n < len(out):
            fr, info = self.pending[0]
            if self.pos == 0:
                self.add_item_tag(0, self.nitems_written(0) + n, pmt.intern('frame'),
                                  pmt.from_long(int(info['seq'])))
            k = min(len(fr) - self.pos, len(out) - n)
            out[n:n + k] = fr[self.pos:self.pos + k]
            n += k
            self.pos += k
            if self.pos == len(fr):
                self.pending.pop(0)
                self.pos = 0
        return n
