# MCP 도구 목록과 공통 규격

상태: 첫 버전의 16개 공개 도구·공통 계약 구현; 실제 호스트 대화 검증은 별도

호스트에 공개할 도구와 공통 타입·입력·응답·오류·재전달 규칙의 정본이다.
도구별 데이터 전달 객체(DTO)는 아래 링크에서 관리한다.
구현한 도구·로컬 검증 범위는 [최초 구현 계획](../../plans/initial-implementation.md#현재-구현-단위와-검증-경계)을 따르며 실제 호스트 연결은 별도 검증이 필요하다.

## 초기 도구 목록

등록 기능을 공통 실행 도구로 호출하며 새로운 업무는 요청·계획·등록 기능의 조합으로 처리한다.
아래 16개 도구를 제공하며 총괄·작업자의 호출은 [업무 실행 조건](../tasks/workflow.md#실행-검사와-기록)을 따른다.
실제 지원 기능·제약은 등록 계약에서 확인한다.

| 도구 | 하는 일 | 상세 규격 |
|---|---|---|
| `daeri_task_create` | 요청 접수 | [업무·결과](tasks.md) |
| `daeri_task_update` | 질문·답변·맥락 보완, 업무 일시중지 | [업무·결과](tasks.md) |
| `daeri_task_get` | profile별 최근 업무·저장 상태·참조 조회 | [업무·결과](tasks.md) |
| `daeri_task_resume` | 소유권·기록 확인 후 재개 준비 | [업무·결과](tasks.md) |
| `daeri_task_cancel` | 추가 실행 중단 | [업무·결과](tasks.md) |
| `daeri_worker` | 승인한 단계의 위임·검색·실행·근거·완료·철회 | [worker 게이트웨이](tasks.md#daeri_worker) |
| `daeri_memory_search` | 현재 profile의 기본 맥락·관련 기억 조회 | [기억](tasks.md#기억-mcp-도구) |
| `daeri_memory_update` | 근거를 확인해 기억 저장·정정·무효화 | [기억](tasks.md#기억-mcp-도구) |
| `daeri_resource_list` | 등록 리소스·기능 요약 | [리소스](resources.md) |
| `daeri_capability_get` | 읽기 묶음·개별 기능의 입력·출력·제약 계약 | [리소스](resources.md) |
| `daeri_plan_submit` | 계획 검사·불변 저장 | [계획·승인](plans.md) |
| `daeri_plan_get` | 저장 계획·확인 상태 조회 | [계획·승인](plans.md) |
| `daeri_approval_request` | 호스트 대화의 승인 요청·사용자 응답 연결 | [계획·승인](plans.md) |
| `daeri_capability_execute` | 검사 후 등록 기능 실행 | [리소스](resources.md) |
| `daeri_result_submit` | 호스트 보고·파일 등록과 선별 기억 자동 처리 | [업무·결과](tasks.md) |
| `daeri_result_get` | 보존 자료의 허용 내용 조회 | [업무·결과](tasks.md) |

사용 순서·실행 검사는 [업무 흐름](../tasks/workflow.md)을 따른다.
목록·계약·상태·결과 조회는 로컬 기록만 읽고 원격 리소스 탐색·최신 조회는 승인된 기능 실행을 거친다.
기억 검색은 로컬 정본을 대상으로 하며 회사 임베딩 API 선택 시 [별도 전송 계약](../tasks/memory.md#임베딩-api-계약)을 적용한다.

## 첫 버전의 실행 범위

현재 실행 가능한 목적은 등록된 `collect`·`verify`이며 읽기·분석과 확인에 사용한다.
`change`·`reconcile` 목적과 제품별 지표 상한은 확장 계약이다.
각 호출은 등록 기능이 강제할 수 있는 제약과 현재 계획의 승인을 모두 충족해야 한다.
제약 지원 여부와 검사 실패 결과는 [기능 계약과 실행 규격](resources.md)을 따른다.

기억 검색의 혼합·키워드 전용·부분·불가 상태와 정상 0건은 [검색 도구](tasks.md#daeri_memory_search)가 정의한다.
검색은 준비된 경로를 사용하며 설치·모델 준비·운영 리소스 조회는 각 관리·실행 경로에서 처리한다.

## 호스트 노출과 힌트

호스트에는 도구의 목적·입출력·변경 여부와 승인 조건을 함께 제공한다.
리소스 실행·worker 게이트웨이·기억 검색·갱신·결과 제출은 `openWorldHint: true`로 외부 연결 가능성을 표시한다.
기억 검색·갱신과 보고의 자동 색인은 선택한 회사 임베딩 API를 사용할 수 있다.
도구 설명과 표시용 힌트는 호스트의 안내 자료다.
실제 호출 가능 여부는 실행기가 승인·권한·중복 방지 조건으로 검사한다.
읽기 기능도 이력·사용량을 바꿀 수 있으므로 원격 효과와 로컬 상태 변경을 구분한다.

## MCP와 DAERI 규격의 경계

호스트는 MCP로 도구 정의를 조회하고 구조화된 입력으로 호출한다.
실행기는 선언한 입력·출력 계약에 따라 정상·대기·부분·실패 결과를 구분해 반환한다.

DAERI의 도구 이름·업무 상태·오류 코드는 이 프로젝트의 공개 계약이다.
DAERI 실행기가 계획 승인·리소스 권한을 검사하고 MCP는 도구 정의·호출·응답을 전달한다.
에이전트의 생성·추론·오케스트레이션은 [호스트 책임](../hosts/plugins.md#root와-worker의-호스트-연결)이며 `daeri_worker`는 배정 범위의 DAERI 도구 게이트웨이다.
실행기는 stdio MCP로 도구를 제공하고 도구별 입력 스키마와 공통 출력 스키마를 반환한다.
호스트별 실행은 [플러그인 사양](../hosts/plugins.md), 지원 프로그램·잠금 조합은 [의존성 정본](../../development/dependencies.md#의존성의-정본)을 따른다.

## 입력을 구성하는 원칙

최상위 입력은 JSON object이며 모든 DTO는 명시한 확장 영역 외의 추가 필드를 거부한다.
도구 스키마의 최상위 조건은 `if`·`then`·`else`로 표현해 [호스트의 최상위 조합 변환](https://code.claude.com/docs/en/mcp#tool-input-schemas-with-a-root-level-combinator)에 의존하는 도구 제외를 해소한다.
중첩 자료형의 조합과 실행기의 엄격한 입력 검증은 각 DTO의 조건을 따른다.
필수 필드 누락·타입·enum 위반은 `INVALID_INPUT`이다.

선택 필드의 생략은 표에 정의한 기본 동작을 뜻한다.
`null`은 타입에 허용된 경우만 사용할 수 있다.
요청 JSON의 UTF-8 크기는 일반 도구에서 64 KiB, `daeri_result_submit`에서 2 MiB 이하다.
배열·숫자·페이지 크기의 추가 상한은 각 DTO에 정의한다.

`parameters`와 `scope`는 등록 기능의 `input_schema`, `scope_schema`로 검사한다.
스키마는 검증된 로컬 등록부에서 해소하고 도구 인수는 해당 스키마의 데이터로 검증한다.
리소스 고유의 조회·작업 표현은 검증한 기능·범위에 따라 해당 어댑터로 전달한다.

현재 실행기는 같은 OS 사용자의 로컬 저장소를 권한 경계로 사용하고 실행 소유권은 MCP 프로세스별로 구분한다.
root 소유자는 승인한 단계의 worker 위임을 발급하고 worker는 [토큰 게이트웨이](tasks.md#daeri_worker)에서 해당 범위의 호출 자격을 확인한다.
위임 토큰은 제한된 호출 자격이며 실제 호스트 worker의 신원·도구 권한과 전달 경로는 호스트가 관리한다.
사용자 답변의 출처·현재 대화 판별은 호스트가 담당하며 실행기는 전달받은 응답과 저장한 업무·계획·요청의 연결을 검사한다.
공통 인수는 아래 DTO의 업무 데이터로 구성한다.
사용자의 실제 대화 응답을 현재 승인 요청에 연결하는 입력은 [승인 도구](plans.md#daeri_approval_request)에서만 다룬다.

## 공통 타입

| 타입 | 규격 |
|---|---|
| `ID` | 비어 있지 않은 불투명 string; 객체 식별용 |
| `RecordRef` | 실행기가 등록한 기록·결과·산출물의 불투명 string 참조 |
| `TaskRevision` | 1 이상의 integer; 업무 기록의 현재 개정 값 |
| `PlanVersion` | 1 이상의 integer; 업무 안의 불변 계획 순서 |
| `Cursor` | 실행기가 발급한 불투명 string; 발급 당시 조회 조건에 연결 |
| `PositiveInt` | 1 이상의 integer; 페이지 크기 등 요청량 |
| `NonNegativeInt` | 0 이상의 integer; 0은 해당 실행량의 상한 |
| `JsonObject` | JSON object; 사용하는 DTO·기능의 스키마로 추가 검사 |
| `JsonValue` | JSON으로 표현 가능한 데이터 값 |
| `JsonPointer` | 아래 문법으로 저장 JSON의 특정 값 하나를 가리키는 string |
| `FieldSelector` | 아래 문법으로 제공·마스킹·보존에 적용할 필드 집합을 선택하는 object |

도구별 표의 ID·기록 참조·개정·커서 필드는 위 공통 타입을 따른다.
식별자의 연결은 [업무 흐름](../tasks/workflow.md#업무를-연결하는-식별자)을 따른다.
`task_revision`은 저장 상태의 충돌 검사 값이며 계획·프로그램 버전과 구분한다.

`RecordRef`는 실행기가 등록한 참조와 대조하고 업무 소유·현재 열람 권한·제공 범위를 검사한다.
모델에 제공할 결과와 원본 저장 기록을 구분한다.

일반 기록 메타데이터의 `expires_at`은 시간대를 포함한 ISO 8601 string 또는 null이다.
null은 시간에 따른 무기한 보존을 뜻한다.
모든 기록에 현재 상태·열람·보관 허용·삭제·무결성 검사를 적용한다.
보고·표·파일·기억에 공개하는 같은 필드도 이 형식을 사용하고, 개별 기한은 [업무 보존](../tasks/workflow.md#이력과-완료-근거)과 [기억 보존](../tasks/memory.md#보존과-장기-운영지식)에서 해소한다.
파생 자료는 가장 이른 유한 기한을 승계하며 모든 기여 조건이 무기한일 때만 null이다.

파생 보고·파일·worker 요약과 기억의 출처 검사는 시작·말단 기록을 포함해 한 경로 최대 32개, 검사 전체 최대 256개의 고유 기록을 허용한다.
같은 기록의 반복 인용은 한 번 계산하며 순환·한도 초과·확인 불가는 `RESULT_VALIDATION_FAILED`로 반환한다.

## 필드 경로와 선택

입력 바인딩은 단일 값의 `JsonPointer`, 제공·마스킹·보존 정책은 필드 집합의 `FieldSelector`를 사용한다.
어댑터와 저장·보고·기억 기능의 권한 검사는 이 공통 문법을 사용한다.

### 단일 값의 JsonPointer

[RFC 6901](https://www.rfc-editor.org/rfc/rfc6901)의 JSON 문자열 표현을 사용한다.
`/rows/0/id`는 배열 첫 행의 `id`, 빈 문자열은 JSON 전체를 가리킨다.
키에 포함된 `/`는 `~1`, `~`는 `~0`으로 쓰고 순서대로 한 번만 해소한다.

객체 키는 대소문자·유니코드 코드 포인트가 정확히 같아야 한다.
배열 인덱스는 선행 0 없는 0 이상의 정수이며 배열의 `-`는 읽을 값이 없어 거부한다.
중복 객체 키·잘못된 이스케이프·타입 불일치·범위 밖 인덱스는 해소 실패다.
객체의 `*`·`-`는 문자 그대로의 키다.

바인딩의 출처 경로는 저장한 기능 결과, 대상 경로는 호출의 `parameters`를 기준으로 해소한다.
값을 해소할 때 누락·명시적 null·문자열·숫자의 원래 상태와 타입을 유지한다.

### 필드 집합의 FieldSelector

`FieldSelector`는 필수 `segments: SelectionSegment[]`만 가진다.
각 segment는 아래 세 형태 중 정확히 하나이며 추가 필드를 거부한다.

| 형태 | 의미 |
|---|---|
| `{"key":"필드명"}` | 객체의 정확한 키; `/`·`~`·`*`도 문자 그대로 |
| `{"index":0}` | 배열의 특정 인덱스; 0 이상의 정수 |
| `{"each":true}` | 해당 배열의 모든 원소 한 단계 |

가상 예시: `{"segments":[{"key":"rows"},{"each":true},{"key":"id"}]}`는 각 행의 `id`만 선택한다.
배열이 중첩되면 `each`를 단계마다 명시한다.
부모 객체·배열에서 선택을 끝내면 그 아래 모든 말단 값을 선택한다.
빈 `segments`도 전체 본문 선택이므로 계약과 profile이 전체 범위를 명시적으로 허용할 때만 사용한다.

계획 제출은 선택자의 구조·고유성·마스킹 충돌과 profile의 선택자 상한을 검사한다.
실행·조회는 실제 본문에서 말단 경로로 확장해 현재 제공·보관 정책을 적용한다.
기능 출력의 스키마 검증과 실제 값의 선택 해소는 각 경계에서 수행한다.
스키마가 허용한 선택 필드의 부재는 0개 선택으로 해소하고 빈 배열은 빈 구조 값으로 유지한다.
선택 중 객체·배열의 실제 타입이 다르면 선택 해소 실패다.
바인딩은 단일 경로를 해소하고 실제 제공이 허용된 원본 값과 대조한다.
선택자는 고유하며 한 마스킹 목록의 부모·자식 겹침이나 충돌은 검증 실패다.

서로 다른 제공 정책은 말단 경로의 교집합으로, 마스킹은 그 뒤에 적용한다.
`omit`은 해당 경로를 제거하고 `redact`는 값 내용을 가린다.
부모 객체를 반환할 때에도 모든 자손의 가림 결과를 유지한다.
제공·보존·바인딩은 허용 범위와 출처를 해소한 값에 적용한다.

바인딩 출처의 누락·가림·허용 필드 이탈은 `BINDING_INVALID`다.
기능 결과의 부재 표시는 해당 기능이 실제 부재를 확정한 근거를 요구하며 잘림·가림은 미확인 상태로 처리한다.
입력 바인딩의 같은 대상·부모/자식 대상 겹침은 `PLAN_INVALID`로 거부한다.
JSON 경로 검사와 함께 실제 리소스의 대상·범위를 검사하며 자연어 의미는 호스트가 근거와 대조한다.

## 업무 개정과 멱등 요청

root의 새 상태 변경은 `request_id`와 `expected_task_revision`을 사용한다.
업무 생성에는 비교할 기존 개정 값이 없으므로 `request_id`만 받는다.
기능 실행은 `call_id`가 실행 요청의 멱등 키를 맡는다.

실행기는 주체·도구·업무의 요청 기록을 먼저 확인한다.
같은 키·동일 정규화 입력은 기존 상태만 반환하고 다른 입력은 충돌로 거부한다.
비교 입력에는 최초 `expected_task_revision`도 포함하며 응답 유실 시 인수·키를 그대로 재전달한다.

새 요청이면 실제 `task_revision`과 기대 값을 비교한다.
다르면 `REVISION_CONFLICT`로 반환하고 업무를 다시 읽어 현재 계획·상태를 확인한다.
충돌 후에는 이미 수행된 동작도 대조한다.

root의 계획 제출·직접 기능 실행·보고 보존은 현재 업무의 실행 소유자도 검사한다.
다른 유효 소유자가 있으면 `OWNER_CONFLICT`로 거부하며 이전은 업무 재개 도구에서 처리한다.
업무 일시중지·취소와 실제 승인 응답은 각 동작의 주체 권한을 별도로 검사한다.
worker의 위임 생성·철회는 root 소유권·기대 개정으로, worker 실행·검색·읽기·완료는 현재 유효한 위임 토큰으로 검사한다.
동시 worker의 실행과 완료는 root 업무의 기대 개정 입력 없이 같은 업무의 원자적 예산·호출 기록을 갱신한다.
완료의 `request_id`와 실행의 `call_id` 재전달은 [worker 분기별 멱등 조건](tasks.md#멱등재전달과-수명)을 따른다.

업무·계획·승인·결과에 새 상태를 보존할 때 개정 값을 증가시킨다.
조회와 동일 요청 재전달은 기존 개정·사용량을 유지한다.
외부 호출의 시도·완료 기록도 업무 개정을 변경할 수 있다.

조회 커서는 발급한 MCP 프로세스의 메모리에 보관한다.
프로세스 재시작이나 커서 저장 상한 256개에 따른 초기화 뒤에는 첫 페이지부터 다시 조회한다.
현재 커서의 유효성은 조회 대상·현재 자료 서명으로 판정하며 별도 시간 만료 설정은 확장 범위다.

기억 검색은 불변 검색 확인 기록을 발급하며 업무 상태·개정·실행 예산은 기존 값을 유지한다.
검색 재시도는 새 검색 기록을 만들 수 있으며 로컬 처리 상한을 적용한다.
기억 갱신은 상태 변경 요청으로서 공통 멱등·개정·소유권 검사와 기억 개정 충돌 검사를 적용한다.

재전달에도 현재 열람·제공 권한과 본문·산출물 보존기한을 적용한다.
승인 결정의 반환 값은 조회 시점의 현재 유효성을 반영한다.

요청·호출 키와 비밀을 제외한 최소 입력 비교 정보·처리 상태는 [최소 추적 이력](../tasks/workflow.md#이력과-완료-근거)으로 보존한다.
본문 만료 뒤에도 같은 요청은 보존한 처리 상태로 식별한다.
첫 버전의 최소 이력은 비밀을 제거한 비교 지문·처리 상태로 구성하고 SQLite에 무기한 보존한다.
본문·산출물은 각 전용 저장 영역에서 별도 보존기한을 적용한다.

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
`pending`은 확인 또는 외부 완료 대기 상태이며 최종 결정은 후속 기록으로 확인한다.

호스트가 읽는 구조화 결과와 사람이 읽는 설명의 상태·근거·자료 범위를 일치시킨다.
MCP의 `structuredContent`에 이 객체를 담고 같은 JSON을 텍스트 내용에도 제공한다.
두 표현의 상태·내용과 `isError`의 일치를 통합 검사한다.

## 오류와 다음 행동

`ToolError`는 `code`, `message`, `next_action`의 필수 string 필드로 구성한다.
`message`에는 정제한 원인·영향·후속 행동을 담고 민감 원문은 원래 제공·보존 정책으로 관리한다.
코드는 아래 집합에서 선택하고 대상별 추가 원인은 보존한 실행 근거로 연결한다.

| 코드 | 의미 | 대표 다음 행동 |
|---|---|---|
| `INVALID_INPUT` | 타입·필드·입력 조건 오류 | `fix_input` |
| `NOT_FOUND` | 요청한 업무·대상·참조를 찾을 수 없음 | `refresh` |
| `ACCESS_DENIED` | 현재 주체의 접근·제공 권한 부족 | `stop` |
| `PROFILE_DELETING` | profile 연쇄 삭제가 진행 중 | `stop` |
| `PROFILE_DELETED` | 삭제 완료한 profile의 참조 | `stop` |
| `REVISION_CONFLICT` | 기대 업무 개정과 현재 값이 다름 | `refresh` |
| `REQUEST_ID_CONFLICT` | 같은 요청 키에 다른 입력 사용 | `stop` |
| `CALL_ID_CONFLICT` | 같은 호출 키에 다른 실행 입력 사용 | `stop` |
| `TASK_STATE_INVALID` | 현재 상태에서 허용하지 않는 동작 | `refresh` |
| `OWNER_CONFLICT` | 다른 유효 실행 소유자가 있음 | `refresh` |
| `WORKER_CONFLICT` | 단계가 활성 worker에 배정됐거나 진행 중·미확인 호출과 충돌 | `refresh` |
| `WORKER_UNAVAILABLE` | 위임 상태·기한·root 소유권·계획·승인·업무 조건이 유효하지 않음 | `stop` |
| `CURSOR_INVALID` | 커서와 대상·조건·권한·기한 불일치 | `refresh` |
| `CONTRACT_CHANGED` | 저장한 기능 계약과 현재 계약 불일치 | `refresh` |
| `CONTEXT_REQUIRED` | 업무의 DAERI profile 맥락이 아직 해소되지 않음 | `fix_input` |
| `CONTEXT_CHANGED` | 고정한 profile 바인딩과 현재 대상·주체·정책 불일치 | `stop` |
| `CONNECTION_NOT_READY` | 필요한 실행 환경·리소스 연결·진단이 준비되지 않음 | `stop` |
| `AUTHENTICATION_REQUIRED` | 지정한 인증 참조에 로그인·비밀 등록·잠금 해제가 필요함 | `stop` |
| `PLAN_INVALID` | 계획 구조·참조·범위·한도 검사 실패 | `fix_input` |
| `APPROVAL_UNAVAILABLE` | 사용자 대화 응답을 현재 업무·계획에 연결할 수 없음 | `stop` |
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
| `INTENT_PERSIST_FAILED` | 실행 전 기록 보존 실패; dispatch 전 중단 | `retry_same_request` |
| `RESULT_PERSIST_FAILED` | 외부 실행 후 결과 보존 실패 | `reconcile` |
| `STORE_UNAVAILABLE` | 신뢰할 업무·승인·호출 기록을 읽을 수 없음 | `stop` |
| `RESULT_TRUNCATED` | 원본 결과가 잘리거나 일부만 확보됨 | `refresh` 또는 `request_approval` |
| `RESULT_VALIDATION_FAILED` | 기능 출력·보고 구조·완료 근거·파일 등록 검사 실패; 확인한 외부 효과는 유지 | `fix_input` |
| `RECORD_EXPIRED` | 필요한 기록의 보존 기한 종료 | `stop` |
| `RECORD_UNAVAILABLE` | 기록 내용의 손실·삭제·무결성 문제 | `stop` |
| `MEMORY_SEARCH_INCOMPLETE` | 한쪽 검색만 가능하거나 기억 색인의 일부를 사용할 수 없음 | `refresh` |
| `MEMORY_SEARCH_UNAVAILABLE` | 신뢰할 정책·업무 기록은 있으나 기억 검색을 수행하지 못함 | `refresh` |
| `MEMORY_INDEX_INCOMPLETE` | 기억 정본은 보존했으나 색인 갱신이 끝나지 않음 | `refresh` |
| `MEMORY_CONFLICT` | 정정·무효화 대상의 현재 기억 개정과 입력 참조가 다름 | `refresh` |

`next_action`은 `fix_input`, `refresh`, `request_approval`, `reconcile`, `retry_same_request`, `stop` 중 하나다.
후속 호출은 `next_action`을 참고해 구성하고 현재 승인·권한·멱등 규칙을 다시 검사한다.

`APPROVAL_EXPIRED`·`APPROVAL_REVOKED`와 `INTENT_PERSIST_FAILED`는 확장 계약이다.
조회 뒤 결과·메타데이터 저장 실패는 `RESULT_PERSIST_FAILED`로 반환하며 원 호출의 확인·부분 마감으로 이어간다.
현재 승인 무효화와 저장 실패의 실제 처리는 [승인 유효성](plans.md#approvaloutcome과-실제-응답)·[실행 결과](resources.md#결과와-오류)에서 구분한다.

연결·인증 오류의 `stop`은 현재 새 리소스 호출을 멈춘다는 뜻이다.
관리 경로에서 원 인증 주체와 대상을 복구한 뒤 현재 계획·권한을 다시 검사할 수 있다.
복구 대상은 원 계정·profile이며 설치·로그인은 관리 경로에서 처리한다.
구성 교체가 필요한 경우의 새 업무 경계는 [profile 명세](../configuration/profiles.md)를 따른다.

`reconcile`은 원 호출의 미확인 결과 확인을 우선하는 후속 행동이다.
첫 버전의 실행 목적은 `collect`·`verify`이며 미확인 호출은 확인 대기 상태로 유지한다.
기록 저장소가 복구되지 않았다면 먼저 저장소 문제를 해결한다.
입력 정정·기능 재조회 후에도 범위 변경이 필요하면 새 계획을 승인받는다.

## 프로토콜 오류와 사용자 확인

알 수 없는 도구나 잘못된 프로토콜 요청은 MCP의 프로토콜 오류로 처리한다.
알려진 DAERI 도구의 입력·권한·계획·리소스 오류는 위 완료 응답으로 전달한다.
프로토콜 오류 뒤에는 보존한 호출 기록으로 외부 실행 여부를 확인하고 공통 멱등 규칙에 따라 같은 요청을 재전달한다.

실제 사용자 응답의 취득·해석·현재 계획 대조와 호스트별 검증 범위는 [호스트 승인](../hosts/approval.md), 입력·전이는 [승인 도구](plans.md#daeri_approval_request)를 따른다.

기존 MCP를 재사용하면 상위 호스트와 하위 서버의 인증·지원 기능은 독립적으로 검사한다.
하위 도구의 추가 입력·권한 확인은 DAERI 계획 승인과 구분한다.
연결·응답 경계는 [MCP 재사용](../resources/contract.md#연결-구조)을 따른다.
도구 처리 상태·업무 결과·사용자 승인 상태는 각각의 기록으로 판정한다.

## 스키마와 호환성 검증

공개 입력의 필수 필드·타입·허용 값과 공통 응답·오류를 자동 테스트와 실제 stdio 요청으로 검사한다.
도구 간 업무·계획·승인·호출·보고·기억 참조의 연결은 로컬 통합 시험으로 확인한다.
실제 Codex·Claude Code의 설치·사용자 답변 의미 해석·대화 UI 동작은 별도로 검증해야 한다.
