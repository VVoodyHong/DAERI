---
name: setup
description: DAERI 설치·실행 환경 준비, profile·리소스 설정 또는 연결 진단을 요청할 때 사용한다.
---

# DAERI setup

기본 한국어로 현재 질문·확인한 결과·사용자가 할 행동을 간결하게 안내한다.
운영 대상은 `profile`로 부른다.
`setup PROFILE`은 해당 profile의 세팅을 시작한다.
profile은 대화 입력, 선택·확인은 호스트 질문 도구, 접속 정보·비밀번호·토큰은 profile별 설정 YAML로 받는다.
최초 setup과 이후 설정 변경은 [같은 사용자 설정 파일](../../docs/spec/configuration/profiles.md#생성과-파일-위치)에서 진행하며 저장된 선택·편집 내용과 미완료 단계에서 이어간다.

`../../`는 설치된 플러그인 루트다.
macOS는 `DAERI_MANAGE="<루트>/scripts/manage.sh"`로 스크립트 경로를 정하고 `sh "$DAERI_MANAGE" check`처럼 각 관리 인수를 분리해 전달한다.
Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File "<루트>\scripts\manage.ps1"` 뒤에 각 관리 인수를 전달한다.
실행기가 반환한 `setup_required`는 정상적인 첫 준비 상태다.
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

       AI OPERATIONS AGENT
```

## 세팅 순서

1. profile이 없으면 “설정할 profile을 입력해 주세요. 예: service-prod”라고 묻고 입력을 기다린다.
   profile은 영문·숫자로 시작하는 영문·숫자·`_`·`-`의 1~64자 문자열이다.
   새 세팅은 “DAERI 세팅을 시작할까요?”에 “세팅 시작”·“중단”으로 확인한다.
   시작은 환경 준비로, 중단은 현재 설정을 보존하고 종료로 이어간다.
2. 관리 진입으로 `check`를 호출한다.
   `setup_required`이면 macOS는 `sh "<루트>/scripts/setup.sh" --prepare`, Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File "<루트>\scripts\setup.ps1" -Prepare`로 준비한다.
   `check`의 `version`과 플러그인 `pyproject.toml`의 버전을 대조하고 차이가 있으면 확인한 세팅 범위에서 환경을 준비한다.
   준비 완료 뒤 `check`의 `ready`·버전 일치를 확인하고 `profile list`로 기존 설정을 읽는다.
   `ready: true`여도 `operations` 도구가 열거되지 않았으면 루트 `.mcp.json`의 존재를 확인한다.
   파일이 없으면 이미 시작에 동의한 세팅 범위에서 위 준비 명령을 다시 실행한다.
   준비 과정이 생성한 MCP 연결 구성은 [호스트별 다시 읽기](../../docs/spec/hosts/plugins.md#설치부터-업무-시작까지의-실제-경계)로 반영한다.
   실제 MCP 초기화·도구 목록 확인은 로컬 준비 결과와 구분하고, 현재 대화에서 연결이 아직 준비되지 않았으면 관리 진입으로 설정을 이어간다.
   기존 환경의 손상·중단·버전 차이는 [준비·복구 계약](../../docs/spec/installation/environment.md#구현한-자동-준비의-입출력과-경계)으로 처리한다.
   `setup status PROFILE`의 [초안 상태](../../docs/spec/configuration/profiles.md#생성과-파일-위치)로 다음 단계를 정한다.
   `new`는 선택 단계로, `resume`은 기존 초안 재개로, 오류는 반환한 원인의 복구 안내로 이어간다.
3. 새 초안은 `setup catalog`를 조회하고 “연결할 리소스 종류를 선택해 주세요.”로 복수 선택을 받는다.
   선택지의 `label`은 `groups[].label`, `description`은 `groups[].description`을 그대로 사용한다.
   선택한 항목은 같은 그룹의 `type`에 대응시키고 모든 종류에 같은 선택 형식과 분할 규칙을 적용한다.
   제품이 하나인 종류는 그 제품을 사용하고 여럿이면 `products[].display_name`으로 사용할 제품을 복수 선택받는다.
   선택한 제품마다 “<제품>은 몇 개 연결할까요?”로 1 이상의 정수 개수를 받는다.
   선택지는 “1개”·“2개”·“3개”와 호스트의 기본 입력란으로 구성한다.
   요청에 제품·개수가 명시되어 있으면 그 값을 재사용하고 나머지만 질문한다.
   질문 도구가 지원하는 수만큼 묶고 각 개수가 1 이상의 정수인 실제 응답을 받은 뒤 YAML을 생성한다.
   질문·입력 조건은 [setup 질문 계약](../../docs/spec/hosts/plugins.md#setup의-정형-질문)을 따른다.
4. 기억 검색 선택은 초안 → 기존 profile 순서로 재사용한다.
   처음에는 “기억 검색에 사용할 임베딩 방식을 선택해 주세요.”로 묻는다.
   표시 선택지는 “로컬 임베딩 모델”·“회사 임베딩 API”·“키워드 검색만 사용”이며 내부 값은 각각 `local`·`api`·`keyword_only`다.
   회사 API는 `setup catalog`의 `embedding_adapters`에 있는 `label`·`description`으로 “회사 API의 연결 방식을 확인해 주세요.”라고 묻는다.
   등록된 방식과 함께 “규격 확인 필요”를 선택지로 제공하고 그 응답은 미완료 초안으로 연결한다.
   한 방식만 있어도 회사의 인증·요청·응답 규격과 맞는지 확인받고 선택한 `adapter_id`를 사용한다.
   규격 확인이 남으면 미완료 API 초안을 보존하고 회사 API 계약 확인 또는 키워드 검색 전환으로 이어간다.
5. 제품별 확인 개수만큼 연결을 구성해 `setup draft PROFILE`을 실행한다.
   각 연결은 선택한 `recipe_id`로 `--resource RECIPE`를, 사용자가 역할 이름을 지정했으면 `--resource INSTANCE=RECIPE`를 전달한다.
   연결 이름은 실행기의 [이름 규칙](../../docs/spec/configuration/profiles.md#공통-구조와-어댑터-입력)을 사용한다.
   새 임베딩 선택은 `--embedding`, 회사 API의 확인한 연결 방식은 `--embedding-adapter`로 전달한다.
   기존 설정의 재개는 `setup draft PROFILE`로 경로를 확인하고 이전 위치의 파일은 실행기의 이관 결과를 사용한다.
   제품별 연결 개수와 반환한 파일 위치를 안내하고 접속 정보·인증값을 작성한 뒤 알려 달라고 요청한다.
   파일 내용은 로컬 DAERI의 검증·적용 결과로 확인하고 호스트는 인증 참조·등록 상태를 사용한다.
6. 작성 완료 응답은 `setup validate PROFILE`로 이어간다.
   오류는 필드 위치와 수정 안내로 전달하며 같은 파일의 재검증을 기다린다.
   유효하면 변경 요약과 “이 설정을 적용할까요?”를 보여 주고 “적용”·“중단”으로 확인한다.
   회사 API 선택은 endpoint·모델과 허용 기억 본문·검색 질의 전송을 요약한다.
   적용 응답과 해당 검증의 `confirmation_token`이 있으면 `setup apply PROFILE --confirm TOKEN`을 실행한다.
   파일·개정·계약이 바뀌면 차이를 확인하고 새 검증·확인을 받는다.
7. 적용한 연결을 `resource prepare`로 준비하고 `auth status`로 [YAML에서 등록한 인증](../../docs/spec/configuration/profiles.md#접속-정보와-인증값)의 상태를 확인한다.
   누락·변경할 인증값은 같은 YAML의 해당 `credentials` 항목을 편집해 재검증·적용한다.
   제공자 로그인이 필요한 연결은 [인증 계약](../../docs/spec/configuration/profiles.md#인증-저장과-전달)의 해당 로그인 경로로 진행한다.
   준비 범위 확대·추가 권한이 필요하면 이유와 변경 범위를 확인받는다.
8. `resource diagnose`와 [선택한 기억 검색 준비](../../docs/spec/installation/environment.md#임베딩-선택과-준비)를 진행한다.
   실패는 저장한 설정·제공자 선택을 보존하고 해당 미완료 단계의 다음 조치를 안내한다.
   접속 정보·인증값의 오류는 같은 YAML에서 해당 항목만 수정하고 검증·확인·적용한 뒤 재진단한다.
9. profile 저장·리소스별 연결·기억 검색·현재 MCP 연결을 각각 실제 확인 결과로 보고한다.
   실제 등록을 확인한 profile을 [대화의 선택 대상](../profile/SKILL.md)으로 안내하고 업무는 [work](../work/SKILL.md)의 계획 승인으로 이어간다.

## 질문과 실행 경계

선택·확인은 Claude Code의 `AskUserQuestion`, Codex의 현재 질문 도구를 실제 스키마에 맞춰 사용한다.
질문 도구가 Plan 모드에서만 제공되면 선택 뒤 실행 가능한 모드로 돌아와 파일 생성·적용·설치·로그인을 수행한다.
환경 변경은 최초 준비 계약, 원격 업무는 별도의 업무 계획·승인 계약에 따라 진행한다.
질문·모드의 상세 조건은 [호스트 계약](../../docs/spec/hosts/plugins.md#setup의-정형-질문), 입력·적용은 [YAML 계약](../../docs/spec/configuration/profiles.md#사용자용-setup-yaml)을 따른다.
