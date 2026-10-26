# 최초 구현 범위와 개발 순서

상태: 읽기·자율 도구 선택·호스트 결과 등록·후보 기억 구현; 실제 회사 PC·호스트 사용 검증 필요

현재 제공 기능·코드·검증 경계를 정의한다.
실행 진입점·패키지 책임·의존 관계는 [개발 안내](../development/dependencies.md#실행-진입점과-모듈), 상세 동작은 [정본 지도](../development/documentation-architecture.md#정본-지도)를 따른다.

## 목표와 첫 완료 지점

사용자는 플러그인을 설치하고 profile의 접속 정보·인증·기억 검색 방식을 설정한다.
자연어 요청 → 관련 기억 조회 → 계획·대화 승인 → 총괄 에이전트·하위 에이전트의 자율 수행 → 호스트 보고·파일 등록 → 선별 기억의 자동 저장·색인으로 이어진다.

현재 리소스 실행 목적은 읽기·검증이며 원격 변경·무인 실행은 후속 범위다.
호스트의 자율 판단·보고·파일 작성과 runtime의 승인·출처·저장 책임은 [아키텍처](../architecture/overview.md#오케스트레이션과-자율-수행)를 따른다.
상세 기능·조건은 각 정본에서 관리하고 이 문서는 담당 코드·대표 회귀·실제 확인 경계를 연결한다.

## 현재 구현 단위와 검증 경계

| 영역 | 구현한 동작 | 대표 자동 검증 |
|---|---|---|
| 호스트·준비 | 두 호스트 패키지·네 Skill·하위 에이전트 지침, 정상 미준비 판정·격리 환경·준비 후 MCP 등록·실패 복구 | [진입](../../tests/test_plugin_launch.py)·[준비](../../tests/test_bootstrap.py)·[관리](../../tests/test_management_entry.py) |
| 사용자 설정 | 접속·인증 YAML, 복수 연결·개정·설정 이관·읽기 계약 갱신 | [YAML](../../tests/test_onboarding.py)·[인증](../../tests/test_onboarding_credentials.py)·[설정](../../tests/test_setup_fields.py) |
| 삭제·저장 | profile 귀속 자료 cascade·사용 차단·실패 재시도, SQLite·파일·기한·해시 | [삭제](../../tests/test_profile_delete.py)·[저장](../../tests/test_storage.py)·[파일](../../tests/test_artifact_storage.py) |
| 전체 이관 | 단일 profile의 허용 이력·기억·파일 암호화, 검증·확인 후 새 공간 복원·새 실행 권한 확인 | [통합](../../tests/test_profile_portability.py)·[논리 검증](../../tests/test_profile_bundle_validation.py)·[암호화](../../tests/test_portable_archive.py)·[CLI](../../tests/test_profile_portability_cli.py) |
| 업무·승인 | 접수·보완·복구·불변 계획·실제 대화 응답·재승인·자동 기록 | [업무](../../tests/test_runtime.py)·[계획](../../tests/test_planning.py) |
| 자율 읽기 | 진단한 기존 MCP 도구 탐색·선택, 읽기 묶음·실제 기능·스키마·명시 범위 검사 | [등록](../../tests/test_resources.py)·[자율 조회](../../tests/test_resource_autonomy.py)·[경계](../../tests/test_resource_boundary_regressions.py) |
| 하위 에이전트 | 하위 에이전트 게이트웨이의 위임·배정·병렬 기능 선택·실제 근거 반환·철회·복구 | [위임](../../tests/test_subagents.py)·[패키징](../../tests/test_subagent_packaging.py) |
| 결과 | 호스트의 완료 판단·원출처 연결, 임의 형식 파일 등록·재전달·현재 권한 조회 | [호스트 결과](../../tests/test_host_results.py)·[실행](../../tests/test_execution_flow.py)·[현재 정책](../../tests/test_derived_visibility.py) |
| 페이지 사용 | 호스트가 기존 MCP의 다음 페이지를 조회하고 파일·보고·기억 후보를 제출 | [페이지 stdio](../../tests/test_paged_execution.py) |
| 기억 | 호스트 후보의 자동 검증·정본 저장·색인·재시도·검색·정정·무효화 | [자동 갱신](../../tests/test_automatic_memory.py)·[출처](../../tests/test_knowledge.py)·[검색](../../tests/test_embedding_memory.py)·[규모](../../tests/test_memory_search_scale.py) |
| 회상 평가 | 고정 합성 기억·질의의 유형별 회수율·순위·시간·접근 불가 반환 검사 | [평가 명령](../../evaluations/memory_recall.py)·[자료](../../evaluations/memory_recall.json)·[평가 회귀](../../tests/test_memory_recall_evaluation.py) |
| 임베딩 | 로컬·OpenAI 호환 회사 API·키워드 전용 선택과 전송·응답·모델 호환 검사 | [API](../../tests/test_embedding_provider.py)·[통합](../../tests/test_embedding_integration.py) |
| 공개 MCP | 16개 도구·호스트 호환 입력·구조화 응답·실제 stdio·요청 취소 후 연결 유지 | [스키마](../../tests/test_contracts.py)·[stdio](../../tests/test_mcp_server.py) |
| 개발 검사 | 코어 의존 경계·패키지 자원 경로·공통 검증·실패 결과 보존·문서 링크·배포 원본 대조와 GitHub 실행 구성 | [패키지 경계](../../tests/test_package_boundaries.py)·[저장소 검사](../../tests/test_repository_checks.py)·[문서 링크](../../tests/test_documentation_links.py) |

[첫 업무](../../tests/test_first_work_defaults.py)는 접속 전용 YAML → 승인 조회 → 호스트 보고·파일 → 선별 기억 → 다음 업무 회상을 확인한다.
[피드백](../../tests/test_feedback_memory.py)은 정정 요청·새 승인·기존 지식 무효화·수정 경험의 회상을 확인한다.
자료 제공·출처·내보내기·보존 조건은 [profile](../spec/configuration/profiles.md#로컬-저장-구조)과 [기억](../spec/tasks/memory.md)이 소유한다.

### 확보한 검증 근거

위 대표 회귀는 승인·현재 권한·동시성·저장 실패·복구와 모의 MCP의 실제 stdio 전달을 확인한다.
고정 하위 패키지의 설치·import·메타데이터, 모의 HTTP·제공자 SDK, 준비한 실제 로컬 모델의 DNS 차단 검색도 각각의 검사에서 확인한다.
자동 검사 결과와 조건부 생략 사유는 [공통 검사 보고서](../development/dependencies.md#공통-자동-검사)에 기록한다.

형식별 시험 바이트 검사는 등록 전후 동일성과 메타데이터를 확인한다.
XLSX·PDF 등의 실제 문서 구조·내용·호스트 생성 품질은 해당 작성 도구로 확인한다.
실제 호스트의 설치·자연어 승인 해석·도구 선택·파일 작성·하위 에이전트 시작·UI는 별도 검증 대상이다.
회사 리소스의 실제 계정 권한·네트워크·SSO·회사 임베딩 API와 캐시 없는 PC의 전체 준비도 해당 환경의 근거가 필요하다.
Windows 스크립트·구성·가짜 API 회귀는 있으나 실제 Windows 실행·정책·인증 UI 검증은 별도다.
로컬 모델 검사는 준비한 모델 경로가 있는 경우만 실행한다.

## 개발 순서

핵심 흐름을 먼저 완성하고 실제 사용에서 필요한 확장을 추가한다.
단순화 작업은 아래 순서로 구현·사양·검증을 함께 대조한다.

| 순서 | 완료 조건 | 구현·검증 |
|---|---|---|
| 1. 업무 입력 | 모델은 업무·목적·대상·결과를 전달하고 식별·개정·기본 정책은 runtime이 관리 | `core/contracts.py`·`application/planning.py`·`application/runtime.py`, [단순 계획](../../tests/test_simple_plan.py)·[기본 수행](../../tests/test_simple_workflow.py) |
| 2. 승인·자율 실행 | 계획 제출로 승인 대기, 실제 답변 뒤 자율 조회·병렬 하위 에이전트 수행 | [승인](../spec/tasks/approval.md), [하위 에이전트 검증](../../tests/test_subagents.py) |
| 3. 실패·재개 | 읽기 중단 뒤 새 관찰, 무관한 설정·인증 갱신 뒤 수행 유지 | [복구](../../tests/test_execution_persistence.py)·[연결](../../tests/test_resources.py) |
| 4. 결과·학습 | 보고·파일·선별 기억의 자동 저장·색인과 다음 업무 회상 | [첫 업무 통합](../../tests/test_first_work_defaults.py)·[자동 기억](../../tests/test_automatic_memory.py) |
| 5. 문서·코드 대조 | 현재 API·호스트 지침·정본·검증이 같은 동작을 설명 | [공통 검사](../development/dependencies.md#공통-자동-검사) |

현재 자동 검증은 로컬 모의 MCP·모의 임베딩 API를 사용한다.
실제 회사 접속·회사 임베딩 품질과 호스트의 대화·하위 에이전트 수행은 위 검증 경계에서 확인한다.
원격 변경·무인 실행·기업별 세부 통제는 구체적인 요구와 실제 연결의 효과·복구 검증을 갖춰 확장한다.
