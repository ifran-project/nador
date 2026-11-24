# Logs

With `logs: true` under `ran`, `core` or `host` (see [the profile](profile.md)), NADOR keeps the logs of
those pods in Loki, where Grafana shows them. An [experiment run](experiments.md) then exports the lines
written during the run to `logs/loki.jsonl`, with `logs/summary.json` saying how many lines, how long the
export took and whether it may be incomplete.

A gNB can log a lot: OCUDU at `info` writes a PHY line for every slot, several hundred thousand lines in
a couple of minutes, and exporting 400,000 lines takes about 20 minutes. There are two ways to keep that
in check, and they combine:

| | Set the gNB's log levels at deployment | Leave lines out of a run's export |
| --- | --- | --- |
| Where | IFRAN's `advanced_config` of each RAN component | `ExperimentRun(..., loki_drop=...)` |
| What changes | What the gNB writes: Loki, Grafana and `kubectl logs` all see less | Only what a run saves in `logs/loki.jsonl` |
| When | Before `ran.deploy()`; changing it means deploying the RAN again | For each run, without touching the network |
| Use it for | Logs you never want | Logs you want in Grafana but not in every run's artifacts |

## Setting the gNB's log levels at deployment

IFRAN renders each RAN component's configuration from its `advanced_config`; the log section is one of
its fields. Set it in the deployment model, per component (a split RAN has one per CU-CP, CU-UP and DU):

### OCUDU: `advanced_config.log`

```yaml
ran:
  implementation: ocudu
  components:
    gnb:
      type: gnb
      advanced_config:
        log:
          filename: stdout   # keep it: any other value writes a file in the pod, and Loki sees nothing
          all_level: info
          phy_level: warning
```

`log` **replaces** IFRAN's whole log section, which is `{filename: stdout, all_level: info,
config_level: debug}`: write every option you want. Options not written take OCUDU's defaults, and
OCUDU's default `filename` is `/tmp/gnb.log`, a file inside the pod: **always write `filename: stdout`**,
or Loki, Grafana and the exported runs get no gNB log at all.

The levels are, from least to most detailed, `none`, `error`, `warning`, `info` and `debug`. The options,
from [OCUDU's configuration reference](https://docs.ocudu.org/user_manual/config_reference/) (the complete
[gnb.yaml](https://docs.ocudu.org/config_examples/gnb.yaml) lists every section):

| Option | Default | What it sets |
| --- | --- | --- |
| `filename` | `/tmp/gnb.log` | Where logs go. Write `stdout` for Loki. |
| `all_level` | `warning` | One level for PHY, MAC, RLC, PDCP, RRC, SDAP, NGAP and GTP-U; the options below override it per layer |
| `phy_level`, `mac_level`, `rlc_level`, `pdcp_level`, `rrc_level`, `sdap_level`, `rohc_level` | `warning` | One radio layer |
| `ngap_level`, `f1ap_level`, `f1u_level`, `e1ap_level`, `e2ap_level`, `xnap_level`, `nrppa_level`, `gtpu_level` | `warning` | One interface |
| `cu_level`, `du_level` | `warning` | The CU and DU applications |
| `fapi_level` | `warning` | FAPI, with split 7.2 and split 6 only |
| `ofh_level`, `hal_level`, `ntn_level` | `warning` | Open Fronthaul, hardware acceleration, non-terrestrial networks |
| `radio_level` | `info` | The radio (UHD, ZMQ) |
| `lib_level`, `sec_level` | `warning` | Libraries, security functions |
| `config_level` | `none` | The configuration printed at start: `none`, `info` or `debug` only |
| `hex_max_size` | `0` | Bytes of each message printed in hex (0: none, -1: all, up to 1024) |
| `broadcast_enabled` | `false` | Also log broadcast messages and every PRACH occasion |
| `f1ap_json_enabled`, `e1ap_json_enabled` | `false` | Log F1AP or E1AP messages as JSON |
| `high_latency_diagnostics_enabled` | `false` | Log diagnostics when processing falls behind |
| `phy_rx_symbols_filename`, `phy_rx_symbols_port`, `phy_rx_symbols_prach` | none, `0`, `false` | Dump received IQ symbols to a file |

The options can change between OCUDU releases: `gnb log -h` in the image you deploy prints its own list.

**What NADOR reads from these logs, and checks.** `nador.configure_ran_monitoring(ran, config)`, called before
`ran.deploy()`, refuses a `log` section that would hide it, with the component and the option to change:

| The profile turns on | NADOR reads | So each component's `log` must have |
| --- | --- | --- |
| `ran.logs` | Every OCUDU pod's log, into Loki | `filename: stdout` |
| `ran_kpis` or `csi` | The UE lines of the gNB, CU or CU-CP (`ue=0 c-rnti=0x4601: UE created`, `ue=0 ran_ue=0`), for the RNTI mapper to name the UE behind each RNTI | `filename: stdout`, and CU and NGAP at `info` or `debug` |

`cu_level` and `ngap_level` take `all_level` when not written, so `all_level: info` is enough; with
`all_level: warning`, write `cu_level: info` and `ngap_level: info`. For example, this is refused when the
profile turns on `ran_kpis`:

```
ValueError: OCUDU log settings hide what monitoring reads (see the Logs guide):
  - gnb: log.cu_level must be info or debug for the RNTI mapper, not warning
  - gnb: log.ngap_level must be info or debug for the RNTI mapper, not warning
```

Components without their own `log` keep IFRAN's, which fits.

### OAI: `advanced_config.log_config`

```yaml
ran:
  implementation: oai
  components:
    gnb:
      type: gnb
      advanced_config:
        log_config:
          global_log_level: info
          nr_phy_log_level: warn
          nr_mac_log_level: warn
```

`log_config` **replaces** IFRAN's log section, which is `global_log_level`, `hw_log_level`,
`phy_log_level`, `mac_log_level`, `rlc_log_level`, `pdcp_log_level` and `rrc_log_level` at `info`, and
`ngap_log_level` and `f1ap_log_level` at `debug`. A layer not written takes `global_log_level`.

The levels are, from least to most detailed, `error`, `warn`, `analysis`, `info`, `debug` and `trace`
(OAI's [logging documentation](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/common/utils/LOG/DOC/rtusage.md)).
The options:

| Option | What it sets |
| --- | --- |
| `global_log_level` | The level of every layer not set below |
| `<component>_log_level` | One layer: the component's name in lowercase |
| `global_log_online` | `0` discards every console message |
| `global_log_options` | What each line's header shows: `nocolor`, `level`, `thread`, `thread_id`, `function`, `line_num`, `time`, `wall_clock` or `utc_time` |
| `<component>_log_infile` | `1` writes that layer to a file in `/tmp` of the pod instead (Loki no longer sees it) |

The component is the name between brackets in the log line, in lowercase: a `[NR_PHY]` line follows
`nr_phy_log_level`, a `[PHY]` line `phy_log_level`. The 5G layers are `nr_phy`, `nr_phy_dci`,
`nr_phy_rach`, `nr_mac`, `nr_mac_dci`, `nr_rrc`, `phy`, `mac`, `rlc`, `pdcp`, `sdap`, `rrc`, `ngap`,
`f1ap`, `f1u`, `e1ap`, `xnap`, `nrppa`, `gtpu`, `sctp`, `hw`, `gnb_app` and `itti`; the complete list
is printed at start-up with the `dbgl5` debug option of OAI's configuration module.

The RNTI mapper reads OAI's `nrRRC_stats.log` file, not the logs: OAI's levels do not change it.

## Leaving lines out of a run's export

`ExperimentRun` exports the lines of the logs the profile keeps, between the run's start and end. Three
options choose what it exports, and one warns about a long export:

| Option | Default | What it does |
| --- | --- | --- |
| `loki_selector` | every namespace with logs | Which streams: a LogQL selector, such as `{namespace="oai-core"}` |
| `loki_drop` | OCUDU's PHY `info` and `debug` lines | Line patterns (RE2) Loki leaves out; `[]` keeps every line |
| `loki_warn_lines` | 100000 | Above this many lines, warn before exporting, with how to shorten it; `0`: never |
| `loki_limit`, `loki_max_queries` | 5000, 1024 | Lines per query and queries per run |

The lines are filtered by Loki itself: what is left out never travels to your computer. Before
exporting, the run asks Loki how many lines remain, and warns when there are more than
`loki_warn_lines`:

```
RuntimeWarning: Loki export: about 420,000 log lines for this run, 84+ queries; this can take many
minutes (roughly 20 min for 400,000 lines). To shorten it, narrow loki_selector ...
```

### Choosing gNB layers: `ran_log_filter`

Rather than writing patterns, choose gNB/CU/DU layers and the lowest level kept of each:

```python
from nador.artifacts import ExperimentRun, ran_log_filter

drop = (
    ran_log_filter(["PHY", "MAC"], keep_from="warning")  # PHY and MAC: only warnings and errors
    + ran_log_filter(["RLC"], keep_from="none")          # RLC: nothing
)                                                        # every other layer: untouched
async with ExperimentRun(run_directory, monitoring=monitoring, loki_drop=drop) as experiment:
    ...
```

The same name selects the layer in OCUDU logs (`[MAC     ] [I]`) and in OAI logs (`[NR_MAC]   I` or
`[MAC]   I`). The layers are those of `nador.artifacts.RAN_LOG_LAYERS`:

PHY, MAC, SCHED, RLC, PDCP, SDAP, GTPU, RRC, NGAP, F1AP, E1AP, CU-CP, CU-UP, DU, FAPI, RU, OFH, HW, SCTP, METRICS

| `keep_from` | Left out |
| --- | --- |
| `info` | debug and trace |
| `warning` (default) | debug, trace and info |
| `error` | everything below errors |
| `none` | the whole layer |

Without `loki_drop`, a run leaves out OCUDU's PHY `info` and `debug` lines only (`OCUDU_PHY_DETAIL`); OAI's
PHY lines are kept. Your own `loki_drop` replaces that default: add it back if you still want it:

```python
from nador.artifacts.loki_export import OCUDU_PHY_DETAIL

drop = [OCUDU_PHY_DETAIL, *ran_log_filter(["MAC"])]
```

Leaving lines out of the export never changes the RNTI mapper, which reads the pods directly.

### Other log lines

`loki_drop` takes any [RE2](https://github.com/google/re2/wiki/Syntax) pattern, for lines of the core or
of NADOR's pods too. Each pattern is one Loki line filter (`!~`), so a backquote cannot be part of it:

```python
loki_drop=[r"GET /metrics HTTP", r"\[amf_sbi\] \[info\]"]
```

### What the export records

`logs/summary.json` has, besides the line count and whether the export may be incomplete:

| Field | Meaning |
| --- | --- |
| `seconds` | How long the export took |
| `dropped_lines_matching` | The `loki_drop` patterns applied |
| `selector` | The `loki_selector` used |
| `queries`, `max_queries` | Loki queries made, and the budget |
