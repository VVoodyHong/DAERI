# 호스트 플러그인과 실행 연결

상태: 클라우드 접속부 설계와 기존 로컬 플러그인 구현 계약.
로컬 Claude Code 플러그인 정적 검사 완료; 실제 호스트 설치·하위 에이전트 전달·대화 UI는 별도 검증.

Codex·Claude Code에 DAERI를 배포하고 사용자 요청을 실행 환경에 연결하는 흐름을 정의한다.
환경 준비는 [설치·배포](../installation/environment.md), setup 입력은 [사용자용 YAML](../configuration/profiles.md#사용자용-setup-yaml), 업무 확인은 [대화 승인](approval.md)이 정본이다.

## 클라우드 접속부

플러그인은 프로젝트·서비스 선택, 업무 요청, 진행 표시, 실제 사용자 승인·피드백, 결과·파일 전달을 담당한다.
서버 연결부가 현재 사용자·업무에 맞춰 기억을 제공하고 AWS의 `InvokeHarness`를 호출한다.
실제 승인 응답과 호출 대기를 왕복하는 연결은 [승인 계약](approval.md#클라우드-접속부의-승인-전달)을 따른다.

기존 호스트의 로컬 판단·도구 반복은 클라우드에서 관리형 harness로 옮긴다.
각 사용자는 공용 기억을 회상하고, 같은 사용자의 업무 재개는 현재 권한·업무·세션을 확인해 연결한다.
클라우드 전송·로그인·스트리밍·재개는 [개발 단계](../../development/dependencies.md#클라우드-개발-상태)의 구현 대상이다.

## 기존 로컬 구현의 적용 범위

아래 절은 현재 네 Skill·하위 에이전트 지침·로컬 runtime의 설치와 실행 계약이다.

## 공통 구성

플러그인은 네 Skill·하위 에이전트 지침·로컬 DAERI MCP runtime을 연결한다.
호스트는 판단·협업·결과 작성을, runtime은 승인된 실행·기록·자동 기억을 맡으며 책임 경계는 [아키텍처](../../architecture/overview.md)를 따른다.

| 구성 | 역할 |
|---|---|
| 호스트별 플러그인 | 마켓플레이스 등록·설치와 Skill·runtime 연결 |
| setup Skill | 환경 준비·profile·리소스 연결 관리 진입 |
| profile Skill | 현재 대화의 기본 대상 선택·전환, profile 백업·확인 후 복원·삭제 |
| work Skill | 총괄 에이전트의 계획·승인·직접 수행·작업 배정·결과 통합·보고 지침 |
| 하위 에이전트 지침·호스트 진입 | 배정받은 목표·단계의 수행 루프와 근거 반환, 호스트별 네이티브 하위 에이전트 연결 |
| list-profiles Skill | 저장한 profile 목록 요약 |
| 공통 참조 문서 | 업무·승인·리소스 계약을 필요할 때 읽는 자료 |
| 로컬 실행 진입점 | 필요한 환경을 준비하고 설치된 DAERI MCP 실행 |

두 호스트는 같은 Skill·제품 계약·사용자 저장소를 사용하고 설치 사본별 실행 환경을 준비한다.
개발 지침은 저장소에, 사용자용 Skill·제품 참조는 설치한 플러그인에 둔다.

## 사용자 안내

setup·profile·list-profiles·work의 대화 응답은 확인한 결과와 필요한 사용자 선택·다음 조치로 구성한다.
기본 언어는 한국어이며 사용자가 다른 언어를 명시하면 따른다.
명령·설정 키·제품명은 원래 표기를 유지하고 운영 대상은 `profile`로 부른다.
대상을 구별할 때는 사용자가 지정한 값과 필요한 식별자를 표시한다.
진행 안내는 준비·설정 확인·연결 확인·업무 분석처럼 사용자가 이해할 작업 단계로 표현한다.

| 상황 | 사용자에게 제공할 내용 |
|---|---|
| 선택·입력 대기 | 현재 질문·선택지와 입력에 필요한 예시 |
| 조회·작업 완료 | 요청한 결과와 필요한 후속 조치 |
| 초기 준비 필요 | 관리 진입이 반환한 짧은 메시지와 현재 호스트의 setup 호출 |
| 실패·부분 결과 | 실패한 작업의 영향과 해결에 필요한 다음 조치 |
| 사용자 직접 조치 | 편집할 YAML·전달할 산출물의 위치, 제공자 로그인 화면 등 해당 행동에 필요한 정보 |
| 상세 진단 요청·직접 복구 필요 | 확인한 기술적 원인과 관련 프로그램·버전·경로·명령, 조치의 영향 |

계획·승인에서는 수행 방법·자료·권한·예상 효과와 확인이 필요한 사항을 [승인 계약](approval.md)에 따라 제시한다.
첫 준비의 `setup_required`는 정상 안내로 처리하고 실제 실행 실패와 구분한다.

호스트의 도구 호출 카드·터미널 출력·권한 확인 UI는 호스트가 표시한다.
이 계약은 DAERI가 작성하는 대화 안내와 결과 요약의 범위를 정의한다.

## 배포와 최초 사용

배포자는 소스와 호스트별 마켓플레이스 구성을 제공한다.
사용자는 마켓플레이스 추가·플러그인 설치 후 호스트의 setup 진입점으로 세팅한다.
setup은 아래 [정형 질문](#setup의-정형-질문)과 [환경 준비](../installation/environment.md#아무-개발-도구가-없는-최초-진입) 순서로 진행한다.
준비 결과는 profile 저장·리소스별 연결·기억·현재 호스트 MCP 연결로 구분한다.
해당 대화에서 확인한 profile을 대상으로 work 또는 자연어로 업무를 요청한다.

### 호스트별 구성

| 항목 | Codex | Claude Code |
|---|---|---|
| 마켓플레이스 | `.agents/plugins/marketplace.json` | `.claude-plugin/marketplace.json` |
| 플러그인 정의 | `.codex-plugin/plugin.json` 호환 형식 | `.claude-plugin/plugin.json` |
| MCP 구성 | 준비 성공 후 생성한 루트 `.mcp.json`을 기본 탐색 | 같은 루트 파일을 기본 탐색 |
| 공유 Skill | `skills/<Skill명>/SKILL.md`의 setup·profile·work·list-profiles | 같은 네 Skill |
| 하위 에이전트 진입 | 호스트가 제공하는 기본 하위 에이전트에 공통 지침의 설치된 절대 경로 전달 | 루트 `agents/subagent.md`를 기본 탐색해 `daeri:subagent`로 사용 |
| 공통 하위 에이전트 지침 | `skills/work/references/subagent.md` | 같은 지침을 `${CLAUDE_PLUGIN_ROOT}`에서 읽음 |
| 사용자 진입 | `$daeri:setup`, `$daeri:profile`, `$daeri:work`, `$daeri:list-profiles` 또는 Skill 선택 화면 | `/daeri:setup`, `/daeri:profile`, `/daeri:work`, `/daeri:list-profiles` |
| 준비 동작 | 선택한 Skill이 설치된 플러그인 루트에서 OS별 준비 실행 | 같은 방식 |

두 카탈로그의 이름은 `daeri-marketplace`, 플러그인 이름은 `daeri`다.
각 카탈로그는 저장소 루트의 플러그인을 가리키며 호스트는 설치한 사본에서 Skill과 프로그램을 읽는다.
첫 배포물은 Skill·하위 에이전트 지침·프로그램 소스를 전달하고 MCP 연결 구성은 해당 사본의 전용 환경 준비 성공 후 생성한다.
마켓플레이스를 추가할 때는 두 호스트의 카탈로그를 포함한 저장소 전체를 제공한다.

DAERI runtime의 MCP 등록명과 초기화 응답의 `serverInfo.name`은 `runtime`이다.
Claude Code는 [서버 이름 규칙](https://code.claude.com/docs/en/plugins/components#server-names-tool-names-and-reloads)에 따라 `plugin:daeri:runtime`으로 표시한다.
공개 도구 이름은 [MCP 규격](../mcp/common.md)의 `daeri_*` 형식을 따른다.

첫 설치 명령과 사용자 호출은 [사용자 안내](../../../README.md#설치)가 정본이다.
호스트를 종료한 상태에서 OS 터미널로 설치하고 성공 뒤 호스트를 시작한다.
사내 배포는 해당 clone URL·원격 브랜치를 사용한다.
Codex의 브랜치는 [공식 CLI 규격](https://learn.chatgpt.com/docs/cli/reference)의 `--ref`, Claude Code는 [터미널 설치 규격](https://code.claude.com/docs/en/discover-plugins#manage-plugins-from-your-shell)의 URL `#ref`로 지정한다.

Codex 플러그인의 Skill 이름은 [공식 구현](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/core-skills/src/loader/namespace.rs#L175-L180)의 `플러그인명:Skill명`이며 `$`는 호스트의 Skill 호출 표시다.
`/skills`·`$` 선택기 또는 앱의 Skill 선택 화면에서도 DAERI의 같은 Skill을 선택할 수 있다.
Claude Code는 [공식 이름 규칙](https://code.claude.com/docs/en/skills#choose-where-skills-load)에 따라 `/플러그인명:Skill명`으로 노출한다.

이미 실행 중인 호스트에서 설치·업데이트한 경우의 새로고침은 복구 절차로만 안내한다.
Claude Code에서는 `/reload-plugins`로 플러그인 변경을 반영한다.
활성화한 플러그인의 `/daeri:setup`이 목록에 없으면 `/reload-skills`로 Skill·명령 디렉터리를 다시 읽고 `/skills` 또는 `/` 메뉴에서 확인한다.
그래도 없으면 `/plugin`의 Installed에서 활성화 상태를, Errors에서 로딩 실패를 확인한다.
새로고침 지원이 없거나 로딩 문제가 지속되면 Claude Code 프로세스를 종료하고 다시 실행한다.
두 새로고침의 역할은 [공식 명령 안내](https://code.claude.com/docs/en/commands)를 따른다.
`/reload-skills`의 Skill 목록 노출과 MCP의 실제 연결은 각각 확인한다.

이미 pull한 로컬 사본을 쓰려면 두 호스트 모두 마켓플레이스 추가의 URL 대신 해당 저장소 루트 경로를 받는다.
공백이 있는 경로는 터미널에서 한 인자로 인용한다.
로컬 경로 추가는 해당 경로에 준비한 checkout을 사용한다.
비공개 저장소의 전달은 사용자의 Git 인증·회사 접근 정책을 충족해야 한다.
DAERI profile 인증은 이후 등록 리소스의 연결에 사용한다.

패키징은 공식 [Codex](https://developers.openai.com/plugins/build/plugins)·[Claude Code](https://code.claude.com/docs/en/plugins) 규격을 따른다.
생성한 MCP 구성은 두 OS에서 설치 사본의 전용 `.venv/bin/python.exe`와 `-I -m daeri.server`를 사용한다.
공통 실행 파일·자식 환경은 [환경 격리](../installation/environment.md#공통-실행-파일과-환경-격리), 구성 발행과 모듈 책임은 [개발 안내](../../development/dependencies.md#실행-진입점과-모듈)가 소유한다.

### 설치한 플러그인 업데이트

배포자는 새 코드가 캐시에 반영되도록 플러그인·실행 패키지 버전을 함께 올린다.
사용자는 처음 등록한 Git 브랜치를 갱신하므로 다른 브랜치나 고정된 태그로 설치했으면 배포 대상을 먼저 확인한다.
기본 배포 대상은 `main`이며 이전 릴리즈 브랜치로 등록한 마켓플레이스는 `main`을 보도록 다시 등록한다.
마켓플레이스 목록과 설치한 플러그인을 각각 갱신하며 아래 명령은 OS 터미널에서 실행한다.

```shell
claude plugin marketplace update daeri-marketplace
claude plugin update daeri@daeri-marketplace
```

```shell
codex plugin marketplace upgrade daeri-marketplace
codex plugin add daeri@daeri-marketplace
```

명령은 [Claude Code 업데이트](https://code.claude.com/docs/en/discover-plugins#update-plugins-now)와 [Codex 플러그인 관리](https://learn.chatgpt.com/docs/developer-commands#codex-plugin-marketplace)를 따른다.
업데이트 뒤 호스트를 다시 시작하고 setup으로 플러그인 프로젝트 버전과 설치한 runtime 패키지 버전을 대조한다.
버전이 다르거나 환경·MCP 등록이 누락되면 명시적 준비로 해당 사본을 준비하고 연결 구성을 발행한다.
업데이트 반영은 기본 검사와 배포본·실행 패키지의 버전 일치, 아래 새 구성 읽기와 실제 MCP 연결로 확인한다.
프로그램 환경은 버전별 설치 사본에 두고 profile·업무·기억은 별도 사용자 저장소를 사용한다.
업데이트 뒤 자료는 [기존 보존·만료 상태](../installation/environment.md#업데이트와-데이터-보존)를 승계한다.

## 설치부터 업무 시작까지의 실제 경계

같은 PC·OS 사용자의 Codex와 Claude Code는 같은 사용자 저장소의 profile·연결·기억·업무 기록을 사용한다.
각 호스트가 설치한 플러그인 사본의 Python 환경과 MCP 연결 구성은 그 사본에서 준비한다.
연결 진입의 저장소는 명시한 CLI `--data-dir` → `DAERI_DATA_DIR` → 해당 설치 사본의 MCP 구성에 저장한 경로 → OS 기본 위치 순서로 선택하고 해소한 절대 경로를 MCP 구성에 고정한다.
처음 연결하는 다른 호스트 사본도 사용자 지정 저장소를 쓰려면 같은 경로를 명시한다.

기존 profile의 선택·목록·업무 진입은 [관리 스크립트](../../development/dependencies.md#관리-입력과-결과)에 `--connect profile list` 또는 `--connect profile show PROFILE`을 전달한다.
이 경로는 현재 설치의 runtime·버전·MCP 구성을 확인하고 필요한 실행 환경만 준비한 뒤 일반 `profile list/show` JSON을 반환한다.
선택한 profile이 로컬 임베딩을 사용하면 이 설치의 추론 패키지를 준비하고 기존 모델·색인·접속 정보·인증을 재사용한다.
등록 profile이 있으면 profile 선택이나 요청한 업무로 이어가고, 새 profile 설정·명시적인 구성 변경은 [setup](#setup의-정형-질문)으로 진행한다.

처음 설정하는 PC의 setup은 사용자가 세팅 시작을 선택한 뒤 [전용 환경 준비와 MCP 구성 발행](../installation/environment.md#구현한-자동-준비의-입출력과-경계)을 수행한다.
플러그인 전달·로컬 준비·리소스 연결·업무 실행은 각 단계의 실제 결과로 안내한다.
같은 이름의 profile을 수정하면 [YAML 변경 계약](../configuration/profiles.md#검증과-변경-의미)에 따라 저장한 구성에서 요청한 설정만 이어간다.

관리 진입의 MCP 등록 보고가 `next_action: reload_host`이면 호스트가 설치 사본의 새 연결 구성을 다시 읽는다.
`check_host_connection`이면 현재 호스트의 실제 연결을 확인한다.
Claude Code는 `/reload-plugins` 또는 새 세션을 사용한다.
Codex는 현재 클라이언트에서 지원을 확인한 새로고침을 사용하거나 프로세스를 재시작한다.
호스트의 실제 MCP 초기화와 `runtime` 서버의 도구 목록이 확인되면 업무 실행으로 이어간다.

로컬 기본 검사의 `ready`와 호스트 연결은 별도 상태다.
`ready: true`이고 `runtime` 서버가 열거되지 않은 경우에는 루트 `.mcp.json`의 존재를 확인한다.
파일이 없으면 이미 동의한 세팅 범위에서 명시적 준비를 다시 실행하고, 파일이 있으면 새 구성 읽기·실제 연결 결과를 확인한다.
등록 뒤 전용 Python·필수 실행 패키지의 손상은 실제 연결 실패로 진단하고 setup의 복구 경로로 이어간다.
임베딩 모델·색인의 준비 상태는 [기억 검색 준비](../installation/environment.md#임베딩-선택과-준비)에서 별도로 확인한다.
runtime의 표준 출력은 MCP 전달에만 사용하며 시작 실패는 진단 출력으로 구분한다.

호스트는 각 단계에 필요한 도구를 이름으로 찾아 입력 스키마를 읽고 호출한다.
지연 로딩 도구는 Claude Code의 [ToolSearch](https://code.claude.com/docs/en/mcp#scale-with-mcp-tool-search), 다른 호스트의 도구 검색·목록 경로로 발견한다.
발견 실패는 서버의 실제 연결 상태·도구 목록·호스트 지원 조건을 확인한 뒤 해당 제한과 복구 경로를 안내한다.

## setup의 정형 질문

새 profile 설정·기존 구성 변경은 **profile 확인 → 시작 확인 → 리소스 종류·제품·연결 개수 확인 → 임베딩 선택 → YAML 편집 → 적용 확인** 순서로 진행한다.
첫 대화 응답은 [DAERI 시작 로고](../../../skills/setup/SKILL.md#시작-로고)와 현재 단계의 질문·진행 안내로 구성한다.
profile 문자열은 대화 입력, 선택·확인은 질문 도구, 접속 정보·인증값은 [사용자용 YAML](../configuration/profiles.md#사용자용-setup-yaml)로 받는다.
사용자 입력은 아래 단계 표로 구성하고 기존 설정의 보존·변경은 YAML 적용 계약을 따른다.
초안이 있으면 그 선택·편집을 재사용하고 파일 작성 완료 응답은 바로 검증으로 이어간다.
호스트는 [setup Skill](../../../skills/setup/SKILL.md#세팅-순서)로 각 입력을 관리 호출·다음 단계에 연결한다.

| 입력 조건·단계 | 사용자에게 표시할 내용 | 응답 후 처리 |
|---|---|---|
| profile 미지정 | 자유 문자열 입력 요청과 형식 예시 | 실제 입력을 기다리고 검증 후 시작 확인 |
| profile이 지정된 새 세팅 | 세팅 시작·중단 확인 | 시작이면 환경 준비, 중단이면 현재 설정을 보존하고 종료 |
| 리소스 선택 | “연결할 리소스 종류를 선택해 주세요.” | 종류를 복수 선택하고 각 종류의 제품 확인 |
| 선택한 종류에 제품이 여럿 | “데이터베이스에서 사용할 제품을 모두 선택해 주세요.”처럼 해당 종류를 표시 | 제품을 복수 선택하고 각 제품의 연결 개수 확인 |
| 제품 선택 완료 | “PostgreSQL은 몇 개 연결할까요?”처럼 실제 제품명과 연결 개수 질문 | 제품별로 사용자가 확정한 1 이상의 정수를 YAML 생성에 사용 |
| 초안·기존 profile에 임베딩 설정 없음 | “기억 검색에 사용할 임베딩 방식을 선택해 주세요.” | 아래 선택값으로 YAML 구성 |
| 회사 API의 연결 방식 미확인 | “회사 API의 연결 방식을 확인해 주세요.” | 실제 지원 목록과 회사 규격을 대조해 선택; 미확인이면 초안 보존 |
| YAML 생성 완료 | 생성한 파일 위치와 “파일을 작성한 뒤 알려 주세요.” | 작성 완료 응답을 받아 검증 |
| 파일 검증 성공 | 변경 요약과 “이 설정을 적용할까요?”; “적용”·“중단” | 적용이면 확인한 설정 저장, 중단이면 편집 초안을 보존하고 종료 |

profile의 형식 오류는 [입력 규격](../configuration/profiles.md#구현한-profile-입력과-공개-설정)과 유효한 예시를 안내하고 다시 입력받는다.

임베딩 선택의 표시와 설정값은 다음과 같다.
기억 정본·색인은 로컬에 두며 아래 내부 설정값은 표시명·설명의 선택지로 안내한다.

| 선택지 | 설명 | `embedding.provider` |
|---|---|---|
| 로컬 임베딩 모델 | 이 PC에 모델을 준비해 벡터를 생성합니다. | `local` |
| 회사 임베딩 API | 회사 API에 허용한 텍스트를 보내 벡터를 생성합니다. | `api` |
| 키워드 검색만 사용 | 임베딩 없이 키워드로 기억을 검색합니다. | `keyword_only` |

회사 API의 연결 방식은 `setup catalog`의 `embedding_adapters`를 사용한다.
선택지 표시명·설명은 각 항목의 `label`·`description`, 설정값은 `adapter_id`다.
“규격 확인 필요”는 회사 계약을 확인하고 미완료 초안을 보존하는 진행 선택으로 함께 제공한다.
목록이 하나여도 회사 API가 해당 인증·요청·응답 규격을 제공하는지 사용자 확인을 받는다.
회사의 규격이 미확인이면 [미완료 API 초안](../configuration/profiles.md#공통-구조와-어댑터-입력)을 보존하고 계약 확인·구현된 방식 선택 또는 키워드 전용 전환을 안내한다.
세부 입력·선택 적용·기존 설정 호환은 [임베딩 설정](../configuration/profiles.md#임베딩-제공자-선택)을 따른다.

사용자에게 포트는 “리소스 종류”, 연결할 어댑터는 “제품”으로 표현한다.
`setup catalog`의 그룹은 다음 표시·선택 계약으로 사용한다.

| 필드 | 역할 |
|---|---|
| `groups[].label` | 종류 선택지의 표시명; 데이터베이스·캐시·인프라·소스 코드·문서·이슈 |
| `groups[].description` | 종류 선택지의 설명; “사용 가능한 제품: …”과 해당 그룹의 모든 제품명 |
| `groups[].type` | 사용자가 선택한 종류를 연결하는 내부 값 |
| `groups[].instance_prefix` | 코드가 제공하는 기본 연결 이름; 생성·복수 연결은 [YAML 이름 규칙](../configuration/profiles.md#공통-구조와-어댑터-입력) 적용 |
| `groups[].products[].display_name` | 복수 제품이 있는 종류에서 선택할 제품명 |
| `groups[].products[].recipe_id` | 제품 선택 뒤 YAML 생성에 사용하는 내부 값 |

선택지의 표시명·설명은 해당 필드의 문자열을 그대로 사용하고 제품 선택은 위 단계 표를 따른다.
요청에서 알려준 개수를 재사용하고 나머지만 “1개”·“2개”·“3개”와 호스트의 입력 경로로 받아 제품별 1 이상 정수로 확정한다.
형식 오류는 유효한 예시와 함께 해당 제품의 개수를 다시 받는다.
YAML 안내에는 확정한 종류·제품·개수를 짧게 표시한다.

개수 확인은 Skill을 따르는 호스트의 책임이다.
runtime은 [초안 생성 계약](../configuration/profiles.md#생성과-파일-위치)의 반복된 `--resource` 항목을 검증해 연결 블록으로 변환한다.
업무의 세부 조회 대상은 [계획 승인](../tasks/approval.md)을 따른다.

카탈로그의 모든 범주는 같은 선택 단계와 복수 선택 형식을 사용한다.
질문 도구의 항목 한도 때문에 나눠야 하면 같은 복수 선택 형식의 페이지로 나눈다.
각 페이지는 순서만 표시하고 가능한 호스트에서는 같은 질문 호출에 함께 제공한다.
페이지는 실제 항목만으로 도구의 최소·최대 선택지 수를 만족하도록 균등하게 구성한다.
추가 범주·제품도 카탈로그 등록만으로 같은 규칙을 따른다.

단일 선택만 지원하는 도구에서는 각 종류·제품에 같은 “사용”·“사용 안 함” 선택을 제공한다.
가능한 질문 수만큼 묶어 표시한다.
호출 인자는 현재 질문 도구 스키마에 선언된 필드로 구성한다.

시작 확인은 해당 profile 세팅에 필요한 전용 환경과 선택한 연결의 준비를 포함한다.
소스 코드 제품은 “로컬 소스 코드”로 표시하고 개수만큼 디렉터리 경로 입력을 생성하며 [Source 준비](../configuration/profiles.md#준비-카탈로그와-개별-프로그램-설치)에서 접근 상태를 확인한다.
로컬 임베딩 모델 선택은 기기 모델 준비, 회사 API 선택은 인증·API 진단, 키워드 선택은 FTS 검색 준비로 이어간다.
회사 API의 설정 적용 요약에는 endpoint·모델과 허용 기억 본문·검색 질의의 전송을 표시한다.

Claude Code는 [AskUserQuestion](https://code.claude.com/docs/en/hooks#askuserquestion)을 사용한다.
Codex는 현재 제공되는 `request_user_input` 또는 같은 역할의 질문 도구를 사용한다.
기본 입력란·취소 기능은 호스트 UI를 사용한다.
값의 확정과 진행은 해당 질문에 대한 실제 사용자 응답을 조건으로 한다.
중단 응답은 기존 설정·편집 초안을 보존하고 현재 세팅을 종료한다.

질문 도구가 현재 모드에 없으면 필요한 모드를 안내한다.
Codex의 도구가 Plan 모드에서만 제공되면 `/plan`에서 선택·공개 문서 읽기·스키마 조회를 수행한다.
profile 저장소를 여는 YAML 검증과 초안 생성·적용·설치·로그인·진단은 실행 가능한 모드로 돌아온 뒤 진행한다.
모드·권한 전환과 사용자 응답은 호스트가 제공하는 실제 사용자 경로로 받는다.
App Server의 [사용자 입력 규격](https://learn.chatgpt.com/docs/app-server#toolrequestuserinput)과 실제 CLI의 모드별 UI는 각각의 지원 범위로 확인한다.

파일 검증 오류는 YAML 필드 위치·수정 안내로 전달하고 같은 파일의 수정·재검증으로 이어간다.
호스트는 [인증값 계약](../configuration/profiles.md#접속-정보와-인증값)의 검증 요약·인증 참조·등록 상태로 진행한다.
업무 실행은 세팅 완료 뒤 해당 업무 계획의 승인을 받는다.
실제 호스트 질문 화면·모드 전환은 미검증이다.

## Skill 지침

Skill은 첫 입력·관리 호출·결과 분기를 수행하고 필요한 계약을 현재 단계에서 호스트의 파일 읽기 도구로 확인한다.
관리 호출은 [고정 스크립트](../../development/dependencies.md#관리-입력과-결과)가 실행 조건을 판정하며 대화는 [사용자 안내](#사용자-안내)를 따른다.
개발 작업의 읽기 경로는 저장소 AGENTS가 정한다.

[setup](../../../skills/setup/SKILL.md)은 환경·연결 관리 요청과 업무의 미준비 안내를 처리한다.
[profile](../../../skills/profile/SKILL.md)·[list-profiles](../../../skills/list-profiles/SKILL.md)는 [선택·목록](../configuration/profiles.md#선택과-기본값)을, profile은 [삭제](../configuration/profiles.md#profile-삭제)·[암호화 백업·복원](../configuration/profiles.md#이관백업복원)도 처리한다.
[work](../../../skills/work/SKILL.md)는 관련 자연어 요청·명시 호출에서 업무·계획·대화 승인·자동 기억을 현재 [MCP 규격](../mcp/common.md)에 연결한다.

### 총괄 에이전트와 하위 에이전트의 호스트 연결

[work Skill](../../../skills/work/SKILL.md#총괄-에이전트와-하위-에이전트)은 총괄 에이전트의 작업 분해·위임 발급·하위 에이전트 생성·진행 확인·결과 통합을 연결한다.
독립적인 조사·분석이나 별도 문맥의 검증으로 시간·문맥 이득이 있을 때 필요한 수만 생성하고 바로 앞 결과가 필요한 순차 판단은 총괄 에이전트가 이어간다.
사용자는 하나의 업무 계획을 승인하며 분해·배정·병렬 수행도 같은 [승인 범위](../tasks/approval.md#하위-에이전트-위임과-승인-범위)에서 수행한다.

총괄 에이전트는 `daeri_subagent`의 `create`로 승인된 단계·하위 목표·완료 조건·허용 출처를 배정하고 발급된 위임 토큰을 해당 호스트 하위 에이전트의 작업 지시에 전달한다.
작업 지시에는 공통 지침의 설치된 절대 경로와 업무·위임·할당·계획 참조·필요한 근거 요약·기대 결과를 포함한다.
로컬 Source를 배정하면 `daeri_resource_list`에서 확인한 연결별 `target.path`도 호스트 작업 지시에 전달한다.
실제 하위 에이전트 생성·모델 추론·대기는 호스트가 수행하고 DAERI 도구는 위임과 실행을 관리한다.

| 호스트 | 현재 패키지의 연결 방식 |
|---|---|
| Claude Code | 루트 [하위 에이전트 정의](../../../agents/subagent.md)를 기본 `agents/` 탐색으로 제공하고 네이티브 Agent 도구에서 `daeri:subagent`를 선택 |
| Codex | 네이티브 하위 에이전트 생성 도구의 기본 하위 에이전트를 사용하고 공통 지침의 설치된 절대 경로를 지시로 전달 |

Claude Code의 기본 탐색 위치는 [플러그인 규격](https://code.claude.com/docs/en/plugins-reference#plugin-directory-structure), 플러그인 이름 범위와 도구 제한은 [하위 에이전트 규격](https://code.claude.com/docs/en/sub-agents)을 따른다.
현재 `agents/subagent.md`는 `Read`·`Grep`·`Glob`·`Bash`와 `ToolSearch`·`mcp__plugin_daeri_runtime__daeri_subagent`를 제공한다.
하위 에이전트는 호스트 도구로 로컬 Source를 조사하고 조회·분석 근거를 반환하며 파일 작성은 총괄 에이전트가 담당한다.
`Read`는 `${CLAUDE_PLUGIN_ROOT}/skills/work/references/subagent.md`와 필요한 제품 계약을 읽고, DAERI 업무 자료·관찰·완료 기록은 단일 `daeri_subagent` 게이트웨이로 처리한다.
Claude 플러그인 정의는 기본 `agents/` 디렉터리 탐색을 사용한다.

Codex의 생성·후속 지시·대기·종료는 [호스트 하위 에이전트 도구](https://learn.chatgpt.com/docs/agent-configuration/subagents#orchestration-and-thread-controls)를 사용한다.
Codex의 [공식 플러그인 규격](https://developers.openai.com/plugins/build/plugins)은 Skill·MCP 전달 경로의 근거다.
현재 Codex 패키지는 기존 호스트의 기본 하위 에이전트와 DAERI의 공통 수행 지침을 연결한다.
기본 하위 에이전트가 지시받은 절대 경로를 읽어 두 호스트의 같은 [하위 에이전트 수행 지침](../../../skills/work/references/subagent.md)을 적용한다.
파일 작성·실행 도구가 제공되는 하위 에이전트에게는 해당 도구와 업무별 작업 경로로 파일 작성을 배정할 수 있다.
총괄 에이전트는 하위 에이전트의 실제 도구·권한에 맞춰 작성·검증을 분담하고 최종 보고·파일 등록을 처리한다.

할당 복구·수행·근거 반환은 [공통 하위 에이전트 지침](../../../skills/work/references/subagent.md), 위임 상태·호출 수명은 [실행 계약](../tasks/workflow.md#하위-에이전트-위임과-병렬-실행)을 따른다.

중단·재배정은 총괄 에이전트가 DAERI 위임을 철회하고 호스트 도구로 하위 에이전트를 중단하는 두 동작으로 처리한다.
위임 철회는 DAERI의 새 호출 자격을 차단하며 이미 시작한 호출의 결과는 저장 이력으로 확인한다.
네이티브 도구·필요한 DAERI 접점이 없거나 하위 에이전트 시작이 실패하면 위임을 회수하고 총괄 에이전트가 이어간다.

### 하위 에이전트 권한과 신뢰 경계

위임 토큰은 그 소지자에게 배정 범위의 DAERI 호출 자격을 주는 bearer 자격이다.
runtime은 현재 총괄 에이전트 업무·계획·승인·소유권·배정 단계·출처·자료 정책을 검사한다.
실제 수신 하위 에이전트의 신원과 토큰 전달·보관은 호스트의 신뢰 경계이며 사용자 보고·산출물·기억에는 결과와 근거 참조를 사용한다.

하위 에이전트의 실제 도구 권한·프로세스·파일·다른 MCP 접근은 호스트의 도구 구성과 권한 정책이 관리한다.
Claude의 위 도구 목록은 하위 에이전트 진입의 구성이고 Codex 하위 에이전트는 [부모의 sandbox·승인 설정](https://learn.chatgpt.com/docs/agent-configuration/subagents#approvals-and-sandbox-controls)을 상속한다.
DAERI 위임의 강제 범위는 DAERI 도구 호출이며 호스트 전체의 격리는 호스트 sandbox·권한 구성에 따른다.
실제 생성된 하위 에이전트의 도구 목록·MCP 연결·권한 적용은 해당 호스트에서 확인한다.

## 재개·동시 사용·환경 고정

새 대화·문맥 압축·호스트 전환 뒤에는 Skill과 관련 계약을 다시 읽고 [현재 설치의 연결](#설치부터-업무-시작까지의-실제-경계)을 확인한다.
새 업무의 대상 선택 증거가 불확실하면 [profile 선택 계약](../configuration/profiles.md#선택과-기본값)에 따라 정형 질문으로 다시 고른다.

기존 업무는 [work의 재개 절차](../../../skills/work/SKILL.md#업무-재개)로 요청·계획·승인·보고·호출을 복구한다.
사용자의 이어가기 의사가 확인되면 명시적 인계로 현재 호스트에 소유권을 연결한다.
진행 중인 호출의 완료를 기다리고 같은 계획·유효 승인·사용량을 이어가며 필요한 하위 에이전트는 현재 호스트에서 다시 배정한다.
일시중지·입력·승인 대기의 전이는 [업무 재개 계약](../tasks/workflow.md#중단재개와-외부-결과-확인)을 따른다.

호스트 전환·재연결·업데이트·대화 기본 profile 변경 뒤에도 기존 업무의 대상·스냅샷·수행 근거를 보존한다.
현재 실행 환경이나 연결 조건이 미준비이면 해당 준비를 안내한 뒤 저장한 업무의 재개 조건을 다시 검사한다.

## 로컬 실행과 연결 수명

stdio MCP 서버는 호스트가 DAERI에 연결할 때 runtime·저장소를 열고 연결 종료 시 닫는다.
관리 CLI는 저장소가 필요한 명령마다 열고 명령 종료 시 닫는다.

MCP 요청 취소는 해당 응답의 대기를 중단하고 서버 연결은 후속 요청에 계속 사용한다.
이미 시작한 동기 처리는 완료·실패를 기록한 뒤 취소를 전달하며 저장한 결과는 원 요청·호출 키로 조회한다.
업무의 일시중지·취소 상태는 [업무 중단 계약](../tasks/workflow.md#중단재개와-외부-결과-확인)의 도구로 전환한다.

runtime은 해당 업무의 profile에서 필요한 연결만 사용한다.
원격 접속·자료 조회는 승인된 업무에서 필요하다고 판단한 연결을 대상으로 수행한다.
프로그램 준비·리소스 인증·업무 자료 조회는 각각 요청된 범위에서 수행한다.

저장소 이동·삭제·교체를 발견한 runtime의 오류·중단은 [저장소 동일성 검사](../configuration/profiles.md#설정과-업무-기록의-저장)를 따른다.
호스트는 재연결 안내와 해당 복구 조건을 전달하고 새 MCP 연결에서 현재 저장소의 업무 상태를 다시 읽는다.

연결 중단 뒤 읽기는 [업무 복구](../tasks/workflow.md#중단재개와-외부-결과-확인), 보고 후 기억 처리는 [저장한 대기의 재시도](../tasks/memory.md#업무-후-갱신과-정정)로 이어간다.

## 지원 경계와 검증

사용자 PC에서 동작하는 Codex·Claude Code 플러그인을 지원한다.
다른 실행 환경의 지원은 해당 환경의 로컬 프로그램·저장소·인증 접근을 별도로 검증해야 한다.

정적 패키징·MCP 통합의 확인 범위와 실제 호스트 설치·대화·하위 에이전트·권한의 남은 검증은 [개발 안내](../../development/dependencies.md#검증-경계)가 소유한다.
