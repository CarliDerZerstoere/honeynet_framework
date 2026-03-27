"""
Prompt-derived scenario-fit evaluation.

Extracts expected services, technologies, and concepts from the user's
natural language prompt and checks how many are covered by the generated
WorldModel.  Works automatically without a benchmark YAML file.

This is a lightweight complement to the formal scenario-fit evaluation
(scenario_fit_formal.py) that uses the prompt itself as the reference.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from .models import WorldModel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extraction patterns — what the user mentioned in their prompt
# ---------------------------------------------------------------------------

# Concrete technology names (map to expected image/system names)
_TECH_PATTERNS: dict[str, re.Pattern] = {
    "postgres": re.compile(r'\bpostgres(?:ql)?\b', re.I),
    "mysql": re.compile(r'\bmysql\b', re.I),
    "mariadb": re.compile(r'\bmariadb\b', re.I),
    "mongo": re.compile(r'\bmongo(?:db)?\b', re.I),
    "redis": re.compile(r'\bredis\b', re.I),
    "memcached": re.compile(r'\bmemcached\b', re.I),
    "elasticsearch": re.compile(r'\belasticsearch\b', re.I),
    "opensearch": re.compile(r'\bopensearch\b', re.I),
    "clickhouse": re.compile(r'\bclickhouse\b', re.I),
    "cassandra": re.compile(r'\bcassandra\b', re.I),
    "influxdb": re.compile(r'\binflux(?:db)?\b', re.I),
    "neo4j": re.compile(r'\bneo4j\b', re.I),
    "couchdb": re.compile(r'\bcouchdb\b', re.I),
    "nginx": re.compile(r'\bnginx\b', re.I),
    "haproxy": re.compile(r'\bhaproxy\b', re.I),
    "traefik": re.compile(r'\btraefik\b', re.I),
    "caddy": re.compile(r'\bcaddy\b', re.I),
    "envoy": re.compile(r'\benvoy\b', re.I),
    "rabbitmq": re.compile(r'\brabbitmq\b', re.I),
    "kafka": re.compile(r'\bkafka\b', re.I),
    "nats": re.compile(r'\bnats\b', re.I),
    "grafana": re.compile(r'\bgrafana\b', re.I),
    "prometheus": re.compile(r'\bprometheus\b', re.I),
    "kibana": re.compile(r'\bkibana\b', re.I),
    "jaeger": re.compile(r'\bjaeger\b', re.I),
    "loki": re.compile(r'\bloki\b', re.I),
    "fluentd": re.compile(r'\bfluentd\b', re.I),
    "vault": re.compile(r'\bvault\b', re.I),
    "consul": re.compile(r'\bconsul\b', re.I),
    "keycloak": re.compile(r'\bkeycloak\b', re.I),
    "ldap": re.compile(r'\bldap\b', re.I),
    "jenkins": re.compile(r'\bjenkins\b', re.I),
    "gitlab": re.compile(r'\bgitlab\b', re.I),
    "gitea": re.compile(r'\bgitea\b', re.I),
    "minio": re.compile(r'\bminio\b', re.I),
    "wordpress": re.compile(r'\bwordpress\b', re.I),
    "nextcloud": re.compile(r'\bnextcloud\b', re.I),
}

# Abstract service concepts (match against system names, kinds, roles)
_CONCEPT_PATTERNS: dict[str, re.Pattern] = {
    "sso": re.compile(r'\bsso\b|single.sign.on', re.I),
    "hl7": re.compile(r'\bhl7\b', re.I),
    "fhir": re.compile(r'\bfhir\b', re.I),
    "dicom": re.compile(r'\bdicom\b', re.I),
    "pacs": re.compile(r'\bpacs\b', re.I),
    "emr": re.compile(r'\bemr\b|electronic.medical.record', re.I),
    "ehr": re.compile(r'\behr\b|electronic.health.record', re.I),
    "mpi": re.compile(r'\bmpi\b|master.patient.index', re.I),
    "vpn": re.compile(r'\bvpn\b', re.I),
    "dns": re.compile(r'\bdns\b', re.I),
    "ntp": re.compile(r'\bntp\b', re.I),
    "smtp": re.compile(r'\bsmtp\b|email|e-mail', re.I),
    "backup": re.compile(r'\bbackup\b', re.I),
    "billing": re.compile(r'\bbilling\b', re.I),
    "gateway": re.compile(r'\bgateway\b', re.I),
    "proxy": re.compile(r'\bproxy\b|reverse.proxy|load.balanc', re.I),
    "cache": re.compile(r'\bcache\b|session.cache', re.I),
    "queue": re.compile(r'\bmessage.queue\b|\bqueue\b|\badt.feed', re.I),
    "search": re.compile(r'\bsearch\b|operational.search', re.I),
    "monitoring": re.compile(r'\bmonitor\b', re.I),
    "logging": re.compile(r'\blogging\b|audit.log', re.I),
    "scheduler": re.compile(r'\bscheduler\b|job.schedul', re.I),
    "object_store": re.compile(r'\bobject.stor\b|blob.stor', re.I),
    "database": re.compile(r'\bdatabase\b|clinical.database', re.I),
    "worklist": re.compile(r'\bworklist\b|radiology.worklist', re.I),
    "order_routing": re.compile(r'\border.rout\b', re.I),
    "imaging": re.compile(r'\bimaging\b|imaging.viewer', re.I),
    "portal": re.compile(r'\bportal\b|patient.portal', re.I),
    "api": re.compile(r'\bapi\b|rest.api', re.I),
    "registry": re.compile(r'\bregistry\b|container.registry', re.I),
    "ci_cd": re.compile(r'\bci/?cd\b|pipeline|continuous.integrat', re.I),
    "scada": re.compile(r'\bscada\b|hmi\b|ot\b|ics\b', re.I),
    "mqtt": re.compile(r'\bmqtt\b', re.I),
}


def extract_prompt_requirements(user_request: str) -> list[str]:
    """Extract expected service/technology names from a user prompt.

    Returns a deduplicated list of requirement IDs (e.g. ["redis", "elasticsearch",
    "sso", "fhir", "dicom"]).
    """
    requirements: list[str] = []
    seen: set[str] = set()

    for req_id, pattern in _TECH_PATTERNS.items():
        if pattern.search(user_request) and req_id not in seen:
            requirements.append(req_id)
            seen.add(req_id)

    for req_id, pattern in _CONCEPT_PATTERNS.items():
        if pattern.search(user_request) and req_id not in seen:
            requirements.append(req_id)
            seen.add(req_id)

    return requirements


def _system_tokens(world_model: WorldModel) -> set[str]:
    """Collect all searchable tokens from the WorldModel (system names, images, roles, kinds)."""
    tokens: set[str] = set()
    for sys_name, system in world_model.systems.items():
        tokens.add(sys_name.lower())
        # Split system names on underscores for partial matching
        for part in sys_name.lower().split("_"):
            if len(part) >= 3:  # skip very short fragments
                tokens.add(part)
        if system.deploy:
            img = (system.deploy.image or "").lower().split(":")[0]
            tokens.add(img)
            # Also add the last segment (e.g. "opensearch" from "opensearchproject/opensearch")
            if "/" in img:
                tokens.add(img.rsplit("/", 1)[-1])
        kind_val = system.kind.value if hasattr(system.kind, "value") else str(system.kind)
        tokens.add(kind_val.lower())
        if system.simulate:
            role = (getattr(system.simulate, "role", "") or "").lower()
            if role:
                tokens.add(role)
                for part in role.split():
                    if len(part) >= 3:
                        tokens.add(part)
    return tokens


# Zone/network segment concepts that should map to separate zones
_ZONE_PATTERNS: dict[str, re.Pattern] = {
    "dmz": re.compile(r'\bdmz\b', re.I),
    "internal": re.compile(r'\binternal\b|internally', re.I),
    "public": re.compile(r'\bpublic\b|internet.facing|external', re.I),
    "management": re.compile(r'\bmanagement\b|admin.zone', re.I),
    "monitoring": re.compile(r'\bmonitoring\b', re.I),
    "data": re.compile(r'\bdata.(?:zone|tier|layer|storage|federation|center)\b', re.I),
    "integration": re.compile(r'\bintegration\b|federat', re.I),
    "compute": re.compile(r'\bcompute\b|processing|gpu.allocat|job.schedul', re.I),
    "storage": re.compile(r'\bstorage\b|object.stor|petabyte|dataset', re.I),
}


def extract_zone_concepts(user_request: str) -> list[str]:
    """Extract expected network zones/segments from the prompt."""
    zones: list[str] = []
    for zone_id, pattern in _ZONE_PATTERNS.items():
        if pattern.search(user_request):
            zones.append(zone_id)
    return zones


def _zone_tokens(world_model: WorldModel) -> set[str]:
    """Collect searchable tokens from WorldModel zones."""
    tokens: set[str] = set()
    for zone_name in world_model.zones:
        tokens.add(zone_name.lower())
        for part in zone_name.lower().split("_"):
            if len(part) >= 3:
                tokens.add(part)
    return tokens


@dataclass
class PromptFitResult:
    """Result of prompt-derived scenario-fit evaluation."""
    requirements: list[str]
    matched: list[str]
    unmatched: list[str]
    service_recall: float
    zone_requirements: list[str]
    zone_matched: list[str]
    zone_unmatched: list[str]
    zone_recall: float
    zone_count: int
    system_count: int

    def to_dict(self) -> dict:
        return {
            "prompt_fit_service_recall": round(self.service_recall, 4),
            "prompt_fit_requirements_total": len(self.requirements),
            "prompt_fit_requirements_matched": len(self.matched),
            "prompt_fit_matched": self.matched,
            "prompt_fit_unmatched": self.unmatched,
            "prompt_fit_zone_recall": round(self.zone_recall, 4),
            "prompt_fit_zone_requirements": self.zone_requirements,
            "prompt_fit_zone_matched": self.zone_matched,
            "prompt_fit_zone_unmatched": self.zone_unmatched,
            "zone_count": self.zone_count,
            "system_count": self.system_count,
        }


def compute_prompt_fit(user_request: str, world_model: WorldModel) -> PromptFitResult:
    """Compute how well the WorldModel covers what the user asked for.

    Extracts technology and concept mentions from the prompt, then checks
    if each one is represented in the WorldModel (by system name, image,
    kind, or role).  Also checks zone/network segment coverage.
    """
    requirements = extract_prompt_requirements(user_request)
    zone_requirements = extract_zone_concepts(user_request)

    # Service matching
    sys_tokens = _system_tokens(world_model)
    matched: list[str] = []
    unmatched: list[str] = []
    for req_id in requirements:
        # Use exact token match (==) instead of substring (in) to avoid
        # false positives like "sql" matching "postgresql" or "nosql".
        found = any(req_id == t for t in sys_tokens if len(t) >= 3)
        if found:
            matched.append(req_id)
        else:
            unmatched.append(req_id)
    service_recall = len(matched) / len(requirements) if requirements else 1.0

    # Zone matching
    z_tokens = _zone_tokens(world_model)
    zone_matched: list[str] = []
    zone_unmatched: list[str] = []
    for zr in zone_requirements:
        found = any(zr == t for t in z_tokens if len(t) >= 3)
        if found:
            zone_matched.append(zr)
        else:
            zone_unmatched.append(zr)
    zone_recall = len(zone_matched) / len(zone_requirements) if zone_requirements else 1.0

    return PromptFitResult(
        requirements=requirements,
        matched=matched,
        unmatched=unmatched,
        service_recall=service_recall,
        zone_requirements=zone_requirements,
        zone_matched=zone_matched,
        zone_unmatched=zone_unmatched,
        zone_recall=zone_recall,
        zone_count=len(world_model.zones),
        system_count=len(world_model.systems),
    )
