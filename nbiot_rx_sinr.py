#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: NB-IoT downlink decoder + SINR analysis
# Description: NB-IoT downlink broadcast decoder with per-subframe SINR / co-channel interference analysis (standalone / guard-band anchor): NPSS sync, cell ID, MIB-NB, SIB1-NB and SI messages. RTL-SDR, stock blocks + 2 EPBs + Python Module.
# GNU Radio version: 3.10.10.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import analog
from gnuradio import blocks
from gnuradio import blocks, gr
from gnuradio import eng_notation
from gnuradio import fft
from gnuradio.fft import window
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import gr, pdu
from gnuradio import soapy
import nbiot_rx_sinr_dec as dec  # embedded python block
import nbiot_rx_sinr_nbiot as nbiot  # embedded python module
import nbiot_rx_sinr_sync as sync  # embedded python block
import sip



class nbiot_rx_sinr(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "NB-IoT downlink decoder + SINR analysis", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("NB-IoT downlink decoder + SINR analysis")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("GNU Radio", "nbiot_rx_sinr")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)

        ##################################################
        # Variables
        ##################################################
        self.samp_rate = samp_rate = 1.92e6
        self.npss_taps = npss_taps = nbiot.npss_matched_taps(samp_rate / 8)
        self.if_offset = if_offset = 300e3
        self.f_nb = f_nb = 935.4e6
        self.split_db = split_db = 8
        self.ppm = ppm = 34
        self.npss_len = npss_len = len(npss_taps)
        self.lo_freq = lo_freq = f_nb + if_offset
        self.half_sc = half_sc = [complex(x) for x in nbiot.SHIFT_VEC]
        self.gain = gain = 30
        self.chan_taps = chan_taps = firdes.low_pass(1.0, samp_rate, 100e3, 30e3, window.WIN_HAMMING)
        self.cfo_hz = cfo_hz = 0

        ##################################################
        # Blocks
        ##################################################

        self._split_db_range = qtgui.Range(0, 20, 1, 8, 150)
        self._split_db_win = qtgui.RangeWidget(self._split_db_range, self.set_split_db, "Clean/interfered split [dB]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._split_db_win, 4, 3, 1, 1)
        for r in range(4, 5):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(3, 4):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._ppm_tool_bar = Qt.QToolBar(self)
        self._ppm_tool_bar.addWidget(Qt.QLabel("RTL ppm" + ": "))
        self._ppm_line_edit = Qt.QLineEdit(str(self.ppm))
        self._ppm_tool_bar.addWidget(self._ppm_line_edit)
        self._ppm_line_edit.editingFinished.connect(
            lambda: self.set_ppm(eng_notation.str_to_num(str(self._ppm_line_edit.text()))))
        self.top_grid_layout.addWidget(self._ppm_tool_bar, 0, 1, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._gain_range = qtgui.Range(0, 49.6, 0.1, 30, 200)
        self._gain_win = qtgui.RangeWidget(self._gain_range, self.set_gain, "RTL gain [dB]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._gain_win, 0, 2, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(2, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._f_nb_tool_bar = Qt.QToolBar(self)
        self._f_nb_tool_bar.addWidget(Qt.QLabel("NB-IoT carrier [Hz]" + ": "))
        self._f_nb_line_edit = Qt.QLineEdit(str(self.f_nb))
        self._f_nb_tool_bar.addWidget(self._f_nb_line_edit)
        self._f_nb_line_edit.editingFinished.connect(
            lambda: self.set_f_nb(eng_notation.str_to_num(str(self._f_nb_line_edit.text()))))
        self.top_grid_layout.addWidget(self._f_nb_tool_bar, 0, 0, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self._cfo_hz_range = qtgui.Range(-5000, 5000, 50, 0, 200)
        self._cfo_hz_win = qtgui.RangeWidget(self._cfo_hz_range, self.set_cfo_hz, "Fine tune [Hz]", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_grid_layout.addWidget(self._cfo_hz_win, 0, 3, 1, 1)
        for r in range(0, 1):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(3, 4):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.agc = analog.agc2_cc((1e-5), (1e-5), 1.0, 1.0, 1e6)
        self.agc.set_min_output_buffer(2097152)
        self.v2s = blocks.vector_to_stream(gr.sizeof_gr_complex*1, 128)
        self.sync = sync.blk(lib=nbiot, verbose=True)
        self.sync.set_min_output_buffer(2048)
        self.sras = qtgui.time_raster_sink_f(
            1000,
            300,
            10,
            [],
            [],
            "Serving-cell SINR per subframe (dB): 10 columns = subframes 0..9, 1 row = 1 frame, 3 s per screen",
            1,
            None
        )

        self.sras.set_update_time(0.10)
        self.sras.set_intensity_range((-5), 25)
        self.sras.enable_grid(False)
        self.sras.enable_axis_labels(True)
        self.sras.set_x_label("subframe")
        self.sras.set_x_range(0.0, 10.0)
        self.sras.set_y_label("frames")
        self.sras.set_y_range(0.0, 300.0)

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        colors = [2, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.sras.set_line_label(i, "Data {0}".format(i))
            else:
                self.sras.set_line_label(i, labels[i])
            self.sras.set_color_map(i, colors[i])
            self.sras.set_line_alpha(i, alphas[i])

        self._sras_win = sip.wrapinstance(self.sras.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._sras_win, 4, 0, 1, 3)
        for r in range(4, 5):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.spec = qtgui.freq_sink_c(
            4096, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            f_nb, #fc
            samp_rate, #bw
            "Channel (NB-IoT carrier at centre)", #name
            1,
            None # parent
        )
        self.spec.set_update_time(0.10)
        self.spec.set_y_axis((-120), 0)
        self.spec.set_y_label('Relative Gain', 'dB')
        self.spec.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.spec.enable_autoscale(False)
        self.spec.enable_grid(True)
        self.spec.set_fft_average(0.1)
        self.spec.enable_axis_labels(True)
        self.spec.enable_control_panel(False)
        self.spec.set_fft_window_normalized(False)

        self.spec.disable_legend()


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.spec.set_line_label(i, "Data {0}".format(i))
            else:
                self.spec.set_line_label(i, labels[i])
            self.spec.set_line_width(i, widths[i])
            self.spec.set_line_color(i, colors[i])
            self.spec.set_line_alpha(i, alphas[i])

        self._spec_win = sip.wrapinstance(self.spec.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._spec_win, 1, 0, 1, 2)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 2):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.s2v = blocks.stream_to_vector(gr.sizeof_gr_complex*1, 12)
        self.s2v.set_min_output_buffer(65536)
        self.s2str = pdu.pdu_to_tagged_stream(gr.types.float_t, 'packet_len')
        self.rtl = None
        dev = 'driver=rtlsdr'
        stream_args = 'bufflen=16384'
        tune_args = ['']
        settings = ['']

        def _set_rtl_gain_mode(channel, agc):
            self.rtl.set_gain_mode(channel, agc)
            if not agc:
                  self.rtl.set_gain(channel, self._rtl_gain_value)
        self.set_rtl_gain_mode = _set_rtl_gain_mode

        def _set_rtl_gain(channel, name, gain):
            self._rtl_gain_value = gain
            if not self.rtl.get_gain_mode(channel):
                self.rtl.set_gain(channel, gain)
        self.set_rtl_gain = _set_rtl_gain

        def _set_rtl_bias(bias):
            if 'biastee' in self._rtl_setting_keys:
                self.rtl.write_setting('biastee', bias)
        self.set_rtl_bias = _set_rtl_bias

        self.rtl = soapy.source(dev, "fc32", 1, "",
                                  stream_args, tune_args, settings)

        self._rtl_setting_keys = [a.key for a in self.rtl.get_setting_info()]

        self.rtl.set_sample_rate(0, samp_rate)
        self.rtl.set_frequency(0, lo_freq)
        self.rtl.set_frequency_correction(0, ppm)
        self.set_rtl_bias(bool(False))
        self._rtl_gain_value = gain
        self.set_rtl_gain_mode(0, bool(False))
        self.set_rtl_gain(0, 'TUNER', gain)
        self.ofdm_fft = fft.fft_vcc(128, True, window.rectangular(128), True, 1)
        self.nras = qtgui.time_raster_sink_f(
            (samp_rate / 8),
            200,
            2400,
            [],
            [],
            "NPSS correlation, 10 ms per row (vertical line = sync)",
            1,
            None
        )

        self.nras.set_update_time(0.10)
        self.nras.set_intensity_range(0, 0.5)
        self.nras.enable_grid(False)
        self.nras.enable_axis_labels(True)
        self.nras.set_x_label("")
        self.nras.set_x_range(0.0, 10.0)
        self.nras.set_y_label("")
        self.nras.set_y_range(0.0, 2.0)

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        colors = [2, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.nras.set_line_label(i, "Data {0}".format(i))
            else:
                self.nras.set_line_label(i, labels[i])
            self.nras.set_color_map(i, colors[i])
            self.nras.set_line_alpha(i, alphas[i])

        self._nras_win = sip.wrapinstance(self.nras.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._nras_win, 1, 2, 1, 2)
        for r in range(1, 2):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(2, 4):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.npow = blocks.complex_to_mag_squared(1)
        self.nnorm = blocks.divide_ff(1)
        self.nmag = blocks.complex_to_mag_squared(1)
        self.neps = blocks.add_const_ff((1e-12))
        self.nen = blocks.moving_average_ff(npss_len, 1, 4000, 1)
        self.ncorr = filter.fft_filter_ccc(1, npss_taps, 1)
        self.ncorr.declare_sample_delay(0)
        self.mdbg = blocks.message_debug(True, gr.log_levels.info)
        self.keep12 = blocks.keep_m_in_n(gr.sizeof_gr_complex, 12, 128, 59)
        self.k8 = blocks.keep_one_in_n(gr.sizeof_gr_complex*1, 8)
        self.halfsc = blocks.multiply_const_vcc(half_sc)
        self.gras = qtgui.time_raster_sink_f(
            (12 * 14000),
            280,
            12,
            [],
            [],
            "Resource grid: 12 sub-carriers x 280 symbols (2 frames)",
            1,
            None
        )

        self.gras.set_update_time(0.10)
        self.gras.set_intensity_range(15, 45)
        self.gras.enable_grid(False)
        self.gras.enable_axis_labels(True)
        self.gras.set_x_label("sub-carrier")
        self.gras.set_x_range(0.0, 12.0)
        self.gras.set_y_label("")
        self.gras.set_y_range(0.0, 20.0)

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        colors = [2, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.gras.set_line_label(i, "Data {0}".format(i))
            else:
                self.gras.set_line_label(i, labels[i])
            self.gras.set_color_map(i, colors[i])
            self.gras.set_line_alpha(i, alphas[i])

        self._gras_win = sip.wrapinstance(self.gras.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._gras_win, 2, 0, 2, 1)
        for r in range(2, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(0, 1):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.gmag = blocks.complex_to_mag_squared(1)
        self.gdb = blocks.nlog10_ff(10, 1, 0)
        self.dec = dec.blk(lib=nbiot, verbose=True, split_db=split_db)
        self.chan = filter.freq_xlating_fft_filter_ccc(1, chan_taps, (-if_offset + cfo_hz), samp_rate)
        self.chan.set_nthreads(1)
        self.chan.declare_sample_delay(0)
        self.cdirty = qtgui.const_sink_c(
            1216, #size
            "NPDSCH: INTERFERED subframes (SINR < split)", #name
            0, #number of inputs
            None # parent
        )
        self.cdirty.set_update_time(0.10)
        self.cdirty.set_y_axis((-2), 2)
        self.cdirty.set_x_axis((-2), 2)
        self.cdirty.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.cdirty.enable_autoscale(False)
        self.cdirty.enable_grid(True)
        self.cdirty.enable_axis_labels(True)

        self.cdirty.disable_legend()

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.cdirty.set_line_label(i, "Data {0}".format(i))
            else:
                self.cdirty.set_line_label(i, labels[i])
            self.cdirty.set_line_width(i, widths[i])
            self.cdirty.set_line_color(i, colors[i])
            self.cdirty.set_line_style(i, styles[i])
            self.cdirty.set_line_marker(i, markers[i])
            self.cdirty.set_line_alpha(i, alphas[i])

        self._cdirty_win = sip.wrapinstance(self.cdirty.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._cdirty_win, 2, 3, 2, 1)
        for r in range(2, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(3, 4):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.cclean = qtgui.const_sink_c(
            1216, #size
            "NPDSCH: CLEAN subframes (SINR >= split)", #name
            0, #number of inputs
            None # parent
        )
        self.cclean.set_update_time(0.10)
        self.cclean.set_y_axis((-2), 2)
        self.cclean.set_x_axis((-2), 2)
        self.cclean.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.cclean.enable_autoscale(False)
        self.cclean.enable_grid(True)
        self.cclean.enable_axis_labels(True)

        self.cclean.disable_legend()

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.cclean.set_line_label(i, "Data {0}".format(i))
            else:
                self.cclean.set_line_label(i, labels[i])
            self.cclean.set_line_width(i, widths[i])
            self.cclean.set_line_color(i, colors[i])
            self.cclean.set_line_style(i, styles[i])
            self.cclean.set_line_marker(i, markers[i])
            self.cclean.set_line_alpha(i, alphas[i])

        self._cclean_win = sip.wrapinstance(self.cclean.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._cclean_win, 2, 2, 2, 1)
        for r in range(2, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(2, 3):
            self.top_grid_layout.setColumnStretch(c, 1)
        self.cbch = qtgui.const_sink_c(
            1216, #size
            "NPBCH (MIB-NB), equalised", #name
            0, #number of inputs
            None # parent
        )
        self.cbch.set_update_time(0.10)
        self.cbch.set_y_axis((-2), 2)
        self.cbch.set_x_axis((-2), 2)
        self.cbch.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.cbch.enable_autoscale(False)
        self.cbch.enable_grid(True)
        self.cbch.enable_axis_labels(True)

        self.cbch.disable_legend()

        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.cbch.set_line_label(i, "Data {0}".format(i))
            else:
                self.cbch.set_line_label(i, labels[i])
            self.cbch.set_line_width(i, widths[i])
            self.cbch.set_line_color(i, colors[i])
            self.cbch.set_line_style(i, styles[i])
            self.cbch.set_line_marker(i, markers[i])
            self.cbch.set_line_alpha(i, alphas[i])

        self._cbch_win = sip.wrapinstance(self.cbch.qwidget(), Qt.QWidget)
        self.top_grid_layout.addWidget(self._cbch_win, 2, 1, 2, 1)
        for r in range(2, 4):
            self.top_grid_layout.setRowStretch(r, 1)
        for c in range(1, 2):
            self.top_grid_layout.setColumnStretch(c, 1)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.dec, 'npbch'), (self.cbch, 'in'))
        self.msg_connect((self.dec, 'npdsch_clean'), (self.cclean, 'in'))
        self.msg_connect((self.dec, 'npdsch_dirty'), (self.cdirty, 'in'))
        self.msg_connect((self.dec, 'text'), (self.mdbg, 'print'))
        self.msg_connect((self.dec, 'sinr'), (self.s2str, 'pdus'))
        self.connect((self.agc, 0), (self.k8, 0))
        self.connect((self.agc, 0), (self.spec, 0))
        self.connect((self.agc, 0), (self.sync, 0))
        self.connect((self.chan, 0), (self.agc, 0))
        self.connect((self.gdb, 0), (self.gras, 0))
        self.connect((self.gmag, 0), (self.gdb, 0))
        self.connect((self.halfsc, 0), (self.ofdm_fft, 0))
        self.connect((self.k8, 0), (self.ncorr, 0))
        self.connect((self.k8, 0), (self.npow, 0))
        self.connect((self.keep12, 0), (self.gmag, 0))
        self.connect((self.keep12, 0), (self.s2v, 0))
        self.connect((self.ncorr, 0), (self.nmag, 0))
        self.connect((self.nen, 0), (self.neps, 0))
        self.connect((self.neps, 0), (self.nnorm, 1))
        self.connect((self.nmag, 0), (self.nnorm, 0))
        self.connect((self.nnorm, 0), (self.nras, 0))
        self.connect((self.npow, 0), (self.nen, 0))
        self.connect((self.ofdm_fft, 0), (self.v2s, 0))
        self.connect((self.rtl, 0), (self.chan, 0))
        self.connect((self.s2str, 0), (self.sras, 0))
        self.connect((self.s2v, 0), (self.dec, 0))
        self.connect((self.sync, 0), (self.halfsc, 0))
        self.connect((self.v2s, 0), (self.keep12, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("GNU Radio", "nbiot_rx_sinr")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.set_chan_taps(firdes.low_pass(1.0, self.samp_rate, 100e3, 30e3, window.WIN_HAMMING))
        self.set_npss_taps(nbiot.npss_matched_taps(self.samp_rate / 8))
        self.rtl.set_sample_rate(0, self.samp_rate)
        self.spec.set_frequency_range(self.f_nb, self.samp_rate)

    def get_npss_taps(self):
        return self.npss_taps

    def set_npss_taps(self, npss_taps):
        self.npss_taps = npss_taps
        self.set_npss_len(len(self.npss_taps))
        self.ncorr.set_taps(self.npss_taps)

    def get_if_offset(self):
        return self.if_offset

    def set_if_offset(self, if_offset):
        self.if_offset = if_offset
        self.set_lo_freq(self.f_nb + self.if_offset)
        self.chan.set_center_freq((-self.if_offset + self.cfo_hz))

    def get_f_nb(self):
        return self.f_nb

    def set_f_nb(self, f_nb):
        self.f_nb = f_nb
        Qt.QMetaObject.invokeMethod(self._f_nb_line_edit, "setText", Qt.Q_ARG("QString", eng_notation.num_to_str(self.f_nb)))
        self.set_lo_freq(self.f_nb + self.if_offset)
        self.spec.set_frequency_range(self.f_nb, self.samp_rate)

    def get_split_db(self):
        return self.split_db

    def set_split_db(self, split_db):
        self.split_db = split_db

    def get_ppm(self):
        return self.ppm

    def set_ppm(self, ppm):
        self.ppm = ppm
        Qt.QMetaObject.invokeMethod(self._ppm_line_edit, "setText", Qt.Q_ARG("QString", eng_notation.num_to_str(self.ppm)))
        self.rtl.set_frequency_correction(0, self.ppm)

    def get_npss_len(self):
        return self.npss_len

    def set_npss_len(self, npss_len):
        self.npss_len = npss_len
        self.nen.set_length_and_scale(self.npss_len, 1)

    def get_lo_freq(self):
        return self.lo_freq

    def set_lo_freq(self, lo_freq):
        self.lo_freq = lo_freq
        self.rtl.set_frequency(0, self.lo_freq)

    def get_half_sc(self):
        return self.half_sc

    def set_half_sc(self, half_sc):
        self.half_sc = half_sc
        self.halfsc.set_k(self.half_sc)

    def get_gain(self):
        return self.gain

    def set_gain(self, gain):
        self.gain = gain
        self.set_rtl_gain(0, 'TUNER', self.gain)

    def get_chan_taps(self):
        return self.chan_taps

    def set_chan_taps(self, chan_taps):
        self.chan_taps = chan_taps
        self.chan.set_taps(self.chan_taps)

    def get_cfo_hz(self):
        return self.cfo_hz

    def set_cfo_hz(self, cfo_hz):
        self.cfo_hz = cfo_hz
        self.chan.set_center_freq((-self.if_offset + self.cfo_hz))




def main(top_block_cls=nbiot_rx_sinr, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
