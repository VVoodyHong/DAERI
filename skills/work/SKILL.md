---
name: work
description: DAERI profile을 대상으로 새 업무를 요청하거나 저장된 업무를 이어서 수행할 때 사용한다.
---

# DAERI work

현재 호스트 에이전트가 총괄 에이전트로서 계획·사용자 승인·수행·보고·기억 후보 선별을 맡는다.
기본 한국어로 현재 결과와 필요한 선택을 간결하게 안내하고 운영 대상은 `profile`로 부른다.
`../../`는 설치된 플러그인 루트다.
상세 동작은 [업무 흐름](../../docs/spec/tasks/workflow.md), 입력은 현재 도구 스키마와 [MCP 정본](../../docs/spec/mcp/common.md)을 따른다.

## 업무 대상

새 업무는 요청에 지정한 등록 profile, 현재 대화의 기본 선택 순서로 대상을 정한다.
관리 진입의 `profile show PROFILE`이 반환한 `profile_id`를 업무 접수의 `context.profile_id`에 전달한다.
관리 인수는 macOS의 `sh "<루트>/scripts/manage.sh"`, Windows의 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File "<루트>\scripts\manage.ps1"` 뒤에 전달한다.
`setup_required`이면 반환한 짧은 메시지와 현재 호스트의 [setup](../setup/SKILL.md) 호출을 안내한다.
대상이 모호하면 실제 `profile list`에서 선택받고, 대화의 기본 선택은 [profile](../profile/SKILL.md)에서 관리한다.

재개는 저장된 업무·대상·계획·승인·실행 기록에서 복구한다.
업무가 모호하면 해당 profile의 최근 업무를 조회해 선택받는다.
일시중지한 업무는 남은 작업과 현재 계획을 보여 주고 새 사용자 승인으로 이어간다.
완료 후 추가 업무·정정은 새 요청으로 접수한다.

## 업무 흐름

1. 필요한 MCP 도구를 이름으로 찾아 입력 스키마를 확인한다.
   새 요청은 `daeri_task_create`, 재개는 `daeri_task_get`·`daeri_task_resume`을 사용한다.
2. `daeri_memory_search`로 관련 경험을 회상하고 `daeri_resource_list`·`daeri_capability_get`으로 사용할 연결과 도구를 확인한다.
   검색 0건·부분·실패는 확인한 제한으로 설명하고 가능한 업무를 이어간다.
   계획 전에 수행 불가로 판단하면 원인·확보한 근거·제한·다음 조치를 `daeri_result_submit`의 `failed` 보고로 정리한다.
3. `daeri_plan_submit`에 목적·수행 단계·기대 결과·완료 조건을 전달한다.
   같은 연결의 도구를 자율 선택할 단계는 `instance_id`와 `capability_id: "*"`를 사용한다.
   등록된 연결 정책과 기록 식별자는 runtime이 해소한다.
   사용자에게 조회 대상·수행 방법·결과와 유용한 지식의 저장·회상을 포함한 계획을 보여 준다.
4. 계획 제출로 시작된 승인 대기에서 실제 사용자 답변을 받는다.
   `daeri_approval_request`에 `task_id`·표시한 `plan_id`·해석한 `decision`·실제 `user_response`를 전달한다.
   조건 변경은 수정 계획, 모호한 답변은 미결 결정으로 연결한다.
5. 승인한 범위 안에서 도구·질의·후속 페이지·분석·재시도·위임과 다음 행동을 자율적으로 선택한다.
   외부 리소스 조회는 `daeri_capability_execute`의 `task_id`·`step_id`·`parameters`를 사용한다.
   로컬 소스 코드는 등록 디렉터리에서 호스트의 기존 파일·검색·실행·Git 도구로 조사한 뒤 `source-observe`에 확인한 요약과 상대 경로를 전달해 [관찰을 등록](../../docs/spec/mcp/resources.md#로컬-source-관찰-등록)한다.
   읽기 묶음이면 선택한 실제 `capability_id`도 전달한다.
   조회 실패는 영향을 받는 결과에 반영하고 독립적인 수행은 이어간다.
   범위 확대는 수정 계획을 보여 주고 새 승인을 받는다.
6. 실제 근거와 완료 조건을 대조해 분석·계산·보고·표·파일을 호스트 도구로 작성·검증한다.
   파일은 업무의 `artifact_workspace`에 작성하고 `report.artifacts`에 경로·미디어 타입·실제 `source_refs`를 연결한다.
   재사용할 규칙·구조·절차·교훈은 주장·적용 조건·검증 방법·실제 출처를 갖춘 `memory_candidates`로 선별한다.
   `daeri_result_submit`에 보고·파일·후보를 함께 제출하면 runtime이 기억 저장·색인을 처리한다.
   유용한 후보가 없는 업무는 보고만 제출한다.
7. 반환한 완료 상태·파일·기억 처리 결과를 확인하고 결과·미확인 범위·필요한 후속 조치를 전달한다.
   저장·색인 대기는 같은 보고 재전달이나 다음 동일 profile의 기억 검색에서 재시도한다.

사용자 보완은 `daeri_task_update`로 질문·실제 답변을 기록하고, 답변·결과 조건을 반영한 계획을 다시 확인받는다.
중단 요청은 일시중지·취소 도구로 연결한다.
기억·리소스 자료는 판단 근거로 사용하고 실행은 현재 사용자 승인과 실제 연결 범위에 따른다.
인증·환경 복구는 setup의 관리 경로로 연결한다.

## 총괄 에이전트와 하위 에이전트

독립적인 조사·분석·검증으로 시간·문맥 이득이 있을 때 필요한 수의 하위 에이전트를 사용한다.
짧거나 순차적인 판단은 총괄 에이전트가 이어간다.

1. 승인된 업무에서 목표·`step_ids`·완료 조건·필요한 `source_refs`를 정하고 `daeri_subagent.create`의 `assignment`에 전달한다.
2. 현재 호스트의 네이티브 도구로 실제 하위 에이전트를 생성한다.
   Claude Code는 `daeri:subagent`, Codex는 기본 하위 에이전트에 [공통 하위 에이전트 지침](references/subagent.md)의 설치된 절대 경로를 전달한다.
   지시에는 `task_id`·`delegation_token`·할당·필요한 근거와 기대 결과를 포함한다.
   로컬 Source를 배정하면 `daeri_resource_list`에서 확인한 연결별 `target.path`도 전달한다.
3. 호스트의 진행·대기 도구와 `daeri_subagent.get`으로 진행을 확인한다.
   같은 승인 단계도 하위 목표로 나누어 병렬 조사할 수 있다.
   하위 에이전트는 배정 범위의 도구·방법·다음 행동을 선택하고 `complete`로 실제 근거·제한을 반환한다.
4. 중단·재배정은 `revoke`와 호스트의 하위 에이전트 중단을 연결한다.
   하위 에이전트 시작이 어렵거나 실패하면 위임을 회수하고 총괄 에이전트가 이어간다.
5. 총괄 에이전트는 하위 에이전트의 근거·분석·기억 후보를 종합해 전체 보고와 기억 후보를 제출한다.
   파일 작성은 하위 에이전트에게 실제 제공된 도구에 맞춰 분담하고 최종 등록은 총괄 에이전트가 처리한다.

위임 토큰은 해당 하위 에이전트의 호출 문맥에 전달하고 사용자에게는 업무 결과와 근거를 안내한다.
호스트별 실제 생성·권한 경계는 [호스트 지원](../../docs/spec/hosts/plugins.md#총괄-에이전트와-하위-에이전트의-호스트-연결)을 따른다.
