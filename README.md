# DAERI

서버 운영 업무를 대리 수행하는 멀티 에이전트 플러그인입니다.
Claude Code 또는 Codex에서 자연어로 요청하면 총괄 에이전트가 필요한 작업자와 협업해 연결된 리소스를 조회·분석하고 결과를 보고합니다.

서비스별 **profile**에 연결 정보·권한·업무 이력·재사용할 기억을 관리합니다.
실행기와 저장소는 사용자 PC에서 동작합니다.
업무 판단과 답변은 선택한 호스트의 모델이 담당합니다.

현재는 **조회·분석을 위한 초기 버전**이며 운영 리소스 변경·배포는 후속 확장 범위입니다.

## 동작 방식

![기억 조회·계획 승인 후 총괄과 작업자가 협업하고, 공통 실행기가 리소스 호출과 자동 기억을 처리하는 DAERI 동작 흐름](docs/assets/daeri-flow.svg)

사용자는 호스트 대화에서 계획을 확인하고 “승인할게”처럼 답합니다.
승인 범위 안에서는 자율적으로 진행하며 범위 확대가 필요하면 수정 계획을 다시 승인받습니다.

총괄이 업무 배정·진행 확인·결과 통합을 맡고 작업자는 배정된 목표 안에서 방법과 다음 행동을 판단합니다.
작업자 실행은 호스트의 네이티브 기능을 사용하며 해당 기능을 사용할 수 없는 환경에서는 총괄이 직접 이어갑니다.

호스트가 MCP의 조회 결과로 분석·보고·파일 작성을 수행하고 DAERI 실행기는 승인·근거·파일 등록을 관리합니다.
보고에 연결한 재사용 지식은 자동 저장·색인합니다.
구성 요소와 통신 경로는 [멀티 에이전트 아키텍처](docs/architecture/overview.md)에서 확인할 수 있습니다.

## 설치

플러그인 기능을 지원하는 **Codex CLI 또는 Claude Code가 설치·로그인된 환경**에서 시작합니다.
저장소 접근과 필요한 패키지 다운로드를 위한 네트워크가 필요합니다.
기억 검색용 임베딩은 로컬 임베딩 모델·회사 임베딩 API 중에서 선택하며 키워드 전용도 지원합니다.

실행 중인 호스트는 종료하고 아래 명령을 터미널 또는 PowerShell에서 순서대로 실행합니다.
각 설치 명령의 성공을 확인한 뒤 마지막 명령으로 호스트를 시작합니다.
macOS는 터미널, Windows는 PowerShell에서 실행합니다.

### Codex

터미널 또는 PowerShell에서 플러그인을 설치한 뒤 Codex를 시작합니다.

```shell
codex plugin marketplace add https://github.com/VVoodyHong/DAERI.git --ref main
codex plugin add daeri@daeri-marketplace
codex
```

Codex 대화창에서 setup을 실행합니다.

```text
$daeri:setup service-prod
```

`/skills` 또는 `$` 선택기에서도 **DAERI의 setup**을 찾을 수 있습니다.
선택·확인 도구에 Plan 모드가 필요하면 `/plan`으로 전환합니다.
설치·설정 저장·로그인은 실행 가능한 모드로 돌아온 뒤 진행합니다.
설치 명령은 [OpenAI 공식 안내](https://learn.chatgpt.com/docs/developer-commands#codex-plugin-marketplace)를 참고하세요.

### Claude Code

터미널 또는 PowerShell에서 플러그인을 설치한 뒤 Claude Code를 시작합니다.

```shell
claude plugin marketplace add 'https://github.com/VVoodyHong/DAERI.git#main'
claude plugin install daeri@daeri-marketplace --scope user
claude
```

Claude Code 대화창에서 setup을 실행합니다.

```text
/daeri:setup service-prod
```

설치 명령은 [Claude Code 공식 안내](https://code.claude.com/docs/en/discover-plugins#manage-plugins-from-your-shell)를 참고하세요.
사내 저장소로 배포했다면 위 명령의 URL을 해당 Git 저장소 주소로 바꿉니다.
비공개 저장소의 Git 인증은 먼저 준비해야 합니다.

## 최초 설정과 첫 업무

`service-prod`는 예시 profile이며 서비스·환경별로 여러 개를 만들 수 있습니다.
접속 정보와 비밀번호·토큰은 선택한 연결의 YAML 파일에 입력합니다.
데이터베이스·캐시·인프라·소스 코드·문서·이슈를 연결하고 같은 종류를 여러 개 등록할 수 있습니다.

1. “DAERI 세팅을 시작할까요?”에서 **세팅 시작**을 선택합니다.
2. 리소스 종류와 제품을 선택하고 제품별 연결 개수와 임베딩 방식을 확인합니다.
3. 생성한 `config/profiles/<profile>.yaml`에 접속 정보와 비밀번호·토큰을 입력합니다.
4. 호스트에 작성을 마쳤다고 알리고 검증한 변경 요약을 확인해 **적용**합니다.
5. 필요한 제공자 로그인을 마친 뒤 리소스·기억 검색·MCP 연결의 실제 상태를 확인합니다.
6. 준비한 profile에서 work 또는 자연어로 업무를 요청하고 계획을 승인합니다.

setup은 DAERI 전용 실행 환경을 준비하고 기존 개발 환경을 유지합니다.
설정 파일은 [DAERI 사용자 저장소](docs/development/dependencies.md#저장-위치와-데이터-역할)에 두며 이후 연결·임베딩 설정도 같은 YAML에서 수정하고 검증·적용합니다.
적용한 비밀번호·토큰은 OS 인증 저장소에 저장하고 같은 YAML의 입력값은 인증 참조로 바꿉니다.

연결 실패 때에는 해당 항목만 같은 파일에서 수정해 다시 검증·적용합니다.
회사 API는 설정 적용 때 허용 기억·검색 질의가 해당 주소로 전송된다는 요약을 확인합니다.
로컬 임베딩 모델을 선택하면 해당 모델을 PC에 준비합니다.

첫 설치에서는 setup을 먼저 실행합니다.
전용 환경 준비가 끝나면 호스트가 DAERI 연결 구성을 다시 읽도록 합니다.
Claude Code는 `/reload-plugins` 또는 새 세션을 사용하고, Codex는 클라이언트에서 지원하는 새로고침을 사용하거나 재시작합니다.
그 뒤 실제 MCP 연결과 도구 목록을 확인하고 첫 업무를 진행합니다.

| 할 일 | Claude Code | Codex |
|---|---|---|
| 환경·profile 설정 | `/daeri:setup service-prod` | `$daeri:setup service-prod` |
| 이 대화의 profile 전환 | `/daeri:profile service-prod` | `$daeri:profile service-prod` |
| profile과 관련 로컬 자료 삭제 | `/daeri:profile delete service-prod` | `$daeri:profile delete service-prod` |
| 업무 지시 | `/daeri:work 요청 내용` | `$daeri:work 요청 내용` |
| profile 목록 | `/daeri:list-profiles` | `$daeri:list-profiles` |

삭제는 대상·범위를 확인한 뒤 진행합니다.
해당 profile의 업무·기억·관리 파일·설정을 정리하며 공유 설치 자산·공유 인증·원격 리소스는 유지합니다.
자세한 범위와 실패 시 재시도는 [profile 삭제](docs/spec/configuration/profiles.md#profile-삭제)를 참고하세요.

PC 이동이나 공유는 profile 관리에서 백업·복원을 요청할 수 있습니다.
설정·업무·허용 기억·등록 파일을 암호화 묶음으로 옮기며, 새 환경에서는 인증과 실행 승인을 다시 확인합니다.
지원 범위와 암호 입력·복원 절차는 [profile 이동](docs/spec/configuration/profiles.md#이관백업복원)을 참고하세요.

가상 업무 요청 예시입니다.
실제로 등록된 기능과 권한 안에서 계획을 세웁니다.

```text
현재 profile의 소스와 운영 문서를 비교해서 설정 차이를 정리해줘.
어떤 리소스를 읽고 어떤 결과를 만들지 먼저 계획을 보여줘.
```

## 지원 범위와 추가 안내

macOS의 로컬 실행·자동 준비·모의 리소스 통합을 검증했습니다.
실제 호스트 설치·회사 리소스 연결과 Windows 실행은 해당 환경에서 추가 검증이 필요합니다.

- [지원 제품과 연결 범위](docs/spec/resources/initial-adapters.md#어댑터별-실제-지원-경계)
- [플러그인 업데이트](docs/spec/hosts/plugins.md#설치한-플러그인-업데이트)
- [환경 준비와 문제 해결](docs/spec/installation/environment.md)
- [상세 문서 지도](docs/development/documentation-architecture.md)
