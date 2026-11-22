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

3. Make your change.
4. Run the same checks as CI:

   ```bash
   ruff check src && ruff format --check src
   pyright src
   ```

5. Open a pull request into `develop` that says what changed and why.

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
