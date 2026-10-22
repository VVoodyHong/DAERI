# 호스트 플러그인과 로컬 실행

상태: 공통 오프라인 진입·YAML setup 구현; 실제 호스트 설치·질문 UI·대화 연동 미검증

Codex·Claude Code에 DAERI를 배포하고 사용자의 PC에서 실행하는 흐름을 정의한다.
환경 준비는 [설치·배포](../installation/environment.md), setup 입력은 [사용자용 YAML](../configuration/profiles.md#사용자용-setup-yaml), 업무 확인은 [대화 승인](approval.md)이 정본이다.

## 공통 구성

호스트가 요청을 해석하고 필요한 자료·수행 방법·다음 행동을 판단한다.
플러그인은 setup·profile·work·list-profiles Skill과 로컬 DAERI MCP 실행기를 연결한다.
실행기는 profile·승인·범위·이력·기억을 관리하고 필요한 리소스의 연결을 사용한다.

| 구성 | 역할 |
|---|---|
| 호스트별 플러그인 | 마켓플레이스 등록·설치와 Skill·실행기 연결 |
| setup Skill | 환경 준비·profile·리소스 연결 관리 진입 |
| profile Skill | 현재 대화에서 새 업무의 기본 대상 선택·전환과 확인한 profile 삭제 |
| work Skill | 계획 전 기억 조회·계획·승인·실행·보고·기억 갱신 지침 |
| list-profiles Skill | 저장한 profile 목록 요약 |
| 공통 참조 문서 | 업무·승인·리소스 계약을 필요할 때 읽는 자료 |
| 로컬 실행 진입점 | 필요한 환경을 준비하고 설치된 DAERI MCP 실행 |

공통 Skill·참조는 같은 제품 명세를 따르고 호스트별 차이만 플러그인 구성에서 처리한다.
개발 지침은 DAERI 저장소에, 사용자용 Skill·제품 참조는 설치한 플러그인에 둔다.
판단은 현재 호스트 모델이 담당하고 실행기·저장소는 사용자 PC에서 동작한다.
호스트가 기억의 필요성과 갱신 후보를 판단하며 선택한 임베딩 제공자는 검색용 벡터를 만든다.
플러그인·현재 호스트 모델·로컬 실행기로 기본 구성을 완성한다.

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
| 실패·부분 결과 | 사용자가 이해할 실패한 작업·영향과 가능한 다음 조치를 한두 문장으로 요약 |
| 사용자 직접 조치 | 편집할 YAML·전달할 산출물의 위치, 보안 입력 명령·로그인 화면 등 해당 행동에 필요한 정보 |
| 상세 진단 요청·직접 복구 필요 | 확인한 기술적 원인과 관련 프로그램·버전·경로·명령, 조치의 영향 |

계획·승인에서는 수행 방법·자료·권한·예상 효과와 확인이 필요한 사항을 [승인 계약](approval.md)에 따라 제시한다.
첫 준비의 `setup_required`는 정상 안내로 처리하고 실제 실행 실패와 구분한다.

호스트의 도구 호출 카드·터미널 출력·권한 확인 UI는 호스트가 표시한다.
이 계약은 DAERI가 작성하는 대화 안내와 결과 요약의 범위를 정의한다.

## 배포와 최초 사용

setup은 간단한 선택과 사용자용 YAML을 조합한다.
호스트별 질문 도구는 [선택·확인 계약](#setup의-정형-질문), 접속값 입력은 사용자용 YAML 계약을 따른다.

1. 배포자가 저장소에 DAERI 소스와 호스트별 마켓플레이스 배포 구성을 제공한다.
2. 사용자가 해당 저장소의 마켓플레이스를 Codex 또는 Claude Code에 추가하고 DAERI 플러그인을 설치한다.
3. 사용자가 `/setup {profile}`에 해당하는 호스트 진입점으로 세팅을 시작하고 시작 여부를 확인한다.
4. 최소 환경을 확인·준비하고 실제 카탈로그에서 사용할 리소스와 제품을 선택한다.
5. 선택한 어댑터의 접속 정보·공개 인증 설정만 담은 사용자용 YAML 초안을 생성하고 사용자가 편집한다.
6. 코드가 전체 입력을 검증하고 비밀 없는 변경 요약을 보여 준 뒤 명시적으로 확인한 구성을 하나의 profile 개정으로 적용한다.
7. 선택한 연결의 프로그램·인증·진단과 기억 준비를 진행한다.
8. profile 저장·리소스별 연결·기억 준비·현재 호스트 MCP 연결의 실제 결과와 남은 조치를 구분해 안내한다.
9. 해당 대화에서 확인한 profile을 대상으로 work 또는 자연어로 업무를 요청한다.

초안의 구조·저장 위치·기존 설정 보존·검증·적용 계약은 [사용자용 setup YAML](../configuration/profiles.md#사용자용-setup-yaml)을 따른다.
호스트는 코드가 생성한 파일을 안내하고 검증·적용 결과를 전달한다.
대화의 표시 범위는 [사용자 안내](#사용자-안내), 기술 정보 확인은 [상세 진단](../installation/environment.md#검사계획설치)을 따른다.

`/setup {profile}`은 공통 사용 흐름을 설명하는 논리적 호출 형식이다.
실제 호출 이름과 설치 방식은 아래 호스트별 구성을 따른다.
사용 준비는 소스 배포·마켓플레이스 등록·플러그인 설치·환경 준비·profile 설정의 각 완료 결과로 확인한다.

배포 저장소와 profile이 접근할 Source 리소스는 서로 다른 역할이다.
구체적인 첫 배포·사용 대상은 [사용 예정 환경](../product.md#사용-예정-환경)에 모아 관리한다.

### 호스트별 구성

| 항목 | Codex | Claude Code |
|---|---|---|
| 마켓플레이스 | `.agents/plugins/marketplace.json` | `.claude-plugin/marketplace.json` |
| 플러그인 정의 | `.codex-plugin/plugin.json` 호환 형식 | `.claude-plugin/plugin.json` |
| MCP 구성 | 플러그인 정의 안의 `mcpServers.daeri` | 루트 `.mcp.json`의 `mcpServers.daeri` |
| 공유 Skill | `skills/<Skill명>/SKILL.md`의 setup·profile·work·list-profiles | 같은 네 Skill |
| 사용자 진입 | `$daeri:setup`, `$daeri:profile`, `$daeri:work`, `$daeri:list-profiles` 또는 Skill 선택 화면 | `/daeri:setup`, `/daeri:profile`, `/daeri:work`, `/daeri:list-profiles` |
| 준비 동작 | 선택한 Skill이 설치된 플러그인 루트에서 OS별 준비 실행 | 같은 방식 |

두 카탈로그의 이름은 `daeri-marketplace`, 플러그인 이름은 `daeri`다.
각 카탈로그는 저장소 루트의 플러그인을 가리키며 호스트는 설치한 사본에서 Skill과 프로그램을 읽는다.
마켓플레이스를 추가할 때는 두 호스트의 카탈로그를 포함한 저장소 전체를 제공한다.

아래 URL은 사내 저장소를 가정한 예시다.
실제 clone URL로 바꾸고 지정한 브랜치를 먼저 원격에 배포해야 한다.
첫 설치는 호스트를 종료한 상태에서 OS 터미널로 수행하고 설치 성공 뒤 호스트를 시작한다.
Codex는 [공식 CLI 규격](https://learn.chatgpt.com/docs/cli/reference)에 따라 다음 명령을 사용한다.

```shell
codex plugin marketplace add https://gitlab.example.com/team/DAERI.git --ref main
codex plugin add daeri@daeri-marketplace
codex
```

시작한 Codex의 새 대화에서 `$daeri:setup service-prod`로 준비한다.
준비 후에는 `$daeri:profile service-prod`, `$daeri:work 요청 내용`, `$daeri:list-profiles`로 호출한다.
삭제는 `$daeri:profile delete service-prod`로 요청한다.
Codex 플러그인의 Skill 이름은 [공식 구현](https://github.com/openai/codex/blob/rust-v0.147.0/codex-rs/core-skills/src/loader/namespace.rs#L175-L180)의 `플러그인명:Skill명`이며 `$`는 호스트의 Skill 호출 표시다.
`/skills`·`$` 선택기 또는 앱의 Skill 선택 화면에서도 DAERI의 같은 Skill을 선택할 수 있다.

Claude Code는 [터미널 설치 규격](https://code.claude.com/docs/en/discover-plugins#manage-plugins-from-your-shell)에 따라 아래를 순서대로 실행한다.
브랜치 지정은 URL의 `#ref`이며 Codex 터미널 명령의 `--ref`와 구분한다.

```shell
claude plugin marketplace add 'https://gitlab.example.com/team/DAERI.git#main'
claude plugin install daeri@daeri-marketplace --scope user
claude
```

Claude Code 플러그인은 위 명령에서 사용자 범위로 설치하고 시작한 대화에서 사용한다.
이미 실행 중인 호스트에서 설치·업데이트한 경우의 새로고침은 복구 절차로만 안내한다.
이 경우 `/reload-plugins`로 플러그인 변경을 반영한다.
활성화한 플러그인의 `/daeri:setup`이 목록에 없으면 `/reload-skills`로 Skill·명령 디렉터리를 다시 읽고 `/skills` 또는 `/` 메뉴에서 확인한다.
그래도 없으면 `/plugin`의 Installed에서 활성화 상태를, Errors에서 로딩 실패를 확인한다.
새로고침 지원이 없거나 로딩 문제가 지속되면 Claude Code 프로세스를 종료하고 다시 실행한다.
두 새로고침의 역할은 [공식 명령 안내](https://code.claude.com/docs/en/commands)를 따른다.
`/reload-skills`의 Skill 목록 노출과 MCP의 실제 연결은 각각 확인한다.
그다음 `/daeri:setup service-prod`로 준비하고 `/daeri:work` 또는 자연어로 업무를 요청한다.
대화의 기본 대상을 고를 때는 `/daeri:profile`, 저장한 목록만 볼 때는 `/daeri:list-profiles`를 사용한다.
삭제는 `/daeri:profile delete service-prod`로 요청하며 같은 profile Skill에서 대상·범위를 확인한다.
플러그인 Skill은 [공식 이름 규칙](https://code.claude.com/docs/en/skills#choose-where-skills-load)에 따라 `/플러그인명:Skill명`으로 노출된다.
현재 제공하는 업무 진입 Skill명은 work다.

이미 pull한 로컬 사본을 쓰려면 두 호스트 모두 마켓플레이스 추가의 URL 대신 해당 저장소 루트 경로를 받는다.
공백이 있는 경로는 터미널에서 한 인자로 인용한다.
로컬 경로 추가는 해당 경로에 준비한 checkout을 사용한다.
비공개 저장소의 전달은 사용자의 Git 인증·회사 접근 정책을 충족해야 한다.
DAERI profile 인증은 이후 등록 리소스의 연결에 사용한다.

공식 [Codex 플러그인 안내](https://developers.openai.com/plugins/build/plugins)와 [Claude Code 플러그인 안내](https://code.claude.com/docs/en/plugins)를 기준으로 실제 패키징을 확인한다.
준비 프로그램과 실행기의 책임·연결은 [개발 안내](../../development/dependencies.md#실행-진입점과-모듈)를 따른다.
macOS와 Windows는 같은 전용 Python stdio 진입 구성을 사용한다.
Codex의 command는 `${PLUGIN_ROOT}/.venv/bin/python.exe`, Claude Code는 `${CLAUDE_PLUGIN_ROOT}/.venv/bin/python.exe`이며 args는 `-I`, `-m`, `daeri.server`다.
공통 실행 파일의 OS별 준비는 [환경 격리](../installation/environment.md#공통-실행-파일과-환경-격리)를 따른다.
OS별 shell은 명시적 setup에 사용하고 일반 MCP는 준비된 전용 Python을 직접 실행한다.
실행기의 환경은 설치한 플러그인 루트의 전용 환경으로 고정한다.
setup 준비는 시작 응답과 호스트의 명령 실행 권한, 실제 업무는 업무 계획의 유효 승인을 각각 요구한다.
위 설치 문법·파일 구성은 공식 문서·호스트 소스·로컬 CLI 정의로 검토했다.
실제 마켓플레이스 추가·설치·Skill 호출은 미검증이다.

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
업데이트 뒤 호스트를 다시 시작하고 setup으로 플러그인 프로젝트 버전과 설치한 실행기 패키지 버전을 대조한다.
버전이 다르거나 환경이 없으면 명시적 준비로 실행 환경을 교체하고 새 프로세스로 MCP를 다시 연결한다.
업데이트 반영은 기본 검사와 배포본·실행 패키지의 버전 일치, 새 프로세스 연결로 확인한다.
프로그램 환경은 버전별 설치 사본에 두고 profile·업무·기억은 별도 사용자 저장소를 사용한다.
기존 profile의 보존 설정을 바꾸려면 기존 연결·정책을 유지하면서 해당 설정만 명시적으로 변경한다.
기존 자료는 업데이트 뒤에도 저장한 기한·만료 상태를 유지한다.

## 설치부터 업무 시작까지의 실제 경계

플러그인 설치는 Skill과 로컬 MCP 시작 구성을 전달한다.
아직 런타임이 없는 PC에서는 MCP 시작이 실패할 수 있지만 setup Skill은 설치된 Markdown 지침으로 진입한다.
설치 실패는 로컬 실행 준비 상태로, 리소스·업무 실패는 해당 실행 결과로 안내한다.
같은 이름의 profile이 있으면 저장한 구성과 정책을 읽고 요청한 설정을 이어간다.
기존 설정 변경은 요청한 항목만 반영하고 다른 연결·정책·보존 선택을 유지한다.

setup의 리소스 선택은 [범주별 카탈로그 안내](../resources/initial-adapters.md#범주와-어댑터)를 따른다.
선택·확인은 아래의 정형 질문으로 받고 접속값은 YAML로 입력한다.
선택창의 모양과 항목 수는 호스트가 제공한다.

| 사용자 입력·상황 | 호스트·DAERI의 처리 | 사용자에게 보이는 결과 |
|---|---|---|
| 최초 setup 요청 | profile과 고정된 시작 확인 뒤 전용 환경 준비 | 세팅 시작·중단 선택과 실제 준비 상태 |
| 프로그램 준비 완료 | 실행기 검사 결과와 현재 호스트 연결을 구분해 확인 | 필요한 경우 호스트 재시작·MCP 재연결 안내 |
| profile 준비 요청 | 대상 서비스의 리소스·연결·인증 참조 구성 | 등록 정보와 리소스별 진단 결과 |
| profile 선택 요청 | 저장 목록·지정 profile을 확인하고 대화의 기본 대상 선택 | 이후 새 업무에 사용할 profile·식별자 |
| profile 삭제 요청 | 관리 CLI로 대상·범위를 미리 확인하고 사용자 확인 뒤 연관 자료 정리 | 완료 또는 사용 차단을 유지한 정리 대기 |
| profile 목록 요청 | 저장된 profile 목록을 이름순으로 요약 | profile·리소스 범주, 구별에 필요한 식별자 |
| 자연어 업무 요청 | 현재 제공 도구와 profile의 저장 정보를 읽고 접수 | 업무 식별자·필요한 추가 입력 |
| 승인 답변 | 현재 업무·저장된 계획·대기 승인 요청과 실제 사용자 답변 연결 | 승인·수정 요청·거절·미결 상태 |
| 재개 요청 | 최근 업무나 지정 업무의 저장 상태·계획·남은 작업 확인 | 필요한 새 승인과 이어갈 범위 |

공통 MCP 시작은 준비된 전용 Python으로 설치된 패키지만 실행한다.
필수 모델·패키지 누락 또는 버전 불일치는 setup 준비 안내로 처리한다.
실행기의 표준 출력은 MCP 전달에만 사용하며 시작 실패는 진단 출력으로 구분한다.

호스트는 실제로 열거된 도구와 입력 스키마를 기준으로 행동한다.
누락 도구·미지원 기능·검색 실패는 해당 제한과 가능한 복구 경로를 보고한다.

## setup의 정형 질문

setup은 **profile 확인 → 시작 확인 → 리소스·제품 선택 → YAML 편집 → 적용 확인** 순서다.
첫 대화 응답은 [DAERI 시작 로고](../../../skills/setup/SKILL.md#시작-로고)와 현재 단계의 질문·진행 안내로 구성한다.
profile 문자열은 대화 입력, 선택·확인은 질문 도구, 접속값은 [사용자용 YAML](../configuration/profiles.md#사용자용-setup-yaml)로 받는다.
각 단계의 사용자 안내는 현재 질문·선택지와 입력에 필요한 예시로 구성한다.
setup의 사용자 입력은 아래 단계 표로 구성하고 기존 설정의 보존·변경은 [YAML 적용 계약](../configuration/profiles.md#사용자용-setup-yaml)을 따른다.
초안이 있으면 그 선택·편집을 재사용하고 파일 작성 완료 응답은 바로 검증으로 이어간다.
첫 질문과 고정 실행 순서는 [setup Skill](../../../skills/setup/SKILL.md#세팅-순서)에서 직접 제공한다.

| 입력 조건·단계 | 사용자에게 표시할 내용 | 응답 후 처리 |
|---|---|---|
| profile 미지정 | 자유 문자열 입력 요청과 형식 예시 | 실제 입력을 기다리고 검증 후 시작 확인 |
| profile이 지정된 새 세팅 | 세팅 시작·중단 확인 | 시작이면 환경 준비, 중단이면 현재 설정을 보존하고 종료 |
| 리소스 선택 | “연결할 리소스 종류를 선택해 주세요.” | 선택한 종류의 제품 확인 |
| 선택한 범주에 제품이 여럿 | “사용할 제품을 선택해 주세요.” | 선택값을 YAML 생성에 사용 |
| 초안·기존 profile에 임베딩 설정 없음 | “기억 검색에 사용할 임베딩 방식을 선택해 주세요.” | 아래 선택값으로 YAML 구성 |
| YAML 생성 완료 | 생성한 파일 위치와 “파일을 작성한 뒤 알려 주세요.” | 작성 완료 응답을 받아 검증 |
| 파일 검증 성공 | 변경 요약과 “이 설정을 적용할까요?”; “적용”·“중단” | 적용이면 확인한 설정 저장, 중단이면 편집 초안을 보존하고 종료 |

입력 형식은 [profile 입력 규격](../configuration/profiles.md#구현한-profile-입력과-공개-설정)을 따른다.
형식 오류에는 수정할 조건과 유효한 예시를 안내하고 profile을 다시 입력받는다.

임베딩 선택의 표시와 설정값은 다음과 같다.
기억 저장·검색은 로컬에서 수행하고 선택한 방식은 검색용 벡터의 생성 방법을 결정한다.

| 선택지 | 설명 | `embedding.provider` |
|---|---|---|
| 로컬 임베딩 모델 | 이 PC에 모델을 준비해 벡터를 생성합니다. | `local` |
| 회사 임베딩 API | 회사 API에 허용한 텍스트를 보내 벡터를 생성합니다. | `openai_compatible` |
| 키워드 검색만 사용 | 임베딩 없이 키워드로 기억을 검색합니다. | `keyword_only` |

회사 API의 주소·모델·인증 참조·전송 허용은 [임베딩 설정](../configuration/profiles.md#임베딩-제공자-선택)을 따른다.

사용자에게 포트는 “리소스 종류”, 연결할 어댑터는 “제품”으로 표현한다.
`setup catalog`의 그룹은 다음 표시·선택 계약으로 사용한다.

| 필드 | 역할 |
|---|---|
| `groups[].label` | 종류 선택지의 표시명; 데이터베이스·캐시·인프라·소스 코드·문서 |
| `groups[].description` | 종류 선택지의 설명; “사용 가능한 제품: …”과 해당 그룹의 모든 제품명 |
| `groups[].type` | 사용자가 선택한 종류를 연결하는 내부 값 |
| `groups[].products[].display_name` | 복수 제품이 있는 종류에서 선택할 제품명 |
| `groups[].products[].recipe_id` | 제품 선택 뒤 YAML 생성에 사용하는 내부 값 |

선택지의 표시명·설명은 해당 필드의 문자열을 그대로 사용한다.
제품이 하나면 그 제품을 선택하고 여럿이면 실제 목록에서 선택받는다.
표시한 범주·제품을 코드의 식별값에 매핑해 선택한 연결의 YAML 필드를 생성한다.
입력할 접속 정보·공개 인증 설정은 YAML 계약, 업무의 세부 조회 대상은 [계획 승인](../tasks/approval.md)을 따른다.

카탈로그의 모든 범주는 같은 선택 단계와 복수 선택 형식을 사용한다.
질문 도구의 항목 한도 때문에 나눠야 하면 같은 복수 선택 형식의 페이지로 나눈다.
각 페이지는 순서만 표시하고 가능한 호스트에서는 같은 질문 호출에 함께 제공한다.
페이지는 실제 항목만으로 도구의 최소·최대 선택지 수를 만족하도록 균등하게 구성한다.
추가 범주·제품도 카탈로그 등록만으로 같은 규칙을 따른다.

단일 선택만 지원하는 도구에서는 각 범주에 같은 “사용”·“사용 안 함” 선택을 제공한다.
가능한 질문 수만큼 묶어 표시한다.
호출 인자는 현재 질문 도구 스키마에 선언된 필드로 구성한다.

시작 확인은 해당 profile 세팅에 필요한 전용 환경과 선택한 연결의 준비를 포함한다.
로컬 임베딩 모델 선택은 기기 모델 준비, 회사 API 선택은 인증·전송 허용·API 진단, 키워드 선택은 FTS 검색 준비로 이어간다.

Claude Code는 [AskUserQuestion](https://code.claude.com/docs/en/hooks#askuserquestion)을 사용한다.
Codex는 현재 제공되는 `request_user_input` 또는 같은 역할의 질문 도구를 사용한다.
질문은 실제 도구 스키마에 맞는 업무 선택지로 구성하고 기본 입력란·취소 기능은 호스트 UI를 사용한다.
값의 확정과 진행은 해당 질문에 대한 실제 사용자 응답을 조건으로 한다.
중단 응답은 기존 설정·편집 초안을 보존하고 현재 세팅을 종료한다.

질문 도구가 현재 모드에 없으면 필요한 모드를 안내한다.
Codex의 도구가 Plan 모드에서만 제공되면 `/plan`에서 선택·파일 읽기·스키마 조회까지만 수행한다.
profile 저장소를 여는 YAML 검증과 초안 생성·적용·설치·로그인·진단은 실행 가능한 모드로 돌아온 뒤 진행한다.
모드·권한 전환과 사용자 응답은 호스트가 제공하는 실제 사용자 경로로 받는다.
App Server의 [사용자 입력 규격](https://learn.chatgpt.com/docs/app-server#toolrequestuserinput)과 실제 CLI의 모드별 UI는 각각의 지원 범위로 확인한다.

파일 검증 오류는 YAML 필드 위치·수정 안내로 전달하고 같은 파일의 수정·재검증으로 이어간다.
비밀값은 [보안 입력 경로](../configuration/profiles.md#인증-저장과-전달)에서 받고 YAML에는 인증 참조를 저장한다.
업무 실행은 세팅 완료 뒤 해당 업무 계획의 승인을 받는다.
실제 호스트 질문 화면·모드 전환은 미검증이다.

## Skill 지침

네 사용자 진입점은 아래 역할로 나누며 공통 실행기와 사용자 저장소를 사용한다.
Skill 본문은 첫 입력·관리 호출·결과 분기를 바로 수행할 지침을 제공한다.
추가 계약은 해당 단계에서 필요한 절을 호스트의 파일 읽기 도구로 확인한다.
Claude Code의 `Read`처럼 파일 내용을 직접 읽는 도구가 있으면 그 도구를 사용한다.
관리 호출은 [고정 스크립트](../../development/dependencies.md#관리-입력과-결과)가 실행 조건을 판정한다.
대화의 표시 범위는 [사용자 안내](#사용자-안내)를 따른다.
개발 에이전트는 저장소 AGENTS의 읽기 경로를 적용한다.

| Skill | Markdown 경로 | 수행 범위 |
|---|---|---|
| setup | `skills/setup/SKILL.md` | 환경·리소스 설정, 준비 결과와 대상 profile 안내 |
| profile | `skills/profile/SKILL.md` | 현재 대화의 기본 profile 선택·전환, 확인한 대상의 연관 자료와 profile 삭제 |
| work | `skills/work/SKILL.md` | 선택한 대상의 업무 접수·계획·승인·실행·보고 |
| list-profiles | `skills/list-profiles/SKILL.md` | 저장한 profile의 목록 요약 |

setup은 사용자가 환경·연결 관리를 요청했을 때 시작한다.
work의 환경 준비 필요 상태는 setup 안내로 연결한다.
설정 완료 시 profile·식별자와 실제 확인한 준비 상태를 안내하고 해당 대화에서 선택한 맥락으로 사용한다.
profile 선택·list-profiles는 관리 CLI의 `profile list`·`profile show`를 사용하고 선택은 현재 대화의 맥락에 둔다.
선택 우선순위·재선택·목록의 범위는 [profile 선택 계약](../configuration/profiles.md#선택과-기본값)을 따른다.
profile 삭제도 같은 Skill을 사용한다.
호스트는 `profile delete NAME`의 미리보기를 요약해 사용자에게 확인받고 `--confirm TOKEN`으로 실행한다.
사용 차단·연관 자료 정리·재시도·최종 삭제는 [실행기의 삭제 계약](../configuration/profiles.md#profile-삭제)이 담당한다.
삭제의 기술 절차는 실행기가 수행하고 profile Skill은 대상·범위 확인과 관리 명령 연결을 담당한다.

work는 관련 자연어 요청과 사용자의 명시 호출에 적용한다.
[work Skill](../../../skills/work/SKILL.md)은 아래 정본의 동작을 현재 MCP의 도구·입력에 연결한다.

| 계약 | 정본과 적용 범위 |
|---|---|
| 접수·실행·복구·보고 | [업무 흐름](../tasks/workflow.md); 승인 범위 안의 자율적인 도구·방법·순서 선택과 완료 근거 |
| 계획·재계획 | [계획과 승인](../tasks/approval.md); 수행 방법·대상·자료·예산과 범위 변경 |
| 사용자 결정 | [대화 승인](approval.md); 실제 사용자 응답 해석과 현재 계획 연결 |
| 기억 조회·갱신 | [기억 명세](../tasks/memory.md); 계획 전 검색, 결과별 제한과 업무 후 선별 지식 갱신 |
| 도구 입력·출력 | [MCP 규격](../mcp/common.md); 공개 DTO·상태·오류·멱등 계약 |

## 재개·동시 사용·환경 고정

새 대화·문맥 압축·호스트 전환 뒤에는 Skill과 관련 계약을 다시 읽고 저장한 업무·계획·실행 이력을 확인한다.
새 업무의 대상 선택 증거가 불확실하면 [profile 선택 계약](../configuration/profiles.md#선택과-기본값)에 따라 정형 질문으로 다시 고른다.
업무를 특정할 수 없으면 해당 profile의 최근 업무 목록을 보여 주고 사용자가 선택하게 한다.
새 접수와 기존 업무 재개는 사용자의 의도와 저장 기록으로 구분한다.
정확한 업무·승인 상태는 저장 기록에서 복구하고 유사 기억은 수행 방법 판단에 참고한다.

한 업무의 실행 주체는 동시에 하나만 유지한다.
일시중지와 취소를 구분하고 일시중지한 업무는 상태·남은 작업·계획을 보여 준 뒤 다시 승인받는다.
모호한 응답·미완료 승인 요청은 호스트 전환 뒤에도 대기 상태를 유지한다.

재연결·재계획 뒤에도 기존 사용량·진행 내역·미확인 효과를 승계한다.
업데이트·기본 profile 변경 뒤에도 진행 중인 업무의 대상·스냅샷·계약 참조를 보존한다.
프로그램 전환 뒤에는 [현재 실행 환경](../installation/environment.md#업데이트와-데이터-보존)과 저장한 업무의 재개 조건을 대조한다.
현재 환경의 재개 조건 검증에 실패하면 필요한 준비를 안내하고 [재개 조건](../tasks/workflow.md)을 다시 확인한다.

## 로컬 실행과 연결 수명

플러그인 설치 뒤 setup은 개발 도구가 없는 PC에서도 시작할 수 있도록 준비한다.
일반 업무 시작은 준비된 실행기를 연결하며 필요한 환경이 없으면 setup 안내를 반환한다.
사용자 저장소와 프로그램 관리 원칙은 [환경 준비](../installation/environment.md)를 따른다.

실행기는 해당 업무의 profile에서 필요한 연결만 사용한다.
원격 접속·자료 조회는 승인된 업무에서 필요하다고 판단한 연결을 대상으로 수행한다.
프로그램 준비·리소스 인증·업무 자료 조회는 각각 요청된 범위에서 수행한다.

호스트 연결이 끊기면 새 호출을 멈추고 보존한 계획·결정·실행 기록을 유지한다.
결과가 불명확한 호출은 미확인 상태로 보존하고 재개 시 외부 결과를 확인한다.
초기 실행 수명은 현재 PC의 연결된 호스트 세션이다.
기억 선별·갱신은 현재 호스트 작업 중 수행하고 다음 세션은 저장된 갱신 상태에서 이어간다.

## 지원 경계와 검증

사용자 PC에서 동작하는 Codex·Claude Code 플러그인을 초기 대상으로 한다.
다른 실행 환경의 지원은 해당 환경의 로컬 프로그램·저장소·인증 접근을 별도로 검증해야 한다.

두 호스트 각각에서 마켓플레이스 추가·플러그인 설치·setup·자연어 요청·계획 승인·실행·결과 보고를 확인한다.
이어 문맥 복구·호스트 전환·일시중지·재연결·동시 실행과 환경 미준비 안내를 검증한다.
계획 전 기억 조회·기억 없는 첫 업무·검색 실패 안내·업무 후 갱신·갱신 재시도도 확인한다.
진입 파일·로컬 실행기의 구현·검증 범위는 [최초 구현 계획](../../plans/initial-implementation.md#현재-구현-단위와-검증-경계)을 따른다.
실제 마켓플레이스 등록·설치·Skill 진입·호스트 대화 승인은 미검증이다.
