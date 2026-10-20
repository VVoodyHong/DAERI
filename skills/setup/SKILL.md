---
name: setup
description: Use when the user requests DAERI setup, profile or resource configuration, connection diagnostics, or preparation of a missing local DAERI environment.
---

# DAERI setup

사용자가 DAERI의 설치·환경·profile·리소스 연결 관리를 요청했을 때 적용한다.
이 Skill에서 `../../`는 설치된 플러그인 루트이며 사용자 작업 디렉토리와 구분한다.
[환경 준비](../../docs/spec/installation/environment.md), [profile·인증](../../docs/spec/configuration/profiles.md), [개발 의존성과 실행](../../docs/development/dependencies.md)을 필요한 범위에서 실제 읽는다.

1. 사용자 OS와 현재 플러그인 루트를 확인한다.
   macOS는 `sh scripts/setup.sh --check`, Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File scripts/setup.ps1 -Check`로 검사한다.
   이 검사와 일반 MCP 시작은 설치·다운로드·로그인·원격 자료 조회를 수행하지 않는다.
2. 누락 항목이 있으면 사용자 전용 uv·Python·잠긴 패키지와 설치 위치·다운로드·사용자 PATH 변경 범위를 설명한다.
   사용자가 요청하거나 확인한 준비 범위에서 macOS는 `sh scripts/setup.sh --prepare`, Windows는 같은 PowerShell 진입에 `-Prepare`를 지정한다.
   명시적 준비만 프로그램을 설치하며 실패를 다른 출처·임의 최신 버전·관리자 권한으로 우회하지 않는다.
3. 새 uv 설치 뒤에는 호스트 재시작과 MCP 재연결 필요 여부를 안내한다.
   현재 호스트의 PATH와 실제 MCP 도구 연결을 다시 확인한다.
   프로그램 준비 성공을 현재 대화의 MCP 연결 성공이나 profile 연결 성공으로 대신하지 않는다.
4. 준비된 Python의 `-m daeri.cli --help`와 해당 하위 명령 도움말에서 현재 관리 기능을 확인한다.
   macOS는 `.venv/bin/python`, Windows는 `.venv/Scripts/python.exe`를 사용하고 모든 명령은 확인한 플러그인 루트에서 실행한다.
   `/setup {profile명}`의 이름은 해당 profile 생성·선택에 사용한다.
   기본 관리는 `profile create/list/show`, 이동 가능한 설정은 `profile import CONFIG_JSON`·`profile export NAME`을 사용한다.
5. 해당 서비스에서 사용하는 리소스 범주·어댑터·실제 대상·연결·인증 방식을 정리한다.
   지원 카탈로그를 우선 사용하고 선택한 연결에 필요한 프로그램만 명시적으로 준비한다.
   `resource catalog`·`resource template RECIPE --profile NAME --instance ID`로 설정안을 만들고 실제 대상·허용 범위·자료 정책을 채워 가져온다.
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

명령·이름·경로·사용자 입력은 별도 인자로 안전하게 전달한다.
조직 정책과 인증서·출처 검사를 끄지 않으며 자동화할 수 없는 로그인·조직 조치는 사용자에게 안내한다.
일반 업무의 실패를 숨은 설치·인증·profile 변경으로 전환하지 않는다.
