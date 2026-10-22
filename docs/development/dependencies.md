# 개발 의존성과 로컬 실행

상태: 첫 버전의 준비·실행·구현 책임과 로컬 검증 정리; 실제 회사·호스트·Windows 검증은 별도

어떤 프로그램이 설치되고 구성 요소가 어떤 자료를 주고받는지 설명한다.
업무·설정·보존 계약은 [제품 명세 지도](documentation-architecture.md#정본-지도), 제공 범위와 실제 확인 경계는 [최초 구현 계획](../plans/initial-implementation.md#현재-구현-단위와-검증-경계)이 정본이다.

## 의존성의 정본

| 정본 | 관리하는 내용 |
|---|---|
| [pyproject.toml](../../pyproject.toml)·[uv.lock](../../uv.lock) | 실행기 Python 지원 범위·직접 의존성·잠긴 전체 조합·개발 의존성·실행 명령 |
| [준비 도구 정책](../../scripts/dependencies.env) | 최초 준비 도구의 고정 버전·공식 출처·SHA256 |
| [연결 카탈로그](../../src/daeri/catalog.json) | 첫 리소스 recipe의 제품·패키지·실행 이름·고정 안전 설정 |
| `adapters/`의 setup 계약 | 어댑터별 공개·인증 입력 스키마, 저장 표현·목적지와 선택한 인증 방식의 최종 구성 |
| `embedding.py`의 `API_ADAPTERS` | 구현한 임베딩 API 연결 방식·표시·생성 필드·검증·호출 |
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
| SQL 구문 분석기 | DB 읽기 입력의 실제 대상·구문·제약 검사 |
| SQLite·FTS5·벡터 확장 | 업무·기억 정본과 로컬 키워드·벡터 검색 |
| 선택 임베딩 어댑터 | 준비한 로컬 모델 또는 허용된 회사 API로 지식·질의를 벡터화; 키워드 전용도 선택 가능 |
| OS 인증 저장소 연결 | 모델·profile JSON 대신 사용자 비밀 참조 해소 |
| uv·Hatchling·pytest | 잠긴 환경 준비·패키지 빌드·독립적인 동작 검증 |

## 개발 환경과 검사

개발자는 uv가 있는 저장소 루트에서 다음 명령으로 환경과 테스트 의존성을 준비한다.
`uv sync`는 지원 Python과 잠긴 패키지를 다운로드할 수 있다.

```sh
uv sync --frozen --extra local-embedding
uv run --no-sync pytest
```

일반 테스트는 사내 인증·운영 리소스·호스트 전역 설정 없이 로컬 계약을 확인한다.
위 개발 명령은 로컬 추론 회귀를 위해 선택 의존성도 포함한다.
API·키워드 전용 사용자 환경은 공통 의존성의 기본 설치로 동작한다.
준비한 실제 모델을 쓰는 검사는 `DAERI_TEST_MODEL_CACHE`가 지정된 경우만 수행한다.
이 검사는 검색 중 DNS를 차단해 모델 로딩·임베딩·한국어 질의와 영어 지식의 검색이 로컬에서 이루어지는지 확인한다.
검증 결과는 실행한 대상·환경·방법의 범위로 보고한다.

플러그인 사용자는 명시적인 setup으로 실행 환경을 준비한다.
macOS는 `sh scripts/setup.sh --prepare`, Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/setup.ps1 -Prepare`를 사용한다.
기본 `--check`·`-Check`는 기존 실행 환경의 로컬 검사 결과를 반환한다.

준비는 고정한 공식 uv를 플러그인 루트의 `.runtime/uv.exe`에 두고 절대 경로로 실행한다.
준비의 변경 대상은 전용 도구·후보 환경·관리 캐시이며 기존 시스템·사용자 환경을 보존한다.
별도 후보 환경에 잠긴 패키지를 설치하고 실제 실행 검사를 통과한 환경만 `.venv`로 전환한다.
설치 출처·해시·관리 Python·환경 정리·실패·중복 준비·전환 복구는 [자동 준비 사양](../spec/installation/environment.md#구현한-자동-준비의-입출력과-경계)을 따른다.

`check`는 설치된 실행 패키지의 `version`과 Python 지원 범위·MCP·SQLite·실제 FTS5 검색 결과를 반환한다.
`ready`는 기본 실행 환경의 검사 결과다.
리소스 인증·진단·기억 모델은 해당 관리 경로의 결과로 확인한다.
저장소 개설 전의 실행기 검사와 저장된 profile의 연결 진단은 별도 결과로 제공한다.

## 관리 입력과 결과

관리 호출은 macOS의 `sh "<플러그인 루트>/scripts/manage.sh"`, Windows의 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File "<플러그인 루트>\scripts\manage.ps1"`에 아래 인수를 전달한다.
실행 프로그램·스크립트 경로·각 인수는 별도 값으로 구성한다.
경로를 재사용할 때 변수에는 스크립트 경로를 보관하고 macOS의 sh·zsh에서 다음과 같이 호출한다.

```sh
DAERI_MANAGE="<플러그인 루트>/scripts/manage.sh"
sh "$DAERI_MANAGE" resource prepare PROFILE INSTANCE --recipe RECIPE
```

스크립트가 전용 환경을 검사하고 준비된 `.venv/bin/python.exe`에 `-I -m daeri.cli`와 분리된 관리 인수를 전달한다.
실행 위치는 플러그인 루트이며 부모의 Python·가상환경 관련 변수는 자식 실행에서 정리한다.

환경과 중단 흔적이 모두 부재하면 `status: setup_required`·짧은 setup 안내와 종료 코드 0을 반환한다.
이 분기는 저장소 개설·설치에 앞선 정상 준비 상태다.
비정상 경로·실행 권한 부족·중단 흔적은 `RUNTIME_UNAVAILABLE` 오류와 종료 코드 1로 반환한다.
실행된 CLI의 결과·오류·종료 코드는 그대로 전달한다.
실제 환경 검사·설치·복구는 사용자가 요청한 [setup](../spec/installation/environment.md#아무-개발-도구가-없는-최초-진입)에서 수행한다.

Skill은 `setup_required` 메시지와 현재 호스트의 setup 호출을 안내하고 해당 요청을 종료한다.
실제 실패는 사용자에게 필요한 영향·다음 조치로 요약하고 기술 정보는 [상세 진단 조건](../spec/hosts/plugins.md#사용자-안내)에 따라 제공한다.
공통 진입·자식 환경·공개 인수는 [환경 격리](../spec/installation/environment.md#공통-실행-파일과-환경-격리), 비밀 입력은 [인증 경로](../spec/configuration/profiles.md#인증-저장과-전달)를 따른다.
아래 표의 대문자는 사용자가 선택할 값이며 실제 명령 이름·필수 인수는 `--help`로 확인할 수 있다.

| 입력 | 처리와 반환 |
|---|---|
| `setup catalog/status/draft/validate/apply` | 카탈로그·초안 상태 조회, 선택별 YAML 생성·검증·인증 등록·일괄 적용; 인수·확인값·복수 연결은 [YAML 관리 계약](../spec/configuration/profiles.md#관리-명령과-재시도) 참조 |
| `profile create/list/show` | 이름·기본 맥락·기간 생성, 등록 profile 목록을 이름순으로 조회·지정 구성 확인 |
| `profile import CONFIG_JSON` | 크기·JSON·비밀·구성 검증 뒤 새 profile 개정 저장 |
| `profile export NAME` | 이동 가능한 공개 구성 필드 반환 |
| `profile delete NAME` | 삭제할 대상·개정·관련 자료 수·확인값을 미리 반환 |
| `profile delete NAME --confirm TOKEN` | 확인한 대상을 차단하고 귀속 자료를 정리한 뒤 profile을 마지막에 삭제 |
| `resource catalog/template` | 지원 recipe와 사용자가 채울 대상·정책 설정안 반환 |
| `resource setup-schema RECIPE` | 선택한 어댑터의 접속·공개 인증 JSON Schema·설정 목적지·인증 메타데이터 반환 |
| `resource validate-setup RECIPE VALUES_JSON` | 값 객체 파일의 부분 검증; 유효성·완성 여부·누락 필드·값 없는 오류 반환 |
| `resource configure RECIPE --profile NAME --instance ID --values VALUES_JSON --expected-revision N` | 전체 필수 입력과 현재 개정을 검사하고 기존 profile의 선택한 인스턴스만 추가·부분 갱신 |
| `resource prepare PROFILE INSTANCE --recipe ID` | 선택한 고정 recipe만 격리 환경에 준비·검사 |
| `resource login PROFILE INSTANCE` | 지원되는 제공자 로그인 시작·공개 확인 URL 표시·결과 반환 |
| `resource diagnose PROFILE INSTANCE` | 인증·대상과 MCP 도구 목록을 확인해 기능 계약 등록 |
| `auth set/status/delete REFERENCE` | 사용자 터미널의 숨김 입력·지정 참조 상태·삭제; 터미널 부재는 `input_required`·종료 코드 0 |
| `memory configure PROFILE --config CONFIG_JSON --expected-revision N` | 임베딩 객체만 검증·개정 대조 후 선택 적용; 다른 profile 설정 보존 |
| `memory status/diagnose/prepare/reindex` | 선택한 제공자의 상태·시험 진단·로컬 준비·허용 정본 재색인; 인수·실패는 [준비 계약](../spec/installation/environment.md#임베딩-선택과-준비) 참조 |
| `cleanup` | 만료된 업무 본문·기억·색인 정리 |

일반 관리 결과는 JSON으로 반환한다.
CLI 예외는 `status: error`·`data: null`·오류 코드·고정된 안전 메시지·`next_action: stop`과 종료 코드 1로 반환한다.
인수 파싱 실패의 종료 코드는 2다.
호스트는 오류 코드와 현재 단계로 영향·복구 방법을 안내하며 예외의 원문 메시지는 내부 진단 정보다.
`setup validate`·`resource validate-setup`의 입력 오류는 각 검증 결과의 필드별 `issues`와 종료 코드 1로 구분한다.
`VALUES_JSON` 인자는 UTF-8 공개 값 객체를 담은 파일의 경로다.
스키마 조회·부분 검증은 저장소·비밀 조회·설치·네트워크 접속 없이 수행한다.
`setup status`는 초안 파일 상태만 읽는 명령이며 YAML 내용의 유효성은 초안 재개·전체 검증에서 확인한다.
기본 setup은 [YAML 초안·검증·일괄 적용](../spec/configuration/profiles.md#사용자용-setup-yaml)을 사용한다.
테이블·키·저장소·문서 같은 업무별 조회 대상은 승인한 계획으로 전달한다.
초안·확인값·기준 상태의 연결과 충돌 뒤 명시적 `--refresh`도 이 계약을 따른다.
Plan의 지원 동작은 공개 문서 읽기·카탈로그·스키마 조회와 배포 스키마·공개 값 파일의 개별 JSON 검증이다.
YAML의 전체 검증·초안 생성·적용은 profile 저장소를 사용하므로 실행 가능한 모드에서 수행한다.
`resource configure`는 실행 모드에서 적용 요약을 확인할 때 사용한 profile 개정을 명시해 수행하며 새 profile은 먼저 `profile create`로 만든다.
입력 스키마·기존값 재사용·검증 결과·동시 변경 거부·보존 경계는 [설정 입력 계약](../spec/configuration/profiles.md#리소스-설정-입력과-검증)을 따른다.
새 profile의 `--retention-days`와 `--long-term-memory-days`는 각각 생략하거나 `unlimited`로 지정하면 무기한이며 양의 정수로 유한 기간을 선택한다.
기존 profile의 기간 변경은 전체 export 설정에서 해당 `retention_days`·`long_term_memory_days`만 수정해 import하고 다른 설정을 유지한다.
변경은 새 업무에 적용하며 기존 기한·만료 상태를 보존한다.
저장 의미와 기한 승계는 [보존 설정](../spec/configuration/profiles.md#구현한-profile-입력과-공개-설정)을 따른다.
제공자 로그인만 공개 확인 URL·사용자 코드를 `pending` JSON으로 먼저 전달하고 완료 또는 실패를 반환한다.
모델 준비·로그인·연결 진단의 결과를 확인한 뒤 실제 업무 조회는 해당 계획의 승인을 받아 수행한다.

profile·정책·인증 입력의 필드·실패·지원 경계는 [사용자 구성](../spec/configuration/profiles.md), 기억 준비와 부분 검색은 [기억 사양](../spec/tasks/memory.md)을 따른다.
입력 완성·자료 정책·인증 준비는 각 검증 결과와 미완료 사유로 안내한다.
YAML 인증 등록·참조 치환·실패 복구는 [적용 계약](../spec/configuration/profiles.md#확인적용재개)을 따른다.
profile의 선택·list-profiles Skill은 기존 목록·구성 조회를 사용한다.
대상 선택의 저장 범위는 현재 대화이며 선택 우선순위·목록 표시는 [profile 선택 계약](../spec/configuration/profiles.md#선택과-기본값)을 따른다.
삭제는 같은 profile Skill에서 사용자 확인 후 관리 CLI로 수행하며 귀속 범위·공유 자료 보존·실패 재시도는 [profile 삭제 계약](../spec/configuration/profiles.md#profile-삭제)을 따른다.

## 실행 진입점과 모듈

`daeri`는 관리 CLI, `daeri-mcp`는 호스트가 연결할 MCP 서버다.
두 호스트의 플러그인 구성은 전용 `.venv/bin/python.exe`를 절대 경로로 실행하고 `-I -m daeri.server`를 전달한다.
실행기 선택은 설치한 플러그인의 전용 Python 절대 경로로 고정한다.
일반 MCP는 준비된 실행기를 시작하고 표준 출력은 MCP 전달에 사용한다.
준비·로그인 요청은 관리 진입으로 연결한다.

호스트가 계획·사용자 응답·호출·보고를 전달하면 경계에서 입력을 검사하고 실행기가 저장 상태·정책과 대조한다.
실제 리소스 요청은 등록부·어댑터를 통과하고 허용 자료만 보고·기억에 연결한다.
다음 모듈은 같은 로컬 배포 안에서 이 흐름의 책임을 나눈다.

| 모듈·파일 | 책임과 자료 흐름 |
|---|---|
| `server.py`·`contracts.py` | 15개 MCP 도구의 입력·응답을 검사하고 공통 실행기에 전달 |
| `engine.py`·`planning.py` | 업무 상태·소유권·멱등 처리, 계획·승인·실행·예산과 근거 연결 |
| `resources.py` | 공통 기능 등록·계약 지문·인증 참조 해소·하위 MCP 세션·스키마 및 전송 검사 |
| `adapters/__init__.py`·`adapters/base.py` | 신뢰하는 어댑터 등록·식별자 조회와 공통 읽기 검사·결과 처리 계약 |
| `adapters/`의 제품별 모듈 | 제품별 설정 입력 스키마·대상 설정안·실행 시 인증 변환·로그인·요청 의미·접속 대상·범위·결과 검사 |
| `policy.py`·`security.py` | 필드 제공·마스킹·출처 경로와 알려진 인증 문자열 보조 검사 |
| `results.py` | 실제 근거·완료 조건·표 계산·허용 파일 생성·보고 검증 |
| `memory.py`·`knowledge.py` | profile별 지식의 출처·정책·기한·개정·검색·갱신 검사 |
| `embedding.py` | 임베딩 제공자·API 어댑터 등록·설정 및 호환 입력 검증·선택 구현 생성·공통 전송 허용과 어댑터별 요청·인증·응답 검사 |
| `storage.py` | SQLite 제어 기록과 불변 파일 발행·무결성·만료·정리, 프로세스 소유자의 신호 없는 생존 확인 |
| `profile_deletion.py` | profile 소유 참조·삭제 미리보기·사용 차단·연관 자료 정리·실패 재시도 |
| `cli.py`·`setup.py` | 명시적인 관리 진입·공통 연결 변환·recipe 준비·진단 |
| `onboarding.py`·`setup_yaml.py` | 초안 생성·제한된 YAML 검사·인증값 분리·확인값 대조·일괄 적용과 참조 치환 |
| `credentials.py`·`provider_login.py` | OS 인증 참조와 별도 제공자 SDK의 사용자 로그인 처리 |
| `scripts/`·호스트별 플러그인·`skills/` | 관리 진입의 정상 미준비 판정·환경 격리, 최초 준비·검사와 네 사용자 Skill 전달 |

리소스 범주, 구체 어댑터, 설치 recipe, profile의 실제 인스턴스는 서로 다른 역할이다.
어댑터 추가 절차와 승인 경계는 [리소스 계약](../spec/resources/contract.md#결과와-제품-교체), 식별자·호환 이름·recipe는 [첫 연결 사양](../spec/resources/initial-adapters.md#어댑터별-실제-지원-경계)을 따른다.

## 저장 위치와 데이터 역할

CLI와 MCP는 같은 OS 사용자의 저장소를 공유한다.
`DAERI_DATA_DIR`로 위치를 지정할 수 있고 CLI의 `--data-dir PATH`는 이 값보다 우선한다.
환경 변수는 절대 경로나 현재 사용자 홈을 뜻하는 `~/` 경로만 받으며 상대 경로는 거부한다.
명시적인 CLI `--data-dir` 인자는 현재 작업 디렉터리 기준의 상대 경로도 받는다.
기본 위치는 macOS `~/Library/Application Support/DAERI`, Windows `%LOCALAPPDATA%/DAERI`다.

| 구분 | 위치·책임 |
|---|---|
| 프로그램 | 플러그인 `.runtime/uv.exe`·`.venv`; 사용자 `tools/`의 잠긴 하위 MCP 환경 |
| 관리 런타임 | 사용자 `runtime/python`·`runtime/cache`; 플러그인 업데이트와 별개로 유지하는 Python과 준비 캐시 |
| 설정·제어 | `profiles/` 개정별 JSON; SQLite의 활성 설정·업무·승인·호출·참조 |
| 편집 초안 | `setup/<profile>/setup.yaml`과 관리 정보; 확인한 일괄 적용의 입력, 등록한 인증값은 참조로 치환 |
| 기억·검색 | SQLite의 선별 지식 정본·FTS5·벡터 색인 |
| 본문·산출물 | `records/`·`artifacts/`의 허용 파일; SQLite에서 기한·무결성과 참조 관리 |
| 모델·준비 | `models/`의 로컬 모델; `staging/`의 미공개 파일 |
| 비밀 | OS 인증 저장소·선택한 제공자 표준 캐시; DAERI에는 인증 참조만 저장 |

업데이트·재설치 뒤에도 사용자 저장소의 설정·업무·기억·자료를 유지한다.
만료·삭제·기억의 출처 승계·복원 조건과 현재 제공 범위 검사는 [로컬 저장 구조](../spec/configuration/profiles.md#로컬-저장-구조)가 정본이다.
인증 문자열 제거는 알려진 패턴을 검사하는 보조 기능이며 자료 제공은 필드 허용·마스킹·현재 정책을 함께 검사한다.
임의 비밀·개인정보의 탐지는 별도의 검증 대상이다.

## 검증의 해석

로컬 자동 테스트는 허용·거부·부분 결과·충돌·복구와 실제 stdio 전달을 확인한다.
하위 프로그램의 잠긴 설치·import·도구 메타데이터 확인과 모의 리소스의 전체 실행은 서로 다른 검증이다.
실제 호스트 설치·대화 의미 해석·회사 리소스·제공자 로그인·Windows 실행은 별도의 근거가 필요하다.
현재 검증·남은 범위·실행 결과는 [최초 구현 계획](../plans/initial-implementation.md#현재-구현-단위와-검증-경계)에서 관리한다.
