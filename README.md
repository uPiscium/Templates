# Templates

Nix flake templates for reproducible development environments.

## Existing templates

The existing `python` and `rust` templates remain available under their current
flake template names while the Agent-ready template system is introduced
incrementally.

```sh
nix flake init -t github:upiscium/Templates#python
nix flake init -t github:upiscium/Templates#rust
```

## Generated Agent-ready templates

Agent-ready templates are generated artifacts. Do not edit files under
`templates/<name>/` directly. Their sources live under:

```text
components/agent-core/
components/adapters/<adapter>/
```

The generator fails on component path collisions instead of silently choosing
an overwrite order. It also preserves dotfiles, symlinks, and executable bits.

The first generated template is the language-neutral composition fixture:

```sh
nix flake init -t github:upiscium/Templates#agent-base
```

## Template generation API

The Templates repository exposes generation through the `template` Just
module:

```sh
just template::render agent-base
just template::render-all
just template::check
```

- `render` rebuilds one registered template from Agent Core plus its adapter.
- `render-all` rebuilds all registered generated templates in deterministic
  name order.
- `check` renders into temporary directories and fails when committed generated
  output has drifted from its component sources. It also rejects unregistered
  generated template directories.

Template registrations live in `templates/manifest.json`.

The legacy `python/` and `rust/` directories are intentionally outside this
generated tree until their Project Adapter migration is implemented separately.
