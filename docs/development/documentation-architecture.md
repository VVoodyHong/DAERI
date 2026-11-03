# 문서 구조와 읽기 경로

상태: AWS 공용 서비스 문서의 정본 지도.
동작·입력·상태는 제품 명세 한 곳에 통합하고 소개·구조·개발·작업 규칙을 독자와 변경 책임에 따라 구분한다.

## 정본 지도

| 정본 | 소유하는 내용 |
|---|---|
| [README](../../README.md) | 제품 소개·활용 예시·구성도·시작 경로 |
| [제품 명세](../spec/product.md) | 확정 요구·업무·승인·프로젝트 공유·기억·설정 입력·실패·복구 동작 |
| [아키텍처](../architecture/overview.md) | AWS·DAERI 책임·구성도·Memory·S3 수명·요청 연결·설계 근거 |
| [개발 안내](dependencies.md) | 환경·의존성·패키지·구현 단계·대표 검증·배포 조건 |
| [문서 규칙](../rules/documentation.md) | 문서 작성·변경·통합·Git 기록 |
| [기획 규칙](../rules/planning.md) | 요구 해석·선택·확정·미결 |
| [개발 규칙](../rules/development.md) | 클린 아키텍처·최소 코드·의존 방향·입력 경계 |
| [검토 규칙](../rules/review.md) | 프로젝트 전체의 문서·코드 정합성·간소화·검증 |
| [AGENTS](../../AGENTS.md)·[CLAUDE](../../CLAUDE.md) | Codex·Claude Code의 공유 개발 진입점 |
| `docs/assets/` | README의 동작 구성도와 상세 AWS 구성도 |
| [플러그인 Skill](../../plugins/daeri/skills/daeri/SKILL.md) | 공개 도구의 호스트 호출·사용자 확인·결과 표시 절차; 동작 계약은 제품 명세 |

## 작업과 재개

제품 소개 → 제품 명세 → 관련 구조·개발 안내 순서로 읽는다.
개발 에이전트는 [AGENTS](../../AGENTS.md)를 먼저 읽고 작업별 규칙과 변경 대상 정본을 확인한다.
최신 사용자 지시·현재 파일·실제 승인·실행 기록에서 작업 기준을 복구하며 링크는 필요한 본문을 찾는 경로로 사용한다.

같은 계약은 담당 정본에서 개정하고 관련 문서는 링크를 갱신한다.
독자·독립적인 질문·변경 책임이 생길 때 문서를 나누며 구체 기준은 [문서 규칙](../rules/documentation.md)을 따른다.
기존 로컬 제품·MCP·setup·SQLite 명세는 [local-agent 브랜치](https://github.com/VVoodyHong/DAERI/tree/local-agent)의 문서로 확인한다.
