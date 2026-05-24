# Contributing to tckr

## Release flow

Releases are tagged on `main` and published to three places:

| Destination | How | Triggered by |
|---|---|---|
| GitHub (source of truth, PRs, issues) | `git push origin <tag>` | manual (you) |
| PyPI (`pip install tckr`) | GitHub Actions `release.yml`, trusted publisher | the tag push above |
| [gitlawb](https://gitlawb.com) (decentralized mirror) | `scripts/release-to-gitlawb.sh` (local) | manual (you) |

The gitlawb step is deliberately local — see [gitlawb mirror setup](#gitlawb-mirror-setup) below for why.

### Cutting a release

```bash
# 1. Make sure main is clean and CI is green
git checkout main && git pull

# 2. Bump version in pyproject.toml and CHANGELOG.md, commit
$EDITOR pyproject.toml CHANGELOG.md
git add pyproject.toml CHANGELOG.md
git commit -m "release: v0.2.0"

# 3. Tag and push to GitHub. This triggers PyPI publish via release.yml.
git tag v0.2.0
git push origin main
git push origin v0.2.0

# 4. Mirror to gitlawb (local only — see setup below)
scripts/release-to-gitlawb.sh v0.2.0
```

---

## gitlawb mirror setup

gitlawb is a decentralized git host where each user is a cryptographic DID and every push is signed with an Ed25519 keypair. The push protocol is custom (`gitlawb://<DID>/<repo>`) and needs:

- the `gl` CLI
- the `git-remote-gitlawb` helper (usually installed alongside `gl`)
- an Ed25519 identity at `~/.gitlawb/identity.pem`

### One-time setup

```bash
# 1. Install the gl CLI (see https://gitlawb.com/start for current install command)

# 2. Generate your identity (Ed25519 keypair stored locally)
gl identity new

# 3. Register the DID on the network
gl register

# 4. Create the tckr repo on gitlawb
gl repo create tckr --description "Async, cached crypto data layer + agent toolkit"

# 5. Add the gitlawb remote alongside `origin` (GitHub)
#    Get the URL from `gl repo show tckr` or substitute your DID below.
git remote add gitlawb gitlawb://$YOUR_DID/tckr
git remote -v   # confirm both origin (GitHub) and gitlawb are present
```

### Per-release mirror

After the GitHub push and PyPI publish have succeeded:

```bash
scripts/release-to-gitlawb.sh v0.2.0
```

That script just calls `git push gitlawb main` + `git push gitlawb <tag>`. Inlined so it's auditable:

```bash
#!/usr/bin/env bash
set -euo pipefail
TAG="${1:?usage: release-to-gitlawb.sh <tag>}"
git push gitlawb main
git push gitlawb "$TAG"
echo "mirrored $TAG to gitlawb"
```

### Why local-only?

Mirroring from GitHub Actions would require putting the Ed25519 private key into a repo secret. Anyone who compromises CI (a malicious dependency, a hijacked Action) gets your gitlawb identity — and the gitlawb identity is the whole authentication model. Local mirror keeps the key on a machine you control. The cost is one extra command per release.

If you later decide the trade-off is worth it (e.g., for a project with many maintainers where local mirror is impractical), the pattern would be:

1. Generate a *release-only* gitlawb identity (separate DID from your personal one)
2. Give that DID push access to the repo only
3. Encrypt the keypair, store in GitHub secrets, decrypt in the workflow
4. Add a `mirror-to-gitlawb` job to `release.yml` that installs `gl` and pushes the tag

This is documented but not implemented. PRs welcome.

---

## Development workflow

```bash
# Set up a venv and install editable + dev extras
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

# Run the keyless smoke tests + registry guard
pytest -m "not needs_keys" -v

# Run lint
ruff check tckr tests

# If you add a new data-source module, also:
#   - add a registry entry in tckr/registry.py
#   - add the env var(s) in tckr/settings.py
#   - update tests/test_registry.py::test_every_data_source_module_in_registry
#   - add a row to the README sources table
#   - add a keyless smoke test in tests/test_keyless_smoke.py (if applicable)
#   - add 1-3 tool wrappers in tckr/agent_toolkit/core.py
```

The registry typo-guard test (`tests/test_registry.py::test_registry_env_vars_exist_in_settings`) catches mismatches between env-var names declared in the registry vs settings.py. Run it after any registry edit.

## Adding a new agent tool

See the `add-agent-tool` skill (in the Market-Research-Comp project, but the pattern documented there now points at this package). TL;DR:

1. Add an async function in `tckr/agent_toolkit/core.py` decorated with `@register_tool(name, description, module, schema)`.
2. The `module` arg must be a key in `tckr/registry.py::REGISTRY` — drives the tier-tag prefix.
3. Functions return raw data and raise on error; per-adapter wrapping handles platform-specific envelopes.

All four adapters (Claude SDK, MCP stdio, OpenAI, LangChain) pick up new tools automatically.
