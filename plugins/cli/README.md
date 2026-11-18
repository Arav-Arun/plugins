# Command Line

The `ardia` command as an Ardia plugin: drive Ardia, read pages and run its commands from your terminal or an agent.
Its manual is [docs/cli.md](https://github.com/ardia-browser/ardia/blob/main/docs/cli.md) in ardia-browser/ardia.

Ardia ships this plugin built in and **always on**: there is nothing to install or turn on. At launch Ardia copies it
into `~/Library/Application Support/Ardia/Programs/cli/`, checks the copy against the digest the release signed, and
links `~/.local/bin/ardia` to it. An `ardia` installed the old way is adopted: its link moves to the new copy, and
`~/Library/Application Support/Ardia/CLI/current` points at it, so paths saved through the old home keep working. The
first time it's installed it turns on "Let the ardia command control Ardia", unless you turned that off; after that the
setting is yours. The program runs as you, outside Ardia's sandbox, like anything you install.

It contributes:

- `alwaysOn`, which only a plugin built into Ardia may declare;
- `programs`: `ardia` (`bin/ardia`), with `afterUpdate` run once per new version, as `install.sh` did after an upgrade:
  `skills install --auto`, `skills refresh`, `computer refresh`;
- the `control` permission.

It has no helper, page or tools.

## Build

```sh
ARDIA_PLUGIN_VERSION=0.2.60 plugins/cli/build.sh <out dir>
```

This writes `<out dir>/cli`, the plugin folder Ardia bundles, plus the release tarball
`ardia-cli-<version>-<platform>.tar.gz` and its `.sha256`, which Linux and older Ardia installs download. It needs uv
and cargo (`rustup`); `scripts/build.sh --help` lists the rest. Ardia's release runs it from the submodule commit it
pins.

## Test

```sh
cd plugins/cli
uv venv --python 3.13 /tmp/ardia-cli-venv
VIRTUAL_ENV=/tmp/ardia-cli-venv uv pip install --require-hashes --no-build -r requirements.lock
/tmp/ardia-cli-venv/bin/python -m unittest discover -s tests
(cd rust && cargo test --locked)
```

A few checks compare against Ardia's own source, such as the ingest key, the tab group colours and the feedback
service. They run when this repo is mounted as ardia-browser/ardia's `plugins` submodule, or with `ARDIA_REPO` pointing at an
ardia checkout, and skip otherwise. `tests/fixtures/plugins` is a copy of ardia's `tests/fixtures/plugins`, and
`ardia_cli/schemas/plugin.schema.json` is the schema Ardia writes. ardia's tests fail when either differs, so refresh
them from there.
