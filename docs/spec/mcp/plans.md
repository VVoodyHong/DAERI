# 계획과 승인 MCP 도구

상태: 읽기 업무의 계획 저장·대화 승인 구현; 로컬 계약·통합 검증

호스트는 목표·연결·수행 범위·결과·완료 근거를 계획으로 제시한다.
runtime은 등록 연결과 기본 정책을 해소해 저장하며 실제 사용자 응답 뒤 실행을 허용한다.
공통 타입·응답은 [MCP 공통](common.md), 제품 동작은 [계획·승인](../tasks/approval.md)을 따른다.

## 계획 자료형

아래 표는 모델용 공개 입력이다.
진단 지문·현재 계획 개정·승인 참조·자료 정책은 runtime이 저장 계획에 연결한다.

### `PlanDraft`

| 필드 | 타입 | 필수·기본값 |
|---|---|---|
| `purpose` | string | 필수; 업무 목적 |
| `steps` | PlanStep[] | 필수; 수행할 연결·방법 |
| `output` | PlanOutput | 필수; 표현·전달 위치 |
| `completion_criteria` | CompletionCriterion[] | 필수; 하나 이상 |
| `budget` | PlanBudget | 선택; 전체 호출·시간 상한 없음 |
| `memory_search_refs` | RecordRef[] | 선택; [] |
| `memory_refs` | RecordRef[] | 선택; [] |
| `memory_limitations` | string[] | 선택; [] |
| `carried_evidence_refs` | RecordRef[] | 선택; 재계획에 승계할 실제 근거 |

검색 기록은 선택한 기억의 출처를 확인하는 자료다.
검색을 생략하거나 일부 경로가 실패해도 현재 자료로 계획을 구성할 수 있다.
선택한 기억은 실제 검색 결과·현재 profile·유효 개정과 대조하고 검색의 제한을 계획에 반영한다.

### `PlanStep`

| 필드 | 타입 | 필수·기본값 |
|---|---|---|
| `step_id` | ID | 필수; 계획 안에서 고유 |
| `description` | string | 필수; 수행 설명 |
| `instance_id` | ID | 필수; 등록 연결 |
| `capability_id` | ID | 선택; 기본 `*` 읽기 묶음 |
| `purpose` | `collect\|verify` | 선택; `collect` |
| `scope` | JsonObject | 선택; {} |
| `depends_on` | ID[] | 선택; [] |

한 단계는 연결 하나의 읽기·Source 관찰 기능 또는 [읽기 묶음](resources.md#리소스-읽기-묶음)을 선택한다.
빈 scope는 해당 연결의 실제 읽기 권한 영역이나 등록한 Source 디렉터리이며 명시한 조건은 매 호출에 적용한다.
호스트는 단계 안에서 질의·페이지·추가 관찰·병렬 수행을 선택하고 선행 관계는 실제 완료 근거로 확인한다.

### 후속 결과의 사용

호스트는 실제 읽기 결과를 보고 후속 도구의 parameters와 수행 순서를 구성한다.
Source는 [호스트 조사 후 등록한 관찰](resources.md#로컬-source-관찰-등록)을 후속 근거로 사용한다.
필수 선행 순서는 depends_on으로 표현하고 runtime은 해당 단계의 실제 완료 근거를 확인한다.
질의·분석·추가 관찰의 선택은 승인한 연결·scope와 완료 목표 안에서 이뤄진다.

### 예산·보고·완료 조건

| 타입 | 공개 필드·기본값 |
|---|---|
| `PlanBudget` | 선택 `max_tool_calls:integer\|null`, `max_runtime_ms:integer\|null`; 각각 기본 null |
| `PlanOutput` | 필수 `format:string`, 선택 `destination:string`; 기본 `conversation` |
| `CompletionCriterion` | 필수 `criterion_id:ID`, `description:string`, `step_ids:ID[]`; 선택 `required:boolean`, 기본 true |

0 이상의 명시적 호출·시간 상한은 같은 업무의 누적 소비·진행 중 예약에 적용한다.
새 계획의 전체 상한은 이미 사용·예약한 양 이상으로 정한다.
상한을 생략하면 전체 업무 한도는 없으며 개별 읽기의 timeout은 실행 경계에서 적용한다.

모델 제공·파일·보관·기억 범위는 등록 profile에서 자동으로 해소한다.
기존에 명시한 필드·마스킹·보존·기억 정책은 해당 범위를 유지하고 선별 기억 저장·향후 회상을 계획에 포함한다.

완료 조건 ID는 고유하고 참조 단계는 현재 계획에 속한다.
호스트는 [보고 검사](tasks.md#reportcheck)에서 각 조건의 실제 근거·충분성·제한을 판단한다.

### 저장 계획과 예산 맥락

저장 계획은 입력 초안과 해소한 연결·기능·정책·보존 맥락을 보관한다.
`plan_id`·`plan_version`·`contract_refs`는 이 저장 기록을 식별하는 반환값이다.
`budget_context`는 이전 상한·현재 소비·예약·잔여량을 표시한다.

## `daeri_plan_submit`

필수 입력은 `task_id:ID`, `draft:PlanDraft`다.
현재 소유자가 최초·수정 계획을 제출하고 runtime은 구조·연결·범위·기억·선행 근거·완료 조건을 검사한다.
같은 내용은 현재 계획을 유지하며 수정 내용은 새 불변 계획으로 저장한다.

저장과 함께 업무를 `awaiting_approval`로 바꾸고 대화 승인 대기를 만든다.
수정 계획은 이전 승인·하위 에이전트 위임을 갱신하며 이전 근거는 명시한 `carried_evidence_refs`로 이어받는다.
연결·대상 변경의 업무 경계는 [등록 변경 기준](../resources/contract.md#등록부와-계약-변경)을 따른다.
사용자에게는 저장한 계획·명시한 제한·기대 결과·선별 기억을 설명한다.

반환 data는 `task_id`, `task_revision`, `plan_id`, `plan_version`, `contract_refs`, `render_ref`, `retention_context`, `approval_request`, `budget_context`, `recall_limitations`다.
계획 검증 실패는 `PLAN_INVALID`이며 초안을 수정해 다시 제출한다.

## `daeri_plan_get`

필수 입력은 `task_id:ID`, `plan_id:ID`다.
반환 data는 `task_id`, `task_revision`, `plan`이며 plan에 저장 초안·식별자·context·계약 참조·render 참조·보존 맥락·활성 여부·승인·예산 맥락을 제공한다.
계획 본문은 저장한 당시 내용이고 승인 상태·현재 자료 접근은 조회 시점에 확인한다.

## `daeri_approval_request`

계획을 표시한 뒤 실제 사용자 응답을 전달한다.
승인 대기는 계획 제출에서 이미 생성되므로 이 도구는 응답을 연결한다.

| 필드 | 타입 | 필수·의미 |
|---|---|---|
| `task_id` | ID | 필수; 현재 업무 |
| `plan_id` | ID | 필수; 사용자에게 표시한 현재 계획 |
| `decision` | `approve\|request_changes\|decline\|cancel\|null` | 필수; 실제 응답의 해석 |
| `user_response` | string | 필수; 실제 사용자 답변 |
| `user_message_ref` | ID | 선택; 호스트가 제공한 실제 메시지 참조 |

### 응답 해석과 검사의 책임

호스트는 현재 계획에 대한 실제 사용자 의사와 보완 조건을 판단한다.
runtime은 현재 대기·업무·계획·저장 응답을 연결한다.
응답이 불명확하면 null로 기록해 확인을 이어가며 범위 변경은 수정 계획을 표시한다.

### `ApprovalOutcome`과 실제 응답

반환 data는 `task_id`, `task_revision`, `approval_request_id`, `state`, `decision`, `feedback`, `recall_limitations`와 유효할 때의 `approval_id`다.
이 식별자는 runtime이 기록하며 후속 실행에서 자동으로 해소한다.
계획 제출·승인 응답의 `recall_limitations`는 현재 선택 기억을 사용할 수 없을 때 `{memory_ref,error_code}` 목록을 반환한다.
호스트는 이 제한을 보고 같은 승인 범위의 새 관찰로 보완하며 범위가 바뀌면 수정 계획을 제시한다.

| 결정 | 승인 상태·업무 동작 |
|---|---|
| `approve` | `approved`; 업무 `running` |
| `request_changes`, `decline` | `rejected`; 수정 계획 작성 |
| `cancel` | 해당 승인 대기 `cancelled`; 업무 종료는 [취소 도구](tasks.md#daeri_task_cancel) 사용 |
| null | `awaiting_response`; 실제 의사 확인 |

대기는 `status:pending`, 처리한 응답은 `ok`다.
같은 승인 응답은 보존한 결정을 반환하고 현재 유효성도 함께 표시한다.
일시중지·취소·새 계획·실제 연결 변경은 해당 실행 조건을 다시 확인한다.

## 오류와 호스트 연결의 경계

승인은 현재 계획을 수행할 사용자 의사를 확인한다.
하위 서비스의 로그인·실제 읽기 권한과 현재 자료 접근은 호출·조회 경계에서 검사한다.
전체 흐름은 [호스트 승인](../hosts/approval.md)을 따른다.

## 구현과 검증 경계

[DTO](../../../src/daeri/core/contracts.py)·[계획 검사](../../../src/daeri/application/planning.py)·[runtime](../../../src/daeri/application/runtime.py)이 담당한다.
선택 입력·실제 승인·재계획·범위·예산은 로컬 테스트로 확인하며 실제 대화·회사 리소스는 [검증 범위](../../plans/initial-implementation.md#현재-구현-단위와-검증-경계)를 따른다.
