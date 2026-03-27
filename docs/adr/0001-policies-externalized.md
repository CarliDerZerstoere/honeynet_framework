# ADR 0001: Richtlinien und Schwellen außerhalb des Anwendungscodes

## Status

Accepted

## Kontext

Das Framework soll **deterministische** Deployments aus einem World Model erzeugen. **Geschäfts- und Sicherheitsregeln** (Ports, Provider-Pins, Allowlists, Risiko-Schwellen) ändern sich häufiger als Kernlogik und sollen **ohne** erneutes Release nur durch Code-Änderung anpassbar sein. Die Projektregel „No Hardcoded Policies“ verbietet **feste Policy-Werte direkt im Anwendungscode** (Routing, Schwellen, Allowlists als Literale); ausgenommen sind minimale technische Defaults, die ausdrücklich keine Policy sind.

## Entscheidung

- **Policy-Quellen** liegen in **Konfigurationsdateien**, **Metadaten** (z. B. Catalog-Snapshot-JSON, geladene YAML-Invarianten) oder **Plugin-Metadaten** (Entry-Points mit dokumentierter Signatur).
- **Anwendungscode** enthält nur **Loader**, **Validierung der Struktur** (Schema/Typen) und **Ausführung** (Evaluator, Resolver), keine dauerhaften Allowlists/Thresholds als Literale für Entscheidungen.
- **Defaults** im Code sind nur technische **Nicht-Policy-Fallbacks** und sind als solche gekennzeichnet (z. B. minimale Strukturvalidierung ohne Geschäftsregel).

## Konsequenzen

- Neue Regeln werden als **Daten** oder **Plugins** nachgeliefert; Reviews fokussieren auf Integrität dieser Artefakte.
- Tests sollten Policy-Beispiele aus **Fixture-Dateien** laden, nicht aus kopierten Konstanten in Produktionsmodulen.
- Abweichungen müssen durch **neue ADRs** oder Erweiterung der Policy-Schema-Dokumente begründet werden.

## Bezüge

- `docs/ARCHITECTURE_STATUS.md` — Ist/Soll
- `docs/catalog_snapshot.schema.json` — Beispiel für versionierte Policy-Daten
- Projektregel `.cursor/rules/no-hardcoded-policies.mdc`
