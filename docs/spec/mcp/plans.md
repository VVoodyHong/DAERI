# 계획과 승인 MCP 도구

상태: 계획·승인 도구와 공개 계약 구현; 실제 호스트 대화는 별도 검증

불변 계획·승인의 세 도구와 `PlanDraft`·`SavedPlan`의 공개 계약을 정의한다.
승인 원칙은 [계획 명세](../tasks/approval.md), 실행 상태는 [업무 흐름](../tasks/workflow.md)을 따른다.
공통 타입·응답·오류·재전달은 [공통 규격](common.md)을 적용한다.
구현·검증 범위는 [최초 구현 계획](../../plans/initial-implementation.md#현재-구현-단위와-검증-경계)을 따른다.

## 계획 자료형

모든 객체는 선언한 필드만 허용하며 ID·참조는 공통 규격을 따른다.
설명 문자열은 비어 있지 않은 정제 문장으로 작성하고 배열의 ID·필드 선택자는 고유하게 지정한다.
목적·방법·범위·결과 제공 조건을 담으며 인증은 로컬 설정의 참조로 해소한다.

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

`task_id`와 `expected_task_revision`의 입력 위치는 도구 인수다.
사용할 목적·방법·대상·제공 범위를 기존 업무와 현재 권한에 대조한다.
자료 반환은 현재 권한·필드 정책·비밀 제거·마스킹 조건을 적용한다.
실패 처리는 [공통 기본 동작](../tasks/workflow.md#일부-조회-실패의-기본-동작)을 따른다.

기억 검색 참조는 [기억 조회 도구](tasks.md#daeri_memory_search)가 실제 발급한 기록이어야 한다.
실행기는 업무·profile 스냅샷·최신 요청과 보완 맥락·조회 당시 현재 계획 참조, 선택한 기억의 버전·현재 권한·만료·무효화를 대조한다.
`memory_refs`는 연결한 검색 기록들이 실제 반환한 개정 참조의 부분집합이다.
추가 검색은 참조 배열로 함께 연결해 서로 다른 질의에서 찾은 지식을 사용할 수 있다.
현재 맥락으로 조회하지 않았거나 선택한 기억이 유효하지 않으면 `PLAN_INVALID`로 새 조회·계획 수정을 요구한다.

0건 조회는 `memory_refs: []`로 정상 계획을 만들 수 있다.
검색 제한·실패가 있는 기록도 계획에 연결할 수 있지만 서버가 확인한 제한을 표시본과 `memory_limitations`에 남긴다.
검색 기록 발급에 필요한 저장소 확인에 실패하면 `STORE_UNAVAILABLE`로 반환한다.
검색 참조는 수행한 조회의 근거이며 지식의 실제 적용·충분성·현재 상태는 호스트가 계획과 함께 검토한다.

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
| `reconciliation` | ReconciliationTargets | 조건부 | `purpose=reconcile`의 필수 필드 |

한 단계는 인스턴스 하나와 기능 하나를 사용하며 승인 범위 안에서 여러 호출을 수행할 수 있다.
기능의 읽기·쓰기·부수 효과는 [기능 계약](resources.md)의 `effects`를 사용한다.
실제 호출 인수는 해당 기능의 `input_schema`와 승인 `scope`를 모두 충족해야 한다.

`purpose`는 기능의 `supported_purposes`와 부작용·검증 계약에 일치해야 한다.
상태별 허용 목적은 [기능 실행 규격](resources.md#daeri_capability_execute)을 따른다.

중복 단계, 존재하지 않는 의존 단계, 자기 참조와 순환을 거부한다.
현재 계획의 바인딩 출처는 의존 관계의 선행 단계여야 한다.
이전 계획의 출처는 `carried_evidence_refs`에 명시한 결과의 실제 단계·호출로만 연결한다.
승계 결과의 단계 ID·리소스·기능·계약과 원계획의 조회 범위가 현재 단계에 대응해야 한다.
첫 구현은 구조화된 조회 범위가 동등할 때만 승계를 인정한다.
승계 범위가 다르면 새 조회를 확보해 후속 입력·완료 근거를 구성한다.

`depends_on`은 다음 호출에 필요한 선행 준비 조건이다.
전체 호출·페이지 완전성은 아래 별도 완전성 검사로 확인한다.
바인딩된 의존 단계는 지정한 출처 호출의 확정·보존된 필드와 조건을 검사하고, 바인딩 없는 순서 의존은 선행 단계의 정상 완료 호출 근거를 요구한다.
전체 자료를 사용하는 연산은 기능 계약의 완전성 근거가 확보돼야 실행할 수 있다.
업무 전체의 대상·페이지 완전성은 완료 조건과 보고에서 별도로 검증한다.
`reconcile` 단계의 의존성은 아래 원 호출 확인 규칙을 우선한다.

제출 시 단계 구조·계약 참조·목적·범위 스키마·제공 상한·예산 조건을 검사한다.
이 계획 검사 실패는 `PLAN_INVALID`이며 기능 조회 자체의 준비·등록 실패는 해당 오류를 반환한다.
실제 호출 인수·기본값·변환 후 대상의 범위 판정은 실행 시 추가한다.

`scope`에는 기간·필드·대상·필터 등 기능 계약이 검증하고 실행 중 강제할 승인 조건을 담는다.
인증·연결 대상은 고정 profile에서, 리소스 작업 입력은 `parameters`에서 해소한다.
계약·실제 대상 변경이 승인한 방법·범위에 영향을 주면 새 조건의 계획을 승인받는다.

### 미확인 호출의 확인 대상

이 절은 `reconcile` 기능을 등록할 때 적용하는 확장 계약이다.
첫 버전의 실제 실행 목적은 `collect`·`verify`다.

`ReconciliationTargets`는 아래 세 배열을 모두 가지며 항목은 고유하고 하나 이상의 배열에 값이 있어야 한다.

| 필드 | 타입 | 확인할 원 호출 |
|---|---|---|
| `source_step_ids` | string[] | 같은 계획의 다른 단계; `depends_on`에도 포함 |
| `prior_call_ids` | string[] | 같은 업무의 이전 계획에 속한 미확인·진행 중 호출 |
| `external_call_refs` | CallRef[] | 같은 로컬 저장소의 종료 업무에 남은 미확인·진행 중 호출 |

`CallRef`는 필수 `task_id: string`, `call_id: string`으로 원 업무·호출을 지정한다.
계획에는 원 대상·계획·이미 요청한 변경·남은 불확실성과 확인 범위를 표시한다.
확인 단계의 의존성은 원 호출의 실행 의도·실제 요청 기록을 기준으로 판정한다.
실제 호출 선택·검증·양 업무의 결과·예산 처리는 [실행 연결 규격](resources.md#원-호출과-상태-확인의-연결)을 따른다.
새 변경은 별도 실행 승인, 자료 승계는 `carried_evidence_refs` 검사를 적용한다.

### 결과 바인딩과 조건

| InputBinding 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `binding_id` | string | 예 | 해당 단계 안에서 고유한 연결 ID |
| `source_step_id` | string | 예 | 선행 단계 또는 명시적으로 승계한 결과의 단계 |
| `source_pointer` | JsonPointer | 예 | 기능 출력에서 읽을 허용 값 위치 |
| `target_pointer` | JsonPointer 또는 null | 예 | `parameters` 안의 입력 위치; null은 조건 판단 전용 |

포인터는 [공통 경로 문법](common.md#필드-경로와-선택)을 따른다.
계획 제출은 문법·대상 경로 겹침·선행 단계 연결을 검사한다.
실행은 저장한 실제 출처 값·필드 권한과 연결 후 입력 스키마·승인 범위를 검사한다.
입력용 `target_pointer`는 비어 있지 않아야 하고 한 단계의 대상 위치끼리 충돌할 수 없다.
바인딩 대상은 해당 기능 `parameters`의 허용 입력 위치로 한정한다.
인증·인스턴스·기능·승인·예산은 각각의 고정 설정·계획 계약에서 해소한다.

| StepCondition 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `binding_id` | string | 예 | 같은 단계의 결과 바인딩 |
| `operator` | string | 예 | `exists`, `eq`, `in` |
| `value` | scalar 또는 scalar[] | 조건부 | `eq`는 scalar, `in`은 비어 있지 않은 scalar[] |

scalar는 JSON의 string·number·boolean·null이다.
`exists`는 `binding_id`·`operator`로 구성하고 여러 조건은 모두 충족해야 한다.

`exists`는 지정한 실제 필드를 읽을 수 있을 때 충족한다.
부재 분기는 기능이 반환한 명시적인 부재 표시를 `eq`·`in`으로 비교한다.
`eq`·`in`은 문자열·boolean·null·정수·실수의 원래 타입과 값을 함께 비교한다.
자료 누락·가림 등으로 값이나 부재를 확인할 수 없으면 `BINDING_INVALID`로 멈춘다.
null 비교에는 실제 확인한 null 값을 사용한다.

거짓인 조건은 외부 실행 없이 `CONDITION_NOT_MET`·`fix_input`으로 반환한다.
호스트는 다른 승인된 단계를 선택하거나 실제 조건을 만족하는 근거를 확보해 다음 호출을 구성한다.

실행 도구의 `bindings[{binding_id, source_call_id}]`로 실제 출처 호출을 고른다.
같은 출처 단계의 여러 바인딩은 이번 호출에서 같은 `source_call_id`를 사용한다.

서버가 저장 결과에서 연결 값을 해소하는 순서와 스키마·권한·범위 검사는 [기능 실행 규격](resources.md#daeri_capability_execute)을 따른다.

이전 결과의 후속 사용은 `carried_evidence_refs`에 정확한 참조를 담아 새 계획과 함께 검토한다.
승계 결과는 출처·현재 제공 권한·새 scope와 실제 효과의 확정 상태를 검사한다.

### 예산·보고·완료 조건

| PlanBudget 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `max_tool_calls` | integer | 예 | 0 이상; 논리 기능 호출의 누적 한도 |
| `max_external_requests` | integer 또는 null | 예 | 숫자는 실제 외부 요청의 누적 한도; null은 요청 수 상한 미설정 |
| `max_runtime_ms` | integer | 예 | 0 이상; DAERI의 호출 대기·실행 관리 시간의 누적 한도 |
| `resource_limits` | ResourceLimit[] | 예 | 기능별 자료·변경량 한도; 없으면 빈 배열 |

`ResourceLimit`의 필수 필드는 `instance_id: string`, `metric_id: string`, `maximum: number`다.
`maximum`은 0 이상이며 인스턴스·metric 조합은 고유하다.

관련 기능이 metric의 측정·강제를 지원해야 하며 동일 metric의 단위·집계 의미는 [기능 계약](resources.md#capabilitydescriptor)을 따른다.

0은 해당 실행량의 상한이다.
논리 호출 수의 집계 단위는 서로 다른 기능 실행 호출이다.
계획·조회와 같은 요청 재전달은 기존 소비량을 유지한다.

외부 요청 수에는 내부 페이지·재시도·상태 조회를 포함하고 승인 대기 시간은 실행 시간에서 제외한다.
실제 예약·소비량 계산은 [실행 제어](../tasks/workflow.md#실행-검사와-기록)를 따른다.

하위 MCP의 내부 요청 수를 보장할 수 없으면 `max_external_requests: null`인 계획을 사용할 수 있다.
계획 설명에는 실제 외부 요청 수의 상한이 없고 사용량도 미측정일 수 있음을 표시한다.
이 경우에도 실제 소비·승인·자료 범위는 각각의 계약으로 검사한다.

숫자 상한을 요청하면 모든 실행 단계가 `budget_support.external_requests_enforceable=true`를 충족해야 한다.
미지원이면 `PLAN_INVALID`로 보완을 요구하며 실행 시에도 강제 불가 조건을 거부한다.
새 숫자 상한을 적용하려면 동일 업무의 과거 외부 소비·예약량도 확정돼야 한다.

`max_runtime_ms`가 끝나면 DAERI의 추가 dispatch와 계속 기다리는 처리를 중단한다.
이 한도의 측정 대상은 DAERI의 대기·실행 관리 시간이다.
원격 작업의 실제 종료·CPU·과금 시간은 해당 리소스의 별도 제어·측정 계약으로 다룬다.
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
현재 파일 하나의 상한은 10 MiB다.
파일 생성에는 승인한 형식 계약과 현재 준비 상태의 일치가 필요하다.
계약·형식 변경은 수정 계획으로, 필요한 설치는 환경 준비 경로로 처리한다.

| ResourceOutputRule 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `instance_id` | string | 예 | 고정 profile 안의 원본 리소스; 인스턴스마다 고유 |
| `allowed_model_fields` | FieldSelector[] | 예 | 해당 리소스 결과 중 모델에 제공할 필드 |
| `export_fields` | FieldSelector[] | 예 | 해당 리소스 결과 중 산출물에 포함할 필드 |
| `retained_fields` | FieldSelector[] | 예 | 업무 종료 후 로컬에 유지할 최소 조회 본문 필드 |
| `memory` | MemoryRule | 예 | 이 리소스에서 선별할 지식의 범주·출처·재제공·보존 허용 |
| `masking` | MaskingRule[] | 예 | 해당 리소스의 가림 조건; 없으면 빈 배열 |

실행 단계·승계 근거·선택한 기억의 기여 리소스마다 규칙을 하나씩 명시하고 호스트 대화에서도 리소스별로 표시한다.
원본 기록의 인스턴스별 규칙을 선택하고 실제 출처·선택 필드를 대조한다.
모델 제공·내보내기·종료 후 본문 보관은 각각 해당 계획 배열과 profile의 대응 상한을 교차 검사한다.
각 배열의 빈 값은 해당 처리의 0개 필드 범위를 뜻한다.
리소스 호출·승계 근거·리소스 기억이 없는 계획에서는 `resource_rules: []`를 사용하고 요청 자료의 등록 제공 정책을 적용한다.
승계 근거는 원본 보관·제공 조건·현재 권한·새 계획의 해당 규칙을 모두 충족해야 한다.

`retained_fields`는 종료 후 본문 보관 범위다.
모델 제공·내보내기는 각 전용 필드로 검사한다.
현재 보관 권한과 비밀 제거·마스킹을 적용하며 이력·활성 업무 복구 자료의 보존은 [업무 명세](../tasks/workflow.md#이력과-완료-근거)를 따른다.
실행기는 고정 profile에서 [공통 보존기간](../tasks/workflow.md#이력과-완료-근거)을 해소해 표시한다.
같은 기간을 적용해도 조회 보관·보고·내보내기의 필드별 허용 범위는 별개다.

| MemoryRule 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `categories` | enum[] | 예 | `service_rule`, `resource_structure`, `procedure`, `lesson`; 항목 고유 |
| `source_fields` | FieldSelector[] | 예 | 후보 작성에 사용할 원본 필드; 모델 제공·본문 보관 허용의 교집합 안에서 선택 |
| `context_sources` | enum[] | 예 | 실제 사용자 확인 자료 `request`, `clarification`; 수집 범위가 없으면 [] |
| `retention` | enum | 예 | `none`, `source_bound`, `long_term` |
| `allow_model_recall` | boolean | 예 | 향후 업무의 호스트 모델에 해당 지식을 제공할 수 있는지 |

`none`이면 범주·출처 필드·사용자 확인 자료는 빈 배열이고 재제공은 false다.
그 외에는 범주와 하나 이상의 원본 필드 또는 사용자 확인 자료를 profile 정책 안에서 명시한다.
출처 필드는 후보에 사용할 자료 범위이며 장기 지식 보존은 `retention=long_term`과 해당 정책을 별도로 검사한다.
기능 출력은 `source_fields`, 실제 사용자 요청·정정은 `context_sources`로 구분해 해소한다.
실제 대상 리소스의 규칙과 해당 자료의 제공 정책을 함께 적용한다.

실행기는 profile에서 해소한 일반 경험·장기 지식의 기간과 미래 재제공 조건을 불변 표시본에 포함한다.
`long_term`은 원자료와 별개의 운영지식을 자체 기한까지 보관하는 명시적인 승인 항목이다.
상세 대상·기한·정정은 고정 설정과 [기억 보존](../tasks/memory.md#보존과-장기-운영지식)을 따른다.
혼합 출처의 허용 범위는 모든 기여 리소스 조건의 교집합이다.

기억을 보고·CSV에 인용할 때에도 원지식의 내보내기 허용과 현재 계획의 `export_fields`·마스킹을 검사한다.
인용에는 기억이 보존한 원 출처 필드 대응과 현재 제공 허용이 필요하다.
재보관·내보내기도 각 원출처의 허용 조건을 검사한다.

선택자는 [공통 문법](common.md#필드-경로와-선택)으로 기능 계약의 결과 본문을 가리킨다.
`MaskingRule`의 필수 필드는 `field: FieldSelector`, `action: "omit" | "redact"`다.
부모·자식의 선택·가림·충돌은 공통 경로 규칙으로 처리한다.

`destination`은 사람이 읽을 전달 대상 설명이다.
메일 전송·외부 게시처럼 실제 외부 전달이 필요하면 별도의 변경 기능 단계로 승인받는다.
필드·마스킹의 제품 의미는 [결과·이력 명세](../tasks/workflow.md#이력과-완료-근거)를 따른다.

### 파생 표의 허용 범위

`TableRule`은 보고·CSV의 로컬 결합·계산 범위를 승인하기 위한 자료형이다.
로컬 결합·계산의 소스·연산·한도를 지정하고 원자료는 승인된 리소스 단계에서 확보한다.

| TableRule 필드 | 타입 | 제약 |
|---|---|---|
| `table_id` | ID | 계획 안에서 고유한 파생 표 식별자 |
| `sources` | TableSourceRule[] | 하나 이상; `source_id` 고유 |
| `join_mode` | enum | `none`, `inner`, `full` |
| `coverage` | enum | `complete`, `selected` |
| `allowed_operations` | TableOperation[] | 허용할 [셀 연산](tasks.md#파생-표와-셀-검증); 중복 없는 하나 이상 |
| `max_rows` | PositiveInt | 최대 10,000의 결과 행 수 |
| `ratio_scale` | NonNegativeInt 또는 null | 비율 연산을 허용하면 0~18의 반올림 소수 자릿수; 그 밖에는 null |

`TableSourceRule`의 필수 필드는 `source_id:ID`, `step_id:ID`, `rows_pointer:JsonPointer`, `key_pointers:JsonPointer[]`다.
`step_id`는 현재 계획의 자료 확보 단계이며 저장 결과의 리소스·기능·계약이 이 단계와 일치해야 한다.
기존 결과를 재사용하려면 `carried_evidence_refs`에도 명시하고 해당 단계와의 대응을 확인한다.
승계 검사를 통과한 기존 결과는 해당 논리 단계의 근거로 재사용한다.

`rows_pointer`는 기능 결과의 객체 배열 또는 단일 객체를, `key_pointers`는 각 행 안의 결합키를 가리킨다.
단일 객체는 한 행으로 취급하며 소스별 여러 기록의 대응·페이지 완전성은 제출 시 검사한다.
객체 하나뿐인 단일 소스는 빈 키를 허용하고 그 밖에는 하나 이상의 키가 필요하다.
복합 키의 순서·타입은 모든 소스가 같아야 하며 누락·null·중복 키는 거부한다.

`none`은 단일 소스, `inner`는 모든 소스에 있는 키, `full`은 어느 소스에든 있는 키의 합집합이다.
초기 로컬 결합은 유일키의 등가 결합을 지원한다.
필요한 그룹 집계는 승인된 리소스 기능으로 먼저 확보하고 그 확정 결과를 결합·계산한다.

`complete`는 승인 범위의 원자료 완전성과 결과의 누락 없음이 모두 확인돼야 한다.
`selected`는 선택한 일부 결과로 표시하고 완료 조건의 범위와 별도로 대조한다.
결합 방식·키·허용 연산·전체성 범위를 바꾸거나 행 한도를 늘리려면 수정 계획을 승인받는다.
계산 결과·출처·마스킹·기한의 검사는 [파생 표 계약](tasks.md#파생-표와-셀-검증)으로 연결한다.

### 완료 조건

| CompletionCriterion 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `criterion_id` | string | 예 | 계획 안에서 고유한 완료 조건 |
| `description` | string | 예 | 무엇을 어떤 근거로 확인할지 설명 |
| `required` | boolean | 예 | 전체 완료에 필요한 조건인지 여부 |
| `step_ids` | string[] | 예 | 근거를 만들 단계; 모두 계획에 존재 |

`steps: []`에서도 `output`과 `completion_criteria`는 필수다.
`step_ids: []`인 조건은 요청 자료나 저장 기록에 근거한 검증에만 사용한다.
외부 변경 성공을 확인하는 조건은 해당 실제 실행 단계와 관찰 근거에 연결한다.

필수 조건의 적용 여부는 등록 관찰·승인 분기의 검증 결과로 판정한다.

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

`context`, `draft`, `contract_refs`, `render_ref`, `retention_context`는 저장한 개정의 원본을 유지한다.
계획 수정은 새 버전으로 저장하고 보존 중인 각 버전에도 [본문 기한과 정리](../tasks/workflow.md#이력과-완료-근거)를 적용한다.
`is_active`·승인 요청 상태·예산 맥락은 조회 당시 기록이며 계획 내용과 구분한다.
표시본은 [호스트 대화의 계획 제시](../hosts/approval.md#화면과-사용자-흐름)에 필요한 전체 조건에 대응하며 비밀값·내부 인증 참조를 제외한다.
현재 `render_ref`는 불변 계획 본문과 같은 관리 참조다.
호스트가 `daeri_plan_get`의 전체 계획을 읽어 사람이 확인할 설명·조건으로 제시한다.

`RetentionContext`는 필수 `task_body_days:PositiveInt|null`, `long_term_memory_days:PositiveInt|null`을 갖는다.
모델 입력이 아닌 고정 설정에서 구하며 표시본에 같은 기간 또는 무기한 여부를 보여 준다.
`task_body_days=null`은 업무 본문의 무기한 설정, `long_term_memory_days=null`은 별도 허용한 장기 운영지식의 무기한 설정이다.
업무·기여 출처·기존 자료에 유한 기한이 남아 있으면 [업무 보존 조건](../tasks/workflow.md#이력과-완료-근거)에 따라 승계한다.
개별 기억의 저장·기한은 이 설정과 함께 [기억 보존 기준](../tasks/memory.md#보존과-장기-운영지식)의 원출처·승인·기존 기한을 검사한다.

`BudgetContext`의 필수 필드는 `previous_budget: PlanBudget/null`, `consumed: BudgetAmounts`, `reserved: BudgetAmounts`, `remaining: BudgetRemaining`다.
`BudgetRemaining`은 [기능 실행 규격](resources.md)의 자료형을 사용한다.

`BudgetAmounts`는 `tool_calls: integer/null`, `external_requests: integer/null`, `runtime_ms: integer/null`, `resource_limits: MetricAmount[]`를 모두 갖는다.
`MetricAmount`는 필수 `instance_id: string`, `metric_id: string`, `amount: number/null`을 갖는다.

소비·예약은 0 이상이며 미확정량은 null로 표시한다.
새 총 한도에서 계획 교체 전후의 누적 소비량과 미정산 예약량을 모두 차감한다.
승인 전의 `remaining`은 새 총량을 기준으로 계산한 검토용 값이며 실행은 유효 승인·현재 조건 검사를 거친다.

외부 요청 상한이 null이면 그 항목의 잔여량도 null이다.
미확정 소비·예약의 null과 구분할 때는 원본 `PlanBudget`의 상한 값을 함께 확인한다.
숫자 상한을 없애는 변경도 제한 완화로 표시해 재승인받고 기존 기록은 보존한다.

## `daeri_plan_submit`

호스트가 만든 계획을 검사하고 불변 원본과 표시본을 보존한다.
초기 저장 흐름은 `planning → awaiting_approval`이며 사용자 승인 후 실행 단계로 전이한다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `request_id` | string | 예 | 공통 내용 멱등 키 |
| `task_id` | string | 예 | 제출할 업무 |
| `expected_task_revision` | integer | 예 | 1 이상; 새 제출의 현재 개정과 일치 |
| `draft` | PlanDraft | 예 | 위 자료형과 등록 기능 계약으로 검증 |

반환 `data`의 필수 필드는 `task_id`, `task_revision`, `plan_id`, `plan_version`, `contract_refs`, `render_ref`, `retention_context`, `budget_context`다.
각 타입은 공통 규격과 `SavedPlan` 표를 따른다.
성공은 `status: "ok"`이며 최초 제출 또는 교체를 기록할 때 개정을 한 번 증가시킨다.
실행기는 업무의 `TaskContext`를 SavedPlan에 연결한다.
실행 계획에는 해소한 profile 맥락·스냅샷 안의 인스턴스·준비된 등록 계약이 필요하다.
연결·로그인·진단은 관리 경로에서 준비하고 계획 제출은 그 로컬 기록을 검사한다.
기억 검색은 먼저 호출해 발급받은 기록·참조를 위 `PlanDraft` 조건으로 검사한다.

교체는 현재 계획을 `previous_plan_id`로 연결하는 재계획 경로에서만 허용한다.
최초 제출은 `planning`, 교체는 `planning|awaiting_approval|running|reconciling|verifying`에서 허용한다.
교체를 보존하면 이전 승인·대기 요청의 무효화와 새 현재 계획의 `awaiting_approval` 전이를 같은 트랜잭션으로 확정한다.
별도 재계획 도구 없이 이 제출로 승인 대기에 진입하며 진행 중인 호출은 원 계획·승인에 연결해 보존한다.
미답변 질문이 있는 `awaiting_input`에서는 먼저 보완·재개 경로로 질문을 해소한다.
`paused`에서 새 계획 제출은 `TASK_STATE_INVALID`이며 먼저 업무 재개로 상태·기존 계획을 검토한다.
진행 중 호출·미확인 외부 효과는 원 호출과 연결한 상태로 보존하고 후속 확인 범위를 검토한다.
`reconcile`을 지원하는 연결에서는 필요한 확인 단계·예산을 담은 수정 계획을 제출한다.
종료된 업무는 `TASK_STATE_INVALID`로 거부하며 기존 계획은 현재 권한·본문 보존기한 안에서 열람할 수 있다.

교체 후의 새 호출에는 새 현재 계획의 유효 승인이 필요하다.
[재계획 승계 원칙](../tasks/approval.md#수정-계획에서-작업과-예산-승계)에 따라 근거·사용량을 유지하며 멱등 처리는 공통 규격을 따른다.

## `daeri_plan_get`

저장한 계획과 현재 확인 상태·예산 맥락을 로컬 기록에서 읽는다.
현재 업무·계획 열람 권한과 제공 범위를 검사해 로컬 보존 기록을 반환한다.

| 입력 필드 | 타입 | 필수 | 제약 |
|---|---|---|---|
| `task_id` | string | 예 | 열람할 업무 |
| `plan_id` | string | 예 | 같은 업무에 저장된 특정 계획 |

반환 `data`는 필수 `task_id: string`, `task_revision: integer`, `plan: SavedPlan`을 갖는 `ok` 응답이다.
과거 계획도 열람할 수 있으며 실행 대상 여부는 `is_active`와 현재 승인·권한으로 판정한다.
조회는 기존 개정을 유지하고 대상 부재·권한 부족은 각각의 오류로 반환한다.

필수 계획 본문·표시본이 만료되면 `RECORD_EXPIRED`, `data=null`로 반환한다.
보존 중인 파일 유실·손상은 `RECORD_UNAVAILABLE`로 구분한다.
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
| `user_response` | string | 조건부 | `respond`에서 필수; 비밀을 제거하고 원래 의미·표현을 유지한 실제 답변 |
| `user_message_ref` | string | 아니요 | `respond`에서 호스트가 제공할 때만 전달하는 불투명 사용자 메시지 참조 |

`ApprovalDecision`은 `approve`, `request_changes`, `decline`, `cancel`의 enum이다.
`request`는 응답 관련 필드를 생략한 요청 분기다.
실제 사용자 답변을 받은 뒤 `respond`에 응답 필드를 전달하며 답변 전에는 `awaiting_response`를 유지한다.

`respond`의 `user_response`는 비어 있지 않은 실제 사용자 답변이다.
호스트는 응답의 비밀값만 제거하며 원래의 대상·조건·부정·결정 의미를 유지한다.
비밀 제거 후 의사가 모호하면 `decision=null`로 두고 대상·조건·결정을 다시 확인한다.
실행기도 저장·반환 전에 비밀 제거를 적용하고 정제한 답변을 `feedback`으로 보존한다.

`user_message_ref`는 실제 응답과 승인 대상을 대조하는 선택 참조다.
호스트가 이 참조를 제공할 때 전달하고 나머지 환경에서는 생략한다.
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
수정 요구는 `feedback`에 남기고 변경 조건을 반영한 새 계획을 제출한다.

실제 답변 전달과 해석의 신뢰 경계는 [호스트 승인 계약](../hosts/approval.md#대화-승인의-범위)을 따른다.

새 확인 요청과 새 응답은 `awaiting_approval`에서만 허용한다.
`paused`·종료 상태 등에서는 `TASK_STATE_INVALID`를 반환하고 현재 상태를 유지한다.
실행기는 대기 요청의 업무·계획·버전·표시본이 현재 대상과 일치하는지 확인한다.
이 연결과 현재 승인 권한을 모두 확인한 뒤 승인을 발급한다.

`approve`를 기록하기 직전에 선택 기억·검색 기록의 [현재 유효성](../tasks/memory.md#계획-전-조회와-활용)을 다시 검사한다.
계약·대상·범위·예산 등 실제 실행 조건은 각 dispatch 직전에 다시 검사한다.
승인 상태는 해당 불변 계획에 대한 사용자 동의를 기록하며 실제 호출은 그 시점의 실행 조건을 충족하면 진행한다.
기억 전제가 깨졌으면 `PLAN_INVALID`로 재조회·수정 계획을 요구하며 대기 요청의 연결 실패는 `APPROVAL_UNAVAILABLE`이다.

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
무효 요청·응답 검증 실패는 `error`로 반환한다.
도구 처리 상태·승인 요청 상태·업무 상태는 각각의 결과 필드로 확인한다.

결정과 업무 전이는 다음과 같다.
`feedback`은 사용자 답변 자료로 보존하고 계획 수정은 새 제출로 처리한다.
응답이 있으면 `feedback`은 비어 있지 않아야 한다.

| `decision` | 요청 `state` | 검증·보존 후 업무 상태 |
|---|---|---|
| null | `awaiting_response` | `awaiting_approval`; 설명·대상 확인 뒤 새 응답 대기 |
| `approve` | `approved` | `running`; 새 호출은 현재 실행 검사 적용 |
| `request_changes`, `decline` | `rejected` | `planning`; 조건 수정은 새 계획으로 제출 |
| `cancel` | `cancelled` | `awaiting_approval`; 확인 요청만 취소하고 업무는 유지 |

업무 일시중지는 [업무 수정](tasks.md#daeri_task_update)의 `action=pause`, 업무 취소는 [업무 취소](tasks.md#daeri_task_cancel)로 처리한다.
승인 도구의 `cancel`은 확인 요청 취소에 해당한다.
“이 확인 요청만 취소해 줘”와 “업무를 취소해 줘”를 구분할 수 없으면 null로 두고 뜻을 확인한다.
확인 요청 기한은 실행 승인 만료와 구분한다.

동일 요청 재전달은 기존 상태·승인·예산을 반환한다.
현재 계획의 유효 대기 요청은 하나이며 중복 생성 요청은 `APPROVAL_UNAVAILABLE`로 반환한다.
업무 취소·계획 교체·일시중지나 현재 전제 변경으로 무효화한 요청은 `invalidated`로 반환한다.
실행 가능한 `approval_id`는 현재 유효한 승인에서만 반환한다.
최종 결정은 현재 업무·요청 유효성을 다시 검사해 한 번 확정하고 원 결정을 보존한다.

현재 무효화 조건은 계획 교체·일시중지·업무 종료와 요청·profile·선택 기억 전제의 변경이다.
승인·확인 요청의 시간 제한과 개별 승인 철회 API는 확장 계약이다.
기록 본문은 별도의 [보존기한](../tasks/workflow.md#이력과-완료-근거)을 적용한다.

## 오류와 호스트 연결의 경계

사용자 응답을 현재 업무·계획·확인 요청에 연결할 수 없으면 `APPROVAL_UNAVAILABLE`로 반환한다.
연결 실패 시 현재 대기·일시중지·종료 상태를 유지한다.
호스트의 대화 응답 전달·구분과 재시작·재계획 시 연결 동작은 [호스트 검증 범위](../hosts/approval.md#구현-시-검증)를 구현 때 확인한다.
