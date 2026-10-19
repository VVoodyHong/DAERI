# 업무·결과 MCP 도구

상태: 일곱 도구의 입출력 설계 제안

이 문서는 접수·보완·일시중지·조회·재개·취소와 보고 제출·조회 도구의 필드·DTO를 정의한다.
공통 타입·응답·오류·멱등 처리는 [공통 규격](common.md), 제품 동작은 [업무 흐름](../tasks/workflow.md)을 따른다.
주체·소유권은 입력 ID 대신 검증한 호출 문맥에서 확인한다.
아래 DTO는 별도 표시가 없으면 모든 필드가 필수다.

## 공통 업무 DTO

### ContextSelection과 DesiredOutput

| 타입 | 필드 | 타입·필수 여부 | 의미 |
|---|---|---|---|
| `ContextSelection` | `profile_id` | ID, 선택 | 사용자 로컬 DAERI profile |
| `ContextSelection` | `department_id` | ID, 선택 | 부처의 등록 ID |
| `ContextSelection` | `environment_id` | ID, 선택 | 환경의 등록 ID |
| `DesiredOutput` | `format` | string, 선택 | 원하는 형식; 지원 확정이 아님 |
| `DesiredOutput` | `description` | string, 선택 | 원하는 결과·완료 조건 |

생략한 맥락은 검증한 기본값으로 해소하거나 질문으로 보완한다.
부처·환경이 선택 profile과 다르면 `INVALID_INPUT`이다.
기본 profile이 없거나 모호하면 `awaiting_input`이며 다른 profile을 임의 선택·합병하지 않는다.

DAERI profile과 리소스 제공자의 인증 설정은 구분한다.
구성·선택은 [profile 명세](../configuration/profiles.md), 최종 형식·제공 범위는 [PlanDraft](plans.md)에 따른다.

### TaskContext

실행기가 로컬 설정에서 해소한 업무 맥락이다.
해소 전에는 아래에 허용한 null을 사용한다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `profile_id` | ID 또는 null | 선택 profile |
| `profile_revision` | 1 이상 integer 또는 null | 고정한 구성 개정 |
| `profile_snapshot_ref` | RecordRef 또는 null | 사용자·연결·리소스 바인딩의 내부 스냅샷 |
| `department_id`, `environment_id` | 각각 ID 또는 null | profile 분류; 해소 전 요청 선택값 |

profile 관련 세 필드는 모두 값이 있거나 모두 null이어야 한다.
스냅샷 참조는 설정·비밀 조회 권한이나 결과 조회 대상이 아니다.
호스트에는 허용한 표시 정보·리소스 요약만 제공한다.

스냅샷 고정·현재 권한·구성 변경은 [profile 규칙](../configuration/profiles.md#업무에-고정하는-스냅샷)을 따른다.

### WaitingReason·PlanRef·CallSummary

| DTO | 필수 필드·타입 |
|---|---|
| `WaitingReason` | `kind:enum`, `message:string` |
| `PlanRef` | `plan_id:ID`, `plan_version:PlanVersion` |
| `CallSummary` | `call_id:ID`, `plan_id:ID`, `plan_version:PlanVersion`, `step_id:ID`, `call_state:enum`, `external_effect:enum` |

`WaitingReason.kind`는 `input|approval|pause|scope|reconcile|owner|storage`다.
`message`는 비밀을 제외한 사람이 읽을 설명이다.
호출 enum은 [리소스 도구](resources.md)의 `CallResult`와 같으며 `call_state=completed|blocked|failed|pending|unknown`, `external_effect=none|confirmed|unknown`이다.

### TaskSnapshot·TaskReceipt

`TaskSnapshot`은 저장 업무의 현재 요약이다.
값이 없으면 지정한 null 또는 빈 배열을 사용한다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `task_id` | ID | 접수 업무 |
| `task_revision` | TaskRevision | 현재 개정 |
| `request_ref` | RecordRef | 비밀을 제외한 최초 요청 |
| `state` | TaskState | 현재 업무 상태 |
| `context` | TaskContext | 고정한 구성·부처·환경 |
| `waiting_reason` | WaitingReason 또는 null | 답변·확인·복구 사유 |
| `active_plan_ref` | PlanRef 또는 null | 현재 계획 |
| `last_report_ref` | RecordRef 또는 null | 마지막 보고 |
| `active_calls` | CallSummary[] | 진행 중·확인 필요 호출 |
| `uncertain_effects` | ID[] | 효과 미확인 call ID |
| `execution_owner_status` | enum | `self\|other\|unassigned\|recovery_required` |

`TaskState`는 `planning|awaiting_input|awaiting_approval|paused|running|reconciling|verifying|succeeded|partial|failed|cancelled`다.
상태 전이는 업무 흐름 정본을 따른다.
내부 프로세스·인증 정보는 요약에 노출하지 않는다.

`TaskReceipt`는 `task_id`, `task_revision`, `request_ref`, `state`, `context`, `waiting_reason`을 갖는다.
타입은 TaskSnapshot과 같고 수정으로 답변 기록이 생겼을 때만 선택 필드 `clarification_ref:RecordRef`를 추가한다.

## daeri_task_create

자연어 요청을 기록하고 업무 ID를 만든다.
최초 실행 소유자는 검증한 접수 연결로 지정한다.
리소스 조회·계획 생성·승인을 함께 수행하지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 접수 재전달 키 |
| `request_text` | 비어 있지 않은 string | 예 | 보존 전 비밀 제거 |
| `context` | ContextSelection | 아니요 | 기본값 확인 또는 질문 |
| `desired_output` | DesiredOutput | 아니요 | 형식·완료 조건 요청 |

`data`는 TaskReceipt다.
맥락이 충분하면 `planning`, 부족하면 `awaiting_input`으로 접수하며 둘 다 `status=ok`다.
비밀을 안전하게 분리할 수 없으면 입력 보완을 요청한다.

profile 해소는 로컬 설정만 읽는다.
원격 로그인·연결 진단·설치를 시작하지 않으며 접수 성공은 리소스 접근 승인이 아니다.
기본 profile이 바뀌어도 동일 요청 재전달은 최초 맥락을 유지한다.

## daeri_task_update

질문 대기, 답변·원하는 결과·맥락 보완 또는 사용자 요청의 일시중지를 기록한다.
최초 요청·계획·승인·상태를 입력값으로 덮어쓰지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 수정 재전달 키 |
| `task_id` | ID | 예 | 접근 가능한 업무 |
| `expected_task_revision` | TaskRevision | 예 | 읽은 개정 |
| `patch` | TaskPatch | 조건부 | 허용 필드 최소 하나; `action`과 함께 사용 불가 |
| `action` | enum `pause` | 조건부 | 업무 일시중지; `patch`와 함께 사용 불가 |

`patch`와 `action` 중 정확히 하나를 지정한다.
아래 보완 규칙은 `patch`에 적용하고 일시중지는 별도 분기를 따른다.

| TaskPatch 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `question` | 비어 있지 않은 string | 아니요 | 다른 patch 필드와 함께 사용 불가 |
| `clarification` | 비어 있지 않은 string | 아니요 | 답변을 별도 기록으로 보존 |
| `context` | ContextSelection | 아니요 | 아래 구성 변경 조건 적용 |
| `desired_output` | DesiredOutput | 아니요 | 결과 요청 보완 |

답변·맥락·결과 보완은 `planning|awaiting_input`에서만 허용한다.
다른 상태·종료 업무는 `TASK_STATE_INVALID`이며 자료의 승인 문구를 실제 승인으로 취급하지 않는다.
기존 계획의 방법·범위를 자동 바꾸거나 실행 예산을 초기화하지 않는다.

`context`는 승인 이력과 외부 호출이 **한 번도 없는 업무**에서만 변경할 수 있다.
같은 `profile_id`를 명시해도 현재 구성으로 새 스냅샷을 해소한다.
미승인 계획·확인 요청을 대체하고 개정을 증가시키며 새 계획을 작성한다.

질문만 기록하는 patch는 `planning|running|reconciling|verifying|awaiting_input`에서 허용한다.
실행 중에는 현재 소유자가 처리하며 다른 유효 소유자가 있으면 `OWNER_CONFLICT`다.
질문 대기 동안 신규 리소스 호출은 차단하고 이미 보낸 호출의 결과는 보존한다.

질문을 `waiting_reason.message`, 종류를 `input`으로 저장하고 복귀 단계를 연결한 뒤 `awaiting_input`으로 전이한다.
반복 질문은 원래 복귀 단계를 `awaiting_input`으로 덮어쓰지 않는다.
답변 뒤 현재 계획의 유효 승인이 있어야 `reconciling` 또는 저장한 실행 단계로 돌아간다.
승인이 없으면 계획 검토·보완이 필요한 `planning` 또는 재승인이 필요한 `awaiting_approval`로 연결한다.

방법·대상·제공 범위를 바꿔야 하면 `planning`에서 수정 계획을 작성한다.
다음 호출은 현재 승인·권한·입력·예산을 다시 검사한다.
`data`는 TaskReceipt이며 답변 기록이 있을 때만 `clarification_ref`를 포함한다.

### 일시중지 분기

`action="pause"`는 현재 주체의 업무 중단 권한으로 처리하며 다른 실행 소유자가 있어도 중단할 수 있다.
종료 업무는 `TASK_STATE_INVALID`이며 이미 `paused`이면 새 개정 없이 현재 기록을 반환한다.
업무의 `paused` 전이·개정 증가, 기존 승인 철회와 대기 확인 요청 무효화를 함께 보존한다.
`waiting_reason.kind=pause`로 두고 원 대기 사유·미답변 질문과 복귀에 필요한 상태를 보존한다.

dispatch 직전 상태를 재검사하며 늦은 결과·정산·자동 보고는 `paused`를 해제하지 못한다.
보존·복구 원칙은 [일시중지 흐름](../tasks/workflow.md#중단재개와-외부-결과-확인)을 따른다.
성공은 `status=ok`, `data`는 현재 TaskReceipt다.

## daeri_task_get

profile의 최근 업무 목록 또는 한 업무의 저장 상태·계획·호출·보고 참조를 읽는다.
원격 상태 확인이나 미완료 실행을 자동 수행하지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | ID | 조건부 | 단건 조회할 업무; `profile_id`와 함께 사용 불가 |
| `profile_id` | ID | 조건부 | 최근 목록의 profile; `task_id`와 함께 사용 불가 |
| `cursor` | Cursor | 아니요 | 동일 조회 종류·대상·권한의 목록 커서 |
| `limit` | PositiveInt | 아니요 | 생략 시 설정 기본값 |

두 ID 중 정확히 하나를 지정한다.
`task_id` 조회의 data는 다음과 같다.

| data 필드 | 타입 |
|---|---|
| `task_id`, `task_revision` | ID, TaskRevision |
| `task` | TaskSnapshot |
| `records` | RecordSummary[] |
| `next_cursor` | Cursor 또는 null |
| `has_more` | boolean |

`RecordSummary`의 필수 필드는 `record_ref:RecordRef`, `record_kind:enum`, `summary:string`이다.
`record_kind`는 `request|clarification|local_check|plan|approval|call_result|report|artifact`다.
요약에는 허용 메타데이터만 담고 원문은 해당 계획·결과 조회 도구로 읽는다.

`local_check`는 실행기가 해당 업무에서 실제 수행한 준비·입력·계획 검사의 기록이다.
모델의 설명으로 만들지 않으며 `daeri_task_get`으로 참조를 찾고 `daeri_result_get`으로 읽는다.
실패 응답·검사 기록을 원 요청 키에 연결하고 같은 실패 요청의 재전달로 중복 기록을 만들지 않는다.

| LocalCheck 필드 | 타입 | 제약 |
|---|---|---|
| `check_kind` | enum | `preparation\|input\|plan` |
| `outcome` | enum | `passed\|failed` |
| `error_code` | string 또는 null | 실패이면 공통 오류 코드, 성공이면 null |
| `message` | string | 비밀을 제외한 실제 검사 결과 |

`profile_id` 조회의 data는 다음과 같다.

| data 필드 | 타입 |
|---|---|
| `profile_id` | ID |
| `tasks` | RecentTaskSummary[] |
| `next_cursor` | Cursor 또는 null |
| `has_more` | boolean |

`RecentTaskSummary`는 `task_id:ID`, `title:string`, `state:TaskState`, `last_activity_at:string`을 갖는다.
시각은 시간대를 포함한 ISO 8601 값이며 마지막 활동 내림차순·같은 시각이면 업무 ID 오름차순으로 조회한다.
등록된 profile과 업무에 고정한 profile ID를 대조하고 현재 주체의 profile·업무별 열람·제공 권한을 매 페이지 검사한다.
제목은 비밀을 제거한 요청 요약이며 원문·리소스 결과를 목록에 포함하지 않는다.
조회 가능한 업무가 없으면 `tasks: []`이며 허용하지 않은 업무의 존재·제목을 노출하지 않는다.

목록 선택 뒤 단건 상태를 읽고 재개 도구의 검사를 거친다.
조회·선택은 소유권·승인·개정·예산을 바꾸지 않으며 종료 업무는 재실행하지 않는다.
정상 페이지 분할의 `has_more=true`는 `status=ok`다.

## daeri_task_resume

재연결 후 소유권과 저장 상태를 확인해 재개를 준비한다.
도구 자체가 외부 상태 확인·재시도·승인을 수행하지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 재개 재전달 키 |
| `task_id` | ID | 예 | 이어받을 권한이 있는 업무 |
| `expected_task_revision` | TaskRevision | 예 | 읽은 개정 |

| data 필드 | 타입 |
|---|---|
| `task_id`, `task_revision` | ID, TaskRevision |
| `state` | TaskState |
| `resumed` | boolean |
| `waiting_reason` | WaitingReason 또는 null |
| `active_plan_ref` | PlanRef 또는 null |
| `unresolved_call_ids` | ID[] |

현재 계획·승인·권한·예산과 소유권 회수 가능 여부를 확인한다.
다른 유효 소유자가 있으면 `OWNER_CONFLICT`이며 강제 탈취 인수를 받지 않는다.
저장 profile을 사용하고 현재 기본값·다른 인증 주체로 대체하지 않는다.

`paused`에서의 재개는 보존한 상태·대기 이유·현재 계획을 확인한 뒤에도 `resumed=false`, `status=pending`이다.
미답변 질문은 `awaiting_input`, 계획 작성·수정이 필요하면 `planning`, 현재 계획을 재확인할 수 있으면 `awaiting_approval`로 연결한다.
미답변 질문의 복귀 단계는 계획 검토로 연결하며 답변만으로 실행 상태로 돌아가지 않는다.
이 분기는 일반 재개보다 우선하며 같은 계획이라도 새 실제 승인 전에는 `reconciling`으로 전이하지 않는다.

대기 조건이 남으면 `status=pending`, `resumed=false`다.
현재 계획의 유효 승인이 없으면 승인 대기·계획 보완 상태를 유지한다.
유효 승인이 있고 진행 중 호출의 완료 기록이 없으면 재실행 대신 `reconciling`을 우선한다.
승인 만료·철회 뒤에는 확인 범위·예산을 새 reconcile 계획으로 승인받을 수 있다.

`resumed=false`도 유효 승인·현재 검사 안의 reconcile 호출은 가능하며 정상 업무 재개나 효과 해소를 뜻하지 않는다.
효과·소비·예약 승계와 종료 업무의 후속 확인은 [복구 원칙](../tasks/workflow.md#중단재개와-외부-결과-확인)을 따른다.

## daeri_task_cancel

권한이 있는 업무의 새 실행을 멈추고 취소를 보존한다.
원격 rollback·삭제·작업 취소는 함께 수행하지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 취소 재전달 키 |
| `task_id` | ID | 예 | 중단할 권한이 있는 업무 |
| `expected_task_revision` | TaskRevision | 예 | 읽은 개정 |
| `reason` | string | 아니요 | 비밀을 제외한 이유 |

| data 필드 | 타입 |
|---|---|
| `task_id`, `task_revision` | ID, TaskRevision |
| `state` | enum `cancelled` |
| `active_calls` | CallSummary[] |
| `uncertain_effects` | ID[] |

정상 보존은 `status=ok`이며 일시중지 업무도 취소할 수 있다.
이미 취소된 업무는 현재 기록을 반환하고 다른 종료 상태를 취소로 덮어쓰지 않는다.
신규 호출 차단·결과 보존·후속 확인은 [업무 취소 원칙](../tasks/workflow.md#중단재개와-외부-결과-확인)을 따른다.

## 결과 DTO

결과 도구는 원격 리소스를 조회·변경하지 않는다.
추가 검증은 먼저 승인된 리소스 호출로 근거를 확보한다.
이 절의 string은 비어 있지 않아야 하며 자료 크기는 공통 설정 상한을 적용한다.

### ReportDraft·Finding

| ReportDraft 필드 | 타입 | 제약 |
|---|---|---|
| `summary` | string | 요청 해석·수행 결과 요약 |
| `proposed_task_state` | enum | `succeeded\|partial\|failed\|cancelled` |
| `findings` | Finding[] | 관찰·파생·가설 구분 |
| `checks` | ReportCheck[] | 계획의 모든 완료 조건을 한 번씩 포함 |
| `limitations` | string[] | 누락·잘림·실패·마스킹의 영향 |
| `uncertain_effects` | ID[] | 효과 미확인 call ID |
| `artifacts` | ArtifactDraft[] | 관리 자료 생성·첨부 |
| `next_actions` | string[] | 후속 판단 설명; 새 실행 명령·승인이 아님 |

| Finding 필드 | 타입 | 제약 |
|---|---|---|
| `kind` | enum | `observation\|derived\|hypothesis` |
| `text` | string | 관찰·계산 방법·결과 또는 미확인 가설 |
| `evidence_refs` | RecordRef[] | 관찰·파생 결과는 최소 하나 |

관찰·파생·가설의 의미는 [근거 원칙](../tasks/workflow.md#이력과-완료-근거)을 따른다.
가설은 근거 배열이 비어 있을 수 있지만 검증 근거를 대신하지 않는다.

### ReportCheck

| 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `criterion_id` | ID | 예 | 저장 완료 조건 ID; 중복 금지 |
| `status` | enum | 예 | `met\|unmet\|unknown\|not_applicable` |
| `method` | string | 예 | 실제 확인 방법·범위 |
| `evidence_refs` | RecordRef[] | 예 | 사용한 등록 근거 |
| `reason` | string | 조건부 | `unmet\|unknown\|not_applicable`이면 필수 |

`met`는 필요한 근거와 보존한 검증 결과가 있어야 한다.
`not_applicable`은 `required=false`이거나 승인 분기가 false임을 등록 관찰로 확인했을 때만 허용한다.
이유만 적어 필수 조건을 면제하거나 다른 완료 조건과 모순되면 검증 실패다.

### ArtifactDraft

아래 표는 형식 변환·기존 첨부 규격이며 [확정된 파생 표·CSV 요구](../tasks/workflow.md#결과-제공과-전달)를 아직 표현하지 못한다.
**3단계 승인·공통 실행·보고 착수 전**, 개발 담당자가 다음 계약을 이 절에 확정하고 로컬 시험 자료로 검증한다.

- 기존 `daeri_result_submit` 안에서 복수 원본 참조·필드 출처·결합/계산 방법·구조화된 표·검증 기록을 연결할 입력.
- 실행기가 계산·원본 대응을 검증하는 로컬 방식과 파생 열의 제공·마스킹·보존기한 판정.
- 정상 계산과 오류·누락·중복·잘림·범위 밖 제공을 구분하는 허용·거부 사례.

이 게이트는 앞선 호환성·저장 기반 작업과 구분하며 단일 원본 변환 통과로 대신하지 않는다.
사용자 추가 결정을 기다리는 항목이 아니며 임의 코드 실행·범용 DSL·새 공개 도구는 도입하지 않는다.

| 형태 | 필수 필드·타입 | 제약 |
|---|---|---|
| 생성 | `kind="generated"`, `source_ref:RecordRef`, `format:enum`, `label:string` | 허용 보존 자료의 형식 변환 |
| 기존 첨부 | `kind="existing"`, `artifact_ref:RecordRef`, `label:string` | 해당 업무의 보존 산출물 |

`format`은 `text|json|csv`를 제안하며 CSV는 표로 변환 가능한 자료에만 허용한다.
`label`은 표시 이름으로 파일 경로·저장 위치를 지정하지 않는다.
임의 경로·URI·템플릿 코드·shell 명령을 통한 읽기·쓰기는 허용하지 않는다.

생성·첨부에는 [제공·보존 정책](../tasks/workflow.md#결과-제공과-전달)을 적용한다.
원문 HTML·수식·스크립트는 실행하지 않는다.

## daeri_result_submit

보고 초안을 실제 기록·완료 조건·제공 범위와 대조해 보존한다.
모델의 완료 주장을 그대로 업무 상태에 복사하지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 보고 재전달 키 |
| `task_id` | ID | 예 | 보고 저장 권한이 있는 업무 |
| `expected_task_revision` | TaskRevision | 예 | 중복 검사 뒤 개정 비교 |
| `plan_id` | ID 또는 null | 예 | 현재 불변 계획 |
| `plan_version` | PlanVersion 또는 null | 예 | 저장 버전 일치 |
| `report` | ReportDraft | 예 | 위 결과 DTO |

새 보고의 계획은 현재 `active_plan_ref`와 일치해야 한다.
과거 계획으로 현재 업무를 마감하지 않으며 이전 근거는 `carried_evidence_refs`로 명시적으로 승계한다.
원 계획·승인 연결과 현재 제공 범위를 유지한다.

저장 계획·승인·외부 호출 이력이 전혀 없는 업무의 `failed|cancelled` 보고에서 두 계획 필드를 함께 null로 사용할 수 있다.
`checks`, `artifacts`, `uncertain_effects`는 빈 배열이며 허용된 요청·보완 자료와 실제 준비·검사 실패 또는 취소 이유만 보존한다.
`failed`는 실제 실패한 `local_check`를 관찰 근거로 연결하며 외부 실행·검증을 수행했다고 표현하지 않는다.
실행기는 업무·기록 권한 확인 뒤 상태 변경 요청의 준비·입력·계획 검사 실패를 이 로컬 기록으로 보존한다.
`cancelled`는 이미 취소된 업무에서만 허용하며 기존 계획 이력이 있으면 현재 계획을 참조한다.
계획 전 보고의 `validation.completion`은 `unknown`으로 두고 빈 완료 조건을 충족으로 해석하지 않는다.

### 검증과 상태 반영

`paused`에서는 새 보고 제출·산출물 생성을 `TASK_STATE_INVALID`로 거부한다.
기존 요청의 재전달·기록 열람과 이미 시작된 호출의 기록 보존은 현재 제공 권한 안에서 처리한다.

종료 업무의 추가 보고·첨부도 최초 종료 시각에 고정한 보존기한을 적용한다.
만료 후 새 보고 본문·산출물 보존은 `RECORD_EXPIRED`로 거부하며 새 요청 ID로 기한을 초기화하지 않는다.

실행기는 입력·업무·계획·참조 소유, 완료 조건과 호출·근거·제공 범위를 검사한다.
리소스 없는 분석도 등록 요청 자료와 해석·계산 근거를 사용하지만 외부 실행 증거는 아니다.
자연어 의미 전체를 자동 증명한다고 보장하지 않는다.

초안과 실제 기록이 모순되거나 필수 근거가 부족하면 `RESULT_VALIDATION_FAILED`다.
`uncertain_effects=[]`가 실제 미확인 호출을 해소하지 않는다.
실제 불확실성을 빠뜨리거나 다른 업무의 call ID를 넣으면 검증에 실패한다.
거부 시 상태를 유지하고 성공 보고·유효 산출물 참조를 발급하지 않는다.

진행 중 호출·미확인 단계·미해소 외부 확인 대상이 있거나 현재 계획의 검증된 승인 이력이 없으면 `succeeded`를 거부한다.
새 계획 제출만으로 근거 승계·제공 범위 확대를 허용하지 않는다.
다른 업무 확인은 신규 확인 결과를 근거로 쓰고 원 업무·호출·승인 연결을 유지한다.

불확실성과 발생한 변경을 포함한 `partial|failed` 보고는 상태 조건에 맞춰 저장할 수 있다.
`cancelled` 주장은 이미 취소된 업무에서만 허용한다.
종료 업무의 새 보고는 종료 상태를 바꾸지 않으며 취소 자체는 취소 도구로 처리한다.

과거 승인 연결·현재 제공 권한·[보고 보존 원칙](../tasks/workflow.md#이력과-완료-근거)을 검사하며 추가 원격 조회를 숨겨 실행하지 않는다.

검증 통과 시 보고·산출물 참조·업무 상태·요청 기록을 원자적으로 보존하고 개정을 한 번 증가시킨다.
보존 실패는 성공 마감이나 리소스 재실행으로 처리하지 않는다.
중복은 공통 멱등 규칙에 따라 새 개정·파일 생성을 만들지 않는다.

### 출력 data

| 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | 보존 업무·개정 |
| `report_ref` | RecordRef | 보고 참조 |
| `artifact_refs` | RecordRef[] | 생성·첨부 산출물 |
| `task_state` | enum | 실제 `succeeded\|partial\|failed\|cancelled` |
| `validation` | ReportValidation | 구조·근거·조건 결과 |
| `delivery_status` | enum | `not_confirmed\|delivered\|failed`; 기본 `not_confirmed` |

| ReportValidation 필드 | 타입 | 의미 |
|---|---|---|
| `structure` | enum `passed` | 구조 검사 통과 |
| `evidence` | enum `passed` | 보존에 필요한 근거 구조 통과 |
| `completion` | enum `met\|unmet\|unknown` | 필수 조건 충족 여부 |
| `checks` | ReportCheck[] | 실행기가 대조한 조건 결과 |
| `issues` | string[] | 미충족·미확인·제한 |

`evidence=passed`는 전체 업무 성공을 뜻하지 않는다.
업무가 `partial|failed|cancelled`여도 보고를 정상 보존하면 도구는 `status=ok`다.

## daeri_result_get

보존 요청·답변·호출 결과·보고·산출물에서 현재 허용 자료를 읽는다.
참조는 임의 URL·파일·명령이 아니며 새 원격 조회·실행 승인으로 사용하지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | ID | 예 | 현재 열람 가능한 업무 |
| `record_ref` | RecordRef | 예 | 해당 업무의 등록 기록 |
| `cursor` | Cursor | 아니요 | 이전 응답의 연속 조회 참조 |
| `limit_bytes` | PositiveInt | 아니요 | 설정 상한 이내; 생략 시 기본값 |

등록 기록의 종류·소유와 [현재 제공·보존 조건](../tasks/workflow.md#이력과-완료-근거)을 매 페이지 검사한다.
삭제·만료 사유와 남은 허용 근거 참조를 안내하며 본문을 재구성하거나 원격 재조회하지 않는다.
만료한 본문·산출물은 `RECORD_EXPIRED`이고 추적 참조·검증 이력은 별도로 조회할 수 있다.
계획 전 요청·검사 기록도 허용 범위에서 읽으며 계획·승인 본문은 `daeri_plan_get`을 사용한다.
조회는 과거 실행 권한·개정을 바꾸지 않으며 마스킹 설명·참조로 제외 자료를 우회 노출하지 않는다.

| data 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | 조회 업무·개정 |
| `record_kind` | enum | `request\|clarification\|local_check\|call_result\|report\|artifact` |
| `payload` | JsonValue | 허용 JSON·텍스트·표; 없으면 null |
| `next_cursor` | Cursor 또는 null | 후속 조회 참조 |
| `has_more` | boolean | 허용 보존 자료의 후속 조각 |
| `redactions` | string[] | 비밀 없는 마스킹·제외 설명 |
| `evidence_refs` | RecordRef[] | 현재 제공 가능한 근거 참조 |
| `integrity` | enum | `intact\|incomplete\|unavailable` |
| `coverage` | object | 아래 필수 필드 |

`coverage`는 `source_complete:boolean|null`, `returned_scope:string`, `missing_scope:string[]`를 갖는다.
원본 완전성을 판단할 수 없으면 `source_complete=null`이다.
표 payload는 열·행을 가진 JSON object, 텍스트는 string이다.

`limit_bytes`는 허용 payload의 직렬화 바이트 상한이다.
JSON·문자를 깨뜨리는 조각은 반환하지 않으며 커서는 기록·위치·제공 조건에 바인딩한다.
만료·변조·다른 기록 커서는 `CURSOR_INVALID`다.

정상 페이지 분할은 `has_more=true`, `status=ok`다.
원본 부족·손실은 유용한 부분과 원인을 `partial`로, 자료가 전혀 없으면 `error`, `payload=null`로 반환한다.
원격 조회 누락·잘림은 `RESULT_TRUNCATED`, 보존 손실은 `RECORD_UNAVAILABLE`로 구분한다.

업무가 `partial`이어도 온전한 보존 보고 조회는 도구 실패가 아니다.
부분·오류의 응답 형식과 `isError`는 공통 규격을 따른다.

## 전달과 구현 전 확인

호스트 전달 증거의 한계와 재전달은 [결과 전달 원칙](../tasks/workflow.md#결과-제공과-전달)을 따른다.
저장 표현·변환·마스킹 상한·소유권 회수는 [구현 게이트](../../plans/initial-implementation.md)에서 정한다.
실제 MCP·로컬 저장·근거 대조·권한 차단·상태 전이는 아직 구현·검증하지 않았다.
