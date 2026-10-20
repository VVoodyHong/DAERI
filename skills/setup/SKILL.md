---
name: setup
description: DAERI 설치·실행 환경 준비, profile·리소스 설정 또는 연결 진단을 요청할 때 사용한다.
---

# DAERI setup

첫 안내부터 질문·진행 상황·완료 보고까지 기본적으로 한국어로 응답한다.
사용자가 다른 언어를 명시하면 그 언어를 사용하고 명령·설정 키·제품명은 원래 표기를 유지한다.

사용자가 DAERI의 설치·환경·profile·리소스 연결 관리를 요청했을 때 적용한다.
`setup PROFILE`은 해당 profile의 구성을 준비한다.
등록 목록만 조회할 때는 [list-profiles](../list-profiles/SKILL.md), 기존 profile을 이 대화의 기본 대상으로 선택할 때는 [profile](../profile/SKILL.md)을 사용한다.
이 Skill에서 `../../`는 설치된 플러그인 루트이며 사용자 작업 디렉토리와 구분한다.
[환경 준비](../../docs/spec/installation/environment.md), [profile·인증](../../docs/spec/configuration/profiles.md), [개발 의존성과 실행](../../docs/development/dependencies.md)을 필요한 범위에서 실제 읽는다.

## 사용자 질문

[정형 질문 계약](../../docs/spec/hosts/plugins.md#setup의-정형-질문)을 읽고 초기 설정의 선택·입력·확인은 호스트 질문 도구로 받는다.
Claude Code는 `AskUserQuestion`, Codex는 현재 제공되는 `request_user_input` 또는 같은 역할의 질문 도구를 사용한다.
일반 채팅에 질문 목록을 적거나 사용자가 설정 JSON 전체를 작성하게 하지 않는다.
선택지는 실제 카탈로그에서 만들고 비밀이 아닌 직접 입력도 질문 도구의 입력란에서 필드명·형식·가상 예시·필수 여부를 안내한다.
이미 확인한 값은 재사용하고 다음 결정에 필요한 미확정 항목만 소량씩 질문한다.
입력 누락·취소·시간 경과를 기본값 확정이나 진행 동의로 취급하지 않는다.

질문 도구가 현재 모드에 없으면 필요한 대화 모드로 전환하도록 안내하고 답변을 임의로 채워 진행하지 않는다.
Codex의 질문 도구가 Plan 모드에서만 제공되면 `/plan` 전환을 안내해 질문·읽기만 수행하고, 입력 요약을 확인한 뒤 실행 가능한 모드에서 설치·저장·로그인·진단을 진행한다.
Plan에서는 파일로 확인할 수 있는 입력을 먼저 수집하고 상태를 바꿀 수 있는 관리 명령은 실행 모드로 미룬다.
모드 전환이나 호스트의 실행 권한을 플러그인이 대신 변경하지 않는다.
런타임이 아직 없으면 질문에 필요한 범주는 설치된 플러그인의 `src/daeri/catalog.json`에서 읽고 일반 업무용 MCP 시작을 우회하지 않는다.
비밀값은 질문 UI에도 입력받지 않고 아래의 보안 입력·제공자 인증 경로를 사용한다.

## 준비와 설정

1. 사용자 OS와 현재 플러그인 루트를 확인한다.
   macOS는 `sh scripts/setup.sh --check`, Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/setup.ps1 -Check`로 검사한다.
   이 검사와 일반 MCP 시작은 설치·다운로드·로그인·원격 자료 조회를 수행하지 않는다.
   업데이트 뒤에는 전용 `.venv/bin/python.exe`를 `-I`로 실행해 확인한 `importlib.metadata.version('daeri-agent')`와 `pyproject.toml`의 프로젝트 버전을 대조한다.
   기본 검사의 성공만으로 설치한 실행기가 새 버전이라고 판단하지 않는다.
2. 누락·비호환 항목이나 업데이트할 실행기가 있으면 전용 uv·관리 Python·잠긴 패키지의 설치 위치와 다운로드 범위를 설명한다.
   uv는 플러그인 루트의 `.runtime/uv.exe`, Python·준비 캐시는 사용자 데이터 경로의 `runtime/python`·`runtime/cache`를 사용한다.
   기존 uv·Python·가상환경·설치 패키지, 사용자·시스템 PATH와 shell 시작 파일을 변경하지 않는다.
   사용자가 요청하거나 확인한 준비 범위에서 macOS는 `sh scripts/setup.sh --prepare`, Windows는 같은 PowerShell 진입에 `-Prepare`를 지정한다.
   명시적 준비만 프로그램을 설치하며 실패를 다른 출처·임의 최신 버전·관리자 권한으로 우회하지 않는다.
   PATH의 다른 uv를 대신 사용하지 않고 환경 정리는 [공통 실행·격리 기준](../../docs/spec/installation/environment.md#공통-실행-파일과-환경-격리)에 맡긴다.
3. 실행 환경을 준비하거나 교체했으면 새 프로세스로 MCP를 다시 연결하고 실제 제공 도구를 확인한다.
   일반 MCP는 플러그인 루트의 `.venv/bin/python.exe`로 `-I -m daeri.server`를 실행하며 uv·PATH 검색·shell·설치·다운로드를 사용하지 않는다.
   프로그램 준비 성공을 현재 대화의 MCP 연결 성공이나 profile 연결 성공으로 대신하지 않는다.
   이전 버전의 PATH 항목이 남아 있으면 과거 항목임을 알리고 수동 정리를 안내하며 자동 삭제·shell 파일 재작성을 하지 않는다.
4. 두 OS 모두 플러그인 루트의 `.venv/bin/python.exe`를 절대 경로로 지정해 `-I -m daeri.cli --help`와 하위 명령 도움말에서 현재 관리 기능을 확인한다.
   모든 관리 명령은 같은 전용 Python과 `-I`를 사용하고 확인한 플러그인 루트에서 실행한다.
   자식 환경의 `__PYVENV_LAUNCHER__`는 빈값으로 전달하고 사용자의 원래 환경 변수는 수정하지 않는다.
   `/setup {profile명}`의 이름은 해당 profile 생성·선택에 사용한다.
   `profile list/show`로 같은 이름의 기존 profile을 먼저 확인하고 있으면 이어서 설정한다.
   기존 profile을 `profile create`나 빈 template으로 덮어쓰지 않는다.
   새 profile은 `profile create`로 만들고 구성 이동은 `profile import CONFIG_JSON`·`profile export NAME`을 사용한다.
   보존기간은 [profile 보존 설정](../../docs/spec/configuration/profiles.md#구현한-profile-입력과-공개-설정)을 읽고 원문 자료와 장기기억을 구분해 설명한다.
   기존 보존 설정을 바꾸려면 export한 전체 설정에서 요청한 필드만 수정해 import하고 다른 연결·정책을 보존한다.
5. 해당 서비스에서 사용하는 리소스 범주·어댑터·실제 대상·연결·인증 방식을 정리한다.
   먼저 `resource catalog`의 실제 결과를 읽고 `type`별로 묶어 사용자가 필요한 리소스 범주를 선택하게 한다.
   그다음 선택한 범주 안에서 `display_name`으로 지원 제품·연결 방식을 안내하고 선택을 내부 recipe ID에 연결한다.
   범주·제품 목록과 개수를 Skill에 고정하지 않고, 질문 도구의 항목 제한이 있으면 질문을 나눠 지원 선택지를 모두 제공한다.
   미지원 제품은 현재 미지원으로 알리고, 제품명·내부 recipe ID·패키지명·인증 환경 변수명만으로 선택을 요구하지 않는다.
   선택한 제품에 필요한 접속 정보·인증 방식·허용 범위를 순서대로 확인하고 선택한 연결에 필요한 프로그램만 명시적으로 준비한다.
   `resource catalog`·`resource template RECIPE --profile NAME --instance ID`로 설정안을 만들고 실제 대상·허용 범위·자료 정책을 채워 가져온다.
   template은 단일 리소스의 설정안이므로 기존 profile에 추가·수정할 때는 export한 전체 설정의 해당 리소스만 병합한 뒤 import한다.
   이미 있는 다른 리소스·기본 맥락·정책·보존 설정을 단일 리소스 template의 기본값으로 교체하지 않는다.
   `resource prepare PROFILE INSTANCE --recipe RECIPE`는 선택한 격리 프로그램만 준비한다.
   외부 MCP의 임의 명령을 추정해 설치하거나 원격 서버 제품을 사용자 PC에 설치하지 않는다.
6. 비밀이 없는 profile 설정과 OS 인증 저장소의 참조를 연결하고 리소스별 진단을 수행한다.
   실제 비밀 입력은 지원되는 로컬 보안 입력 경로로 처리한다.
   비밀값을 대화·명령 인자·profile 설명·설정 예시·로그에 넣지 않는다.
   `auth set REFERENCE`의 숨김 입력은 사용자의 실제 로컬 터미널에서 처리하며 필요하면 명시적 `--stdin` 경로를 안내한다.
   `auth status/delete REFERENCE`는 상태·삭제만 수행한다.
   Infra SSO는 준비된 recipe의 `resource login PROFILE INSTANCE`를 사용한다.
   최초 연결에 필요한 `--start-url`·`--sso-region`·`--role`은 해당 제공자의 공개 설정이고 실제 대상과 일치해야 한다.
   표시된 제공자 확인 URL·사용자 코드로 인증하게 하며 이를 DAERI 업무 계획 승인으로 처리하지 않는다.
   인증 준비 뒤 `resource diagnose PROFILE INSTANCE`로 선택 기능의 메타데이터 연결을 확인한다.
7. 기억 모델 준비와 로컬 추론·검색 준비 상태를 별도로 확인한다.
   `memory status`·`memory prepare`·`memory reindex [--profile NAME]`로 준비·복구한다.
   준비가 필요하면 명시적 관리 경로로 진행하며 외부 임베딩 서비스나 생성 LLM 설치로 대체하지 않는다.
8. 준비한 프로그램·현재 MCP 연결·profile 저장·리소스별 연결과 권한·기억 준비·남은 사용자 조치를 구분해 보고한다.
   실제 진단하지 않은 항목을 성공으로 채우지 않는다.
   설정을 저장한 뒤 `profile show`가 성공한 이름·`profile_id`를 확인하고 [profile의 선택 범위](../profile/SKILL.md)에 따라 이 대화의 이후 새 업무 대상으로 안내한다.
   기존 업무의 스냅샷·승인을 바꾸지 않고 업무가 필요하면 [work](../work/SKILL.md)로 이어가게 한다.
   setup 완료를 업무 접수·실행 또는 업무 계획 승인으로 처리하지 않는다.

명령·이름·경로·사용자 입력은 별도 인자로 안전하게 전달한다.
조직 정책과 인증서·출처 검사를 끄지 않으며 자동화할 수 없는 로그인·조직 조치는 사용자에게 안내한다.
일반 업무의 실패를 숨은 설치·인증·profile 변경으로 전환하지 않는다.
