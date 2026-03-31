"""Lightweight hybrid retrieval utilities for catalog entry selection.

Provides:
- ``BM25Scorer``: Okapi BM25 scorer over a fixed corpus (pure Python / stdlib).
- ``reciprocal_rank_fusion``: combine multiple ranked lists via RRF.
- ``hybrid_rank``: convenience wrapper for two-channel (lexical + BM25) RRF.
- ``select_images_for_scenario``: pick the most relevant verified images from the
  known-good registry for a given scenario description.

Design goals:
- Zero additional runtime dependencies (math, collections from stdlib).
- Reproducible: same input → same output.
- Measurable: every score is a plain float so callers can log and compare.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# BM25 Scorer
# ---------------------------------------------------------------------------

class BM25Scorer:
    """Okapi BM25 scorer over a static corpus of tokenized documents.

    Parameters
    ----------
    corpus :
        A list of token lists (one per document).  Documents must not change
        after construction.
    k1 :
        Term-frequency saturation parameter (typical range 1.2–2.0).
    b :
        Length normalisation parameter (0 = no normalisation, 1 = full).
    """

    def __init__(
        self,
        corpus: list[list[str]],
        k1: float = 1.5,
        b: float = 0.75,
    ) -> None:
        self._k1 = k1
        self._b = b
        self._n = len(corpus)

        if self._n == 0:
            self._avgdl = 0.0
            self._idf: dict[str, float] = {}
            self._tf: list[Counter[str]] = []
            self._dl: list[int] = []
            return

        self._dl = [len(doc) for doc in corpus]
        self._avgdl = sum(self._dl) / self._n
        self._tf = [Counter(doc) for doc in corpus]

        df: Counter[str] = Counter()
        for doc in corpus:
            df.update(set(doc))

        # BM25+ IDF — avoids negative values for very frequent terms.
        self._idf = {
            term: math.log((self._n - freq + 0.5) / (freq + 0.5) + 1.0)
            for term, freq in df.items()
        }

    def score(self, doc_index: int, query_tokens: list[str]) -> float:
        """Return the BM25 score for a single document given query tokens."""
        if self._n == 0 or not query_tokens:
            return 0.0
        tf = self._tf[doc_index]
        dl = self._dl[doc_index]
        avgdl = self._avgdl
        k1 = self._k1
        b = self._b
        total = 0.0
        for term in query_tokens:
            idf = self._idf.get(term, 0.0)
            f = tf.get(term, 0)
            total += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / max(avgdl, 1.0)))
        return total

    def rank(
        self,
        query_tokens: list[str],
        top_k: Optional[int] = None,
    ) -> list[tuple[int, float]]:
        """Return ``[(doc_index, score), ...]`` sorted descending by score.

        Parameters
        ----------
        query_tokens :
            Tokenised query (same tokenisation as used to build the corpus).
        top_k :
            Return only the top *k* entries.  Returns all if ``None``.
        """
        scores = [(i, self.score(i, query_tokens)) for i in range(self._n)]
        scores.sort(key=lambda x: x[1], reverse=True)
        if top_k is not None:
            return scores[:top_k]
        return scores


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(
    ranked_lists: list[list[int]],
    k: int = 60,
) -> list[tuple[int, float]]:
    """Combine multiple ranked lists of document indices using RRF.

    RRF score for document d:  ``Σ 1 / (k + rank(d, list_i))``
    where rank is 1-based.

    Parameters
    ----------
    ranked_lists :
        Each list contains document indices in descending relevance order.
    k :
        Controls how much early ranks dominate (typical value 60).

    Returns
    -------
    A list of ``(doc_index, rrf_score)`` sorted descending by RRF score.
    """
    if not ranked_lists:
        return []

    all_docs: set[int] = set()
    for ranked in ranked_lists:
        all_docs.update(ranked)

    rank_lookup: list[dict[int, int]] = []
    for ranked in ranked_lists:
        lut = {doc: rank + 1 for rank, doc in enumerate(ranked)}
        rank_lookup.append(lut)

    scores: dict[int, float] = {}
    for doc in all_docs:
        rrf = 0.0
        for i, ranked in enumerate(ranked_lists):
            worst_rank = len(ranked) + 1
            rank = rank_lookup[i].get(doc, worst_rank)
            rrf += 1.0 / (k + rank)
        scores[doc] = rrf

    return sorted(scores.items(), key=lambda x: x[1], reverse=True)


def hybrid_rank(
    lexical_ranked: list[int],
    bm25_ranked: list[int],
    k: int = 60,
) -> list[tuple[int, float]]:
    """Combine lexical and BM25 ranked lists via RRF.

    A thin convenience wrapper around ``reciprocal_rank_fusion`` for the
    common two-channel case.
    """
    return reciprocal_rank_fusion([lexical_ranked, bm25_ranked], k=k)


# ---------------------------------------------------------------------------
# Image selection for extraction prompts
# ---------------------------------------------------------------------------

# Keyword annotations for image categories whose names don't contain the
# domain terms a scenario description would use.  Each entry maps an image
# base name (key in known_good_images.json) to additional search tokens.
_IMAGE_ANNOTATIONS: dict[str, list[str]] = {
    # --- Web servers ---
    "nginx": ["web", "server", "proxy", "reverse", "static", "http", "frontend", "portal", "site"],
    "httpd": ["apache", "web", "server", "http", "static", "site", "portal", "intranet", "landing"],
    "caddy": ["web", "proxy", "server", "https", "automatic", "tls", "reverse"],
    # --- Databases (relational) ---
    "postgres": [
        "database", "sql", "relational", "rdbms", "persistent",
        "patient", "record", "ledger", "account", "transaction", "metadata",
        "inventory", "catalog", "store", "backend",
    ],
    "mysql": ["database", "sql", "relational", "rdbms", "backend", "store", "inventory"],
    "mariadb": ["database", "sql", "mysql", "relational", "rdbms", "backend"],
    # --- Databases (NoSQL / specialty) ---
    "mongo": ["nosql", "database", "document", "json", "schema", "flexible", "catalog"],
    "cassandra": ["nosql", "database", "distributed", "wide", "column", "scale", "partition"],
    "couchdb": ["nosql", "database", "document", "sync", "replication", "offline"],
    "neo4j": ["graph", "database", "relationship", "knowledge", "network", "connection", "link"],
    "influxdb": [
        "historian", "timeseries", "metrics", "scada", "ot", "telemetry", "tsdb",
        "monitoring", "influx", "sensor", "measurement", "iot", "industrial",
    ],
    "clickhouse/clickhouse-server": ["analytics", "columnar", "timeseries", "olap", "metrics", "warehouse", "data"],
    # --- Cache / KV ---
    "redis": ["cache", "session", "kv", "queue", "pubsub", "coordination", "fast", "memory"],
    "memcached": ["cache", "memory", "session", "fast", "kv"],
    # --- Search / Indexing ---
    "elasticsearch": [
        "search", "fulltext", "index", "elk", "log", "analytics",
        "catalog", "dataset", "research", "publication", "document",
    ],
    "kibana": ["dashboard", "elk", "visualization", "log", "analytics", "search", "explore"],
    "opensearchproject/opensearch": ["search", "fulltext", "index", "analytics", "log", "opensearch"],
    # --- Monitoring / Observability ---
    "grafana/grafana": ["dashboard", "visualization", "monitoring", "metrics", "graph", "panel", "alert"],
    "prom/prometheus": ["monitoring", "metrics", "alerting", "scraping", "timeseries", "exporter"],
    "grafana/loki": ["log", "logging", "aggregation", "grafana", "trace"],
    "prom/alertmanager": ["alert", "monitoring", "notification", "incident", "page"],
    "jaegertracing/all-in-one": ["tracing", "trace", "distributed", "observability", "span", "request"],
    "fluent/fluentd": ["log", "logging", "aggregation", "forwarding", "elk", "collect", "ship"],
    # --- Message brokers / Queues ---
    "rabbitmq": [
        "message", "queue", "broker", "amqp", "event", "streaming",
        "transaction", "payment", "settlement", "async", "worker",
    ],
    "nats": ["message", "queue", "broker", "streaming", "pubsub", "event", "microservice"],
    "eclipse-mosquitto": [
        "mqtt", "broker", "iot", "telemetry", "messaging", "pub", "sub",
        "sensor", "device", "scada", "plc", "gateway", "embedded",
    ],
    "emqx": ["mqtt", "broker", "iot", "telemetry", "messaging", "sensor", "device"],
    "ghcr.io/bitnami/kafka": ["kafka", "event", "stream", "bus", "topic", "queue", "messaging", "pipeline"],
    "apache/kafka": ["kafka", "event", "stream", "bus", "topic", "queue", "messaging", "pipeline"],
    "zookeeper": ["zookeeper", "coordination", "kafka", "cluster", "distributed", "consensus"],
    "ghcr.io/bitnami/zookeeper": ["zookeeper", "coordination", "kafka", "cluster"],
    # --- Identity / Auth ---
    "osixia/openldap": [
        "ldap", "directory", "identity", "auth", "active", "ad",
        "staff", "employee", "federation", "organization", "corporate",
    ],
    "keycloak/keycloak": [
        "sso", "oauth", "identity", "auth", "saml", "oidc",
        "login", "portal", "user", "federation", "provider", "token",
    ],
    "hashicorp/vault": ["secret", "credential", "pki", "encryption", "auth", "certificate", "tls", "key"],
    "hashicorp/consul": ["service", "discovery", "mesh", "config", "health", "cluster", "register"],
    # --- Storage ---
    "minio/minio": [
        "object", "storage", "s3", "bucket", "pcap", "forensic", "backup",
        "dataset", "research", "archive", "data", "lake", "blob", "artifact",
    ],
    "registry": ["container", "registry", "docker", "image", "harbor", "artifact", "repository"],
    # --- Proxies / Gateways ---
    "traefik": ["proxy", "reverse", "gateway", "ingress", "load", "balancer", "api", "frontend", "router"],
    "haproxy": ["proxy", "load", "balancer", "gateway", "tcp", "frontend", "backend"],
    "envoyproxy/envoy": ["proxy", "service", "mesh", "sidecar", "ingress", "grpc"],
    # --- CI/CD / DevOps ---
    "gitea/gitea": ["git", "source", "code", "repository", "version", "control", "forge"],
    "jenkins/jenkins": ["ci", "cd", "pipeline", "build", "automation", "deploy", "job"],
    "gitlab/gitlab-ce": ["git", "ci", "cd", "devops", "repository", "merge", "pipeline"],
    "sonarqube": ["code", "quality", "scan", "analysis", "lint", "vulnerability"],
    "drone/drone": ["ci", "cd", "pipeline", "build", "container"],
    # --- Admin / Management ---
    "portainer/portainer-ce": ["docker", "management", "container", "ui", "admin", "orchestration"],
    "phpmyadmin": ["mysql", "database", "admin", "web", "management"],
    "adminer": ["database", "admin", "mysql", "postgres", "web", "management"],
    # --- CMS / Collaboration ---
    "nextcloud": [
        "file", "share", "cloud", "storage", "collaboration", "engineering",
        "document", "intranet", "team", "office",
    ],
    "wordpress": ["cms", "blog", "web", "php", "portal", "intranet", "site", "content"],
    "ghost": ["blog", "cms", "web", "content", "publishing"],
    "drupal": ["cms", "web", "php", "portal", "content", "enterprise"],
    "redmine": ["project", "management", "issue", "tracker", "ticket", "task", "agile"],
    "matomo": ["analytics", "tracking", "web", "visitor", "privacy"],
    # --- Security ---
    "vaultwarden/server": ["password", "manager", "credential", "bitwarden", "secret"],
    "hwdsl2/ipsec-vpn-server": ["vpn", "ipsec", "gateway", "remote", "access", "tunnel", "network"],
    # --- Runtimes ---
    "node": ["nodejs", "javascript", "api", "rest", "service", "runtime", "express", "backend"],
    "python": ["python3", "api", "service", "runtime", "script", "http", "flask", "django", "backend"],
    "ruby": ["rails", "api", "service", "runtime", "backend"],
    "golang": ["go", "api", "service", "runtime", "backend", "grpc"],
    "openjdk": ["java", "spring", "api", "service", "jvm", "backend", "enterprise"],
    # --- Base images ---
    "alpine": ["base", "minimal", "linux", "shell"],
    "ubuntu": ["base", "linux", "shell", "apt"],
    "debian": ["base", "linux", "shell"],
    "busybox": ["minimal", "toolbox", "utility"],
}


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric chars, drop empty tokens."""
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


_registry_cache: dict | None = None


def _load_registry() -> dict:
    """Load the known-good images registry (cached after first successful load).

    Unlike ``@lru_cache``, this does NOT cache failures — a transient read
    error or missing file will be retried on the next call.
    """
    global _registry_cache
    if _registry_cache is not None:
        return _registry_cache
    registry_path = Path(__file__).parent / "data" / "known_good_images.json"
    if not registry_path.exists():
        return {}
    try:
        import json
        data = json.loads(registry_path.read_text(encoding="utf-8"))
        _registry_cache = data
        return data
    except Exception:
        return {}


def _estimate_top_k(scenario: str) -> int:
    """Scale pre-fetch count with scenario complexity.

    Simple prompts ("web server with database") get ~20 images.
    Complex prompts ("enterprise with 15 services across 5 zones") get up to 50.
    """
    tokens = _tokenize(scenario)
    word_count = len(tokens)

    registry = _load_registry()
    images = registry.get("images", {})
    if not images:
        return 25

    # Count how many image categories match at least one prompt token
    token_set = set(tokens)
    matched = 0
    for cat in images:
        cat_tokens = set(_tokenize(cat))
        annotations = set(_IMAGE_ANNOTATIONS.get(cat, []))
        if token_set & (cat_tokens | annotations):
            matched += 1

    # Base: matched categories + headroom for related images
    base = max(20, matched + 5)
    # Long prompts describe complex scenarios — give more headroom
    if word_count > 80:
        base += 10
    elif word_count > 40:
        base += 5
    return min(base, 50)


def select_images_for_scenario(
    scenario: str,
    top_k: int | None = None,
) -> list[str]:
    """Return the most scenario-relevant verified image tags.

    Scores each image category from the known-good registry against the
    scenario description using a hybrid BM25 + lexical overlap approach.
    Returns the preferred (first listed) tag for each selected category so
    the extraction prompt always receives concrete, verified image references.

    When *top_k* is ``None`` (default), the count scales automatically with
    the complexity of the scenario description.

    Parameters
    ----------
    scenario :
        The raw user scenario description.
    top_k :
        Maximum number of image references to return.  ``None`` = adaptive.

    Returns
    -------
    A list of Docker image references (e.g. ``["nginx:1.25-alpine", ...]``).
    Empty list if the registry cannot be loaded.
    """
    if top_k is None:
        top_k = _estimate_top_k(scenario)
    registry = _load_registry()
    if not registry:
        return []

    images: dict[str, list[str]] = registry.get("images", {})
    if not images:
        return []

    # Build a corpus: one document per image category.
    # Each document is the tokenized category name + tag tokens + domain annotations.
    categories: list[str] = list(images.keys())
    corpus: list[list[str]] = []
    for cat in categories:
        tokens = _tokenize(cat)
        for tag in images[cat]:
            tokens = tokens + _tokenize(tag)
        # Append domain keyword annotations so that scenario terms like
        # "historian", "mqtt", "vpn" can match the right image categories
        # even when those words don't appear in the image name itself.
        annotations = _IMAGE_ANNOTATIONS.get(cat, [])
        tokens = tokens + annotations
        corpus.append(tokens)

    query_tokens = _tokenize(scenario)

    # BM25 ranking
    bm25 = BM25Scorer(corpus)
    bm25_ranked_with_scores = bm25.rank(query_tokens)
    bm25_ranked = [idx for idx, _ in bm25_ranked_with_scores]

    # Lexical overlap ranking (simple token intersection count)
    query_set = set(query_tokens)
    lexical_scores = [
        (i, len(query_set & set(corpus[i])))
        for i in range(len(corpus))
    ]
    lexical_scores.sort(key=lambda x: x[1], reverse=True)
    lexical_ranked = [idx for idx, _ in lexical_scores]

    # Combine via RRF
    fused = hybrid_rank(lexical_ranked, bm25_ranked)

    # Select top-k categories and return the preferred (first) tag for each
    result: list[str] = []
    for idx, _score in fused[:top_k]:
        cat = categories[idx]
        tags = images.get(cat, [])
        if tags:
            result.append(tags[0])

    return result
