# EPLAN AI Automation Toolkit

[English](README.md) · [中文](README.zh-CN.md) · **Русский**

[![MCP Badge](https://lobehub.com/badge/mcp/covagashi-eplan_2026_ia_mcp_scripts)](https://lobehub.com/mcp/covagashi-eplan_2026_ia_mcp_scripts)

Автоматизация **EPLAN Electric P8** и **EPLAN EEC Pro 2026** с помощью ИИ, построенная на
Model Context Protocol (MCP).

Репозиторий содержит четыре независимых подпроекта: локальный MCP-сервер, который управляет
запущенным экземпляром EPLAN, и три удалённых MCP-сервера на Cloudflare Workers, которые
отдают проиндексированную документацию EPLAN.

> Работаете здесь вместе с языковой моделью? Прочитайте [`llm.md`](llm.md) — там на языке,
> обращённом к LLM, описано всё, что этот набор инструментов умеет и что в нём настраивается.

## Структура репозитория

```
.
├── eplan-p8-mcp-server/          # ЛОКАЛЬНО: MCP-сервер, управляющий EPLAN P8
├── cloudflare-rag-eplan-p8/      # УДАЛЁННО: Cloudflare Worker с RAG по документации P8 через MCP
├── cloudflare-rag-eecpro/        # УДАЛЁННО: Cloudflare Worker с RAG по документации EEC Pro через MCP
├── cloudflare-rag-eplan-2027/    # УДАЛЁННО: Cloudflare Worker с вики API 2027 (D1/FTS5, поиск по ключевым словам)
└── claude-skills/                # НАВЫК:    зеркало covagashi/eplan-development-skill
```

| Каталог | Тип | Назначение | Продукт EPLAN |
|---|---|---|---|
| `eplan-p8-mcp-server/` | Локальный MCP на Python | Управление запущенным экземпляром EPLAN из Claude: открытие и закрытие проектов, экспорт, отчёты, скрипты | EPLAN Electric P8 |
| `cloudflare-rag-eplan-p8/` | Удалённый Cloudflare Worker | Индекс документации P8 как удалённый MCP + REST API | EPLAN Electric P8 |
| `cloudflare-rag-eecpro/` | Удалённый Cloudflare Worker | Индекс документации EEC Pro как удалённый MCP + REST API | EPLAN EEC Pro 2026 |
| `cloudflare-rag-eplan-2027/` | Удалённый Cloudflare Worker | Вики API 2027 как удалённый MCP; поиск по ключевым словам через D1 + FTS5, дополняет семантический индекс выше | EPLAN Electric P8 2027 |
| `claude-skills/eplan-development/` | Навык Claude Code | Учит Claude писать корректные скрипты EPLAN, код API и приложения Remote Client. Зеркало отдельного репозитория [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | EPLAN Electric P8 |

У каждого подпроекта есть собственный README с деталями установки и использования.

## Что такое MCP?

**MCP (Model Context Protocol)** — открытый стандарт, позволяющий ИИ-ассистентам
взаимодействовать с внешними инструментами и сервисами. Claude не просто генерирует код: он
*выполняет* действия внутри EPLAN в реальном времени и обращается к документации через поиск.

## Быстрый старт

### Локальная автоматизация EPLAN (P8)

```bash
pip install pythonnet mcp
claude mcp add eplan -- python YOURPATH/eplan-p8-mcp-server/mcp_server/server.py
claude mcp list   # в списке должен появиться "eplan"
```

Затем запустите EPLAN, откройте Claude Code и напишите `connect to eplan`. Полное руководство —
в [`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md).

![Claude CLI configured](image.png)

#### Предварительное условие

Прежде чем сервер сможет подключиться, в EPLAN нужно включить удалённый доступ: параметр
**Разрешить удалённый доступ через Remote Client** в разделе **Файл → Настройки… → Рабочее
место → Интерфейсы → Удалённый доступ**.

![Allow remote access via Remote Client](Remoting_Setting_AllowLocalAccess.png)

#### Что предоставляет сервер

В режиме по умолчанию (`full`) сервер публикует **199 инструментов**:

| Группа | Кол-во | Что покрывает |
|---|---|---|
| Подключение и служебные | 8 | Подключение, выбор версии, состояние, список расширений |
| Типизированные действия EPLAN (`eplan_*`) | 183 | По одному инструменту на каждое документированное или проверенное действие; все выполняются молча внутри C#-скрипта в режиме QuietMode, поэтому ни один диалог EPLAN не заблокирует автоматический запуск |
| Каталог действий (`eplan_action_catalog` / `_describe` / `_run` / `_ribbon_catalog`) | 4 | Открывает доступ ещё примерно к 1050 действиям, существующим только как кнопки интерфейса; они извлечены из файла `MFTools.xml` установленной программы, а не из официальной документации |
| Asset Administration Shell (`aas_*`) | 4 | Экспорт и импорт цифровых двойников AAS/AASX |

Для этих ~1050 действий из каталога намеренно *не* сделано по отдельному инструменту: это
утроило бы общее число инструментов и ухудшило бы выбор инструмента для всего остального.

Среди 183 типизированных инструментов есть четыре, работающих с «живой» DataModel, —
`eplan_live_query_functions`, `eplan_live_query_pages`, `eplan_live_set_function_text` и
`eplan_live_set_connection_designations`. Они читают и изменяют объектную модель открытого
проекта через рефлексию во время выполнения, обходя ограничение движка скриптов на
статические директивы `using`.

Помимо отдельных действий, набор покрывает всё необходимое для полностью автономного цикла
разработка → развёртывание → тестирование:

- **Жизненный цикл приложения** — `eplan_app_launch` / `eplan_app_shutdown` /
  `eplan_app_restart`: выйти из EPLAN, заменить DLL надстройки, перезапустить, переподключиться,
  снова открыть проект.
- **Одноразовые тестовые проекты** — `eplan_scratch_project_*`: временные проекты, клонируемые
  из шаблона.
- **Диагностика** — `eplan_get_system_messages`: чтение дерева сообщений EPLAN, те же ошибки и
  предупреждения, которые пользователь видит в интерфейсе.
- **Приватные модули расширений** — см. [ниже](#приватные-модули-расширений-eplan_mcp_extensions).

Справочник по каждому инструменту — в [вики проекта](https://github.com/covagashi/eplan-rag-mcp/wiki).

#### Режим discovery

`EPLAN_MCP_MODE=discovery` публикует 13 инструментов вместо 199: список инструментов перестаёт
съедать токены ценой одного дополнительного обращения к поиску на задачу. Это оправданно для
MCP-клиентов, которые присылают полную схему каждого инструмента в каждом запросе.

**В Claude Code его включать не нужно.** Claude Code сам откладывает загрузку схем: сначала
только список имён, схема запрашивается по требованию. Поэтому полный список из 199
инструментов обходится ему примерно так же дёшево, как 13 в режиме discovery, а
дополнительный слой «поиск → описание → вызов» лишь добавляет один лишний обмен. Компромисс и
измерения описаны в
[`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md#discovery-mode-eplan_mcp_mode).

### Удалённые RAG по документации (P8, EEC Pro, 2027)

Уже развёрнуты и готовы к работе — локальные данные не нужны:

```bash
# Документация EPLAN Electric P8 (2026, семантический поиск)
claude mcp add eplan-rag -- cmd /c npx mcp-remote https://rag2026.covaga.xyz/mcp

# Документация EPLAN EEC Pro 2026
claude mcp add eecpro-rag -- cmd /c npx mcp-remote https://rageecpro.covaga.xyz/mcp

# Документация EPLAN Electric P8 (2027, полнотекстовый поиск по ключевым словам)
claude mcp add eplan-wiki-2027 -- cmd /c npx mcp-remote https://rag2027.covaga.xyz/mcp
```

`eplan-wiki-2027` — намеренно отдельный сервер, а не обновление `eplan-rag` с 2026 на 2027: он
индексирует другую версию документации *и* использует другой режим поиска (SQLite FTS5/bm25 по
вики, входящей в [`cloudflare-rag-eplan-2027/`](cloudflare-rag-eplan-2027/), вместо
семантического поиска Vectorize + bge). При прямом сравнении на реальных запросах они ошибаются
по-разному: FTS5 выигрывает при поиске точного имени («какая сигнатура у X»), семантический
поиск — когда в запросе вообще нет слов из документации. Ставьте оба.

Оба также предоставляют обычный REST API — это удобно для проверки имён и параметров действий
EPLAN во время разработки:

```bash
curl -X POST https://rag2026.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "export project pdf", "topK": 3}'

curl -X POST https://rag2027.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "FindAction", "topK": 3}'
```

Инструменты, REST-эндпоинты и архитектура описаны в
[`cloudflare-rag-eplan-p8/README.md`](cloudflare-rag-eplan-p8/README.md) и
[`cloudflare-rag-eecpro/README.md`](cloudflare-rag-eecpro/README.md).

### Навык Claude Code для разработки под EPLAN

MCP-серверы позволяют Claude *действовать* в EPLAN. Навык
[**eplan-development**](https://github.com/covagashi/eplan-development-skill) учит его *писать
корректный код для EPLAN*: точки входа скриптов, проверенные параметры действий, доступ к базе
изделий, автоматизация Remote Client (динамические порты, EPLAN без интерфейса, Cogineer) — и
подводные камни, которые проявляются только в реальной работе: псевдоасинхронная блокировка
команд, поток-монитор цикла сообщений, дисциплина dispose, изменения remoting в EPLAN 2025.

Навык живёт в собственном репозитории и намеренно **не зависит от окружения**: он не
предполагает ни MCP-сервера, ни конкретного исполнителя скриптов, ни конкретного индекса
документации, поэтому полезен сам по себе — независимо от того, используете ли вы что-то ещё
отсюда. Тот репозиторий является основной копией; `claude-skills/` здесь — его зеркало.

```
/plugin marketplace add covagashi/eplan-development-skill
/plugin install eplan-development@eplan-skills
```

Этот репозиторий также является каталогом плагинов, поэтому навык можно установить и отсюда:

```
/plugin marketplace add covagashi/eplan-rag-mcp
/plugin install eplan-development@eplan-tools
```

Ручная установка и подробности:
[`claude-skills/eplan-development/README.md`](claude-skills/eplan-development/README.md).

## Добавление новых действий EPLAN

Локальный MCP-сервер регистрирует инструменты **динамически**, по списку `__all__` каждого
пакета действий, поэтому добавление действия занимает два шага и не требует шаблонного кода.

### 1. Реализуйте действие

В файле `eplan-p8-mcp-server/mcp_server/api/actions/<your_module>.py`:

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

### 2. Экспортируйте её

Добавьте функцию в импорты **и** в `__all__` в файле
`eplan-p8-mcp-server/mcp_server/api/actions/__init__.py`. После этого она автоматически
зарегистрируется как `eplan_open_project`.

### 3. Перезапустите MCP-сервер

Новый инструмент станет доступен после перезапуска Claude или сервера.

### 4. Сверьтесь с официальной документацией (необязательно)

`eplan-p8-mcp-server/tools/validate_actions.py` сверяет каждое имя действия и каждый параметр,
объявленные в обёртках, с официальным RAG по документации EPLAN и записывает отчёт в markdown:

```bash
python eplan-p8-mcp-server/tools/validate_actions.py
```

![EPLAN test](image-1.png)

### Советы

1. **Сверяйтесь с документацией.** Используйте удалённый RAG по P8
   (`https://rag2026.covaga.xyz`), чтобы подтвердить точное имя действия EPLAN и его параметры.
2. **Пишите содержательные docstring и аннотации типов.** Именно они становятся описанием
   инструмента и схемой входных данных, которые видит и на которые опирается модель.
3. **Аккуратно обращайтесь с путями.** Пути Windows требуют экранирования (`\\`) или прямых
   слэшей (`/`).

## Приватные модули расширений (`EPLAN_MCP_EXTENSIONS`)

Сервер умеет загружать **дополнительные модули инструментов извне этого репозитория** — для
корпоративных или приватных задач (собственные стенды тестирования надстроек, внутренние
процессы), которым не место в публичном репозитории.

Укажите в переменной окружения `EPLAN_MCP_EXTENSIONS` один или несколько каталогов (в Windows
разделитель — `;`). Каждый файл `*.py` верхнего уровня, имя которого не начинается с `_`,
импортируется при запуске, а функции из его `__all__` регистрируются как MCP-инструменты — ровно
так же, как встроенные действия:

```python
# my_company_tools.py  (в приватном репозитории, НЕ в eplan-rag-mcp)
TOOL_PREFIX = "acme_"          # необязательно, по умолчанию "eplan_"
__all__ = ["run_smoke_test"]

import actions                  # каталог api/ сервера уже в sys.path
from actions._base import _get_connected_manager

def run_smoke_test(project_path: str) -> dict:
    """Docstring becomes the tool description the LLM sees."""
    clone = actions.scratch_project_create(project_path)
    ...
    return {"success": True}
```

Правила и поведение:

- `TOOL_PREFIX` задаёт пространство имён инструментов (в примере — `acme_run_smoke_test`).
- Расширения могут импортировать всё, что используют встроенные действия: `actions`,
  `actions._base`, `actions.scripted._execute_script` (запуск C# внутри EPLAN),
  `eplan_connection`.
- Сломанное расширение выводится в stderr и пропускается; оно никогда не мешает серверу
  запуститься.
- `eplan_list_extensions` показывает, что было загружено.

Вместе с инструментами жизненного цикла и одноразовыми проектами это даёт полностью автономный
цикл разработки приватных надстроек EPLAN: собрать DLL → развернуть → `eplan_app_restart` →
убедиться, что действия надстройки зарегистрировались (например, скриптом с `FindAction`) →
выполнить их на одноразовом проекте → `eplan_get_system_messages`, чтобы поймать всё, на что
пожаловался EPLAN.

## Выбор версии EPLAN (автоматически)

**Настраивать ничего не нужно.** При запуске сервер сканирует
`C:\Program Files\EPLAN\Platform` в поисках установленных версий и далее:

- **Автоматический режим (по умолчанию):** `eplan_connect` подключается к **самой свежей
  установленной версии** и сам выбирает подходящую среду .NET — coreclr для EPLAN 2027 и выше,
  .NET Framework для 2026 и старше.
- **Явный режим:** модель может вызвать `eplan_versions`, чтобы получить список установленных
  версий, и затем подключиться к нужной через `eplan_connect(version="2026")` — например,
  «connect to eplan 2026».

Примечания:

- EPLAN установлен в нестандартном месте? Укажите в `EPLAN_PLATFORM_ROOT` путь к его каталогу
  `Platform`.
- Как только DLL одной версии загружены в процесс, переключение на другую версию требует
  перезапуска MCP-сервера — среду .NET нельзя заменить на лету.
- `eplan_connect` также принимает `host` (и `"host:port"`) для подключения к экземпляру EPLAN на
  другой машине; автоопределение порта работает только на localhost.

## Связанные репозитории

| Репозиторий | Что это |
|---|---|
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | Тот самый навык Claude Code, отдельно и без привязки к окружению. Установка: `/plugin marketplace add covagashi/eplan-development-skill`. |
| [**eplan-ctxmenu-kit**](https://github.com/covagashi/eplan-ctxmenu-kit) | Добавление собственных пунктов в контекстные меню EPLAN и чтение строки, по которой щёлкнул пользователь. Инструмент разведки плюс рабочий пример. |

## Ресурсы

- [Документация API EPLAN](https://www.eplan.help/)
- [Спецификация протокола MCP](https://modelcontextprotocol.io/)
- [Документация Claude Code](https://docs.anthropic.com/claude-code)

## Лицензия

MIT — см. [`license`](license).
