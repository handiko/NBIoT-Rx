# NB-IoT decoder + SINR analysis: `nbiot_rx_sinr.grc`

This is a copy of `nbiot_rx.grc` (same RTL-SDR front end, sync, cell search, MIB/SIB decoding) with extras that answer one question: **is the smeared constellation caused by bursty traffic from a co-channel sector?** The original flowgraph is unchanged.

## What is added

| Addition | What it shows |
|---|---|
| **SINR raster** (bottom) | One row per frame, 10 columns = subframes 0–9, 3 s per screen. Dark = high serving-cell SINR, light = low. |
| **NPDSCH: CLEAN subframes** | SIB1/SI symbols from subframes whose SINR ≥ the split (default 8 dB). |
| **NPDSCH: INTERFERED subframes** | The same from subframes below the split. |
| `[sinr]` console line per SIB1 repetition / SI transmission | e.g. `subframe SINR 13 3 -2 15 2 -3 -7 4 dB -> 2 clean / 6 interfered; QPSK SNR clean 5.6, interfered 3.4` |
| `[sinr]` summary every 5 s | For each subframe: median / 10th-percentile SINR and the % of frames below the split. |
| **Clean/interfered split [dB]** control | Takes effect when the flowgraph is restarted. |

## How the SINR is measured

For every subframe that carries the serving cell's NRS, the decoder compares each pilot with the frame-wide channel model. The frame-wide model averages the bursts out, but a co-channel data burst landing on that one subframe's pilots inflates the residual:

**SINR = model power / residual power** (8 pilots per subframe and port, so expect about ±1.5 dB of scatter).

Columns with no measurement show as −10 dB (light):
- subframe 5 (NPSS)
- subframe 9 in even frames (NSSS)
- before SIB1, subframes not yet known to carry NRS (2, 6, 7, 8)

## Reading it

- **Light streaks scattered randomly in the data subframes,** and a CLEAN plot that stays tight while INTERFERED stays a blob: the interference is bursty traffic from the other sector. That's the expected result here.
- **Whole columns permanently light:** a steady collision. For example, both sectors broadcasting in the same subframes.
- **Everything uniformly light:** plain low SNR (weak signal or wrong gain), not interference.
- **Clean QPSK SNR below the pilot SINR** (e.g. 8 dB vs 13 dB) is normal. The other sector's always-on NRS sits on some of the serving cell's data REs (PCI mod 6 differs), which hits the data and never the pilots.

## Verified (GNU Radio 3.10.9.2, generated flowgraph)

**Test signal:** two time-aligned sectors (PCI 129 + PCI 130, 1 dB apart), with the neighbour sending data bursts in 40% of its data subframes.
- Burst-hit subframes measured −15 to +4 dB; untouched ones 10–20 dB.
- The CLEAN constellation showed four tight clusters (QPSK SNR 6–8.5 dB). INTERFERED was a blob (3 dB or noise).
- The full chain (MIB, SIB1, SI messages) still decoded.
