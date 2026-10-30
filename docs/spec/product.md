# DAERI 제품 명세

상태: AWS 공용 서비스의 확정 요구사항.
기존 로컬 기능을 기반으로 클라우드를 개발하며 현재 구현·검증 범위는 [개발 안내](../development/dependencies.md#클라우드-개발-상태)가 소유한다.

## 목적

DAERI는 여러 직군의 구성원이 자연어로 업무를 맡기고 서비스 경험을 함께 축적하는 멀티 오케스트레이션 에이전트다.
같은 프로젝트의 신규 구성원도 현재 권한으로 기존 지식·성공 절차·실패 원인·피드백을 첫 업무부터 활용한다.
숙련도 향상은 누적 지식을 회상·적용·검증하는 능력으로 평가한다.

이름에는 숙련된 대리급 직원의 역할과 업무를 대리 수행한다는 의미가 있다.
AWS 관리형 기능이 실행·문맥·연결·보안·관측·평가를 맡고 DAERI는 프로젝트 업무와 공용 지식의 연결에 집중한다.
책임과 연결은 [아키텍처](../architecture/overview.md)에서 정의한다.

## 확정 요구사항

| ID | 요구사항 | 담당 명세 |
|---|---|---|
| P-01 | 자연어 요청·과정·결과·피드백을 업무로 관리한다. | [업무 흐름](tasks/workflow.md) |
| P-02 | 실제 사용자에게 계획을 승인받고 그 범위에서 도구·방법·순서·보고 형식을 자율 선택한다. | [계획·승인](tasks/approval.md) |
| P-03 | AgentCore managed harness가 모델·도구 호출 루프와 문맥을 관리한다. | [구조](../architecture/overview.md) |
| P-04 | 총괄 에이전트가 필요한 하위 작업을 배정하고 근거를 통합한다. | [업무 흐름](tasks/workflow.md) |
| P-05 | 프로젝트·서비스별 지식을 공용 PostgreSQL·pgvector에 축적한다. | [공유 기억](tasks/memory.md#클라우드-공유-기억) |
| P-06 | 요청 시 관련 기억을 제공하고 결과·근거·피드백을 보존해 지식 정리·색인을 자동 처리한다. | [공유 기억](tasks/memory.md#클라우드-공유-기억) |
| P-07 | 기억은 공유하고 각 업무의 목표·승인·실행·소스 작업 사본은 분리한다. | [업무 흐름](tasks/workflow.md#클라우드-업무-흐름) |
| P-08 | Claude Code·Codex 접속부에서 같은 공용 서비스와 업무를 사용한다. | [접속부](hosts/plugins.md#클라우드-접속부) |
| P-09 | 모델과 임베딩은 각각 사내 API 또는 Amazon Bedrock으로 구성한다. | [구성](configuration/profiles.md#클라우드-구성) |
| P-10 | Gateway·Policy·Identity·Memory·Observability·Evaluations 등 AWS 관리형 기능을 우선 사용한다. | [구조](../architecture/overview.md) |
| P-11 | 현재 사용자·프로젝트·출처 권한과 실제 승인을 실행·기억·파일에 적용한다. | [승인](tasks/approval.md#클라우드-업무-승인) |
| P-12 | 연결된 MCP·API를 재사용하고 필요한 프로젝트·업무·지식 연결만 구현한다. | [리소스 계약](resources/contract.md) |

사용자의 목표·권한·완료 조건을 기준으로 업무를 구성한다.
현재 수치·상태는 최신 자료로 확인하고 기억의 적용 조건·출처·관찰 시점을 함께 사용한다.

## 사용 예정 환경

| 역할 | 환경 |
|---|---|
| 사용자 접점 | Claude Code·Codex 플러그인; 다른 직군의 추가 채널은 사용 검증 후 결정 |
| 수행·문맥 | AgentCore managed harness·Runtime·Memory |
| 도구·접근 | Gateway·Policy·Identity와 연결된 리소스 MCP·API |
| 공유 지식·업무 | PostgreSQL·pgvector; Aurora Serverless·Data API 우선 검증안 |
| 파일 | S3 보관안 |
| 생성 모델·임베딩 | 독립 선택한 사내 API 또는 Amazon Bedrock |
| 코드 작업 | 업무별 사본·브랜치; 실행 위치·Git 자격은 환경 연결 단계에서 결정 |

프로젝트는 공유·접근 경계, 서비스는 업무 대상, profile은 서비스·환경별 연결 묶음이다.
구체 계정·리전·SSO·보존·네트워크는 실제 배포 정보를 받은 뒤 설정한다.

## 현재 제공 범위

현재 클라우드 서비스는 개발 단계이며 설치 가능한 기존 로컬 제품은 `local-agent` 브랜치에 보존한다.
기존 구현은 PC의 profile·계획·실제 대화 승인·읽기 실행·보고·선별 기억과 Codex·Claude Code 간 업무 재개를 제공한다.
기존 공개 MCP·setup YAML·SQLite 저장 규격은 로컬 구현 계약으로 유지한다.

클라우드 첫 개발 단위는 요청의 관련 기억 제공 → harness 호출 → 응답·지식 갱신 작업 보존 → 경험 정리·임베딩 → 공용 PG 회상이다.
실제 배포·회사 인증·클라우드 플러그인 연결·인프라는 이 핵심 경로 이후 구현·검증한다.
각 단계의 구현 상태와 완료 근거는 [개발 상태](../development/dependencies.md#클라우드-개발-상태)를 따른다.
