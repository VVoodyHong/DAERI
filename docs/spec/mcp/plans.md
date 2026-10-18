# 계획과 승인 MCP 도구

상태: 도구 이름·입출력·자료형·승인 연결 설계 제안

불변 계획·승인의 세 도구와 `PlanDraft`·`SavedPlan`의 공개 계약을 정의한다.
승인 원칙은 [계획 명세](../tasks/approval.md), 실행 상태는 [업무 흐름](../tasks/workflow.md)을 따른다.
공통 타입·응답·오류·재전달은 [공통 규격](common.md)을 적용하며 실제 구현·호스트 지원은 미검증이다.

## 계획 자료형

모든 객체는 선언한 필드만 허용하며 ID·참조는 공통 규격을 따른다.
설명 문자열은 비어 있지 않아야 하고 배열의 ID·필드 선택자는 중복하지 않는다.
목적·설명·범위·결과 제공 조건에 비밀값이나 인증 입력을 넣지 않는다.

### `PlanDraft`

| 필드 | 타입 | 필수 | 제약·의미 |
|---|---|---|---|
| `purpose` | string | 예 | 업무 요청에 연결된 목적 |
| `steps` | PlanStep[] | 예 | 수행 단계; 리소스 실행이 없으면 빈 배열 |
| `budget` | PlanBudget | 예 | 업무 전체 누적 실행 한도 |
| `output` | PlanOutput | 예 | 보고 형식과 제공 범위 |
| `completion_criteria` | CompletionCriterion[] | 예 | 최소 한 개의 완료 판단 기준 |
| `previous_plan_id` | string | 아니요 | 같은 업무의 현재 계획을 대체할 때 필수 |
| `carried_evidence_refs` | string[] | 아니요 | 승계할 같은 업무의 기존 결과 참조; 기본 [] |

`task_id`와 `expected_task_revision`은 도구 인수에 두며 draft에 중복하지 않는다.
사용할 목적·방법·대상·제공 범위를 기존 업무와 현재 권한에 대조한다.
단순한 목적 설명이나 필드 선택자가 비밀 자료의 반환을 허가하지 않는다.
실패 처리는 [공통 기본 동작](../tasks/workflow.md#일부-조회-실패의-기본-동작)을 따르며 별도 정책 엔진을 추가하지 않는다.

### `PlanStep`

| 필드 | 타입 | 필수 | 제약·의미 |
|---|---|---|---|
| `step_id` | string | 예 | 계획 안에서 고유한 논리 단계 |
| `description` | string | 예 | 사람이 확인할 수행 방법 |
| `purpose` | string | 예 | `collect`, `change`, `verify`, `reconcile` |
| `depends_on` | string[] | 예 | 선행 관찰·입력이 준비되어야 하는 같은 계획의 단계 |
| `instance_id`, `capability_id` | string | 예 | 등록된 인스턴스·기능 한 쌍 |
| `contract_ref` | string | 예 | 계획 작성에 사용한 불변 기능 계약 참조 |
| `scope` | object | 예 | 해당 기능의 `scope_schema`로 검증 |
| `input_bindings` | InputBinding[] | 예 | 저장 결과의 후속 사용; 없으면 빈 배열 |
| `conditions` | StepCondition[] | 아니요 | 충족할 때만 수행하는 단순 조건 |
| `reconciliation` | ReconciliationTargets | 조건부 | `purpose=reconcile`일 때 필수; 다른 목적에는 넣지 않음 |

한 단계는 인스턴스 하나와 기능 하나를 사용하며 승인 범위 안에서 여러 호출을 수행할 수 있다.
기능의 읽기·쓰기·부수 효과는 [기능 계약](resources.md)의 `effects`를 사용한다.
실제 호출 인수는 해당 기능의 `input_schema`와 승인 `scope`를 모두 충족해야 한다.

`purpose`는 기능의 `supported_purposes`와 부작용·검증 계약에 일치해야 한다.
상태별 허용 목적은 [기능 실행 규격](resources.md#daeri_capability_execute)을 따른다.

중복 단계, 존재하지 않는 의존 단계, 자기 참조와 순환을 거부한다.
현재 계획의 바인딩 출처는 의존 관계의 선행 단계여야 한다.
이전 계획의 출처는 `carried_evidence_refs`에 명시한 결과의 실제 단계·호출로만 연결한다.

`depends_on`은 다음 호출에 필요한 준비 조건이며 선행 단계의 모든 호출·페이지 완료를 뜻하지 않는다.
바인딩된 의존 단계는 지정한 출처 호출의 확정·보존된 필드와 조건을 검사하고, 바인딩 없는 순서 의존은 선행 단계의 정상 완료 호출 근거를 요구한다.
전체 자료 확보를 전제로 하는 연산은 기능 계약의 완전성 근거도 확인하며 근거가 없으면 실행하지 않는다.
업무 전체의 대상·페이지 완전성은 완료 조건과 보고에서 별도로 검증한다.
`reconcile` 단계의 의존성은 아래 원 호출 확인 규칙을 우선한다.

등록되지 않은 기능·계약 불일치·강제할 수 없는 범위는 `PLAN_INVALID`다.

`scope`는 자유 SQL·shell 명령·임의 인증·endpoint를 전달하는 통로가 아니다.
기간·필드·대상·필터 등 기능 계약이 검증하고 실행 중 강제할 조건만 담는다.
계약이나 실제 대상이 승인한 방법·범위에 영향을 주게 바뀌면 기존 승인으로 실행하지 않는다.

제품 고유 SQL·명령·API 인수는 [등록된 기능](../resources/contract.md#기능-설계-단위)의 `parameters`로 전달하도록 설계한다.
자료형 검사와 실제 대상·효과·허용 범위 검사를 같은 보장으로 취급하지 않는다.

### 미확인 호출의 확인 대상

`ReconciliationTargets`는 필수 `source_step_ids: string[]`, `prior_call_ids: string[]`, `external_call_refs: CallRef[]`를 갖는다.
각 배열은 중복을 금지하며 셋 중 하나에는 최소 한 항목이 있어야 한다.
같은 계획의 원 호출은 `source_step_ids`, 이전 계획의 원 호출은 정확한 `prior_call_ids`로 승인받는다.

`CallRef`는 필수 `task_id: string`, `call_id: string`으로 원 업무·호출을 지정한다.
`external_call_refs`는 같은 로컬 저장소의 종료 업무에 남은 미확인·진행 중 호출을 새 업무에서 확인할 때 사용한다.
두 업무의 현재 권한과 원 기록을 확인하고 원 대상·이미 발생한 효과·확인 범위를 새 계획에 함께 표시한다.

`source_step_ids`는 현재 계획의 다른 단계이며 `depends_on`에도 포함한다.
확인 단계의 의존성은 원 호출의 외부 완료 대신 실행 의도의 보존과 실제 요청 여부를 검사한다.
원 호출이 미확인이라는 이유로 승인된 확인 단계까지 막지 않는다.

`prior_call_ids`는 같은 업무의 이전 계획에 속한 미확인·진행 중 호출이어야 한다.
사용자에게 그 호출의 계획·대상·이미 요청한 변경과 남은 불확실성을 함께 표시한다.
완료 결과가 없어도 보존한 의도로 대조할 수 있지만 원 기록을 읽을 수 없으면 확인을 실행하지 않는다.

실행 시 `reconcile_of_call_id`와 필요하면 `reconcile_task_id`로 이 범위 안의 원 호출 하나를 지정한다.
원 기능의 `verification` 계약과 확인 기능, 실제 대상·원격 작업 참조가 대응해야 한다.
일반 상태 관찰이나 모델의 요약을 특정 원 호출의 완료 증거로 바꾸지 않는다.

이전 호출을 확인 대상으로 넣었다고 그 호출의 변경을 다시 승인하거나 결과 전체의 후속 사용을 허용하지 않는다.
자료 승계는 아래 `carried_evidence_refs`를 별도로 검사한다.
다른 업무의 확인 결과·예산·종료 상태를 다루는 방식은 [실행 연결 규격](resources.md#원-호출과-상태-확인의-연결)을 따른다.

### 결과 바인딩과 조건

| InputBinding 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `binding_id` | string | 예 | 계획 안에서 고유한 연결 ID |
| `source_step_id` | string | 예 | 선행 단계 또는 명시적으로 승계한 결과의 단계 |
| `source_pointer` | string | 예 | 기능 출력에서 읽을 JSON Pointer |
| `target_pointer` | string 또는 null | 예 | 기능 입력 위치; null은 조건 판단 전용 |

JSON Pointer는 JSON 객체의 값 위치를 가리키며 스키마가 허용한 위치만 참조한다.
입력용 `target_pointer`는 비어 있지 않아야 하고 한 단계의 대상 위치끼리 충돌할 수 없다.
인증·인스턴스·기능·승인·예산 필드를 결과 바인딩으로 바꾸지 않는다.

| StepCondition 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `binding_id` | string | 예 | 같은 단계의 결과 바인딩 |
| `operator` | string | 예 | `exists`, `eq`, `in` |
| `value` | scalar 또는 scalar[] | 조건부 | `eq`는 scalar, `in`은 비어 있지 않은 scalar[] |

scalar는 JSON의 string·number·boolean·null이며 객체나 실행 표현식을 받지 않는다.
`exists`에는 `value`를 넣지 않고, 여러 조건은 모두 충족해야 한다.

`exists`는 실제로 확인한 필드의 존재·부재를 판단하며 `eq`·`in`은 동일 JSON 타입의 scalar를 비교한다.
자료 누락·가림 등으로 값이나 부재를 확인할 수 없으면 `BINDING_INVALID`로 멈춘다.
null도 실제 확인한 값일 때만 비교하며 추정한 null을 사용하지 않는다.

거짓인 조건은 외부 실행 없이 `CONDITION_NOT_MET`·`fix_input`으로 반환한다.
호스트는 다른 승인된 단계를 선택할 수 있으며 조건을 생략해 재시도하지 않는다.

실행 도구의 `bindings[{binding_id, source_call_id}]`로 실제 출처 호출을 고른다.
같은 출처 단계의 여러 바인딩은 이번 호출에서 같은 `source_call_id`를 사용한다.

서버가 허용된 저장 결과를 해석하고 출력·입력 스키마, 현재 제공 권한과 범위를 다시 검사한다.
모델이 전달한 값이 저장 결과를 대신하지 않으며 실제 해석 규칙은 [기능 실행 규격](resources.md)을 따른다.

`previous_plan_id`만으로 이전 결과 전체의 후속 사용을 허용하지 않는다.
`carried_evidence_refs`에 정확한 결과 참조를 담아 새 계획과 함께 검토한다.
승계한 결과도 출처·현재 제공 권한·새 scope를 검사하며 미확인 효과를 완료 근거로 바꾸지 않는다.

### 예산·보고·완료 조건

| PlanBudget 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `max_tool_calls` | integer | 예 | 0 이상; 논리 기능 호출의 누적 한도 |
| `max_external_requests` | integer 또는 null | 예 | 숫자는 실제 외부 요청의 누적 한도; null은 이 상한을 요청하지 않음 |
| `max_runtime_ms` | integer | 예 | 0 이상; DAERI의 호출 대기·실행 관리 시간의 누적 한도 |
| `resource_limits` | ResourceLimit[] | 예 | 기능별 자료·변경량 한도; 없으면 빈 배열 |

`ResourceLimit`의 필수 필드는 `instance_id: string`, `metric_id: string`, `maximum: number`다.
`maximum`은 0 이상이며 인스턴스·metric 조합은 중복하지 않는다.

관련 기능이 metric의 측정·강제를 지원해야 하며 동일 metric의 단위·집계 의미는 [기능 계약](resources.md#capabilitydescriptor)을 따른다.

0인 한도는 해당 실행을 허용하지 않는 의미이며 무제한으로 해석하지 않는다.
논리 호출 수는 서로 다른 기능 실행 호출을 세며 계획·조회 도구나 같은 요청의 재전달은 세지 않는다.

외부 요청 수에는 내부 페이지·재시도·상태 조회를 포함하고 승인 대기 시간은 실행 시간에서 제외한다.
실제 예약·소비량 계산은 [실행 제어](../tasks/workflow.md#실행-검사와-기록)를 따른다.

하위 MCP의 내부 요청 수를 보장할 수 없으면 `max_external_requests: null`인 계획을 사용할 수 있다.
확인 화면에는 실제 외부 요청 수의 상한이 없고 사용량도 미측정일 수 있음을 표시한다.
이 값은 0 요청·승인 면제·자료 범위 확대를 뜻하지 않는다.

숫자 상한을 요청하면 모든 실행 단계가 `budget_support.external_requests_enforceable=true`를 충족해야 한다.
미지원이면 `PLAN_INVALID`로 보완을 요구하며 실행 시에도 강제 불가 조건을 거부한다.
동일 업무의 과거 외부 소비량이 미확정이면 이를 0으로 바꿔 새 숫자 상한을 적용하지 않는다.

`max_runtime_ms`가 끝나면 DAERI의 추가 dispatch와 계속 기다리는 처리를 중단한다.
이미 요청한 원격 작업의 실제 종료·CPU 사용·과금 시간까지 같은 한도로 보장하지 않는다.
원격 작업 시간의 강제 상한이 필요하면 지원하는 `resource_limits` 항목으로 별도 승인받는다.

| PlanOutput 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `format` | string | 예 | 로컬 실행기가 지원할 보고 형식 |
| `destination` | string | 예 | 사람이 이해할 전달 대상 설명 |
| `resource_rules` | ResourceOutputRule[] | 예 | 업무 계획마다 승인할 리소스별 제공 범위 |

| ResourceOutputRule 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `instance_id` | string | 예 | 고정 profile 안의 원본 리소스; 중복 금지 |
| `allowed_model_fields` | string[] | 예 | 해당 리소스 결과 중 모델에 제공할 필드 |
| `export_fields` | string[] | 예 | 해당 리소스 결과 중 산출물에 포함할 필드 |
| `retained_fields` | string[] | 예 | 업무 종료 후 로컬에 유지할 최소 조회 본문 필드 |
| `masking` | MaskingRule[] | 예 | 해당 리소스의 가림 조건; 없으면 빈 배열 |

실행 단계와 승계 근거의 리소스마다 규칙을 하나씩 명시하고 확인 화면에서도 리소스별로 표시한다.
원본 기록의 인스턴스로 규칙을 선택하며 다른 리소스의 같은 필드명에 적용하지 않는다.
빈 필드 배열은 해당 모델 제공·내보내기·종료 후 본문 보관을 허용하지 않으며 규칙·출처를 확인할 수 없으면 허용하지 않는다.
리소스 호출·승계 근거가 없는 계획에서는 `resource_rules: []`를 사용하고 요청 자료의 등록 제공 정책을 적용한다.
승계 근거는 원본 보관·제공 조건·현재 권한·새 계획의 해당 규칙을 모두 충족해야 한다.

`retained_fields`는 종료 후 본문 보관 범위이며 모델 제공·내보내기 허용을 뜻하지 않는다.
현재 보관 권한과 비밀 제거·마스킹을 적용하며 이력·활성 업무 복구 자료의 보존은 [업무 명세](../tasks/workflow.md#이력과-완료-근거)를 따른다.
실행기는 고정 profile에서 [공통 보존기간](../tasks/workflow.md#이력과-완료-근거)을 해소해 표시하며 모델 인수로 기간을 바꾸지 않는다.
같은 기간을 적용해도 조회 보관·보고·내보내기의 필드별 허용 범위는 별개다.

필드 선택자는 기능 결과의 JSON Pointer이며 wildcard나 자유 선택식을 받지 않는다.
`MaskingRule`의 필수 필드는 `field: string`, `action: "omit" | "redact"`다.
`field`도 JSON Pointer이며 같은 필드의 충돌한 규칙을 거부한다.

선택자에 포함된 영역도 현재 열람 권한·비밀 제외·마스킹 조건을 만족해야 반환한다.

`destination`은 경로나 URL·인증 참조를 실행하는 지시가 아니다.
메일 전송·외부 게시처럼 실제 외부 전달이 필요하면 별도의 변경 기능 단계로 승인받는다.
필드·마스킹의 제품 의미는 [결과·이력 명세](../tasks/workflow.md#이력과-완료-근거)를 따른다.

| CompletionCriterion 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `criterion_id` | string | 예 | 계획 안에서 고유한 완료 조건 |
| `description` | string | 예 | 무엇을 어떤 근거로 확인할지 설명 |
| `required` | boolean | 예 | 전체 완료에 필요한 조건인지 여부 |
| `step_ids` | string[] | 예 | 근거를 만들 단계; 모두 계획에 존재 |

`steps: []`에서도 보고·제공 범위와 완료 조건은 생략하지 않는다.
`step_ids: []`인 조건은 요청 자료나 저장 기록에 근거한 검증에만 사용한다.
외부 변경이 성공했다는 증거를 이 빈 참조로 대신하지 않는다.

필수 조건의 적용 제외는 승인된 분기 조건이 거짓임을 확인한 경우처럼 검증 가능한 근거가 있어야 한다.
모델의 이유 설명만으로 `required` 조건을 제외하지 않는다.

### 저장 계획과 예산 맥락

| SavedPlan 필드 | 타입 | 필수 | 내용 |
|---|---|---|---|
| `plan_id`, `plan_version` | string, integer | 예 | 불변 계획 ID와 업무 내 증가하는 1 이상 버전 |
| `context` | TaskContext | 예 | 계획 저장 시의 고정 profile 구성; [업무 DTO](tasks.md#taskcontext)와 같은 타입 |
| `draft` | PlanDraft | 예 | 검증하고 보존한 계획 원본 |
| `contract_refs` | string[] | 예 | 단계에서 사용한 중복 없는 계약 참조 |
| `render_ref` | string | 예 | 원본 전체 조건에 대응하는 불변 표시본 참조 |
| `is_active` | boolean | 예 | 현재 업무의 실행 대상 계획인지 여부 |
| `approval_request` | ApprovalOutcome 또는 null | 예 | 연결된 최신 확인 요청; 없으면 null |
| `budget_context` | BudgetContext | 예 | 이전 한도와 실제 소비·예약·남은 양 |

`context`, `draft`, `contract_refs`와 `render_ref`는 저장 후 수정하지 않는다.
`is_active`·승인 요청 상태·예산 맥락은 조회 당시 기록이며 계획 내용과 구분한다.
표시본은 [승인 화면 계약](../hosts/approval.md#화면과-사용자-흐름)의 전체 조건에 대응하며 비밀값·내부 credential 참조를 제외한다.

`BudgetContext`의 필수 필드는 `previous_budget: PlanBudget/null`, `consumed: BudgetAmounts`, `reserved: BudgetAmounts`, `remaining: BudgetRemaining`다.
`BudgetRemaining`은 [기능 실행 규격](resources.md)의 자료형을 사용한다.

`BudgetAmounts`는 `tool_calls: integer/null`, `external_requests: integer/null`, `runtime_ms: integer/null`, `resource_limits: MetricAmount[]`를 모두 갖는다.
`MetricAmount`는 필수 `instance_id: string`, `metric_id: string`, `amount: number/null`을 갖는다.

소비·예약은 0 이상이며 미확정량은 null로 표시한다.
새 총 한도에서 누적 소비량과 미정산 예약량을 모두 차감하고 계획 교체로 초기화하지 않는다.
승인 전의 `remaining`은 새 총량을 기준으로 계산한 검토용 값이며 실행 허가가 아니다.

외부 요청 상한이 null이면 그 항목의 잔여량도 null이다.
미확정 소비·예약의 null과 구분할 때는 원본 `PlanBudget`의 상한 값을 함께 확인한다.
숫자 상한을 없애는 변경도 제한 완화로 표시해 재승인받고 기존 기록은 보존한다.

## `daeri_plan_submit`

호스트가 만든 계획을 검사하고 불변 원본과 표시본을 보존한다.
계획 저장은 실행 승인이 아니며 초기 흐름은 `planning → awaiting_approval`다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | string | 예 | 공통 내용 멱등 키 |
| `task_id` | string | 예 | 제출할 업무 |
| `expected_task_revision` | integer | 예 | 1 이상; 새 제출의 현재 개정과 일치 |
| `draft` | PlanDraft | 예 | 위 자료형과 등록 기능 계약으로 검증 |

반환 `data`의 필수 필드는 `task_id`, `task_revision`, `plan_id`, `plan_version`, `contract_refs`, `render_ref`, `budget_context`다.
각 타입은 공통 규격과 `SavedPlan` 표를 따른다.
성공은 `status: "ok"`이며 최초 제출 또는 교체를 기록할 때 개정을 한 번 증가시킨다.
실행기는 업무의 `TaskContext`를 SavedPlan에 연결하며 입력으로 다른 profile이나 스냅샷 참조를 받지 않는다.
profile 맥락 미해소, 스냅샷 밖 인스턴스, 연결·인증의 미준비 상태는 실행 가능한 계획으로 저장하지 않는다.
원격 연결이나 로그인을 계획 검사에서 암묵적으로 수행하지 않는다.

교체는 현재 계획을 `previous_plan_id`로 연결하는 재계획 경로에서만 허용한다.
`paused`에서 새 계획 제출은 `TASK_STATE_INVALID`이며 먼저 업무 재개로 상태·기존 계획을 검토한다.
진행 중 호출·미확인 외부 효과를 숨기거나 새 변경을 재실행할 근거로 사용하지 않는다.
상태 확인의 승인·예산이 부족하면 필요한 `reconcile` 단계의 새 계획을 제출해 확인받을 수 있다.
종료된 업무는 `TASK_STATE_INVALID`로 거부하며 기존 계획은 계속 열람할 수 있다.

교체를 보존할 때 이전 승인과 확인 요청으로 새 호출을 시작하지 못하게 한다.
[재계획 승계 원칙](../tasks/approval.md#수정-계획에서-작업과-예산-승계-제안)에 따라 근거·사용량을 유지하며 멱등 처리는 공통 규격을 따른다.

## `daeri_plan_get`

저장한 계획과 현재 확인 상태·예산 맥락을 로컬 기록에서 읽는다.
현재 업무·계획 열람 권한과 제공 범위를 검사하며 원격 상태 조회나 실행을 시작하지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | string | 예 | 열람할 업무 |
| `plan_id` | string | 예 | 같은 업무에 저장된 특정 계획 |

반환 `data`는 필수 `task_id: string`, `task_revision: integer`, `plan: SavedPlan`을 갖는 `ok` 응답이다.
과거 계획도 열람할 수 있지만 `is_active: false`는 실행 허가가 아니다.
조회는 개정을 증가시키지 않으며 존재하지 않거나 접근할 수 없는 계획을 빈 성공으로 반환하지 않는다.

## `daeri_approval_request`

저장한 현재 계획을 실제 사용자에게 표시하고 응답을 같은 불변 계획에 연결한다.
모델이 승인 응답을 제출하는 별도 도구는 두지 않는다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | string | 예 | 공통 내용 멱등 키 |
| `task_id` | string | 예 | 확인할 업무 |
| `expected_task_revision` | integer | 예 | 1 이상; 새 요청의 현재 개정과 일치 |
| `plan_id`, `plan_version` | string, integer | 예 | 현재 저장 계획과 1 이상의 버전 |

`approved`, `user_decision`이나 확인 주체를 입력으로 받지 않는다.
새 확인 요청은 `awaiting_approval`에서만 허용하며 `paused`·종료 상태 등에서는 `TASK_STATE_INVALID`로 거부한다.
최종 응답 보존 때도 현재 상태·요청 유효성을 다시 검사하고 이전 응답이 일시중지·종료를 해제하지 못하게 한다.
현재 계획·승인 요청 권한·표시본의 동일성을 확인한 뒤 확인 요청을 보존한다.
새 요청 생성과 검증된 최종 결정 기록은 각각 업무 개정을 증가시킨다.

### `ApprovalOutcome`과 실제 응답

반환 `data`는 필수 `task_id: string`, `task_revision: integer`와 아래 필드를 갖는다.
`SavedPlan.approval_request`에는 아래 필드만 연결한다.

| 필드 | 타입 | 필수 | 내용 |
|---|---|---|---|
| `approval_request_id` | string | 예 | 저장한 확인 요청 ID |
| `state` | string | 예 | `awaiting_response`, `approved`, `rejected`, `cancelled`, `invalidated` |
| `approval_id` | string | 조건부 | `approved`에서만 반환 |
| `decision` | string 또는 null | 예 | `approve`, `request_changes`, `decline`, `cancel` 또는 결정 전 null |
| `feedback` | string 또는 null | 예 | 비밀을 제거한 UI 설명; `request_changes`이면 비어 있지 않음 |

대기는 `pending`, 보존한 승인·거절·명시적 확인 취소는 `ok`다.
무효 요청·응답 검증 실패는 `error`이며 실행 권한을 부여하지 않는다.
도구의 정상 처리가 업무 성공·업무 취소를 뜻하지 않는다.

실제 응답의 출처·서명·유효성은 [호스트 승인 계약](../hosts/approval.md#승인-응답-검증)으로 검증한다.
결정과 업무 전이는 다음과 같으며 `feedback`은 데이터로 보존하고 새 계획에 자동 적용하지 않는다.

| `decision` | 요청 `state` | 검증·보존 후 업무 상태 |
|---|---|---|
| `approve` | `approved` | `running`; 미확인 효과가 있으면 `reconciling` |
| `request_changes`, `decline` | `rejected` | `planning`; 조건 수정은 새 계획으로 제출 |
| `cancel` | `cancelled` | `awaiting_approval`; 업무 취소와 구분 |

브라우저 닫기·인증기 취소·연결 손실은 결정 제출이 아니며 `decision=null`이다.
요청·인증 시도 기한은 실행 승인 만료와 구분한다.

동일 요청 재전달은 기존 상태를 조회하며 UI를 다시 열거나 승인·예산을 추가하지 않는다.
현재 계획의 유효 대기 요청이 있으면 다른 ID로 확인창을 중복 생성하지 않는다.
취소·교체·만료·철회·일시중지로 무효화한 요청과 서명 시도는 `invalidated`로 반환하고 실행 가능한 `approval_id`를 재전달하지 않는다.
최종 결정은 현재 업무·요청 유효성을 다시 검사해 한 번만 기록한다.

## 오류와 호스트 연결의 경계

출처·주체·표시 동일성을 검증할 수 없으면 `APPROVAL_UNAVAILABLE`로 반환한다.
권한을 부여하지 않고 현재 대기·일시중지·종료 상태를 유지하며 [호스트 검증 범위](../hosts/approval.md#구현-시-검증)를 구현 때 확인한다.
