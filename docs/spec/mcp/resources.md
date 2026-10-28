# 리소스 탐색과 기능 실행 MCP 도구

상태: 등록 연결 탐색·읽기 실행·로컬 Source 관찰 등록; 로컬 계약 검증, 실제 회사·호스트 연결은 별도

입력·반환 DTO를 정의한다.
연결·인증·도구 등록은 [리소스 계약](../resources/contract.md), 공통 응답은 [MCP 공통](common.md)을 따른다.

## `daeri_resource_list`

현재 업무의 profile에 등록한 연결·기능 요약을 로컬 기록에서 읽는다.

| 입력 | 타입·기본값 |
|---|---|
| `task_id` | 필수 ID |
| `types` | 선택; 고유한 범주 ID 배열, 생략 시 전체 |
| `cursor` | 선택 Cursor |
| `limit` | 선택 integer; 기본 20, 1~100 |

반환 data는 `task_id`, `task_revision`, `instances`, `next_cursor`, `has_more`다.
각 인스턴스는 `instance_id`, `type`, `label`, `capability_summaries`를 갖는다.
호스트 연결인 로컬 Source 항목에는 `target: {path: string}`도 제공하며 호스트는 이 등록 루트에서 소스를 조사한다.
기능 요약은 `capability_id`, `contract_ref`, `description`, `changes_state:false`다.

준비한 연결은 개별 기능과 `*` 읽기 묶음을 함께 표시한다.
진단이 필요한 연결은 빈 기능 배열로, 연결이 없으면 빈 instances로 반환한다.
목록의 페이지와 실제 리소스 조회의 페이지는 별개다.

## `daeri_capability_get`

필수 입력은 `task_id:ID`, `instance_id:ID`, `capability_id:ID`다.
반환 data는 `task_id`, `task_revision`, `descriptor:CapabilityDescriptor`다.
조회는 로컬 등록 기록을 읽으며 실제 서비스 호출은 아래 실행 도구를 사용한다.

### `CapabilityDescriptor`

| 필드 | 의미 |
|---|---|
| `instance_id`, `capability_id`, `contract_ref` | 연결·기능·저장 계약 참조 |
| `description` | 기능 설명 |
| `input_schema`, `output_schema` | 요청 인수·제공 결과의 JSON Schema |
| `scope_schema` | `equals`·`one_of`·`path_prefixes`·`max_values` 객체와 등록 제약 |
| `supported_purposes` | 현재 `["collect","verify"]` |
| `effects` | `changes_state:false`, 읽기의 로그·요금·부하와 중단 특성 |
| `retry` | 실제 시도 1회·live 읽기; 다음 관찰은 호스트가 선택 |
| `budget_support`, `supported_metrics` | 하위 요청 수 강제 false·지표 [] |
| `idempotency`, `verification`, `continuation` | 하위 중복 억제·별도 완료 확인·자동 페이지 연결의 현재 미지원 표시 |
| `metadata_discovery_allowed` | 등록한 구조 메타데이터 제공 여부 |

입력 스키마는 실행·관찰 기능의 실제 계약, 출력 스키마는 어댑터가 정규화한 제공 결과다.
페이지 인수가 있는 하위 도구는 호스트가 실제 응답·cursor·total을 보고 반복 호출한다.
등록 범위·읽기 판정은 [어댑터 기준](../resources/adapters.md)을 따른다.

### 리소스 읽기 묶음

`capability_id:"*"`는 한 연결에서 진단한 읽기 기능의 묶음이다.
`capability_refs[{capability_id,contract_ref}]`와 `capability_constraints`에 구성 기능·제약을 제공한다.
공통 입력·출력은 빈 객체 스키마이며 실제 호출에는 개별 기능의 스키마를 적용한다.

계획은 연결별 묶음을 승인받고 호스트가 목적에 맞는 실제 도구를 선택할 수 있다.
실행에서는 개별 `capability_id`를 지정하며 기록에는 승인 묶음과 실제 선택 기능을 함께 보존한다.
연결·구성 기능·명시적 제약·입출력 의미의 변경은 묶음 참조를 갱신한다.

## `daeri_capability_execute`

현재 승인 계획의 단계에서 실제 읽기 기능을 호출하거나 호스트의 Source 관찰을 등록한다.

### 입력

| 필드 | 타입 | 필수·기본값 |
|---|---|---|
| `task_id` | ID | 필수 |
| `step_id` | ID | 필수; 현재 승인 계획의 단계 |
| `parameters` | JsonObject | 필수; 실제 도구 인수 또는 Source 관찰 입력 |
| `capability_id` | ID | 선택; 개별 기능 단계는 자동 해소, 묶음은 실제 기능 지정 |
| `instance_id` | ID | 선택; 단계의 연결로 해소 |

현재 계획·승인·계약 참조·개정·호출 식별자는 runtime이 연결한다.
호스트는 실제 결과에서 후속 인수를 구성하고 [승인한 수행 순서](plans.md#후속-결과의-사용)를 따른다.

runtime은 현재 승인·위임·실제 대상·입력 스키마·명시적 범위와 선택한 호출·시간 상한을 검사한다.
고정값·하위 기본값·어댑터 변환을 적용한 최종 인수로 검사·전송한다.
읽기 한 번의 timeout은 총괄 에이전트 기본 60초이며 명시한 전체 잔여 시간이 더 짧으면 그 값을 사용한다.

### 로컬 Source 관찰 등록

`local_source` 연결의 기능 ID는 `source-observe`, 도구 이름은 `source_observe`다.
호스트가 승인된 계획 범위에서 기존 파일·검색·실행·Git 도구로 조사한 후 이 기능을 호출한다.
읽기 묶음으로 승인한 단계에서는 `capability_id: source-observe`를 함께 전달한다.

| `parameters` 필드 | 타입·의미 |
|---|---|
| `summary` | 필수 string; 공백 이외의 내용이 있는 호스트 관찰 요약 |
| `paths` | 선택 string[]; 고유한 상대 경로 최대 100개, 기본 [] |

반환한 정규화 자료는 `path`(등록 루트), `summary`, `paths`, `observation_source: "host"`다.
runtime은 현재 루트의 존재·읽기·탐색 접근과 각 상대 경로의 존재·루트 포함 관계를 검사한다.
루트가 유효한 절대 디렉터리가 아니면 `INVALID_INPUT`, 관찰 경로 부재는 `NOT_FOUND`, 루트 이탈은 `SCOPE_VIOLATION`이다.
진단 뒤 루트의 실제 대상이 바뀌면 `CONTEXT_CHANGED`로 경로 재설정·진단을 안내한다.
파일·Git 실행과 요약의 의미·충분성 확인은 호스트가, 경로 검사와 관찰 보존은 runtime이 담당한다.
등록 결과의 `evidence_refs`를 기존 보고·파일·기억의 실제 기여 출처로 사용한다.
연결·준비·실행 책임은 [로컬 Source 계약](../resources/adapters.md#로컬-소스-코드)을 따른다.

### 원 호출과 상태 확인의 연결

같은 진행 중 요청은 기존 호출 상태를 반환한다.
읽기 실패·단절·프로세스 종료 뒤에는 보존 상태를 확인하고 필요한 새 관찰을 실행한다.
진행 중인 호출과 재개는 [업무 상태](tasks.md#daeri_task_resume)로 확인한다.

### 반환 `data`

| 필드 | 타입·의미 |
|---|---|
| `task_id`, `task_revision` | 업무·현재 저장 개정 |
| `call_id`, `plan_id`, `plan_version`, `step_id` | runtime이 연결한 실행 기록 |
| `call_state` | `pending\|completed\|blocked\|failed` |
| `external_effect` | 현재 읽기는 `none` |
| `actual_capability_id`, `actual_contract_ref` | 실제 선택 기능·계약; 이전 기록은 생략 가능 |
| `result_ref` | 보존한 결과 참조 또는 null |
| `inline_result` | 현재 제공 가능한 정규화 결과 또는 null |
| `evidence_refs` | 사용 가능한 근거 참조 배열 |
| `warnings` | 실제 잘림·제공 제한 |
| `usage` | `logical_calls`, `external_requests`, `runtime_ms`, `metrics` |
| `budget_remaining` | 현재 호출·시간 잔여량과 기존 저장 메타데이터 |
| `retry_safety` | `automatic_retry_allowed`, `effect_known`, `reason` |

하위 MCP로 전송한 호출의 external_requests는 미측정 null, metrics는 []다.
runtime_ms는 실제 로컬 대기·관리 시간이며 하위 서비스의 종료 보장과 구분한다.
전체 상한이 없으면 해당 잔여량은 null이다.

`inline_result`는 비밀 제거·현재 제공 조건·출력 스키마를 통과한 본문이다.
확보한 결과·잘림·실패는 실제 관찰대로 기록하고 호스트가 후속 페이지와 완료 여부를 판단한다.

### 결과와 오류

| 상황 | 처리 |
|---|---|
| 정상 읽기 | `ok`, `completed` |
| 같은 요청이 진행 중 | `pending`; 저장 호출 상태 |
| 전송 전 조건 변경 | `error`, `blocked`; 원인 코드 |
| 하위에서 확인한 실패 | `error`, `failed`, `EXTERNAL_EXECUTION_FAILED` |
| 단절·timeout·실행 프로세스 종료 | `failed`, `READ_INTERRUPTED`; 상태 확인 후 새 관찰 가능 |
| 출력 검사 실패 | `RESULT_VALIDATION_FAILED`; 확보하지 못한 본문은 null |
| 알려진 원격 잘림 | 확보한 본문·근거와 `partial`, `RESULT_TRUNCATED` |
| 응답 처리 크기 초과 | `error`, `RESULT_TRUNCATED`; 본문 null |
| 결과 저장 실패 | `RESULT_PERSIST_FAILED`; 저장소 복구 뒤 기록 확인·새 관찰 |

조회 실패는 오류와 실제 확보 범위를 기록한다.
runtime은 소비·시간 예약을 정리하고 읽기 재시도 여부는 호스트가 승인 범위 안에서 선택한다.
늦게 끝난 호출은 실제 결과를 보존하면서 일시중지·취소 상태를 유지한다.

## 구현과 검증 경계

[리소스 등록·전송](../../../src/daeri/infrastructure/resources.py)과 [runtime](../../../src/daeri/application/runtime.py)이 담당한다.
로컬 stdio의 탐색·호출·인증·범위·실패와 Source의 경로·호스트 관찰 등록을 구분해 확인하며 [검증 범위](../../development/dependencies.md#구현과-대표-검증)를 따른다.
