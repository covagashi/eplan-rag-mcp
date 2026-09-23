# Kit de automatización de EPLAN con IA

[English](README.md) · **Español** · [한국어](README.ko.md) · [Deutsch](README.de.md) · [中文](README.zh-CN.md) · [Русский](README.ru.md)

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/covagashi/eplan-rag-mcp)
[![MCP Badge](https://lobehub.com/badge/mcp/covagashi-eplan_2026_ia_mcp_scripts)](https://lobehub.com/mcp/covagashi-eplan_2026_ia_mcp_scripts)

Automatización asistida por IA para **EPLAN Electric P8** y **EPLAN EEC Pro 2026**, basada en el Model Context Protocol (MCP).

Este repositorio contiene el servidor MCP local que controla una instancia de EPLAN en ejecución. Los tres RAG remotos de documentación están en el repositorio independiente [eplan-cloudflare-rags](https://github.com/covagashi/eplan-cloudflare-rags).

> ¿Trabajas aquí con un LLM? Lee [`llm.md`](llm.md): describe para el modelo todas las funciones y opciones de configuración del kit.

## Estructura de los repositorios

```text
.
└── eplan-p8-mcp-server/          # LOCAL: servidor MCP que controla EPLAN P8
```

| Componente | Tipo | Función | Producto EPLAN |
|---|---|---|---|
| `eplan-p8-mcp-server/` | MCP local en Python | Controlar una instancia de EPLAN desde Claude: abrir/cerrar proyectos, exportaciones, informes y scripts | EPLAN Electric P8 |
| [**eplan-cloudflare-rags**](https://github.com/covagashi/eplan-cloudflare-rags) | Cloudflare Workers remotos, repositorio independiente | Servir la documentación de P8 2026, P8 2027 y EEC Pro mediante MCP y REST | EPLAN Electric P8 y EEC Pro |
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | Skill de Claude Code, repositorio independiente | Ayudar a Claude a escribir scripts, código API y aplicaciones Remote Client correctos | EPLAN Electric P8 |

Cada repositorio tiene su propio README con instrucciones de instalación y uso.

## ¿Qué es MCP?

**MCP (Model Context Protocol)** es un estándar abierto que permite a los asistentes de IA interactuar con herramientas y servicios externos. Claude no se limita a generar código: también puede ejecutar acciones en EPLAN en tiempo real y consultar su documentación.

## Inicio rápido

### Automatización local de EPLAN (P8)

```bash
pip install pythonnet mcp
claude mcp add eplan -- python YOURPATH/eplan-p8-mcp-server/mcp_server/server.py
claude mcp list   # debe mostrar "eplan"
```

Después, inicia EPLAN, abre Claude Code y di `connect to eplan`. La guía completa está en [`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md).

![Claude CLI configurado](image.png)

#### Requisito previo

Antes de conectar el servidor, habilita el acceso remoto en EPLAN: activa **Allow remote access via Remote Client** en **File → Settings… → Workstation → Interfaces → Remote access**.

![Permitir acceso remoto mediante Remote Client](Remoting_Setting_AllowLocalAccess.png)

#### Herramientas disponibles

En el modo `full` predeterminado, el servidor publica **199 herramientas**:

| Grupo | Cantidad | Alcance |
|---|---:|---|
| Conexión y utilidades | 8 | Conectar, elegir versión, consultar estado y listar extensiones |
| Acciones tipadas de EPLAN (`eplan_*`) | 183 | Una herramienta por acción documentada o verificada; se ejecuta sin diálogos mediante un script C# en QuietMode |
| Catálogo de acciones (`eplan_action_catalog` / `_describe` / `_run` / `_ribbon_catalog`) | 4 | Acceso a otras ~1050 acciones disponibles como botones de la interfaz, extraídas de `MFTools.xml` |
| Asset Administration Shell (`aas_*`) | 4 | Importación y exportación de gemelos digitales AAS/AASX |

Las ~1050 acciones del catálogo no tienen una herramienta envoltorio cada una: triplicaría el número de herramientas y empeoraría su selección.

Entre las 183 herramientas tipadas hay cuatro de DataModel en vivo: `eplan_live_query_functions`, `eplan_live_query_pages`, `eplan_live_set_function_text` y `eplan_live_set_connection_designations`. Leen y editan el modelo de objetos del proyecto abierto mediante reflexión en tiempo de ejecución, evitando la limitación de las directivas `using` estáticas del motor de scripts.

El conjunto también permite un ciclo desatendido de desarrollo → despliegue → prueba:

- **Ciclo de vida:** `eplan_app_launch` / `eplan_app_shutdown` / `eplan_app_restart` cierran EPLAN, reemplazan DLL de complementos, reinician, reconectan y reabren el proyecto.
- **Proyectos descartables:** `eplan_scratch_project_*` crea copias temporales de una plantilla.
- **Diagnóstico:** `eplan_get_system_messages` lee el árbol de mensajes, incluidos los errores y avisos visibles en la interfaz.
- **Extensiones privadas:** consulta [la sección correspondiente](#modulos-de-extension-privados-eplan_mcp_extensions).

La [wiki del proyecto](https://github.com/covagashi/eplan-rag-mcp/wiki) contiene la referencia de cada herramienta.

#### Modo discovery

`EPLAN_MCP_MODE=discovery` publica 13 herramientas en vez de 199. Reduce el peso de la lista de herramientas, a cambio de una consulta adicional de búsqueda por tarea; resulta útil para clientes MCP que envían el esquema completo de todas las herramientas en cada solicitud.

**No lo uses en Claude Code.** Claude Code ya difiere los esquemas: envía primero los nombres y obtiene cada esquema cuando lo necesita. Por eso, el listado de 199 herramientas en modo `full` cuesta aproximadamente lo mismo que el de 13 en `discovery`; añadir la secuencia búsqueda → descripción → llamada solo introduce otra ida y vuelta. Consulta las mediciones en [la guía del servidor](eplan-p8-mcp-server/mcp_server/README.md#discovery-mode-eplan_mcp_mode).

### RAG remotos de documentación (P8, EEC Pro, 2027)

Ya están desplegados y no requieren datos locales:

```bash
# Documentación de EPLAN Electric P8 (2026; búsqueda semántica)
claude mcp add eplan-rag -- cmd /c npx mcp-remote https://rag2026.covaga.xyz/mcp

# Documentación de EPLAN EEC Pro 2026
claude mcp add eecpro-rag -- cmd /c npx mcp-remote https://rageecpro.covaga.xyz/mcp

# Documentación de EPLAN Electric P8 (2027; búsqueda por palabras clave/texto completo)
claude mcp add eplan-wiki-2027 -- cmd /c npx mcp-remote https://rag2027.covaga.xyz/mcp
```

`eplan-wiki-2027` es un servidor separado, no una actualización de `eplan-rag` de 2026 a 2027: indexa otra versión de la documentación y emplea otro método de búsqueda (FTS5/bm25 de SQLite sobre [la wiki incluida](https://github.com/covagashi/eplan-cloudflare-rags/tree/main/cloudflare-rag-eplan-2027), frente a Vectorize + bge para búsqueda semántica). Las pruebas con consultas reales muestran comportamientos complementarios: FTS5 destaca al buscar nombres exactos («¿cuál es la firma de X?»); la búsqueda semántica funciona mejor cuando la consulta no comparte vocabulario con la documentación. Instala ambos.

También hay una API REST para verificar nombres de acciones y parámetros mientras desarrollas:

```bash
curl -X POST https://rag2026.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "export project pdf", "topK": 3}'

curl -X POST https://rag2027.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "FindAction", "topK": 3}'
```

Consulta [el README del RAG P8 2026](https://github.com/covagashi/eplan-cloudflare-rags/blob/main/cloudflare-rag-eplan-p8/README.md) y [el del RAG EEC Pro](https://github.com/covagashi/eplan-cloudflare-rags/blob/main/cloudflare-rag-eecpro/README.md) para conocer herramientas, endpoints REST y arquitectura.

### Skill de Claude Code para desarrollar en EPLAN

Los servidores MCP permiten a Claude *actuar* sobre EPLAN. La skill [**eplan-development**](https://github.com/covagashi/eplan-development-skill) le enseña a *escribir código EPLAN correcto*: puntos de entrada de scripts, parámetros de acciones verificados, acceso a la base de datos de artículos, automatización con Remote Client (puertos dinámicos, EPLAN sin interfaz, Cogineer) y problemas de producción como el bloqueo de comandos pseudoasíncronos, el hilo de supervisión del bucle de mensajes, la liberación de recursos y los cambios de remoting de EPLAN 2025.

La skill vive en su propio repositorio y es **independiente del entorno anfitrión**: no presupone un servidor MCP, un ejecutor de scripts ni un índice de documentación concretos. Puede utilizarse por sí sola.

```text
/plugin marketplace add covagashi/eplan-development-skill
/plugin install eplan-development@eplan-skills
```

Este repositorio también funciona como marketplace de plugins y apunta a la misma skill:

```text
/plugin marketplace add covagashi/eplan-rag-mcp
/plugin install eplan-development@eplan-tools
```

La instalación manual y otros detalles están en [el README de la skill](https://github.com/covagashi/eplan-development-skill#readme).

## Añadir nuevas acciones de EPLAN

El servidor MCP local registra herramientas **dinámicamente** a partir de la lista `__all__` de cada paquete de acciones. Basta con implementar y exportar la función; no hay que crear un envoltorio individual.

### 1. Implementa la acción

En `eplan-p8-mcp-server/mcp_server/api/actions/<your_module>.py`:

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

### 2. Exporta la función

Añádela tanto a las importaciones como a `__all__` en `eplan-p8-mcp-server/mcp_server/api/actions/__init__.py`. Se registrará automáticamente como `eplan_open_project`.

### 3. Reinicia el servidor MCP

La nueva herramienta estará disponible cuando reinicies Claude o el servidor.

### 4. Valida con la documentación oficial (opcional)

`eplan-p8-mcp-server/tools/validate_actions.py` coteja los nombres y parámetros de las acciones de los envoltorios con el RAG de la documentación oficial y genera un informe Markdown:

```bash
python eplan-p8-mcp-server/tools/validate_actions.py
```

![Prueba de EPLAN](image-1.png)

### Consejos

1. **Contrasta con la documentación.** Usa el RAG remoto de P8 (`https://rag2026.covaga.xyz`) para confirmar el nombre exacto de cada acción y sus parámetros.
2. **Escribe docstrings y anotaciones de tipo útiles.** Forman la descripción y el esquema de entrada que consulta el LLM.
3. **Cuida las rutas.** En Windows, escapa las barras invertidas (`\\`) o usa barras normales (`/`).

## Módulos de extensión privados (`EPLAN_MCP_EXTENSIONS`)

El servidor puede cargar **módulos de herramientas externos al repositorio**: utilidades privadas o específicas de una empresa que no deben publicarse aquí, como pruebas de complementos o flujos internos.

Define `EPLAN_MCP_EXTENSIONS` con uno o varios directorios (separados por `;` en Windows). Al iniciarse, importa los archivos `*.py` del nivel superior cuyo nombre no empiece por `_` y registra las funciones de su lista `__all__` como herramientas MCP:

```python
# my_company_tools.py (en un repositorio privado, NO en eplan-rag-mcp)
TOOL_PREFIX = "acme_"          # opcional; valor predeterminado: "eplan_"
__all__ = ["run_smoke_test"]

import actions                  # api/ del servidor está en sys.path
from actions._base import _get_connected_manager

def run_smoke_test(project_path: str) -> dict:
    """Este docstring será la descripción de la herramienta para el LLM."""
    clone = actions.scratch_project_create(project_path)
    ...
    return {"success": True}
```

Reglas y comportamiento:

- `TOOL_PREFIX` crea un espacio de nombres (por ejemplo, `acme_run_smoke_test`).
- Una extensión puede importar lo mismo que las acciones integradas: `actions`, `actions._base`, `actions.scripted._execute_script` (para ejecutar C# en EPLAN) y `eplan_connection`.
- Una extensión defectuosa se informa por stderr y se omite, sin impedir el arranque del servidor.
- `eplan_list_extensions` muestra los módulos cargados.

Junto con las herramientas de ciclo de vida y proyectos temporales, esto permite desarrollar complementos privados sin intervención: compilar la DLL → desplegar → `eplan_app_restart` → comprobar que se registraron sus acciones (por ejemplo, con un script `FindAction`) → probarlas en una copia descartable → revisar `eplan_get_system_messages`.

## Selección automática de la versión de EPLAN

**No hay que configurar nada.** Al iniciarse, el servidor examina `C:\Program Files\EPLAN\Platform` y:

- **Modo automático (predeterminado):** `eplan_connect` usa la **versión instalada más reciente** y selecciona el entorno .NET correspondiente: coreclr para EPLAN 2027+ y .NET Framework para 2026 y anteriores.
- **Modo explícito:** el LLM puede listar las versiones con `eplan_versions` y llamar a `eplan_connect(version="2026")` para elegir una concreta.

Notas:

- Si EPLAN está instalado en otro lugar, define `EPLAN_PLATFORM_ROOT` con la ruta a su carpeta `Platform`.
- Tras cargar las DLL de una versión en el proceso, cambiar a otra requiere reiniciar el servidor MCP; no se puede sustituir el entorno .NET en caliente.
- `eplan_connect` acepta `host` (o `"host:port"`) para conectar con EPLAN en otra máquina. La detección automática del puerto solo funciona en localhost.

## Repositorios relacionados

| Repositorio | Descripción |
|---|---|
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | Skill independiente de Claude Code; instala con `/plugin marketplace add covagashi/eplan-development-skill`. |
| [**eplan-ctxmenu-kit**](https://github.com/covagashi/eplan-ctxmenu-kit) | Añade entradas al menú contextual de EPLAN y consulta la fila seleccionada; incluye una herramienta de exploración y un ejemplo. |

## Recursos

- [Documentación de la API de EPLAN](https://www.eplan.help/)
- [Especificación de MCP](https://modelcontextprotocol.io/)
- [Documentación de Claude Code](https://docs.anthropic.com/claude-code)

## Licencia

MIT — consulta [`license`](license).
