# MCP 도구 목록과 공통 규격

상태: DAERI 도구 규격 설계 제안

호스트에 공개할 도구와 공통 타입·입력·응답·오류·재전달 규칙의 정본이다.
도구별 DTO는 아래 링크에서 관리하며 실제 구현·호스트 연결은 검증하지 않았다.

## 초기 도구 목록

업무별 도구를 늘리지 않고 등록 기능을 공통 실행 도구로 호출한다.
아래 13개 이름은 설계 제안이며 직접 승인·인증 변경·검사 우회 도구는 제공하지 않는다.

| 도구 | 하는 일 | 상세 규격 |
|---|---|---|
| `daeri_task_create` | 요청 접수 | [업무·결과](tasks.md) |
| `daeri_task_update` | 질문·답변·맥락 보완, 업무 일시중지 | [업무·결과](tasks.md) |
| `daeri_task_get` | profile별 최근 업무·저장 상태·참조 조회 | [업무·결과](tasks.md) |
| `daeri_task_resume` | 소유권·기록 확인 후 재개 준비 | [업무·결과](tasks.md) |
| `daeri_task_cancel` | 추가 실행 중단 | [업무·결과](tasks.md) |
| `daeri_resource_list` | 등록 리소스·기능 요약 | [리소스](resources.md) |
| `daeri_capability_get` | 기능의 입력·출력·제약 계약 | [리소스](resources.md) |
| `daeri_plan_submit` | 계획 검사·불변 저장 | [계획·승인](plans.md) |
| `daeri_plan_get` | 저장 계획·확인 상태 조회 | [계획·승인](plans.md) |
| `daeri_approval_request` | 실제 사용자 확인 연결 | [계획·승인](plans.md) |
| `daeri_capability_execute` | 검사 후 등록 기능 실행 | [리소스](resources.md) |
| `daeri_result_submit` | 완료 근거·보고·산출물 보존 | [업무·결과](tasks.md) |
| `daeri_result_get` | 보존 자료의 허용 내용 조회 | [업무·결과](tasks.md) |

사용 순서·실행 검사는 [업무 흐름](../tasks/workflow.md)을 따른다.
목록·계약·상태·결과 조회는 로컬 기록만 읽고 원격 탐색·최신 조회는 승인된 기능 실행을 거친다.

## 호스트 노출과 힌트

`tools/list`는 공통 도구를 일정한 순서로 제공한다.
실제 호출 가능 여부는 각 도구에서 검사하며 description에 동작·변경 상태·승인 조건·거부 사유를 표시한다.

| 구분 | `readOnlyHint` | `openWorldHint` |
|---|---|---|
| task_get·resource_list·capability_get·plan_get·result_get | true | false |
| 나머지 로컬 상태 변경 도구 | false | false |
| approval_request·capability_execute | false | true |

상태 변경 도구의 `idempotentHint`는 보수적으로 false다.
범용 실행의 `destructiveHint`는 true이며 읽기 실행도 이력·예산을 바꾸므로 `readOnlyHint=false`다.
[MCP annotations](https://modelcontextprotocol.io/specification/latest/schema#toolannotations)는 승인·권한·중복 방지의 대체가 아니다.

## MCP와 DAERI 규격의 경계

호스트는 MCP의 `tools/list`로 도구 정의를 얻고 `tools/call`로 호출한다.
정의에 `name`, `description`, `inputSchema`, `outputSchema`를 제공한다.
[MCP Tools](https://modelcontextprotocol.io/specification/latest/server/tools)에 따라 입력은 `params.arguments`, DAERI 완료 응답은 `result.structuredContent`에 둔다.

DAERI의 도구 이름·업무 상태·오류 코드는 이 프로젝트의 설계 제안이다.
MCP 표준이 DAERI의 계획 승인이나 리소스 권한을 대신 검사하지 않는다.
`outputSchema`는 정상·대기·부분·실패 응답을 모두 표현한다.

필요한 프로토콜 메타데이터·초기화·지원 선언은 호스트 연결 계층이 처리한다.
지원할 프로토콜·SDK·호스트의 조합은 추후 dependency 목록과 실제 호환성 검사에서 관리한다.

## 입력을 구성하는 원칙

최상위 입력은 JSON object이며 모든 DTO는 명시한 확장 영역 외의 추가 필드를 거부한다.
필수 필드 누락·타입·enum 위반은 `INVALID_INPUT`이다.

선택 필드의 생략은 표에 정의한 기본 동작을 뜻한다.
`null`은 타입에 허용된 경우만 사용할 수 있다.
문자열·배열·중첩 깊이·요청 바이트의 상한은 실행기 설정으로 제한한다.

`parameters`와 `scope`는 등록 기능의 `input_schema`, `scope_schema`로 검사한다.
외부 URL의 스키마를 자동 다운로드하거나 도구 인수를 로컬 코드로 평가하지 않는다.
등록된 SQL·원격 스크립트 실행은 검증한 기능·범위에 따라 해당 어댑터로 전달하며 인수 평가와 구분한다.

실제 요청자·세션·권한은 실행기가 확인한 호출 문맥과 로컬 설정에서 구한다.
모델 인수와 클라이언트의 표시용 이름을 인증 증거로 사용하지 않는다.
승인 여부·권한·인증 비밀값을 제출하는 공통 인수는 두지 않는다.

## 공통 타입

| 타입 | 규격 |
|---|---|
| `ID` | 비어 있지 않은 불투명 string; 다른 객체의 권한을 증명하지 않음 |
| `RecordRef` | 실행기가 등록한 기록·결과·산출물의 불투명 string 참조 |
| `TaskRevision` | 1 이상의 integer; 업무 기록의 현재 개정 값 |
| `PlanVersion` | 1 이상의 integer; 업무 안의 불변 계획 순서 |
| `Cursor` | 실행기가 발급한 불투명 string; 다른 조건으로 재사용 불가 |
| `PositiveInt` | 1 이상의 integer; 페이지 크기 등 요청량 |
| `NonNegativeInt` | 0 이상의 integer; 0은 해당 실행량을 허용하지 않음 |
| `JsonObject` | JSON object; 사용하는 DTO·기능의 스키마로 추가 검사 |
| `JsonValue` | JSON으로 표현 가능한 값; 그 자체를 로컬 실행 코드나 권한 선언으로 해석하지 않음 |

도구별 표의 ID·기록 참조·개정·커서 필드는 위 공통 타입을 따른다.
식별자의 연결은 [업무 흐름](../tasks/workflow.md#업무를-연결하는-식별자)을 따른다.
`task_revision`은 저장 상태의 충돌 검사 값이며 계획·프로그램 버전과 구분한다.

`RecordRef`에 URL이나 로컬 경로를 넣어 임의 읽기·쓰기를 요청할 수 없다.
기록 참조도 업무 소유·현재 열람 권한·제공 범위를 검사한다.
모델에 제공할 결과와 원본 저장 기록을 구분한다.

## 업무 개정과 멱등 요청

새 상태 변경은 `request_id`와 `expected_task_revision`을 사용한다.
업무 생성에는 비교할 기존 개정 값이 없으므로 `request_id`만 받는다.
기능 실행은 `call_id`가 실행 요청의 멱등 키를 맡는다.

실행기는 주체·도구·업무의 요청 기록을 먼저 확인한다.
같은 키·동일 정규화 입력은 기존 상태만 반환하고 다른 입력은 충돌로 거부한다.
비교 입력에는 최초 `expected_task_revision`도 포함하며 응답 유실 시 인수·키를 그대로 재전달한다.

새 요청이면 실제 `task_revision`과 기대 값을 비교한다.
다르면 `REVISION_CONFLICT`로 거부하며 자동으로 최신 계획을 선택하지 않는다.
충돌 후에는 업무를 다시 읽고 이미 수행된 동작을 확인한다.

계획 제출·기능 실행·보고 보존은 현재 업무의 실행 소유자도 검사한다.
다른 유효 소유자가 있으면 `OWNER_CONFLICT`로 거부하며 이전은 업무 재개 도구에서 처리한다.
업무 일시중지·취소와 실제 승인 응답은 각 동작의 주체 권한을 별도로 검사한다.

업무·계획·승인·결과에 새 상태를 보존할 때 개정 값을 증가시킨다.
조회와 단순 중복 수신은 값과 사용량을 증가시키지 않는다.
외부 호출의 시도·완료 기록도 업무 개정을 변경할 수 있다.

재전달에도 현재 열람·제공 권한과 본문·산출물 보존기한을 적용한다.
승인 결정은 현재 유효성을 표시하며 과거 성공 응답으로 철회된 권한·승인을 복원하지 않는다.

요청·호출 키와 비밀을 제외한 최소 입력 비교 정보·처리 상태는 [최소 추적 이력](../tasks/workflow.md#이력과-완료-근거)으로 보존한다.
본문 만료 뒤에도 같은 요청을 새 외부 실행으로 처리하지 않는다.
초기에는 이 비교 정보를 자동 만료하지 않으며 실제 저장 표현은 구현할 때 정한다.

## 완료 응답

DAERI 완료 응답은 다음 세 필드를 항상 가진다.

| 필드 | 타입 | 조건 |
|---|---|---|
| `status` | `ok / pending / partial / error` | 아래 표의 도구 처리 상태 |
| `data` | 도구별 object 또는 null | 해당 도구의 DTO; 실패에도 확보한 참조·자료를 담을 수 있음 |
| `error` | `ToolError` 또는 null | `partial`, `error`에는 원인 포함; `ok`, `pending`은 null |

| `status` | 의미 | MCP `isError` |
|---|---|---|
| `ok` | 도구의 요청한 처리를 완료 | false |
| `pending` | 확인이나 외부 완료를 기다리며 아직 처리가 끝나지 않음 | false |
| `partial` | 도구 결과의 일부만 확보했고 제한·실패가 있음 | true |
| `error` | 도구 요청을 거부하거나 완료하지 못함 | true |

업무 결과 상태와 도구 처리 상태는 다르다.
실패한 업무의 보고를 정상 보존했으면 보고 도구는 `ok`다.
`pending`을 승인·외부 변경 성공·업무 완료로 해석하지 않는다.

`content`에는 같은 허용 응답 JSON을 text로 직렬화해 구조화 응답과 상태·근거·자료 범위를 일치시킨다.
바깥 필드는 [MCP 결과 명세](https://modelcontextprotocol.io/specification/latest/server/tools#structured-content)를 따른다.

## 오류와 다음 행동

`ToolError`는 `code`, `message`, `next_action`의 필수 string 필드로 구성한다.
`message`에는 비밀값·민감 원문·원격 오류의 전체 내용을 복사하지 않는다.
코드는 아래 집합에서 선택하고 대상별 추가 원인은 보존한 실행 근거로 연결한다.

| 코드 | 의미 | 대표 다음 행동 |
|---|---|---|
| `INVALID_INPUT` | 타입·필드·입력 조건 오류 | `fix_input` |
| `NOT_FOUND` | 요청한 업무·대상·참조를 찾을 수 없음 | `refresh` |
| `ACCESS_DENIED` | 현재 주체의 접근·제공 권한 부족 | `stop` |
| `REVISION_CONFLICT` | 기대 업무 개정과 현재 값이 다름 | `refresh` |
| `REQUEST_ID_CONFLICT` | 같은 요청 키에 다른 입력 사용 | `stop` |
| `CALL_ID_CONFLICT` | 같은 호출 키에 다른 실행 입력 사용 | `stop` |
| `TASK_STATE_INVALID` | 현재 상태에서 허용하지 않는 동작 | `refresh` |
| `OWNER_CONFLICT` | 다른 유효 실행 소유자가 있음 | `refresh` |
| `CURSOR_INVALID` | 커서와 대상·조건·권한·기한 불일치 | `refresh` |
| `CONTRACT_CHANGED` | 저장한 기능 계약과 현재 계약 불일치 | `refresh` |
| `CONTEXT_REQUIRED` | 업무의 DAERI profile 맥락이 아직 해소되지 않음 | `fix_input` |
| `CONTEXT_CHANGED` | 고정한 profile 바인딩과 현재 대상·주체·정책 불일치 | `stop` |
| `CONNECTION_NOT_READY` | 필요한 실행 환경·MCP 연결·TLS·진단이 준비되지 않음 | `stop` |
| `AUTHENTICATION_REQUIRED` | 지정한 인증 참조에 로그인·비밀 등록·잠금 해제가 필요함 | `stop` |
| `PLAN_INVALID` | 계획 구조·참조·범위·한도 검사 실패 | `fix_input` |
| `APPROVAL_UNAVAILABLE` | 신뢰할 실제 사용자 확인 경로 없음 | `stop` |
| `APPROVAL_REQUIRED` | 현재 계획의 유효 승인 없음 | `request_approval` |
| `APPROVAL_EXPIRED` | 승인 유효기간 종료 | `request_approval` |
| `APPROVAL_REVOKED` | 기존 승인 철회 | `request_approval` |
| `SCOPE_VIOLATION` | 승인한 방법·대상·제공 범위 이탈 | `request_approval` |
| `BINDING_INVALID` | 후속 값의 출처·범위·확정 여부 검증 실패 | `fix_input` |
| `CONDITION_NOT_MET` | 승인한 분기 조건이 실제 근거에서 false | `fix_input` |
| `UNSUPPORTED_CONSTRAINT` | 해당 기능이 요구 제약을 강제하지 못함 | `stop` |
| `BUDGET_EXCEEDED` | 소비·예약을 포함한 누적 한도 부족 | `request_approval` |
| `EXTERNAL_EXECUTION_FAILED` | 원격 호출의 실패가 확인됨; 효과 여부는 별도 기록 | `fix_input` 또는 `stop` |
| `EXTERNAL_OUTCOME_UNKNOWN` | 이미 요청한 외부 작업의 효과 미확인 | `reconcile` |
| `INTENT_PERSIST_FAILED` | 실행 전 기록 보존 실패; 외부 호출하지 않음 | `retry_same_request` |
| `RESULT_PERSIST_FAILED` | 외부 실행 후 결과 보존 실패 | `reconcile` |
| `STORE_UNAVAILABLE` | 신뢰할 업무·승인·호출 기록을 읽을 수 없음 | `stop` |
| `RESULT_TRUNCATED` | 원본 결과가 잘리거나 일부만 확보됨 | `refresh` 또는 `request_approval` |
| `RESULT_VALIDATION_FAILED` | 완료 주장·근거·산출물 검증 실패 | `fix_input` |
| `RECORD_EXPIRED` | 필요한 기록의 보존 기한 종료 | `stop` |
| `RECORD_UNAVAILABLE` | 기록 내용의 손실·삭제·무결성 문제 | `stop` |

`next_action`은 `fix_input`, `refresh`, `request_approval`, `reconcile`, `retry_same_request`, `stop` 중 하나다.
다음 행동은 추가 실행 권한이 아니며 자동 재시도·새 호출 ID 발급의 근거로 사용하지 않는다.

연결·인증 오류의 `stop`은 현재 새 리소스 호출을 멈춘다는 뜻이다.
관리 경로에서 원 인증 주체와 대상을 복구한 뒤 현재 계획·권한을 다시 검사할 수 있다.
다른 계정·profile로 대체하거나 업무 도구가 자동 설치·로그인을 수행하지 않는다.
구성 교체가 필요한 경우의 새 업무 경계는 [profile 명세](../configuration/profiles.md)를 따른다.

`reconcile`도 승인·현재 권한·남은 예산이 있어야 리소스를 호출할 수 있다.
기록 저장소가 복구되지 않았다면 먼저 저장소 문제를 해결한다.
입력 정정·기능 재조회 후에도 범위 변경이 필요하면 새 계획을 승인받는다.

## 프로토콜 오류와 사용자 확인

알 수 없는 도구나 잘못된 프로토콜 요청은 MCP의 프로토콜 오류로 처리한다.
알려진 DAERI 도구의 입력·권한·계획·리소스 오류는 위 완료 응답으로 전달한다.
프로토콜 오류에는 외부 실행이 있었는지 확인할 수 없는 경우가 있으므로 재전달을 성공으로 추정하지 않는다.

사용자 확인의 추가 입력 요청은 DAERI 완료 응답·승인과 구분한다.
신뢰할 응답을 확인한 뒤 최종 DAERI 응답을 보존한다.

현재 공식 명세의 `input_required` 방식에서는 `inputRequests`, `requestState`, `inputResponses`를 프로토콜 계층이 처리한다.
정확한 호스트별 연결 방식은 [MCP 호환성 명세](https://modelcontextprotocol.io/specification/latest/basic/lifecycle)와 실제 호스트 검사로 정한다.

이 값들을 모델이 제출하는 `arguments`의 승인 필드로 옮기지 않는다.
출처·확인 요청·불변 계획·현재 사용자 권한의 대조는 [승인 명세](../hosts/approval.md)를 따른다.

기존 MCP를 재사용하면 상위 호스트와 하위 서버의 인증·지원 기능은 독립적으로 검사한다.
하위의 추가 입력을 DAERI 승인 인수로 바꾸지 않으며 연결·응답 경계는 [MCP 재사용](../resources/contract.md#연결-구조)을 따른다.

MCP의 `resultType: complete`는 도구 요청 완료이며 업무의 `succeeded`·실제 승인을 뜻하지 않는다.
추가 입력 처리의 근거는 [MCP elicitation 명세](https://modelcontextprotocol.io/specification/latest/client/elicitation)에 있다.

## 스키마와 호환성 검증

표·DTO를 필수 필드·enum·`additionalProperties` 규칙으로 표현하고 입력·모든 완료 응답 스키마를 검사한다.
프로토콜의 입력 요청 결과는 도구별 완료 `outputSchema`와 별도로 검사한다.

SDK 스키마 생성, 호스트의 구조화 결과 처리, 실제 확인 응답과 오류 전달은 아직 검증하지 않았다.
