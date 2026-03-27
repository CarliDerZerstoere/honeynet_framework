"""
Honeynet Framework CLI - Command line interface for generating and deploying honeynets.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from .llm import LLMConfig
from .orchestrator import HoneynetOrchestrator, OrchestratorConfig
from .models import DeploymentStatus


def _configure_stdio_for_windows() -> None:
    """Avoid hard crashes when logs/errors include non-cp1252 characters on Windows."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(errors="replace")
            except OSError:
                # Keep default stream behavior if reconfigure is unavailable.
                pass


def _safe_console_text(value: object) -> str:
    """
    Return text that can be emitted to the active stdout encoding.
    """
    text = str(value)
    stream = getattr(sys, "stdout", None)
    encoding = getattr(stream, "encoding", None) or "utf-8"
    try:
        text.encode(encoding)
        return text
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def main():
    """Main entry point for the honeynet CLI."""
    _configure_stdio_for_windows()
    parser = argparse.ArgumentParser(
        prog="honeynet",
        description="Generate and deploy Docker-based honeynets from natural language prompts.",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # --- deploy command ---
    deploy_parser = subparsers.add_parser("deploy", help="Deploy a honeynet from a prompt")
    deploy_parser.add_argument("--prompt", "-p", type=str, help="Honeynet description prompt")
    deploy_parser.add_argument("--prompt-file", type=str, help="File containing the prompt")
    _add_common_args(deploy_parser)
    deploy_parser.add_argument(
        "--catalog-path",
        type=str,
        default=None,
        help="Optional catalog snapshot JSON (docs/catalog_snapshot.schema.json); resolves deploy.catalog_archetype",
    )

    # --- generate command ---
    gen_parser = subparsers.add_parser("generate", help="Generate a World Model without deploying")
    gen_parser.add_argument("--prompt", "-p", type=str, help="Honeynet description prompt")
    gen_parser.add_argument("--prompt-file", type=str, help="File containing the prompt")
    _add_common_args(gen_parser)

    # --- destroy command ---
    destroy_parser = subparsers.add_parser("destroy", help="Destroy a deployed honeynet")
    destroy_parser.add_argument("--work-dir", type=str, default="output", help="Working directory")
    destroy_parser.add_argument("--verbose", "-v", action="store_true")
    destroy_parser.add_argument("--docker-tofu", action="store_true", help="Run tofu inside Docker container")
    destroy_parser.add_argument("--tofu-container", type=str, default="opentofu", help="Name of the tofu Docker container")

    # --- benchmark command ---
    bench_parser = subparsers.add_parser("benchmark", help="Run benchmark scenarios")
    bench_sub = bench_parser.add_subparsers(dest="bench_action", help="Benchmark actions")
    bench_run = bench_sub.add_parser("run", help="Run benchmark corpus")
    bench_run.add_argument("--corpus", type=str, required=True, help="Path to benchmark corpus directory")
    bench_run.add_argument("--output", type=str, default="benchmark_results", help="Output directory")
    bench_run.add_argument("--prompt-variant", type=str, default="detailed", help="Prompt variant to use (default: detailed)")
    bench_run.add_argument("--difficulty", type=str, default=None, choices=["easy", "medium", "hard"])
    bench_run.add_argument("--scenario", type=str, default=None, help="Run only this scenario ID")
    bench_run.add_argument("--dry-run", action="store_true", help="Validate YAMLs without deploying")
    _add_common_args(bench_run)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # Setup logging
    level = logging.DEBUG if getattr(args, "verbose", False) else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.command == "deploy":
        asyncio.run(_run_deploy(args))
    elif args.command == "generate":
        asyncio.run(_run_generate(args))
    elif args.command == "destroy":
        asyncio.run(_run_destroy(args))
    elif args.command == "benchmark":
        if getattr(args, "bench_action", None) == "run":
            asyncio.run(_run_benchmark(args))
        else:
            bench_parser.print_help()
            sys.exit(1)


def _add_common_args(parser: argparse.ArgumentParser):
    """Add common arguments shared between deploy and generate."""
    parser.add_argument("--work-dir", type=str, default="output", help="Working directory (default: output)")

    llm = parser.add_argument_group("LLM settings")
    llm.add_argument("--provider", type=str, default="ollama", choices=["ollama", "openai", "anthropic"])
    llm.add_argument("--model", type=str, default="qwen2.5-coder:32b", help="LLM model name")
    llm.add_argument(
        "--llm-url", type=str, default=None,
        help="LLM API URL (default: auto per provider — ollama=http://localhost:11434, "
             "openai=https://api.openai.com/v1, anthropic=https://api.anthropic.com)",
    )
    llm.add_argument("--api-key", type=str, default=None, help="API key (for openai/anthropic)")
    llm.add_argument("--temperature", type=float, default=0.3, help="LLM temperature (default: 0.3)")
    llm.add_argument("--llm-timeout", type=float, default=1200.0, help="LLM timeout in seconds")
    llm.add_argument("--no-preload", action="store_true", help="Skip Ollama model preloading")

    deploy = parser.add_argument_group("Deploy settings")
    deploy.add_argument("--docker-tofu", action="store_true", help="Run tofu inside Docker container")
    deploy.add_argument("--tofu-container", type=str, default="opentofu", help="Docker container for tofu")
    deploy.add_argument("--deploy-timeout", type=int, default=900, help="Deploy timeout in seconds")
    deploy.add_argument("--max-image-repair", type=int, default=3, help="Max image repair attempts")
    deploy.add_argument("--no-cleanup", action="store_true", help="Don't cleanup old state before deploy")
    deploy.add_argument(
        "--multi-phase-extraction",
        action="store_true",
        help="Split extraction into Architecture → Config → Enrich phases (better for complex scenarios).",
    )

    obs = parser.add_argument_group("Observability")
    obs.add_argument(
        "--telemetry-events",
        action="store_true",
        help="Append JSONL pipeline events under work_dir/telemetry/events.jsonl",
    )
    obs.add_argument(
        "--semantic-judge",
        action="store_true",
        help="Run optional LLM semantic check after image repair (writes semantic_judge.json)",
    )
    obs.add_argument(
        "--judge-fail-on-error",
        action="store_true",
        help="Fail deploy when semantic judge does not pass (requires --semantic-judge)",
    )
    obs.add_argument(
        "--judge-prompt-path",
        type=str,
        default=None,
        help="Path to custom semantic-judge system prompt (optional)",
    )
    obs.add_argument(
        "--benchmark-reference",
        type=str,
        default=None,
        help="Optional benchmark scenario YAML for formal scenario-fit scoring.",
    )
    obs.add_argument(
        "--benchmark-required",
        action="store_true",
        help="Fail deploy if --benchmark-reference is not set (release-style gate).",
    )

    gating = parser.add_argument_group("Validation / repair gating")
    gating.add_argument(
        "--command-warnings-are-errors",
        action="store_true",
        help="Promote ONE_SHOT_COMMAND/FICTIONAL_SCRIPT validation warnings to hard errors.",
    )
    gating.add_argument(
        "--enable-plugins",
        action="store_true",
        help="Enable registered repair-strategy plugins (supply-chain risk; use allowlist in prod).",
    )
    gating.add_argument(
        "--plugin-allowlist",
        action="append",
        default=[],
        metavar="NAME",
        help="Repeatable: only load these repair plugin entry points (empty with --enable-plugins = allow all).",
    )

    art = parser.add_argument_group("Run artifacts")
    art.add_argument(
        "--run-artifacts",
        type=str,
        choices=["legacy", "per_run", "both"],
        default="legacy",
        help="Artifact layout: legacy = root metrics.json only; per_run = runs/<id>/ only (no root metrics.json); both = root + per-run.",
    )
    art.add_argument(
        "--emit-repair-incidents",
        action="store_true",
        help="Append repair incidents to runs/<run_id>/repair_attempts.jsonl when applicable.",
    )

    qa = parser.add_argument_group("QA runtime")
    qa.add_argument(
        "--qa-canary-check",
        action="append",
        default=[],
        metavar="PREFIX_OR_ID",
        help="Repeatable: only run QA checks whose id matches this prefix or exact id (empty = all).",
    )
    qa.add_argument(
        "--qa-tcp-extra-attempts",
        type=int,
        default=1,
        metavar="N",
        help="Extra TCP connect retries per check after transient failures (default: 1).",
    )

    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")


def _get_prompt(args) -> str:
    """Get prompt from args or stdin."""
    if args.prompt:
        return args.prompt
    if args.prompt_file:
        prompt_path = Path(args.prompt_file)
        if not prompt_path.is_file():
            print(f"Error: Prompt file not found: {prompt_path}")
            sys.exit(1)
        try:
            return prompt_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as e:
            print(f"Error: Cannot read prompt file {prompt_path}: {e}")
            sys.exit(1)

    print("Enter your honeynet description (press Ctrl+D / Ctrl+Z when done):")
    try:
        return sys.stdin.read().strip()
    except (EOFError, KeyboardInterrupt):
        print("\nAborted.")
        sys.exit(1)


_PROVIDER_DEFAULT_URLS: dict[str, str] = {
    "ollama": "http://localhost:11434",
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com",
}


def _build_config(args) -> OrchestratorConfig:
    """Build OrchestratorConfig from CLI args."""
    # Resolve provider-specific default URL when --llm-url was not explicitly set.
    llm_url = args.llm_url
    if not llm_url:
        llm_url = _PROVIDER_DEFAULT_URLS.get(args.provider, "http://localhost:11434")
    return OrchestratorConfig(
        llm_config=LLMConfig(
            provider=args.provider,
            model=args.model,
            base_url=llm_url,
            api_key=args.api_key,
            temperature=args.temperature,
            timeout=args.llm_timeout,
        ),
        work_dir=Path(args.work_dir),
        use_docker_for_tofu=args.docker_tofu,
        tofu_container=args.tofu_container,
        deploy_timeout=args.deploy_timeout,
        max_image_repair_attempts=args.max_image_repair,
        preload_ollama_model=not args.no_preload,
        cleanup_before_deploy=not args.no_cleanup,
        enable_telemetry_events=getattr(args, "telemetry_events", False),
        enable_semantic_judge=getattr(args, "semantic_judge", False),
        judge_fail_on_error=getattr(args, "judge_fail_on_error", False),
        judge_prompt_path=Path(args.judge_prompt_path)
        if getattr(args, "judge_prompt_path", None)
        else None,
        catalog_snapshot_path=Path(args.catalog_path)
        if getattr(args, "catalog_path", None)
        else None,
        benchmark_reference_path=Path(args.benchmark_reference)
        if getattr(args, "benchmark_reference", None)
        else None,
        benchmark_reference_required=getattr(args, "benchmark_required", False),
        command_warnings_are_errors=getattr(args, "command_warnings_are_errors", False),
        enable_plugins=getattr(args, "enable_plugins", False),
        plugin_allowlist=list(getattr(args, "plugin_allowlist", None) or []),
        run_artifacts_mode=getattr(args, "run_artifacts", "legacy"),
        emit_repair_incidents=getattr(args, "emit_repair_incidents", False),
        qa_canary_check_ids=_merge_qa_canary_cli_env(args),
        qa_tcp_connect_extra_attempts=int(getattr(args, "qa_tcp_extra_attempts", 1) or 0),
        multi_phase_extraction=getattr(args, "multi_phase_extraction", False),
    )


def _merge_qa_canary_cli_env(args) -> list[str]:
    cli_ids = list(getattr(args, "qa_canary_check", None) or [])
    if cli_ids:
        return cli_ids
    env_c = os.environ.get("HONEYNET_QA_CANARY_IDS", "").strip()
    if not env_c:
        return []
    return [x.strip() for x in env_c.split(",") if x.strip()]


def _validate_args(args) -> None:
    """Validate flag combinations and ranges; exit with clear message on error."""
    # --judge-fail-on-error requires --semantic-judge
    if getattr(args, "judge_fail_on_error", False) and not getattr(args, "semantic_judge", False):
        print("Error: --judge-fail-on-error requires --semantic-judge to be enabled.")
        sys.exit(1)

    if getattr(args, "benchmark_required", False) and not getattr(args, "benchmark_reference", None):
        print("Error: --benchmark-required requires --benchmark-reference to be set.")
        sys.exit(1)

    # Provider-specific URL plausibility
    provider = getattr(args, "provider", "ollama")
    llm_url = getattr(args, "llm_url", "")
    if provider in ("openai", "anthropic") and "localhost" in (llm_url or ""):
        print(
            f"Warning: --provider={provider} with --llm-url={llm_url} "
            f"looks like an Ollama URL. Did you mean to use the default "
            f"{'https://api.openai.com/v1' if provider == 'openai' else 'https://api.anthropic.com'}?"
        )

    # Numeric range checks
    temp = getattr(args, "temperature", 0.3)
    if not (0.0 <= temp <= 2.0):
        print(f"Error: --temperature must be between 0.0 and 2.0 (got {temp})")
        sys.exit(1)

    timeout = getattr(args, "deploy_timeout", 900)
    if timeout < 30:
        print(f"Error: --deploy-timeout must be at least 30 seconds (got {timeout})")
        sys.exit(1)


async def _run_deploy(args):
    """Run the deploy command."""
    _validate_args(args)  # Validate before prompt input to avoid wasted typing
    prompt = _get_prompt(args)
    if not prompt:
        print("Error: No prompt provided.")
        sys.exit(1)

    config = _build_config(args)
    orchestrator = HoneynetOrchestrator(config)

    print(f"\nDeploying honeynet to {config.work_dir}...")
    print(f"LLM: {config.llm_config.provider}/{config.llm_config.model}\n")

    result = await orchestrator.deploy(prompt)

    if result.success:
        print(f"\n{'='*60}")
        if result.strict_success:
            print(f"Deployment successful!")
        else:
            print(f"Deployment completed with warnings (status: {result.status.value})")
        print(f"Status: {result.status.value}")
        print(f"Containers: {result.running_containers}/{result.expected_containers}")
        if result.summary:
            print(f"\n{_safe_console_text(result.summary)}")

        # Display metrics
        if result.metrics:
            d = result.metrics.get("deployability", {})
            s = result.metrics.get("scenario_fit", {})
            print(f"\n--- Deployment Metrics ---")
            print(f"  Container Start Rate:  {d.get('running_containers', 0)}/{d.get('expected_containers', 0)} ({d.get('container_start_rate', 0):.0%})")
            print(f"  Health Check Rate:     {d.get('health_checks_passed', 0)}/{d.get('health_checks_total', 0)} ({d.get('health_check_rate', 0):.0%})")
            print(f"  Zones: {s.get('zone_count', 0)}  Systems: {s.get('system_count', 0)}")
            print(f"  Model Dep Rate:        {s.get('model_dep_rate', 0):.0%}")

        print(f"\nArtifacts in: {config.work_dir}")
        print(f"  - world_model.yaml")
        print(f"  - main.tofu.json")
        print(f"  - metrics.json")
        print(f"  - qa_report.json")
        print(f"{'='*60}")
    else:
        print(f"\n{'='*60}")
        print(f"Deployment FAILED")
        if result.run_id:
            print(f"  run_id: {result.run_id}")
        if result.failure_stage:
            print(f"  failure_stage: {result.failure_stage}")
        for error in result.errors:
            print(f"  ERROR: {_safe_console_text(error)}")
        if result.warnings:
            for warning in result.warnings:
                print(f"  WARNING: {_safe_console_text(warning)}")
        print(f"{'='*60}")
        sys.exit(1)


async def _run_generate(args):
    """Run the generate command (World Model only, no deploy)."""
    _validate_args(args)
    prompt = _get_prompt(args)
    if not prompt:
        print("Error: No prompt provided.")
        sys.exit(1)

    config = _build_config(args)
    orchestrator = HoneynetOrchestrator(config)

    print(f"Generating World Model...")
    world_model = await orchestrator.generate(prompt)

    sys_count = len(world_model.deployable_systems)
    zone_count = len(world_model.zones)
    print(f"\nGenerated: {sys_count} systems in {zone_count} zones")
    print(f"Saved to: {config.work_dir}/world_model.yaml")


async def _run_destroy(args):
    """Destroy a deployed honeynet."""
    from .deployer import TerraformDeployer, DeployerConfig, find_tofu_binary

    work_dir = Path(args.work_dir)
    if not (work_dir / "main.tofu.json").exists():
        print(f"No honeynet found in {work_dir}")
        sys.exit(1)

    deployer = TerraformDeployer(DeployerConfig(
        work_dir=work_dir,
        tofu_binary=find_tofu_binary(),
        use_docker=getattr(args, "docker_tofu", False),
        docker_container=getattr(args, "tofu_container", "opentofu"),
    ))

    print(f"Destroying honeynet in {work_dir}...")
    result = await deployer.destroy()
    if result.success:
        print("Honeynet destroyed successfully.")
    else:
        print(f"Destroy failed: {result.stderr}")
        sys.exit(1)


async def _run_benchmark(args):
    """Run benchmark scenarios."""
    from .benchmark_runner import run_benchmark_corpus

    corpus_dir = Path(args.corpus)
    if not corpus_dir.exists():
        print(f"Corpus directory not found: {corpus_dir}")
        sys.exit(1)

    output_dir = Path(args.output)
    config = _build_config(args)

    print(f"Running benchmarks from {corpus_dir}...")
    if args.dry_run:
        print("(dry-run mode — validating YAMLs only, no deployment)")

    results = await run_benchmark_corpus(
        corpus_dir=corpus_dir,
        output_dir=output_dir,
        config=config,
        prompt_variant=args.prompt_variant,
        difficulty_filter=args.difficulty,
        scenario_filter=args.scenario,
        dry_run=args.dry_run,
    )

    passed = sum(1 for r in results if not r.dry_run and r.benchmark_pass and r.deploy_success)
    total = sum(1 for r in results if not r.dry_run)
    dry = sum(1 for r in results if r.dry_run)
    dry_valid = sum(1 for r in results if r.dry_run and r.dry_run_valid)

    print(f"\n{'=' * 60}")
    if args.dry_run:
        print(f"Dry-run: {dry_valid}/{dry} scenarios valid")
    else:
        print(f"Benchmark: {passed}/{total} scenarios passed")
    print(f"Report: {output_dir / 'benchmark_report.html'}")
    print(f"{'=' * 60}")

    if total > 0 and passed < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
