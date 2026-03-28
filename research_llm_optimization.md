# Research: Optimizing Multi-Agent LLM Systems for Honeynet Infrastructure Generation

Collected: 2026-03-28

---

## 1. Multi-Agent LLM Architecture Optimization

### 1.1 MACOG: Multi-Agent Code-Orchestrated Generation for IaC (Key Paper)

**Source:** https://arxiv.org/abs/2510.03902

The most directly relevant paper found. MACOG decomposes IaC generation into 8 specialized agents communicating via a **shared-blackboard, finite-state orchestrator**:

1. **Architect** -- Parses natural language into Infrastructure Intermediate Representation (I-IR) with explicit invariants (encryption, residency constraints)
2. **Provider Harmonizer** -- Instantiates abstract resources against provider schemas, resolves version constraints
3. **Engineer** -- Compiles I-IR to HCL using grammar- and schema-constrained decoding that masks inadmissible tokens
4. **Reviewer** -- Runs static validators (terraform validate, linters) for missing variables, naming issues
5. **Security Prover** -- Evaluates OPA/Rego policies, runs Checkov scans
6. **Cost and Capacity Planner** -- Deterministic price estimates, SKU availability checks
7. **DevOps** -- Executes terraform plan/apply in sandboxes (LocalStack, ephemeral accounts)
8. **Memory Curator** -- Stores verified configuration tuples as reusable motifs

**Key architectural patterns applicable to honeynet framework:**
- **Typed Intermediate Representation**: Model infrastructure as an explicit graph (resources, edges, effects) rather than flat YAML. Enables structural verification before code emission.
- **Constrained Realization**: Use grammar/schema masks during generation to eliminate invalid tokens early, reducing later repair cycles.
- **Counterexample-Guided Loop**: Route validator outputs through deterministic mappings to surgical edits, preserving working components.
- **Validator-Centric Organization**: Organize agents around what tools can check (policy, deployment) rather than generic code-gen roles.
- **Proof-Carrying Artifacts**: Bundle configs with policy traces and deployment logs.

**Performance:** GPT-5 improved from 54.90 (RAG baseline) to 74.02 (+35% relative). Ablation: removing DevOps sandbox = -17.09 points; removing constrained decoding = -9.13; removing Security Prover = -12.57.

### 1.2 Multi-Agent Architecture Patterns

**Source:** https://collabnix.com/multi-agent-and-multi-llm-architecture-complete-guide-for-2025/

Three primary patterns:
- **Flat/mesh**: Every agent can communicate with every other. Maximum flexibility, coordination complexity.
- **Centralized supervisor**: One agent routes tasks and manages distribution.
- **Hierarchical supervisors**: Tree-like org with supervisors managing supervisors.

**Optimization finding:** AgentPrune technique achieves 5-10x cost reductions while maintaining accuracy by pruning unnecessary inter-agent messages via magnitude pruning and low-rank regularization.

**Practical takeaway for honeynet framework:** A 3-phase pipeline is essentially a linear supervisor pattern. Consider whether phases need backflow channels (Phase 3 results feeding back to Phase 1) and whether the supervisor should have access to validation results to decide re-routing.

### 1.3 Inter-Phase Context Passing

**Source:** https://deepchecks.com/orchestrating-multi-step-llm-chains-best-practices/

Best practices for multi-step LLM chains:
- Pass prior outputs using structured formats (JSON) with metadata (source IDs, confidence scores)
- Manage token limits by compressing context or selectively injecting based on embedding relevance
- Maintain state across steps using external state stores (Redis) or correlation IDs
- Wrap each step in error handling with contextual logging (inputs, prompts, step identifiers)
- Break chains into modular, independent components for reuse and parallel execution

**Source:** https://arxiv.org/html/2403.11322v1 (StateFlow)

StateFlow conceptualizes LLM inference as a **finite state machine**, using states to represent context. Key challenge: "How do you pass information calculated in an early step to a much later step, skipping intermediate components that don't need it?"

**Practical takeaway:** The honeynet framework should ensure Phase 1 (scenario extraction) outputs are available in Phase 3 (deployment verification), not just Phase 2. Lost context between phases is a root cause of zone placement violations.

---

## 2. Reducing Hallucination in Structured Output

### 2.1 RAG for Structured Outputs (Key Paper)

**Source:** https://arxiv.org/html/2404.08189v1

Core technique: Instead of retrieving facts, **retrieve JSON objects that could be part of the output document**. A siamese transformer encoder maps both NL queries and JSON objects into shared semantic space.

**Results:**
- Without RAG: 21% hallucinated steps/tables
- With RAG: <7.5% hallucinated steps, <4.5% hallucinated tables
- Fine-tuned 7B model with RAG matched 15.5B model without RAG
- Maintained performance across 5 out-of-domain datasets without retraining

**Practical takeaway for honeynet framework:** Build a vector index of known-good service configurations (JSON/YAML fragments). When the LLM generates services for a scenario like "DNS sinkhole," retrieve relevant service definitions as few-shot context. This directly addresses the service mapping failure problem.

### 2.2 Constrained Decoding / Grammar-Guided Generation

**Sources:**
- https://mbrenndoerfer.com/writing/constrained-decoding-structured-llm-output
- https://github.com/Saibo-creator/Awesome-LLM-Constrained-Decoding

Constrained decoding intervenes in the sampling phase, masking out invalid tokens based on a grammar/schema:
- **Guarantees 100% schema compliance** -- no post-hoc validation needed for syntax
- Can use context-free grammars (CFGs) for YAML/JSON structure
- Frameworks: Guidance, Outlines, llama.cpp grammars, XGrammar
- DOMINO (ICML 2024) achieves zero or negative overhead vs unconstrained decoding

**Practical takeaway:** For the honeynet framework, define a YAML grammar that constrains the LLM to only generate valid service definitions, zone placements, and dependency structures. This eliminates syntactic hallucination entirely and lets the system focus on semantic correctness.

### 2.3 Chain-of-Verification (CoVe)

**Source:** https://learnprompting.org/docs/advanced/self_criticism/chain_of_verification

Four-step process:
1. Generate baseline response
2. Plan verification questions
3. Answer verification questions independently
4. Refine output based on verification

CoVe outperforms Zero-Shot, Few-Shot, and Chain-of-Thought methods. Critical finding for code generation: **generating code first, then reasoning about it works better than reasoning first then generating.**

**Practical takeaway:** After LLM generates a service mapping, have a verification step that asks: "Does this DNS sinkhole configuration include a DNS server? Does it include logging? Is the sinkhole in the DMZ?" This catches domain-specific omissions.

---

## 3. Domain-Specific Grounding

### 3.1 Knowledge Graph Grounding

**Sources:**
- https://alessandro-negro.medium.com/grounding-llms-the-knowledge-graph-foundation-every-ai-project-needs-1eef81e866ec
- https://arxiv.org/abs/2305.13269 (Chain-of-Knowledge)

Knowledge graphs provide factual grounding and structured memory for LLMs. Chain-of-Knowledge (CoK) dynamically incorporates grounding from heterogeneous sources (Wikidata, tables, structured data) resulting in reduced hallucination.

**Practical takeaway for honeynet framework:** Build a small knowledge graph of:
- Scenario types -> required services (DNS sinkhole needs: DNS server, sinkhole redirector, logging, firewall)
- Services -> valid zones (DNS server can be in DMZ or internal; monitoring must be in monitoring zone)
- Services -> dependencies (sinkhole depends on DNS; logging depends on network tap)

This graph can be queried during generation to validate/constrain LLM output.

### 3.2 Few-Shot Prompting for IaC

**Source:** https://arxiv.org/html/2404.00227v1 (Survey: LLMs for IaC Generation)

Key findings:
- GPT-3.5-Turbo achieved 59.16% accuracy on AWS Terraform tasks; open-source models much lower
- **Specialized fine-tuned models outperform general-purpose models** on specific domains (WISDOM-ANSIBLE outperformed Codex)
- Few-shot prompting with sample configurations enables rapid prototyping without fine-tuning
- **Main failure modes:** Security vulnerabilities, outdated practices, hallucinated but syntactically valid configs, missing documentation

**Practical takeaway:** For the honeynet framework, maintain a curated library of reference configurations for each scenario type. Include these as few-shot examples in the prompt. Domain-specific fine-tuning would yield the best results but few-shot is the fastest path.

### 3.3 Reference Architecture Templates

**Source:** https://arxiv.org/html/2504.02052v2

Well-designed prompt templates significantly strengthen instruction-following abilities. A compiled prompt typically combines:
- Hard-coded template from developer
- Few-shot examples of valid outputs
- Retrieved context from external sources
- Relevant documents from vector database

**Practical takeaway:** Create "scenario templates" -- validated reference architectures for each honeynet scenario (DNS sinkhole, VPN gateway, web server farm, etc.). The LLM adapts these templates rather than generating from scratch, reducing the search space dramatically.

---

## 4. Self-Correction and Repair in LLM Systems

### 4.1 The Exploration-Exploitation Tradeoff (Key Paper)

**Source:** https://openreview.net/forum?id=o863gX6DxA (NeurIPS 2024)

**Core finding:** LLM repair exposes an explore-exploit tradeoff. The tree of possible refinements is infinitely deep with infinite branching. You must choose between:
- **Exploit**: Refine the program that passes the most tests
- **Explore**: Refine a lesser-considered program that might lead to a different solution

**REx algorithm** (Refine, Explore, Exploit):
- Frames repair as an arm-acquiring bandit problem solved with Thompson Sampling
- Constructs and navigates a tree of refinements
- **Solves more problems in fewer LLM calls, typically 1.5x-5x fewer API calls**

**This directly explains the honeynet framework's "ineffective self-repair" problem:** The repair loop keeps proposing the same failing image because it only exploits (refines the current best) without exploring alternative approaches. REx-style exploration would try fundamentally different service configurations instead of tweaking the same one.

### 4.2 Limits of LLM Self-Correction

**Sources:**
- https://direct.mit.edu/tacl/article/doi/10.1162/tacl_a_00713/125177 (Critical Survey, TACL)
- https://theelderscripts.com/self-correction-in-llm-calls-a-review/

Key findings:
- **LLMs cannot reliably self-correct without external feedback** -- intrinsic self-correction is severely lacking
- When evaluator has same blind spots as generator, iteration rearranges errors without removing them
- The model is not learning during refinement -- it accesses knowledge it already had but failed to retrieve
- **First 2 review passes capture 75% of reachable improvement**
- **Beyond 5-6 rounds, risk of introducing new errors exceeds benefit**
- Self-correction shifts output toward higher-certainty regions, which can mean the same wrong answer repeatedly

**Practical takeaway:** The honeynet framework's repair loop needs:
1. External feedback (validator output, not just LLM self-review)
2. A cap of 2-3 repair iterations before trying a fundamentally different approach
3. Diversity injection -- if repair attempt N fails the same way as N-1, force exploration of a different solution

### 4.3 Tool-Augmented Self-Correction

**Sources:**
- https://arxiv.org/abs/2403.17134 (RepairAgent)
- https://github.com/teacherpeterpan/self-correction-llm-papers

RepairAgent is an autonomous LLM agent for program repair that:
- Plans and executes repair actions by invoking tools (validators, test runners)
- Freely interleaves: gathering bug info -> gathering repair ingredients -> validating fixes
- Uses structured error feedback from tool execution to guide next repair attempt

The **validate-and-fix paradigm**: generate -> validate via compilation/execution -> construct refinement prompt with precise error info -> regenerate iteratively.

**Practical takeaway:** The honeynet framework should feed Docker build errors, network connectivity test results, and service health check outputs directly into the repair prompt. Generic "this failed" messages are insufficient -- the LLM needs the specific error text.

### 4.4 Best-First Tree Search for Debugging

**Source:** https://arxiv.org/pdf/2407.19055

Uses best-first tree search to explore multiple repair paths simultaneously rather than linear refinement. Prioritizes promising branches while maintaining diversity.

**Practical takeaway:** Instead of a linear repair loop (generate -> fail -> repair -> fail -> repair), maintain multiple candidate configurations and explore the most promising ones. This prevents getting stuck on a single failing approach.

---

## 5. Honeynet/Honeypot Architecture

### 5.1 ADLAH: Adaptive Multi-Layered Honeynet

**Source:** https://arxiv.org/pdf/2512.07827

Three-layer architecture:
1. **Sensor Layer** (Low-Interaction): MADCAT nodes capture first-packet data
2. **Decision Layer** (RL Agent): Deep Q-Network decides escalation based on network features
3. **Honeypot Layer** (High-Interaction): Dynamically provisioned containers (Cowrie, etc.)

Orchestration: Kubernetes-based (k3s), DNAT traffic redirection, pod deployment on-demand.
Node types: Sensor Nodes, Hive Nodes (central decision/logging), Cluster Nodes (honeypot management).

**Key lesson:** Even state-of-the-art honeynet architectures use dynamic, container-based deployment with RL-driven decisions. The honeynet framework's approach of LLM-driven configuration generation is a valid and novel direction.

### 5.2 HoneyFactory: Container-Based Honeynet

**Source:** https://www.mdpi.com/2079-9292/13/2/361

Five-module architecture generating honeynets from containers based on business networks. Uses HMM model for deception stage evaluation. Deploys three honeypot categories:
- Simulation honeypots
- Traditional honeypots
- Vulnerability honeypots

Multiple container networks with different honeypot types are launched at different attack stages.

### 5.3 T-Pot: The All-In-One Honeypot Platform

**Source:** https://github.com/telekom-security/tpotce

T-Pot includes 30+ honeypot types via Docker: adbhoney, beelzebub, ciscoasa, citrixhoneypot, conpot, cowrie, ddospot, dicompot, dionaea, elasticpot, endlessh, galah, go-pot, glutton, h0neytr4p, hellpot, heralding, honeyaml, honeypots, honeytrap, ipphoney, log4pot, mailoney, medpot, miniprint, redishoneypot, sentrypeer, snare, tanner, wordpot.

Key architectural decisions:
- All honeypots encapsulated in their own network namespace
- ELK stack (Elasticsearch, Logstash, Kibana) for visualization
- Nginx reverse proxy for management access
- Requires 8-16 GB RAM, 128 GB disk
- Cockpit for web management

**Practical takeaway:** T-Pot's service list is an excellent reference catalog for the honeynet framework. The framework should know which T-Pot honeypots map to which scenarios.

### 5.4 DNS Sinkhole + Honeypot Integration

**Sources:**
- https://thisvsthat.io/dns-sinkhole-vs-honeypot
- https://live.paloaltonetworks.com/t5/general-topics/honey-pot-recommendation-for-dns-sink-holing/td-p/504339

A DNS sinkhole deployment typically needs:
- DNS server (to intercept and redirect queries)
- Sinkhole IP/service (controlled endpoint for redirected traffic)
- Logging/monitoring (to record all redirected queries)
- Firewall rules (to force DNS traffic through the sinkhole)
- Web server on sinkhole IP (to capture HTTP callbacks from malware)

Combined sinkhole + honeypot strategy: sinkholes absorb/neutralize mass traffic; honeypots provide deep interaction for intelligence gathering.

### 5.5 Network Zone Design for Honeynets

**Sources:**
- https://www.giac.org/paper/gppa/548/deploying-honeypots-security-architecture-fictitious-company/105318
- https://flylib.com/books/en/1.48.1.23/1/

Standard zones:
- **DMZ**: External-facing honeypots (web, mail, DNS). Best default placement for honeypots.
- **Internal/LAN**: Internal honeypots for detecting lateral movement and insider threats.
- **Monitoring Zone**: IDS, logging, SIEM, analysis tools. Isolated from honeypot traffic.
- **Management Zone**: Administration interfaces, configuration management.

Placement rules:
- DMZ honeypots detect external attacks and probes
- Internal honeypots act as early-warning that threats bypassed perimeter
- Monitoring must be isolated -- compromise of honeypot should not reach monitoring
- Production honeypots on DMZ warn of malicious activity within the DMZ

---

## 6. Benchmarking and Evaluation

### 6.1 IaC-Eval Benchmark

**Source:** https://arxiv.org/abs/2510.03902 (MACOG paper)

49 AWS Terraform tasks evaluated by functional correctness (exact match against human-written references using JSON execution plans). Metrics: BLEU, CodeBERTScore, LLM-judge.

### 6.2 Evaluating LLMs for IaC

**Source:** https://medium.com/gft-engineering/evaluating-llms-for-infrastructure-as-code-9f8b9ac4ca33

Key concerns:
- Security risks from insecure configurations
- Outdated/inefficient setups leading to resource waste
- Mitigation: embed compliance rules in templates, diverse training data, regular audits, human oversight for critical systems

### 6.3 Multi-Agent Evaluation Frameworks

**Sources:**
- https://arxiv.org/abs/2503.01935 (MultiAgentBench)
- https://arxiv.org/pdf/2502.18836 (REALM-Bench)
- https://aclanthology.org/2025.emnlp-industry.106.pdf (GEMMAS)

**MultiAgentBench**: Measures task completion AND collaboration quality using milestone-based KPIs. Tasks segmented into sub-goals tracked by LLM-based detector.

**REALM-Bench**: Real-world dynamic planning/scheduling tasks.

**GEMMAS**: Graph-based structural metrics:
- Information Diversity Score (IDS): measures collaboration breadth
- Unnecessary Path Ratio (UPR): measures efficiency

Key finding: **Agent performance drops from 60% single-run to 25% when measuring 8-run consistency.** Cost variations of up to 50x for similar precision levels. Pass@k metrics and multi-run protocols are essential.

**Practical takeaway for honeynet framework benchmarks:**
- Measure not just final output quality but also per-phase success rates
- Track consistency across runs (same scenario should produce similar quality)
- Include cost-normalized metrics (tokens consumed per successful generation)
- Use milestone-based evaluation: correct services? correct zones? correct dependencies? deploys successfully?

---

## 7. Actionable Recommendations for the Honeynet Framework

Based on all research findings, here are concrete recommendations mapped to each problem:

### Problem 1: Service Mapping Failures
- **Build a RAG index** of known-good service configurations per scenario type (Section 2.1)
- **Create scenario templates** -- validated reference architectures that the LLM adapts rather than generates from scratch (Section 3.3)
- **Use few-shot examples** of correct service mappings for each scenario type (Section 3.2)
- **Build a knowledge graph** mapping scenario types to required services (Section 3.1)

### Problem 2: Zone Placement Violations
- **Encode zone placement rules** as hard constraints, not LLM suggestions (Section 2.2, constrained decoding)
- **Add a verification step** that checks zone assignments against known rules before proceeding (Section 2.3, CoVe)
- **Pass scenario context through all phases** -- zone decisions in Phase 1 must be visible in Phase 3 (Section 1.3)
- **Use the knowledge graph** to enforce valid zone assignments per service type (Section 3.1)

### Problem 3: Dependency Coverage Gaps
- **Model infrastructure as a typed graph (I-IR)** with explicit dependency edges, not flat service lists (Section 1.1, MACOG)
- **Validate dependency completeness** using graph analysis (all required edges present?) before code generation
- **Retrieve dependency templates** from the RAG index that show typical dependency patterns per scenario

### Problem 4: Ineffective Self-Repair
- **Implement REx-style exploration** -- when repair attempt N fails the same way as N-1, explore a fundamentally different approach (Section 4.1)
- **Cap linear repair at 2-3 iterations**, then branch to alternative solutions (Section 4.2)
- **Feed specific error messages** (Docker build output, health check results) into repair prompts, not generic failure notifications (Section 4.3)
- **Maintain multiple candidate configurations** and use best-first search to pick the most promising (Section 4.4)
- **Use external validators**, not LLM self-review, as the primary feedback signal (Section 4.2)

### Problem 5: Long Runtime Verification
- **Use constrained decoding** to guarantee syntactic validity at generation time, eliminating syntax-level verification (Section 2.2)
- **Validate incrementally** -- check each phase's output before proceeding to the next, catching errors early (Section 1.1, MACOG validator classes)
- **Cache verified motifs** (Memory Curator pattern from MACOG) so previously validated configurations can be reused (Section 1.1)
- **Parallelize independent validations** -- schema, policy, and cost checks can run simultaneously (Section 1.1)
- **Pre-compute scenario templates** so the LLM is adapting a known-good baseline rather than generating from scratch (Section 3.3)
