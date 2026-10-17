# 문서 구조와 정본

상태: 문서 체계 초안

이 문서는 정보를 저장할 위치와 작업별 참조 경로를 정의한다.
대상 독자는 문서를 수정하는 사람과 개발 에이전트다.
공통 작업 진입점은 [AGENTS](../../AGENTS.md)다.

## 분류 기준

따라야 할 기준은 `rules`, 제품 동작은 `spec`, 책임과 연결은 `architecture`에 둔다.
개발 도구와 문서 체계를 설명하는 내용은 `development`에 둔다.
현재 단계와 다음 작업은 `current-state.md`에서 관리한다.

파일 수보다 독자가 찾는 질문과 변경 책임을 기준으로 나눈다.
각 문서는 자신이 소유하는 내용과 적용 범위를 짧게 밝힌다.
인접한 계약은 링크하고 전체 내용을 복사하지 않는다.

## 디렉터리와 정본

| 위치 | 소유하는 내용 | 정본 |
|---|---|---|
| 루트 | 기본 지침과 호스트 진입점 | [AGENTS](../../AGENTS.md), [CLAUDE](../../CLAUDE.md) |
| `docs/rules/` | 상세 작업 규칙 | [문서 작성](../rules/documentation.md), [Markdown 표현](../rules/markdown.md), [개발](../rules/development.md) |
| `docs/architecture/` | 구성 요소의 책임과 연결 | [구조 설명](../architecture/overview.md) |
| `docs/architecture/decisions/` | 중요한 선택의 이유와 대안 | [호스트 판단 방식](../architecture/decisions/0001-host-agent-local-runtime.md), [공통 지침 방식](../architecture/decisions/0002-shared-agent-instructions.md) |
| `docs/spec/` | 목적과 확정 제품 조건 | [제품 요구사항](../spec/product.md) |
| `docs/spec/tasks/` | 업무 수행 계약 | [흐름·상태](../spec/tasks/workflow.md), [계획·승인](../spec/tasks/approval.md), [결과·이력](../spec/tasks/results.md), [사례](../spec/tasks/scenarios.md) |
| `docs/spec/resources/` | 리소스 실행 계약과 초기 연동 | [공통 계약](../spec/resources/contract.md), [최초 어댑터](../spec/resources/initial-adapters.md) |
| `docs/spec/installation/` | 로컬 환경과 배포 계약 | [환경 준비](../spec/installation/environment.md), [배포·업데이트](../spec/installation/distribution.md) |
| `docs/development/` | 문서 구조와 도구 동작 설명 | 이 문서, [문맥 복구](agent-context.md) |
| `docs/current-state.md` | 단계·미결·검증·다음 작업 | [현재 상태](../current-state.md) |

현재는 README를 작성하지 않는다.
문서가 정리되면 사용자에게 읽기 경로와 사용 방법을 안내하는 진입점으로 추가한다.

## 주제별 경계

업무 흐름 문서는 승인 단계가 언제 나타나는지를 설명한다.
승인 문서는 계획의 범위와 승인·차단 조건을 정의한다.
결과 문서는 이력·근거·완료 검증을 정의한다.
사례는 이 계약을 설명하며 새로운 규칙을 정의하지 않는다.

리소스 공통 계약과 최초 제품별 기능을 구분한다.
설치 환경 문서는 필요한 프로그램의 검사·준비를, 배포 문서는 패키지 전달과 업데이트를 소유한다.
규칙 문서에서 미결 기술 선택을 제품의 확정 조건으로 바꾸지 않는다.

## 작업별 참조 경로

| 작업 | 관련 정본 |
|---|---|
| 새 작업·인계·압축 후 재개 | AGENTS → 현재 상태 → 이 문서 → 작업별 규칙과 관련 명세 |
| 문서 작성·수정 | 문서 작성 + Markdown 표현 + 수정할 주제의 정본 |
| 사용자 흐름·상태 변경 | 제품 + 업무 흐름 + 관련 승인·결과 계약 |
| 승인·허용 범위 변경 | 계획·승인 + 업무 흐름 + 리소스 계약 |
| 리소스·어댑터 변경 | 공통 계약 + 최초 어댑터 + 계획·승인 + 구조 |
| 설치·배포 변경 | 환경 준비 + 배포·업데이트 + 최초 어댑터 + ADR-0001 |
| 구성 요소 책임 변경 | 구조 + 관련 명세 + 관련 ADR |
| 규칙·문서 체계 변경 | AGENTS + 관련 rules + 이 문서 + 문맥 복구 + ADR-0002 |

링크는 파일을 찾아 읽는 경로다.
본문 전체가 호스트에 자동 로딩된다는 뜻은 아니다.
코드 변경에는 개발 규칙도 함께 적용한다.

## 확장과 이관

독립적으로 찾거나 수정할 내용이 생기면 문서를 분리한다.
짧은 내용이 같은 질문에 답한다면 기존 문서의 절로 유지한다.
분리 때문에 같은 계약의 반복 설명이 늘어나면 경계를 다시 검토한다.

실제 절차가 생기면 사용자 안내는 `docs/how-to/`, 실행 계획은 `docs/plans/`에 추가한다.
어댑터별 상세 계약이 길어지면 리소스 디렉터리에서 해당 문서를 추가한다.
빈 분류와 제목만 있는 문서를 미리 늘리지 않는다.

변경과 이관은 [문서 작성 규칙](../rules/documentation.md#변경과-이관)을 따른다.
현재 요구 동작은 명세에서, 선택의 이유는 ADR에서 유지한다.
제품의 실행 이력과 이 저장소의 개발 진행 기록은 구분한다.

## 참고한 문서 설계

[Diátaxis 적용 지침](https://diataxis.fr/how-to-use-diataxis/)은 실제 내용을 작은 단위로 개선하며 문서 구조를 발전시키는 접근을 제시한다.
이 저장소는 설명·참조·절차를 구분하는 원칙을 주제별 디렉터리에 적용했다.
디렉터리 이름 자체를 Diátaxis의 필수 규격으로 취급하지 않는다.

[Google 문서 구성 지침](https://developers.google.com/tech-writing/one/documents)은 독자와 목적을 먼저 정의하고 내용을 구성하도록 안내한다.
이를 바탕으로 DAERI에서는 독자의 질문과 변경 책임이 다른 내용을 분리했다.
구체적인 파일 경계는 프로젝트에 맞춘 설계 판단이다.

[Google 문단 작성 지침](https://developers.google.com/style/paragraph-structure)은 한 주제를 중심으로 문단을 구성하는 기준을 제공한다.
가독성의 상세 기준은 Markdown 규칙에서 관리한다.

[MADR](https://adr.github.io/madr/)의 Markdown 결정 기록 방식을 참고한다.
ADR에는 상태·배경·대안·선택·영향·관련 정본을 기록한다.
날짜와 변경 이력은 Git으로 관리한다.
