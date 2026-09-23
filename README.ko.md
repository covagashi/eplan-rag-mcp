# EPLAN AI 자동화 툴킷

[English](README.md) · [Español](README.es.md) · **한국어** · [Deutsch](README.de.md) · [中文](README.zh-CN.md) · [Русский](README.ru.md)

[![Ask DeepWiki](https://deepwiki.com/badge.svg)](https://deepwiki.com/covagashi/eplan-rag-mcp)
[![MCP Badge](https://lobehub.com/badge/mcp/covagashi-eplan_2026_ia_mcp_scripts)](https://lobehub.com/mcp/covagashi-eplan_2026_ia_mcp_scripts)

Model Context Protocol(MCP)을 기반으로 **EPLAN Electric P8** 및 **EPLAN EEC Pro 2026**을 AI로 자동화하는 도구 모음입니다.

이 저장소에는 실행 중인 EPLAN 인스턴스를 제어하는 로컬 MCP 서버가 있습니다. 세 가지 원격 문서 RAG는 별도 저장소인 [eplan-cloudflare-rags](https://github.com/covagashi/eplan-cloudflare-rags)에 있습니다.

> 여기서 LLM을 사용하나요? [`llm.md`](llm.md)에서 모델이 수행하고 설정할 수 있는 모든 기능을 확인하세요.

## 저장소 구성

```text
.
└── eplan-p8-mcp-server/          # 로컬: EPLAN P8을 제어하는 MCP 서버
```

| 구성 요소 | 유형 | 역할 | EPLAN 제품 |
|---|---|---|---|
| `eplan-p8-mcp-server/` | 로컬 Python MCP | Claude에서 실행 중인 EPLAN 제어: 프로젝트 열기/닫기, 내보내기, 보고서, 스크립트 | EPLAN Electric P8 |
| [**eplan-cloudflare-rags**](https://github.com/covagashi/eplan-cloudflare-rags) | 원격 Cloudflare Workers, 별도 저장소 | P8 2026, P8 2027, EEC Pro 문서를 MCP 및 REST로 제공 | EPLAN Electric P8 및 EEC Pro |
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | Claude Code 스킬, 별도 저장소 | Claude가 올바른 EPLAN 스크립트, API 코드, Remote Client 앱을 작성하도록 지원 | EPLAN Electric P8 |

각 저장소에는 설치 및 사용 방법을 설명하는 자체 README가 있습니다.

## MCP란?

**MCP(Model Context Protocol)**는 AI 어시스턴트가 외부 도구 및 서비스와 상호 작용하게 하는 개방형 표준입니다. Claude는 코드를 생성하는 데 그치지 않고 EPLAN 내부에서 실시간으로 작업을 실행하고 검색으로 문서를 참조할 수 있습니다.

## 빠른 시작

### 로컬 EPLAN 자동화(P8)

```bash
pip install pythonnet mcp
claude mcp add eplan -- python YOURPATH/eplan-p8-mcp-server/mcp_server/server.py
claude mcp list   # "eplan"이 표시되어야 함
```

그다음 EPLAN을 실행하고 Claude Code를 열어 `connect to eplan`이라고 요청하세요. 전체 안내는 [`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md)에 있습니다.

![설정된 Claude CLI](image.png)

#### 사전 요구 사항

서버를 연결하기 전에 EPLAN의 **File → Settings… → Workstation → Interfaces → Remote access**에서 **Allow remote access via Remote Client**를 켜야 합니다.

![Remote Client를 통한 원격 액세스 허용](Remoting_Setting_AllowLocalAccess.png)

#### 서버에서 제공하는 도구

기본 `full` 모드에서 서버는 **199개 도구**를 제공합니다.

| 그룹 | 개수 | 기능 |
|---|---:|---|
| 연결/유틸리티 | 8 | 연결, 버전 선택, 상태 확인, 확장 모듈 조회 |
| 유형별 EPLAN 작업(`eplan_*`) | 183 | 문서화되거나 검증된 작업마다 하나의 도구. QuietMode의 C# 스크립트 안에서 조용히 실행되므로 대화상자가 무인 실행을 막지 않음 |
| 작업 카탈로그(`eplan_action_catalog` / `_describe` / `_run` / `_ribbon_catalog`) | 4 | 공식 문서가 아닌 설치본의 `MFTools.xml`에서 찾은, GUI 버튼으로만 존재하는 약 1,050개의 추가 작업 |
| Asset Administration Shell(`aas_*`) | 4 | AAS/AASX 디지털 트윈 가져오기 및 내보내기 |

카탈로그의 약 1,050개 작업에 개별 래퍼 도구를 두지 않은 이유는 도구 수가 세 배로 늘어 다른 도구 선택 정확도가 떨어지기 때문입니다.

183개 유형별 도구에는 `eplan_live_query_functions`, `eplan_live_query_pages`, `eplan_live_set_function_text`, `eplan_live_set_connection_designations`의 네 가지 라이브 DataModel 도구가 있습니다. 런타임 리플렉션으로 열린 프로젝트의 객체 모델을 읽고 편집하며, 스크립트 엔진의 정적 `using` 지시문 제한을 우회합니다.

개별 작업 외에도 무인 개발 → 배포 → 테스트에 필요한 기능을 제공합니다.

- **애플리케이션 수명 주기:** `eplan_app_launch` / `eplan_app_shutdown` / `eplan_app_restart`로 EPLAN 종료, 애드인 DLL 교체, 재실행, 재연결, 프로젝트 다시 열기를 수행합니다.
- **폐기 가능한 테스트 프로젝트:** `eplan_scratch_project_*`로 템플릿 복제본을 만듭니다.
- **진단:** `eplan_get_system_messages`로 GUI에 표시되는 것과 같은 오류 및 경고의 메시지 트리를 읽습니다.
- **비공개 확장 모듈:** [아래 설명](#비공개-확장-모듈-eplan_mcp_extensions)을 참조하세요.

도구별 세부 정보는 [프로젝트 위키](https://github.com/covagashi/eplan-rag-mcp/wiki)에 있습니다.

#### Discovery 모드

`EPLAN_MCP_MODE=discovery`는 199개 대신 13개 도구를 공개합니다. 요청마다 모든 도구의 전체 스키마를 보내는 MCP 클라이언트에서는 토큰 사용량을 줄이지만, 작업마다 검색 왕복 호출이 하나 더 필요합니다.

**Claude Code에서는 사용하지 마세요.** Claude Code 자체가 처음에는 도구 이름만 보내고 필요할 때 스키마를 가져옵니다. 따라서 `full` 모드의 199개 도구 목록도 `discovery` 모드의 13개와 비용이 거의 같으며, 검색 → 설명 → 호출 단계를 추가하면 왕복 호출만 늘어납니다. 비교와 측정치는 [서버 안내서](eplan-p8-mcp-server/mcp_server/README.md#discovery-mode-eplan_mcp_mode)를 참조하세요.

### 원격 문서 RAG(P8, EEC Pro, 2027)

이미 배포되어 있으며 로컬 데이터는 필요하지 않습니다.

```bash
# EPLAN Electric P8 문서(2026, 의미 검색)
claude mcp add eplan-rag -- cmd /c npx mcp-remote https://rag2026.covaga.xyz/mcp

# EPLAN EEC Pro 2026 문서
claude mcp add eecpro-rag -- cmd /c npx mcp-remote https://rageecpro.covaga.xyz/mcp

# EPLAN Electric P8 문서(2027, 키워드/전체 텍스트 검색)
claude mcp add eplan-wiki-2027 -- cmd /c npx mcp-remote https://rag2027.covaga.xyz/mcp
```

`eplan-wiki-2027`은 `eplan-rag`의 2026 → 2027 업그레이드가 아니라 별도 서버입니다. 색인하는 문서 버전도 다르고, 검색 방식도 다릅니다([포함된 위키](https://github.com/covagashi/eplan-cloudflare-rags/tree/main/cloudflare-rag-eplan-2027)에 SQLite FTS5/bm25 키워드 검색 적용, 2026은 Vectorize + bge 의미 검색). 실제 질의 비교에서는 정확한 이름이나 시그니처를 찾을 때 FTS5가 강하고, 질의와 문서에 공통 어휘가 전혀 없을 때 의미 검색이 강했습니다. 둘 다 설치하는 것이 좋습니다.

개발 중 EPLAN 작업 이름과 매개변수를 확인할 때 REST API도 사용할 수 있습니다.

```bash
curl -X POST https://rag2026.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "export project pdf", "topK": 3}'

curl -X POST https://rag2027.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "FindAction", "topK": 3}'
```

도구, REST 엔드포인트 및 구조는 [P8 2026 RAG README](https://github.com/covagashi/eplan-cloudflare-rags/blob/main/cloudflare-rag-eplan-p8/README.md)와 [EEC Pro RAG README](https://github.com/covagashi/eplan-cloudflare-rags/blob/main/cloudflare-rag-eecpro/README.md)를 참조하세요.

### EPLAN 개발용 Claude Code 스킬

MCP 서버는 Claude가 EPLAN에서 *작업을 실행*하게 합니다. [**eplan-development**](https://github.com/covagashi/eplan-development-skill) 스킬은 Claude가 *올바른 EPLAN 코드를 작성*하도록 돕습니다. 스크립트 진입점, 검증된 작업 매개변수, 부품 데이터베이스 접근, Remote Client 자동화(동적 포트, 헤드리스 EPLAN, Cogineer), 그리고 의사 비동기 명령의 블로킹, 메시지 루프 감시 스레드, 리소스 해제 규칙, EPLAN 2025의 remoting 변경 사항 같은 운영상 주의점을 다룹니다.

이 스킬은 자체 저장소에 있으며 특정 **호스트에 종속되지 않습니다**. MCP 서버, 스크립트 실행기 또는 문서 색인을 전제로 하지 않으므로 단독으로도 유용합니다.

```text
/plugin marketplace add covagashi/eplan-development-skill
/plugin install eplan-development@eplan-skills
```

이 저장소도 같은 스킬을 가리키는 플러그인 마켓플레이스입니다.

```text
/plugin marketplace add covagashi/eplan-rag-mcp
/plugin install eplan-development@eplan-tools
```

수동 설치 및 자세한 내용은 [스킬 README](https://github.com/covagashi/eplan-development-skill#readme)를 참조하세요.

## 새로운 EPLAN 작업 추가

로컬 MCP 서버는 각 작업 패키지의 `__all__` 목록에서 도구를 **동적으로 등록**합니다. 따라서 함수를 구현하고 내보내기만 하면 되며 도구별 등록 코드는 필요하지 않습니다.

### 1. 작업 구현

`eplan-p8-mcp-server/mcp_server/api/actions/<your_module>.py`에서:

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

### 2. 함수 내보내기

`eplan-p8-mcp-server/mcp_server/api/actions/__init__.py`의 import 목록과 `__all__` 양쪽에 추가하세요. 그러면 `eplan_open_project`로 자동 등록됩니다.

### 3. MCP 서버 재시작

Claude 또는 서버가 다시 시작되면 새 도구를 사용할 수 있습니다.

### 4. 공식 문서로 검증(선택 사항)

`eplan-p8-mcp-server/tools/validate_actions.py`는 래퍼에 선언된 작업 이름과 매개변수를 공식 EPLAN 문서 RAG와 비교하고 Markdown 보고서를 작성합니다.

```bash
python eplan-p8-mcp-server/tools/validate_actions.py
```

![EPLAN 테스트](image-1.png)

### 팁

1. **문서로 확인하세요.** 원격 P8 RAG(`https://rag2026.covaga.xyz`)에서 정확한 작업 이름과 매개변수를 확인합니다.
2. **의미 있는 docstring과 타입 힌트를 작성하세요.** LLM이 사용하는 도구 설명 및 입력 스키마가 됩니다.
3. **경로를 주의하세요.** Windows 경로는 백슬래시를 이스케이프(`\\`)하거나 슬래시(`/`)를 사용해야 합니다.

## 비공개 확장 모듈(`EPLAN_MCP_EXTENSIONS`)

서버는 이 저장소 **외부의 추가 도구 모듈**을 로드할 수 있습니다. 공개 저장소에 둘 수 없는 사내 애드인 테스트나 내부 워크플로에 유용합니다.

`EPLAN_MCP_EXTENSIONS` 환경 변수에 하나 이상의 디렉터리를 지정하세요(Windows에서는 `;`로 구분). 시작 시 최상위 `*.py` 파일 중 이름이 `_`로 시작하지 않는 파일을 가져와, 해당 파일의 `__all__` 함수를 내장 작업처럼 MCP 도구로 등록합니다.

```python
# my_company_tools.py (eplan-rag-mcp가 아닌 비공개 저장소)
TOOL_PREFIX = "acme_"          # 선택 사항; 기본값 "eplan_"
__all__ = ["run_smoke_test"]

import actions                  # 서버의 api/ 폴더가 sys.path에 포함됨
from actions._base import _get_connected_manager

def run_smoke_test(project_path: str) -> dict:
    """이 docstring이 LLM에 표시되는 도구 설명이 됩니다."""
    clone = actions.scratch_project_create(project_path)
    ...
    return {"success": True}
```

규칙 및 동작:

- `TOOL_PREFIX`는 도구 이름에 네임스페이스를 부여합니다(예: `acme_run_smoke_test`).
- 확장 모듈은 내장 작업과 같은 `actions`, `actions._base`, `actions.scripted._execute_script`(EPLAN에서 C# 실행), `eplan_connection`을 가져올 수 있습니다.
- 오류가 있는 확장 모듈은 stderr에 보고되고 건너뛰며 서버 시작을 막지 않습니다.
- `eplan_list_extensions`는 로드된 모듈을 표시합니다.

수명 주기 및 임시 프로젝트 도구와 조합하면 비공개 EPLAN 애드인 개발을 무인으로 진행할 수 있습니다. DLL 빌드 → 배포 → `eplan_app_restart` → 등록된 작업 확인(예: `FindAction` 스크립트) → 폐기 가능한 프로젝트에서 실행 → `eplan_get_system_messages`로 오류 확인.

## EPLAN 버전 자동 선택

**별도 설정이 필요 없습니다.** 시작할 때 서버는 `C:\Program Files\EPLAN\Platform`에서 설치된 버전을 찾습니다.

- **자동 모드(기본값):** `eplan_connect`가 **설치된 최신 버전**을 선택하고 맞는 .NET 런타임을 사용합니다. EPLAN 2027 이상은 coreclr, 2026 이하는 .NET Framework입니다.
- **명시적 모드:** LLM이 `eplan_versions`로 설치 버전을 확인한 후 `eplan_connect(version="2026")`처럼 특정 버전을 선택할 수 있습니다.

참고:

- 다른 경로에 설치했다면 `EPLAN_PLATFORM_ROOT`를 해당 `Platform` 폴더로 설정하세요.
- 한 버전의 DLL을 프로세스에 로드한 뒤 다른 버전으로 바꾸려면 MCP 서버를 다시 시작해야 합니다. 실행 중 .NET 런타임을 교체할 수는 없습니다.
- `eplan_connect`의 `host`(또는 `"host:port"`)로 다른 컴퓨터의 EPLAN에 연결할 수 있습니다. 포트 자동 탐지는 localhost에서만 작동합니다.

## 관련 저장소

| 저장소 | 설명 |
|---|---|
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | 독립적인 호스트 비종속 Claude Code 스킬. `/plugin marketplace add covagashi/eplan-development-skill`로 설치합니다. |
| [**eplan-ctxmenu-kit**](https://github.com/covagashi/eplan-ctxmenu-kit) | EPLAN 오른쪽 클릭 메뉴에 항목을 추가하고 클릭한 행을 읽는 도구와 예제입니다. |

## 참고 자료

- [EPLAN API 문서](https://www.eplan.help/)
- [MCP 프로토콜 명세](https://modelcontextprotocol.io/)
- [Claude Code 문서](https://docs.anthropic.com/claude-code)

## 라이선스

MIT — [`license`](license)를 참조하세요.
