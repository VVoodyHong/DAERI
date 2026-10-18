# 리소스 탐색과 기능 실행 MCP 도구

상태: 도구 이름·입출력·실행 경계 설계 제안

등록 기능의 목록·계약 조회와 승인된 실행을 위한 세 도구의 DTO를 정의한다.
공통 타입·입력 검사·응답·오류·멱등성은 [MCP 공통](common.md), 도구 등록·변환은 [리소스 계약](../resources/contract.md)을 따른다.

## `daeri_resource_list`

현재 열람 권한과 업무의 고정 profile에 속한 로컬 등록 인스턴스·기능 요약을 조회한다.
원격 진단·업무 원문 조회는 수행하지 않으므로 계획 승인은 필요하지 않다.
profile이 해소되지 않았으면 `CONTEXT_REQUIRED`를 반환하며 다른 profile의 등록부로 대체하지 않는다.

### 입력

| 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | string | 예 | 열람할 업무 ID |
| `types` | string[] | 아니요 | `DB`, `Cache`, `Infra`, `Log`, `Ticket`, `Mail`, `Source`; 중복 금지 |
| `cursor` | string | 아니요 | 이전 응답의 불투명 목록 커서 |
| `limit` | integer | 아니요 | 1 이상, 로컬 설정의 최대 반환 수 이하 |

`types`를 생략하면 접근 가능한 모든 유형을 대상으로 한다.
빈 배열은 거부하며 `limit` 생략 시 로컬 설정의 기본값을 사용한다.
커서는 업무·주체·필터·등록부 조회 조건에 연결한다.
다른 조건에 재사용하거나 등록부 변경으로 유효성을 잃으면 `CURSOR_INVALID`로 반환한다.

### 반환 `data`

| 필드 | 타입 | 필수 | 내용 |
|---|---|---|---|
| `task_id`, `task_revision` | string, integer | 예 | 업무 ID와 현재 개정 번호 |
| `instances` | InstanceSummary[] | 예 | 권한 필터를 거친 인스턴스 |
| `next_cursor` | string 또는 null | 예 | 다음 목록 페이지 참조 |
| `has_more` | boolean | 예 | 같은 조회에서 추가 페이지가 있는지 여부 |

`InstanceSummary`는 `instance_id: string`, `type: string`, `label: string`, `capability_summaries: CapabilitySummary[]`를 갖는다.
`type`은 입력의 유형 enum을 따른다.
`CapabilitySummary`는 `capability_id: string`, `contract_ref: string`, `description: string`, `changes_state: boolean`을 갖는다.
두 객체의 필드는 모두 필수이며 비어 있는 기능 배열도 허용한다.

등록된 인스턴스가 없으면 `instances: []`, `has_more: false`, `next_cursor: null`인 `ok` 응답이다.
요약의 `changes_state`는 설명용이며 실행 허가나 재시도 안전성을 대신하지 않는다.
목록의 후속 페이지가 있다는 사실은 실제 리소스 데이터의 잘림을 뜻하지 않는다.

## `daeri_capability_get`

업무의 고정 profile·현재 열람 권한 안에서 선택한 기능의 로컬 계약을 반환한다.
계획에 필요한 입력·범위 스키마와 제약을 제공하며 원격 조회·자격 증명 노출은 수행하지 않는다.

### 입력과 반환

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | string | 예 | 업무 ID |
| `instance_id` | string | 예 | 열람 가능한 등록 인스턴스 |
| `capability_id` | string | 예 | 그 인스턴스의 등록 기능 |

반환 `data`는 필수 `task_id: string`, `task_revision: integer`, `descriptor: CapabilityDescriptor`를 갖는다.
존재하지 않거나 열람할 수 없는 대상을 성공한 빈 계약으로 반환하지 않는다.

### `CapabilityDescriptor`

표와 하위 타입의 모든 필드는 필수이며 미지원도 false·null·빈 배열로 명시한다.

| 필드 | 타입 | 제약·의미 |
|---|---|---|
| `instance_id`, `capability_id` | string | 조회한 인스턴스·기능과 일치 |
| `contract_ref` | string | 의미·스키마·제약에 연결된 불변 계약 참조 |
| `description` | string | 기능 목적과 제품별 의미 |
| `input_schema` | JSON Schema object | 구조화된 `parameters`의 검증 기준 |
| `output_schema` | JSON Schema object | 결과 객체의 검증 기준 |
| `scope_schema` | JSON Schema object | 계획 단계의 승인 범위 검증 기준 |
| `supported_purposes` | string[] | `collect`, `change`, `verify`, `reconcile` 중 하나 이상; 중복 금지 |
| `supported_metrics` | MetricDescriptor[] | 기능별 측정·한도 강제 지원 항목 |
| `budget_support` | BudgetSupport | 실제 외부 요청 수 상한의 강제 지원 여부 |
| `effects` | EffectsDescriptor | 업무 상태 변경과 부수 효과 |
| `retry` | RetryDescriptor | 반복 읽기 의미와 서버 재시도 조건 |
| `idempotency` | IdempotencyDescriptor | 원격 중복 방지 보장 |
| `verification` | VerificationDescriptor | 외부 완료를 확인할 기능 |
| `continuation` | ContinuationDescriptor | 페이지·후속 참조와 일관성 |
| `metadata_discovery_allowed` | boolean | 등록된 구조 메타데이터의 사전 노출 허용 여부 |

`metric_id`는 같은 기능 안에서 중복하지 않는다.
등록 시 같은 인스턴스의 동일 metric ID가 같은 단위·측정 의미·집계 범위를 가리키는지 검사한다.
서로 다른 지표에는 다른 ID를 부여하며 `unit`·설명은 비어 있지 않아야 한다.

| 하위 타입 | 필드와 타입 |
|---|---|
| `MetricDescriptor` | `metric_id: string`, `unit: string`, `enforceable: boolean` |
| `BudgetSupport` | `external_requests_enforceable: boolean` |
| `EffectsDescriptor` | `changes_state: boolean`, `side_effects: string[]`, `interruptible: boolean` |
| `RetryDescriptor` | `auto_retry_supported: boolean`, `max_attempts: integer`, `read_repeatability: string`, `retryable_codes: string[]` |
| `IdempotencyDescriptor` | `supported: boolean`, `key_mode: string`, `key_pointer: string/null`, `scope: string`, `validity: string` |
| `VerificationDescriptor` | `supported: boolean`, `capability_ids: string[]`, `external_reference_pointers: string[]` |
| `ContinuationDescriptor` | `supported: boolean`, `cursor_input_pointer: string/null`, `next_cursor_output_pointer: string/null`, `has_more_output_pointer: string/null`, `consistency: string`, `validity: string/null` |

`retry.max_attempts`는 최초 시도를 포함한 1 이상의 상한이며 자동 재시도 미지원이면 1·빈 `retryable_codes`다.
`read_repeatability`는 `snapshot`, `live`, `not_applicable` 중 하나다.

`idempotency.key_mode`는 `none`, `server_generated`, `parameters` 중 하나다.
`parameters`이면 `key_pointer`로 입력 스키마의 위치를 지정하며 다른 모드에서는 null이다.
`supported: false`이면 `key_mode: "none"`이며 `scope`·`validity`에 미지원 한계를 설명한다.
지원하면 `none`을 사용할 수 없다.
`verification.supported: false`이면 두 배열은 비고 true이면 등록된 확인 기능을 하나 이상 지정한다.

`continuation.consistency`는 `snapshot`, `live`, `unspecified` 중 하나다.
페이지 미지원이면 세 pointer와 `validity`는 null이다.
페이지 지원이면 세 pointer와 `validity`는 비어 있지 않은 문자열이다.
pointer는 각 스키마의 존재하는 위치를 가리키는 JSON Pointer다.
출력의 후속 값을 입력에 연결하는 허용 방식은 [계획 규격](plans.md)의 `input_bindings`에 둔다.

세 스키마는 최상위 객체·허용 속성을 정의하고 서버가 검증한 등록부에서 제공한다.
하위 출력이 객체가 아니거나 스키마가 없어도 검증한 정규화 계약으로 연결할 수 있다.
참조 해소·정규화·등록 조건은 [리소스 계약](../resources/contract.md#도구-등록)을 따른다.

`enforceable: false`인 metric을 계획의 강제 한도로 사용하면 실행할 수 없다.
`budget_support.external_requests_enforceable`은 요청 전 통제 또는 검증한 최대 요청 수의 예약이 가능할 때만 true다.
사후 집계·annotation만으로 true를 부여하지 않으며 지원 여부와 숫자·null의 판정은 [예산 규격](plans.md)을 따른다.
`supported_purposes`는 실제 `effects`·`verification` 계약과 일치하도록 등록 시 검증한다.
호스트가 임의로 `reconcile` 목적을 붙여 변경 기능의 실행 권한을 바꾸지 못한다.
`metadata_discovery_allowed`는 등록 메타데이터 노출만 뜻하며 원격 탐색의 계획 승인을 면제하지 않는다.
제품별 실제 권한과 변환 후 범위 검사는 [하위 MCP 승인 검사](../resources/contract.md#계획과-승인-범위)를 따른다.

## `daeri_capability_execute`

저장 계획의 등록 기능을 현재 권한·승인 범위 안에서 실행한다.

### 입력

| 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | string | 예 | 업무 ID |
| `expected_task_revision` | integer | 예 | 1 이상, 새 호출 시작 시 현재 업무 개정과 일치 |
| `plan_id`, `plan_version` | string, integer | 예 | 불변 계획 ID와 1 이상의 버전 |
| `step_id`, `call_id`, `approval_id` | string | 예 | 계획 단계·호출 멱등 키·승인 참조 |
| `instance_id`, `capability_id` | string | 예 | 저장된 계획 단계와 일치 |
| `contract_ref` | string | 예 | 저장된 단계와 현재 등록 계약의 참조 |
| `parameters` | object | 예 | 해당 기능 `input_schema`로 검증 |
| `bindings` | BindingSelection[] | 아니요 | 승인한 후속 값 연결; `binding_id` 중복 금지 |
| `reconcile_of_call_id` | string | 조건부 | `purpose=reconcile` 단계에서 필수; 다른 목적에는 넣지 않음 |
| `reconcile_task_id` | string | 아니요 | 확인 대상의 원 업무; 생략 시 현재 업무, `purpose=reconcile`에서만 허용 |

`BindingSelection`은 필수 `binding_id: string`, `source_call_id: string`만 갖는다.
`call_id`를 멱등 키로 사용하며 별도 `request_id`는 받지 않는다.
재전달과 개정 비교 순서는 [MCP 공통](common.md#업무-개정과-멱등-요청)을 따른다.

새 호출의 단계·목적·분기·의존 관계는 [계획 규격](plans.md)으로 검사한다.
상태·권한·승인·실제 대상 검사와 예약·의도 보존·dispatch는 [실행 제어](../tasks/workflow.md#실행-검사와-기록)를 따른다.

서버는 저장한 `input_bindings`의 허용 source pointer에서 값을 읽어 target pointer에 채운다.
`target_pointer: null`인 바인딩은 조건 판단에만 사용한다.
단계·조건에서 사용하는 모든 바인딩을 선택해야 하며 미정의 바인딩을 거부한다.
같은 source 단계를 참조한 여러 바인딩은 동일 `source_call_id`를 선택한다.

source는 같은 업무의 현재 계획 또는 `carried_evidence_refs`로 승계한 이전 계획의 허용 단계 기록이어야 한다.
서버가 현재 제공 권한·출처를 확인하며 client가 넘긴 값만 믿지 않는다.

`partial`·`pending`에서도 실제 관찰·보존한 확정 필드만 scope 안에서 사용할 수 있다.
빈 실패 결과·미확인 값을 추정해 채우지 않는다.
호스트가 같은 target에 다른 값을 제출하면 `BINDING_INVALID`로 거부한다.
source 결과는 출력 스키마로, 연결한 입력은 입력 스키마와 승인 `scope`로 다시 검사한다.
기본값·후속 값·하위 MCP 인수 변환을 적용한 실제 요청의 대상·연산도 최종 범위 검사에 포함한다.
자료 가림·미확인으로 값을 판단할 수 없으면 분기 조건이 충족됐다고 추정하지 않는다.

### 원 호출과 상태 확인의 연결

`reconcile_of_call_id`는 단계의 승인된 `reconciliation` 대상인 미확인·진행 중 원 호출 하나를 지정한다.
다른 업무면 `reconcile_task_id`와 계획의 `external_call_refs`에 같은 업무·호출을 지정한다.

확인 기능은 원 인스턴스의 `verification.capability_ids`에 등록돼야 한다.
보존한 의도·실제 대상·원격 작업 참조·중복 방지 키 등 등록된 확인 방법으로 원 호출과 연결한다.
현재 자원 상태가 같다는 사실만으로 그 호출의 성공을 확정하지 않는다.

인수는 원 기록에서 채우거나 제출 값과 대조하며 다른 작업·계정·대상은 `BINDING_INVALID`다.
원 기록·연결 근거를 읽지 못하거나 확인 의미를 보장하지 못하면 실행하지 않는다.

서버 보존 원본으로 판단하고 확정 근거·확인 결과·원 호출 상태·외부 효과를 함께 갱신한다.
근거 부족·보존 실패·미확정 소비의 처리와 상태 복귀는 [중단·복구 명세](../tasks/workflow.md#중단재개와-외부-결과-확인)를 따른다.

같은 확인 대상의 다른 요청이 이미 실행 중이면 새 확인을 중복 dispatch하지 않는다.
원 호출이 이미 해소됐다면 `TASK_STATE_INVALID`·`refresh`로 현재 기록을 안내한다.

모든 미확인 호출이 해소되고 현재 상태가 `reconciling`이며 유효 승인·현재 검사를 충족할 때만 저장한 `running|verifying`으로 돌아간다.
늦은 확인 결과는 일시중지·대기·종료 상태를 해제하지 않는다.

다른 업무의 확인은 같은 로컬 저장소의 종료 업무에 한하며 양 업무의 현재 권한을 검사한다.
profile이 달라도 원 인스턴스·실제 대상·권한 문맥의 바인딩이 일치해야 한다.

확인 호출의 사용량·예약은 새 업무, 원 호출의 사용량·예약은 원 업무에 남기고 과거 종료 상태를 유지한다.
확인 근거는 양 업무·원 호출·계획·승인에 연결하고 원 호출 갱신·양 업무 개정과 함께 보존한다.
보존 실패 시 확정 해소를 반환하지 않으며 임의 URL·파일에서 원 기록을 읽지 않는다.

### 반환 `data`

아래 객체를 `CallResult`로 정의한다.

| 필드 | 타입 | 필수 | 내용 |
|---|---|---|---|
| `task_id`, `task_revision` | string, integer | 예 | 업무 ID와 현재 개정 |
| `call_id`, `plan_id`, `plan_version`, `step_id` | string, string, integer, string | 예 | 저장한 호출 연결 |
| `call_state` | string | 예 | `completed`, `blocked`, `failed`, `pending`, `unknown` |
| `external_effect` | string | 예 | `none`, `confirmed`, `unknown` |
| `result_ref` | string 또는 null | 예 | 보존된 결과의 불투명 참조 |
| `inline_result` | object 또는 null | 예 | 제공이 허용된 기능 결과 |
| `usage` | Usage | 예 | 이 호출과 내부 요청의 소비·예약 |
| `budget_remaining` | BudgetRemaining | 예 | 기존 실행을 포함해 계산한 잔여량 |
| `evidence_refs` | string[] | 예 | 제공할 수 있는 근거 기록 참조 |
| `warnings` | string[] | 예 | 잘림·일관성·마스킹 등 해석의 제한 |
| `retry_safety` | RetrySafety | 예 | 현재 서버 재시도 가능 여부와 이유 |
| `reconciliation` | ReconciliationOutcome 또는 null | 예 | 확인 호출의 원 호출 연결·해소 결과; 다른 목적은 null |

`inline_result`가 있으면 해당 `output_schema`를 만족해야 한다.
마스킹 뒤 스키마를 만족할 수 없으면 null로 반환하고 제한을 표시한다.
URL·파일 경로는 검증한 원본 근거의 허용 내용으로만 제공한다.
`external_effect: "none"`은 업무 대상 변경이 없다는 뜻이며 읽기의 부수 효과는 기능 계약에 남긴다.

| 하위 타입 | 필드와 타입 |
|---|---|
| `Usage` | `logical_calls: integer`, `external_requests: integer/null`, `runtime_ms: integer/null`, `metrics: MetricUsage[]` |
| `MetricUsage` | `instance_id: string`, `metric_id: string`, `consumed: number/null`, `reserved: number/null` |
| `BudgetRemaining` | `remaining_tool_calls: integer/null`, `remaining_external_requests: integer/null`, `remaining_runtime_ms: integer/null`, `resource_limits: MetricRemaining[]` |
| `MetricRemaining` | `instance_id: string`, `metric_id: string`, `remaining: number/null` |
| `RetrySafety` | `automatic_retry_allowed: boolean`, `effect_known: boolean`, `reason: string` |
| `ReconciliationOutcome` | `original_task_id: string`, `original_task_revision: integer`, `original_call_id: string`, `resolution: string`, `original_call_state: string`, `original_external_effect: string`, `evidence_refs: string[]` |

이 하위 객체의 필드는 모두 필수이고 숫자는 0 이상이다.
미확정 소비·예약·잔여량은 null이며 0 소비나 예산 해제로 대신하지 않는다.
예산 항목은 [계획 규격](plans.md)의 `max_tool_calls`, `max_external_requests`, `max_runtime_ms`, `resource_limits`와 대응한다.
`logical_calls`로 내부 페이지·재시도의 외부 요청 수를 대신 계산하지 않는다.

`runtime_ms`는 DAERI가 이 호출에 사용한 대기·실행 관리 시간을 표시한다.
원격 작업의 실제 실행 시간이나 종료 보장은 별도 근거로 표시한다.
외부 요청 상한이 없는 계획의 `remaining_external_requests`는 null이며 실제 소비량을 알면 계속 기록한다.

`resolution`은 `resolved`, `pending`, `unknown` 중 하나다.
원 호출의 상태·외부 효과 enum은 위 표를 따르며 반환 시의 보존 값이다.
`original_task_revision`은 공통 `TaskRevision`이다.
`resolved`에는 등록된 확정 근거가 필요하며 원 호출의 `external_effect=unknown`과 함께 반환할 수 없다.
원 호출이 `completed|failed`로 끝났음을 확인해야 `resolved`가 될 수 있다.
`pending|unknown`이면 일부 효과가 관찰돼도 확인 대기를 해소하지 않는다.

원 작업의 실패·취소·변경 없음이 확정돼도 확인 도구는 `ok`일 수 있으며 원 작업 성공을 뜻하지 않는다.

### 결과와 오류

| 상황 | `status` · `call_state` · 오류 |
|---|---|
| 정상 완료 | `ok` · `completed` · null |
| 비동기 접수 | `pending` · `pending` · null |
| 확정된 원격 실패 | `error` · `failed` · `EXTERNAL_EXECUTION_FAILED` |
| 단절·시간 초과 등으로 결과 미확인 | `error` · `unknown` · `EXTERNAL_OUTCOME_UNKNOWN` |
| 일부 결과 잘림 | `partial` · 실제 확인 상태 · `RESULT_TRUNCATED` |

도구·호출·업무의 완료와 외부 효과는 구분하며 접수만 확인한 변경의 효과는 `unknown`이다.
확정 실패보다 효과 미확인이 우선하고 실제 상태 확인은 새 승인된 확인 호출로 수행한다.
검사·저장 실패와 후속 행동은 [공통 오류](common.md#오류와-다음-행동)를 따른다.

## 구현 시 확인

로컬 JSON Schema 검증 범위, 커서 만료·계약 참조 보존과 SDK 제약 강제는 구현에서 확인해야 한다.
