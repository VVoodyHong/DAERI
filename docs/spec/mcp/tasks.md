# 업무·하위 에이전트·기억·결과 MCP 도구

상태: 업무·위임·검색·보고·자동 기억 구현; 로컬 계약·통합 검증

모델용 입력과 반환 DTO를 정의한다.
공통 타입·응답은 [MCP 공통](common.md), 동작·보존은 [업무 흐름](../tasks/workflow.md)과 [기억](../tasks/memory.md)을 따른다.
표에서 선택으로 표시한 필드를 제외한 입력은 필수다.

## 공통 업무 DTO

### ContextSelection과 DesiredOutput

`ContextSelection`은 선택 `profile_id:ID`로 사용할 DAERI profile을 지정한다.
생략하면 `awaiting_input`에서 profile을 선택하고 `daeri_task_update`로 연결한다.
현재 업무 분류는 profile을 사용한다.

`DesiredOutput`은 선택 `format:string`, `description:string`으로 희망 형식·결과를 전달한다.
호스트는 실제 사용 가능한 도구와 계획으로 결과를 구성한다.

### TaskContext

반환 context는 `profile_id`, `profile_revision`, `profile_snapshot_ref`와 현재 null인 `department_id`·`environment_id`를 갖는다.
profile 해소 전에는 관련 값도 null이다.
runtime은 [profile 스냅샷](../configuration/profiles.md#업무에-고정하는-스냅샷)과 현재 연결·실제 대상을 대조한다.

### WaitingReason·PlanRef·CallSummary

| DTO | 필드 |
|---|---|
| `WaitingReason` | `kind:string`, `message:string` |
| `PlanRef` | `plan_id:ID`, `plan_version:integer` |
| `CallSummary` | `call_id`, `plan_id`, `plan_version`, `step_id`, `call_state`, `external_effect` |

대기 사유는 사용자 입력·승인·일시중지·범위·복구를 표시한다.
호출 상태는 [실행 응답](resources.md#반환-data)을 따른다.

### TaskSnapshot·TaskReceipt

`TaskReceipt`는 `task_id`, `task_revision`, `request_ref`, `state`, `context`, `waiting_reason`, `artifact_workspace`다.
입력 보완 기록을 만들면 `clarification_ref`도 반환한다.

`TaskSnapshot`은 receipt에 `active_plan_ref`, `last_report_ref`, `active_calls`, `uncertain_effects`, `execution_owner_status`, `subagents`를 추가한다.
빈 참조는 null, 목록은 []다.
owner 상태는 `self|other|unassigned|recovery_required`다.

현재 주요 업무 상태는 `planning|awaiting_input|awaiting_approval|running|paused|succeeded|partial|failed|cancelled`다.
`subagents`의 항목은 `delegation_id`, `state`, `step_ids`, `plan_ref`, `expires_at`, `assignment_ref`, `result_ref`를 갖는다.
파일 작성 경로는 runtime이 반환한 `artifact_workspace`를 사용한다.

## daeri_task_create

| 입력 | 타입·조건 |
|---|---|
| `request_text` | 비어 있지 않은 string; 자연어 업무 요청 |
| `context` | 선택 ContextSelection |
| `desired_output` | 선택 DesiredOutput |

자연어 요청을 보존하고 최초 실행 소유자·업무 ID를 만든다.
profile이 있으면 planning, 선택이 필요하면 awaiting_input이다.
반환 data는 TaskReceipt다.

## daeri_task_update

필수 입력은 `task_id:ID`와 `patch:TaskPatch` 또는 `action:"pause"` 중 하나다.

| patch 필드 | 동작 |
|---|---|
| `question:string` | 질문 기록·입력 대기; 단독 필드 |
| `clarification:string` | 실제 사용자 보완·정정 기록 |
| `context:ContextSelection` | 첫 승인 전에 profile 연결 |
| `desired_output:DesiredOutput` | 원하는 결과 보완 |

patch는 하나 이상의 필드를 받는다.
질문은 수행 중에도 입력 대기로 전환할 수 있고 답변·context·결과 보완은 계획·입력 대기에서 처리한다.
답변·context·결과 보완을 적용하면 이전 승인·하위 에이전트 위임을 종료하고 현재 조건의 계획을 다시 구성한다.
context 변경은 첫 승인 전까지 처리하며 승인 이력이 있는 업무의 실제 대상 변경은 새 업무로 접수한다.

### 일시중지 분기

`action:"pause"`는 새 실행을 멈추고 승인·하위 에이전트 위임을 갱신한다.
진행 중 읽기의 실제 결과는 기록하며 재개 뒤 현재 계획을 확인한다.
반환 data는 TaskReceipt다.

## daeri_task_get

`task_id:ID` 또는 `profile_id:ID` 중 하나를 지정한다.
선택 입력은 `cursor:Cursor`, `limit:integer`이며 기본 20, 범위 1~100이다.

| 조회 | 반환 data |
|---|---|
| 업무 하나 | `task_id`, `task_revision`, `task:TaskSnapshot`, `records`, `next_cursor`, `has_more` |
| profile 최근 업무 | `profile_id`, `tasks`, `next_cursor`, `has_more` |

records는 `record_ref`, `record_kind`, `summary`이며 본문은 계획·결과 도구로 읽는다.
최근 업무 항목은 `task_id`, `title`, `state`, `last_activity_at`이고 마지막 활동 내림차순이다.
조회는 기존 업무의 실행 상태를 유지한다.

## daeri_task_resume

필수 입력은 `task_id:ID`다.
runtime은 현재 실행 소유자·저장 기록·승인·입력 대기를 확인한다.
다른 유효 소유자가 있으면 `OWNER_CONFLICT`다.

종료한 실행 프로세스의 미완료 읽기는 실패 기록으로 정리하고 시간 예약을 해제한다.
유효한 승인과 실행 조건이면 running으로 이어가며 필요한 새 관찰을 선택할 수 있다.
일시중지·수정 계획은 현재 사용자 확인을 거쳐 진행한다.

반환 data는 `task_id`, `task_revision`, `state`, `waiting_reason`, `resumed:boolean`, `active_plan_ref`, `unresolved_call_ids`다.
즉시 이어갈 수 있는 상태는 ok, 입력·승인·실제 진행 중 호출이 남으면 pending이다.
종료 업무는 보존 결과·후속 기억 처리에 사용하며 새 외부 업무는 새로 접수한다.

## daeri_task_cancel

필수 `task_id:ID`, 선택 `reason:string`을 받는다.
업무를 cancelled로 바꾸고 추가 실행·승인·위임을 종료한다.
이미 진행 중인 읽기는 늦게 확보한 결과를 기록하며 취소 상태를 유지한다.

반환 data는 `task_id`, `task_revision`, `state:"cancelled"`, `active_calls`, `uncertain_effects`다.
같은 취소는 현재 상태를 반환한다.

## daeri_subagent

호스트가 생성한 하위 에이전트에 현재 승인 단계의 일부를 배정하는 게이트웨이다.
총괄 에이전트가 역할·완료 조건을 정하고 하위 에이전트의 실제 생성·대기·회수는 [호스트 연결](../hosts/plugins.md#총괄-에이전트와-하위-에이전트의-호스트-연결)을 사용한다.

### 분기별 입력

모든 분기는 `operation`, `task_id`를 받는다.

| operation | 추가 필수 입력 | 선택 입력 |
|---|---|---|
| `create` | `assignment:SubagentAssignment` | 없음 |
| `get` | `delegation_token:string` | 없음 |
| `execute` | `delegation_token`, `call:SubagentCall` | `timeout_ms`; 기본 10,000, 1~60,000 |
| `search` | `delegation_token`, `query:string` | `instance_ids`, `limit`; 검색 도구와 같음 |
| `read` | `delegation_token`, `record_ref` | `cursor`, `limit_bytes`; 결과 조회와 같음 |
| `complete` | `delegation_token`, `result:JsonValue` | `evidence_refs:RecordRef[]`; 기본 [] |
| `revoke` | `delegation_id:ID` | 없음 |

SubagentAssignment는 `label:string`, `objective:string`, `step_ids:ID[]`, `completion_criteria:string[]`와 선택 `source_refs:RecordRef[]`를 갖는다.
step_ids는 1~100개, 완료 조건은 1~20개이며 참조는 실제 제공한 조회·기억이다.

SubagentCall은 `step_id:ID`, `parameters:JsonObject`와 선택 `capability_id:ID`다.
읽기 묶음의 실제 기능은 capability_id로 선택한다.
로컬 Source는 호스트 도구로 조사한 뒤 [관찰 입력](resources.md#로컬-source-관찰-등록)을 `execute.call`에 전달한다.
result는 하위 에이전트가 선택한 문장·표·JSON 등 JSON으로 표현 가능한 결과다.
evidence_refs는 결과에 기여한 고유 출처를 별도 전달한다.
생략·빈 배열은 현재 할당·자신의 완료한 호출·검색한 기억에서 제공 가능한 출처를 연결한다.

### 생성과 호출 허용 조건

create는 현재 승인 계획의 단계를 배정한다.
같은 승인 단계에 여러 하위 에이전트를 배정해 병렬로 읽을 수 있으며 명시한 전체 호출·시간 한도는 업무가 공유한다.
하위 에이전트는 배정한 연결·단계와 제공받은 출처·자신의 호출 결과·해당 위임에서 검색한 현재 기억을 사용한다.

### 반환 `data`

create·get·complete는 `task_id`, `task_revision`, `delegation_id`, `state`, `expires_at`, `plan_ref`, `assignment`, `steps`, `contracts`, `calls`, `result_ref`를 반환한다.
create에는 `delegation_token`, 완료 결과가 있으면 `result`도 포함한다.
보존한 result는 `content`의 자유형 결과와 별도 `evidence_refs`로 구성한다.
revoke는 위임 식별자·현재 상태·기한·계획·결과 참조를 반환한다.

execute·search·read의 data는 대응하는 실행·기억 검색·결과 조회 도구와 같다.
진행 중인 자신의 호출이 끝나면 complete로 결과를 보존하고 총괄 에이전트가 업무 목표에 맞춰 통합한다.

### 멱등·재전달과 수명

신규 위임의 기본 expires_at은 null이며 현재 승인·계획·업무·소유권이 유효한 동안 사용한다.
일시중지·취소·새 계획·총괄 에이전트 소유권 이전·철회로 위임을 갱신한다.
토큰은 배정된 DAERI 호출 자격이고 실제 하위 에이전트의 신원·호스트 도구 접근은 호스트가 관리한다.

## 기억 MCP 도구

검색·저장은 같은 profile과 실제 리소스 대상을 기준으로 분리한다.
검색 방식은 setup에서 선택한 키워드·로컬 모델·회사 API를 사용하며 [임베딩 계약](../tasks/memory.md#임베딩-api-계약)을 따른다.

### MemoryItem

| 필드 | 타입·의미 |
|---|---|
| `memory_ref` | 정확한 기억 개정의 RecordRef |
| `category` | `service_rule\|resource_structure\|procedure\|lesson` |
| `statement`, `applicability` | 지식과 적용 조건 string |
| `instance_ids` | 실제 기여 리소스 ID 배열 |
| `sources` | `{task_id,record_ref,fields:FieldSelector[]}` 배열 |
| `verification_method`, `verified_at` | 확인 방법·실제 관찰 시각 |
| `expires_at` | ISO 8601 string 또는 null |
| `retention` | `source_bound\|long_term` |

현재 유효한 개정·profile·실제 대상·보존 조건을 통과한 본문을 반환한다.
장기 지식의 원문이 만료됐으면 최소 검증 설명을 유지하고 원문 열람 제한을 표시한다.

### daeri_memory_search

| 입력 | 타입·기본값 |
|---|---|
| `task_id`, `query` | 필수 ID·string |
| `instance_ids` | 선택; 허용 대상 부분집합 |
| `limit` | 선택; 기본 10, 1~50 |

query는 비밀 제거 후 UTF-8 4,096바이트까지 받는다.
반환 data는 `task_id`, `task_revision`, `memory_search_ref`, `core_context`, `items:MemoryItem[]`, `retrieval_state`, `limitations`다.
검색 기록은 선택한 기억의 실제 조회 근거로 사용한다.

retrieval_state는 `hybrid|keyword_only|vector_only|unavailable`이다.
정상 0건과 선택한 검색 방식의 정상 처리는 ok다.
일부 경로·색인 장애는 partial·MEMORY_SEARCH_INCOMPLETE, 검색 불가는 error·MEMORY_SEARCH_UNAVAILABLE로 구분한다.

검색 제한은 계획에 반영하며 현재 자료의 업무 수행을 이어갈 수 있다.
회사 API 방식의 제공 가능한 기억이 0건이면 API 전송을 생략하고 정상 키워드 0건을 반환한다.

### daeri_memory_update

| 입력 | 타입·조건 |
|---|---|
| `task_id` | ID |
| `operation` | `store\|invalidate` |
| `source_refs` | 하나 이상의 고유 RecordRef |
| `reason` | 비어 있지 않은 string |
| `draft` | store 필수 MemoryDraft |
| `memory_ref` | store 정정 시 선택, invalidate 필수 |

MemoryDraft는 `category`, `statement`, `applicability`, `instance_ids`, `verification_method`다.
모든 문자열은 비어 있지 않고 instance_ids는 하나 이상의 고유한 등록 연결이다.
후보 JSON은 UTF-8 16KiB까지 받는다.
자동 완료 경로는 아래 결과 제출의 memory_candidates를 사용한다.

### 기억의 출처 필드 확정

호스트는 실제 기여한 source_refs로 지식·적용 조건·확인 방법을 설명한다.
runtime은 선언한 출처를 실제 요청·사용자 보완·관찰까지 해소하고 현재 제공·보관·기억 사용 범위와 실제 대상을 확인한다.
출처가 제공하지 않은 사실이나 자연어 내용의 정확성은 호스트가 검증한다.

### 갱신 검사와 결과

store는 실제 승인 이력과 보고를 보존한 종료 업무에서 처리한다.
기존의 명시적 정책·기한을 유지하며 새 후보와 정정은 같은 profile·실제 대상의 근거를 사용한다.
invalidate는 현재 소유자가 실제 정정·반증으로 기억을 검색에서 제외한다.

반환 data는 `task_id`, `task_revision`, `update_ref`, `memory_ref`, `memory_state:active|invalidated`, `expires_at`, `index_state:ready|pending`다.
정본은 보존했지만 색인이 미완성이면 partial·MEMORY_INDEX_INCOMPLETE다.
기억 실패는 기존 보고·종료 상태를 유지하고 저장·색인 경로에서 복구한다.

## 자유형 결과와 파일

호스트는 사용자 요청과 실제 자료를 대조해 분석 방법·결과 표현·완료 여부를 판단한다.
결과 본문은 문장·표·JSON 등 호스트가 작성한 JSON 값 그대로 보존한다.
실행 상태·기여 출처·파일·기억 후보는 본문 밖의 별도 입력으로 전달한다.
runtime은 실제 승인·현재 자료 조건·기한·파일 무결성을 검사한다.

### 호스트 결과와 파일

호스트는 사용 가능한 도구로 텍스트·표·JSON·CSV·문서·스프레드시트 등을 생성하고 검증한다.
보존할 파일은 TaskReceipt의 artifact_workspace에 작성한다.
runtime은 일반 파일·경로·심볼릭 링크를 검사해 원래 바이트·크기·SHA-256·이름·미디어 타입을 관리 영역에 등록한다.

등록한 작업 원본 중 복사 당시와 같은 내용은 다음 정리에서 제거한다.
실패한 등록의 원본·등록 후 수정한 작업 파일은 업무의 정리 경로를 따른다.
파일 내용·계산·미디어 타입·레이아웃의 일치는 호스트가 검증한다.

### ArtifactDraft

| 종류 | 필수 필드 |
|---|---|
| 새 파일 | `artifact_id:ID`, `label:string`, `local_path:string`, `media_type:string` |
| 기존 파일 | `kind:"existing"`, `artifact_id:ID`, `label:string`, `artifact_ref:RecordRef` |

새 파일의 선택 kind는 file이며 local_path는 해당 업무 작업 영역 안의 절대 경로다.
표시명·ID는 비어 있지 않고 ID는 보고 안에서 고유하다.
media_type은 255자 이내의 미디어 타입이다.
선택 source_refs는 실제 기여 출처이며 기본 []다.
생략·빈 배열은 같은 제출의 evidence_refs를 파일의 기여 출처로 연결한다.

runtime은 파일에 선언한 출처와 현재 내보내기 범위를 검사한다.
파일 기한은 업무·기여 출처의 가장 이른 유한 기한을 따른다.
같은 artifact_id는 등록한 바이트·미디어 타입을 유지하고 다른 내용은 새 ID로 등록한다.

## daeri_result_submit

| 입력 | 타입·기본값 |
|---|---|
| `task_id` | 필수 ID |
| `report` | 필수 JsonValue; 호스트가 선택한 결과 본문 |
| `state` | 선택 `succeeded\|partial\|failed\|cancelled`; 기본 `succeeded` |
| `evidence_refs` | 선택 고유 RecordRef[]; 기본 [] |
| `artifacts` | 선택 ArtifactDraft[]; 기본 [] |
| `memory_candidates` | 선택 후보 목록; 기본 [] |

현재 계획·개정·보고 중복 기록은 runtime이 해소한다.
기여 출처가 생략되거나 빈 배열이면 현재 계획의 완료한 호출 결과를 연결하고 해당 호출이 없으면 업무 요청을 사용한다.
명시한 출처는 해당 업무에서 실제 제공받은 기록·현재 승인·접근·기한을 확인한다.

MemoryCandidate는 `draft:MemoryDraft`, 1~100개의 고유한 `source_refs:RecordRef[]`와 정정 시 선택 `memory_ref`다.
호스트는 재사용할 규칙·구조·절차·교훈을 업무 완료 보고에 함께 선별한다.
runtime은 후보의 검증·정본 저장·선택한 임베딩·색인을 처리한다.

### 검증과 상태 반영

성공 보고와 파일 등록은 실제 계획 승인을 확인한다.
호스트가 목표 달성 여부를 판단해 state를 전달하고 runtime은 현재 업무의 승인·진행 중 호출·취소 상태를 확인해 종료 상태를 기록한다.
계획 전에 수행할 수 없는 업무는 호스트가 확인한 결과를 `state: failed`로 제출한다.
취소 보고는 실제 cancelled 업무에 연결한다.

runtime은 별도 evidence_refs와 파일의 source_refs를 현재 자료 조건에 대조한다.
일시중지 업무는 재개 후 제출한다.
파일·보고·업무 종료는 같은 저장 트랜잭션으로 연결한다.
기억 후보의 오류는 해당 항목의 처리 결과로 반환한다.

### 출력 data

| 필드 | 의미 |
|---|---|
| `task_id`, `task_revision`, `task_state` | 현재 업무·최종 상태 |
| `report_ref`, `artifact_refs` | 보존 보고·파일 참조 |
| `validation` | 구조·근거 검사와 호스트 완료 판단 |
| `delivery_status` | 현재 `not_confirmed`; 실제 화면 전달은 호스트가 확인 |
| `memory` | 자동 기억의 실제 처리 상태 |

validation은 `structure: passed`, `evidence: passed`, `completion: met|unmet`, `completion_assessed_by: host`, `issues`를 반환한다.
completion은 호스트가 선택한 succeeded 상태를 met, 그 밖의 상태를 unmet으로 표시한다.
보존 보고의 payload는 `content`의 원래 결과 본문과 별도 `evidence_refs`·`artifact_refs`·`task_state`·`validation`으로 구성한다.
memory.state는 `ready|pending|partial|skipped`이며 항목마다 상태·기억 참조·오류·필요한 재제출 조치를 표시한다.

후보가 없으면 skipped와 빈 항목 목록이다.
후보별 문법·출처 검증 실패는 해당 항목의 skipped 상태와 원인으로 반환하고 보고·업무 종료는 보존한다.
검증한 후보의 저장·색인 장애는 보존한 대기에서 재시도한다.
대기 저장 실패·검증 전 장애·본문 없는 과거 대기는 `next_action: resubmit`으로 후보를 포함한 새 제출을 안내한다.
대기 저장에 실패한 제출은 같은 요청 키로 재전송해도 최초 실패 상태를 반환하며 후보를 포함한 새 제출에서 대기 생성을 다시 시도한다.

## daeri_result_get

| 입력 | 타입·기본값 |
|---|---|
| `task_id`, `record_ref` | 필수 ID·RecordRef |
| `cursor` | 선택 Cursor |
| `limit_bytes` | 선택; 기본 65,536, 1~1,048,576 |

현재 제공 가능한 보존 기록·보고·파일을 읽는다.
계획은 plan_get, 기억 본문은 memory_search로 읽는다.

반환 data는 `task_id`, `task_revision`, `record_kind`, `payload`, `next_cursor`, `has_more`, `redactions`, `evidence_refs`, `integrity`, `coverage`, `managed_file`다.
coverage는 `source_complete:boolean|null`, `returned_scope:string`, `missing_scope:string[]`다.
파일의 payload는 null이고 managed_file에 `artifact_ref`, `format_id`, `media_type`, `size_bytes`, `local_path`, `expires_at`과 신규 파일의 filename을 제공한다.

문자열은 UTF-8 문자 경계, JSON은 큰 배열의 항목 경계로 나눈다.
정상 페이지 분할은 ok·has_more:true이며 큰 단일 항목은 RESULT_TRUNCATED로 한도를 안내한다.
원격 원본의 잘림은 partial, 만료는 RECORD_EXPIRED, 파일 유실·변조는 RECORD_UNAVAILABLE로 구분한다.

## 전달과 검증 경계

[DTO](../../../src/daeri/core/contracts.py)·[runtime](../../../src/daeri/application/runtime.py)·[하위 에이전트](../../../src/daeri/application/subagents.py)·[보고 검사](../../../src/daeri/application/results.py)·[기억 출처](../../../src/daeri/application/knowledge.py)·[자동 기억](../../../src/daeri/application/automatic_memory.py)이 담당한다.
공개 필드·실제 근거·파일·자동 기억은 로컬 시험으로 확인하고 실제 회사·호스트·OS 범위는 [구현 계획](../../plans/initial-implementation.md#현재-구현-단위와-검증-경계)을 따른다.
