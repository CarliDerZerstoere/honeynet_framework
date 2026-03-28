# Honeynet Framework

Generate and deploy realistic Docker-based honeynets from natural language prompts using LLMs.

Describe your target infrastructure in plain text, and the framework produces a fully deployable honeynet with zone-segmented networks, honeytokens, healthchecks, and OpenTofu orchestration.

## Features

- **Prompt-to-Deploy** -- describe an architecture (e.g. "zero-trust enterprise with Vault, Keycloak, three service tiers") and the pipeline generates a complete honeynet
- **Multi-Provider LLM** -- Ollama (default), OpenAI, Anthropic
- **World Model** -- structured intermediate representation with systems, zones, dependencies, secrets, and simulate blocks
- **OCI Image Introspection** -- validates images against Docker Hub / registries, fixes entrypoints, healthchecks, and required env vars automatically
- **OpenTofu Deployment** -- renders Docker provider HCL, applies with state management, supports containerized tofu execution
- **Benchmark Corpus** -- 30 scenarios across easy/medium/hard difficulty for systematic evaluation
- **Validation & Repair** -- command policy checks, dependency repair, placement validation, pluggable repair strategies
- **Deception QA** -- honeytoken coverage scoring, diversity checks, attacker detection zones
- **Multi-Phase Extraction** -- optional 3-phase pipeline (Architecture -> Config -> Enrich) for complex scenarios

## Requirements

- Python >= 3.10
- Docker (running)
- OpenTofu (`tofu` CLI) or `--docker-tofu` flag
- An LLM backend: Ollama with a model loaded, or an OpenAI/Anthropic API key

## Installation

```bash
pip install -e .
```

## Quick Start

### Deploy from a prompt

```bash
honeynet deploy -p "Create a honeynet mimicking an e-commerce platform with web server, database, and payment gateway"
```

### Using OpenAI

```bash
honeynet deploy \
  --provider openai \
  --model gpt-4o \
  --api-key $OPENAI_API_KEY \
  -p "Zero-trust enterprise with mTLS gateway, Vault, Keycloak, and monitoring"
```

### Generate World Model only (no deploy)

```bash
honeynet generate -p "Corporate email system with SMTP, IMAP, and webmail"
```

### Destroy a deployed honeynet

```bash
honeynet destroy --work-dir output
```

## Benchmarks

Run the full benchmark corpus:

```bash
honeynet benchmark run \
  --corpus benchmarks/corpus_v1 \
  --output benchmark_results \
  --provider openai --model gpt-4o --api-key $OPENAI_API_KEY
```

Filter by difficulty or single scenario:

```bash
honeynet benchmark run --corpus benchmarks/corpus_v1 --difficulty easy --dry-run
honeynet benchmark run --corpus benchmarks/corpus_v1 --scenario h04_zerotrust_enterprise
```

## CLI Reference

| Flag | Description |
|---|---|
| `--provider` | `ollama`, `openai`, `anthropic` (default: `ollama`) |
| `--model` | Model name (default: `qwen2.5-coder:32b`) |
| `--llm-url` | Override API endpoint |
| `--api-key` | API key for OpenAI/Anthropic |
| `--work-dir` | Output directory (default: `output`) |
| `--docker-tofu` | Run tofu inside a Docker container |
| `--multi-phase-extraction` | 3-phase extraction for complex scenarios |
| `--dry-run` | Benchmark: validate without deploying |
| `--semantic-judge` | Run LLM-based semantic validation after deploy |
| `--telemetry-events` | Emit JSONL pipeline events |

## Python API

```python
from honeynet_framework import HoneynetOrchestrator, OrchestratorConfig, LLMConfig

config = OrchestratorConfig(
    llm_config=LLMConfig(provider="ollama", model="qwen2.5-coder:32b"),
)
orchestrator = HoneynetOrchestrator(config)
result = await orchestrator.deploy("Create a honeynet mimicking a healthcare system")
```

## Project Structure

```
honeynet_framework/
  cli.py              # CLI entry point
  orchestrator.py     # Main pipeline orchestrator
  extraction/         # LLM-based World Model extraction
  llm.py              # Multi-provider LLM client
  deploy_compiler.py  # World Model -> Docker deployment projection
  deployer.py         # OpenTofu apply/destroy
  image_resolver.py   # OCI image validation and repair
  healthcheck_fixup.py # Healthcheck rewriting
  qa.py               # Post-deploy quality assurance
  qa_deception.py     # Honeytoken/deception scoring
  detection/          # Attacker detection (ALLOW/REVIEW/LIKELY_AGENT)
  catalog/            # Image catalog snapshots
  plugins/            # Pluggable repair strategies
benchmarks/
  corpus_v1/          # 30 benchmark scenarios (10 easy, 10 medium, 10 hard)
```

## License

MIT
