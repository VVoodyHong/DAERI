---
name: setup
description: DAERI 설치·실행 환경 준비, profile·리소스 설정 또는 연결 진단을 요청할 때 사용한다.
---

# DAERI setup

기본 한국어로 현재 질문·확인한 결과·사용자가 할 행동을 간결하게 안내한다.
운영 대상은 `profile`로 부른다.
`setup PROFILE`은 기존 profile의 연결 또는 요청한 설정 작성을 시작한다.
profile은 대화 입력, 선택·확인은 호스트 질문 도구, 접속 정보·비밀번호·토큰은 profile별 설정 YAML로 받는다.
최초 setup과 이후 설정 변경은 [같은 사용자 설정 파일](../../docs/spec/configuration/profiles.md#생성과-파일-위치)에서 진행하며 저장된 선택·편집 내용과 미완료 단계에서 이어간다.

`../../`는 설치된 플러그인 루트다.
macOS는 `DAERI_MANAGE="<루트>/scripts/manage.sh"`로 스크립트 경로를 정하고 `sh "$DAERI_MANAGE" check`처럼 각 관리 인수를 분리해 전달한다.
Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File "<루트>\scripts\manage.ps1"` 뒤에 각 관리 인수를 전달한다.
runtime이 반환한 `setup_required`는 정상적인 첫 준비 상태다.
상세 계약이 필요한 단계에서는 링크한 해당 절을 호스트의 파일 읽기 도구로 확인한다.

## 시작 로고

setup 호출의 첫 대화 응답에 아래 로고를 `text` 코드 블록으로 한 번 표시한다.
로고 다음에는 현재 단계의 질문이나 진행 안내를 이어서 전달한다.

```text
    ____  ___    __________  ____
   / __ \/   |  / ____/ __ \/  _/
  / / / / /| | / __/ / /_/ // /
 / /_/ / ___ |/ /___/ _, _// /
/_____/_/  |_/_____/_/ |_/___/

   MULTI ORCHESTRATION AGENT
```

## 기존 profile 연결

이미 등록한 profile을 현재 호스트에서 쓰려는 요청은 관리 진입의 `--connect profile show PROFILE`로 확인한다.
이 경로가 준비한 현재 설치의 실행 환경과 기존 접속·인증·기억 선택을 사용하고 [새 MCP 구성 읽기](../../docs/spec/hosts/plugins.md#설치부터-업무-시작까지의-실제-경계)를 안내한다.
실제 등록을 확인한 profile은 [대화의 선택 대상](../profile/SKILL.md)으로 연결하고 이어가기 요청은 [work](../work/SKILL.md)로 진행한다.
새 profile 생성이나 접속·인증·임베딩 등의 설정 변경 요청은 아래 세팅 순서에서 해당 입력을 편집·검증·적용한다.

## 세팅 순서

1. profile이 없으면 “설정할 profile을 입력해 주세요. 예: service-prod”라고 묻고 입력을 기다린다.
   새 세팅은 “DAERI 세팅을 시작할까요?”에 “세팅 시작”·“중단”으로 확인한다.
   시작은 환경 준비로, 중단은 현재 설정을 보존하고 종료로 이어간다.
2. 관리 진입으로 `check`를 호출한다.
   `setup_required`이면 macOS는 `sh "<루트>/scripts/setup.sh" --prepare`, Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File "<루트>\scripts\setup.ps1" -Prepare`로 준비한다.
   `ready`와 `pyproject.toml`의 버전 일치를 확인하고 기존 설정을 `profile list`로 읽는다.
   손상·중단·버전 차이는 [준비·복구 계약](../../docs/spec/installation/environment.md#구현한-자동-준비의-입출력과-경계)으로 처리한다.
   MCP 구성 누락·새 구성 읽기·실제 도구 목록 확인은 [호스트 연결 계약](../../docs/spec/hosts/plugins.md#설치부터-업무-시작까지의-실제-경계)을 적용한다.
   현재 대화의 MCP 연결이 미준비이면 관리 진입으로 설정을 이어간다.
   `setup status PROFILE`의 [초안 상태](../../docs/spec/configuration/profiles.md#생성과-파일-위치)로 다음 단계를 정한다.
   `new`는 선택 단계로, `resume`은 기존 초안 재개로, 오류는 반환한 원인의 복구 안내로 이어간다.
3. 새 초안은 `setup catalog`를 조회하고 [setup 질문 계약](../../docs/spec/hosts/plugins.md#setup의-정형-질문)을 읽어 종류·제품·연결 개수를 확인한다.
   카탈로그의 표시명·설명을 사용하고 제품별 실제 1 이상 정수 개수를 확정한다.
   요청에 명시한 제품·개수는 재사용하고 나머지만 질문한다.
4. 기억 검색 선택은 초안 → 기존 profile 순서로 재사용하고 둘 다 없으면 같은 질문 계약의 임베딩 선택을 받는다.
   회사 API는 `embedding_adapters`와 회사 규격을 대조해 `adapter_id`를 확인받는다.
   미확인은 API 초안을 보존하고 규격 확인 또는 키워드 전용 전환으로 이어간다.
5. 제품별 확인 개수만큼 연결을 구성해 `setup draft PROFILE`을 실행한다.
   각 연결은 선택한 `recipe_id`로 `--resource RECIPE`를, 사용자가 역할 이름을 지정했으면 `--resource INSTANCE=RECIPE`를 전달한다.
   연결 이름은 runtime의 [이름 규칙](../../docs/spec/configuration/profiles.md#공통-구조와-어댑터-입력)을 사용한다.
   새 임베딩 선택은 `--embedding`, 회사 API의 확인한 연결 방식은 `--embedding-adapter`로 전달한다.
   기존 설정의 재개는 `setup draft PROFILE`로 경로를 확인하고 이전 위치의 파일은 runtime의 이관 결과를 사용한다.
   제품별 연결 개수와 반환한 파일 위치를 안내하고 접속 정보·인증값을 작성한 뒤 알려 달라고 요청한다.
   파일 내용은 로컬 DAERI의 검증·적용 결과로 확인하고 호스트는 인증 참조·등록 상태를 사용한다.
6. 작성 완료 응답은 `setup validate PROFILE`로 이어간다.
   오류는 필드 위치와 수정 안내로 전달하며 같은 파일의 재검증을 기다린다.
   유효하면 변경 요약과 “이 설정을 적용할까요?”를 보여 주고 “적용”·“중단”으로 확인한다.
   회사 API 선택은 endpoint·모델과 허용 기억 본문·검색 질의 전송을 요약한다.
   적용 응답과 해당 검증의 `confirmation_token`이 있으면 `setup apply PROFILE --confirm TOKEN`을 실행한다.
   파일·개정·계약이 바뀌면 차이를 확인하고 새 검증·확인을 받는다.
7. `resource prepare PROFILE`로 [저장한 연결 전체를 준비](../../docs/spec/configuration/profiles.md#준비-카탈로그와-개별-프로그램-설치)하고 반환한 연결별 성공·실패를 확인한다.
   로컬 소스 코드는 등록 디렉터리 접근을 확인하고 외부 서비스는 해당 연결의 프로그램·인증을 준비한다.
   `auth status`로 [YAML에서 등록한 인증](../../docs/spec/configuration/profiles.md#접속-정보와-인증값)의 상태를 확인한다.
   누락·변경할 인증값은 같은 YAML의 해당 `credentials` 항목을 편집해 재검증·적용한다.
   제공자 로그인이 필요한 연결은 [인증 계약](../../docs/spec/configuration/profiles.md#인증-저장과-전달)의 해당 로그인 경로로 진행한다.
   준비 범위 확대·추가 권한이 필요하면 이유와 변경 범위를 확인받는다.
8. `resource diagnose`와 [선택한 기억 검색 준비](../../docs/spec/installation/environment.md#임베딩-선택과-준비)를 진행한다.
   실패는 저장한 설정·제공자 선택을 보존하고 해당 미완료 단계의 다음 조치를 안내한다.
   접속 정보·인증값의 오류는 같은 YAML에서 해당 항목만 수정하고 검증·확인·적용한 뒤 재진단한다.
9. profile 저장·리소스별 연결·기억 검색·현재 MCP 연결을 각각 실제 확인 결과로 보고한다.
   실제 등록을 확인한 profile을 [대화의 선택 대상](../profile/SKILL.md)으로 안내하고 업무는 [work](../work/SKILL.md)의 계획 승인으로 이어간다.

## 질문과 실행 경계

질문 도구·실제 응답·Plan과 실행 모드의 조건은 [호스트 계약](../../docs/spec/hosts/plugins.md#setup의-정형-질문)을 적용한다.
입력·확인·적용은 [YAML 계약](../../docs/spec/configuration/profiles.md#사용자용-setup-yaml), 원격 업무는 별도의 계획 승인을 따른다.
