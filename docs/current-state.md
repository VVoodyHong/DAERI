# 현재 상태와 다음 작업

상태: 설계 및 문서화 단계

## 현재 목표와 범위

AGENTS에는 기본 지침만 두고 상세 규칙과 제품 설계를 주제별 정본으로 정리한다.
사람과 에이전트가 코드를 읽지 않아도 동작과 맥락을 이해할 수 있게 한다.

현재 허용된 작업은 Markdown 작성·정리와 설계 조사다.
프로그램 설치, Git 초기화, 제품 코드 작성, 실제 리소스 연결은 진행하지 않는다.
자동 설치는 [향후 제품 기능](spec/installation/environment.md)이다.

## 정본과 확정 결정

| 내용 | 참조 |
|---|---|
| 제품 조건 전체 | [제품 명세](spec/product.md) |
| 기존 호스트 에이전트 + 로컬 MCP 실행기 | [ADR-0001](architecture/decisions/0001-host-agent-local-runtime.md) |
| 승인 범위 안의 자율 실행과 범위 이탈 전 수정 승인 | [계획·승인](spec/tasks/approval.md) |
| 최초 PostgreSQL·Redis·AWS·GitLab 연동 | [초기 어댑터](spec/resources/initial-adapters.md) |
| Windows·macOS 지원 설계, 추후 Mac부터 구현·검증 | [환경 준비](spec/installation/environment.md) |
| AGENTS 기본 지침, 상세 rules, CLAUDE 가져오기만 | [ADR-0002](architecture/decisions/0002-shared-agent-instructions.md) |

## 이번에 정리한 내용

- AGENTS를 기본 지침과 작업별 읽기 경로 중심으로 줄였다.
- `docs/rules`에 문서 작성·Markdown 표현·개발 규칙을 분리했다.
- 업무 명세를 흐름·상태, 계획·승인, 결과·이력, 수행 사례로 분리했다.
- 리소스는 공통 계약과 최초 어댑터로 나눴다.
  설치는 환경 준비와 배포·업데이트로 나눴다.
- 이전 정본을 삭제하고 관련 문서의 참조를 새 경로로 갱신했다.
- README 작성은 보류하고 날짜 메타데이터는 사용하지 않는다.
- 문서 구조의 분리 기준과 공식 참고 자료를 [문서 구조](development/documentation-architecture.md)에 기록했다.

실제 제품, 설치 프로그램, 어댑터, 실행 가능한 구현 계획은 아직 없다.

## 미결 사항

| 항목 | 다음 판단의 정본 |
|---|---|
| 언어·버전·잠금 방식 | [환경 준비](spec/installation/environment.md) |
| 사전 탐색·승인 수집·계획 범위 표현 | [계획·승인](spec/tasks/approval.md) |
| 초기 기능·제품별 인증·실제 인스턴스 | [초기 어댑터](spec/resources/initial-adapters.md) |
| 부처별 접근·공통 실행 계약 | [리소스 계약](spec/resources/contract.md) |
| OS·호스트 패키징과 최초 전달 경로 | [배포·업데이트](spec/installation/distribution.md) |
| 무인 정기 실행·서비스 UI | [제품 명세](spec/product.md) |

Python 3.12 계열·uv·공식 MCP SDK·SQLite는 제안이다.
제한된 읽기 기능과 AWS EC2 시작도 제안이며 실제 환경 검증을 마친 선택은 아니다.
접속 정보와 비밀값은 문서에 넣지 않고 사용자 로컬 설정에서 처리한다.

## 검증 상태

전체 Markdown의 내부 링크와 제목 앵커가 유효한 것을 확인했다.
단일 H1, 제목 간격, 코드 블록 구분도 확인했다.

CLAUDE는 정확히 `@AGENTS.md` 한 줄이다.
이전 정본과 경로 참조, README 파일, 날짜 메타데이터가 남지 않았다.

분리 문서의 원문 대조와 독립 검토에서 주요 제품 계약의 누락은 발견되지 않았다.
검토에서 확인한 ADR 변경 규칙과 사전 탐색의 정본 참조를 보정했다.
검증 범위는 Markdown 원문과 파일 참조다.

새 호스트 세션의 지침 로딩, Markdown 렌더링 미리보기는 아직 검증하지 않았다.
제품 코드 실행과 외부 리소스 연결도 수행하지 않았다.

## 다음 작업

이번 작업의 정본은 [문서 구조](development/documentation-architecture.md)에서 찾는다.
문서 작성·수정은 [내용 규칙](rules/documentation.md)과 [표현 규칙](rules/markdown.md)을 따른다.

피드백을 정본에 반영하고 승인 수집·계획 범위·초기 어댑터 기능을 구체화한다.
구현 지시가 오면 현재 상태와 관련 명세를 갱신하고 실제 구현 계획을 작성한다.
