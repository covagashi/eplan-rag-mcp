# EPLAN-KI-Automatisierungs-Toolkit

[English](README.md) · [Español](README.es.md) · [한국어](README.ko.md) · **Deutsch** · [中文](README.zh-CN.md) · [Русский](README.ru.md)

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/covagashi/eplan-rag-mcp)
[![MCP Badge](https://lobehub.com/badge/mcp/covagashi-eplan_2026_ia_mcp_scripts)](https://lobehub.com/mcp/covagashi-eplan_2026_ia_mcp_scripts)

KI-gestützte Automatisierung für **EPLAN Electric P8** und **EPLAN EEC Pro 2026** auf Basis des Model Context Protocol (MCP).

Dieses Repository enthält den lokalen MCP-Server, der eine laufende EPLAN-Instanz steuert. Die drei entfernten Dokumentations-RAGs liegen im separaten Repository [eplan-cloudflare-rags](https://github.com/covagashi/eplan-cloudflare-rags).

> Arbeitest du hier mit einem LLM? [`llm.md`](llm.md) beschreibt aus Sicht des Modells alle Funktionen und Konfigurationsmöglichkeiten des Toolkits.

## Repository-Struktur

```text
.
└── eplan-p8-mcp-server/          # LOKAL: MCP-Server zur Steuerung von EPLAN P8
```

| Komponente | Typ | Zweck | EPLAN-Produkt |
|---|---|---|---|
| `eplan-p8-mcp-server/` | Lokaler Python-MCP-Server | Eine laufende EPLAN-Instanz aus Claude steuern: Projekte öffnen/schließen, Exporte, Berichte und Skripte | EPLAN Electric P8 |
| [**eplan-cloudflare-rags**](https://github.com/covagashi/eplan-cloudflare-rags) | Entfernte Cloudflare Workers, separates Repository | Dokumentation zu P8 2026, P8 2027 und EEC Pro über MCP und REST bereitstellen | EPLAN Electric P8 und EEC Pro |
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | Claude-Code-Skill, separates Repository | Claude beim Schreiben korrekter EPLAN-Skripte, API-Programme und Remote-Client-Anwendungen unterstützen | EPLAN Electric P8 |

Jedes Repository enthält ein eigenes README mit Installations- und Nutzungshinweisen.

## Was ist MCP?

**MCP (Model Context Protocol)** ist ein offener Standard, über den KI-Assistenten mit externen Werkzeugen und Diensten interagieren. Claude kann damit nicht nur Code erzeugen, sondern Aktionen in EPLAN in Echtzeit ausführen und die Dokumentation durchsuchen.

## Schnellstart

### Lokale EPLAN-Automatisierung (P8)

```bash
pip install pythonnet mcp
claude mcp add eplan -- python YOURPATH/eplan-p8-mcp-server/mcp_server/server.py
claude mcp list   # sollte "eplan" anzeigen
```

Starte danach EPLAN, öffne Claude Code und sage `connect to eplan`. Die vollständige Anleitung steht in [`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md).

![Konfigurierte Claude CLI](image.png)

#### Voraussetzung

Vor dem Verbindungsaufbau muss Remoting in EPLAN aktiviert sein: **Allow remote access via Remote Client** unter **File → Settings… → Workstation → Interfaces → Remote access** einschalten.

![Remote-Zugriff über Remote Client erlauben](Remoting_Setting_AllowLocalAccess.png)

#### Bereitgestellte Werkzeuge

Im voreingestellten `full`-Modus stellt der Server **199 Werkzeuge** bereit:

| Gruppe | Anzahl | Funktion |
|---|---:|---|
| Verbindung und Hilfsfunktionen | 8 | Verbinden, Version auswählen, Status prüfen, Erweiterungen auflisten |
| Typisierte EPLAN-Aktionen (`eplan_*`) | 183 | Ein Werkzeug je dokumentierter oder verifizierter Aktion; Ausführung ohne blockierende Dialoge in einem C#-Skript mit QuietMode |
| Aktionskatalog (`eplan_action_catalog` / `_describe` / `_run` / `_ribbon_catalog`) | 4 | Weitere ~1050 nur als GUI-Schaltflächen vorhandene Aktionen aus `MFTools.xml` |
| Asset Administration Shell (`aas_*`) | 4 | AAS/AASX-Digitalzwillinge importieren und exportieren |

Die ~1050 Katalogaktionen bekommen bewusst nicht jeweils ein eigenes Wrapper-Werkzeug: Das würde die Werkzeugzahl verdreifachen und die Auswahl verschlechtern.

Zu den 183 typisierten Werkzeugen gehören vier Live-DataModel-Werkzeuge: `eplan_live_query_functions`, `eplan_live_query_pages`, `eplan_live_set_function_text` und `eplan_live_set_connection_designations`. Sie lesen und ändern das Objektmodell des geöffneten Projekts mittels Laufzeitreflexion und umgehen damit die Einschränkung statischer `using`-Direktiven der Skript-Engine.

Die Werkzeuge decken außerdem einen unbeaufsichtigten Entwicklungs-, Bereitstellungs- und Testzyklus ab:

- **Anwendungslebenszyklus:** `eplan_app_launch` / `eplan_app_shutdown` / `eplan_app_restart` beenden EPLAN, tauschen Add-in-DLLs aus, starten neu, verbinden sich wieder und öffnen das Projekt.
- **Wegwerf-Testprojekte:** `eplan_scratch_project_*` erzeugt Klone einer Vorlage.
- **Diagnose:** `eplan_get_system_messages` liest den Meldungsbaum mit denselben Fehlern und Warnungen wie in der GUI.
- **Private Erweiterungen:** siehe [unten](#private-erweiterungsmodule-eplan_mcp_extensions).

Eine Referenz aller Werkzeuge bietet das [Projekt-Wiki](https://github.com/covagashi/eplan-rag-mcp/wiki).

#### Discovery-Modus

`EPLAN_MCP_MODE=discovery` veröffentlicht 13 statt 199 Werkzeuge. Das verkleinert die Werkzeugliste auf Kosten eines zusätzlichen Suchaufrufs pro Aufgabe und lohnt sich für MCP-Clients, die bei jeder Anfrage sämtliche vollständigen Werkzeugschemas übertragen.

**In Claude Code nicht verwenden.** Claude Code lädt Schemas ohnehin bei Bedarf nach: zuerst nur Namen, dann das benötigte Schema. Die 199 Namen des `full`-Modus kosten deshalb ungefähr so wenig wie die 13 im `discovery`-Modus. Dessen Suche → Beschreibung → Aufruf fügt nur eine weitere Runde hinzu. Vergleich und Messungen stehen in der [Server-Anleitung](eplan-p8-mcp-server/mcp_server/README.md#discovery-mode-eplan_mcp_mode).

### Entfernte Dokumentations-RAGs (P8, EEC Pro, 2027)

Sie sind bereits bereitgestellt; lokale Daten sind nicht nötig:

```bash
# Dokumentation zu EPLAN Electric P8 (2026; semantische Suche)
claude mcp add eplan-rag -- cmd /c npx mcp-remote https://rag2026.covaga.xyz/mcp

# Dokumentation zu EPLAN EEC Pro 2026
claude mcp add eecpro-rag -- cmd /c npx mcp-remote https://rageecpro.covaga.xyz/mcp

# Dokumentation zu EPLAN Electric P8 (2027; Schlagwort-/Volltextsuche)
claude mcp add eplan-wiki-2027 -- cmd /c npx mcp-remote https://rag2027.covaga.xyz/mcp
```

`eplan-wiki-2027` ist ein eigener Server und kein Upgrade von `eplan-rag` von 2026 auf 2027: Er indexiert eine andere Dokumentationsversion und nutzt eine andere Suchart (SQLite FTS5/bm25 über das [mitgelieferte Wiki](https://github.com/covagashi/eplan-cloudflare-rags/tree/main/cloudflare-rag-eplan-2027) statt Vectorize + bge für semantische Suche). Vergleichsmessungen mit echten Anfragen zeigen unterschiedliche Stärken: FTS5 gewinnt bei exakten Namen („Welche Signatur hat X?“), semantische Suche bei Anfragen ohne gemeinsame Begriffe mit der Dokumentation. Installiere beide.

Zum Prüfen von Aktionsnamen und Parametern während der Entwicklung gibt es auch eine REST-API:

```bash
curl -X POST https://rag2026.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "export project pdf", "topK": 3}'

curl -X POST https://rag2027.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "FindAction", "topK": 3}'
```

Werkzeuge, REST-Endpunkte und Architektur sind in den READMEs für [P8 2026](https://github.com/covagashi/eplan-cloudflare-rags/blob/main/cloudflare-rag-eplan-p8/README.md) und [EEC Pro](https://github.com/covagashi/eplan-cloudflare-rags/blob/main/cloudflare-rag-eecpro/README.md) beschrieben.

### Claude-Code-Skill für die EPLAN-Entwicklung

Die MCP-Server lassen Claude in EPLAN *handeln*. Der Skill [**eplan-development**](https://github.com/covagashi/eplan-development-skill) hilft ihm, *korrekten EPLAN-Code zu schreiben*: Skript-Einstiegspunkte, verifizierte Aktionsparameter, Zugriff auf die Artikeldatenbank, Remote-Client-Automatisierung (dynamische Ports, EPLAN ohne Oberfläche, Cogineer) und Fallstricke im Betrieb wie pseudoasynchrone blockierende Befehle, der Message-Loop-Monitor-Thread, saubere Ressourcenfreigabe und Änderungen am Remoting ab EPLAN 2025.

Der Skill liegt in einem eigenen Repository und ist **hostunabhängig**: Er setzt weder einen bestimmten MCP-Server noch einen Skript-Runner oder Dokumentationsindex voraus und kann auch allein genutzt werden.

```text
/plugin marketplace add covagashi/eplan-development-skill
/plugin install eplan-development@eplan-skills
```

Dieses Repository ist ebenfalls ein Plugin-Marketplace und verweist auf denselben Skill:

```text
/plugin marketplace add covagashi/eplan-rag-mcp
/plugin install eplan-development@eplan-tools
```

Manuelle Installation und weitere Details: [README des Skills](https://github.com/covagashi/eplan-development-skill#readme).

## Neue EPLAN-Aktionen hinzufügen

Der lokale MCP-Server registriert Werkzeuge **dynamisch** anhand der `__all__`-Liste jedes Aktionspakets. Daher genügen Implementierung und Export; individuelle Registrierungsroutine ist nicht nötig.

### 1. Aktion implementieren

In `eplan-p8-mcp-server/mcp_server/api/actions/<your_module>.py`:

```python
def open_project(project_path: str, open_mode: str = None) -> dict:
    """Open a project in EPLAN.

    Args:
        project_path: Full path to the .elk project file.
        open_mode: "Standard", "ReadOnly", or "Exclusive" (optional).
    """
    manager, error = _get_connected_manager()
    if error:
        return error
    action = _build_action("ProjectOpen", Project=project_path, OpenMode=open_mode)
    return manager.execute_action(action)
```

### 2. Funktion exportieren

Füge sie sowohl zu den Importen als auch zu `__all__` in `eplan-p8-mcp-server/mcp_server/api/actions/__init__.py` hinzu. Sie wird dann automatisch als `eplan_open_project` registriert.

### 3. MCP-Server neu starten

Nach dem Neustart von Claude bzw. des Servers ist das neue Werkzeug verfügbar.

### 4. Mit offizieller Dokumentation prüfen (optional)

`eplan-p8-mcp-server/tools/validate_actions.py` vergleicht alle Aktionsnamen und Parameter der Wrapper mit dem RAG zur offiziellen EPLAN-Dokumentation und schreibt einen Markdown-Bericht:

```bash
python eplan-p8-mcp-server/tools/validate_actions.py
```

![EPLAN-Test](image-1.png)

### Tipps

1. **Mit der Dokumentation abgleichen.** Das entfernte P8-RAG (`https://rag2026.covaga.xyz`) bestätigt die genauen Aktionsnamen und Parameter.
2. **Aussagekräftige Docstrings und Typannotationen schreiben.** Daraus entstehen Beschreibung und Eingabeschema für das LLM.
3. **Pfade beachten.** Windows-Pfade benötigen maskierte Backslashes (`\\`) oder Schrägstriche (`/`).

## Private Erweiterungsmodule (`EPLAN_MCP_EXTENSIONS`)

Der Server kann **Werkzeugmodule außerhalb dieses Repositorys** laden, etwa firmenspezifische oder private Add-in-Tests und interne Abläufe, die nicht öffentlich abgelegt werden dürfen.

Setze `EPLAN_MCP_EXTENSIONS` auf ein oder mehrere Verzeichnisse (unter Windows durch `;` getrennt). Beim Start werden alle `*.py`-Dateien auf der obersten Ebene importiert, deren Name nicht mit `_` beginnt; ihre `__all__`-Funktionen werden wie eingebaute Aktionen als MCP-Werkzeuge registriert:

```python
# my_company_tools.py (in einem privaten Repo, NICHT in eplan-rag-mcp)
TOOL_PREFIX = "acme_"          # optional; Standard: "eplan_"
__all__ = ["run_smoke_test"]

import actions                  # api/ des Servers liegt auf sys.path
from actions._base import _get_connected_manager

def run_smoke_test(project_path: str) -> dict:
    """Dieser Docstring wird zur Werkzeugbeschreibung für das LLM."""
    clone = actions.scratch_project_create(project_path)
    ...
    return {"success": True}
```

Regeln und Verhalten:

- `TOOL_PREFIX` versieht Werkzeuge mit einem Namensraum (z. B. `acme_run_smoke_test`).
- Erweiterungen dürfen dieselben Module wie eingebaute Aktionen importieren: `actions`, `actions._base`, `actions.scripted._execute_script` (C# in EPLAN ausführen) und `eplan_connection`.
- Fehlerhafte Erweiterungen werden auf stderr gemeldet und übersprungen; sie verhindern den Serverstart nicht.
- `eplan_list_extensions` zeigt die geladenen Erweiterungen.

Mit Lebenszyklus- und Testprojektwerkzeugen lässt sich so ein privates EPLAN-Add-in unbeaufsichtigt entwickeln: DLL bauen → bereitstellen → `eplan_app_restart` → registrierte Aktionen prüfen (z. B. mit `FindAction`) → an einem Wegwerfprojekt testen → Meldungen über `eplan_get_system_messages` auswerten.

## Automatische Auswahl der EPLAN-Version

**Keine Konfiguration erforderlich.** Beim Start durchsucht der Server `C:\Program Files\EPLAN\Platform` nach installierten Versionen:

- **Automatik (Standard):** `eplan_connect` wählt die **neueste installierte Version** und die passende .NET-Laufzeit: coreclr für EPLAN 2027+ und .NET Framework für 2026 und älter.
- **Explizit:** Das LLM listet mit `eplan_versions` die Installationen auf und verbindet sich mit einer gewünschten Version, etwa `eplan_connect(version="2026")`.

Hinweise:

- Bei einem abweichenden Installationsort `EPLAN_PLATFORM_ROOT` auf dessen `Platform`-Ordner setzen.
- Sobald die DLLs einer Version geladen sind, erfordert ein Versionswechsel einen Neustart des MCP-Servers; die .NET-Laufzeit lässt sich nicht im Prozess austauschen.
- `eplan_connect` akzeptiert auch `host` (oder `"host:port"`) für eine EPLAN-Instanz auf einem anderen Rechner. Die automatische Portsuche funktioniert nur auf localhost.

## Verwandte Repositories

| Repository | Beschreibung |
|---|---|
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | Eigenständiger, hostunabhängiger Claude-Code-Skill. Installation: `/plugin marketplace add covagashi/eplan-development-skill`. |
| [**eplan-ctxmenu-kit**](https://github.com/covagashi/eplan-ctxmenu-kit) | Eigene Einträge im EPLAN-Kontextmenü und Zugriff auf die angeklickte Zeile; mit Erkundungswerkzeug und Beispiel. |

## Ressourcen

- [EPLAN-API-Dokumentation](https://www.eplan.help/)
- [MCP-Protokollspezifikation](https://modelcontextprotocol.io/)
- [Claude-Code-Dokumentation](https://docs.anthropic.com/claude-code)

## Lizenz

MIT — siehe [`license`](license).
