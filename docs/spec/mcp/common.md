# MCP 도구 목록과 공통 규격

상태: 기존 로컬 제품의 16개 공개 도구 계약; 로컬·stdio 통합 검증, 실제 회사 연결·호스트 대화는 별도.

모델용 입력과 runtime의 응답을 정의한다.
업무 동작은 [업무 흐름](../tasks/workflow.md), 입력 모델은 [contracts.py](../../../src/daeri/core/contracts.py)가 담당한다.
클라우드 접속부·공개 API는 [후속 구현 범위](../../development/dependencies.md#클라우드-개발-상태)다.

## 도구 목록

| 도구 | 역할 | 정본 |
|---|---|---|
| `daeri_task_create` | 자연어 업무 접수 | [업무](tasks.md#daeri_task_create) |
| `daeri_task_update` | 질문·답변·맥락 보완·일시중지 | [업무](tasks.md#daeri_task_update) |
| `daeri_task_get` | 최근 업무·현재 기록 조회 | [업무](tasks.md#daeri_task_get) |
| `daeri_task_resume` | 소유권·읽기 기록 확인 후 재개 | [업무](tasks.md#daeri_task_resume) |
| `daeri_task_cancel` | 추가 실행 중단 | [업무](tasks.md#daeri_task_cancel) |
| `daeri_subagent` | 하위 에이전트 배정·검색·실행·결과 회수 | [하위 에이전트](tasks.md#daeri_subagent) |
| `daeri_memory_search` | 같은 profile의 관련 기억 검색 | [기억](tasks.md#daeri_memory_search) |
| `daeri_memory_update` | 기억 저장·정정·무효화 | [기억](tasks.md#daeri_memory_update) |
| `daeri_resource_list` | 등록 연결·기능 요약 조회 | [리소스](resources.md#daeri_resource_list) |
| `daeri_capability_get` | 읽기·관찰 기능과 묶음의 스키마 조회 | [리소스](resources.md#daeri_capability_get) |
| `daeri_plan_submit` | 계획 저장과 승인 대기 생성 | [계획](plans.md#daeri_plan_submit) |
| `daeri_plan_get` | 저장 계획·승인 상태 조회 | [계획](plans.md#daeri_plan_get) |
| `daeri_approval_request` | 실제 사용자 응답을 계획에 연결 | [승인](plans.md#daeri_approval_request) |
| `daeri_capability_execute` | 승인 단계의 읽기 실행·Source 관찰 등록 | [리소스](resources.md#daeri_capability_execute) |
| `daeri_result_submit` | 보고·파일과 선별 기억 저장 | [결과](tasks.md#daeri_result_submit) |
| `daeri_result_get` | 보존 기록·파일 조회 | [결과](tasks.md#daeri_result_get) |

## 실행 범위

등록한 읽기 기능의 `collect`·`verify`를 실행한다.
호스트는 승인한 연결·범위 안에서 도구·질의·페이지·병렬 하위 에이전트·분석 방법을 선택한다.
실제 읽기·변환은 [리소스 계약](../resources/contract.md), 보고·기억은 [업무 MCP](tasks.md)를 따른다.

## 호스트 노출과 힌트

MCP annotation은 호스트의 선택을 돕는 표시 정보다.
검색·기억 갱신·보고 후처리는 선택한 회사 임베딩 API를 사용할 수 있어 `openWorldHint:true`를 표시한다.
실제 처리 조건은 runtime과 [임베딩 계약](../tasks/memory.md#임베딩-api-계약)이 검사한다.

## MCP와 DAERI 규격의 경계

MCP는 도구 정의·호출·응답을 전달하고 호스트가 추론·대화·하위 에이전트를 운영한다.
runtime은 실제 계획 승인·연결·인증·명시적 범위·기록을 검사한다.
사용자 응답의 출처·의미와 하위 에이전트의 생성·도구 접근은 [호스트](../hosts/plugins.md#총괄-에이전트와-하위-에이전트의-호스트-연결)가 담당한다.

## 입력을 구성하는 원칙

입력은 JSON object이며 공개 스키마의 필수값·타입·enum·추가 필드를 검사한다.
생략한 선택값은 도구별 기본 동작을 사용하고 `null`은 허용된 타입에서 사용한다.
일반 입력은 UTF-8 JSON 64KiB, 보고 제출은 2MiB까지 받는다.

모델은 목적·대상·인수·실제 응답·결과를 전달한다.
runtime은 현재 개정·승인 참조·진단 지문·요청과 호출 식별자를 해소한다.
이전 호출의 관리 필드는 저장 요청 호환 경로에서 처리하며 모델용 입력 스키마에서는 제외한다.

최상위 분기 조건은 `if/then/else`로 제공한다.
중첩 타입과 실제 검증은 각 도구의 스키마를 따른다.

## 공통 타입

| 타입 | 의미 |
|---|---|
| `ID` | 비어 있지 않은 string 식별자 |
| `RecordRef` | runtime이 발급한 기록·기억·파일의 불투명 참조 |
| `TaskRevision`, `PlanVersion` | 반환·저장 기록의 1 이상 integer 개정 |
| `Cursor` | 조회 대상·조건·본문에 연결한 불투명 string |
| `JsonObject`, `JsonValue` | 유한 숫자를 포함하는 JSON 객체·값 |
| `FieldSelector` | 기존 명시적 자료 정책의 필드 집합 선택자 |

기록의 `expires_at`은 시간대를 포함한 ISO 8601 string 또는 `null`이다.
`null`은 무기한이며 현재 profile·대상·삭제·무효화·무결성 검사는 계속 적용한다.
보존 동작은 [업무 이력](../tasks/workflow.md#이력과-완료-근거)과 [기억](../tasks/memory.md#보존과-장기-운영지식)을 따른다.

## 필드 경로와 선택

### 필드 집합의 FieldSelector

기존 명시적 정책은 `{"segments":[...]}`를 사용한다.
각 segment는 `{"key":"id"}`, `{"index":0}`, `{"each":true}` 중 하나이며 빈 segments는 전체 본문이다.
부모 선택은 하위 값을 포함하고 `each`는 배열 한 단계를 펼친다.

현재 설정과 저장 당시 제공 범위의 교집합에 `omit`·`redact` 마스킹을 적용한다.
잘못된 구조·중복·충돌은 검증 실패다.
일반 업무의 정책은 [등록 profile에서 해소](plans.md#예산보고완료-조건)한다.

## 업무 개정과 멱등 요청

업무·계획·호출의 식별자와 개정은 runtime이 기록한다.
승인 응답·동일 보고의 재전달은 보존 상태를 반환하고 같은 내용의 계획 제출은 현재 계획을 유지한다.
진행 중인 같은 읽기 요청은 해당 호출 상태를 반환하며 완료 후의 새 요청은 새 관찰로 실행한다.

이전 호출자가 명시한 요청·호출 키는 같은 입력의 재전달에 사용한다.
같은 키의 다른 입력은 충돌이며 저장 트랜잭션은 동시 변경의 정합성을 유지한다.
모델의 정상 흐름은 개정 번호나 별도 요청 키 입력 없이 진행한다.

조회 커서는 발급한 MCP 프로세스에 보관한다.
재시작·조회 조건·현재 본문 변경으로 무효해지면 첫 페이지부터 다시 읽는다.
상태 조회와 기억 검색은 로컬 관리 연산으로 처리한다.

## 완료 응답

| 필드 | 타입·의미 |
|---|---|
| `status` | `ok` 완료, `pending` 대기, `partial` 일부 확보, `error` 실패 |
| `data` | 도구별 object 또는 null; 실패에도 확보한 기록을 포함할 수 있음 |
| `error` | null 또는 `{code,message,next_action}` |

MCP `structuredContent`와 텍스트에는 같은 응답 JSON을 제공한다.
`partial`·`error`의 `isError`는 true이고 나머지는 false다.
업무 상태와 도구 처리 상태는 별개이므로 실패 보고를 정상 저장하면 도구는 `ok`다.

## 오류와 다음 행동

| 주요 코드 | 의미·후속 동작 |
|---|---|
| `INVALID_INPUT`, `PLAN_INVALID` | 필드·타입·계획 조건 수정 |
| `NOT_FOUND`, `CURSOR_INVALID` | 현재 대상·첫 페이지 재조회 |
| `CONTEXT_REQUIRED` | 사용할 profile 선택 |
| `CONNECTION_NOT_READY`, `AUTHENTICATION_REQUIRED` | 연결 준비·인증 복구 |
| `CONTEXT_CHANGED`, `CONTRACT_CHANGED` | 실제 변경을 확인하고 [연결·업무 갱신](../resources/contract.md#등록부와-계약-변경) |
| `APPROVAL_REQUIRED`, `APPROVAL_UNAVAILABLE` | 현재 계획과 실제 사용자 응답 확인 |
| `SCOPE_VIOLATION`, `BUDGET_EXCEEDED` | 승인 범위·명시한 상한 확인; 확대 시 수정 계획 |
| `TASK_STATE_INVALID`, `OWNER_CONFLICT`, `SUBAGENT_UNAVAILABLE` | 현재 업무·소유권·위임 조회 |
| `ACCESS_DENIED`, `UNSUPPORTED_CONSTRAINT` | 실제 자료 조건·지원 범위 확인 |
| `EXTERNAL_EXECUTION_FAILED` | 하위에서 확인한 읽기 실패 |
| `READ_INTERRUPTED`, `RESULT_PERSIST_FAILED` | 결과 미확보·저장 실패; 저장소 확인 후 새 관찰 |
| `RESULT_VALIDATION_FAILED`, `RESULT_TRUNCATED` | 출력·출처·파일·처리 크기 확인 |
| `STORE_UNAVAILABLE`, `RECORD_UNAVAILABLE`, `RECORD_EXPIRED` | 저장소·자료 무결성·기한 확인 |
| `MEMORY_SEARCH_INCOMPLETE`, `MEMORY_SEARCH_UNAVAILABLE` | 검색 제한을 표시하고 현재 자료로 업무 진행 |
| `MEMORY_INDEX_INCOMPLETE`, `MEMORY_CONFLICT` | 기억 정본·현재 개정·색인 상태 확인 |
| `PROFILE_DELETING`, `PROFILE_DELETED` | profile 삭제 상태 확인 |

`next_action`은 `fix_input|refresh|request_approval|retry_same_request|stop` 중 후속 처리를 안내한다.
현재 읽기 실패·단절은 [실행 결과](resources.md#결과와-오류)로 정리하고 호스트가 새 관찰 여부를 판단한다.
실제 인증·대상·저장 문제는 해당 원인을 복구한 뒤 다시 검사한다.

## 프로토콜 오류와 사용자 확인

알 수 없는 도구·잘못된 MCP 요청은 프로토콜 오류로 처리한다.
알려진 도구의 입력·업무 오류는 위 공통 응답으로 전달한다.
실제 대화 응답은 [승인 도구](plans.md#daeri_approval_request)로 연결한다.

입력·응답과 실제 stdio 전달의 담당 구현·검증 범위는 [개발 안내](../../development/dependencies.md#구현과-대표-검증)를 따른다.
