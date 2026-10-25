# 리소스 탐색과 기능 실행 MCP 도구

상태: 세 리소스 도구와 읽기 실행 계약 구현; 실제 조직 리소스의 권한·연결은 별도 검증

등록 기능의 목록·계약 조회와 승인된 실행을 위한 세 도구의 DTO를 정의한다.
공통 타입·입력 검사·응답·오류·멱등성은 [MCP 공통](common.md), 도구 등록·변환은 [리소스 계약](../resources/contract.md)을 따른다.

## `daeri_resource_list`

현재 열람 권한과 업무의 고정 profile에 속한 로컬 등록 인스턴스·기능 요약을 조회한다.
계획 전 로컬 등록 정보 조회에 사용한다.
profile 미해소 시 `CONTEXT_REQUIRED`로 반환해 profile 선택을 요구한다.

### 입력

| 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | string | 예 | 열람할 업무 ID |
| `types` | string[] | 아니요 | 고유한 등록 리소스 범주 ID |
| `cursor` | string | 아니요 | 이전 응답의 불투명 목록 커서 |
| `limit` | integer | 아니요 | 기본 20, 범위 1~100 |

`types`를 생략하면 접근 가능한 모든 유형을 대상으로 한다.
현재 내장 연결의 범주는 DB·Cache·Infra·Source·Document·Issue다.
범주 ID는 등록 어댑터에서 결정하며 새 범주는 해당 어댑터·카탈로그 등록으로 확장한다.
빈 배열은 거부한다.
현재 목록 커서는 업무·유형 필터·인스턴스 및 기능 요약의 서명에 연결한다.
다른 조건에 재사용하거나 등록부 변경으로 유효성을 잃으면 `CURSOR_INVALID`로 반환한다.
생명주기와 프로세스 경계는 [공통 커서 규격](common.md#업무-개정과-멱등-요청)을 따른다.

### 반환 `data`

| 필드 | 타입 | 필수 | 내용 |
|---|---|---|---|
| `task_id`, `task_revision` | string, integer | 예 | 업무 ID와 현재 개정 번호 |
| `instances` | InstanceSummary[] | 예 | 권한 필터를 거친 인스턴스 |
| `next_cursor` | string 또는 null | 예 | 다음 목록 페이지 참조 |
| `has_more` | boolean | 예 | 같은 조회에서 추가 페이지가 있는지 여부 |

`InstanceSummary`는 `instance_id: string`, `type: string`, `label: string`, `capability_summaries: CapabilitySummary[]`를 갖는다.
`type`은 등록된 리소스 범주 ID를 사용한다.
`CapabilitySummary`는 `capability_id: string`, `contract_ref: string`, `description: string`, `changes_state: boolean`을 갖는다.
두 객체의 필드는 모두 필수이며 비어 있는 기능 배열도 허용한다.
준비된 인스턴스에는 개별 도구와 리소스 읽기 묶음 `*`의 요약을 함께 제공한다.
미진단·변경·사용 불가 등록은 해당 인스턴스를 유지하고 `capability_summaries: []`로 표시한다.
실제 인증·연결의 준비 상태는 관리 진단과 실행 경계에서 확인한다.

등록된 인스턴스가 없으면 `instances: []`, `has_more: false`, `next_cursor: null`인 `ok` 응답이다.
요약의 `changes_state`는 효과 설명이며 실제 호출은 승인·권한·재시도 계약으로 검사한다.
목록 페이지는 등록 인스턴스 목록의 분할 상태다.
실제 리소스 데이터의 완전성은 기능 실행 결과에서 확인한다.

## `daeri_capability_get`

업무의 고정 profile·현재 열람 권한 안에서 선택한 기능의 로컬 계약을 반환한다.
계획에 필요한 입력·범위 스키마와 제약을 로컬 등록 기록에서 제공한다.

### 입력과 반환

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | string | 예 | 업무 ID |
| `instance_id` | string | 예 | 열람 가능한 등록 인스턴스 |
| `capability_id` | string | 예 | 그 인스턴스의 등록 기능 또는 읽기 묶음 `*` |

반환 `data`는 필수 `task_id: string`, `task_revision: integer`, `descriptor: CapabilityDescriptor`를 갖는다.
대상 부재·열람 권한 부족은 각각의 오류로 반환한다.

### `CapabilityDescriptor`

표와 하위 타입의 모든 필드는 필수이며 미지원도 false·null·빈 배열로 명시한다.
현재 기본 어댑터는 `collect`·`verify`를 지원하며 `supported_metrics: []`와 `external_requests_enforceable: false`를 반환한다.
자동 재시도·원격 중복 억제·완료 확인·자동 페이지 연결은 미지원 값으로 제공한다.
아래의 지원 시 조건은 해당 기능을 추가할 때 적용하는 확장 계약이다.

| 필드 | 타입 | 제약·의미 |
|---|---|---|
| `instance_id`, `capability_id` | string | 조회한 인스턴스·기능과 일치 |
| `contract_ref` | string | 의미·스키마·제약에 연결된 불변 계약 참조 |
| `description` | string | 기능 목적과 제품별 의미 |
| `input_schema` | object | 구조화된 `parameters`의 검증 기준 |
| `output_schema` | object | 결과 객체의 검증 기준 |
| `scope_schema` | object | 계획 단계의 승인 범위 검증 기준 |
| `supported_purposes` | string[] | `collect`, `change`, `verify`, `reconcile` 중 고유한 하나 이상 |
| `supported_metrics` | MetricDescriptor[] | 기능별 측정·한도 강제 지원 항목 |
| `budget_support` | BudgetSupport | 실제 외부 요청 수 상한의 강제 지원 여부 |
| `effects` | EffectsDescriptor | 업무 상태 변경과 부수 효과 |
| `retry` | RetryDescriptor | 반복 읽기 의미와 서버 재시도 조건 |
| `idempotency` | IdempotencyDescriptor | 원격 중복 방지 보장 |
| `verification` | VerificationDescriptor | 외부 완료를 확인할 기능 |
| `continuation` | ContinuationDescriptor | 페이지·후속 참조와 일관성 |
| `metadata_discovery_allowed` | boolean | 등록된 구조 메타데이터의 사전 노출 허용 여부 |

metric을 지원하는 등록 계약에서 `metric_id`는 같은 기능 안에서 고유해야 한다.
같은 인스턴스의 동일 metric ID는 같은 단위·측정 의미·집계 범위를 가리켜야 한다.
서로 다른 지표에는 다른 ID를 부여하며 `unit`·`description`은 비어 있지 않아야 한다.
`description`에는 측정 대상·집계 범위와 페이지·재시도의 합산 방식을 설명한다.

| 하위 타입 | 필드와 타입 |
|---|---|
| `MetricDescriptor` | `metric_id: string`, `unit: string`, `description: string`, `enforceable: boolean` |
| `BudgetSupport` | `external_requests_enforceable: boolean` |
| `EffectsDescriptor` | `changes_state: boolean`, `side_effects: string[]`, `interruptible: boolean` |
| `RetryDescriptor` | `auto_retry_supported: boolean`, `max_attempts: integer`, `read_repeatability: string`, `retryable_codes: string[]` |
| `IdempotencyDescriptor` | `supported: boolean`, `key_mode: string`, `key_pointer: JsonPointer/null`, `scope: string`, `validity: string` |
| `VerificationDescriptor` | `supported: boolean`, `capability_ids: string[]`, `external_reference_pointers: JsonPointer[]` |
| `ContinuationDescriptor` | `supported: boolean`, `cursor_input_pointer: JsonPointer/null`, `next_cursor_output_pointer: JsonPointer/null`, `has_more_output_pointer: JsonPointer/null`, `consistency: string`, `validity: string/null` |

`retry.max_attempts`는 최초 시도를 포함한 1 이상의 상한이며 자동 재시도 미지원이면 1·빈 `retryable_codes`다.
`read_repeatability`는 `snapshot`, `live`, `not_applicable` 중 하나다.

`idempotency.key_mode`는 `none`, `server_generated`, `parameters` 중 하나다.
`parameters`이면 `key_pointer`로 입력 스키마가 허용한 `parameters`의 위치를 지정하며 다른 모드에서는 null이다.
`supported: false`이면 `key_mode: "none"`이며 `scope`·`validity`에 미지원 한계를 설명한다.
지원하는 키 모드는 `server_generated` 또는 `parameters`다.
`verification.supported: false`이면 두 배열은 비고 true이면 등록된 확인 기능을 하나 이상 지정한다.

`continuation.consistency`는 `snapshot`, `live`, `unspecified` 중 하나다.
`continuation.supported: false`이면 세 pointer와 `validity`는 null이다.
`continuation.supported: true`이면 세 pointer와 `validity`는 비어 있지 않은 문자열이다.
이 값은 실행기의 후속 값 연결 지원이며 하위 도구가 제공하는 페이지 인수와 구분한다.
pointer는 등록 계약에서 허용한 단일 필드 위치를 가리키며 [공통 필드 경로](common.md#필드-경로와-선택)를 따른다.
입력 pointer의 기준은 `parameters`, 출력 pointer의 기준은 정규화한 기능 결과 객체다.
출력의 후속 값을 입력에 연결하는 허용 방식은 [계획 규격](plans.md)의 `input_bindings`에 둔다.

세 스키마는 최상위 객체인 JSON Schema이며 서버가 검증한 등록부에서 제공한다.
`input_schema`는 진단한 하위 도구의 계약이고 `output_schema`는 호스트에 제공할 결과, `scope_schema`는 실행기가 해석하는 승인 조건을 정의한다.
`scope_schema.x-daeri-access-constraints`에는 해당 도구의 관리자 제한을 함께 제공한다.
하위 출력이 객체가 아니거나 스키마가 없어도 검증한 정규화 계약으로 연결할 수 있다.
참조 해소·정규화·등록 조건은 [리소스 계약](../resources/contract.md#도구-등록)을 따른다.

계획의 강제 한도에는 `enforceable: true`인 metric이 필요하다.
`budget_support.external_requests_enforceable`은 요청 전 통제 또는 검증한 최대 요청 수의 예약이 가능할 때만 true다.
지원 여부와 숫자·null의 판정은 [예산 규격](plans.md)을 따른다.
추가 실행 목적은 실제 `effects`·`verification` 계약의 검사와 함께 등록한다.
실행 목적은 등록 기능의 허용 목적·효과·현재 계획에 대응해야 한다.
`metadata_discovery_allowed`는 등록 메타데이터 노출 여부이며 원격 탐색은 별도 승인된 기능 실행으로 처리한다.
제품별 실제 권한과 변환 후 범위 검사는 [실제 요청 정규화·판정](../resources/contract.md#실제-요청-정규화와-범위-판정)을 따른다.

### 리소스 읽기 묶음

`capability_id: "*"`는 해당 연결에서 현재 진단한 읽기 기능 전체를 승인할 때 사용한다.
`CapabilityDescriptor`의 공통 필드에 다음 필드를 추가한다.

| 필드 | 타입 | 내용 |
|---|---|---|
| `capability_refs` | object[] | ID순으로 정렬한 구성 기능의 `capability_id`, `contract_ref` 쌍 |
| `capability_constraints` | object | 구성 기능 ID별 관리자 제한 |

묶음 `contract_ref`는 구성 기능의 ID·계약 참조에 연결한 지문이다.
각 개별 참조에는 설정·인증 바인딩·도구 스키마·정규화 계약이 연결되므로 구성이나 의미가 달라지면 묶음도 달라진다.
`effects.changes_state=false`, 지원 목적은 `collect`·`verify`다.

묶음의 `scope_schema`는 공통 승인 조건을 설명한다.
`scope: {}`를 승인하면 고정 대상의 현재 계정 읽기 권한·관리자 제한·자료 제공 정책 안에서 호스트가 필요한 도구와 질의·필터·페이지를 선택한다.
명시한 조건은 실제 선택 도구에도 적용하며 실제 대상이나 자료 범위를 넓히려면 수정 계획을 승인받는다.
묶음의 `input_schema`·`output_schema`는 일반 객체 설명이고, 실제 호출은 선택한 개별 도구의 스키마·읽기 효과·범위 검사로 처리한다.

계획과 위임은 묶음의 `*`·계약 참조에 연결하고 실행 입력의 `capability_id`로 실제 기능을 고른다.
worker는 [호출 입력](tasks.md#daeri_worker)의 선택 필드 `call.capability_id`를 사용하며 묶음 단계에서는 실제 기능을 지정해야 한다.
worker의 배정 조회는 묶음과 구성원의 실제 설명·입출력 스키마를 `contracts`에 함께 반환한다.
개별 기능을 승인한 기존 단계는 동일한 기능으로 실행한다.

하위 목록·스키마의 처리 범위는 [등록 조건](../resources/initial-adapters.md#등록-설정과-진단)을 따른다.
개별 스키마와 묶음·worker 계약 응답의 크기는 하위 실행 결과의 수신 상한과 별개다.
이 계약 응답 전체의 바이트 상한은 현재 미설정이며 대량 도구의 실제 호스트 문맥 수용은 별도 확인 대상이다.

## `daeri_capability_execute`

저장 계획의 등록 기능을 현재 권한·승인 범위 안에서 실행한다.

### 입력

| 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | string | 예 | 업무 ID |
| `expected_task_revision` | integer | 예 | 1 이상, 새 호출 시작 시 현재 업무 개정과 일치 |
| `plan_id`, `plan_version` | string, integer | 예 | 불변 계획 ID와 1 이상의 버전 |
| `step_id`, `call_id`, `approval_id` | string | 예 | 계획 단계·호출 멱등 키·승인 참조 |
| `instance_id` | string | 예 | 저장된 계획 단계의 리소스 |
| `capability_id` | string | 예 | 실행할 실제 등록 읽기 기능; 묶음 실행에서도 `*` 대신 실제 ID 지정 |
| `contract_ref` | string | 예 | 승인한 단계의 개별 또는 묶음 계약 참조 |
| `parameters` | object | 예 | 해당 기능 `input_schema`로 검증 |
| `bindings` | BindingSelection[] | 아니요 | 승인한 후속 값 연결; `binding_id` 고유 |
| `reconcile_of_call_id` | string | 확장 시 조건부 | `purpose=reconcile` 단계에서 확인할 원 호출 |
| `reconcile_task_id` | string | 확장 시 선택 | 원 업무; 생략 시 현재 업무 |

`BindingSelection`은 필수 `binding_id: string`, `source_call_id: string`만 갖는다.
이 도구의 멱등 키는 `call_id`다.
재전달과 개정 비교 순서는 [MCP 공통](common.md#업무-개정과-멱등-요청)을 따른다.

새 호출의 단계·목적·분기·의존 관계는 [계획 규격](plans.md)으로 검사한다.
묶음 단계는 현재 구성 지문과 선택한 구성원의 실제 계약을 확인하며 묶음 변경은 `CONTRACT_CHANGED`로 새 계획을 요구한다.
묶음 밖의 기능·변경 도구·다른 리소스는 `SCOPE_VIOLATION`이며 개별 단계는 기존 기능 일치를 요구한다.
상태·권한·승인·실제 대상 검사와 예약·의도 보존·dispatch는 [실행 제어](../tasks/workflow.md#실행-검사와-기록)를 따른다.
기억을 전제로 하는 새 dispatch에도 [현재 기억 검사](../tasks/memory.md#계획-전-조회와-활용)를 적용하며 전제가 무효이면 `PLAN_INVALID`로 수정 계획을 요구한다.

| 현재 업무 상태 | 새 호출에 허용하는 목적 |
|---|---|
| `running`, `verifying` | 현재 계획에서 승인한 `collect`, `verify` |
| 그 밖의 상태 | `TASK_STATE_INVALID`로 반환 |

허용 목적도 현재 계획·유효 승인·확인 대상·나머지 실행 검사를 모두 충족해야 한다.
첫 버전의 자동 실행 상태는 `running`이며 `verifying` 진입은 확장 범위다.
현재 `reconcile_of_call_id` 또는 `reconcile_task_id`를 제출한 새 호출은 `UNSUPPORTED_CONSTRAINT`로 반환한다.
`change`·`reconcile`과 `reconciling` 상태의 확인 호출은 해당 효과·확인 기능을 등록하는 확장 계약이다.
이미 보낸 호출의 결과 보존과 같은 호출 상태 재전달은 기존 호출 기록에 연결해 처리한다.

서버는 저장한 `input_bindings`의 허용 source pointer에서 값을 읽어 target pointer에 채운다.
`target_pointer: null`인 바인딩은 조건 판단에만 사용한다.
단계·조건에서 사용하는 모든 바인딩을 선택해야 하며 미정의 바인딩을 거부한다.
같은 source 단계를 참조한 여러 바인딩은 동일 `source_call_id`를 선택한다.

source는 같은 업무의 현재 계획 또는 `carried_evidence_refs`로 승계한 이전 계획의 허용 단계 기록이어야 한다.
서버가 저장한 출처 결과와 현재 제공 권한을 검사해 연결 값을 해소한다.

`partial`·`pending`에서도 실제 관찰·보존한 확정 필드를 현재 scope 안에서 사용할 수 있다.
호스트가 같은 target에 다른 값을 제출하면 `BINDING_INVALID`로 거부한다.
출력 계약을 통과한 보존 원본 중 현재 제공이 허용된 필드만 연결하고 입력 스키마와 승인 `scope`로 검사한다.
기본값·후속 값·하위 MCP 인수 변환을 적용한 실제 요청의 대상·연산도 최종 범위 검사에 포함한다.
자료 가림·미확인 등으로 경로를 읽을 수 없으면 `BINDING_INVALID`로 반환한다.
분기 조건은 해소된 실제 값으로 판정한다.

### 원 호출과 상태 확인의 연결

아래는 `reconcile` 기능을 등록할 때 구현·검증할 확장 계약이다.
현재 읽기 어댑터의 미확인 호출은 [중단·재개](../tasks/workflow.md#중단재개와-외부-결과-확인)에 따라 유지한다.

`reconcile_of_call_id`는 단계의 승인된 `reconciliation` 대상인 미확인·진행 중 원 호출 하나를 지정한다.
다른 업무면 `reconcile_task_id`와 계획의 `external_call_refs`에 같은 업무·호출을 지정한다.

확인 기능은 원 인스턴스의 `verification.capability_ids`에 등록하고 보존한 의도·실제 대상·원격 작업 참조·중복 방지 키로 원 호출과 연결한다.
인수는 서버의 원 기록에서 채우거나 제출 값과 대조하고 다른 작업·계정·대상이면 `BINDING_INVALID`로 반환한다.
원 기록의 현재 열람 권한과 등록된 확인 방법을 검사한 뒤 확정 근거·확인 결과·원 호출 상태·외부 효과를 함께 보존한다.

같은 확인 대상에 실행 중 요청이 있으면 그 요청의 상태를 먼저 확인한다.
원 호출이 이미 해소됐다면 `TASK_STATE_INVALID`·`refresh`로 현재 기록을 안내한다.

모든 미확인 호출이 해소되고 현재 상태가 `reconciling`이며 유효 승인·현재 검사를 충족하면 저장한 `running|verifying`으로 돌아간다.
근거 부족·보존 실패·미확정 소비와 늦은 결과의 중지·종료 상태 보존은 [중단·복구 명세](../tasks/workflow.md#중단재개와-외부-결과-확인)를 따른다.

다른 업무의 확인은 같은 로컬 저장소의 종료 업무에 한하며 양 업무의 현재 권한을 검사한다.
profile이 달라도 원 인스턴스·실제 대상·권한 문맥의 바인딩이 일치해야 한다.

확인 호출의 사용량·예약은 새 업무, 원 호출의 사용량·예약은 원 업무에 남기고 과거 종료 상태를 유지한다.
확인 근거는 양 업무·원 호출·계획·승인에 연결하고 원 호출 갱신·양 업무 개정과 함께 보존한다.
확정 해소는 관련 기록의 보존이 성공한 뒤 반환한다.

### 반환 `data`

아래 객체를 `CallResult`로 정의한다.

| 필드 | 타입 | 필수 | 내용 |
|---|---|---|---|
| `task_id`, `task_revision` | string, integer | 예 | 업무 ID와 현재 개정 |
| `call_id`, `plan_id`, `plan_version`, `step_id` | string, string, integer, string | 예 | 저장한 호출 연결 |
| `call_state` | string | 예 | `completed`, `blocked`, `failed`, `pending`, `unknown` |
| `external_effect` | string | 예 | `none`, `confirmed`, `unknown` |
| `result_ref` | string 또는 null | 예 | 보존된 결과의 불투명 참조 |
| `actual_capability_id`, `actual_contract_ref` | string | 신규 호출 | 실제 선택한 개별 기능과 실행 계약; 이전 기록에서는 생략 가능 |
| `inline_result` | object 또는 null | 예 | 제공이 허용된 기능 결과 |
| `usage` | Usage | 예 | 이 호출과 내부 요청의 소비·예약 |
| `budget_remaining` | BudgetRemaining | 예 | 기존 실행을 포함해 계산한 잔여량 |
| `evidence_refs` | string[] | 예 | 제공할 수 있는 근거 기록 참조 |
| `warnings` | string[] | 예 | 잘림·일관성·마스킹 등 해석의 제한 |
| `retry_safety` | RetrySafety | 예 | 현재 서버 재시도 가능 여부와 이유 |
| `reconciliation` | ReconciliationOutcome 또는 null | 예 | 확인 호출의 원 호출 연결·해소 결과; 다른 목적은 null |

`inline_result`가 있으면 해당 `output_schema`를 만족해야 한다.
마스킹 뒤 스키마를 만족할 수 없으면 null로 반환하고 제한을 표시한다.
정규화·출력 스키마 검사를 통과한 본문을 `inline_result`·바인딩·파생 계산·완료 근거로 제공한다.
출력 검사 실패 시에도 실제 확인된 원격 완료·효과·소비·예약은 원 호출에 보존한다.
완료 여부·효과를 확인할 수 있으면 `RESULT_VALIDATION_FAILED`, `inline_result=null`로 반환하고 실제 `call_state`를 유지한다.
효과까지 미확인이면 `EXTERNAL_OUTCOME_UNKNOWN`을 우선하고 승인된 확인 절차를 따른다.
출력 오류 뒤의 재시도·예약 정산·근거 채택은 실제 원격 상태와 해당 실행 계약으로 판정한다.
URL·파일 경로는 검증한 원본 근거의 허용 내용으로만 제공한다.
`external_effect: "none"`은 업무 대상 변경이 없다는 뜻이며 읽기의 부수 효과는 기능 계약에 남긴다.
현재 내장 읽기 호출은 동기식이며 `external_effect: "none"`·`reconciliation: null`을 반환한다.
`pending` 접수·변경 효과·원 호출 확인 결과는 위 확장 계약과 과거 저장 상태를 표현하는 공통 DTO다.

저장 호출·조회 근거의 `capability_id`·`contract_ref`는 승인한 단계의 값이다.
묶음 실행에서는 `*`·묶음 지문을 유지하고 `actual_capability_id`·`actual_contract_ref`로 실제 도구를 함께 추적한다.
단계 의존·바인딩·근거 승계·worker 실행은 같은 승인 단계에 연결하면서 실제 도구의 출력 스키마와 원본을 검사한다.

| 하위 타입 | 필드와 타입 |
|---|---|
| `Usage` | `logical_calls: integer`, `external_requests: integer/null`, `runtime_ms: integer/null`, `metrics: MetricUsage[]` |
| `MetricUsage` | `instance_id: string`, `metric_id: string`, `consumed: number/null`, `reserved: number/null` |
| `BudgetRemaining` | `remaining_tool_calls: integer/null`, `remaining_external_requests: integer/null`, `remaining_runtime_ms: integer/null`, `resource_limits: MetricRemaining[]` |
| `MetricRemaining` | `instance_id: string`, `metric_id: string`, `remaining: number/null` |
| `RetrySafety` | `automatic_retry_allowed: boolean`, `effect_known: boolean`, `reason: string` |
| `ReconciliationOutcome` | `original_task_id: string`, `original_task_revision: integer`, `original_call_id: string`, `resolution: string`, `original_call_state: string`, `original_external_effect: string`, `evidence_refs: string[]` |

이 하위 객체의 필드는 모두 필수이고 숫자는 0 이상이다.
미확정 소비·예약·잔여량은 null로 유지하고 확정 근거가 생기면 정산한다.
예산 항목은 [계획 규격](plans.md)의 `max_tool_calls`, `max_external_requests`, `max_runtime_ms`, `resource_limits`와 대응한다.
측정한 내부 페이지·재시도의 외부 요청 수는 `external_requests`로 기록하고 미측정 값은 null로 유지한다.
현재 하위 전송의 측정 범위는 [호출·출력 계약](../resources/initial-adapters.md#호출출력실패)을 따른다.
현재 읽기 구현의 `metrics`는 빈 목록, 전송한 호출의 `external_requests`는 null이며 전송 전에 차단한 호출은 0이다.
`retry_safety`는 업무 대상 변경이 없는 읽기 효과와 같은 호출 ID의 저장 상태 재전달을 설명하며 `automatic_retry_allowed: false`다.

`runtime_ms`는 DAERI가 이 호출에 사용한 대기·실행 관리 시간을 표시한다.
원격 작업의 실제 실행 시간이나 종료 보장은 별도 근거로 표시한다.
외부 요청 상한이 없는 계획의 `remaining_external_requests`는 null이며 실제 소비량을 알면 계속 기록한다.
현재 한 호출은 남은 업무 실행 시간과 60초 중 작은 값을 예약한다.
실행 완료 시 실제 경과 시간을 소비로 기록하고 미사용 예약을 반환한다.

확장 `resolution`은 `resolved`·`pending`·`unknown`이며 원 호출의 상태·효과는 반환 시 보존 값이다.
`resolved`는 확정 근거·`none|confirmed`인 효과·`completed|failed`인 종료 상태를 요구하고 나머지는 확인 대기를 유지한다.
`original_task_revision`은 공통 `TaskRevision`이며 확인 도구의 `ok`와 원 작업의 성공·실패는 각각의 결과로 구분한다.

### 결과와 오류

| 상황 | `status` · `call_state` · 오류 |
|---|---|
| 정상 완료 | `ok` · `completed` · null |
| 확장 계약의 비동기 접수 | `pending` · `pending` · null |
| 확정된 원격 실패 | `error` · `failed` · `EXTERNAL_EXECUTION_FAILED` |
| 단절·시간 초과 등으로 결과 미확인 | `error` · `unknown` · `EXTERNAL_OUTCOME_UNKNOWN` |
| 출력 정규화·스키마 실패, 실행 결과는 확인됨 | `error` · 실제 확인 상태 · `RESULT_VALIDATION_FAILED` |
| 일부 결과 잘림 | `partial` · 실제 확인 상태 · `RESULT_TRUNCATED` |
| 하위 결과의 2 MiB 수신 처리 상한 초과 | `error` · `failed` · `RESULT_TRUNCATED`; 본문 null |
| 조회 뒤 결과·메타데이터 저장 실패 | `error` · `unknown` · `RESULT_PERSIST_FAILED`; 결과 참조 null |

현재 읽기의 조회 결과 미확인은 원 호출의 `unknown` 상태로 유지한다.
확장 계약의 변경·비동기 처리는 실제 효과와 완료를 별도로 기록하고 등록된 확인 기능으로 원 호출에 연결한다.
검사·저장 실패와 후속 행동은 [공통 오류](common.md#오류와-다음-행동)를 따른다.

실행 의도 보존 전의 검사 실패는 `data=null`이며 호출 기록은 의도 보존에 성공한 뒤 발급한다.
보존 후 dispatch 재검사에서 차단하면 `call_state=blocked`인 보존 호출을 반환한다.
dispatch 전 저장 실패는 `STORE_UNAVAILABLE`이며 요청 전송을 중단한다.
이미 보존한 의도가 있으면 실행기가 중단 사실을 `blocked`로 기록하고 미사용 호출·시간 예약을 해제한다.
조회 뒤 저장 실패는 원 호출을 `unknown`·`RESULT_PERSIST_FAILED`로 기록하며 확인한 로컬 경과 시간을 한 번 정산한다.
이 호출은 사용 가능한 결과 본문이 없는 미확인 상태로 남고 신규 dispatch를 차단하며 부분·실패 보고로 마감할 수 있다.
저장소 전체가 일시 중단되면 같은 실행기에 중단 시각·경과 시간만 유지하고 복구 뒤 같은 업무의 다음 요청에서 먼저 기록한다.
이 복구가 개정을 증가시키면 최신 상태를 읽고 새 변경 요청을 구성한다.
복구 전 실행기까지 종료되면 [프로세스 종료 복구](tasks.md#daeri_task_resume)에서 미확정 예약을 보수적으로 유지한다.
`INTENT_PERSIST_FAILED`와 원격 변경 효과 확인은 확장 계약이다.

## 구현과 검증 경계

등록 계약·정규화·읽기 실행·범위·누적 예산·재전달·실패·잘림을 로컬 하위 MCP 프로세스로 검증했다.
담당 구현은 [리소스 등록부](../../../src/daeri/resources.py)와 [실행기](../../../src/daeri/engine.py)다.
[리소스 자율 조회](../../../tests/test_resource_autonomy.py), [묶음 도구 선택·근거·재전달](../../../tests/test_engine.py), [worker 선택](../../../tests/test_workers.py)에서 공통 경계를 확인한다.
실제 사내 리소스의 권한·연결과 호스트 대화는 [구현 검증 경계](../../plans/initial-implementation.md#현재-구현-단위와-검증-경계)를 따른다.
제품별 조회 범위·후속 페이지·잘림 표시는 [최초 어댑터의 완전성](../resources/initial-adapters.md#조회-완전성)에서 확인한다.
호스트가 요청에 필요한 조회 충분성과 업무 완료를 판단한다.
