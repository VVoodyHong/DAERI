# 개발 의존성과 로컬 실행

상태: 첫 버전의 준비·실행·구현 책임과 로컬 검증 정리; 실제 회사·호스트·Windows 검증은 별도

어떤 프로그램이 설치되고 구성 요소가 어떤 자료를 주고받는지 설명한다.
업무·설정·보존 계약은 [제품 명세 지도](documentation-architecture.md#정본-지도), 제공 범위와 실제 확인 경계는 [최초 구현 계획](../plans/initial-implementation.md#현재-구현-단위와-검증-경계)이 정본이다.

## 의존성의 정본

| 정본 | 관리하는 내용 |
|---|---|
| [pyproject.toml](../../pyproject.toml)·[uv.lock](../../uv.lock) | runtime Python 지원 범위·직접 의존성·잠긴 전체 조합·개발 의존성·실행 명령 |
| [준비 도구 정책](../../scripts/dependencies.env) | 최초 준비 도구의 고정 버전·공식 출처·SHA256 |
| [연결 카탈로그](../../src/daeri/config/catalog.json) | 첫 리소스 recipe의 제품·패키지·실행 이름·고정 안전 설정 |
| `infrastructure/adapters/`의 setup 계약 | 어댑터별 공개·인증 입력 스키마, 저장 표현·목적지와 선택한 인증 방식의 최종 구성 |
| `infrastructure/embedding.py`의 `API_ADAPTERS` | 구현한 임베딩 API 연결 방식·표시·생성 필드·검증·호출 |
| `recipes/<recipe-id>/pyproject.toml`·`uv.lock` | 각 하위 MCP의 별도 Python 범위·정확한 패키지 조합·명시적 호환 조건 |
| [기억 명세](../spec/tasks/memory.md) | 선택형 임베딩·검색·모델 호환·전송·보존 기준 |

버전·출처·잠금 조합은 위 정본에서 관리하고 문서·호스트·Skill은 이를 참조한다.
카탈로그의 패키지와 recipe의 직접 의존성이 다르면 준비를 거부한다.
각 recipe의 격리 환경에서 해당 SDK 주 버전을 설치하고 앱은 자체 잠금 조합을 유지한다.

| 구성 | 사용하는 이유 |
|---|---|
| Python·공식 MCP SDK | 사용자 PC의 CLI·MCP 서버와 하위 MCP 클라이언트 실행 |
| Pydantic·JSON Schema 검사 | 공개 DTO와 등록 기능의 타입·필드·계약 확인 |
| [PyYAML](https://pyyaml.org/wiki/PyYAMLDocumentation) | 안전 로더 기반 YAML 생성·읽기; 중복 키·태그·별칭 등은 별도 검증으로 거부 |
| SQLite·FTS5·벡터 확장 | 업무·기억 정본과 로컬 키워드·벡터 검색 |
| 선택 임베딩 어댑터 | 준비한 로컬 모델 또는 허용된 회사 API로 지식·질의를 벡터화; 키워드 전용도 선택 가능 |
| OS 인증 저장소 연결 | 모델·profile JSON 대신 사용자 비밀 참조 해소 |
| [cryptography](https://cryptography.io/en/latest/hazmat/primitives/symmetric-encryption/) | profile 이동 파일의 인증 암호화와 비밀번호 기반 키 생성 |
| uv·Hatchling·pytest | 잠긴 환경 준비·패키지 빌드·독립적인 동작 검증 |

## 개발 환경과 검사

개발자는 uv가 있는 저장소 루트에서 다음 명령으로 환경과 테스트 의존성을 준비한다.
`uv sync`는 지원 Python과 잠긴 패키지를 다운로드할 수 있다.

```sh
uv python install 3.12
uv sync --frozen
uv run --no-sync python -m pytest
```

일반 테스트는 사내 인증·운영 리소스·호스트 전역 설정 없이 로컬 계약을 확인한다.
기본 개발 환경과 API·키워드 전용 사용자 환경은 공통 의존성으로 동작한다.
실제 로컬 모델 추론을 검사할 때는 `uv sync --frozen --extra local-embedding`으로 선택 의존성을 추가한다.
recipe의 잠금 검사에는 해당 Python 범위에 맞는 3.12 실행 파일을 사용한다.

준비한 실제 모델을 쓰는 검사는 `DAERI_TEST_MODEL_CACHE`가 지정된 경우만 수행한다.
이 검사는 검색 중 DNS를 차단해 모델 로딩·임베딩·한국어 질의와 영어 지식의 검색이 로컬에서 이루어지는지 확인한다.

플러그인 사용자는 명시적인 setup으로 실행 환경을 준비한다.
macOS는 `sh scripts/setup.sh --prepare`, Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/setup.ps1 -Prepare`를 사용한다.
기본 `--check`·`-Check`는 기존 실행 환경을 읽고 로컬 검사 결과를 반환한다.

준비·환경 격리·실패 복구는 [자동 준비 사양](../spec/installation/environment.md#구현한-자동-준비의-입출력과-경계)을 따른다.

`check`는 설치된 실행 패키지의 `version`과 Python 지원 범위·MCP·SQLite·실제 FTS5 검색 결과를 반환한다.
`ready`는 기본 실행 환경의 검사 결과다.
리소스 인증·진단·기억 모델은 해당 관리 경로의 결과로 확인한다.
저장소 개설 전의 runtime 검사와 저장된 profile의 연결 진단은 별도 결과로 제공한다.

### 공통 자동 검사

개발 환경을 준비한 뒤 저장소 루트에서 [공통 검사 명령](../../scripts/check.py)을 실행한다.
이 명령은 아래 검사를 차례로 수행하고 실패가 있어도 나머지 독립 검사의 결과를 기록한다.

```sh
uv run --no-sync python -m scripts.check
```

| 검사 | 성공 조건 |
|---|---|
| 버전·카탈로그 | 프로젝트·루트 잠금·두 호스트 플러그인·Claude marketplace의 버전과 recipe의 고정 직접 의존성이 정본과 일치 |
| 잠금 | 루트와 모든 recipe의 `uv lock --check` 통과; 검사 시 Python은 준비된 실행 파일에서 선택 |
| 문서 | 루트 안내·`docs`·`skills`·`agents`의 내부 파일·이미지·Markdown 제목 앵커 연결이 유효 |
| 회귀·공개 계약 | 전체 pytest 통과; 조건부 생략·실패 근거는 JUnit XML에 기록 |
| 회상 평가 | 고정 합성 자료의 키워드 평가 완료·접근 불가 결과 0건; 회수율·순위는 평가 보고서로 보존 |
| 배포물 | 빈 임시 디렉터리에서 sdist·wheel 생성, 필수 파일·원본 바이트 일치와 설치별 구성·runtime·캐시의 혼입 검사 통과 |

결과는 기본 `build/checks/`에 검사별 로그·`summary.json`·`summary.md`·`pytest.xml`·`memory-recall.json`·검증한 `dist/`로 기록한다.
`--output-dir PATH`로 결과 디렉터리를 지정할 수 있다.
결과 디렉터리는 실행마다 이전 검사 자료를 정리하며 관련 없는 파일이 있으면 해당 자료를 보존하고 다른 경로를 요청한다.
전체 통과는 종료 코드 0, 검사 실패는 1, 명령 인수 파싱 실패는 2다.
검사 환경은 사용자 `DAERI_DATA_DIR`와 실제 모델 경로의 상속을 해제하고 시험 저장소·수치 벡터·모의 API로 구성한다.

[문서 검사](../../scripts/check_docs.py)는 현재 문서에서 쓰는 인라인 링크·이미지와 ATX 제목 앵커를 검사한다.
외부 URL의 접속 상태, 참조형 링크와 Setext 제목의 추가는 해당 형식 도입 시 검사 계약에 반영한다.
배포 검사·로그 보존의 회귀는 [저장소 검사 테스트](../../tests/test_repository_checks.py), 링크 경계는 [문서 링크 테스트](../../tests/test_documentation_links.py)로 확인한다.

### GitHub 실행과 별도 확인

[GitHub workflow](../../.github/workflows/check.yml)는 `main` push·PR·수동 실행에서 Ubuntu의 Python 3.12·3.14로 같은 공통 명령을 수행한다.
uv 버전은 [준비 도구 정책](../../scripts/dependencies.env)을 읽고 잠긴 개발 환경을 준비한다.
각 job은 실패 상태를 전달하고 성공·실패의 요약·로그·평가·검증한 배포물을 GitHub 결과에 보관한다.

수동 실행의 선택 항목은 기본값이 꺼져 있으며 다음 검사를 추가한다.

| 선택 항목 | 실행 범위 |
|---|---|
| `native` | macOS의 공통 검사와 Windows의 native bootstrap 회귀 12개 |
| `local_model` | 공개 로컬 모델 다운로드·준비 → DNS 차단 실제 추론 회귀 → 같은 합성 자료의 의미 회상 평가 |
| `clean_bootstrap` | macOS·Windows의 빈 플러그인 실행 환경·별도 사용자 경로에서 실제 준비 → MCP 구성 발행·runtime 검사 |

GitHub runner에 기본 설치된 OS 도구와 캐시 없는 회사 PC의 전체 설치 경험은 각각의 근거로 확인한다.
회사 API·운영 리소스·SSO·호스트 UI·재연결의 검증은 해당 환경에서 진행한다.
이 workflow는 읽기 권한·고정 액션 참조를 사용하고 외부 배포·병합은 별도 Git 작업의 범위다.
실행 설정은 [uv의 공식 CI 안내](https://docs.astral.sh/uv/guides/integration/github/)와 [GitHub workflow 규격](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)을 기준으로 구성했다.
현재 확인한 로컬·호스팅 실행 경계는 [구현 계획](../plans/initial-implementation.md#확보한-검증-근거)을 따른다.

### 기억 회상 평가

개발 저장소 루트에서 고정 합성 기억·질의를 임시 저장소로 평가한다.
평가용 리소스 연결은 시험 메타데이터이며 실제 MCP를 기동하는 작업은 없다.
기본 명령은 키워드 검색을 실행하고 JSON을 표준 출력과 지정한 결과 파일에 기록한다.

```sh
uv run --no-sync python -m evaluations.memory_recall --output /tmp/daeri-recall.json
```

로컬 평가는 기존에 준비한 모델 경로를 명시해 해당 파일을 읽는다.
모델이 준비되지 않은 경로는 `unavailable`과 종료 코드 1로 반환한다.

```sh
uv run --no-sync python -m evaluations.memory_recall --provider local --model-cache "$DAERI_TEST_MODEL_CACHE"
```

회사 API 평가는 명시한 YAML의 `embedding` 절 또는 같은 필드로 구성한 최상위 매핑을 읽는다.
필드는 [임베딩 API 계약](../spec/tasks/memory.md#임베딩-api-계약)의 `provider: api`, `adapter: openai_compatible`, 요청 주소·모델·인증 참조를 사용한다.
이 명령은 합성 기억 본문·질의를 선택한 API로 전송하고 인증값은 기존 인증 저장소·환경 참조에서 읽는다.

```sh
uv run --no-sync python -m evaluations.memory_recall --provider api --embedding-config /path/to/embedding.yaml
```

실제 사용자 profile·업무·기억 저장소는 평가의 입력·갱신 대상과 분리된다.
보고서에는 선택한 모델 지문과 합성 자료·검색 결과를 기록하고 연결 주소·인증 참조·비밀값은 내부 연결 입력으로 처리한다.
평가 지표·상태·종료 코드와 한계는 [회상 평가 사양](../spec/tasks/memory.md#반복-가능한-회상-평가)을 따른다.

## 관리 입력과 결과

관리 호출은 macOS의 `sh "<플러그인 루트>/scripts/manage.sh"`, Windows의 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File "<플러그인 루트>\scripts\manage.ps1"`에 아래 인수를 전달한다.
실행 프로그램·스크립트 경로·각 인수는 별도 값으로 구성한다.
경로를 재사용할 때 변수에는 스크립트 경로를 보관하고 macOS의 sh·zsh에서 다음과 같이 호출한다.

```sh
DAERI_MANAGE="<플러그인 루트>/scripts/manage.sh"
sh "$DAERI_MANAGE" resource prepare PROFILE
```

스크립트가 전용 환경을 검사하고 준비된 `.venv/bin/python.exe`에 `-I -m daeri.cli`와 분리된 관리 인수를 전달한다.
실행 위치는 플러그인 루트이며 부모의 Python·가상환경 관련 변수는 자식 실행에서 정리한다.

정상 미준비는 `setup_required`·종료 코드 0, 손상·중단 상태는 `RUNTIME_UNAVAILABLE`·종료 코드 1로 구분한다.
판정 대상·환경 검사·설치·복구는 [최초 진입 계약](../spec/installation/environment.md#아무-개발-도구가-없는-최초-진입)을 따른다.
준비된 runtime의 CLI 결과·오류·종료 코드는 그대로 전달한다.

Skill은 `setup_required` 메시지와 현재 호스트의 setup 호출을 안내하고 해당 요청을 종료한다.
실제 실패는 사용자에게 필요한 영향·다음 조치로 요약하고 기술 정보는 [상세 진단 조건](../spec/hosts/plugins.md#사용자-안내)에 따라 제공한다.
공통 진입·자식 환경·공개 인수는 [환경 격리](../spec/installation/environment.md#공통-실행-파일과-환경-격리), 비밀 입력은 [인증 경로](../spec/configuration/profiles.md#인증-저장과-전달)를 따른다.
관리 명령의 기능과 상세 정본은 다음과 같다.
필수 인수·현재 옵션은 관리 CLI의 `--help`와 해당 정본에서 확인한다.

| 명령 | 기능과 상세 계약 |
|---|---|
| `setup catalog/status/draft/validate/apply` | [YAML 생성·검증·인증 분리·확인 후 적용](../spec/configuration/profiles.md#관리-명령과-재시도) |
| `profile create/list/show/import/export` | [profile 생성·조회·개정·공개 설정 이관](../spec/configuration/profiles.md#구현한-profile-입력과-공개-설정) |
| `profile delete` | [삭제 미리보기·확인·사용 차단·연관 자료 정리·최종 삭제](../spec/configuration/profiles.md#profile-삭제) |
| `profile backup/restore` | [단일 profile의 암호화 이동·검증·확인 후 새 공간 복원](../spec/configuration/profiles.md#이관백업복원) |
| `resource catalog/template/setup-schema/validate-setup/configure` | [제품·입력 스키마·접속 정보 검증·개정 검사·명시적 기능 계약 갱신](../spec/configuration/profiles.md#리소스-설정-입력과-검증) |
| `resource prepare/diagnose` | [잠긴 하위 MCP 준비·대상 확인·읽기 기능 등록](../spec/configuration/profiles.md#준비-카탈로그와-개별-프로그램-설치) |
| `resource login`·`auth set/status/delete` | [제공자 로그인·인증 참조 입력·상태·삭제](../spec/configuration/profiles.md#인증-저장과-전달) |
| `memory configure/status/diagnose/prepare/reindex` | [임베딩 선택·준비·시험 진단·재색인](../spec/installation/environment.md#임베딩-선택과-준비) |
| `cleanup` | [기한이 지난 본문·기억·색인과 작업 파일 정리](../spec/configuration/profiles.md#삭제와-만료) |

일반 관리 결과는 JSON으로 반환한다.
CLI 오류는 `status: error`·`data: null`과 `error`의 `code`·`stage`·`message`·`next_action`을 반환한다.
메시지는 등록된 명령·인수 이름과 오류 코드별 고정 설명으로 구성한다.
호스트는 실패 단계와 다음 조치로 영향·복구 방법을 안내한다.

| 실패 | `stage`와 다음 조치 | 종료 코드 |
|---|---|---|
| 명령 인수 누락·형식 오류 | `arguments`·`fix_input`; 누락한 인수 이름과 해당 명령의 `usage` 제공 | 2 |
| 설정·선택 불일치 | `configuration`·`fix_input`; 대상 부재·개정 충돌은 `refresh` | 1 |
| 프로그램 준비 미완료 | `preparation`·`inspect_preparation` | 1 |
| 인증·연결 확인 필요 | `authentication`·`authenticate` 또는 `connection`·`inspect_connection` | 1 |
| 그 밖의 실행 실패 | `operation`·`stop` | 1 |

setup은 [저장한 profile의 연결](../spec/configuration/profiles.md#준비-카탈로그와-개별-프로그램-설치)을 `resource prepare PROFILE` 한 번으로 준비한다.
일괄 결과는 전체 `ready`와 연결별 `resources`를 반환하며 일부 준비 실패의 종료 코드는 1이다.

`setup validate`·`resource validate-setup`의 입력 오류는 각 검증 결과의 필드별 `issues`와 종료 코드 1로 구분한다.
`auth set`과 백업 암호 입력에서 사용자 입력 경로가 필요하면 `input_required`와 종료 코드 0으로 안내한다.
값 파일·기본값·확인값·동시 변경·재시도는 표의 해당 명령 정본에서 정의한다.
호스트 모드별 질문·파일 읽기·실행 조건은 [setup 질문 계약](../spec/hosts/plugins.md#setup의-정형-질문)을 따른다.

제공자 로그인만 공개 확인 URL·사용자 코드를 `pending` JSON으로 먼저 전달하고 완료 또는 실패를 반환한다.
모델 준비·로그인·연결 진단의 결과를 확인한 뒤 실제 업무 조회는 해당 계획의 승인을 받아 수행한다.

## 실행 진입점과 모듈

`daeri`는 관리 CLI, `daeri-mcp`는 호스트가 연결할 MCP 서버다.
명시적 준비가 전용 환경을 활성화한 뒤 `scripts/register_mcp.py`로 설치 사본의 루트 `.mcp.json`을 발행한다.
두 호스트는 해당 파일을 기본 탐색해 전용 `.venv/bin/python.exe`의 절대 경로와 `-I -m daeri.server`를 사용한다.
준비·발행·실패 복구는 [환경 준비](../spec/installation/environment.md#구현한-자동-준비의-입출력과-경계), 새 구성 읽기·실제 연결은 [호스트 계약](../spec/hosts/plugins.md#설치부터-업무-시작까지의-실제-경계)을 따른다.
runtime 선택은 설치한 플러그인의 전용 Python 절대 경로로 고정한다.
일반 MCP는 준비된 runtime을 시작하고 표준 출력은 MCP 전달에 사용한다.
준비·로그인 요청은 관리 진입으로 연결한다.

호스트가 계획·사용자 응답·호출·보고를 전달하면 경계에서 입력을 검사하고 runtime이 저장 상태·정책과 대조한다.
실제 리소스 요청은 등록부·어댑터를 통과하고 허용 자료만 보고·기억에 연결한다.
총괄 에이전트·하위 에이전트의 판단·조정과 실행 검사의 경계는 [멀티 오케스트레이션 에이전트 아키텍처](../architecture/overview.md#오케스트레이션과-자율-수행)가 설명한다.
구현은 같은 로컬 배포 안에서 역할별 패키지로 나눈다.
공개 실행 모듈은 `daeri.cli`와 `daeri.server`다.

```text
src/daeri/
├── cli.py
├── server.py
├── core/
├── application/
├── infrastructure/
│   └── adapters/
└── config/
```

| 위치 | 담당 구현 | 책임 |
|---|---|---|
| 루트 진입점 | `cli.py`·`server.py` | 관리 인수·[공개 MCP 도구](../spec/mcp/common.md#초기-도구-목록)를 받아 설정·업무 처리로 전달 |
| `core/` | `contracts.py`·`policy.py`·`errors.py`·`security.py`·`serialization.py` | 입력 계약, 필드·기억·보존 정책, 공통 오류, 인증 문자열 보조 검사와 정규 JSON 표현 |
| `application/` | `runtime.py`·`planning.py`·`subagents.py`·`results.py`·`knowledge.py`·`automatic_memory.py` | 업무·계획·승인·위임·읽기 실행, 보고·파일의 근거 연결, 기억 후보 검증·갱신·재시도 |
| `infrastructure/` | `storage.py`·`resources.py`·`memory.py`·`embedding.py`·`credentials.py`·`provider_login.py` | SQLite·불변 파일·기한·정리, 하위 MCP와 인증, 기억 검색·색인과 임베딩 제공자 연결 |
| `infrastructure/adapters/` | 공통 `base.py`와 제품별 모듈 | 제품별 설정 스키마·대상·인증·로그인·읽기 의미·요청과 결과 검사 |
| `config/` | `setup.py`·`onboarding.py`·`setup_yaml.py`·`catalog.json`·`profile_*.py`·`portable_archive.py` | 접속·인증 YAML과 recipe 준비·진단, profile 개정·삭제·암호화 백업·검증·복원 |

`core`는 다른 DAERI 패키지를 가져오지 않으며 정책과 공통 자료 처리를 소유한다.
`application`은 `core`를 사용하고 `Runtime`에서 `infrastructure`의 저장소·인증·리소스 등록부·기억 구현을 직접 구성한다.
업무의 일부 저장 조회는 SQLite 연결을 직접 사용하며 `config`와 `infrastructure`도 profile 검증·참조 추적·정리에서 협력한다.
현재 의존 경계는 코어 정책의 독립성과 실행·외부 연결·설정의 책임 구분에 있다.

`scripts/`와 호스트별 플러그인은 환경 격리·최초 준비·검사·MCP 구성 발행을 담당한다.
`skills/`와 `agents/`는 사용자 진입과 총괄 에이전트·하위 에이전트의 업무 지침을 전달한다.

개발용 `evaluations/memory_recall.py`·JSON은 [회상 품질 평가](#기억-회상-평가)의 명령·고정 자료를 소유한다.
제품 runtime의 공개 도구와 별도로 실행하고 지표·격리·실패 처리는 [평가 회귀](../../tests/test_memory_recall_evaluation.py)로 확인한다.
개발용 `scripts/check.py`·`check_docs.py`는 [공통 검사](#공통-자동-검사)를, `.github/workflows/check.yml`은 GitHub의 트리거·실행 환경·결과 보관을 소유한다.

리소스 범주, 구체 어댑터, 설치 recipe, profile의 실제 인스턴스는 서로 다른 역할이다.
어댑터 추가 절차와 승인 경계는 [리소스 계약](../spec/resources/contract.md#결과와-제품-교체), 식별자·호환 이름·recipe는 [첫 연결 사양](../spec/resources/initial-adapters.md#어댑터별-실제-지원-경계)을 따른다.

## 저장 위치와 데이터 역할

CLI와 MCP는 같은 OS 사용자의 저장소를 공유한다.
기본 위치는 macOS `~/Library/Application Support/DAERI`, Windows `%LOCALAPPDATA%/DAERI`다.
`DAERI_DATA_DIR`로 위치를 지정할 수 있고 CLI의 `--data-dir PATH`는 이 값보다 우선한다.
환경 변수는 절대 경로나 현재 사용자 홈 기준의 `~`·`~/...` 경로를 받으며 Windows는 `~\...`도 지원한다.
일반 상대 경로는 검증 오류로 반환한다.
명시적인 CLI `--data-dir` 인자는 현재 작업 디렉터리 기준의 상대 경로도 받는다.

| 구분 | 위치·책임 |
|---|---|
| 프로그램 | 플러그인 `.runtime/uv.exe`·`.venv`와 해당 사본의 생성 `.mcp.json`; 사용자 `tools/`의 잠긴 하위 MCP 환경 |
| 관리 Python runtime | 사용자 `runtime/python`·`runtime/cache`; 플러그인 업데이트와 별개로 유지하는 Python과 준비 캐시 |
| 설정·업무·기억·파일 | [데이터별 정본과 형식](../spec/configuration/profiles.md#데이터별-정본과-형식)에 따라 SQLite·JSON·YAML·작업 및 관리 파일로 분리 |
| 비밀 | OS 인증 저장소·선택한 제공자 표준 캐시에서 해소하고 실행 자료에는 참조를 연결 |

업데이트·재설치 뒤에도 사용자 저장소의 설정·업무·기억·자료를 유지한다.
만료·삭제·기억의 출처 승계·복원 조건과 현재 제공 범위 검사는 [로컬 저장 구조](../spec/configuration/profiles.md#로컬-저장-구조)가 정본이다.
인증 문자열 제거는 알려진 패턴을 검사하는 보조 기능이며 자료 제공은 필드 허용·마스킹·현재 정책을 함께 검사한다.
임의 비밀·개인정보의 탐지는 별도의 검증 대상이다.

## 검증의 해석

로컬 자동 테스트는 허용·거부·부분 결과·충돌·복구와 실제 stdio 전달을 확인한다.
하위 에이전트 게이트웨이·공유 예산·동시 호출의 로컬 검증과 실제 호스트의 하위 에이전트 생성·MCP 전달·완료 회수는 각각의 근거로 확인한다.
하위 프로그램의 잠긴 설치·import·도구 메타데이터 확인과 모의 리소스의 전체 실행은 서로 다른 검증이다.
실제 호스트 설치·대화 의미 해석·하위 에이전트 생성과 도구 권한·회사 리소스·제공자 로그인·Windows 실행은 별도의 근거가 필요하다.
현재 검증·남은 범위·실행 결과는 [최초 구현 계획](../plans/initial-implementation.md#현재-구현-단위와-검증-경계)에서 관리한다.
