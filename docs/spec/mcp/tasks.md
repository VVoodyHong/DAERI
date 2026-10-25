# 업무·worker·기억·결과 MCP 도구

상태: 업무·worker·기억·결과 도구 구현; 로컬 계약·MCP 통합 검증

이 문서는 접수·보완·일시중지·조회·재개·취소, worker 위임과 보고 제출·조회 도구의 필드·DTO를 정의한다.
기억 검색·갱신도 이 문서에서 정의하며 동작·보존 기준은 [기억 명세](../tasks/memory.md)를 따른다.
공통 타입·응답·오류·멱등 처리는 [공통 규격](common.md), 제품 동작은 [업무 흐름](../tasks/workflow.md)을 따른다.
root의 주체·소유권은 검증한 호출 문맥에서, worker의 제한된 호출 자격은 위임 토큰에서 확인한다.
아래 DTO는 별도 표시가 없으면 모든 필드가 필수다.

## 공통 업무 DTO

### ContextSelection과 DesiredOutput

| 타입 | 필드 | 타입·필수 여부 | 의미 |
|---|---|---|---|
| `ContextSelection` | `profile_id` | ID, 선택 | 사용자 로컬 DAERI profile |
| `ContextSelection` | `department_id` | ID, 선택 | 부처의 등록 ID |
| `ContextSelection` | `environment_id` | ID, 선택 | 환경의 등록 ID |
| `DesiredOutput` | `format` | string, 선택 | 원하는 표현·파일 형식; 호스트가 실제 도구와 계획으로 해소 |
| `DesiredOutput` | `description` | string, 선택 | 원하는 결과·완료 조건 |

호스트는 [profile 선택 계약](../configuration/profiles.md#선택과-기본값)에 따라 확인한 profile을 전달하고 생략한 맥락은 질문으로 보완한다.
첫 버전은 `profile_id`로 맥락을 선택한다.
`department_id`·`environment_id`는 확장용 필드이며 값을 전달하면 `INVALID_INPUT`이다.
profile 선택을 생략하면 `awaiting_input`에서 사용자 선택을 확인하고 등록되지 않은 ID는 `NOT_FOUND`로 반환한다.

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
| `department_id`, `environment_id` | 각각 ID 또는 null | 확장용 분류; 현재는 null |

profile 관련 세 필드는 모두 값이 있거나 모두 null이어야 한다.
스냅샷 참조는 업무와 구성 바인딩을 연결하는 내부 식별자다.
호스트에는 현재 권한으로 허용한 표시 정보·리소스 요약을 제공한다.

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
| `workers` | WorkerSummary[] | 같은 업무의 위임 메타데이터; 없으면 [] |
| `artifact_workspace` | string | 호스트가 파일을 작성할 업무 전용 경로 |

`TaskState`는 `planning|awaiting_input|awaiting_approval|paused|running|reconciling|verifying|succeeded|partial|failed|cancelled`다.
상태 전이는 업무 흐름 정본을 따른다.
요약의 공개 범위는 위 업무 메타데이터다.
현재 실행기는 승인 후 `running`에서 조회·확인을 수행하며 `verifying`·`reconciling` 전이는 후속 실행 확장에 사용한다.

`TaskReceipt`는 `task_id`, `task_revision`, `request_ref`, `state`, `context`, `waiting_reason`, `artifact_workspace`를 갖는다.
타입은 TaskSnapshot과 같고 수정으로 질문·답변·맥락 보완 기록을 만들었을 때 선택 필드 `clarification_ref:RecordRef`를 추가한다.
접수·수정·상세 조회와 해당 요청의 재전달에서 같은 업무의 작업 경로를 제공한다.

`WorkerSummary`는 `delegation_id:ID`, `state:active|completed|revoked`, `step_ids:ID[]`, `plan_ref:PlanRef`, `expires_at:string`, `assignment_ref:RecordRef`, `result_ref:RecordRef|null`을 갖는다.
위임 기한은 시간대를 포함한 ISO 8601 값이며 할당·결과 본문은 등록 참조로 조회한다.
위임 상태와 기한의 실행 의미는 [worker 수명](#멱등재전달과-수명)을 따른다.

## daeri_task_create

자연어 요청을 기록하고 업무 ID를 만든다.
최초 실행 소유자는 검증한 접수 연결로 지정한다.
접수 후 기억 조회·계획 작성·사용자 승인·실행 순서로 진행한다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 접수 재전달 키 |
| `request_text` | 비어 있지 않은 string | 예 | 보존 전 비밀 제거 |
| `context` | ContextSelection | 아니요 | 확인한 profile 전달 또는 질문 |
| `desired_output` | DesiredOutput | 아니요 | 형식·완료 조건 요청 |

`data`는 TaskReceipt다.
맥락이 충분하면 `planning`, 부족하면 `awaiting_input`으로 접수하며 둘 다 `status=ok`다.
비밀을 안전하게 분리할 수 없으면 입력 보완을 요청한다.

profile 해소는 로컬 설정만 읽는다.
원격 로그인·연결 진단·설치는 관리 경로에서 준비하고 실제 리소스 접근은 현재 계획의 승인을 거친다.
대화의 기본 profile이 바뀌어도 동일 요청 재전달은 최초 맥락을 유지한다.

## daeri_task_update

질문 대기, 답변·원하는 결과·맥락 보완 또는 사용자 요청의 일시중지를 기록한다.
보완 자료는 최초 요청에 연결한 새 기록으로 보존하고 계획·승인·상태는 각 전이 규칙으로 관리한다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 수정 재전달 키 |
| `task_id` | ID | 예 | 접근 가능한 업무 |
| `expected_task_revision` | TaskRevision | 예 | 읽은 개정 |
| `patch` | TaskPatch | 조건부 | 허용 필드 최소 하나; 입력 분기는 `patch` 또는 `action` |
| `action` | enum `pause` | 조건부 | 업무 일시중지; 입력 분기는 `action` 또는 `patch` |

`patch`와 `action` 중 정확히 하나를 지정한다.
아래 보완 규칙은 `patch`에 적용하고 일시중지는 별도 분기를 따른다.

| TaskPatch 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `question` | 비어 있지 않은 string | 아니요 | 질문 하나로 구성하는 단독 patch |
| `clarification` | 비어 있지 않은 string | 아니요 | 답변을 별도 기록으로 보존 |
| `context` | ContextSelection | 아니요 | 아래 구성 변경 조건 적용 |
| `desired_output` | DesiredOutput | 아니요 | 결과 요청 보완 |

답변·맥락·결과 보완은 `planning|awaiting_input`에서만 허용한다.
다른 상태·종료 업무는 `TASK_STATE_INVALID`로 반환한다.
실제 승인은 승인 응답 도구로, 방법·범위 변경은 수정 계획으로 처리하며 누적 예산을 유지한다.

`context`는 승인 이력과 외부 호출이 **한 번도 없는 업무**에서만 변경할 수 있다.
같은 `profile_id`를 명시해도 현재 구성으로 새 스냅샷을 해소한다.
미승인 계획·확인 요청을 대체하고 개정을 증가시키며 새 계획을 작성한다.

질문만 기록하는 patch는 `planning|running|reconciling|verifying|awaiting_input`에서 허용한다.
실행 중에는 현재 소유자가 처리하며 다른 유효 소유자가 있으면 `OWNER_CONFLICT`다.
질문 대기 동안 신규 리소스 호출은 차단하고 이미 보낸 호출의 결과는 보존한다.

질문을 `waiting_reason.message`, 종류를 `input`으로 저장하고 `awaiting_input`으로 전이한다.
반복 질문은 현재 대기 질문을 새 질문으로 바꾸고 입력 대기를 유지한다.
답변·맥락·결과 보완을 보존하면 요청 맥락 개정을 증가시킨다.
profile이 해소돼 있으면 `planning`, 미해소이면 `awaiting_input`으로 연결한다.
변경한 요청 맥락은 새 기억 검색과 수정 계획으로 검토하고 실행 범위를 다시 승인받는다.

방법·대상·제공 범위를 바꿔야 하면 `planning`에서 수정 계획을 작성한다.
다음 호출은 현재 승인·권한·입력·예산을 다시 검사한다.
`data`는 TaskReceipt이며 새 질문·보완 기록의 `clarification_ref`를 포함한다.

### 일시중지 분기

`action="pause"`는 현재 주체의 업무 중단 권한으로 처리하며 다른 실행 소유자가 있어도 중단할 수 있다.
종료 업무는 `TASK_STATE_INVALID`이며 이미 `paused`이면 새 개정 없이 현재 기록을 반환한다.
업무의 `paused` 전이·개정 증가, 기존 승인 철회와 대기 확인 요청 무효화를 함께 보존한다.
`waiting_reason.kind=pause`로 두고 원 대기 사유·미답변 질문과 복귀에 필요한 상태를 보존한다.

dispatch 직전에 상태를 재검사한다.
늦은 호출 결과·정산을 보존할 때에도 `paused`를 유지하며 재개는 재개 도구로 처리한다.
보존·복구 원칙은 [일시중지 흐름](../tasks/workflow.md#중단재개와-외부-결과-확인)을 따른다.
성공은 `status=ok`, `data`는 현재 TaskReceipt다.

## daeri_task_get

profile의 최근 업무 목록 또는 한 업무의 저장 상태·계획·호출·보고 참조를 읽는다.
조회 대상은 로컬 보존 상태이며 원격 상태 확인은 승인된 리소스 호출로 처리한다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | ID | 조건부 | 단건 조회 분기의 업무 ID |
| `profile_id` | ID | 조건부 | 최근 목록 조회 분기의 profile ID |
| `cursor` | Cursor | 아니요 | 동일 조회 종류·대상·권한의 목록 커서 |
| `limit` | PositiveInt | 아니요 | 기본 20, 최대 100 |

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
`record_kind`는 `request|clarification|local_check|memory_search|memory_update|worker_assignment|worker_result|plan|approval|call_result|report|table|artifact`다.
요약에는 허용 메타데이터만 담고 원문은 해당 계획·결과 조회 도구로 읽는다.

`local_check`는 실행기가 해당 업무에서 실제 수행한 준비·입력·계획 검사의 기록이다.
`daeri_task_get`으로 실제 검사 참조를 찾고 `daeri_result_get`으로 읽는다.
실패 응답·검사 기록은 원 요청 키에 연결하며 같은 요청의 재전달은 기존 기록을 반환한다.

| LocalCheck 필드 | 타입 | 제약 |
|---|---|---|
| `check_kind` | enum | `preparation\|input\|plan` |
| `outcome` | enum | `passed\|failed` |
| `error_code` | string 또는 null | 실패이면 공통 오류 코드, 성공이면 null |
| `message` | string | 비밀을 제외한 실제 검사 결과 |

현재 자동 기록 대상은 업무·요청 키가 연결된 `PLAN_INVALID` 실패이며 `check_kind=plan`, `outcome=failed`다.
준비·일반 입력 검사 기록과 성공 검사는 이 DTO를 사용하는 확장 범위다.
형식 오류처럼 업무 맥락 해소 전에 거부한 요청은 도구 오류로 확인한다.

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
목록에는 현재 열람 가능한 업무의 메타데이터와 비밀을 제거한 요청 요약 제목을 담는다.
조회 가능한 업무가 없으면 `tasks: []`다.

목록 선택 뒤 단건 상태를 읽고 재개 도구의 검사를 거친다.
조회·선택은 기존 소유권·승인·개정·예산을 유지한다.
종료 업무의 후속 작업은 아래 재개 도구의 로컬 처리 범위를 따른다.
정상 페이지 분할의 `has_more=true`는 `status=ok`다.

## daeri_task_resume

재연결 후 소유권과 저장 상태를 확인해 재개를 준비한다.
재개 준비는 로컬 상태·소유권 확인이며 외부 상태 확인·재시도·사용자 승인은 각 전용 경로로 처리한다.

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

저장 상태·소유권과 현재 계획의 요청·profile·기억 전제를 확인한다.
실제 계약·대상·인증·예산의 실행 가능 여부는 다음 기능 호출에서 검사한다.
다른 유효 소유자가 있으면 `OWNER_CONFLICT`로 반환한다.
재개할 업무에 저장된 profile·인증 주체를 사용한다.
현재 계획의 기억 참조도 [현재 유효성 검사](../tasks/memory.md#계획-전-조회와-활용)를 적용한다.
선택 기억의 유효성 검사가 실패하면 기억 재조회·수정 계획으로 연결한다.

종료 업무는 원격 실행·계획·승인 재개 없이 로컬 기억 갱신·허용 추가 보고만 준비할 수 있다.
종료 상태와 `resumed=false`를 유지하고, 다른 유효 소유자가 없을 때 기록 확인 후 소유권을 회수한다.
미확인 호출이 없으면 `status=ok`이며 소유권 변경이 있으면 업무 개정을 증가시킨다.
본문 만료·제공 권한은 그대로 적용한다.
기억 저장 결과는 `memory_update` 기록으로, 자동 처리의 대기·색인 상태는 원 보고 요청의 동일 재전달에서 확인한다.

`paused`에서의 재개는 보존한 상태·대기 이유·현재 계획을 확인한 뒤에도 `resumed=false`, `status=pending`이다.
미답변 질문은 `awaiting_input`, 계획 작성·수정이 필요하면 `planning`, 현재 계획을 재확인할 수 있으면 `awaiting_approval`로 연결한다.
미답변 질문의 답변 뒤에는 계획 검토를 거쳐 새 사용자 승인을 받는다.
이 분기는 일반 재개보다 우선하며 `running|verifying|reconciling` 진입에는 새 실제 승인이 필요하다.

대기 조건이 남으면 `status=pending`, `resumed=false`다.
현재 계획의 유효 승인이 없으면 승인 대기·계획 보완 상태를 유지한다.
완료 기록이 없는 `pending|unknown` 호출은 `unresolved_call_ids`에 담고 `status=pending`, `resumed=false`로 반환한다.
미확인 원 호출은 후속 확인 근거가 확보될 때까지 대기 상태를 유지한다.
확인된 프로세스 종료 뒤의 dispatch·예약 처리와 향후 reconcile 확장은 [복구 원칙](../tasks/workflow.md#중단재개와-외부-결과-확인)을 따른다.
현재 읽기 어댑터의 실행 목적은 `collect`·`verify`다.

worker 호출은 실제 실행을 담당한 MCP 프로세스의 내부 소유자도 기록한다.
root 소유권이 유지돼 있어도 재개 검사가 해당 실행 프로세스의 종료를 확인하면 연결된 활성 위임을 `revoked`로 바꾼다.
이미 dispatch한 호출은 `unknown`·`EXTERNAL_OUTCOME_UNKNOWN`과 미정산 예약을 유지한다.
dispatch 전 호출은 `blocked`·`OWNER_CONFLICT`로 보존하고 미사용 호출·시간 예약을 해제한다.
이는 로컬 MCP 프로세스의 생존 검사이며 호스트 모델 worker의 추론 상태는 호스트 도구로 확인한다.
종료 업무에도 이 복구를 적용해 호출·예약·위임의 변경을 영속 저장하고 원 종료 상태와 `resumed=false`를 유지한다.

## daeri_task_cancel

권한이 있는 업무의 새 실행을 멈추고 취소를 보존한다.
이 도구의 취소 대상은 로컬 업무의 후속 실행이다.
원격 rollback·삭제·원격 작업 취소는 해당 리소스 기능과 별도 승인이 필요하다.

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
이미 취소된 업무는 현재 기록을 반환하며 다른 종료 상태에는 `TASK_STATE_INVALID`를 반환한다.
신규 호출 차단·결과 보존·후속 확인은 [업무 취소 원칙](../tasks/workflow.md#중단재개와-외부-결과-확인)을 따른다.

## daeri_worker

하나의 root 업무·현재 계획·실제 사용자 승인·공유 예산 아래 worker의 제한된 수행을 연결하는 게이트웨이다.
root는 `create|revoke`로 작업을 배정·철회하고 worker는 토큰으로 `get|execute|search|read|complete`를 호출한다.
worker 생성·추론·대기는 [호스트](../hosts/plugins.md)가 담당하고 이 도구는 DAERI를 통한 위임·자료 제공·실행 조건을 검사한다.
업무 제어·계획·승인·최종 보고·기억 후보 제출은 기존 root 도구를 사용한다.

### 분기별 입력

모든 분기의 필수 필드는 `task_id:ID`와 `operation`이다.
`operation`은 아래 일곱 값 중 하나이며 해당 행의 필수·선택 필드만 함께 지정한다.
추가 필드·다른 분기의 필드·필수값 누락·명시적 null·잘못된 타입은 `INVALID_INPUT`이다.
입력 자료형은 필드에 선언한 타입과 엄격히 비교한다.

| `operation` | 추가 필수 필드 | 선택 필드·기본값 |
|---|---|---|
| `create` | `request_id`, `expected_task_revision`, `assignment` | `ttl_seconds=1800` |
| `get` | `delegation_token` | 없음 |
| `execute` | `delegation_token`, `call` | `timeout_ms=10000` |
| `search` | `delegation_token`, `query` | `instance_ids`: 배정 단계의 전체 인스턴스; `limit=10` |
| `read` | `delegation_token`, `record_ref` | `cursor`, `limit_bytes=65536` |
| `complete` | `request_id`, `delegation_token`, `result` | 없음 |
| `revoke` | `request_id`, `expected_task_revision`, `delegation_id` | 없음 |

| 입력 필드 | 타입·범위 | 의미 |
|---|---|---|
| `request_id` | ID | 생성·완료·철회의 멱등 키 |
| `expected_task_revision` | TaskRevision | 생성·철회 시 root가 확인한 현재 개정 |
| `assignment` | WorkerAssignment | 하위 목표·단계·완료 조건·명시적으로 제공할 출처 |
| `ttl_seconds` | 1~86,400 integer | 발급 시각부터 위임 기한까지의 초 |
| `delegation_id` | ID | 철회할 업무 내 위임 |
| `delegation_token` | 비어 있지 않은 string | 해당 업무의 위임을 사용할 bearer 토큰 |
| `call` | WorkerCall | 배정 단계의 한 기능 호출 |
| `timeout_ms` | 1~60,000 integer | 실행기의 호출 시간 예약 상한; 남은 업무 시간과 작은 값 적용 |
| `query` | 비어 있지 않은 string | [기억 검색](#daeri_memory_search)의 질의 크기·비밀 제거 조건 적용 |
| `instance_ids` | 중복 없는 ID[] | 배정 단계의 인스턴스 부분집합; 지정하면 하나 이상 |
| `limit` | 1~50 integer | 검색 결과 건수 |
| `record_ref` | RecordRef | 배정한 출처 또는 자기 호출의 보존 결과 |
| `cursor` | Cursor | 같은 기록의 연속 읽기 |
| `limit_bytes` | 1~1,048,576 integer | 보존 결과 읽기의 직렬화 바이트 상한 |
| `result` | WorkerResult | worker의 요약·실제 근거·제한 |

`WorkerAssignment`는 아래 필드를 갖는다.

| 필드 | 타입 | 필수 | 조건 |
|---|---|---|---|
| `label`, `objective` | 각각 비어 있지 않은 string | 예 | 작업 표시명과 해결할 목표 |
| `step_ids` | 중복 없는 ID[] | 예 | 현재 계획의 단계 1~100개 |
| `completion_criteria` | 비어 있지 않은 string[] | 예 | 하위 작업의 확인 조건 1~20개 |
| `source_refs` | 중복 없는 RecordRef[] | 아니요 | 기본 []; root가 명시적으로 제공할 현재 허용 근거 |

`WorkerCall`의 필수 필드는 `step_id:ID`, `call_id:ID`, `parameters:JsonObject`다.
선택 필드 `capability_id:ID`는 리소스 읽기 묶음 단계의 실제 도구를 선택하며 해당 단계에서는 필수다.
단일 기능 단계는 저장한 기능을 사용한다.
선택 필드 `bindings:BindingSelection[]`의 기본값은 []이며 [바인딩 계약](plans.md#결과-바인딩과-조건)을 따른다.
실행기는 배정한 단계에서 인스턴스·기능·계약·계획·승인을 해소하고 실제 인수·범위·의존 관계·허용 분기·공유 예산을 검사한다.
명시적 바인딩의 원 호출 결과도 배정한 `source_refs` 또는 해당 위임의 호출 결과여야 한다.

`WorkerResult`의 필수 필드는 `summary:string`, `evidence_refs:RecordRef[]`, `limitations:string[]`다.
`summary`와 각 제한 문장은 비어 있지 않아야 하며 `evidence_refs`는 중복 없이 지정한다.
근거·제한이 없으면 각각 []를 전달한다.
완료 기록의 `evidence_refs`는 명시적으로 배정한 `source_refs`와 해당 위임의 호출 결과만 인용할 수 있다.
검색으로 새로 찾은 기억은 판단 자료이며 완료 근거로 인용하려면 현재 계획에 선택되어 root가 제공한 출처여야 한다.

### 생성과 호출 허용 조건

`create`는 현재 root 소유자가 유효한 승인을 가진 `running|verifying` 업무에서 처리한다.
배정 단계는 현재 계획에 존재하며 각 단계는 기한이 남은 활성 위임 하나에 배정한다.
해당 단계에 `pending|unknown` 호출이 있으면 `WORKER_CONFLICT`다.
제공할 출처는 현재 계획에서 모델 제공이 허용된 `call_result` 또는 선택된 기억이며 명시적으로 승계한 조회 근거도 사용할 수 있다.
각 출처의 현재 계획·제공 정책·기한을 검사한다.
명시적으로 전달한 다른 리소스의 근거는 읽고 활용할 수 있으며 새 조회·기억 검색의 대상은 배정 단계의 인스턴스로 정한다.

토큰 호출은 해당 업무의 위임·기한·root 소유권과 프로세스 생존·현재 계획·승인·업무 상태·profile 바인딩을 확인한다.
토큰에 대응하는 위임이 없으면 `ACCESS_DENIED`, 위임 상태·기한·소유권·계획·승인·업무 상태 검사가 실패하면 `WORKER_UNAVAILABLE`이다.
profile·기능 계약·기록 본문의 검사 실패는 해당 공통 오류로 반환한다.
토큰의 소지는 해당 범위의 호출 자격이며 실제 호스트 worker의 신원·수신자 관리는 호스트의 전달 경계를 따른다.
호스트의 도구 권한과 토큰 전달은 [호스트 신뢰 경계](../../architecture/overview.md#권한과-데이터의-경계)를 따른다.

활성 위임의 새 호출은 `execute`에서만 접수하며 root의 같은 단계 직접 호출은 `WORKER_CONFLICT`다.
worker 실행은 위임·현재 업무 조건을 기준으로 같은 업무 기록의 의도·예약·dispatch·소비·결과를 원자적으로 갱신한다.
한 단계의 `pending` 호출과 중복되는 새 호출은 `WORKER_CONFLICT`다.
배정·진행·미확인 호출의 검사와 함께 모든 worker가 업무 전체의 누적 한도를 공유한다.

`search`는 배정 단계의 인스턴스와 현재 profile 기억 정책을 적용한다.
profile에 해당 기억의 제공 허용이 없으면 `ACCESS_DENIED`로 반환한다.
기억의 임베딩 전송에는 기존 전송 정책을 적용하고 검색 반환 전에 위임·현재 업무·계획·기억을 재검사한다.
검색 기록은 같은 업무에 남긴다.
`read`는 허용한 출처 또는 자기 호출 결과의 현재 제공·기한을 검사하고 [결과 조회](#daeri_result_get)의 페이지 계약을 사용한다.
기억 본문은 `search`로 제공한다.

`complete`는 해당 위임의 `pending|unknown` 호출이 없고 모든 인용 근거의 현재 제공 검사를 통과할 때 요약·근거·제한을 보존한다.
위임 상태를 `completed`로 바꾸고 root 업무 개정을 증가시키며 업무 전체의 완료 조건과 종료 상태는 유지한다.
`worker_result`는 root의 판단·추적을 위한 요약이며 root가 실제 호출 근거를 대조해 원 참조로 최종 보고와 기억 후보를 제출한다.

### 반환 `data`

`create|get|complete`는 아래 `WorkerView`를 반환한다.
`create`와 그 유효한 재전달에서만 추가 필드 `delegation_token:string`을 반환한다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | root 업무와 조회 당시 개정 |
| `delegation_id` | ID | 위임 식별자 |
| `state` | enum | `active\|completed\|revoked` |
| `expires_at` | string | 시간대를 포함한 ISO 8601 위임 기한 |
| `plan_ref` | PlanRef | 발급 당시 root의 불변 계획 |
| `assignment` | WorkerAssignment | 저장한 하위 목표·단계·완료 조건·출처 |
| `steps` | PlanStep[] | 배정된 현재 계획의 단계 |
| `contracts` | CapabilityDescriptor[] | 배정 단계의 현재 계약; 읽기 묶음과 구성 기능의 실제 입력 스키마 포함 |
| `calls` | WorkerCallSummary[] | 해당 위임의 호출만 포함 |
| `result_ref` | RecordRef 또는 null | 보존 완료 결과; 완료 전 null |
| `result` | WorkerResult | 보존 결과가 있을 때만 반환 |

`PlanStep`은 [계획 단계](plans.md), `CapabilityDescriptor`는 [기능 계약](resources.md#capabilitydescriptor)을 따른다.
같은 인스턴스·기능 계약은 한 번씩 반환한다.
worker는 제공받은 계약으로 실제 도구·인수를 선택하고 호스트에 제공된 분석 도구로 결과를 검토한다.
최종 보고·파일의 작성·검증과 등록은 총괄의 [호스트 결과 계약](#호스트-결과와-파일)으로 연결한다.
`WorkerCallSummary`의 필수 필드는 `call_id:ID`, `step_id:ID`, `call_state:enum`, `result_ref:RecordRef|null`, `error_code:string|null`이다.
호출 상태는 공통 `CallSummary`와 같다.

`execute`는 [기능 실행](resources.md#반환-data)의 `CallResult`와 동일한 `data`·상태·오류를 반환한다.
`search`는 [기억 검색](#daeri_memory_search)의 반환 필드·검색 상태·오류를 사용하며 별도 `memory_search_ref`를 발급한다.
`read`는 [결과 조회](#daeri_result_get)의 반환 필드·페이지 상태·오류를 사용한다.
할당 본문은 현재 보존기한을 검사한다.
완료 결과는 본문과 모든 인용 출처의 현재 보존·제공 조건을 함께 검사한다.
완료 저장 당시 각 출처의 허용 필드 경로를 내부 증명으로 남기고 현재 허용 범위가 그 경로를 모두 포함할 때 요약을 제공한다.
원 필드가 제거·마스킹되거나 증명을 확인할 수 없으면 `ACCESS_DENIED`이며 내부 증명은 공개 `WorkerResult`에서 제외한다.
`revoke`는 `task_id`, `task_revision`, `delegation_id`, `state`, `expires_at`, `plan_ref`, `result_ref`만 반환한다.
철회는 저장된 위임 제어 정보·현재 root 소유권·개정으로 처리한다.

### 멱등·재전달과 수명

`create|complete|revoke`의 키는 같은 업무의 `request_id`이며 정규화한 전체 입력으로 비교한다.
동일 키·입력은 현재 허용한 기존 처리 결과와 사용량을 유지하며 다른 입력은 `REQUEST_ID_CONFLICT`다.
`create|revoke`의 비교 입력에는 최초 `expected_task_revision`도 포함한다.
`execute`의 `call_id`는 root와 다른 worker의 호출을 포함해 같은 업무에서 고유하며 동일 입력 재전달은 원 호출 상태를 반환한다.
기능·인수·바인딩이 달라지면 새 `call_id`가 필요하며 충돌은 `CALL_ID_CONFLICT`다.

`get|read`는 `active|completed` 위임, `execute|search`와 새로운 `complete`는 `active` 위임에서 허용한다.
완료 요청의 동일 재전달은 `completed`도 허용하며 모든 토큰 호출에는 남은 기한과 현재 root 실행 조건을 적용한다.
생성 응답의 재전달도 현재 유효한 활성 위임을 확인한 뒤 같은 토큰을 반환한다.
일반 조회·업무 요약의 공개 필드는 `WorkerView`·`TaskSnapshot`을 따른다.
제어 정보에는 토큰의 해시를 저장하고 생성 응답 재전달용 원값은 같은 OS 사용자의 SQLite 요청 기록에 보존한다.
이 내부 기록의 접근 경계는 사용자 로컬 저장소이며 root가 해당 생성 요청으로만 토큰을 복구한다.

root의 일시중지·취소·종료·계획 교체·소유권 이전과 승인 무효화 처리는 연결된 활성 위임을 `revoked`로 기록한다.
기억 정정·만료 등으로 승인 전제가 깨진 경우도 현재 승인 검사에서 새 호출을 차단하고 재계획·재개 절차로 연결한다.
위임 기한의 경과는 새 enum 전이 없이 호출 시각의 기한 검사로 차단한다.
`revoke`는 root가 개별 위임을 철회하는 처리이며 기한·profile 연결이 바뀐 뒤에도 현재 root 소유권·개정으로 수행할 수 있다.
진행 중에 철회한 호출의 늦은 결과는 원 호출에 보존하고 root의 현재 중지·종료 상태를 유지한다.
새 계획·승인·재개 후 실행에는 현재 조건의 새 위임을 발급한다.

위임 제어 정보는 SQLite의 업무 메타데이터에서 관리한다.
할당 본문과 결과 본문은 각각 `worker_assignment`·`worker_result` 기록으로 분리해 기존 업무·기여 출처의 보존 조건을 적용한다.
본문이 만료되면 조회 오류를 반환한다.
호스트 worker 프로세스의 종료와 실행 중 호출 복구는 [업무 재개](#daeri_task_resume)의 `unknown|blocked` 처리를 따른다.

## 기억 MCP 도구

`daeri_task_get`은 정확한 업무를 선택·복구하고 `daeri_result_get`은 특정 보존 자료를 읽는다.
아래 도구의 검색·갱신 대상은 선별한 기억 정본이다.

### MemoryItem

검색 결과의 본문은 아래 타입을 사용하며 필드는 모두 필수다.
`memory_ref`는 특정 기억 개정의 불변 참조이며 실제 열람은 현재 권한·profile·대상 검사를 거친다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `memory_ref` | RecordRef | 기억 ID·개정에 연결한 불투명 참조 |
| `category` | enum | `service_rule`, `resource_structure`, `procedure`, `lesson` |
| `statement`, `applicability` | 각각 string | 허용한 지식과 적용 조건 |
| `instance_ids` | ID[] | 기여 리소스; 실제 대상 바인딩은 서버가 보존·대조 |
| `sources` | MemorySource[] | 현재 제공 가능한 출처 업무·기록; 원문 만료·제공 제한 시 [] 가능 |
| `verification_method` | string | 실제 확인 방법과 제한 |
| `verified_at` | string | 시간대를 포함한 실제 검증의 ISO 8601 시각 |
| `expires_at` | string 또는 null | 유한 기한의 ISO 8601 시각; 활성 무기한 지식은 null |
| `retention` | enum | `source_bound`, `long_term` |

검색에는 현재 사용 가능한 개정만 반환한다.
따라서 검색 결과의 `expires_at=null`은 활성 무기한 지식이며 만료 검사만 생략하고 현재 개정·권한·대상 검사는 유지한다.
`MemorySource`의 필수 필드는 `task_id:ID`, `record_ref:RecordRef`, `fields:FieldSelector[]`다.
`fields`는 실제 제공 기록에서 서버가 확정한 기여 필드다.
출처 참조가 반환돼도 원문 열람은 해당 업무의 현재 권한·기한을 별도로 검사한다.
원문이 만료된 장기 지식에는 보존한 최소 검증 설명과 원문 열람 제한을 표시한다.
`verified_at`은 실제 관찰·등록 사용자 자료의 검증 시각에서 해소한다.

### daeri_memory_search

계획 전에 현재 업무의 profile 기본 맥락·관련 기억을 조회한다.
현재 등록 정책과 저장 당시 허용한 미래 모델 제공 조건을 검사해 로컬 기억 정본을 검색한다.
회사 API 선택 시 검색 질의의 벡터 생성에는 [임베딩 전송 계약](../tasks/memory.md#임베딩-api-계약)을 추가 적용한다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | ID | 예 | profile 맥락을 해소한 업무 |
| `query` | string | 예 | 비밀 제거 후 최대 4,096 UTF-8 바이트의 검색 목적·용어 |
| `instance_ids` | ID[] | 아니요 | 하나 이상의 허용 대상 부분집합; 생략하면 현재 허용 대상 |
| `limit` | PositiveInt | 아니요 | 기본 10, 최대 50 |

업무·profile 접근 권한과 실제 대상 바인딩을 검사한 뒤 키워드·벡터 후보를 함께 검색한다.
고정 profile의 검색 권한 밖 대상을 지정하면 `ACCESS_DENIED`로 반환한다.
반환 직전 정본의 현재 개정·권한·기한·무효화와 본문 제공 조건을 다시 검사한다.

| data 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | 조회 업무·현재 개정; 기존 값 유지 |
| `memory_search_ref` | RecordRef | 실행기가 발급한 불변 검색 확인 기록 |
| `core_context` | string[] | 현재 제공 가능한 사용자 설정의 작은 기본 맥락 |
| `items` | MemoryItem[] | 허용한 관련 지식; 결과 없음은 [] |
| `retrieval_state` | enum | `hybrid`, `keyword_only`, `vector_only`, `unavailable` |
| `limitations` | string[] | 검색 실패·색인 지연·분량 제한·원문 열람 제한 |

검색 기록은 업무·profile 스냅샷·최신 요청/보완 맥락·조회 당시 현재 계획 참조·조회 조건·실제 반환 개정 참조·검색 상태를 묶는다.
업무의 상태·개정·실행 예산은 기존 값을 유지하며 로컬 연산·입력·반환량 상한을 적용한다.
일반 로그·최소 이력은 식별·검사 메타데이터로 구성하고 본문은 기억 정본과 검색 응답의 제공·보존 정책으로 관리한다.
새 계획마다 현재 요청·profile 맥락으로 수행한 검색 기록을 연결한다.
추가 검색 결과를 합쳐 쓰려면 각 검색 참조를 `PlanDraft.memory_search_refs`에 함께 연결한다.

완전한 혼합 검색과 사용자가 선택한 키워드 전용 경로의 완전한 검색은 `status=ok`이며 0건도 정상이다.
API 방식에서 제공 가능한 기억이 0건이면 전송을 생략하고 키워드 조회가 정상 완료된 `items=[]`, `retrieval_state=keyword_only`, `limitations=[]`를 `ok`로 반환한다.
제공자 연결의 준비 여부는 관리 진단으로 별도 확인한다.
한쪽 검색 장애·일부 색인 누락은 `status=partial`, `MEMORY_SEARCH_INCOMPLETE`로 검색 제한을 표시한다.
사용 가능한 쪽만 검색했다면 `keyword_only|vector_only`로 구분한다.
사용자가 키워드 전용을 선택했거나 API 선택 변경·인증·응답 오류가 발생해도 실제 검색 방식과 제한을 같은 필드로 표시한다.
검색 자체가 실패해도 업무·정책 기록을 신뢰할 수 있으면 `status=error`, `MEMORY_SEARCH_UNAVAILABLE`과 검색 실패 기록을 반환한다.
이 경우 `items=[]`, `retrieval_state=unavailable`이며 계획은 실패를 명시해야 한다.
공통 저장소 확인에 실패하면 `STORE_UNAVAILABLE`, `data=null`로 반환한다.

### daeri_memory_update

실제 근거로 지식을 저장·정정하거나 기존 개정을 무효화한다.
기본 업무의 저장은 [결과 제출의 자동 기억](#daeri_result_submit)으로 처리하고 이 도구는 정밀한 정정·무효화와 기존 연동에도 사용한다.
처리 범위는 선별 기억 정본과 검색 색인의 갱신이다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 갱신 재전달 키 |
| `task_id` | ID | 예 | 근거를 가진 현재 업무 |
| `expected_task_revision` | TaskRevision | 예 | 갱신 전 읽은 업무 개정 |
| `operation` | enum | 예 | `store`, `invalidate` |
| `memory_ref` | RecordRef | 조건부 | 정정·무효화 대상의 현재 개정; 새 저장에는 생략 |
| `draft` | MemoryDraft | 조건부 | `store`일 때만 필수 |
| `source_refs` | RecordRef[] | 예 | 현재 업무의 실제 근거; 최소 하나 |
| `reason` | string | 예 | 저장·정정·무효화의 이유 |

`MemoryDraft`는 필수 `category:enum`, `statement:string`, `applicability:string`, `instance_ids:ID[]`, `verification_method:string`을 갖는다.
범주 enum은 MemoryItem과 같고 인스턴스는 하나 이상이며 근거·계획과 일치해야 한다.
초안의 UTF-8 JSON 크기는 16KiB 이하여야 하며 초과하면 `INVALID_INPUT`이다.
만료·승인·검증 시각·profile·실제 연결 대상은 실행기가 현재 기록·정책에서 해소한다.

### 기억의 출처 필드 확정

초기에는 기록 전체를 인용하는 보수적인 규칙을 사용한다.
후보의 `source_refs`마다 그 기록으로 호스트에 실제 제공한 모든 필드의 누적 집합을 기여 출처로 삼는다.
서버는 저장한 제공·보관 경로와 현재 허용 경로를 실제 JSON 말단 값에 대조한다.
저장·내보내기 정책은 해소한 전체 기여 필드에 적용한다.

| 출처 기록 | 서버가 해소할 필드와 근거 |
|---|---|
| 기능 결과 | 실제 제공 기록의 말단 필드·원본 인스턴스·기능 계약·관찰 시점 |
| 요청·보완 | 실제 사용자 본문의 전체 제공 부분과 `context_sources` 허용 |
| 로컬 검사 | 실제 검사 필드와 연결된 리소스·로컬 검사 근거 |
| 보고·표·산출물 | 보존한 필드별 출처를 따라 원 관찰·요청·검사까지 재귀 해소 |

새 확인 근거는 원 관찰·실제 요청·검사까지 해소되는 기록으로 구성한다.
순환·누락·만료·불명확한 대응은 `RESULT_VALIDATION_FAILED`로 반환한다.
추가 원자료는 필요한 승인 계획으로 별도 조회한다.
각 출처 참조에는 하나 이상의 실제 제공 필드와 유효한 말단 근거가 필요하다.
출처 그래프는 [공통 출처 검사](common.md#공통-타입)의 한도를 따르며 최종 저장에 기여하는 말단 출처는 최대 100개다.
범위 초과·0개 필드·확인 불가는 `RESULT_VALIDATION_FAILED`다.
같은 정책·관찰 시점의 원본 참조·말단 경로를 중복 제거하고 다른 관찰은 각각 유지한다.

모든 기여 필드가 현재 계획·profile의 지식 범주·`source_fields`·모델 제공·보관 조건을 만족해야 후보를 저장한다.
실제 사용자 요청·보완은 리소스별 `context_sources`로 검사한다.
마스킹·미래 재제공·내보내기는 가장 엄격한 조건을 적용한다.
일반 경험은 업무·기여 출처의 가장 이른 유한 기한을 승계하며 모두 무기한이면 null을 사용한다.
필요하면 이후 업무에서 적법한 계획으로 조회·모델 제공 범위를 좁혀 새 근거를 확보한다.

실행기는 해소한 `(원 업무, 원 기록, 필드 경로, 실제 대상, 정책, 관찰·검증 시점)`을 기억 개정에 연결한다.
실행기는 출처·정책·필드 대응을 검증하고 후보 문장의 자연어 의미는 호스트가 실제 근거와 대조한다.
기한 계산·장기 파생 지식 허용은 [기억 보존](../tasks/memory.md#보존과-장기-운영지식)을 그대로 적용한다.

### 갱신 검사와 결과

`store`는 업무 보고를 보존한 종료 업무에서 현재 실행 소유자가 처리한다.
실패·취소 업무도 실제 확인한 사실만 저장하며 현재 계획의 해당 자료 보존 승인을 검사한다.
출처는 같은 업무의 요청·보완·로컬 검사·실제 결과·보고·표·산출물이며 이전 업무 자료는 적법하게 승계한 근거만 사용한다.
절차·실패 원인의 검증은 현재 열람·보관이 허용된 실제 관찰·사용자 확인과 대조한다.

실행기는 [MemoryRule](plans.md#예산보고완료-조건)과 profile 정책에서 범주·원출처 필드·재제공·보존을 해소한다.
허용·근거·비밀 제거·마스킹 검사를 통과하면 정본 개정·출처 연결·만료·멱등 기록을 보존한다.
중복 후보·동일 출처의 재저장·문장 정정은 기존 상태·기한을 승계한다.
실제 새 검증이 있는 개정은 원출처·승인·기존 기한에 따라 보존 조건을 다시 해소한다.
기존 유한 기한은 새 허용·더 긴 기본값·무기한 기본값을 선택한 뒤에도 유지한다.

`memory_ref`를 사용한 정정·무효화는 같은 profile·실제 대상의 현재 개정만 받는다.
현재 개정과 다르면 `MEMORY_CONFLICT`를 반환해 기억을 다시 조회하도록 한다.
`invalidate`는 현재 사용자 정정이나 실제 반증을 근거로 하며 `paused`를 제외한 업무에서 현재 소유자가 처리할 수 있다.
유효한 반증에 따른 무효화는 지식 저장 승인과 별도로 검사하며 새 지식 저장에는 해당 보존 승인이 필요하다.
원문·색인이 삭제된 항목도 최소 무효화 기록을 유지한다.

| data 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | 보존 업무·개정 |
| `update_ref` | RecordRef | 본문 복제 없는 갱신 결과 기록 |
| `memory_ref` | RecordRef | 저장·정정·무효화한 정확한 개정 |
| `memory_state` | enum | `active`, `invalidated` |
| `expires_at` | string 또는 null | 활성 지식의 유한 기한; 활성 무기한 지식과 무효화 결과는 null |
| `index_state` | enum | `ready`, `pending` |

성공한 정본 변경과 갱신 기록은 업무 개정을 한 번 증가시키고 기존 종료 상태·외부 효과를 유지한다.
유효성은 `memory_state`·기한·현재 권한·대상을 함께 검사한다.
`active`와 null의 조합은 활성 무기한 지식이며 `invalidated`는 검색 차단 상태다.
무효화·정정은 정본 검사로 이전 개정의 검색 노출을 즉시 막는다.
정본 보존 실패는 `STORE_UNAVAILABLE`이며 부분 저장 여부를 확인하고 같은 요청으로 복구한다.
정본은 보존했지만 색인이 미완성이면 `status=partial`, `MEMORY_INDEX_INCOMPLETE`, `index_state=pending`으로 반환한다.
API·키워드 전용 선택과 벡터 갱신 실패의 구분은 [임베딩 계약](../tasks/memory.md#임베딩-api-계약)을 따른다.
같은 요청의 재전달은 기존 정본·기한과 현재 색인 상태를 반환하며 관리 복구에서 재색인한다.
기억 갱신 실패 시 보존된 보고·종료 상태를 유지하고 기억 갱신 경로에서 복구한다.

첫 구현의 갱신 확인 기록에는 작업 종류·기억 개정·출처 참조·상태와 이유 확인용 해시만 남긴다.
지식 본문은 기억 정본 영역의 제공·보존 정책으로 관리한다.
확인 시각은 실제 관찰이나 등록된 사용자 자료의 불변 시각을 유지한다.

## 결과 DTO

보고 내용·분석·계산·파일 형식은 호스트가 사용자 요청과 실제 자료에 맞춰 구성하고 적절한 도구로 검증한다.
실행기는 승인·근거 참조·현재 제공 권한·보존기한·파일 무결성을 검사해 결과를 보관한다.
자연어 주장의 정확성과 업무 완료 판단은 호스트의 책임이다.

### ReportDraft·Finding

| 필드 | 타입 | 의미 |
|---|---|---|
| `summary` | 비어 있지 않은 string | 사용자에게 전달할 요약 |
| `proposed_task_state` | enum | `succeeded`, `partial`, `failed`, `cancelled` |
| `findings` | Finding[] | 실제 관찰·분석·기억·가설 |
| `checks` | ReportCheck[] | 승인한 완료 조건별 호스트 판단과 실제 근거 |
| `limitations` | string[] | 조회 범위·잘림·미확인 사항 |
| `uncertain_effects` | 중복 없는 ID[] | 실행기가 보존한 미확인 call ID 전체 |
| `artifacts` | ArtifactDraft[] | 호스트 생성 파일의 등록 또는 기존 파일 첨부 |
| `next_actions` | string[] | 필요한 후속 행동 |

요약·상태와 위 배열은 필수이며 각 배열은 최대 1,000개 항목을 받는다.
보고 전체의 입력 한도는 공통 도구의 보고 크기 조건을 따른다.
과거 요청의 빈 `tables: []`는 호환 입력으로 해석한다.
표 내용은 `Finding.text`의 텍스트 또는 호스트가 작성한 파일로 전달한다.

`Finding`은 `kind`, `text`, `evidence_refs`를 갖는다.
`kind`는 `observation|derived|memory|hypothesis`, `text`는 비어 있지 않은 문자열, 참조는 중복 없는 RecordRef 목록이다.
관찰은 실제 요청·보완·로컬 검사·호출 결과에 연결한다.
분석은 실제 근거를 연결하고 기억은 현재 계획에 선택한 유효 개정에 연결한다.
가설은 추가 확인이 필요한 내용으로 표시한다.

### ReportCheck

| 필드 | 타입 | 조건 |
|---|---|---|
| `criterion_id` | ID | 현재 계획의 완료 조건; 조건마다 한 번 |
| `status` | enum | `met`, `unmet`, `unknown`, `not_applicable` |
| `method` | 비어 있지 않은 string | 실제 수행·검증 방법 |
| `evidence_refs` | 중복 없는 RecordRef[] | 완료 판단에 사용한 근거 |
| `reason` | string | 선택; `unmet`, `unknown`, `not_applicable`에는 비어 있지 않은 값 |

`met`에는 실제 근거가 필요하고 완료 조건의 단계들이 인용한 호출의 승인 대상·기능·범위와 대응해야 한다.
리소스 읽기 묶음의 경우 실제 선택 도구도 호출 이력에 보존한다.
외부 작업의 수행 사실은 실제 호출·관찰 기록으로 확인하고 과거 기억은 수행 방법을 판단하는 자료로 활용한다.
필수 조건의 적용 제외는 승인한 모든 관련 분기가 실제 허용 관찰에서 false임을 확인해야 한다.

호스트는 표본·단건·전수 조사 등 요청한 목적에 맞춰 충분한 관찰 범위를 판단한다.
원본 전체성·잘림·다음 페이지 정보는 이 판단에 사용하는 자료다.
예를 들어 최근 5건을 요청한 업무는 해당 표본을 확인해 완료할 수 있고, 전수 조사에서는 호스트가 필요한 후속 페이지를 조회·대조한다.

### 호스트 결과와 파일

호스트는 현재 사용할 수 있는 파일·실행·MCP 도구로 필요한 텍스트·표·JSON·CSV·문서·스프레드시트 등을 작성한다.
지원 형식은 해당 도구의 실제 기능에 따라 정한다.
파일을 보존할 때에는 TaskReceipt·TaskSnapshot의 `artifact_workspace`에 작성하고 아래 등록 정보를 제출한다.

실행기는 일반 파일·경로·심볼릭 링크를 검사하고 바이트를 스트리밍으로 관리 영역에 복사한다.
실제 크기·SHA-256·파일 이름·미디어 타입과 출처를 등록하고 입력 파일의 내용은 변환 없이 보존한다.
미디어 타입·내용·계산·레이아웃의 일치는 호스트가 선택한 도구로 확인한다.
등록이 완료된 작업용 원본 중 복사 당시와 같은 내용은 다음 저장소 정리에서 제거한다.
실패한 보고의 원본과 등록 후 수정한 작업 파일은 작업 영역에 남고 업무 기한·profile 삭제에 따라 정리한다.

### ArtifactDraft

| 입력 종류 | 필수 필드 |
|---|---|
| 호스트 생성 파일 | `artifact_id:ID`, `label:string`, `local_path:string`, `media_type:string`, `source_refs:RecordRef[]` |
| 기존 파일 | `kind:"existing"`, `artifact_id:ID`, `label:string`, `artifact_ref:RecordRef` |

생성 파일의 `kind:"file"`은 선택이며 생략하면 같은 의미다.
ID·표시명은 비어 있지 않은 문자열이고 한 보고 안의 ID는 고유하다.
`local_path`는 해당 업무 작업 영역 안의 실제 일반 파일을 가리키는 절대 경로다.
`media_type`은 길이 255 이내의 미디어 타입이며 `source_refs`는 하나 이상의 중복 없는 실제 기여 출처다.
기존 파일은 같은 업무에 등록된 파일의 형식·기한·출처를 이어받는다.
같은 파일 ID는 기존 바이트·미디어 타입을 유지하며 다른 내용은 새 ID로 등록한다.

자유 파일은 보고 근거·선택 기억·요청·실제 제공한 호출 자료에 현재 내보내기 허용을 적용한다.
모델에 제공한 원출처 필드 전체가 내보내기 허용 범위 안에 있어야 하며 파생 자료는 말단 출처까지 대조한다.
파일의 말단 기여 출처는 보고 근거에도 연결해 이후 조회·기억 검증에 사용한다.
파일 기한은 업무와 기여 출처의 가장 이른 유한 기한을 승계한다.
파일 경로·해시·출처 검사는 파일 내용의 사실성·계산 정확성을 독립적으로 증명하는 검사와 구분한다.

## daeri_result_submit

호스트가 작성·검증한 보고와 파일을 등록하고 선별한 지식의 자동 저장을 함께 요청한다.

| 입력 필드 | 타입 | 필수 | 조건 |
|---|---|---|---|
| `request_id` | ID | 예 | 같은 보고의 멱등 키 |
| `task_id` | ID | 예 | 현재 업무 |
| `expected_task_revision` | TaskRevision | 예 | 현재 개정 |
| `plan_id` | ID 또는 null | 예 | 현재 계획; 계획 전 실패·취소는 null |
| `plan_version` | PlanVersion 또는 null | 예 | 현재 계획 개정과 함께 지정 |
| `report` | ReportDraft | 예 | 호스트의 보고·완료 판단·파일 등록 |
| `memory_candidates` | MemoryCandidate[] | 아니요 | 최대 20개; 생략·[]는 새 기억 저장 생략 |

`MemoryCandidate`의 필수 필드는 `draft:MemoryDraft`, `source_refs:RecordRef[]`이며 정정 대상의 `memory_ref:RecordRef`는 선택이다.
출처는 1~100개의 고유 참조로 지정하고 초안은 [기억 입력](#daeri_memory_update)의 필드·16KiB 조건을 따른다.
호스트는 실제 업무에서 재사용할 규칙·구조·절차·교훈을 선별해 같은 보고에 연결한다.
실행기는 승인한 업무 흐름에서 후보의 현재 출처·정책을 검사하고 정본 저장·색인·재시도를 자동 처리한다.
자동 기억의 실패는 보존한 보고·업무 종료를 유지하고 실제 처리 상태로 반환한다.

### 검증과 상태 반영

실행기는 현재 업무 소유권·개정·계획 참조와 실제 승인 이력을 확인한다.
활성 업무의 파일 등록·성공 보고에는 현재 유효한 승인이 필요하다.
종료 업무의 후속 보고·파일 등록은 현재 계획의 실제 승인 이력과 현재 자료 제공 조건을 검사하고 기존 종료 상태를 유지한다.
성공 보고는 진행 중·미확인 호출이 해소돼야 하며 실제 미확인 효과를 보고 전체와 대조한다.
보고가 인용한 근거는 현재 업무에 귀속되거나 승인한 재계획으로 승계한 기록이어야 한다.
현재 권한·실제 대상·마스킹·기한을 다시 검사하고 허구·만료·차단 참조를 거부한다.

모든 완료 조건은 한 번씩 판단하고 필수 조건의 `met|not_applicable` 및 미확인 효과 없음이 성공 상태의 입력 조건이다.
실행기는 근거 연결을 검사하고 호스트의 의미 판단은 `completion_assessed_by:"host"`로 표시한다.
호스트는 요청 목적에 맞는 확인 범위·제한과 완료 판단을 함께 보고한다.

계획 전에는 실제 로컬 검사 실패·취소 사유를 근거로 `failed|cancelled`를 보존한다.
이때 완료 검사·산출물·미확인 효과는 빈 목록이다.
일시중지 상태는 재개 뒤 결과를 제출하고 취소 보고는 실제 취소 상태와 연결한다.

파일 복사·보고 등록·업무 상태·자동 기억 대기의 공개는 같은 저장 트랜잭션으로 연결한다.
실패한 공개의 미등록 복사본은 다음 정리에서 제거하고 원래 입력 파일은 보존한다.
같은 요청은 저장한 보고·파일을 현재 권한으로 재조회한다.
과거 보고·표·파일과 저장한 요청의 재전달은 기존 자료를 읽는 호환 경로로 유지한다.

### 출력 data

| 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | 업무·개정 |
| `report_ref` | RecordRef | 보존 보고 |
| `task_state` | TaskState | 최종 상태 |
| `artifact_refs` | RecordRef[] | 등록·첨부 파일 |
| `validation` | object | 구조·근거 연결·호스트 완료 판단 |
| `delivery_status` | enum | 현재 `not_confirmed` |
| `memory` | object | 자동 기억의 실제 처리 결과 |

`validation`은 `structure:"passed"`, `evidence:"passed"`, `completion:"met|unmet|unknown"`,
`completion_assessed_by:"host"`, `checks:ReportCheck[]`, `issues:string[]`를 갖는다.
과거 보고에는 당시의 표·산출물 검증 메타데이터가 남아 있을 수 있다.

`memory.state`는 `ready|pending|partial|skipped`이며 항목별 처리 상태·기억 참조·오류 코드를 제공한다.
각 항목의 필수값은 `state:ready|pending|skipped`이고 실제 처리에 따라 `memory_ref`, `error_code`, `next_action:"resubmit"`을 제공한다.
후보가 없으면 `skipped`와 빈 항목 목록이다.
검증된 후보의 일시 저장·색인 장애는 지속 대기로 재시도한다.
검증 전 장애와 본문 없는 과거 대기는 `next_action:"resubmit"`으로 호스트의 새 후보 제출을 안내한다.
자동 후처리는 보고가 확정한 업무 상태·개정을 유지하고 기억·색인의 현재 상태만 갱신한다.

## daeri_result_get

보존 요청·답변·승인 응답·worker 할당·완료 결과·호출 결과·보고·파일과 과거 표에서 현재 허용 자료를 읽는다.

| 입력 필드 | 타입 | 필수 | 조건 |
|---|---|---|---|
| `task_id` | ID | 예 | 열람 가능한 업무 |
| `record_ref` | RecordRef | 예 | 해당 업무에 등록한 기록 |
| `cursor` | Cursor | 아니요 | 이전 응답의 연속 조회 |
| `limit_bytes` | integer | 아니요 | 기본 65,536바이트, 1~1,048,576 |

각 페이지에서 기록 귀속·현재 제공·마스킹·기한·무결성을 검사한다.
신규 실행은 업무의 고정 구성을 사용하고, 보존 결과는 관련 연결·실제 대상·현재 정책을 대조한다.
동일 설정 재저장·기본 맥락 변경·무관한 연결 추가 뒤에도 현재 권한이 유지되는 자료는 열람한다.
대상 변경·리소스 삭제·허용 필드 축소·기억 무효화로 제공 조건이 달라지면 관련 파생 보고·파일도 차단한다.

계획은 `daeri_plan_get`, 기억 본문은 현재 권한으로 `daeri_memory_search`에서 읽는다.
검색·갱신 기록은 허용된 상태·개정 참조 등 메타데이터를 제공한다.

| data 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | 업무·개정 |
| `record_kind` | enum | 요청·검사·승인·호출·보고·파일 등 등록 종류; 과거 `table` 포함 |
| `payload` | JsonValue | 현재 허용 본문; 파일은 null |
| `next_cursor` | Cursor 또는 null | 연속 조회 |
| `has_more` | boolean | 허용 본문의 후속 조각 |
| `redactions` | string[] | 현재 [] |
| `evidence_refs` | RecordRef[] | 현재 제공 가능한 기여 출처 |
| `integrity` | enum | 정상 `intact` |
| `coverage` | object | `source_complete:boolean|null`, `returned_scope:string`, `missing_scope:string[]` |
| `managed_file` | ArtifactFile 또는 null | 관리 파일의 실제 위치·메타데이터 |

`ArtifactFile`은 `artifact_ref`, `format_id`, `media_type`, `size_bytes`, `local_path`, `expires_at`을 갖고 새 등록 파일은 `filename`도 제공한다.
`format_id`는 등록 파일의 보존 형식을 식별하는 메타데이터다.
파일을 직접 읽을 수 있는 경로는 현재 내보내기 허용과 해시를 확인한 뒤 호스트의 파일 링크로 전달한다.
사용자에게 전달한 사본의 수명은 해당 사본의 관리 경로를 따른다.

문자열은 UTF-8 문자 경계, JSON은 가장 큰 배열의 항목 경계로 나눈다.
커서는 업무·기록·위치·현재 본문 서명과 연결하고 잘못된 커서는 `CURSOR_INVALID`다.
단일 항목이 반환 한도를 초과하면 `RESULT_TRUNCATED`로 더 큰 한도를 안내한다.
정상 분할은 `has_more:true`·`status:ok`다.
알려진 원격 잘림은 `partial`·`RESULT_TRUNCATED`, 만료는 `RECORD_EXPIRED`, 파일 유실·변조는 `RECORD_UNAVAILABLE`로 구분한다.
업무가 부분 완료여도 보존 보고 자체가 온전하면 그 조회는 `status:ok`다.

## 전달과 검증 경계

파일 내용 작성·계산·의미 판단은 호스트와 선택한 도구가 담당한다.
실행기가 검증한 것은 승인·근거 연결·현재 권한·기한·무결성이며 호스트 전달 성공은 실제 증거로 확인한다.
저장별 정본·공개·정리는 [로컬 저장](../configuration/profiles.md#로컬-저장-구조),
실제 stdio·호스트·회사·OS 검증 범위는 [구현 계획](../../plans/initial-implementation.md#현재-구현-단위와-검증-경계)을 따른다.

담당 구현은 [입력 DTO](../../../src/daeri/contracts.py), [업무·결과 연결](../../../src/daeri/engine.py), [worker 게이트웨이](../../../src/daeri/workers.py), [보고 검사](../../../src/daeri/results.py), [기억 출처 검사](../../../src/daeri/knowledge.py), [자동 기억 처리](../../../src/daeri/automatic_memory.py)다.
[호스트 파일·출처 검사](../../../tests/test_host_results.py), [업무 통합·재전달](../../../tests/test_execution_flow.py), [worker 권한·묶음 선택](../../../tests/test_workers.py), [자동 기억·복구](../../../tests/test_automatic_memory.py)가 대표 검증이다.
