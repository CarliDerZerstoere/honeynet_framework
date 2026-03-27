# Versioning Policy

This project follows [Semantic Versioning 2.0.0](https://semver.org/).

## Version Format

```
MAJOR.MINOR.PATCH[-prerelease]
```

| Component | Increment when… |
|-----------|-----------------|
| `MAJOR`   | Breaking changes to the public API (`HoneynetOrchestrator`, `OrchestratorConfig`, `LLMConfig`, `DeploymentResult`, CLI contract) |
| `MINOR`   | Backward-compatible new features or new public fields with stable defaults |
| `PATCH`   | Backward-compatible bug fixes, documentation, or internal refactors |
| Pre-release | `-alpha`, `-beta`, `-rc.N` for pre-release versions |

## What Counts as a Breaking Change

- Removing or renaming a public field in `OrchestratorConfig` or `LLMConfig`
- Changing the default value of a safety-relevant field (e.g. `judge_fail_on_error_findings`)
- Changing the CLI subcommand name or required argument names
- Changing the `main.tofu.json` output schema in a way that requires manual state migration
- Removing a public method from `HoneynetOrchestrator`

## What Does NOT Count as a Breaking Change

- Adding new optional fields with sensible defaults
- Changing logging output format
- Internal refactors that do not change public interfaces
- Adding new CLI optional flags with defaults
- New benchmark scenarios in `benchmarks/`

## Alpha Period (`0.x.y`)

While the project is in the `0.x.y` range, **minor version bumps may include
breaking changes**.  Changes will always be documented in [CHANGELOG.md](CHANGELOG.md)
with a clear `**behaviour-breaking change**` marker.

## Releasing a New Version

1. Update version in `pyproject.toml` (`version = "X.Y.Z"`).
2. Update `honeynet_framework/__init__.py` (`__version__ = "X.Y.Z"`).
3. Move `[Unreleased]` entries to the new version section in `CHANGELOG.md`.
4. Add the compare link at the bottom of `CHANGELOG.md`.
5. Tag the commit: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`.
6. Push tag: `git push origin vX.Y.Z`.

## Current Status

**`0.1.0` — Alpha.**  Public API may change without deprecation warnings.
