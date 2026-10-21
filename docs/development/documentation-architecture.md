# 문서 구조와 읽기 경로

상태: 공통 문서 체계

정본 위치와 작업에 필요한 읽기 순서를 안내한다.
같은 계약은 한 곳에 두고 설명·전송 규격·작업 규칙을 구분한다.

## 처음 읽기

설치·첫 사용은 [사용자 안내](../../README.md)에서 시작한다.
[제품 명세](../spec/product.md) → [구조 설명](../architecture/overview.md) → [업무 흐름](../spec/tasks/workflow.md) 순서로 읽는다.
개발 에이전트는 [AGENTS.md](../../AGENTS.md)의 재개·작업 규칙도 적용한다.
구현 범위·개발 순서는 [최초 구현 계획](../plans/initial-implementation.md)에서 확인한다.
의존성 정본·관리 입력·실행 경계·구현 모듈은 [개발 의존성과 실행](dependencies.md)을 따른다.
전체 동작은 구조 설명 뒤에 변경 대상의 업무·설정·리소스 사양을 이어서 읽으면 확인할 수 있다.

## 정본 지도

| 위치 | 소유하는 내용 |
|---|---|
| 루트 | [README](../../README.md): 사용자 설치·온보딩. [AGENTS](../../AGENTS.md): 공통 지침. [CLAUDE](../../CLAUDE.md): 공통 지침 가져오기 |
| `docs/assets/` | 사용자 안내와 상세 문서에서 사용하는 이미지 |
| `docs/rules/` | [문서 작성·변경·Git](../rules/documentation.md), [기획](../rules/planning.md), [개발](../rules/development.md), [검토](../rules/review.md) |
| `docs/architecture/` | [구조·책임·권한 경계](../architecture/overview.md) |
| `docs/spec/` | [제품 요구사항](../spec/product.md) |
| `docs/spec/tasks/` | [흐름·실행·근거·보고](../spec/tasks/workflow.md), [계획·승인 범위](../spec/tasks/approval.md), [profile별 기억·검색·보존](../spec/tasks/memory.md) |
| `docs/spec/mcp/` | [도구 목록·공통](../spec/mcp/common.md), [업무·기억·결과](../spec/mcp/tasks.md), [계획·승인](../spec/mcp/plans.md), [리소스](../spec/mcp/resources.md)의 공개 DTO |
| `docs/spec/resources/` | [MCP 재사용·탐색·등록·호출 계약](../spec/resources/contract.md), [리소스 범주와 연결 기준](../spec/resources/initial-adapters.md) |
| `docs/spec/configuration/` | [profile·연결·인증·로컬 저장](../spec/configuration/profiles.md), 같은 정본의 [사용자용 setup YAML](../spec/configuration/profiles.md#사용자용-setup-yaml) |
| `docs/spec/installation/` | [환경·의존성·전달·업데이트](../spec/installation/environment.md) |
| `docs/spec/hosts/` | [플러그인·Skill·로컬 실행](../spec/hosts/plugins.md), [사용자 대화 승인·응답 확인](../spec/hosts/approval.md) |
| `docs/plans/` | [최초 구현 범위·순서·완료 기준](../plans/initial-implementation.md) |
| `docs/development/` | 문서 구조와 읽기 경로, [개발 의존성·실행·모듈 안내](dependencies.md) |

업무·구조 명세는 사람이 이해할 동작·책임을, MCP는 필드·타입·enum을 소유한다.
공개 DTO와 내부 저장 스키마는 구분한다.
상세 사양은 사용자 입력·처리 조건·상태 변화·출력·실패·지원 한계를 설명하고 구현 코드와 관리 명령을 연결한다.
사양에서 실행 책임을 찾을 때는 [모듈 안내](dependencies.md#실행-진입점과-모듈)를, 동작을 확인할 때는 [구현별 대표 검증](../plans/initial-implementation.md#현재-구현-단위와-검증-경계)을 따른다.
읽은 계약을 바꾸면 해당 정본과 영향받는 참조를 함께 갱신한다.

## 작업별 읽기

| 작업 | 순서 |
|---|---|
| 요구·구현 범위 | 기획·개발 → 제품·구조 → 구현 계획·관련 명세 |
| 승인·실행 | 업무 승인·흐름 → 호스트 승인 → MCP 계획·리소스 |
| 기억·검색·보존 | 기억 명세 → profile·업무 보존 → MCP 기억·계획 → 호스트 Skill |
| 로컬 저장·삭제·복구 | profile의 저장 구조 → 업무·기억 보존 → MCP 조회·멱등 규격 → 환경 이관·구현 계획 |
| 리소스·MCP | 리소스 계약·범주별 연결 → 공통 MCP·해당 도구 |
| profile·준비·배포 | 사용자 구성 → 환경 준비 → 호스트 실행 |
| 개발 환경·로컬 실행 | 개발 의존성과 실행 → 최초 구현 계획 → 관련 명세·코드 |
| 문서 변경·검토 | 문서·검토 규칙 → 변경 대상·영향받는 정본 |

## 호스트 로딩과 문맥 복구

Codex는 [AGENTS 지침 체인](https://learn.chatgpt.com/docs/agent-configuration/agents-md)을, Claude Code는 [공통 파일 가져오기](https://code.claude.com/docs/en/memory#share-one-file-with-other-coding-tools)를 사용한다.
상위·전역 설정도 영향을 주며 Markdown 링크는 필요한 본문을 찾는 읽기 경로다.
작업·인계·압축 후에는 AGENTS의 순서로 관련 본문을 실제 읽고 변경 후에도 다시 확인한다.

개인 메모리·대화 요약은 탐색을 돕고 작업 기준은 최신 정본과 실제 승인·실행 기록에서 확인한다.
문맥 복구는 새 작업·압축 후에 관련 본문을 다시 읽는 절차로 수행한다.
새 세션·압축 후의 실제 호스트 검증은 미수행이며 제품 [Skill](../spec/hosts/plugins.md#skill-지침)과 개발 지침은 구분한다.

## 파일을 나누는 기준

다른 독자·독립적인 질문·변경 책임이 생길 때만 분리한다.
같이 읽고 수정하는 짧은 내용은 절로 합치고 상세 계약은 담당 정본으로 연결한다.
README는 사용자 소개·설치·첫 사용을 안내하고 상세 계약은 담당 정본으로 연결한다.
현재 문서는 역할별 규칙·구조·사양·개발 안내·구현 계획으로 구성한다.
선택 근거·미결·검증 범위는 정본에, 변경 이력은 Git에 둔다.

[Diátaxis](https://diataxis.fr/how-to-use-diataxis/)와 [Google 문서 구성](https://developers.google.com/tech-writing/one/documents)의 설명·참조 구분을 적용했다.
역할과 변경 책임을 기준으로 문서를 나누며 표현·이관은 [문서 규칙](../rules/documentation.md)을 따른다.
