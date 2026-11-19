"""
NB-IoT broadcast decoder with interference (SINR) analysis -- Embedded Python Block (GNU Radio 3.10)

Input : complex vectors of 12 = the 12 NB-IoT sub-carriers of one OFDM symbol
        (stock FFT -> Keep M in N -> Stream to Vector), frames marked by the 'frame' tag.
Output message ports:
  'text'   : pmt string per decoded item (cell ID, MIB-NB, SIB1-NB, SI messages) -> Message Debug
  'npbch'  : PDU of equalised NPBCH symbols      -> QT GUI Constellation Sink (msg_complex)
  'npdsch_clean' : PDU of SIB/SI NPDSCH symbols from subframes with SINR >= split_db
  'npdsch_dirty' : the same from subframes with SINR <  split_db (co-channel bursts)
  'sinr'   : PDU f32vector of 10 values per frame = per-subframe SINR (dB) -> QT Time Raster (msg_float)
             (-10 = subframe without serving-cell NRS: NPSS, NSSS, or not yet known to carry NRS)

Why an EPB: NSSS cell-ID search (504 x 4 hypotheses), NRS channel estimation, transmit-
diversity combining, Rel-13/14 NPBCH blind frame alignment, tail-biting Viterbi with
CRC-arbitrated hypotheses, SIB1-NB/SI scheduling and ASN.1 decoding have no stock blocks.
All maths lives in the Python Module `nbiot` (passed in as `lib`).
"""
import numpy as np
import pmt
from gnuradio import gr


class blk(gr.sync_block):
    def __init__(self, lib=None, verbose=True, split_db=8.0):
        gr.sync_block.__init__(self, name='NB-IoT Decoder + SINR (cell ID, MIB-NB, SIBs)',
                               in_sig=[(np.complex64, 12)], out_sig=None)
        for p in ('text', 'npbch', 'npdsch_clean', 'npdsch_dirty', 'sinr'):
            self.message_port_register_out(pmt.intern(p))
        self.verbose = verbose
        self.dec = lib.Decoder(log=self._log, msg=self._msg, const=self._const, vec=self._vec,
                               split_db=float(split_db)) if lib is not None else None
        self.grid = None
        self.n = 0
        self.seq = 0

    def _log(self, s):
        if self.verbose:
            print(s, flush=True)

    def _msg(self, s):
        self.message_port_pub(pmt.intern('text'), pmt.intern(s))

    def _const(self, name, z):
        z = np.asarray(z, np.complex64)
        z = z / (np.sqrt(np.mean(np.abs(z) ** 2)) + 1e-12)
        self.message_port_pub(pmt.intern(name), pmt.cons(pmt.PMT_NIL, pmt.init_c32vector(len(z), z)))

    def _vec(self, name, v):
        v = np.asarray(v, np.float32)
        self.message_port_pub(pmt.intern(name), pmt.cons(pmt.PMT_NIL, pmt.init_f32vector(len(v), v)))

    def work(self, input_items, output_items):
        inp = input_items[0]
        if self.dec is None:
            return len(inp)
        base = self.nitems_read(0)
        starts = sorted((t.offset - base, pmt.to_long(t.value))
                        for t in self.get_tags_in_window(0, 0, len(inp), pmt.intern('frame')))
        bounds = [s for s, _ in starts] + [len(inp)]
        i = 0
        if starts and starts[0][0] > 0:
            self._fill(inp[:starts[0][0]])
        for j, (s, seq) in enumerate(starts):
            self.grid = np.zeros((140, 12), complex)
            self.n = 0
            self.seq = seq
            self._fill(inp[s:bounds[j + 1]])
        if not starts:
            self._fill(inp)
        return len(inp)

    def _fill(self, v):
        if self.grid is None or len(v) == 0:
            return
        k = min(140 - self.n, len(v))
        self.grid[self.n:self.n + k] = v[:k]
        self.n += k
        if self.n == 140:
            try:
                self.dec.feed_frame(self.grid, self.seq)
            except Exception as e:                      # never let one bad frame stop the flowgraph
                print('[decoder] frame %d: %s' % (self.seq, e), flush=True)
            self.grid = None
