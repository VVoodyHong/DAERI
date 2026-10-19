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
| `memory_search_refs` | RecordRef[] | 예 | 현재 요청·profile 맥락에서 발급한 검색 기록; 최소 하나 |
| `memory_refs` | RecordRef[] | 예 | 계획에 적용한 기억의 불변 개정 참조; 없으면 [] |
| `memory_limitations` | string[] | 예 | 검색 실패·누락·적용 조건·재확인 필요; 없으면 [] |
| `completion_criteria` | CompletionCriterion[] | 예 | 최소 한 개의 완료 판단 기준 |
| `previous_plan_id` | string | 아니요 | 같은 업무의 현재 계획을 대체할 때 필수 |
| `carried_evidence_refs` | string[] | 아니요 | 승계할 같은 업무의 기존 결과 참조; 기본 [] |

`task_id`와 `expected_task_revision`은 도구 인수에 두며 draft에 중복하지 않는다.
사용할 목적·방법·대상·제공 범위를 기존 업무와 현재 권한에 대조한다.
단순한 목적 설명이나 필드 선택자가 비밀 자료의 반환을 허가하지 않는다.
실패 처리는 [공통 기본 동작](../tasks/workflow.md#일부-조회-실패의-기본-동작)을 따르며 별도 정책 엔진을 추가하지 않는다.

기억 검색 참조는 [기억 조회 도구](tasks.md#daeri_memory_search)가 실제 발급한 기록이어야 한다.
실행기는 업무·profile 스냅샷·최신 요청과 보완 맥락·조회 당시 현재 계획 참조, 선택한 기억의 버전·현재 권한·만료·무효화를 대조한다.
`memory_refs`는 연결한 검색 기록들이 반환한 항목의 부분집합이며 임의 기록 참조를 넣지 않는다.
추가 검색은 참조 배열로 함께 연결해 서로 다른 질의에서 찾은 지식을 사용할 수 있다.
현재 맥락으로 조회하지 않았거나 선택한 기억이 유효하지 않으면 `PLAN_INVALID`로 새 조회·계획 수정을 요구한다.

0건 조회는 `memory_refs: []`로 정상 계획을 만들 수 있다.
검색 제한·실패가 있는 기록도 계획에 연결할 수 있지만 서버가 확인한 제한을 표시본과 `memory_limitations`에 남긴다.
저장소를 신뢰하지 못해 검색 기록도 발급하지 못하면 계획을 저장하지 않는다.
검색 참조는 조회했다는 근거이며 모델의 숙지·검색의 충분성·현재 상태의 정확성을 증명하지 않는다.

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

`scope`는 리소스 고유의 작업 표현·임의 인증·연결 대상을 전달하는 통로가 아니다.
기간·필드·대상·필터 등 기능 계약이 검증하고 실행 중 강제할 조건만 담는다.
계약이나 실제 대상이 승인한 방법·범위에 영향을 주게 바뀌면 기존 승인으로 실행하지 않는다.

리소스 고유의 조회·작업 입력은 [등록된 기능](../resources/contract.md#기능-설계-단위)의 `parameters`로 전달하도록 설계한다.
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
| `source_pointer` | JsonPointer | 예 | 기능 출력에서 읽을 허용 값 위치 |
| `target_pointer` | JsonPointer 또는 null | 예 | `parameters` 안의 입력 위치; null은 조건 판단 전용 |

포인터는 등록 계약이 허용한 값 위치만 참조하며 [공통 경로 문법](common.md#필드-경로와-선택)을 따른다.
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
계획 설명에는 실제 외부 요청 수의 상한이 없고 사용량도 미측정일 수 있음을 표시한다.
이 값은 0 요청·승인 면제·자료 범위 확대를 뜻하지 않는다.

숫자 상한을 요청하면 모든 실행 단계가 `budget_support.external_requests_enforceable=true`를 충족해야 한다.
미지원이면 `PLAN_INVALID`로 보완을 요구하며 실행 시에도 강제 불가 조건을 거부한다.
동일 업무의 과거 외부 소비량이 미확정이면 이를 0으로 바꿔 새 숫자 상한을 적용하지 않는다.

`max_runtime_ms`가 끝나면 DAERI의 추가 dispatch와 계속 기다리는 처리를 중단한다.
이미 요청한 원격 작업의 실제 종료·CPU 사용·과금 시간까지 같은 한도로 보장하지 않는다.
원격 작업 시간의 강제 상한이 필요하면 지원하는 `resource_limits` 항목으로 별도 승인받는다.

| PlanOutput 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `format` | string | 예 | 호스트 대화에서 사용할 설명·표·구조화 응답 등의 보고 표현 |
| `destination` | string | 예 | 사람이 이해할 전달 대상 설명 |
| `resource_rules` | ResourceOutputRule[] | 예 | 업무 계획마다 승인할 리소스별 제공 범위 |
| `table_rules` | TableRule[] | 예 | 파생 표의 출처·결합·계산·완전성 범위; 없으면 [] |
| `artifact_rules` | ArtifactRule[] | 예 | 생성·제공할 파일 형식·입력 종류·옵션·크기; 파일이 없으면 [] |

대화 응답의 표현과 파일 산출물의 형식을 구분한다.
표·CSV는 최초 검증 사례이며 사용자의 요청과 등록 생성기에 따라 텍스트·JSON·DB 파일 등으로 확장한다.
형식의 발견·등록·생성 경계는 [출력 계약](tasks.md#결과-형식과-생성기)을 따른다.

`ArtifactRule`의 필수 필드는 `artifact_id:ID`, `format_id:string`, `format_contract_ref:RecordRef`, `input_kind:string`, `options:JsonObject`, `max_bytes:PositiveInt`다.
`artifact_id`는 계획 안에서 고유하고 등록 형식·계약·입력 종류·옵션 스키마와 일치해야 한다.
파일 크기는 실행기 상한 안에서 지정하고 실제 자료 범위·형식·옵션·제공 대상 확대는 재승인받는다.
생성기 계약이 바뀌거나 준비되지 않으면 기존 계획으로 생성하지 않으며 설치·다른 형식 대체를 숨겨 수행하지 않는다.

| ResourceOutputRule 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `instance_id` | string | 예 | 고정 profile 안의 원본 리소스; 중복 금지 |
| `allowed_model_fields` | FieldSelector[] | 예 | 해당 리소스 결과 중 모델에 제공할 필드 |
| `export_fields` | FieldSelector[] | 예 | 해당 리소스 결과 중 산출물에 포함할 필드 |
| `retained_fields` | FieldSelector[] | 예 | 업무 종료 후 로컬에 유지할 최소 조회 본문 필드 |
| `memory` | MemoryRule | 예 | 이 리소스에서 선별할 지식의 범주·출처·재제공·보존 허용 |
| `masking` | MaskingRule[] | 예 | 해당 리소스의 가림 조건; 없으면 빈 배열 |

실행 단계·승계 근거·선택한 기억의 기여 리소스마다 규칙을 하나씩 명시하고 호스트 대화에서도 리소스별로 표시한다.
원본 기록의 인스턴스로 규칙을 선택하며 다른 리소스의 같은 필드명에 적용하지 않는다.
빈 필드 배열은 해당 모델 제공·내보내기·종료 후 본문 보관을 허용하지 않으며 규칙·출처를 확인할 수 없으면 허용하지 않는다.
리소스 호출·승계 근거·리소스 기억이 없는 계획에서는 `resource_rules: []`를 사용하고 요청 자료의 등록 제공 정책을 적용한다.
승계 근거는 원본 보관·제공 조건·현재 권한·새 계획의 해당 규칙을 모두 충족해야 한다.

`retained_fields`는 종료 후 본문 보관 범위이며 모델 제공·내보내기 허용을 뜻하지 않는다.
현재 보관 권한과 비밀 제거·마스킹을 적용하며 이력·활성 업무 복구 자료의 보존은 [업무 명세](../tasks/workflow.md#이력과-완료-근거)를 따른다.
실행기는 고정 profile에서 [공통 보존기간](../tasks/workflow.md#이력과-완료-근거)을 해소해 표시하며 모델 인수로 기간을 바꾸지 않는다.
같은 기간을 적용해도 조회 보관·보고·내보내기의 필드별 허용 범위는 별개다.

| MemoryRule 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `categories` | enum[] | 예 | `service_rule`, `resource_structure`, `procedure`, `lesson`; 중복 금지 |
| `source_fields` | FieldSelector[] | 예 | 후보 작성에 사용할 원본 필드; 모델 제공·본문 보관 허용의 교집합 안에서 선택 |
| `context_sources` | enum[] | 예 | 실제 사용자 확인 자료 `request`, `clarification`; 허용하지 않으면 [] |
| `retention` | enum | 예 | `none`, `source_bound`, `long_term` |
| `allow_model_recall` | boolean | 예 | 향후 업무의 호스트 모델에 해당 지식을 제공할 수 있는지 |

`none`이면 범주·출처 필드·사용자 확인 자료는 빈 배열이고 재제공은 false다.
그 외에는 범주와 하나 이상의 원본 필드 또는 사용자 확인 자료를 명시하며 profile 정책을 초과하지 않는다.
출처 필드는 개별 자료의 보존 허용이지 그 내용을 그대로 장기 지식에 복제할 허가가 아니다.
사용자 요청·정정은 `context_sources`로 별도 허용하며 실제 대상 리소스의 규칙과 해당 자료의 제공 정책을 함께 적용한다.
이 선택으로 기능 출력의 필드 선택자에 요청 본문을 끼워 넣거나 승인 응답을 지식으로 수집하지 않는다.

실행기는 profile에서 해소한 일반 경험·장기 지식의 기간과 미래 재제공 조건을 불변 표시본에 포함한다.
`long_term`은 원자료와 별개의 운영지식을 자체 기한까지 보관하는 명시적인 승인 항목이다.
모델 인수로 기간을 늘리지 않으며 상세 대상·기한·정정은 [기억 보존](../tasks/memory.md#보존과-장기-운영지식)을 따른다.
혼합 출처의 허용 범위는 모든 기여 리소스 조건의 교집합이다.

기억을 보고·CSV에 인용할 때에도 원지식의 내보내기 허용과 현재 계획의 `export_fields`·마스킹을 검사한다.
선택자는 기억이 보존한 원 출처 필드 대응으로 검사하며 대응·제공 허용이 불명확하면 인용하지 않는다.
기억을 읽었다는 이유로 원자료·장기 지식을 다시 보관하거나 내보낼 수 있는 것은 아니다.

선택자는 [공통 문법](common.md#필드-경로와-선택)으로 기능 계약의 결과 본문을 가리킨다.
`MaskingRule`의 필수 필드는 `field: FieldSelector`, `action: "omit" | "redact"`다.
부모·자식의 선택·가림·충돌은 공통 경로 규칙으로 처리한다.

선택자에 포함된 영역도 현재 열람 권한·비밀 제외·마스킹 조건을 만족해야 반환한다.

`destination`은 경로나 URL·인증 참조를 실행하는 지시가 아니다.
메일 전송·외부 게시처럼 실제 외부 전달이 필요하면 별도의 변경 기능 단계로 승인받는다.
필드·마스킹의 제품 의미는 [결과·이력 명세](../tasks/workflow.md#이력과-완료-근거)를 따른다.

### 파생 표의 허용 범위

`TableRule`은 보고·CSV의 로컬 결합·계산 범위를 승인하기 위한 자료형이다.
소스·연산·한도만 지정하며 원격 조회나 임의 식을 실행하는 계획 언어가 아니다.

| TableRule 필드 | 타입 | 제약 |
|---|---|---|
| `table_id` | ID | 계획 안에서 고유한 파생 표 식별자 |
| `sources` | TableSourceRule[] | 하나 이상; `source_id` 중복 금지 |
| `join_mode` | enum | `none`, `inner`, `full` |
| `coverage` | enum | `complete`, `selected` |
| `allowed_operations` | TableOperation[] | 허용할 [셀 연산](tasks.md#파생-표와-셀-검증); 중복 없는 하나 이상 |
| `max_rows` | PositiveInt | 로컬 처리·반환 상한 이내의 결과 행 수 |
| `ratio_scale` | NonNegativeInt 또는 null | 비율 연산을 허용하면 0~18의 반올림 소수 자릿수; 그 밖에는 null |

`TableSourceRule`의 필수 필드는 `source_id:ID`, `step_id:ID`, `rows_pointer:JsonPointer`, `key_pointers:JsonPointer[]`다.
`step_id`는 현재 계획의 자료 확보 단계이며 저장 결과의 리소스·기능·계약이 이 단계와 일치해야 한다.
기존 결과를 재사용하려면 `carried_evidence_refs`에도 명시하고 해당 단계와의 대응을 확인한다.
현재 계획에 같은 논리 단계를 두었다는 이유로 이미 끝난 조회를 다시 수행하지 않는다.

`rows_pointer`는 기능 결과의 객체 배열 또는 단일 객체를, `key_pointers`는 각 행 안의 결합키를 가리킨다.
단일 객체는 한 행으로 취급하며 소스별 여러 기록의 대응·페이지 완전성은 제출 시 검사한다.
객체 하나뿐인 단일 소스는 빈 키를 허용하고 그 밖에는 하나 이상의 키가 필요하다.
복합 키의 순서·타입은 모든 소스가 같아야 하며 누락·null·중복 키는 거부한다.

`none`은 단일 소스, `inner`는 모든 소스에 있는 키, `full`은 어느 소스에든 있는 키의 합집합이다.
초기에는 유일키의 등가 결합만 제공하고 다대다·조건식 결합이나 로컬 그룹 집계는 수행하지 않는다.
필요한 그룹 집계는 승인된 리소스 기능으로 먼저 확보하고 그 확정 결과를 결합·계산한다.

`complete`는 승인 범위의 원자료 완전성과 결과의 누락 없음이 모두 확인돼야 한다.
`selected`는 선택한 일부 결과이며 전체 자료를 전제로 하는 완료 조건을 충족하지 못한다.
결합 방식·키·허용 연산·전체성 범위를 바꾸거나 행 한도를 늘리려면 수정 계획을 승인받는다.
계산 결과·출처·마스킹·기한의 검사는 [파생 표 계약](tasks.md#파생-표와-셀-검증)으로 연결한다.

### 완료 조건

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
| `retention_context` | RetentionContext | 예 | 고정 profile에서 해소한 본문·장기 지식의 보존기간 |
| `is_active` | boolean | 예 | 현재 업무의 실행 대상 계획인지 여부 |
| `approval_request` | ApprovalOutcome 또는 null | 예 | 연결된 최신 확인 요청; 없으면 null |
| `budget_context` | BudgetContext | 예 | 이전 한도와 실제 소비·예약·남은 양 |

`context`, `draft`, `contract_refs`, `render_ref`, `retention_context`는 저장 후 수정하지 않는다.
불변은 보존 중 내용 변경을 금지하며 [본문 기한과 정리](../tasks/workflow.md#이력과-완료-근거)를 면제하지 않는다.
`is_active`·승인 요청 상태·예산 맥락은 조회 당시 기록이며 계획 내용과 구분한다.
표시본은 [호스트 대화의 계획 제시](../hosts/approval.md#화면과-사용자-흐름)에 필요한 전체 조건에 대응하며 비밀값·내부 인증 참조를 제외한다.

`RetentionContext`는 필수 `task_body_days:PositiveInt`, `long_term_memory_days:PositiveInt`를 갖는다.
모델 입력이 아닌 고정 설정에서 구하며 표시본에 같은 기간을 보여 준다.
기억 저장·기간 확대를 허용하는 값이 아니며 개별 기억에는 원출처·승인 조건에 따른 더 짧은 기한을 적용한다.

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

반환 `data`의 필수 필드는 `task_id`, `task_revision`, `plan_id`, `plan_version`, `contract_refs`, `render_ref`, `retention_context`, `budget_context`다.
각 타입은 공통 규격과 `SavedPlan` 표를 따른다.
성공은 `status: "ok"`이며 최초 제출 또는 교체를 기록할 때 개정을 한 번 증가시킨다.
실행기는 업무의 `TaskContext`를 SavedPlan에 연결하며 입력으로 다른 profile이나 스냅샷 참조를 받지 않는다.
profile 맥락 미해소, 스냅샷 밖 인스턴스, 연결·인증의 미준비 상태는 실행 가능한 계획으로 저장하지 않는다.
원격 연결이나 로그인을 계획 검사에서 암묵적으로 수행하지 않는다.
기억 검색 기록·참조 검사는 위 `PlanDraft`를 따르며 검색을 계획 검사 안에서 대신 수행하지 않는다.

교체는 현재 계획을 `previous_plan_id`로 연결하는 재계획 경로에서만 허용한다.
최초 제출은 `planning`, 교체는 `planning|awaiting_approval|running|reconciling|verifying`에서 허용한다.
교체를 보존하면 이전 승인·대기 요청의 무효화와 새 현재 계획의 `awaiting_approval` 전이를 같은 트랜잭션으로 확정한다.
별도 재계획 도구 없이 이 제출로 승인 대기에 진입하며 진행 중인 호출은 원 계획·승인에 연결해 보존한다.
미답변 질문이 있는 `awaiting_input`에서는 먼저 보완·재개 경로로 질문을 해소한다.
`paused`에서 새 계획 제출은 `TASK_STATE_INVALID`이며 먼저 업무 재개로 상태·기존 계획을 검토한다.
진행 중 호출·미확인 외부 효과를 숨기거나 새 변경을 재실행할 근거로 사용하지 않는다.
상태 확인의 승인·예산이 부족하면 필요한 `reconcile` 단계의 새 계획을 제출해 확인받을 수 있다.
종료된 업무는 `TASK_STATE_INVALID`로 거부하며 기존 계획은 현재 권한·본문 보존기한 안에서 열람할 수 있다.

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

필수 계획 본문·표시본이 만료되면 `RECORD_EXPIRED`, `data=null`로 반환하며 불완전한 `SavedPlan`을 만들지 않는다.
보존 중인 파일 유실·손상은 `RECORD_UNAVAILABLE`로 구분하고 원문을 재생성하지 않는다.
최소 업무 식별·결정·참조는 현재 제공 권한 안에서 `daeri_task_get`으로 확인한다.

## `daeri_approval_request`

저장한 현재 계획의 승인 요청을 만들고 실제 사용자 대화 응답을 같은 불변 계획에 연결한다.
호스트가 자연어 응답의 결정을 해석하고 실행기가 대상·상태·권한을 검사한다.
호스트는 요청 단계 뒤 계획을 대화에 제시하고 사용자의 답변을 기다린다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | string | 예 | 공통 내용 멱등 키 |
| `task_id` | string | 예 | 확인할 업무 |
| `expected_task_revision` | integer | 예 | 1 이상; 새 요청의 현재 개정과 일치 |
| `plan_id`, `plan_version` | string, integer | 예 | 현재 저장 계획과 1 이상의 버전 |
| `stage` | string | 예 | `request` 또는 `respond` |
| `approval_request_id` | string | 조건부 | `respond`에서 필수; 사용자가 확인한 대기 요청 |
| `decision` | ApprovalDecision 또는 null | 조건부 | `respond`에서 필수; 호스트가 실제 답변·대화 맥락에서 해석한 결정 |
| `user_response` | string | 조건부 | `respond`에서 필수; 비밀 제거를 제외하고 요약·재작성하지 않은 실제 답변 |
| `user_message_ref` | string | 아니요 | `respond`에서 호스트가 제공할 때만 전달하는 불투명 사용자 메시지 참조 |

`ApprovalDecision`은 `approve`, `request_changes`, `decline`, `cancel`의 enum이다.
`request`에는 `approval_request_id`, `decision`, `user_response`, `user_message_ref`를 넣지 않는다.
응답이 없으면 `respond`를 호출하지 않으며 응답 부재를 승인이나 취소로 바꾸지 않는다.

`respond`의 `user_response`는 비어 있지 않아야 하고 모델이 작성한 승인 요약으로 대체하지 않는다.
호스트는 응답의 비밀값만 제거하며 원래의 대상·조건·부정·결정 의미를 유지한다.
제거 때문에 의사를 판단할 수 없으면 `decision=null`로 두고 비밀을 다시 요구하지 않는 방식으로 확인한다.
실행기도 저장·반환 전에 비밀 제거를 적용하고 정제한 답변을 `feedback`으로 보존한다.

`user_message_ref`는 인증 증거나 임의 대화 조회 지시가 아니다.
참조를 제공하지 않는 호스트에서는 생략하며 새로운 호스트 API를 필수로 가정하지 않는다.
참조가 있으면 같은 참조의 이미 보존한 응답·승인 대상과 대조하고 서로 다른 답변이나 계획에 재사용하면 `APPROVAL_UNAVAILABLE`로 거부한다.

### 응답 해석과 검사의 책임

| 담당 | 처리 |
|---|---|
| 호스트 | 실제 사용자 메시지를 모델·도구·리소스 자료와 구분하고 현재 대화에서 승인 대상·결정을 해석 |
| 호스트 | 조건 추가·변경은 `request_changes`, 질문·대상 불명확·아직 결정하지 않음은 null로 전달 |
| 실행기 | 입력 형식·멱등 키·업무 개정·현재 계획과 버전·대기 요청·표시본·현재 권한을 검사 |
| 실행기 | 응답과 해석한 결정을 함께 보존하고 유효한 `approve`에서만 승인 발급 |

“좋아, 다만 조회 기간은 줄여 줘”는 `approve`가 아니라 `request_changes`다.
현재 계획에 이미 있는 조건을 확인하는 말인지 실제 변경 요구인지도 호스트가 문맥에서 구분한다.
호스트가 판단하지 못하면 null로 전달하며 승인으로 추정하지 않는다.
수정 요구는 `feedback`에 남기고 새 계획을 제출하며 답변의 조건으로 기존 계획을 부분 수정하지 않는다.

`approved` 같은 값 하나나 모델이 작성한 “사용자가 승인했다”는 설명만으로 승인을 생성하지 않는다.
실행기는 자연어를 별도 모델로 다시 판정하지 않으며 구조 검사만으로 호스트의 해석 오류를 모두 증명·차단한다고 보장하지 않는다.
실제 답변 전달과 해석의 신뢰 경계는 [호스트 승인 계약](../hosts/approval.md#대화-승인의-범위)을 따른다.

새 확인 요청과 새 응답은 `awaiting_approval`에서만 허용한다.
`paused`·종료 상태 등에서는 `TASK_STATE_INVALID`로 거부하며 과거 응답으로 상태를 해제하지 않는다.
실행기는 대기 요청의 업무·계획·버전·표시본이 현재 대상과 일치하는지 확인한다.
불일치·이미 종료된 요청·철회·무효화·현재 승인 권한 상실은 승인을 발급하지 않는다.

`approve`를 기록하기 직전에 선택 기억의 [현재 유효성](../tasks/memory.md#계획-전-조회와-활용)과 현재 계획의 실행 조건도 다시 검사한다.
계약·대상·조건이 변경됐거나 강제할 수 없으면 기존 계획을 그대로 승인하지 않는다.
계획 전제가 깨졌으면 `PLAN_INVALID`로 재조회·수정 계획을 요구하며 대기 요청의 연결 실패는 `APPROVAL_UNAVAILABLE`이다.

새 확인 요청과 실제 응답 기록은 각각 업무 개정을 한 번 증가시킨다.
질문 등 `decision=null`인 실제 응답도 기록하지만 승인 대기를 유지한다.
다음 사용자 응답은 새 `request_id`와 최신 업무 개정으로 전달한다.
각 요청의 재전달은 원래 키·입력을 유지한다.
멱등 비교에는 `decision`, 정제한 실제 `user_response`와 제공된 `user_message_ref`도 포함한다.
같은 키로 결정만 바꿔 보내면 공통 멱등 충돌로 거부한다.

### `ApprovalOutcome`과 실제 응답

반환 `data`는 필수 `task_id: string`, `task_revision: integer`와 아래 필드를 갖는다.
`SavedPlan.approval_request`에는 아래 필드만 연결한다.

| 필드 | 타입 | 필수 | 내용 |
|---|---|---|---|
| `approval_request_id` | string | 예 | 저장한 확인 요청 ID |
| `state` | string | 예 | `awaiting_response`, `approved`, `rejected`, `cancelled`, `invalidated` |
| `approval_id` | string | 조건부 | 현재 유효한 `approved`에서만 반환 |
| `decision` | ApprovalDecision 또는 null | 예 | 보존한 결정; 요청 직후·미결 응답이면 null |
| `feedback` | string 또는 null | 예 | 비밀을 제거한 실제 답변; 응답 기록이 없으면 null |

대기는 `pending`, 보존한 승인·거절·명시적 확인 취소는 `ok`다.
무효 요청·응답 검증 실패는 `error`이며 실행 권한을 부여하지 않는다.
도구의 정상 처리가 업무 성공·업무 취소를 뜻하지 않는다.

결정과 업무 전이는 다음과 같으며 `feedback`은 자료로 보존하고 새 계획에 자동 적용하지 않는다.
응답이 있으면 `feedback`은 비어 있지 않아야 한다.

| `decision` | 요청 `state` | 검증·보존 후 업무 상태 |
|---|---|---|
| null | `awaiting_response` | `awaiting_approval`; 설명·대상 확인 뒤 새 응답 대기 |
| `approve` | `approved` | `running`; 미확인 효과가 있으면 `reconciling` |
| `request_changes`, `decline` | `rejected` | `planning`; 조건 수정은 새 계획으로 제출 |
| `cancel` | `cancelled` | `awaiting_approval`; 확인 요청만 취소하고 업무는 유지 |

업무 일시중지는 [업무 수정](tasks.md#daeri_task_update)의 `action=pause`, 업무 취소는 [업무 취소](tasks.md#daeri_task_cancel)로 처리한다.
이 요청들을 승인 도구의 `cancel`로 대신 처리하지 않는다.
“이 확인 요청만 취소해 줘”와 “업무를 취소해 줘”를 구분할 수 없으면 null로 두고 뜻을 확인한다.
확인 요청 기한은 실행 승인 만료와 구분한다.

동일 요청 재전달은 기존 상태를 조회하며 승인·예산을 추가하지 않는다.
현재 계획의 유효 대기 요청이 있으면 다른 ID로 승인 요청을 중복 생성하지 않는다.
업무 취소·계획 교체·만료·철회·일시중지로 무효화한 요청은 `invalidated`로 반환하고 실행 가능한 `approval_id`를 재전달하지 않는다.
최종 결정은 현재 업무·요청 유효성을 다시 검사해 한 번만 기록하며 이후 새 응답으로 덮어쓰지 않는다.

## 오류와 호스트 연결의 경계

사용자 응답을 현재 업무·계획·확인 요청에 연결할 수 없으면 `APPROVAL_UNAVAILABLE`로 반환한다.
권한을 부여하지 않고 현재 대기·일시중지·종료 상태를 유지한다.
호스트의 대화 응답 전달·구분과 재시작·재계획 시 연결 동작은 [호스트 검증 범위](../hosts/approval.md#구현-시-검증)를 구현 때 확인한다.
