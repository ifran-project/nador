# CSI in NADOR

NADOR can log, view live and collect the **channel state information (CSI)** measured by OAI and OCUDU gNBs. The CSI
here is the raw uplink channel `H`: one complex value per resource block (RB), receive antenna and SRS port, for
every SRS occasion of every connected UE. It is the data, not an indicator derived from it.

This page explains where the CSI comes from (section 1), how the gNBs extract it (section 2), how NADOR deploys,
views and collects it (section 3), the analysis offered on top of it (section 4) and why it is useful (section 5).
It ends with the limits of the approach (section 6) and the options to expose the CSI to O-RAN xApps (section 7).

## 1. Source of the CSI: the uplink SRS

The **Sounding Reference Signal (SRS)** is an uplink reference signal sent by the UE. The gNB knows its exact
content in advance. It carries no data: it only sounds the radio channel between the UE and each gNB antenna.

The gNB chooses every SRS parameter and sends them to the UE by RRC (`SRS-Config`, in the RRC reconfiguration that
follows the attachment):

| Parameter | Role | Example (OCUDU, 3 UEs) |
|---|---|---|
| Type | periodic, semi-persistent or aperiodic (triggered by DCI) | periodic |
| Period | interval between two SRS occasions | 20 ms |
| Symbols | position in the slot: 1, 2 or 4 symbols | last symbol of the slot |
| Sounded band | RBs covered | 96 RB, almost the whole carrier |
| Comb | the UE only transmits on 1 subcarrier out of K | comb 4: 3 subcarriers per RB |
| Comb offset | shift of the comb | 0, 1 and 2 for the 3 UEs |
| Sequence | Zadoff-Chu sequence (group, number, cyclic shift) | — |
| Ports | number of UE antenna ports sounded (1, 2 or 4) | 1 port |

The comb and the cyclic shift let several UEs sound the same symbol without interfering: with a comb of 4, three
UEs use subcarriers `[0,4,8]`, `[1,5,9]` and `[2,6,10]` of each RB and stay orthogonal in frequency.

At each SRS occasion, the gNB:

1. takes the received samples on the SRS subcarriers of each receive antenna;
2. divides them by the known sequence (least squares, LS, estimate): this gives `H(k)`, the complex channel
   (amplitude and phase) on each pilot subcarrier `k`, for each antenna;
3. estimates the timing offset (time alignment, TA) from the phase slope of `H(k)` and compensates it;
4. computes its metrics: EPRE, RSRP, noise, TA.

The gNB normally uses these estimates for uplink scheduling (RBs, MCS), power control, timing advance and, in TDD,
downlink beamforming by channel reciprocity. The CSI loggers copy them out of the gNB.

### Why the SRS rather than another source

| Source | What it gives | Why it does not fit |
|---|---|---|
| UE CSI reports (CSI-RS, then CQI/PMI/RI) | quantized indices: quality, precoding matrix, rank | not the channel: a few bits per subband, computed by the UE with a proprietary algorithm; `H` cannot be rebuilt |
| PUSCH DMRS | a channel estimate | only on the RBs where the UE is scheduled: nothing without traffic, coverage set by the scheduler |
| PUCCH DMRS | a channel estimate | only 1 or 2 RBs at the band edges |
| **SRS** | complex `H` per RB and receive antenna, over the whole band, at a fixed interval | — |

The SRS gives the raw channel measured at the gNB (amplitude and phase, without quantization or UE algorithm), over
the whole band and independently of the traffic, at a regular interval that makes a real time series. SRS with one
port is mandatory for commercial NR UEs, so COTS UEs provide it.

## 2. Extraction in the gNBs

Both gNBs use a patched image with a CSI logger. In both, the PHY thread only copies the SRS channel estimate into a
memory buffer, and never blocks on I/O; a separate thread writes the buffer to a CSV file on the RAN node every
5 seconds. Nothing goes through E2: collection and live view read this file, outside the gNB.

| | OAI (`oai-csi-logging`, logger v3.1) | OCUDU (`ocudu-csi-logging`) |
|---|---|---|
| Hook | `nr_srs_rx_procedures()` (`phy_procedures_nr_gNB.c`), after the SRS timing advance measurement | `srs_estimator_generic_impl.cpp`, after the TA phase compensation of `mean_lse` |
| Data copied | `srs_estimated_channel_freq`, interpolated over the 12 subcarriers of each RB, int16 fixed point | LS estimate on the pilots only (1 subcarrier out of 2 or 4, depending on the comb), float |
| `rb` granularity (default) | complex mean of the 12 subcarriers of the RB | complex mean of the pilots of the RB |
| `subcarrier` granularity | one row per subcarrier (optionally decimated) | one row per pilot, no interpolation |
| UE identification | RNTI of the SRS PDU | RNTI of the FAPI `srs_context` (0 if absent) |
| Buffer | ring of 1 M rows, priority-inheritance mutex shared with L1, O(1) swap at flush | two pre-allocated buffers of 1 M rows, swapped at flush |
| Writer | thread pinned on `CSI_FLUSH_CORE` | same, plus `fdatasync` and `posix_fadvise(DONTNEED)` after each flush |
| Images | `r2labuser/oai-gnb-csi`, built from the weekly OAI tag | `r2labuser/ocudu-gnb-csi`, `r2labuser/ocudu-du-csi` |

Both write the same **CSV v3.1** format (`csi_per_rb.csv`):

- a JSON header that declares the columns, the granularity and the format version;
- one row per SRS occasion, UE, receive antenna, SRS port and RB: `frame, slot, rnti, ant_rx, port_tx, rb, real,
  imag` (plus `sc` in subcarrier granularity);
- a `# TIMESTAMP:` marker in UTC (1 s resolution) at each flush: it closes the batch of rows written before it;
- a `# DROPPED: n` line when the buffer overflowed: lost rows are counted, never silent.

The logger options are environment variables read when the gNB starts; NADOR sets them from the profile:

| Variable | Values | Default |
|---|---|---|
| `CSI_GRANULARITY` | `rb` or `subcarrier` | `rb` |
| `CSI_SUBCARRIER_SAMPLING` | 1 to 12 (one subcarrier out of N, `subcarrier` only) | 1 |
| `CSI_ANTENNA_SELECTION`, `CSI_PORT_SELECTION` | `all` or indices | `all` |
| `CSI_FLUSH_CORE` | CPU of the writer thread | logger default |

## 3. CSI in NADOR

CSI is a metric of the monitoring profile, off by default. Turn it on with its options:

```yaml
metrics:
  ran:
    csi:
      components: []           # gNB/DU components that log; empty: every OAI or OCUDU gnb and du
      srs_period_ms: 40        # OCUDU SRS period (OAI chooses it from the TDD period)
      granularity: rb          # rb or subcarrier
      subcarrier_sampling: 1   # one subcarrier out of N, with granularity: subcarrier
      antennas: all            # all, or receive antenna indices such as 0,1
      ports: all               # all, or SRS port indices
      output_dir: null         # default /data/csi (OAI), /mnt/csi (OCUDU)
```

**Enabling the logger** (`nador.configure_ran_monitoring(ran, config)`, before `ran.deploy()`; it calls `nador.configure_csi`):

- selects the CSI image built from the same OAI tag (or OCUDU release) for the chosen OAI and OCUDU `gnb`/`du`
  components only; the CU has no PHY, hence no SRS, and asking for it is an error. Every other component keeps its
  image;
- forces periodic SRS: `do_SRS: periodic` (and CSI-RS) in the OAI gNB configuration, `cell_cfg.srs` with
  `srs_period_ms` in the OCUDU one. Only these lines are added to the configuration IFRAN renders, and only for the
  components that log;
- mounts the output directory of the node as a hostPath (`/data/csi` for OAI, `/mnt/csi` for OCUDU), where the
  file survives pod restarts, and refuses two loggers writing the same file on the same node;
- refuses a RAN that is already deployed: the image and configuration are set at deployment.

Without `csi` in the profile, `configure_csi` changes nothing.

**Storage** (`nador.csi.prepare_csi_storage`): the R2Lab and SLICES nodes boot a live image whose root filesystem
is in RAM, which a CSI file of several MB/s fills in minutes. The data partition of the node is bind-mounted on the
output directory. The disk is never formatted, and the call stops with an error rather than writing in RAM when a
node has no partition.

**Live view** (`nador.csi.CsiLiveView`): a Streamlit visualizer (section 4) runs on another node, usually the
monitoring node, never on the RAN node. On the RAN node, only `sshd` and `tail -F` run, at the lowest priority.
The view shows a sliding window (120 s by default), and a tunnel opens it on the notebook host.

**Collection** is per run: `ExperimentRun(..., monitoring=monitoring, collect_csi=True)` collects the CSI file of
every component that logs it. Without `collect_csi=True` the gNB keeps logging, and nothing is read from the node.

- before the workload, the clock offset between the RAN node and the notebook host is measured over SSH, from
  the sample with the smallest round trip; it is applied to the windows only above 0.5 s. It is measured again
  after the workload, and a change above 0.5 s is reported as a warning;
- after the run, the whole file is read on the RAN node (at the lowest priority) and cut per experiment section
  (baseline, ping, each iperf test, ...);
- the artifacts land under `csi/<component>/`: the cuts in `by_window/section/`, the windows on the node clock in
  `csi_windows.json`, and `csi_collection.json` (provenance: node, image, `CSI_*` options, rows, markers, dropped
  rows). `experiment_metadata.json` records, under `csi`, the clock measurements, the offset applied, the summary
  and the warnings (clock drift, dropped rows).

## 4. Proposed analysis

The CSV v3.1 is read by the same tools whichever gNB wrote it: the Streamlit visualizer, in live mode or on a
collected file, and the offline tools of `oai-csi-logging` (`csi_diag.py`). The visualizer organizes the
analysis in eight tabs, in the order of an investigation, each with an explanation, the formulas used and typical
cases:

| Tab | Content |
|---|---|
| Start here | automatic key findings, data health, guide and glossary |
| UEs | level of each UE over time (dB), frequency response, timing, level CDF, per-UE table |
| Frequency | channel along the band: shape (dips), fluctuation at each frequency, weak or unstable RBs |
| Heatmap | RB × time, one panel per UE, optional dB scale, experiment sections drawn |
| Time | evolution and distribution of the level, periodic variations, isolated glitches |
| Channel | power delay profile, frequency correlation and coherence bandwidth, RMS delay spread, temporal correlation and coherence time |
| Timing & phase | delay from the phase slope across RBs; residual phase after removing that slope and the common phase of each occasion |
| Antennas, ports & slots | balance of the radio branches: level per receive antenna, SRS port and slot |

The **key findings** are computed with simple, stated rules, and point to the tab where each can be checked:

- SRS period of each UE;
- missed SRS occasions (the UE was not scheduled or the logger dropped rows) and rescheduled ones (interval that is
  not a multiple of the period, e.g. after a reconfiguration);
- UEs present during part of the file only, and pauses of 1 s or more (UE detached and back, no SRS);
- RBs never sounded (outside the SRS bandwidth, excluded from every view);
- gaps without any CSI row;
- spread of the UE levels (> 3 dB) and frequency-selective channels (dip > 10 dB in the average response).

Statistical findings and channel metrics are only computed for UEs with enough SRS occasions. Raw phase is not
shown as such: it rotates from one occasion to the next, and only the slope (delay) and the residual phase carry
information.

## 5. Why this analysis is useful

- **Characterizing the radio channel of an experiment.** Selectivity, delay spread, coherence bandwidth and
  coherence time describe the channel each UE saw, per antenna. Different positions, setups (R2Lab anechoic
  chamber, SLICES nodes, RFsim) and radios (AW2S, Benetel, USRP) can be compared on the same basis.
- **Explaining the KPIs.** Each experiment section has its own CSI file, on the same timeline as the Prometheus
  metrics and the iperf reports: a throughput drop or a BLER increase can be related to a fading dip, a UE level
  change or a missing antenna, rather than guessed.
- **Diagnosing the RAN.** The SRS stream is a health indicator of the cell: missed occasions reveal scheduling or
  logging problems; pauses reveal UEs that lose the link and re-attach (for instance after radio link failures and
  RRC re-establishments); an unbalanced or dead receive branch shows up in the antenna view; the timing view
  shows the residual delay of each UE.
- **Comparing OAI and OCUDU.** Both gNBs write the same format, through the same chain: the same experiment can be
  analysed with either implementation, and the differences come from the gNBs, not from the tools.
- **Building datasets.** The per-section files, with their provenance (image, logger options, node clock offset),
  are reproducible CSI datasets for offline studies and machine learning (localization, presence or motion
  detection, channel prediction, link adaptation).

## 6. Limits

- **Uplink channel.** In TDD (n78 with Benetel, AW2S or USRP), the uplink channel equals the downlink one by
  reciprocity, up to the RF calibration. In FDD (band 3 in RFsim), the two directions use different frequencies: the
  SRS only gives the uplink.
- **Pilots only with OCUDU.** With a comb of 4, OCUDU logs 3 subcarriers out of 12 per RB; OAI interpolates over the
  12 subcarriers.
- **A UE that transmits SRS is needed.** COTS UEs do; srsUE does not (and the SRS configuration even degrades its
  PUCCH), so the ZMQ setups with srsUE give no CSI.
- **One SRS port with OCUDU.** A UE configured with 2 SRS ports (`maxNumberSRS-PortsPerResource`) is not logged yet,
  with a warning.
- **Relative values.** The amplitude depends on the UE power control, and the linear phase due to the timing offset
  is removed (the hooks are after the TA compensation): the values are relative, not an absolute path loss.
- **Time resolution.** One sample per SRS period (20 to 80 ms in the examples); a shorter period costs uplink
  resources and log volume. The time markers have a 1 s resolution, at each 5 s flush.
- **Volume.** With real radios the file grows by several MB/s (about 5.5 MB/s observed with 3 UEs on a 100 MHz
  Benetel carrier, 4 receive antennas, RB granularity), and each collection reads the whole file: check the space
  left on the data partition (`df -h /data/csi`).

## 7. Towards O-RAN xApps (E2)

The CSI currently reaches the analysis through a file, with a delay of about 5 s (flush period). Both gNBs have an
E2 agent (KPM, RC), but no E2 service model carries a channel estimate: KPM only reports aggregated indicators.

| Option | Data in the xApp | Assessment |
|---|---|---|
| A. CSI indicators in E2SM-KPM | SRS SNR, delay spread, rank, channel variation per UE | standard transport and low volume, but the raw `H` is lost |
| B. Dedicated service model (E2SM-CSI) | `H` per RB (per pilot on demand), filtered by UE, antenna, port | complete data; prototyped on OAI by EURECOM (E2SM-SRS, about 7.7 ms end to end); not interoperable without its decoder |
| C. E2SM-LLC v1.00 | channel information as defined by O-RAN | future standard path, no known open implementation |
| D. xApp reading the file stream | the current data | almost immediate, but not an O-RAN xApp in the strict sense, 5 s delay |
| E. dApp on E3 | SRS, IQ, uplink CIR, below 1 ms | the only real-time option (MU-MIMO, beamforming); research code, OAI only |

Per RB, the stream fits E2 (about 3.5 Mbit/s per UE for 100 MHz, 4 antennas, 2 ports and SRS every 20 ms); per
subcarrier it becomes heavy (about 42 Mbit/s per UE). The PHY hook stays the same in every option: only the
transport changes, in a thread fed by the existing buffer.

For collection and offline analysis, E2 brings almost nothing. It becomes worthwhile to act on the RAN from the CSI
in closed loop (link adaptation, mobility, presence detection, localization), or to give experimenters access to
the CSI through the RIC, without access to the RAN node. The recommended path is option B, on OAI first (from
E2SM-SRS), then on OCUDU, keeping the file logger as the reference trace. Cheaper improvements of the current chain
are a timestamp per SRS occasion and a sequence counter in the CSV.
