"""
LLM prompts for World Model extraction.

Single-call strategy: one prompt generates the complete World Model YAML.
Scale guidance is computed dynamically from the user's prompt complexity.
"""

import re

from ..image_introspector import _INTERPRETERS

EXTRACTION_SYSTEM_MESSAGE = """\
You are a honeynet infrastructure architect specializing in building deceptive
network environments that look indistinguishable from real production infrastructure.

Given a user's description, you generate a complete World Model as **valid YAML**.

Output ONLY raw YAML — no markdown fences, no prose, no explanations.

The YAML must follow this structure:

organization:
  name: "<company/org name inferred from prompt>"

zones:
  <zone_name>:
    network_name: "<lowercase_underscore_name>"
    driver: "bridge"
    internal: true|false

systems:
  <system_name>:
    kind: "web|database|queue|runtime|storage|identity|monitor|infra|unknown"
    deploy:
      image: "<real Docker Hub image>:<specific_tag>"
      zone: "<zone_name>"
      ports:
        - <port_int>
      env:
        - "KEY=value"
      depends_on:
        - "<other_system_name>"
      command: null
      healthcheck:
        test: ["CMD", "<check_command>", "<args>"]
        interval: "30s"
        timeout: "10s"
        retries: 3
        start_period: "30s"
    simulate:
      hostname: "<realistic hostname>"
      role: "<role description>"

Rules:
1. IMAGE SELECTION — USE THE OCI CONFIG DATA PROVIDED:

   The user message includes an IMAGE REGISTRY section with pre-fetched OCI configs
   for scenario-relevant images. USE THIS DATA directly — do not call fetch_image_config
   for images already listed there. Only call tools for images NOT in the registry.

   For each image, read its OCI config and apply these rules:
   a) COMMAND: If entrypoint is a daemon binary → set command: null.
      If cmd is [] (empty) → the image NEEDS a subcommand via the command field.
   b) INTERPRETER IMAGES (python, node, ruby, golang, php) — CRITICAL:
      These contain ONLY a language runtime — NO application code exists.
      There are NO .py, .js, .rb, .go, .sh files inside these images.
      NEVER reference script filenames (app.py, server.js, worker.rb, etc.)
      because they DO NOT EXIST and the container WILL crash.
      Instead, choose an APPLICATION image that natively serves the required role:
      - For APIs/web services → nginx, caddy, httpd, traefik (serve HTTP natively)
      - For workers/processors → a message broker client or daemon image
      - For background tasks → an image with a built-in scheduler/daemon
      Only use interpreter images with a BUILT-IN module command
      (e.g. python -m http.server 8080, ruby -run -e httpd).
      Images marked ⚠ BASE RUNTIME in the registry below must follow this rule.
   c) ENV: If required_env lists variables → you MUST set them in the env section
      with realistic honeypot values (weak passwords, demo tokens).
   d) PORTS: Use the exposed ports from the OCI config for the ports list.
   e) HEALTHCHECK: Set a healthcheck for every system. Use the exposed ports to
      determine the right check. For HTTP ports use wget, for TCP-only use nc.
      Prefer service-native CLIs when available (pg_isready, redis-cli, etc.).

   NEVER invent images that don't exist. For images not in the registry,
   call validate_docker_image to verify they exist.
   NEVER use proprietary/commercial images requiring authentication.
2. Every system must reference a zone defined in the zones section.
3. Create realistic network segmentation. The number of zones should reflect the
   scenario's architecture — a simple app may need 2-3 zones, a multi-tier
   enterprise or research grid may need 5-8 zones with clear trust boundaries.
4. depends_on MUST list every system this container needs at startup:
   - If env vars reference another system's hostname (e.g. POSTGRES_HOST=db_server,
     REDIS_URL=redis://cache:6379), that system MUST be in depends_on.
   - If a web frontend proxies to a backend, the backend MUST be in depends_on.
   - If a service uses a message queue, the queue MUST be in depends_on.
   Review your env vars AFTER generating them — any system name found in an env
   value should appear in that system's depends_on list.
5. Ports are internal container ports (integers only).
6. env values must include realistic defaults the container needs to start.
   Call fetch_image_config to discover which env vars have empty defaults —
   those are REQUIRED and must be set or the container will exit immediately.
   For databases, set weak honeypot passwords (e.g. admin123, password).
   For services requiring API keys or tokens, use demo/default values.
7. Zone names and system names must be lowercase with underscores only.
8. Set internal: false for DMZ/public-facing zones, internal: true for others.
9. Every system MUST include a healthcheck appropriate for its service type.
   Call fetch_image_config to discover what the image ships. Use service-native
   CLIs when available (pg_isready, redis-cli, mysqladmin, rabbitmq-diagnostics).
   IMPORTANT: Many minimal images (traefik, minio, envoy, scratch-based Go binaries)
   have NEITHER wget NOR curl. For these, use TCP checks or service-native CLIs.
   Generic patterns (in order of preference):
   - Service-native: pg_isready, redis-cli ping, mysqladmin ping, etc.
   - HTTP with fallback: ["CMD-SHELL", "curl -sf http://localhost:<port>/ || wget -qO- http://localhost:<port>/ || exit 1"]
   - TCP-only services: ["CMD-SHELL", "nc -z localhost <port> || exit 1"]
   - If unsure: ["CMD-SHELL", "true"] as a minimal always-passing check.
   Set start_period to "30s" for databases and heavy services, "10s" for lightweight ones.
10. CRITICAL — every container MUST run as a **long-lived daemon** process.
    The Docker provider expects PID 1 to stay running. Containers that exit
    immediately (one-shot jobs, batch scripts) will cause deployment failures.
    - NEVER use one-shot commands like "restic backup", "pg_dump", "mysqldump",
      "tar", "cp", "rsync" as the container's main command.
    - For backup/job services, use daemon images with a management API or built-in scheduler.
    - If a service concept is inherently a job, model it as a daemon.
11. The `command` field should ONLY be set when overriding the image's default entrypoint.
    Call fetch_image_config to check if the image has a daemon Entrypoint — if yes,
    set command: null. Only set a command when the image has no Entrypoint or when
    the default Entrypoint needs arguments.
    NEVER reference scripts that don't exist in the image.
12. The healthcheck must match the actual service the container runs.
    A backup container should NOT have a web healthcheck on port 8000.
    If unsure, use: ["CMD-SHELL", "true"] as a minimal always-passing check.
13. ZONE PLACEMENT RULES:
    - Database and storage services (postgres, mysql, redis, elasticsearch, minio, etc.)
      MUST be placed in internal zones (internal: true). Never expose data stores
      to public/DMZ zones.
    - Message brokers (kafka, rabbitmq, nats) MUST be in internal zones.
    - Only web frontends, reverse proxies, and API gateways belong in public zones.
    - Monitoring infrastructure (prometheus, grafana, loki) should be in internal
      or dedicated monitoring zones.
    - Identity services (keycloak, vault, LDAP) MUST be in internal zones.

REALISM GUIDELINES (critical for a convincing honeynet):
- Don't just create one container per mentioned technology. Real infrastructure has
  DEPTH: a database tier has a primary + replica, a web tier has a reverse proxy +
  multiple app servers, a monitoring stack has collectors + dashboards + alerting.
- Include supporting infrastructure that real environments always have:
  * Monitoring/observability (Prometheus, Grafana, log collectors)
  * Admin/management interfaces (phpMyAdmin, admin dashboards, config UIs)
  * Reverse proxies, load balancers, API gateways in front of services
  * Service discovery / coordination (Consul, etcd, ZooKeeper) where appropriate
- For honeypots specifically, include bait systems:
  * Exposed admin panels with weak credentials
  * Intentionally "misconfigured" services (debug mode on, default passwords)
  * Systems with interesting-looking data (backup servers, file shares, credential stores)
- Use realistic hostnames that match the scenario (e.g., for a research grid:
  login-portal.grid.example.edu, gpu-scheduler-01.compute.internal, etc.)
- Create inter-system dependencies that reflect how real systems communicate.
"""

IMAGE_REPAIR_SYSTEM_MESSAGE = """\
You are a Docker image specialist. Some images in a honeynet World Model
could not be found on Docker Hub. For each failed image, suggest a valid
replacement that serves the same purpose.

Common reasons images fail and how to fix them:
- Namespace migration: many official images moved to org namespaces.
  vault → hashicorp/vault, consul → hashicorp/consul, etc.
- Tag doesn't exist: pick a known stable tag (e.g. "latest" or a major version).
  Call validate_docker_image to verify the replacement exists.
- Image renamed: try alternate registries or namespaces.
- PROPRIETARY/PRIVATE IMAGE: If the error says "unauthorized" or "authentication required",
  this image is NOT publicly available. Do NOT suggest another tag of the same image.
  Instead, find an open-source alternative that serves the same purpose.
  Call fetch_image_config on your proposed replacement to verify it has a daemon
  entrypoint and discover its required env vars.

For EVERY replacement you suggest:
1. Call validate_docker_image to confirm it exists.
2. Call fetch_image_config to confirm it runs as a daemon (has a non-interpreter Entrypoint).

Output ONLY raw YAML — no markdown fences, no prose.

Format:
replacements:
  "<old_image_ref>": "<new_valid_image_ref>"
"""


def _estimate_scale(user_request: str) -> tuple[int, int, int, int]:
    """Estimate the appropriate system and zone count from the user request.

    Returns (min_systems, max_systems, min_zones, max_zones).
    """
    text = user_request.lower()
    words = text.split()
    word_count = len(words)

    # Count distinct technology/service mentions
    tech_patterns = [
        r'\b(?:postgres(?:ql)?|mysql|mariadb|mongo(?:db)?|redis|memcached|'
        r'elasticsearch|opensearch|clickhouse|cassandra|cockroach|sqlite|'
        r'influxdb|timescaledb|neo4j|couchdb|dynamodb)\b',
        r'\b(?:nginx|apache|httpd|traefik|haproxy|caddy|envoy|istio)\b',
        r'\b(?:rabbitmq|kafka|nats|pulsar|zeromq|activemq|celery)\b',
        r'\b(?:grafana|prometheus|kibana|jaeger|zipkin|datadog|loki|fluentd|logstash)\b',
        r'\b(?:vault|consul|etcd|zookeeper|keycloak|ldap|openldap|freeipa)\b',
        r'\b(?:jenkins|gitlab|gitea|drone|argo|tekton|github)\b',
        r'\b(?:kubernetes|docker|podman|nomad|swarm|mesos)\b',
        r'\b(?:minio|ceph|gluster|nfs|swift|s3)\b',
        r'\b(?:wordpress|nextcloud|wiki|jira|confluence|mattermost|rocketchat)\b',
        r'\b(?:phpmyadmin|pgadmin|adminer|portainer|webmin)\b',
        r'\b(?:node|python|java|php|ruby|golang|rust|django|flask|spring|express)\b',
        r'\b(?:gpu|cuda|tensorflow|pytorch|jupyter|slurm|condor|pbs)\b',
    ]

    tech_count = 0
    for pattern in tech_patterns:
        matches = set(re.findall(pattern, text))
        tech_count += len(matches)

    # Count explicit service/component mentions (phrases like "job scheduler",
    # "login portal", "dataset index", etc.)
    service_phrases = re.findall(
        r'(?:login portal|admin panel|dashboard|scheduler|allocat|federati|'
        r'data.?base|cache|storage|index|proxy|gateway|load.?balanc|'
        r'monitor|collector|log.?aggregat|backup|file.?share|'
        r'credential|certificate|identity|authentication|'
        r'web.?server|app.?server|api.?server|mail|email|dns|ntp|'
        r'queue|broker|worker|node|cluster|replica|primary|secondary|'
        r'compute|gpu|notebook|portal|registry|repository)',
        text
    )
    service_count = len(set(service_phrases))

    # Count explicit mentions of "multi-", "distributed", "federated", "cluster"
    scale_multipliers = len(re.findall(
        r'\b(?:multi|distributed|federated|cluster|grid|fleet|farm|'
        r'high.?availability|redundant|replicated|scaled|petabyte|'
        r'large.?scale|enterprise|institution|organization)\b',
        text
    ))

    # Base scale from raw signal count
    total_signals = tech_count + service_count
    # Low floor (3) so simple prompts with 1-2 signals stay small;
    # hard scenarios with many signals still scale up naturally.
    base_systems = max(3, total_signals * 2)

    # Scale multiplier for complex scenarios
    complexity_factor = 1.0
    if scale_multipliers >= 3:
        complexity_factor = 1.8
    elif scale_multipliers >= 1:
        complexity_factor = 1.4

    # Long prompts generally describe complex environments
    if word_count > 80:
        complexity_factor *= 1.3
    elif word_count > 40:
        complexity_factor *= 1.1

    min_systems = max(2, int(base_systems * 0.8))
    max_systems = max(min_systems + 3, int(base_systems * complexity_factor * 1.2))
    max_systems = min(max_systems, 50)  # Hard cap
    # Ensure min never exceeds max after capping
    min_systems = min(min_systems, max_systems)

    # Zone estimation
    if total_signals <= 5:
        min_zones, max_zones = 2, 4
    elif total_signals <= 15:
        min_zones, max_zones = 3, 6
    else:
        min_zones, max_zones = 4, 8

    if scale_multipliers >= 2:
        min_zones = max(min_zones, 4)
        max_zones = max(max_zones, 7)

    return min_systems, max_systems, min_zones, max_zones


def _format_oci_configs(image_configs: dict[str, dict]) -> str:
    """Format pre-fetched OCI configs for inclusion in the extraction prompt."""
    if not image_configs:
        return ""

    lines = [
        "\nIMAGE REGISTRY WITH OCI CONFIGS (from live registry — use this data, do NOT guess):",
        "For each image: entrypoint, default cmd, required env vars, and exposed ports.",
        "If cmd is empty, the image needs a subcommand argument via the command field.",
        "If required_env lists variables, you MUST set them in the env section.\n",
    ]
    for image_ref, cfg in image_configs.items():
        if "error" in cfg:
            continue
        ep = cfg.get("entrypoint") or []
        cmd = cfg.get("cmd") or []
        env_list = cfg.get("env") or []
        ports = cfg.get("exposed_ports") or []
        required = [e.split("=")[0] for e in env_list if "=" in e and not e.split("=", 1)[1].strip()]

        lines.append(f"  {image_ref}:")
        lines.append(f"    entrypoint: {ep}")
        if cmd:
            lines.append(f"    cmd: {cmd}")
        else:
            lines.append(f"    cmd: []  # NEEDS subcommand via command field")
        if required:
            lines.append(f"    required_env: {required}  # MUST be set or container exits")
        if ports:
            lines.append(f"    ports: {ports}")
        # Classify image so the LLM knows what it can safely use
        if ep:
            first_bin = ep[0].rsplit("/", 1)[-1].lower()
            if first_bin in _INTERPRETERS:
                lines.append(f"    ⚠ BASE RUNTIME — no app code inside, avoid for service roles")
            else:
                lines.append(f"    ✓ READY-TO-RUN daemon — use with command: null")
    lines.append("")
    return "\n".join(lines)


def build_extraction_prompt(
    user_request: str,
    relevant_images: list[str] | None = None,
    image_configs: dict[str, dict] | None = None,
    scenario_context: str | None = None,
) -> tuple[str, str, int]:
    """Build (system_message, user_message, estimated_max_systems) for world model extraction.

    Parameters
    ----------
    user_request :
        The raw user scenario description.
    relevant_images :
        Optional list of verified Docker image references pre-selected for
        this scenario via BM25 retrieval.
    image_configs :
        Optional pre-fetched OCI image configs keyed by image reference.
        When provided, real Entrypoint/Cmd/Env/ExposedPorts data is injected
        directly into the prompt so the LLM doesn't need tool calls.
    scenario_context :
        Optional natural-language paragraph describing what the deployment
        should contain (services, zones, dependencies, constraints).  Generated
        from a benchmark reference when running benchmarks.

    Returns
    -------
    (system_message, user_message, estimated_max_systems)
        The third element is the estimated upper bound of systems so callers
        can size the LLM token budget dynamically.
    """
    # Estimate scale from user request AND scenario_context (if provided) to
    # ensure benchmark-enriched prompts produce enough systems.  Without this,
    # concise prompts (30 words) yield max_systems=14 even when the scenario
    # context describes 11+ required services — leaving no room for the
    # realistic companion systems that make a honeynet convincing.
    scale_input = user_request
    if scenario_context:
        scale_input = user_request + "\n" + scenario_context
    min_sys, max_sys, min_zones, max_zones = _estimate_scale(scale_input)

    # Scale the number of companion systems proportionally to scenario complexity.
    # Small scenarios (3-4 required services) need only 1-3 companions.
    # Large scenarios (9-11 required services) need 5-10 companions.
    # The extra_ratio scales with max_sys to create a natural distribution:
    #   easy (max_sys ~11): +30% → 5-8 total containers
    #   medium (max_sys ~15): +40% → 8-14 total containers
    #   hard (max_sys ~30+): +50% → 15-25 total containers
    if max_sys <= 12:
        extra_ratio = 0.3   # small: add ~30% companions
    elif max_sys <= 20:
        extra_ratio = 0.4   # medium: add ~40% companions
    else:
        extra_ratio = 0.5   # large: add ~50% companions

    min_with_extras = max(min_sys, int(min_sys * (1 + extra_ratio)))
    max_with_extras = max(max_sys, int(max_sys * (1 + extra_ratio)))
    max_with_extras = min(max_with_extras, 50)  # hard cap
    min_with_extras = min(min_with_extras, max_with_extras)

    # Quantify the companion expectation for the prompt
    companion_count = max_with_extras - max_sys
    scale_guidance = (
        f"\nSCALE GUIDANCE for this request:\n"
        f"- Generate between {min_with_extras} and {max_with_extras} systems.\n"
        f"- Create between {min_zones} and {max_zones} zones.\n"
        f"- Every mentioned technology or service should have at least one dedicated system.\n"
        f"- THEN add approximately {companion_count} companion systems that real production\n"
        f"  infrastructure always has (monitoring, admin UIs, log collectors, caches, proxies).\n"
        f"- CRITICAL: Every system (including companions) MUST have deploy.zone set to an existing zone name.\n"
        f"- Do NOT exceed {max_with_extras} systems — keep the deployment focused and realistic.\n"
    )
    # Use max_with_extras for token budget calculation
    max_sys = max_with_extras

    # Prefer OCI configs (real data) over bare image names
    if image_configs:
        image_hint = _format_oci_configs(image_configs)
    elif relevant_images:
        image_hint = (
            "\nSCENARIO-RELEVANT VERIFIED IMAGES (prefer these over guessing):\n"
            + "\n".join(f"  - {img}" for img in relevant_images)
            + "\nThese images are confirmed to exist on Docker Hub.\n"
        )
    else:
        image_hint = ""

    context_block = ""
    if scenario_context:
        context_block = f"\n{scenario_context}\n"

    user_msg = (
        f"USER REQUEST:\n{user_request}\n"
        f"{context_block}"
        f"{scale_guidance}"
        f"{image_hint}\n"
        f"Generate the complete World Model YAML."
    )
    return EXTRACTION_SYSTEM_MESSAGE, user_msg, max_sys


# ===================================================================== #
# Multi-Phase Extraction Prompts
# ===================================================================== #

PHASE1_ARCHITECTURE_SYSTEM_MESSAGE = """\
You are a honeynet infrastructure architect. Your ONLY task is to design the
network topology: zones, systems, and their relationships.

Output ONLY raw YAML — no markdown fences, no prose, no explanations.

The YAML must follow this structure:

organization:
  name: "<company/org name inferred from prompt>"

zones:
  <zone_name>:
    internal: true|false

systems:
  <system_name>:
    kind: "web|database|queue|runtime|storage|identity|monitor|infra|unknown"
    zone: "<zone_name>"
    depends_on:
      - "<other_system_name>"

Rules:
1. Output ONLY the skeleton above. NO images, NO env, NO ports, NO healthchecks,
   NO volumes, NO simulate blocks. Those will be added in a later phase.
2. Every system must reference a zone defined in the zones section.
3. Create realistic network segmentation:
   - DMZ/public zones (internal: false) for web frontends, API gateways, reverse proxies
   - Internal zones (internal: true) for databases, caches, message brokers, identity services
   - Separate zones for monitoring/ops, data, security, etc. as the scenario requires
4. depends_on MUST list every system this container needs at startup:
   - Databases that a service reads from
   - Caches that a service connects to
   - Message brokers that a service publishes/subscribes to
   - Identity services that authenticate requests
   Review your dependency graph — if A uses B's data, A depends_on B.
5. Zone names and system names must be lowercase with underscores only.
6. ZONE PLACEMENT RULES (CRITICAL):
   - Database and storage services MUST be in internal zones. NEVER in DMZ/public.
   - Message brokers (kafka, rabbitmq, nats) MUST be in internal zones.
   - Identity services (keycloak, vault, LDAP) MUST be in internal zones.
   - Only web frontends, reverse proxies, and API gateways belong in public zones.
   - Monitoring infrastructure should be in dedicated internal zones.

REALISM GUIDELINES:
- Don't just create one system per mentioned technology. Real infrastructure has DEPTH:
  a database tier has a primary + admin UI, a web tier has a proxy + app servers,
  monitoring has collectors + dashboards + alerting.
- Include supporting infrastructure that real environments always have:
  * Monitoring/observability (Prometheus, Grafana, log collectors)
  * Admin/management interfaces (phpMyAdmin, pgAdmin, Portainer)
  * Reverse proxies, load balancers, API gateways in front of services
  * Service discovery / coordination where appropriate
- For honeypots specifically, include bait systems:
  * Exposed admin panels with weak credentials
  * Systems with interesting-looking data (backup servers, file shares)
- Create inter-system dependencies that reflect how real systems communicate.
"""


def build_phase1_architecture_prompt(
    user_request: str,
    scenario_context: str | None = None,
) -> tuple[str, str, int]:
    """Build (system_msg, user_msg, estimated_max_systems) for Phase 1: Architecture."""
    scale_input = user_request
    if scenario_context:
        scale_input = user_request + "\n" + scenario_context
    min_sys, max_sys, min_zones, max_zones = _estimate_scale(scale_input)

    if max_sys <= 12:
        extra_ratio = 0.3
    elif max_sys <= 20:
        extra_ratio = 0.4
    else:
        extra_ratio = 0.5
    min_with_extras = max(min_sys, int(min_sys * (1 + extra_ratio)))
    max_with_extras = max(max_sys, int(max_sys * (1 + extra_ratio)))
    max_with_extras = min(max_with_extras, 50)
    min_with_extras = min(min_with_extras, max_with_extras)

    companion_count = max_with_extras - max_sys
    scale_guidance = (
        f"\nSCALE GUIDANCE:\n"
        f"- Generate between {min_with_extras} and {max_with_extras} systems.\n"
        f"- Create between {min_zones} and {max_zones} zones.\n"
        f"- Every mentioned technology or service needs at least one dedicated system.\n"
        f"- Add approximately {companion_count} companion systems for realism.\n"
        f"- CRITICAL: Every system MUST reference a zone defined in the zones section.\n"
        f"- Do NOT exceed {max_with_extras} systems.\n"
    )

    context_block = ""
    if scenario_context:
        context_block = f"\n{scenario_context}\n"

    user_msg = (
        f"USER REQUEST:\n{user_request}\n"
        f"{context_block}"
        f"{scale_guidance}\n"
        f"Generate ONLY the topology skeleton YAML (organization, zones, systems with kind/zone/depends_on).\n"
        f"Do NOT include images, env, ports, healthchecks, or simulate blocks."
    )
    return PHASE1_ARCHITECTURE_SYSTEM_MESSAGE, user_msg, max_with_extras


PHASE2_CONFIG_SYSTEM_MESSAGE = """\
You are a Docker infrastructure specialist configuring honeynet containers.

For each system listed below, provide its deployment configuration.
Output ONLY raw YAML — no markdown fences, no prose, no explanations.

Output format:
systems:
  <system_name>:
    deploy:
      image: "<real Docker Hub image>:<specific_tag>"
      ports:
        - <port_int>
      env:
        - "KEY=value"
      command: null
      healthcheck:
        test: ["CMD", "<check_command>", "<args>"]
        interval: "30s"
        timeout: "10s"
        retries: 3
        start_period: "30s"
      volumes: []

Rules:
1. IMAGE SELECTION — USE THE OCI CONFIG DATA PROVIDED:
   For each image, read its OCI config and apply these rules:
   a) COMMAND: If entrypoint is a daemon binary → set command: null.
      If cmd is [] (empty) → the image NEEDS a subcommand via the command field.
   b) INTERPRETER IMAGES (python, node, ruby, golang, php) — CRITICAL:
      These contain ONLY a language runtime — NO application code.
      NEVER reference scripts (app.py, server.js) — they DO NOT EXIST.
      Instead, choose an APPLICATION image that natively serves the required role.
   c) ENV: If required_env lists variables → you MUST set them with realistic
      honeypot values (weak passwords like admin123, demo tokens).
   d) PORTS: Use the exposed ports from the OCI config.
   e) HEALTHCHECK: Set a healthcheck for every system. Prefer service-native CLIs
      (pg_isready, redis-cli, mysqladmin). For HTTP: use wget or curl.
      For TCP-only: ["CMD-SHELL", "nc -z localhost <port> || exit 1"]
      If unsure: ["CMD-SHELL", "true"]

2. Every container MUST run as a long-lived daemon process.
   NEVER use one-shot commands like pg_dump, tar, cp, rsync as the main command.

3. The command field should ONLY be set when overriding the image's default.
   If the image has a daemon Entrypoint → set command: null.

4. NEVER invent images that don't exist. For images not in the registry,
   call validate_docker_image to verify they exist.

5. env values must include realistic defaults the container needs to start.
   For databases, set weak honeypot passwords (e.g. admin123, password).
"""


def build_phase2_config_prompt(
    batch_systems: list[dict],
    image_configs: dict[str, dict],
    batch_index: int,
    total_batches: int,
) -> tuple[str, str]:
    """Build (system_msg, user_msg) for Phase 2: Config (one batch)."""
    lines = [
        f"SYSTEMS TO CONFIGURE (batch {batch_index + 1}/{total_batches}):\n",
        "These systems are part of a honeynet. Their topology is decided.\n",
    ]
    for sys_info in batch_systems:
        name = sys_info["name"]
        kind = sys_info.get("kind", "unknown")
        zone = sys_info.get("zone", "")
        deps = sys_info.get("depends_on", [])
        lines.append(f"  system: {name}")
        lines.append(f"    kind: {kind}")
        lines.append(f"    zone: {zone}")
        if deps:
            lines.append(f"    depends_on: {deps}")
        lines.append("")

    # Add OCI configs if available
    if image_configs:
        oci_block = _format_oci_configs(image_configs)
        if oci_block:
            lines.append(oci_block)

    lines.append(
        "For each system above, output YAML with deploy config "
        "(image, ports, env, command, healthcheck, volumes)."
    )
    return PHASE2_CONFIG_SYSTEM_MESSAGE, "\n".join(lines)


PHASE3_ENRICH_SYSTEM_MESSAGE = """\
You are a honeynet narrative specialist. Your task is to add simulation metadata
and optionally suggest companion systems that enhance realism.

Output ONLY raw YAML — no markdown fences, no prose, no explanations.

Output format:
systems:
  <existing_system_name>:
    simulate:
      hostname: "<realistic FQDN matching the organization>"
      role: "<role description>"
      issues: []
      secrets: []
      behaviors: []
      services: []

secrets:
  <secret_name>:
    type: "<credential|token|api_key|certificate>"
    value: "<realistic weak/demo value>"

companion_systems:
  <new_system_name>:
    kind: "<kind>"
    zone: "<zone_name from existing zones>"
    depends_on: [<existing systems>]
    deploy:
      image: "<real Docker image:tag>"
      ports: [<port>]
      env: ["KEY=value"]
      command: null
      healthcheck:
        test: ["CMD-SHELL", "true"]
        interval: "30s"
        timeout: "10s"
        retries: 3
    simulate:
      hostname: "<realistic FQDN>"
      role: "<role>"

Rules:
1. For EVERY existing system, generate a simulate block with a realistic hostname
   that matches the organization (e.g. db01.core.acmebank.internal).
2. Generate secrets for shared credentials (admin passwords, API keys, DB credentials).
3. Companion systems are OPTIONAL but encouraged. Good candidates:
   - Admin UIs for databases (pgAdmin, phpMyAdmin)
   - Log collectors (Fluentd, Filebeat)
   - Monitoring dashboards (Grafana) if not already present
   - Backup servers, file shares with interesting-sounding names
   - Debug/staging endpoints left "accidentally" exposed
4. Companion systems MUST use real Docker images and existing zones.
5. CRITICAL: Every companion system MUST have zone set to one of the existing zone names.
   Systems without a zone will fail validation and break the deployment.
6. Use realistic hostnames that match the scenario context.
"""


def build_phase3_enrich_prompt(
    systems_summary: list[dict],
    user_request: str,
    org_name: str = "",
    zone_names: list[str] | None = None,
) -> tuple[str, str]:
    """Build (system_msg, user_msg) for Phase 3: Enrich."""
    lines = [f"ORGANIZATION: {org_name or 'Unknown Corp'}\n"]

    if zone_names:
        lines.append(f"ZONES: {', '.join(zone_names)}\n")

    lines.append("EXISTING SYSTEMS (already configured):")
    for sys_info in systems_summary:
        name = sys_info["name"]
        kind = sys_info.get("kind", "unknown")
        zone = sys_info.get("zone", "")
        image = sys_info.get("image", "")
        lines.append(f"  - {name} (kind: {kind}, zone: {zone}, image: {image})")

    lines.append(f"\nUSER'S ORIGINAL REQUEST:\n{user_request}")
    lines.append(
        "\nAdd simulate blocks for each system, generate secrets, "
        "and optionally add companion systems."
    )
    return PHASE3_ENRICH_SYSTEM_MESSAGE, "\n".join(lines)


def build_image_repair_prompt(
    failed_images: list[dict],
) -> tuple[str, str]:
    """Build (system_message, user_message) for image repair.

    failed_images: list of {"image": str, "status": str, "message": str}
    """
    lines = ["The following Docker images could not be found or pulled:\n"]
    for entry in failed_images:
        if not isinstance(entry, dict):
            lines.append(f"- (invalid entry: {str(entry)[:100]})")
            continue
        lines.append(f"- {entry.get('image', '?')}: {entry.get('status', '?')} — {entry.get('message', '')}")
    lines.append("\nFor each failed image, suggest a valid replacement.")
    user_msg = "\n".join(lines)
    return IMAGE_REPAIR_SYSTEM_MESSAGE, user_msg


CONTAINER_REPAIR_SYSTEM_MESSAGE = """\
You are a Docker deployment specialist. Some containers in a honeynet
deployment failed to start during `tofu apply`. For each failing container,
work through the repair protocol below, then output the YAML fix.

═══════════════════════════════════════════════════════
CHAIN-OF-THOUGHT REPAIR PROTOCOL (internal reasoning)
Before writing any YAML, reason through these steps silently:

  Step 1 — DIAGNOSE
    • Which container(s) are affected?
    • What is the root cause? (missing script, wrong entrypoint, missing env var,
      image not found, config file expected but absent, interactive REPL with no TTY)
    • Is the failure isolated or systemic?

  Step 2 — SELF-CRITIQUE
    • What assumption in the original World Model was wrong?
    • Does the chosen image ship application code? (base images like python:, node:
      do NOT — they only ship an interpreter)
    • Was a required environment variable or config missing?
    • Did I actually READ the error message? The fix is usually stated
      explicitly in the container's output. Parse it, don't guess.

  Step 3 — TARGETED FIX  (minimal change principle)
    • Change ONLY what's necessary to resolve the error.
    • Preserve ALL other fields (ports, zone, depends_on, env vars that work).
    • Do NOT refactor or improve unrelated containers.

  Step 4 — VERIFY
    • Does the replacement image run as a long-lived daemon without extra config?
    • Does the fix address the root cause found in Step 1?
    • Is there any command that references a file path that doesn't exist in the image?
═══════════════════════════════════════════════════════

MANDATORY FIRST STEP — before writing ANY YAML fix:
  For EVERY failing container, call fetch_image_config on its image.
  Read the Entrypoint, Cmd, Env, and ExposedPorts from the result.
  Base ALL your fix decisions on this real data, not on assumptions.

DIAGNOSIS GUIDE (use AFTER calling fetch_image_config):

- Entrypoint is a wrapper script (e.g. /opt/keycloak/bin/kc.sh, docker-entrypoint.sh)
  AND Cmd is empty:
  → The image needs a subcommand argument. Check the image documentation or README.
    Common patterns: keycloak needs ["start-dev"], minio needs ["server", "/data"].
    Do NOT set command to null — the entrypoint wrapper will exit without arguments.

- Entrypoint is a daemon binary (e.g. nginx, postgres, redis-server):
  → Set command: null. The image starts correctly on its own.

- Env vars with empty values in fetch_image_config result:
  → These are REQUIRED. Add them with realistic honeypot values (weak passwords, demo tokens).

- Container prints "Usage:" or help text and exits immediately:
  → The image's entrypoint is a CLI tool that needs a subcommand.
    Read the FIRST listed subcommand from the help text — this is usually
    the daemon/server mode. E.g., minio prints "minio [FLAGS] COMMAND"
    where the server command is "server /data". Keycloak prints
    "kc.sh [OPTIONS] [COMMAND]" where the command is "start-dev".
    EXTRACT THE COMMAND FROM THE HELP TEXT — do not guess.

- Container prints "environment variable X not set" or "X is required":
  → Read the variable name directly from the error message.
    Add it with a realistic honeypot default value.
    Do NOT change the image or command — only add the missing env var.

- "No such file or directory" / "Cannot find module" in docker_logs:
  → The command references a script that does NOT exist in the image.
    This is a BASE RUNTIME image (python, node) with no application code.
    Switch to an APPLICATION image that serves HTTP natively (nginx, caddy, httpd).
    Do NOT try to fix the script path — the script does not exist.

- No logs at all (container exited before logging):
  → Call fetch_image_config. If Entrypoint is an interpreter with empty Cmd,
    the image runs interactively and exits without a TTY.

For EVERY image you suggest (including keeping the same image):
1. Call fetch_image_config to understand its startup requirements.
2. Call validate_docker_image to confirm it is pullable.

Output ONLY raw YAML — no markdown fences, no prose, no reasoning text.

Format:
fixes:
  "<system_name>":
    command: ["subcommand", "args"]  # based on fetch_image_config result; null ONLY if Entrypoint is a daemon binary
    image: "<image>"  # change only if current image cannot run as a daemon
    env:
      - "NEW_VAR=value"  # only if fetch_image_config shows required env vars

IMPORTANT:
- Include ONLY systems that need fixing.
- NEVER guess commands — always base them on fetch_image_config results.
- NEVER set command to null unless you confirmed the Entrypoint is a standalone daemon.
"""


STARTUP_CONFIG_SYSTEM_MESSAGE = """\
You are a Docker infrastructure expert. Your task is to generate minimal, correct
startup configurations for container images so they actually start and stay running.

For each system listed, output YAML with the following structure:

startup_configs:
  <system_name>:
    env:
      - "KEY=value"   # ONLY vars that are REQUIRED for the container to start.
                      # Use realistic honeypot values (weak passwords, demo tokens).
    healthcheck:
      test: ["CMD", "<binary>", "<args>"]
      interval: "10s"
      timeout: "5s"
      retries: 5
      start_period: "30s"

Rules:
- Only include systems that need env vars or a healthcheck added/corrected.
- Do NOT change image, ports, command, or zone — only env and healthcheck.
- For databases: always include a password env var and a ping/isready healthcheck.
- For web servers: healthcheck should be a curl/wget to localhost:<port>/.
- If you are unsure about required vars for an image, call fetch_image_config first.
- Output ONLY valid YAML — no markdown fences, no prose.

Healthcheck command rules:
- Call fetch_image_config for each image to discover available binaries.
- ALWAYS prefer service-native CLIs (pg_isready, redis-cli, mysqladmin, rabbitmq-diagnostics, etc.)
- Many minimal images (traefik, minio, envoy, scratch-based Go binaries) have NEITHER wget NOR curl.
  For these, use TCP checks: ["CMD-SHELL", "nc -z localhost <port> || exit 1"]
- For HTTP services with uncertain tooling, use a fallback chain:
  ["CMD-SHELL", "curl -sf http://localhost:<port>/ || wget -qO- http://localhost:<port>/ || exit 1"]
- When completely unsure: ["CMD-SHELL", "true"] as a minimal always-passing check.
"""


def build_startup_config_prompt(world_model: "WorldModel") -> tuple[str, str]:
    """Build (system_message, user_message) for startup config generation.

    Produces a prompt that lists all deployed systems so the LLM can generate
    required env vars and healthchecks.  The LLM may call ``fetch_image_config``
    for images it does not recognise.
    """
    lines = ["Generate startup configs for the following container services:\n"]
    for sys_name, system in world_model.systems.items():
        if not system.deploy or not system.deploy.image:
            continue
        existing_env = system.deploy.env or []
        existing_hc = system.deploy.healthcheck
        role = (system.simulate.role if system.simulate else "") or ""
        role_str = f", role: '{role}'" if role else ""
        hc_str = "present" if existing_hc else "MISSING"
        env_str = (
            f"{len(existing_env)} vars ({', '.join(e.split('=')[0] for e in existing_env[:4])})"
            if existing_env else "none"
        )
        lines.append(
            f"- {sys_name}: image={system.deploy.image}"
            f"{role_str}, healthcheck={hc_str}, env={env_str}"
        )
    lines.append(
        "\nAdd required env vars (e.g. DB passwords) and healthchecks where missing. "
        "Use weak/obvious credentials — this is a honeynet."
    )
    return STARTUP_CONFIG_SYSTEM_MESSAGE, "\n".join(lines)


async def build_container_repair_prompt(
    failing_containers: list[dict],
) -> tuple[str, str]:
    """Build (system_message, user_message) for container repair.

    failing_containers: list of dicts with keys:
        system_name, image, command, error, zone
        Optional keys: role (simulate.role), docker_logs (startup output)

    Pre-fetches OCI image configs for all failing images so the LLM has
    real Entrypoint/Cmd/Env data in the prompt (not relying on it to call
    fetch_image_config itself).
    """
    # Pre-fetch OCI configs for all failing images
    from ..image_resolver import fetch_image_config as _fetch_cfg

    image_configs: dict[str, dict] = {}
    for c in failing_containers:
        img = c.get("image", "")
        if img and img not in image_configs:
            try:
                image_configs[img] = await _fetch_cfg(img)
            except Exception:
                image_configs[img] = {"error": "could not fetch config"}

    lines = ["The following containers failed to start during deployment:\n"]
    for c in failing_containers:
        if not isinstance(c, dict):
            continue
        role = c.get("role", "")
        role_str = f", role: '{role}'" if role else ""
        logs = c.get("docker_logs", "")
        lines.append(
            f"- System '{c.get('system_name', '?')}' "
            f"(image: {c.get('image', '?')}, "
            f"command: {c.get('command', 'null')}, "
            f"zone: {c.get('zone', '?')}"
            f"{role_str}): "
            f"{c.get('error', 'unknown error')}"
        )
        # Include pre-fetched OCI config so LLM has real data
        img = c.get("image", "")
        if img and img in image_configs:
            cfg = image_configs[img]
            if "error" not in cfg:
                lines.append(f"  oci_config:")
                lines.append(f"    entrypoint: {cfg.get('entrypoint', [])}")
                lines.append(f"    cmd: {cfg.get('cmd', [])}")
                required = [e.split('=')[0] for e in (cfg.get('env') or [])
                           if '=' in e and not e.split('=', 1)[1].strip()]
                if required:
                    lines.append(f"    required_env: {required}")
                lines.append(f"    exposed_ports: {cfg.get('exposed_ports', [])}")
        if logs:
            trimmed = logs.strip()[-1200:]
            lines.append(f"  docker_logs: |")
            for log_line in trimmed.splitlines():
                lines.append(f"    {log_line}")
    lines.append("\nFor each failing container, suggest a fix based on the oci_config data above.")
    user_msg = "\n".join(lines)
    return CONTAINER_REPAIR_SYSTEM_MESSAGE, user_msg
