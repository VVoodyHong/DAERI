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
| `DesiredOutput` | `format` | string, 선택 | 원하는 형식; 최종 지원은 등록 형식·계획으로 확인 |
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

`TaskState`는 `planning|awaiting_input|awaiting_approval|paused|running|reconciling|verifying|succeeded|partial|failed|cancelled`다.
상태 전이는 업무 흐름 정본을 따른다.
요약의 공개 범위는 위 업무 메타데이터다.
현재 실행기는 승인 후 `running`에서 조회·확인을 수행하며 `verifying`·`reconciling` 전이는 후속 실행 확장에 사용한다.

`TaskReceipt`는 `task_id`, `task_revision`, `request_ref`, `state`, `context`, `waiting_reason`을 갖는다.
타입은 TaskSnapshot과 같고 수정으로 질문·답변·맥락 보완 기록을 만들었을 때 선택 필드 `clarification_ref:RecordRef`를 추가한다.

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

질문을 `waiting_reason.message`, 종류를 `input`으로 저장하고 복귀 단계를 연결한 뒤 `awaiting_input`으로 전이한다.
반복 질문에서도 최초 질문 전의 복귀 단계를 유지한다.
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
| `contracts` | CapabilityDescriptor[] | 배정 단계의 현재 기능 계약 |
| `calls` | WorkerCallSummary[] | 해당 위임의 호출만 포함 |
| `result_ref` | RecordRef 또는 null | 보존 완료 결과; 완료 전 null |
| `result` | WorkerResult | 보존 결과가 있을 때만 반환 |

`PlanStep`은 [계획 단계](plans.md), `CapabilityDescriptor`는 [기능 계약](resources.md#capabilitydescriptor)을 따른다.
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
재귀 해소는 최대 32단계·256개 기록이며 범위 초과·0개 필드·확인 불가는 `RESULT_VALIDATION_FAILED`다.
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

결과 도구의 처리 대상은 로컬에 보존한 근거·보고·관리 산출물이다.
추가 검증은 먼저 승인된 리소스 호출로 근거를 확보한다.
설명·이름 문자열과 ID·기록 참조는 비어 있지 않아야 하며 자료 크기는 공통 설정 상한을 적용한다.
실제 본문·셀 값·키 등 데이터의 빈 문자열은 해당 자료형과 등록 계약이 허용하면 그대로 유지한다.

### ReportDraft·Finding

| ReportDraft 필드 | 타입 | 제약 |
|---|---|---|
| `summary` | string | 요청 해석·수행 결과 요약 |
| `proposed_task_state` | enum | `succeeded\|partial\|failed\|cancelled` |
| `findings` | Finding[] | 관찰·파생·과거 기억·가설 구분 |
| `tables` | TableDraft[] | 검증·보존할 파생 표; 파일이 없어도 제출하며 없으면 [] |
| `checks` | ReportCheck[] | 계획의 모든 완료 조건을 한 번씩 포함 |
| `limitations` | string[] | 누락·잘림·실패·마스킹의 영향 |
| `uncertain_effects` | ID[] | 효과 미확인 call ID |
| `artifacts` | ArtifactDraft[] | 관리 자료 생성·첨부 |
| `next_actions` | string[] | 후속 판단 설명; 실제 실행은 해당 승인·실행 경로 적용 |

`findings`부터 `next_actions`까지 각 최상위 배열은 최대 1,000개 항목을 받는다.
전체 보고 요청에는 [공통 입력 크기](common.md#입력을-구성하는-원칙)를 적용한다.

| Finding 필드 | 타입 | 제약 |
|---|---|---|
| `kind` | enum | `observation\|derived\|memory\|hypothesis` |
| `text` | string | 관찰·계산 방법·과거 지식 또는 미확인 가설 |
| `evidence_refs` | RecordRef[] | 관찰·과거 기억은 최소 하나; 파생 결과는 아래 표 인용과 함께 검사 |
| `table_ids` | ID[] | 선택; 이번 보고의 검증표 인용, 기본 [] |

관찰·파생·과거 기억·가설의 의미는 [근거 원칙](../tasks/workflow.md#이력과-완료-근거)을 따른다.
가설은 근거 배열이 비어 있을 수 있으며 미확인 가설로 표시한다.
`table_ids`는 이번 보고의 `tables`에 제출한 ID만 중복 없이 지정하며 검증에 통과한 표만 근거로 사용한다.
`derived`는 실제 `evidence_refs` 또는 검증표 중 하나 이상의 근거가 있어야 한다.
`observation`·`memory`는 표 인용과 함께 실제 관찰·기억 기록 근거를 요구한다.
`memory`는 현재 계획의 `memory_refs`에 연결한 지식만 가리키며 과거 확인과 현재 관찰을 구분해 표시한다.
현재 실행·상태 확인이 필요한 완료 조건은 실제 현재 관찰을 근거로 `met` 여부를 판정한다.

### ReportCheck

| 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `criterion_id` | ID | 예 | 저장 완료 조건 ID; 조건마다 고유 |
| `status` | enum | 예 | `met\|unmet\|unknown\|not_applicable` |
| `method` | string | 예 | 실제 확인 방법·범위 |
| `evidence_refs` | RecordRef[] | 예 | 사용한 등록 근거 |
| `table_ids` | ID[] | 아니요 | 이번 보고의 검증표 ID; 중복 없이 지정하며 기본 [] |
| `reason` | string | 조건부 | `unmet\|unknown\|not_applicable`이면 필수 |

`reason`을 명시하면 모든 상태에서 문자열이어야 하며 필수 상태에서는 공백을 제외한 설명이 필요하다.
`met`는 필요한 근거와 보존한 검증 결과가 있어야 한다.
이번 보고의 검증표도 해당 완료 조건의 방법·범위·완전성을 실제 충족하면 근거로 사용할 수 있다.
원격 변경 성공·현재 상태 확인은 실제 호출·관찰 근거를 요구한다.
`not_applicable`은 `required=false`이거나 승인 분기가 false임을 등록 관찰로 확인했을 때만 허용한다.
필수 조건을 제외하려면 해당 조건의 모든 단계에서 현재 계획의 `eq`·`in` 분기 중 하나 이상이 false임을 확인해야 한다.
실행기는 바인딩의 출처 단계·리소스·기능 계약과 실제 허용 스칼라 값을 대조한다.
여러 인용 관찰이 있으면 같은 비교가 모두 false여야 한다.
적용 제외의 근거는 값이 실제 해소된 현재 계획 또는 적법하게 승계한 관찰의 `eq`·`in` 비교다.
명시 승계한 관찰도 [단계 대응·승계 기준](plans.md#planstep)을 충족해야 한다.
이유만 적어 필수 조건을 면제하거나 다른 완료 조건과 모순되면 검증 실패다.

### 결과 형식과 생성기

사용자에게는 요청에 맞는 텍스트·표·구조화 응답을 대화로 제공하고 필요한 파일을 함께 제공한다.
출력 형식은 등록된 로컬 생성기를 교체·추가해 확장한다.
표는 내용의 구조, JSON·CSV·DB 파일 등은 그 내용을 전달하는 형식이며 내부 저장 엔진과 별개다.

호스트는 [로컬 등록 목록](resources.md#daeri_resource_list)의 `output_formats`에서 실제 가능한 형식과 입력·옵션·준비 상태를 확인한다.
파일별 형식·계약·입력 종류·옵션·최대 크기는 [ArtifactRule](plans.md#예산보고완료-조건)로 승인받는다.
실행기는 검증한 자료·출처·정해진 관리 위치를 생성기에 제공하고 생성기는 파일만 만들어 반환한다.
생성기의 실행 범위는 제공받은 자료를 지정 관리 위치의 파일로 변환하는 로컬 처리다.

첫 구현에는 준비된 `text`, `json`, `csv` 생성기를 등록한다.
`text`는 텍스트·보고, `json`은 구조화 자료·표·보고, `csv`는 검증표 입력을 받는다.
초기 옵션은 빈 객체이며 형식별 불변 계약 참조와 실제 허용 입력을 등록 목록에서 확인한다.
새 형식은 같은 계약으로 확장한다.
DB 파일 생성기도 검증한 구조화 자료로 새 관리 파일을 만드는 방식으로 확장한다.
운영 DB 쓰기·기존 파일 덮어쓰기·외부 게시에는 별도 리소스 기능과 해당 변경 승인이 필요하다.
입력 종류는 초기 `text`, `json`, `table`, `report`를 사용하되 추가 종류의 스키마·출처·검증 방법을 등록할 수 있다.
지원 여부는 등록 계약·준비 상태·입력 종류로 판정하고 조건 불일치 시 검증 실패와 가능한 대안을 반환한다.

생성기는 등록된 옵션 스키마와 크기 한도를 검사하고 실제 형식·미디어 타입·확장자·바이트 수를 반환한다.
실제 형식·확장자·미디어 타입·크기와 승인 계약이 일치해야 파일을 등록한다.
모든 생성기에 공통 제공·기한·비밀 제거 정책을 적용한다.
파일은 관리 참조로 보존하며 현재 허용된 로컬 파일 위치는 `daeri_result_get`으로 제공한다.
바이너리 파일은 `managed_file`의 경로·메타데이터로 전달한다.

보고 텍스트 파일은 요약·관찰·검증표·완료 조건 검사·제한·후속 행동을 읽을 수 있는 문장으로 만든다.
JSON 보고 파일은 이번 요청에서 검증한 보고의 구조를 유지하고 입력 본문은 `artifacts`를 뺀 보고 자료다.
둘 다 기여 출처의 현재 내보내기 허용을 다시 검사한다.

자유 문장의 일부가 어느 원본 필드에서 왔는지 자동으로 증명할 수 없으므로 첫 버전은 보수적으로 검사한다.
보고·검사·표의 근거, 선택한 기억·요청 자료와 업무에서 실제 제공한 호출 결과를 함께 확인한다.
각 원본에서 모델에 제공한 필드 전체가 현재 내보내기 범위에 포함돼야 텍스트·JSON 보고 파일을 만든다.
기여 경로나 제공 정책이 불명확하면 파일 생성을 거부하고 필요한 자료 범위를 좁힌 계획을 안내한다.

### ArtifactDraft

모든 산출물은 현재 계획의 `artifact_id`에 대응하는 `ArtifactRule`에서 형식·옵션을 해소한다.
한 보고의 `artifacts`에서 ID는 고유하며 계획의 ID 하나는 산출물 하나에 대응한다.
이미 보존한 ID는 같은 내용·형식이면 기존 참조를 재사용하고 다르면 거부한다.
추가 파일은 새 ID로 계획을 승인받고 기존 파일·기한은 원 등록 값을 유지한다.
동일 요청 재전달은 기존 산출물 참조를 반환한다.

| 형태 | 필수 필드·타입 | 제약 |
|---|---|---|
| 생성 | `kind="generated"`, `artifact_id:ID`, `source_ref:RecordRef`, `label:string` | 허용 보존 자료의 형식 변환 |
| 기존 첨부 | `kind="existing"`, `artifact_id:ID`, `artifact_ref:RecordRef`, `label:string` | 해당 업무의 산출물 형식·기한 유지 |
| 파생 표 | `kind="derived"`, `artifact_id:ID`, `table_id:ID`, `label:string` | 이번 보고의 검증표를 등록 형식으로 출력 |
| 보고 파일 | `kind="report"`, `artifact_id:ID`, `label:string` | 이번 요청의 검증한 보고 본문을 파일로 제공 |

생성은 원본 계약의 입력 종류, 파생 표는 `table`, 보고 파일은 `report` 입력을 받는 생성기를 사용한다.
파생 표 파일의 `table_id`는 이번 보고의 `tables`에 제출한 표를 선택하며 검증에 통과한 내용만 생성기에 전달한다.
보고 파일 입력은 이번 보고의 `artifacts`를 제외한 본문·근거이며 출처 참조는 비순환이어야 한다.
기존 첨부의 등록 형식·실제 미디어 타입은 승인한 형식과 일치해야 하며 바꾸려면 생성으로 처리한다.
`label`은 표시 이름이다.
입력 자료는 등록 참조에서 읽고 실제 파일 위치는 실행기의 관리 영역에서 정한다.

생성·첨부에는 [제공·보존 정책](../tasks/workflow.md#결과-제공과-전달)을 적용한다.
원문 HTML·수식·스크립트는 데이터로 취급하고 형식별 무해화 규칙을 적용한다.

파일 참조에는 원문 대신 기여 출처 참조를 연결한다.
생성 파일은 변환한 원자료, 파생 파일은 검증표의 실제 원출처, 보고 파일은 제공 검사를 통과한 맥락·근거를 연결한다.
기존 파일 첨부는 최초 기여 출처·원 승인·기존 기한을 승계한다.
저장된 보고의 형식 변환과 기존 파일의 재첨부에도 현재 출처·내보내기 검사를 적용한다.
새 지식의 근거로 쓰는 파일은 실제 원출처까지 해소돼야 한다.

### 파생 표와 셀 검증

`TableDraft`는 필수 `table_id:ID`, `sources:TableSource[]`, `columns:TableColumn[]`, `rows:TableRow[]`를 가진다.
한 보고의 `tables`에서 `table_id`는 고유하며 현재 계획의 같은 ID인 `TableRule`과 일치해야 한다.
현재 계획의 [TableRule](plans.md#파생-표의-허용-범위)로 결합·키·허용 연산·완전성·행 한도를 검사한다.
표는 파일 없이도 검증·보존하며 파일 생성 여부와 무관하게 같은 출처·제공·가림·현재 권한·보존 검사를 적용한다.
호스트는 결과 후보·근거를 제출하고 실행기가 검증 결과·원본 완전성·기한을 해소한다.

| 타입 | 필수 필드·타입 |
|---|---|
| `TableSource` | `source_id:ID`, `record_refs:RecordRef[]` |
| `TableColumn` | `column_id:ID`, `label:string`, `value_type:enum` |
| `TableRow` | `key:JsonValue[]`, `cells:TableCell[]` |
| `TableCell` | `column_id:ID`, `value:string/boolean/null`, `operation:TableOperation`, `inputs:SourceCell[]` |
| `SourceCell` | `source_id:ID`, `record_ref:RecordRef`, `pointer:JsonPointer` |

`source_id`는 계획의 소스와 일대일로 대응하며 소스마다 중복 없는 `record_refs`를 하나 이상 둔다.
소스 하나의 `record_refs`와 셀 하나의 `inputs`는 각각 최대 1,000개다.
출처 기록은 현재 업무의 실제 결과 또는 명시적으로 승계한 결과만 사용한다.
실행기는 같은 소스의 기록이 승인 단계·인스턴스·기능 계약·조회 조건에 대응하는지 검사한다.
여러 기록을 합칠 때 동일 조회 식별·고유한 키·확인된 페이지 완전성을 검사한다.
초기 로컬 처리 상한은 소스당 10,000행이며 결과 행 수는 승인한 더 작은 한도를 적용한다.
`SourceCell.pointer`는 해당 기록의 기능 결과 본문에서 실제 스칼라 값 하나를 가리켜야 한다.

열 ID는 고유하며 ID·표시 이름에는 공백을 제외한 문자가 하나 이상 있어야 한다.
`value_type`은 `text`, `decimal`, `boolean`이다.
각 행에는 모든 열의 셀이 정확히 하나씩 있고 키는 원본과 같은 타입의 null 아닌 스칼라 배열이다.
셀은 그 행의 키에 대응하는 실제 원본 행에서만 값을 가져온다.
문자열 비교는 원래 값·대소문자·시간대·단위·자료형을 유지한다.

`decimal` 값은 부호·정수부·선택 소수부를 가진 10진 문자열로 전송한다.
허용 문법은 `-?(0|[1-9]\d*)(\.\d+)?`이며 유효숫자 최대 38자리·소수부 최대 18자리다.
원본 숫자는 등록된 계약의 정확한 10진 표현을 사용한다.
산술 입력은 정확한 정수 또는 결과 계약이 `decimal` 타입임을 확인한 10진 값이다.
정밀도 보존 실패·한도 초과는 검증 실패다.
10진 비교·산술은 문자열 순서가 아닌 정확한 수치로 처리하고 `1.0`과 `1.00`, `-0`과 `0`을 같은 값으로 정규화한다.

`TableOperation`은 아래 단일 연산과 실제 원본 셀 참조로 구성한다.

| 연산 | 입력과 검증 |
|---|---|
| `copy` | 원본 셀 하나; 타입·값 대응 확인, 실제 null도 복사 가능 |
| `equal` | 같은 타입의 셀 두 개; 정확한 동등 비교로 boolean 생성 |
| `sum` | 같은 단위 숫자 셀 하나 이상; 정확한 합계 |
| `difference` | 같은 단위 숫자 셀 두 개; 첫 값에서 둘째 값을 뺌 |
| `ratio` | 숫자 셀 두 개; 첫 값을 둘째 값으로 나눔, 분모 0 거부 |
| `missing` | 입력 []; `full` 결합에서 해당 소스 행의 부재를 확인한 경우만 null |

`ratio` 셀의 추가 필수 필드는 `scale:NonNegativeInt`다.
`scale`의 적용 대상은 `ratio`이며 `missing`의 추가 필드는 아래에 정의한다.
scale은 계획의 `ratio_scale`과 같고 최대 18이며, 해당 자리에서 가장 가까운 값으로 반올림하고 정확히 중간이면 짝수 쪽을 택한다.
단위·시간대의 업무상 대응은 원래 자료 표현과 계획 설명·등록 계약의 근거로 확인한다.
셀 재계산은 해당 연산의 수치 정확성을 확인하고 업무 의미는 호스트가 근거와 대조한다.
산술은 정확히 표현 가능한 실제 숫자 값에 적용하며 null·가림·누락은 각각의 상태를 유지한다.
합계·차이는 같은 단위의 입력을 사용하며 단위 불일치는 검증 실패다.
등록 단위가 없으면 단위의 업무상 대응을 자동 검증하지 못했다는 제한을 남긴다.

`missing` 셀에는 추가 필수 `missing_source_id:ID`로 부재를 확인할 소스를 지정한다.
부재 판정에는 해당 소스의 승인 범위·전체 페이지 완전성이 필요하다.
값이 실제 null인 셀은 `copy`로 표현해 행 자체가 없는 경우와 구분한다.

실행기는 승인한 결합키로 원자료의 키 집합을 만들고 `inner|full|none`의 예상 결과와 제출 행을 대조한다.
`complete`는 승인한 조회 범위에서 모든 원소스의 확인된 전체성과 예상 키의 정확한 일치를 요구한다.
전체성의 근거는 모든 기여 기록의 `source_complete=true`다.
`selected`는 유효한 일부 키만 허용하고 보고에 선택 결과임을 표시한다.
원격 집계의 `copy`·행 안의 `sum` 검산은 실제 셀 값·연산을 확인한다.
원격 집계 전체성은 기능의 완전성 근거로, 원인 분석은 관찰·가설 구분으로 검토한다.

출력 값뿐 아니라 결합키·부재·행 포함 판정에 쓰인 필드도 기여 출처다.
모든 기여 필드의 원출처·내보내기 허용·가림·현재 권한을 해소해야 검증을 통과한다.
보존기한은 업무 보고·표와 모든 기여 원자료의 유한 기한 중 가장 이른 값이며, 모두 무기한일 때만 null이다.
보고에는 각 원자료의 실제 의미·관찰 시점·완전성과 그 차이를 제한으로 표시한다.

검증 통과 시 실행기가 `TableValidation`을 생성해 보존할 표와 연결한다.
필수 필드는 `table_ref:RecordRef`, `table_id:ID`, `source_refs:RecordRef[]`, `row_count:NonNegativeInt`, `coverage:enum`, `checks:string[]`, `limitations:string[]`, `expires_at:string|null`이다.
`coverage`는 실제 확인한 `complete|selected`이며 `checks`에는 형식·출처·결합·산술·제공·기한 검사 결과를 남긴다.
검증 실패는 `RESULT_VALIDATION_FAILED`로 반환한다.
유효 표·산출물 참조와 검증 결과는 실행기의 검사·보존 성공 뒤 발급한다.

파생 표를 파일로 생성하면 `ArtifactValidation`으로 파일과 검증표의 연결을 기록한다.
필수 필드는 `artifact_ref:RecordRef`, `artifact_id:ID`, `table_id:ID`, `table_ref:RecordRef`이며 보존한 표 검증 결과에 참조로 연결한다.
파일 생성의 형식·크기·제공·기한 검사는 그대로 적용한다.

CSV는 검증한 표에서만 만들고 UTF-8·쉼표·헤더와 [RFC 4180](https://www.rfc-editor.org/rfc/rfc4180)의 인용·개행 규칙을 적용한다.
숫자·boolean은 정규화한 문자열, null은 빈 셀로 내보내며 null·빈 문자열의 구분은 보존한 구조화 표에서 확인한다.
문자열의 수식 시작 기호·탭·개행은 [CSV 주입 방지](https://owasp.org/www-community/attacks/CSV_Injection)에 맞춰 내보낼 때 무해화하고 변환 여부를 검증 기록에 남긴다.

가상 허용 사례는 유일키가 있는 두 결과를 결합해 각각의 수치와 차이를 검산하는 것이다.
중복 키·다른 행의 셀 참조·누락 행·0 나눗셈·가려진 결합키·완전성 없는 부재 주장·만료 출처는 거부 사례다.
첫 구현은 실제 로컬 SQLite 기록을 이용해 위 허용·거부 경계와 파일 생성을 검증했다.

## daeri_result_submit

보고 초안을 실제 기록·완료 조건·제공 범위와 대조해 보존한다.
업무 상태는 실제 기록·완료 조건 검증 결과로 판정한다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | ID | 예 | 보고 재전달 키 |
| `task_id` | ID | 예 | 보고 저장 권한이 있는 업무 |
| `expected_task_revision` | TaskRevision | 예 | 중복 검사 뒤 개정 비교 |
| `plan_id` | ID 또는 null | 예 | 현재 불변 계획 |
| `plan_version` | PlanVersion 또는 null | 예 | 저장 버전 일치 |
| `report` | ReportDraft | 예 | 위 결과 DTO |
| `memory_candidates` | MemoryCandidate[] | 아니요 | 최대 20개; 생략·[]이면 검증된 수행 메타데이터에서 기본 후보 생성 |

`MemoryCandidate`는 필수 `draft:MemoryDraft`, `source_refs:RecordRef[]`와 선택 `memory_ref:RecordRef`를 갖는다.
출처는 중복 없는 1~100개이며 `draft`는 위 [기억 초안](#daeri_memory_update)과 같은 타입이다.
새 지식은 `memory_ref`를 생략하고 기존 지식 정정에는 현재 개정 참조를 연결한다.

후보가 없으면 보고의 `met` 완료 검사에 대응하는 실제 호출 중 완료·오류 없음·전체성 확인 조건을 갖춘 출처를 최대 100개 모아 수행 절차 후보 하나를 구성한다.
본문은 리소스 범주·기능과 실제 확인한 수행 사실로 구성하고 원문·수치·SQL·전체 보고는 출처 자료로 관리한다.
생성·저장 모두 현재 승인 계획의 기억 범위와 현재 제한·출처 검사를 통과해야 한다.

새 보고의 계획은 현재 `active_plan_ref`와 일치해야 한다.
이전 근거는 `carried_evidence_refs`로 현재 계획에 명시 승계해 마감 검사에 사용한다.
원 계획·승인 연결과 현재 제공 범위를 유지한다.

저장 계획·승인·외부 호출 이력이 전혀 없는 업무의 `failed|cancelled` 보고에서 두 계획 필드를 함께 null로 사용할 수 있다.
`tables`, `checks`, `artifacts`, `uncertain_effects`는 빈 배열이며 허용된 요청·보완 자료와 실제 계획 검사 실패 또는 취소 이유를 보존한다.
`failed`는 실제 실패한 `local_check`를 연결해 계획 검사 실패로 보고한다.
현재 실패 근거는 업무·요청 키가 연결된 실제 `PLAN_INVALID`의 `local_check` 기록을 사용한다.
입력 형식 오류·관리 CLI의 준비 실패를 업무 근거로 등록하는 경로는 확장 범위다.
`cancelled`는 이미 취소된 업무에서만 허용하며 기존 계획 이력이 있으면 현재 계획을 참조한다.
계획 전 보고의 `validation.completion`은 `unknown`이다.

### 검증과 상태 반영

`paused`에서는 새 보고 제출·산출물 생성을 `TASK_STATE_INVALID`로 거부한다.
기존 요청의 재전달·기록 열람과 이미 시작된 호출의 기록 보존은 현재 제공 권한 안에서 처리한다.

종료 업무의 추가 보고·첨부도 최초 종료 때 확정한 유한 기한 또는 무기한 조건을 유지하며 기여 출처의 더 이른 유한 기한을 승계한다.
만료 후 새 보고 본문·산출물 보존은 요청 ID와 관계없이 `RECORD_EXPIRED`로 반환한다.

실행기는 입력·업무·계획·참조 소유, 완료 조건과 호출·근거·제공 범위를 검사한다.
다른 업무에서 만들어진 기억은 현재 계획의 선택 참조·현재 권한·내보내기 허용·기한을 검사한 뒤 인용한다.
리소스 없는 분석은 등록 요청 자료와 해석·계산 근거에 연결한다.
외부 실행은 실제 호출 기록으로 검증하고 자연어 의미는 호스트가 근거와 대조한다.

`uncertain_effects`는 실제 보존한 같은 업무의 미확인 call ID 집합과 정확히 일치해야 한다.
초안과 실제 기록의 모순·필수 근거 부족 등 검증 실패 시 현재 상태를 유지하고 `RESULT_VALIDATION_FAILED`로 반환한다.

진행 중 호출·미확인 단계·미해소 외부 확인 대상이 있거나 현재 계획의 검증된 승인 이력이 없으면 `succeeded`를 거부한다.
근거 승계·제공 범위 확대는 해당 계획의 승계 검사·현재 권한·실제 승인 조건을 충족해야 한다.
다른 업무 확인은 신규 확인 결과를 근거로 쓰고 원 업무·호출·승인 연결을 유지한다.

불확실성과 발생한 변경을 포함한 `partial|failed` 보고는 상태 조건에 맞춰 저장할 수 있다.
새 표·파일이 포함되면 현재 계획의 실제 승인을 검사하며 없으면 `APPROVAL_REQUIRED`로 거부한다.
활성 업무는 현재 유효한 승인, 종료 업무의 추가 보고는 동일 계획의 실제 승인 이력이 필요하다.
`cancelled` 주장은 이미 취소된 업무에서만 허용한다.
종료 업무의 새 보고는 기존 종료 상태를 유지하고 업무 취소는 취소 도구로 처리한다.

과거 승인 연결·현재 제공 권한·[보고 보존 원칙](../tasks/workflow.md#이력과-완료-근거)을 검사해 로컬 근거를 처리한다.

실행기는 표를 검증하고 그 결과로 같은 보고의 표 인용·완료 조건을 대조한 뒤 보고·파일을 검증한다.
각 생성 파일의 크기는 해당 산출물의 승인 상한 이내여야 한다.
셀·결합·완료 근거·파일 크기 중 하나라도 실패하면 새 표·보고·파일의 등록을 함께 취소한다.
검증 통과 시 표·검증 결과·보고·산출물 참조·업무 상태·요청 기록과 기억 처리 대기를 원자적으로 보존한다.
보고 확정 뒤 [자동 기억 처리](../tasks/memory.md#업무-후-갱신과-정정)를 수행하며 업무 개정·종료 상태를 유지하고 별도 기억 상태를 갱신한다.
보존 실패는 기존 상태를 유지하고 저장소·등록 기록을 확인해 복구한다.
동일 요청 재전달은 기존 보고·산출물 참조를 반환하고 연결된 기억 대기를 재시도한다.
총괄의 다음 동일 profile `daeri_memory_search`도 대기 처리를 재시도한다.

완료 조건의 단계 참조는 인용한 실제 호출 또는 검증표의 기여 단계와 대응해야 한다.
직접 호출과 표의 출처 모두 [단계 대응·승계 기준](plans.md#planstep)을 적용한다.
필수 조건의 `met`는 해당 단계에 대응하고 요구 범위·전체성을 확인한 직접 관찰 또는 검증표를 근거로 한다.
초기 구현은 직접 호출 기록을 `met`의 근거로 쓰는 경우에도 `source_complete=true`를 요구한다.
전체성 확인이 없는 기능 결과는 관찰·부분 보고로 남기고 해당 완료 검사는 `unknown`으로 표시한다.
보고 검증은 구조·출처·검산·허용을 확인하고 자유 문장의 자연어 의미는 호스트가 실제 근거와 대조한다.

### 출력 data

| 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | 보존 업무·개정 |
| `report_ref` | RecordRef | 보고 참조 |
| `table_validations` | TableValidation[] | 파일 여부와 무관한 검증표 참조·검증 결과; 해당 표가 없으면 [] |
| `artifact_refs` | RecordRef[] | 생성·첨부 산출물 |
| `artifact_validations` | ArtifactValidation[] | 파생 표 파일과 검증표의 연결; 해당 파일이 없으면 [] |
| `task_state` | enum | 실제 `succeeded\|partial\|failed\|cancelled` |
| `validation` | ReportValidation | 구조·근거·조건 결과 |
| `delivery_status` | enum | 현재 `not_confirmed`; 전달 확인·실패 갱신은 확장 범위 |
| `memory` | MemoryProcessing | 자동 기억·색인 처리 상태 |

`MemoryProcessing`은 `state`와 `items`를 갖는다.
전체 상태는 `ready|pending|partial|skipped`, 각 항목의 상태는 `ready|pending|skipped`다.
항목은 처리한 `memory_ref` 또는 확인한 `error_code`를 함께 제공할 수 있다.
대기는 재시도할 항목, 부분 상태는 완료·대기·생략이 섞인 결과, 생략은 실제 저장할 항목이 없는 결과다.
기억 처리 실패는 보존한 보고의 성공 응답·종료 상태를 유지하고 이 별도 상태로 표시한다.
내부 후보·대기 기록은 업무 목록·본문 조회의 공개 참조에서 제외하며 직접 조회는 `ACCESS_DENIED`다.

| ReportValidation 필드 | 타입 | 의미 |
|---|---|---|
| `structure` | enum `passed` | 구조 검사 통과 |
| `evidence` | enum `passed` | 보존에 필요한 근거 구조 통과 |
| `completion` | enum `met\|unmet\|unknown` | 필수 조건 충족 여부 |
| `checks` | ReportCheck[] | 실행기가 대조한 조건 결과 |
| `issues` | string[] | 미충족·미확인·제한 |

`evidence=passed`는 보존에 필요한 근거 구조 검사 결과다.
업무 완료 여부는 `completion`과 실제 업무 상태로 확인한다.
업무가 `partial|failed|cancelled`여도 보고를 정상 보존하면 도구는 `status=ok`다.

## daeri_result_get

보존 요청·답변·승인 응답·worker 할당·완료 결과·호출 결과·보고·표·산출물에서 현재 허용 자료를 읽는다.
입력 참조는 해당 업무에 등록한 로컬 기록과 대조한다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | ID | 예 | 현재 열람 가능한 업무 |
| `record_ref` | RecordRef | 예 | 해당 업무의 등록 기록 |
| `cursor` | Cursor | 아니요 | 이전 응답의 연속 조회 참조 |
| `limit_bytes` | PositiveInt | 아니요 | 기본 65,536바이트, 최대 1 MiB |

등록 기록의 종류·소유와 [현재 제공·보존 조건](../tasks/workflow.md#이력과-완료-근거)을 매 페이지 검사한다.
자료가 삭제·만료됐으면 해당 오류와 `data=null`을 반환한다.
만료한 본문·산출물은 `RECORD_EXPIRED`이고 추적 참조·검증 이력은 별도로 조회할 수 있다.
계획 전 요청·검사 기록도 허용 범위에서 읽는다.
계획 본문과 최신 확인 상태는 `daeri_plan_get`으로, 특정 과거 승인 요청·실제 응답은 이 도구의 `approval` 기록으로 읽는다.
과거 승인 기록에도 현재 제공·마스킹·보존 조건을 적용한다.
현재 실행 가능 여부는 현재 승인·권한·상태로 별도 판정한다.
`memory_search`·`memory_update` 기록은 검색 조건·상태·개정 참조 등 허용 메타데이터만 읽는다.
현재 기억 본문은 `daeri_memory_search`로 권한·기한을 다시 검사해 조회한다.
결과 조회는 기존 실행 권한·개정을 유지하며 마스킹 설명·참조도 현재 제공 범위로 검사한다.

| data 필드 | 타입 | 의미 |
|---|---|---|
| `task_id`, `task_revision` | ID, TaskRevision | 조회 업무·개정 |
| `record_kind` | enum | `request\|clarification\|local_check\|memory_search\|memory_update\|worker_assignment\|worker_result\|approval\|call_result\|report\|table\|artifact` |
| `payload` | JsonValue | 허용 JSON·텍스트·표; 없으면 null |
| `next_cursor` | Cursor 또는 null | 후속 조회 참조 |
| `has_more` | boolean | 허용 보존 자료의 후속 조각 |
| `redactions` | string[] | 현재 []; 별도 마스킹 설명의 확장 필드 |
| `evidence_refs` | RecordRef[] | 현재 제공 가능한 근거 참조 |
| `integrity` | enum | 현재 정상 조회는 `intact`; `incomplete\|unavailable`은 부분 복구 확장용 |
| `coverage` | object | 아래 필수 필드 |
| `managed_file` | ArtifactFile 또는 null | 허용된 파일 산출물의 실제 위치·형식; 그 밖에는 null |

`ArtifactFile`의 필수 필드는 `artifact_ref:RecordRef`, `format_id:string`, `media_type:string`, `size_bytes:NonNegativeInt`, `local_path:string`, `expires_at:string|null`이다.
`local_path`는 실행기가 생성·등록한 사용자 영역 파일의 절대 경로다.
현재 내보내기 허용·마스킹·기한·실제 파일 무결성을 확인한 뒤 제공하고 호스트가 지원하는 로컬 파일 링크로 전달한다.
경로 제공에는 실제 파일 전체가 현재 필드 제공 조건을 충족해야 한다.
새 마스킹 파일이 필요하면 승인 범위의 결과 제출 경로로 생성한다.
바이너리 파일은 `payload=null`이며 `managed_file`과 허용 메타데이터로 전달한다.
호스트의 파일 링크 지원·전달 실패는 실제 환경에서 검증한다.
보존·삭제의 관리 범위는 실행기에 등록된 원본 파일이며 사용자가 받은 사본은 사용자가 관리한다.

`coverage`는 `source_complete:boolean|null`, `returned_scope:string`, `missing_scope:string[]`를 갖는다.
원본 완전성을 판단할 수 없으면 `source_complete=null`이다.
표 payload는 열·행을 가진 JSON object, 텍스트는 string이다.

`limit_bytes`는 허용 payload의 직렬화 바이트 상한이다.
문자열은 UTF-8 문자 경계로, JSON은 가장 큰 배열의 원소 경계로 나누고 나머지 객체 구조를 각 페이지에 유지한다.
커서는 업무·기록·위치·현재 제공 본문의 서명에 바인딩한다.
연속 조회의 `limit_bytes`는 허용 범위에서 조정할 수 있다.
변경한 크기에 전체 원본이 한 페이지로 들어가면 커서를 생략해 첫 페이지부터 조회한다.
만료·변조·다른 기록 커서는 `CURSOR_INVALID`다.
단일 JSON 항목 하나가 상한에 들어가지 않으면 `RESULT_TRUNCATED`로 반환하며 더 큰 `limit_bytes`로 재조회한다.

정상 페이지 분할은 `has_more=true`, `status=ok`다.
확인된 원격 잘림은 보존한 부분과 원인을 `partial`로 반환한다.
현재 저장 파일의 유실·무결성 실패는 `RECORD_UNAVAILABLE`, `data=null`이며 부분 파일 복구는 후속 범위다.
원격 조회 누락·잘림은 `RESULT_TRUNCATED`, 보존 손실은 `RECORD_UNAVAILABLE`로 구분한다.
확인된 원격 잘림은 최초 실행·같은 호출의 재전달·보존 결과 조회에서도 유지하고 `missing_scope`에 제한을 표시한다.
정상적인 일부 업무 보고 자체의 조회와 그 근거가 된 잘린 원본 조회는 구분한다.

업무가 `partial`이어도 보존 보고가 온전하면 조회 도구는 `status=ok`다.
부분·오류의 응답 형식과 `isError`는 공통 규격을 따른다.

## 전달과 검증 경계

호스트 전달 증거의 한계와 재전달은 [결과 전달 원칙](../tasks/workflow.md#결과-제공과-전달)을 따른다.
저장별 역할·공개·정리는 [로컬 저장 구조](../configuration/profiles.md#로컬-저장-구조)를 따른다.
로컬 업무·저장·입력 검사와 하위 MCP 프로세스를 통한 승인·조회·보고·파일·기억 갱신의 검증 범위는 [최초 구현 계획](../../plans/initial-implementation.md#현재-구현-단위와-검증-경계)을 따른다.
별도 MCP worker 프로세스의 실행·완료와 root의 보고·파일 통합은 실제 호스트가 네이티브 worker를 생성·연결하는 검증과 구분한다.
실제 호스트 대화·사용자 의도 해석과 사내 리소스의 권한·네트워크는 별도 사용자 환경에서 검증한다.
