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
| `recipes/<recipe-id>/pyproject.toml`·`uv.lock` | 각 하위 MCP의 별도 Python 범위·정확한 패키지 조합·명시적 호환 조건 |
| [기억 명세](../spec/tasks/memory.md) | 로컬 모델 선정·임베딩·검색·모델 호환과 보존 기준 |

버전 값을 문서·호스트 설정·Skill에 복제하지 않는다.
카탈로그의 패키지와 recipe의 직접 의존성이 다르면 준비를 거부한다.
각 recipe는 DAERI 실행기와 격리하므로 하위 MCP의 SDK 주 버전이 달라도 앱의 의존성을 바꾸지 않는다.

| 구성 | 사용하는 이유 |
|---|---|
| Python·공식 MCP SDK | 사용자 PC의 CLI·MCP 서버와 하위 MCP 클라이언트 실행 |
| Pydantic·JSON Schema 검사 | 공개 DTO와 등록 기능의 타입·필드·계약 확인 |
| SQL 구문 분석기 | DB 읽기 입력의 실제 대상·구문·제약 검사 |
| SQLite·FTS5·벡터 확장 | 업무·기억 정본과 로컬 키워드·벡터 검색 |
| 로컬 임베딩·ONNX CPU 실행 | 외부 임베딩 서비스 없이 허용 지식·질의를 벡터화 |
| OS 인증 저장소 연결 | 모델·profile JSON 대신 사용자 비밀 참조 해소 |
| uv·Hatchling·pytest | 잠긴 환경 준비·패키지 빌드·독립적인 동작 검증 |

## 개발 환경과 검사

개발자는 uv가 있는 저장소 루트에서 다음 명령으로 환경과 테스트 의존성을 준비한다.
`uv sync`는 지원 Python과 잠긴 패키지를 다운로드할 수 있다.

```sh
uv sync --frozen
uv run --no-sync pytest
```

일반 테스트는 사내 인증·운영 리소스·호스트 전역 설정 없이 로컬 계약을 확인한다.
준비한 실제 모델을 쓰는 검사는 `DAERI_TEST_MODEL_CACHE`가 지정된 경우만 수행한다.
이 검사는 검색 중 DNS를 차단해 모델 로딩·임베딩·한국어 질의와 영어 지식의 검색이 로컬에서 이루어지는지 확인한다.
테스트 통과를 실제 회사 연결이나 호스트 설치의 통과로 일반화하지 않는다.

플러그인 사용자는 명시적인 setup으로 실행 환경을 준비한다.
macOS는 `sh scripts/setup.sh --prepare`, Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/setup.ps1 -Prepare`를 사용한다.
기본 동작인 `--check`·`-Check`는 설치·다운로드·로그인을 하지 않는다.

준비는 고정한 공식 uv를 플러그인 루트의 `.runtime/uv.exe`에 두고 절대 경로로 실행한다.
사용자·시스템 PATH와 shell 시작 파일·기존 uv·Python·가상환경은 변경하지 않는다.
별도 후보 환경에 잠긴 패키지를 설치하고 실제 실행 검사를 통과한 환경만 `.venv`로 전환한다.
설치 출처·해시·관리 Python·환경 정리·실패·중복 준비·전환 복구는 [자동 준비 사양](../spec/installation/environment.md#구현한-자동-준비의-입출력과-경계)을 따른다.

`check`는 실행기의 Python 지원 범위·MCP·SQLite와 실제 FTS5 검색을 확인한다.
이 결과의 `ready`는 기본 실행 환경이며 리소스 인증·진단과 기억 모델 준비를 대신하지 않는다.
사용자 저장소를 만들지 않고 확인할 수 있는 검사와 profile별 연결 상태를 구분한다.

## 관리 입력과 결과

플러그인 루트의 `.venv/bin/python.exe`를 절대 경로로 지정해 `-I -m daeri.cli`로 관리 작업을 수행한다.
두 OS의 공통 진입 파일과 자식 환경 정리는 [환경 격리](../spec/installation/environment.md#공통-실행-파일과-환경-격리)를 따르며 관리 인수에 비밀값을 넣지 않는다.
아래 표의 대문자는 사용자가 선택할 값이며 실제 명령 이름·필수 인수는 `--help`로 확인할 수 있다.

| 입력 | 처리와 반환 |
|---|---|
| `profile create/list/show` | 이름·기본 맥락·기간 생성, 등록 profile 목록을 이름순으로 조회·지정 구성 확인 |
| `profile import CONFIG_JSON` | 크기·JSON·비밀·구성 검증 뒤 새 profile 개정 저장 |
| `profile export NAME` | 내부 상태·인증 비밀을 제외한 이동 가능한 설정 반환 |
| `resource catalog/template` | 지원 recipe와 사용자가 채울 대상·정책 설정안 반환 |
| `resource prepare PROFILE INSTANCE --recipe ID` | 선택한 고정 recipe만 격리 환경에 준비·검사 |
| `resource login PROFILE INSTANCE` | 지원되는 제공자 로그인 시작·공개 확인 URL 표시·결과 반환 |
| `resource diagnose PROFILE INSTANCE` | 인증·대상과 MCP 도구 목록을 확인해 기능 계약 등록 |
| `auth set/status/delete REFERENCE` | 실제 터미널의 숨김 입력·지정 참조 상태·삭제만 처리 |
| `memory prepare/status/reindex` | 명시적 모델 준비·준비 상태·현재 정본 기반 재색인 |
| `cleanup` | 만료된 업무 본문·기억·색인 정리 |

일반 관리 결과는 JSON으로 반환하며 입력·저장·준비 실패는 비밀 없는 오류와 0이 아닌 종료 코드로 구분한다.
새 profile의 `--retention-days`와 `--long-term-memory-days`는 각각 생략하거나 `unlimited`로 지정하면 무기한이며 양의 정수로 유한 기간을 선택한다.
기존 profile의 기간 변경은 전체 export 설정에서 해당 `retention_days`·`long_term_memory_days`만 수정해 import하고 다른 설정을 유지한다.
변경은 새 업무에 적용하며 기존 유한 기한의 자동 연장·만료 자료 복구를 허용하지 않는다.
저장 의미와 기한 승계는 [보존 설정](../spec/configuration/profiles.md#구현한-profile-입력과-공개-설정)을 따른다.
제공자 로그인만 공개 확인 URL·사용자 코드를 `pending` JSON으로 먼저 전달하고 완료 또는 실패를 반환한다.
모델 준비·로그인·연결 진단이 실제 운영 업무 조회를 포함하지 않으며 업무 계획 승인은 별도로 받는다.

profile·정책·인증 입력의 필드·실패·지원 경계는 [사용자 구성](../spec/configuration/profiles.md), 기억 준비와 부분 검색은 [기억 사양](../spec/tasks/memory.md)을 따른다.
미완성 template·기본 거부 정책·누락 인증을 준비 성공으로 채우지 않는다.
profile·list-profiles Skill은 기존 목록·구성 조회를 사용한다.
대화 안의 대상 선택과 목록 표시 범위는 [profile 선택 계약](../spec/configuration/profiles.md#선택과-기본값)을 따르며 별도 전역 선택 CLI를 추가하지 않는다.

## 실행 진입점과 모듈

`daeri`는 관리 CLI, `daeri-mcp`는 호스트가 연결할 MCP 서버다.
두 호스트의 플러그인 구성은 전용 `.venv/bin/python.exe`를 절대 경로로 실행하고 `-I -m daeri.server`를 전달한다.
일반 MCP 시작에 uv·PATH 검색·shell을 사용하지 않으며 현재 작업 디렉토리나 외부 환경으로 실행기를 바꾸지 않는다.
일반 MCP 시작에서 다운로드·설치·로그인을 하지 않으며 표준 출력은 MCP 전달에만 사용한다.

호스트가 계획·사용자 응답·호출·보고를 전달하면 경계에서 입력을 검사하고 실행기가 저장 상태·정책과 대조한다.
실제 리소스 요청은 등록부·어댑터를 통과하고 허용 자료만 보고·기억에 연결한다.
다음 모듈은 이 흐름의 책임을 나누며 서로 별도 서버로 배포하는 구성은 아니다.

| 모듈·파일 | 책임과 자료 흐름 |
|---|---|
| `server.py`·`contracts.py` | 15개 MCP 도구의 입력·응답을 검사하고 공통 실행기에 전달 |
| `engine.py`·`planning.py` | 업무 상태·소유권·멱등 처리, 계획·승인·실행·예산과 근거 연결 |
| `resources.py`·`adapters/` | 등록 기능·하위 MCP 세션·실제 제품별 요청 의미·대상·범위 검사 |
| `policy.py`·`security.py` | 필드 제공·마스킹·출처 경로와 알려진 인증 문자열 보조 검사 |
| `results.py` | 실제 근거·완료 조건·표 계산·허용 파일 생성·보고 검증 |
| `memory.py`·`knowledge.py` | profile별 지식의 출처·정책·기한·개정·검색·갱신 검사 |
| `storage.py` | SQLite 제어 기록과 불변 파일 발행·무결성·만료·정리 |
| `cli.py`·`setup.py` | 명시적인 사용자 구성·환경·recipe 준비·진단 진입 |
| `credentials.py`·`provider_login.py` | OS 인증 참조와 별도 제공자 SDK의 사용자 로그인 처리 |
| `scripts/`·호스트별 플러그인·`skills/` | 최초 준비·검사와 setup·profile·work·list-profiles 지침 전달 |

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
| 기억·검색 | SQLite의 선별 지식 정본·FTS5·벡터 색인 |
| 본문·산출물 | `records/`·`artifacts/`의 허용 파일; SQLite에서 기한·무결성과 참조 관리 |
| 모델·준비 | `models/`의 로컬 모델; `staging/`의 미공개 파일 |
| 비밀 | OS 인증 저장소·선택한 제공자 표준 캐시; DAERI에는 인증 참조만 저장 |

업데이트·재설치가 사용자 자료를 초기화하지 않는다.
만료·삭제·기억의 출처 승계·복원 조건과 현재 제공 범위 검사는 [로컬 저장 구조](../spec/configuration/profiles.md#로컬-저장-구조)가 정본이다.
인증 문자열 제거는 알려진 패턴의 보조 검사이며 모든 비밀·개인정보 탐지를 보장하지 않는다.

## 검증의 해석

로컬 자동 테스트는 허용·거부·부분 결과·충돌·복구와 실제 stdio 전달을 확인한다.
하위 프로그램의 잠긴 설치·import·도구 메타데이터 확인과 모의 리소스의 전체 실행은 서로 다른 검증이다.
실제 호스트 설치·대화 의미 해석·회사 리소스·제공자 로그인·Windows 실행은 별도의 근거가 필요하다.
현재 확보한 검증과 남은 범위는 [최초 구현 계획](../plans/initial-implementation.md#현재-구현-단위와-검증-경계)에 모으고 테스트 개수·버전을 여러 문서에 복제하지 않는다.
