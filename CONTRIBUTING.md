# Contributing to NADOR

Thanks for helping. Bug reports, fixes, new metrics and new example deployments are all welcome.

## Reporting a bug

Open an issue with:

- what you ran: the monitoring profile (`monitoring.yaml`) and the kind of deployment (RFsim, ZMQ, B210, ...);
- what happened: the full error output or the failing cell;
- the NADOR and IFRAN versions (`pip show nador ifran`).

## Making a change

1. Fork the repository and branch off `develop`. `main` only receives releases.
2. Set up a development environment (Python 3.12 or later):

   ```bash
   python -m pip install -e ".[dev]"
   ```

3. Make your change, with a test for any behaviour it adds or changes.
4. Run the same checks as CI:

   ```bash
   ruff check src tests && ruff format --check src tests
   pyright src
   pytest -m ""
   ```

5. Add your change to [`CHANGELOG.md`](CHANGELOG.md), under `[Unreleased]` (see below).
6. Open a pull request into `develop` that says what changed and why.

## Changelog

Every pull request that changes what NADOR does or how it is used adds a line to `CHANGELOG.md`,
under `## [Unreleased]`, in one of these sections:

- `### Added` for new metrics, options, functions or examples;
- `### Changed` for behaviour that changes, especially anything users must adapt to;
- `### Deprecated` and `### Removed` for what is going away or gone;
- `### Fixed` for bugs;
- `### Security` for vulnerabilities.

Write it for someone who uses NADOR, not for its developers: what they can do now, or must change, in
one sentence, naming the profile key, option or function. Changes to tests, CI or internal code that
users do not see need no line; say so in the pull request. At release, `[Unreleased]` becomes the
version and its date.

## Tests

The tests need no cluster. `tests/support.py` builds monitoring profiles and IFRAN networks, and
the `ansible` fixture replaces `run_ansible_role`, the one place NADOR reaches a cluster, with a
recorder. Everything else (validation, planning, the manager's decisions) runs for real.

- `tests/unit/` checks one rule per test, such as "RTT runs only where `at` says" or "removing the
  last session metric removes the session stack". `pytest` runs these, in a few seconds.
- `tests/integration/` starts real processes: an Ansible playbook against your machine and the
  UE sampler against a local HTTP server. Run them with `pytest -m integration`, or everything
  with `pytest -m ""` as CI does.

Name a test after the behaviour it checks, build the profile it needs inside the test, and
assert on what NADOR asks for, not on how it is implemented.

## Dependencies in CI

CI installs locked versions, checked by hash, from `requirements/ci.txt`. After changing a
dependency in `pyproject.toml`, regenerate that file with the command at the top of
`requirements/ci.in` and commit both.

## Documentation

The documentation is built from `docs/` and the notebooks in `examples/`, on every pull request
and published from `main`. Build it locally with:

```bash
python -m pip install -e ".[doc]"
sh docs/convert.sh
sphinx-build -W --keep-going -b html docs docs/_build/html
```

`-W` turns warnings into errors, as in CI.

## Good to know

- **Commands in containers.** IFRAN joins a container command's arguments with spaces and
  splits them again, so every argument must be free of spaces: pass plain argv
  (`["iperf3", "-c", host]`), never `["sh", "-c", "a script"]`.
- **Helm resources.** Don't change the resource requests and limits in the Helm charts in a
  pull request about something else; open an issue first.
- **Examples.** A new example deployment goes under `examples/monitoring/` with its own
  `monitoring.yaml`, and follows the outline of the existing notebooks: Install, Describe the
  network, Provision, Core, RAN, Monitoring, UEs, Traffic and experiment, Clean up.

By contributing, you agree that your contributions are licensed under the project's
[license](LICENSE).
