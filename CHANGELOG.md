# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Changes up to 1.0.0 are listed in the [GitHub releases](https://github.com/ifran-project/nador/releases).

## [Unreleased]

### Fixed
- OCUDU Grafana dashboard with several DUs or gNBs: a `RAN component` selector filters the RAN panels, each curve is
  named after its component (`du01 · ue_rnti=...`), totals are given per component, and connected UEs are counted
  per cell, since two DUs can give a UE the same RNTI. The CPU and memory panels show the DU, CU, CU-CP and CU-UP
  pods as well as the gNB pod.
- OCUDU UEs are named in the dashboard panels per RNTI and in a UE identity table, as with OAI. The OCUDU collector
  writes the C-RNTI in hex (`rnti="4601"`), as OCUDU logs it and the RNTI mapper reports it, instead of in decimal
  (`17921`); this also changes the `rnti` label of the exported OCUDU KPIs. With an OCUDU CU-CP and several DUs, the
  RNTI mapper names the DU of each UE (needs the rnti-mapper image of nador-images with the DU table).
- OAI and OCUDU dashboards with several cells on one gNB or DU: a UE is named by its cell (`pci` of the RNTI mapper)
  when the mapper knows it, else by its RNTI only when no other UE of the component has that RNTI, and never with a
  guessed name. The OAI panels and the RRC ARFCN and SCS show the cell (`pci`) of each curve, and the UE identity table
  its PCI. The uplink PRB saturation of a UE uses the PRBs of its cell when the gNB lists its cells. Needs the
  nador-images oai-gnb-metrics-parser 2026.w46-1 and rnti-mapper images with the cell labels; with older images the
  panels show what they showed before. What is per pod and per cell, and the one unsupported case (the L1 metrics of
  an OAI pod with several cells), are in the profile guide.

### Added
- A warning when an OAI gNB or DU pod serves several cells, whose L1 metrics are then not per cell: the OAI
  dashboard's "Cells per gNB/DU pod" panel turns red, and `ExperimentRun` raises a `RuntimeWarning` and records it
  under `warnings` in `experiment_metadata.json`.

### Changed
- The changelog check skips the image update pull requests that nador-images opens.
- The OAI metrics parser of NADOR's OAI charts is pinned in `images.json` (`oai_gnb_metrics_parser`) and updated by
  the nador-images pull requests like the other images; a profile cannot override it (`images:`), as it is deployed
  with the RAN.

## [1.1.1] - 2026-11-25

### Fixed
- `HandoverWatch` with OCUDU: the report checks step 5, the random access response of the target DU (its RAR to
  the RA-RNTI of the dedicated preamble), and says when the target DU detected the preamble but sent no RAR.

## [1.1.0] - 2026-11-24

### Added
- CSI (channel state information) as a profile metric, `metrics.ran.csi`: OAI and OCUDU gNBs and DUs log it with
  their CSI logger image, and `ExperimentRun(collect_csi=True)` collects it cut by section.
- `configure_ran_monitoring(ran, config)`: one call, before `ran.deploy()`, for everything the profile needs from
  the RAN (OCUDU metrics, CSI logger).
- RNTI mapper: names the UE behind each RNTI in the dashboards (`ran_ue_info`), for OAI and OCUDU RANs with OAI,
  Open5GS or free5gc cores.
- `nador.handover.HandoverWatch`: checks an F1 handover step by step in the OAI or OCUDU logs.
- `iperf_summary`: the throughput of every iperf test of a run.
- Log export controls: `loki_drop` and `ran_log_filter` leave chosen gNB log lines out of a run's export,
  `loki_warn_lines` warns before a large export, and `logs/summary.json` records how long it took.
- Documentation site, with guides to the profile, probes, experiments, logs, CSI and handovers.

### Changed
- `configure_ran_kpis` is no longer exported by `nador`: call `configure_ran_monitoring`.
- A profile with `throughput` and no AMF, SMF and UPF targets is refused when it is read, as with `rtt`.
- The UPF panels of the dashboards show every UPF, of any core, and memory as used, not as a rate.
- `monitoring.output()` writes the profile without its default metrics.

### Fixed
- `deploy_probe` and `remove_probe` keep the deployment model truthful when an installation fails.
- An unpinned CSI logger is refused before deployment, instead of failing the experiment run.
- OCUDU log settings that hide what monitoring reads are refused before deployment.
- IMSIs read as octal numbers in the UE names of the RNTI mapper.

[Unreleased]: https://github.com/ifran-project/nador/compare/v1.1.1...develop
[1.1.1]: https://github.com/ifran-project/nador/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/ifran-project/nador/compare/v1.0.0...v1.1.0
