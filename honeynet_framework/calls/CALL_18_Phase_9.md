# Call 18 — Phase 9: Honeytokens — Köder in den Services

**Schwierigkeit:** ★★☆ mittel  
**Abhängigkeiten:** Call 12 (Phase 6b.5 — `HoneytokenTemplate` in `ImageCatalogEntry`), Call 16 (Phase 7 — `upload_files`-Feld in `DeployContainer`)  
**Risiko:** mittel — neue Compiler-Logik mit Deduplizierungsregeln

---

## Ziel

Honeytokens aus dem Katalog als `upload_files`-Blöcke und `env`-Einträge
in die Container-IR einbauen. Deduplizierung für Datei- und Env-Pfade
(Policy: letztes Template in Originalreihenfolge gewinnt).

---

## Dateien

### Zu editieren
- `honeynet_framework/deploy_compiler.py` — `_phase4_generate_ir()`
- `honeynet_framework/enricher.py` — LLM-Prompt für Honeytoken-Templates

### Als Kontext mitgeben
- `deploy_compiler.py` vollständig
- `catalog/models.py` — Signatur `HoneytokenTemplate`
- `models.py` — `DeployContainer.upload_files`

---

## Änderungen

### 9.2 — `enricher.py`: LLM-Prompt erweitern

Im Prompt-Template ergänzen:
```
- honeytoken_templates: Lure assets to place inside this service.
  Templates support: {{random_password}}, {{random_hex_16}}, {{random_upper_20}}.
  type: "file" = write rendered template to path in container
        "env"  = inject as environment variable (path = env key name)
        "db_init_script" = run as DB init script
```

### 9.3 — `deploy_compiler.py`: Honeytokens als `upload`-Blöcke

**Hilfsfunktion** (außerhalb der Klasse, nach Imports):
```python
import secrets, string

def _render_honeytoken_template(template: str) -> str:
    pw = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(16))
    hex16 = secrets.token_hex(8)
    upper20 = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(20))
    return (template
        .replace("{{random_password}}", pw)
        .replace("{{random_hex_16}}", hex16)
        .replace("{{random_upper_20}}", upper20))
```

**In `_phase4_generate_ir()`, im Container-Loop nach Entry-Lookup:**
```python
upload_files = []
env_extras = []
if entry and getattr(entry, "honeytoken_templates", None):
    # [G7-FIX] Pfad-Deduplizierung: "letztes Template gewinnt".
    # reversed() + insert(0, ...) entspricht "letztes Vorkommen in Originalreihenfolge".
    # [REV32-P3] Dieselbe Policy auch für env-Templates (seen_env_keys).
    seen_paths: set[str] = set()
    seen_env_keys: set[str] = set()
    for t in reversed(entry.honeytoken_templates):
        rendered = _render_honeytoken_template(t.template)
        if t.type in ("file", "db_init_script"):
            if t.path not in seen_paths:
                seen_paths.add(t.path)
                upload_files.insert(0, {
                    "content":    rendered,
                    "file":       t.path,
                    "executable": False,
                })
        elif t.type == "env":
            if t.path not in seen_env_keys:
                seen_env_keys.add(t.path)
                env_extras.insert(0, f"{t.path}={rendered}")

# env_extras vorne an env anfügen (Honeytokens haben Vorrang):
env = env_extras + list(c.get("env", []))
```

**`DeployContainer`-Konstruktion erweitern:**
```python
containers.append(DeployContainer(
    ...
    env=env,
    upload_files=upload_files,   # ← NEU
))
```

---

## Smoke-Test nach dem Call

```python
from honeynet_framework.deploy_compiler import _render_honeytoken_template

rendered = _render_honeytoken_template("pass={{random_password}}")
assert rendered.startswith("pass=")
assert len(rendered) > 5

# Deduplizierung
rendered2 = _render_honeytoken_template("{{random_hex_16}}")
assert len(rendered2) == 16  # 8 bytes = 16 hex chars
```

```python
# Integration: zwei Templates auf gleichem Pfad → nur einer in upload_files
# [aus Testlücke Phase 9]
# template_1 = HoneytokenTemplate("file", "/etc/.pgpass", "first")
# template_2 = HoneytokenTemplate("file", "/etc/.pgpass", "second")
# → upload_files hat genau 1 Eintrag; Inhalt kommt von template_2 ("letztes gewinnt")

# env-Deduplizierung
# template_env_1 = HoneytokenTemplate("env", "SECRET_KEY", "value1")
# template_env_2 = HoneytokenTemplate("env", "SECRET_KEY", "value2")
# → env_extras hat genau 1 SECRET_KEY=... Eintrag
```
