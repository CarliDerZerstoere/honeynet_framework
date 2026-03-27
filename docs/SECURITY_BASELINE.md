# Sicherheits-Mindestlinie (Plan Abschnitt J — komprimiert)

Dieses Dokument ist **kein** vollständiges Threat Model; es fasst Mindestmaßnahmen für Plugins, Geheimnisse und Policy-Artefakte zusammen.

## Plugin-Trust

- Entry-Point-Plugins (`honeynet.catalog_pack`, `honeynet.repair_strategy`, …) werden zur Laufzeit per `importlib.metadata` geladen. **Trust-Modell:** gleiche Trust-Stufe wie **installierte Python-Pakete** (PyPI/Private Index/editable install). Kein automatisches Sandbox-Isolat.
- **Empfehlung:** Plugins nur aus **signierten Builds** oder **intern geprüften** Quellen installieren; Versions-Pins in Deployment-Umgebungen.

## Redaction / Secrets

- API-Keys und Tokens **nicht** in World-Model-YAML, Telemetrie-JSONL oder Commit-Artefakten persistieren; Umgebungsvariablen oder Secret-Stores nutzen (siehe `LLMConfig` / Deployer-Umgebung).
- Logs: keine vollständigen Prompts mit Produktions-PII ohne explizite Freigabe.

## Policy-Integrität

- Catalog-Snapshots und Policy-YAML sind **Daten**. Änderungen sollten über **Review/Versionierung** (Git, Signatur optional) laufen; Hash/Version im Artefakt (`schema_version`, Git-Commit) dokumentieren.

## Angriffsszenario (Beispiel)

**Szenario:** Ein Angreifer mit Schreibzugriff auf `catalog_snapshot.json` trägt einen bösartigen `default_image`-Wert ein (z. B. öffentliches Image mit Backdoor).

**Mitigation:** Schreibzugriff auf Deploy-Artefakte einschränken; Images über **Allowlist/Scanner** in der CI prüfen (Policy-as-Code außerhalb des Framework-Codes); Katalog-Änderungen reviewen wie Code.

## Verweise

- `docs/adr/0001-policies-externalized.md`
- `docs/ARCHITECTURE_STATUS.md`
