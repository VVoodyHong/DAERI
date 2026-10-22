---
name: setup
description: DAERI 설치·실행 환경 준비, profile·리소스 설정 또는 연결 진단을 요청할 때 사용한다.
---

# DAERI setup

응답 언어·용어와 현재 단계의 안내는 [사용자 안내](../../docs/spec/hosts/plugins.md#사용자-안내)를 따른다.
`setup PROFILE`은 해당 profile의 세팅을 시작한다.
사용자 선택은 호스트 질문 도구, 접속값은 DAERI가 생성한 YAML로 받는다.
기존 초안은 저장된 선택·편집 내용과 미완료 단계에서 이어간다.

`../../`는 설치된 플러그인 루트다.
시작 전에 호스트의 파일 읽기로 [선택·확인 계약](../../docs/spec/hosts/plugins.md#setup의-정형-질문), [최초 준비](../../docs/spec/installation/environment.md#아무-개발-도구가-없는-최초-진입), [YAML 계약](../../docs/spec/configuration/profiles.md#사용자용-setup-yaml)을 확인한다.
명령 실행은 [공통 관리 진입](../../docs/development/dependencies.md#관리-입력과-결과)을 따른다.

## 세팅 순서

1. 새 세팅은 호스트 계약의 현재 질문으로 profile과 시작 여부를 확인한다.
   세팅 시작 응답은 환경 준비로, 중단 응답은 현재 설정을 보존하고 종료로 이어간다.
2. OS·플러그인 루트를 확인하고 최초 준비 절차로 실행 환경을 확인·준비한다.
   준비·교체 뒤 실제 MCP 연결을 확인하고 실행 모드에서 `profile list`로 기존 설정을 읽는다.
   `setup status PROFILE`의 [초안 상태](../../docs/spec/configuration/profiles.md#생성과-파일-위치)로 다음 단계를 정한다.
   `new`는 선택 단계로, `resume`은 기존 초안 재개로, 오류는 반환한 원인의 복구 안내로 이어간다.
3. 새 초안은 `setup catalog`의 범주·제품 표시값으로 필요한 리소스를 선택받는다.
   모든 범주에 같은 선택 형식과 분할 규칙을 적용한다.
   제품이 하나인 범주는 그 제품을 사용하고 여럿인 범주는 실제 목록에서 선택받는다.
4. 기억 검색 선택은 초안 → 기존 profile 순서로 재사용한다.
   처음 선택할 때는 호스트 계약의 임베딩 질문·선택지로 받고 세부 입력은 YAML에 담는다.
5. `setup draft PROFILE --resource INSTANCE=RECIPE`로 선택한 연결의 초안을 생성한다.
   복수 연결은 `--resource`를 반복하고 새 임베딩 선택은 `--embedding`으로 전달한다.
   초안 재개는 `setup draft PROFILE`로 경로를 확인한다.
   반환한 파일 위치를 안내하고 작성 완료 응답을 기다린다.
6. 작성 완료 응답은 `setup validate PROFILE`로 이어간다.
   오류는 필드 위치와 수정 안내로 전달하며 같은 파일의 재검증을 기다린다.
   유효하면 변경 요약을 보여 주고 호스트 계약의 적용 확인을 받는다.
   적용 응답과 해당 검증의 `confirmation_token`이 있으면 `setup apply PROFILE --confirm TOKEN`을 실행한다.
   파일·개정·계약이 바뀌면 차이를 확인하고 새 검증·확인을 받는다.
7. 적용한 연결을 `resource prepare`로 준비하고 [인증 계약](../../docs/spec/configuration/profiles.md#인증-저장과-전달)의 숨김 입력 또는 제공자 로그인으로 인증한다.
   준비 범위 확대·추가 권한이 필요하면 이유와 변경 범위를 확인받는다.
8. `resource diagnose`와 [선택한 기억 검색 준비](../../docs/spec/installation/environment.md#임베딩-선택과-준비)를 진행한다.
   실패는 저장한 설정·제공자 선택을 보존하고 해당 미완료 단계의 다음 조치를 안내한다.
9. profile 저장·리소스별 연결·기억 검색·현재 MCP 연결을 각각 실제 확인 결과로 보고한다.
   실제 등록을 확인한 profile을 [대화의 선택 대상](../profile/SKILL.md)으로 안내하고 업무는 [work](../work/SKILL.md)로 이어간다.

## 질문과 실행 경계

질문 도구의 스키마·모드와 확정 응답 조건은 [호스트 계약](../../docs/spec/hosts/plugins.md#setup의-정형-질문)을 따른다.
파일 생성·적용·설치·로그인은 실행 가능한 모드에서 수행한다.
환경 변경은 최초 준비 계약, 원격 업무는 별도의 업무 계획·승인 계약에 따라 진행한다.
