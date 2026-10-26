# 최초 구현 범위와 개발 순서

상태: 읽기·자율 도구 선택·호스트 결과 등록·후보 기억 구현; 실제 회사 PC·호스트 사용 검증 필요

현재 제공 기능·코드·검증 경계를 정의한다.
실행 모듈은 [개발 안내](../development/dependencies.md), 상세 동작은 [정본 지도](../development/documentation-architecture.md#정본-지도)를 따른다.

## 목표와 첫 완료 지점

사용자는 플러그인을 설치하고 profile의 접속 정보·인증·기억 검색 방식을 설정한다.
자연어 요청 → 관련 기억 조회 → 계획·대화 승인 → 총괄·작업자의 자율 수행 → 호스트 보고·파일 등록 → 선별 기억의 자동 저장·색인으로 이어진다.

현재 리소스 실행 목적은 읽기·검증이며 원격 변경·무인 실행은 후속 범위다.
호스트의 자율 판단·보고·파일 작성과 실행기의 승인·출처·저장 책임은 [아키텍처](../architecture/overview.md#오케스트레이션과-자율-수행)를 따른다.
상세 기능·조건은 각 정본에서 관리하고 이 문서는 담당 코드·대표 회귀·실제 확인 경계를 연결한다.

## 현재 구현 단위와 검증 경계

| 영역 | 구현한 동작 | 대표 자동 검증 |
|---|---|---|
| 호스트·준비 | 두 호스트 패키지·네 Skill·작업자 지침, 정상 미준비 판정·격리 환경·준비 후 MCP 등록·실패 복구 | [진입](../../tests/test_plugin_launch.py)·[준비](../../tests/test_bootstrap.py)·[관리](../../tests/test_management_entry.py) |
| 사용자 설정 | 접속·인증 YAML, 복수 연결·개정·설정 이관·읽기 계약 갱신 | [YAML](../../tests/test_onboarding.py)·[인증](../../tests/test_onboarding_credentials.py)·[설정](../../tests/test_setup_fields.py) |
| 삭제·저장 | profile 귀속 자료 cascade·사용 차단·실패 재시도, SQLite·파일·기한·해시 | [삭제](../../tests/test_profile_delete.py)·[저장](../../tests/test_storage.py)·[파일](../../tests/test_artifact_storage.py) |
| 전체 이관 | 단일 profile의 허용 이력·기억·파일 암호화, 검증·확인 후 새 공간 복원·새 실행 권한 확인 | [통합](../../tests/test_profile_portability.py)·[논리 검증](../../tests/test_profile_bundle_validation.py)·[암호화](../../tests/test_portable_archive.py)·[CLI](../../tests/test_profile_portability_cli.py) |
| 업무·승인 | 접수·보완·복구·불변 계획·실제 대화 응답·재승인·멱등 요청 | [업무](../../tests/test_engine.py)·[계획](../../tests/test_planning.py) |
| 자율 읽기 | 진단한 기존 MCP 도구 탐색·선택, 읽기 묶음·실제 기능·스키마·명시 범위 검사 | [등록](../../tests/test_resources.py)·[자율 조회](../../tests/test_resource_autonomy.py)·[경계](../../tests/test_resource_boundary_regressions.py) |
| worker | 작업자 게이트웨이의 위임·배정·공유 예산·기능 선택·실제 근거 반환·철회·복구 | [위임](../../tests/test_workers.py)·[패키징](../../tests/test_worker_packaging.py) |
| 결과 | 호스트의 완료 판단·원출처 연결, 임의 형식 파일 등록·재전달·현재 권한 조회 | [호스트 결과](../../tests/test_host_results.py)·[실행](../../tests/test_execution_flow.py)·[현재 정책](../../tests/test_derived_visibility.py) |
| 페이지 사용 | 호스트가 기존 MCP의 다음 페이지를 조회하고 파일·보고·기억 후보를 제출 | [페이지 stdio](../../tests/test_paged_execution.py) |
| 기억 | 호스트 후보의 자동 검증·정본 저장·색인·재시도·검색·정정·무효화 | [자동 갱신](../../tests/test_automatic_memory.py)·[출처](../../tests/test_knowledge.py)·[검색](../../tests/test_embedding_memory.py)·[규모](../../tests/test_memory_search_scale.py) |
| 회상 평가 | 고정 합성 기억·질의의 유형별 회수율·순위·시간·접근 불가 반환 검사 | [평가 명령](../../evaluations/memory_recall.py)·[자료](../../evaluations/memory_recall.json)·[평가 회귀](../../tests/test_memory_recall_evaluation.py) |
| 임베딩 | 로컬·OpenAI 호환 회사 API·키워드 전용 선택과 전송·응답·모델 호환 검사 | [API](../../tests/test_embedding_provider.py)·[통합](../../tests/test_embedding_integration.py) |
| 공개 MCP | 16개 도구·호스트 호환 입력·구조화 응답·실제 stdio·요청 취소 후 연결 유지 | [스키마](../../tests/test_contracts.py)·[stdio](../../tests/test_mcp_server.py) |
| 개발 검사 | 공통 검증·실패 결과 보존·문서 링크·배포 원본 대조와 GitHub 실행 구성 | [저장소 검사](../../tests/test_repository_checks.py)·[문서 링크](../../tests/test_documentation_links.py) |

[첫 업무](../../tests/test_first_work_defaults.py)는 접속 전용 YAML → 승인 조회 → 호스트 보고·파일 → 선별 기억 → 다음 업무 회상을 확인한다.
[피드백](../../tests/test_feedback_memory.py)은 정정 요청·새 승인·기존 지식 무효화·수정 경험의 회상을 확인한다.
자료 제공·출처·내보내기·보존 조건은 [profile](../spec/configuration/profiles.md#로컬-저장-구조)과 [기억](../spec/tasks/memory.md)이 소유한다.

### 확보한 검증 근거

위 대표 회귀는 승인·현재 권한·동시성·저장 실패·복구와 모의 MCP의 실제 stdio 전달을 확인한다.
고정 하위 패키지의 설치·import·메타데이터, 모의 HTTP·제공자 SDK, 준비한 실제 로컬 모델의 DNS 차단 검색도 각각의 검사에서 확인한다.
자동 검사 결과와 조건부 생략 사유는 [공통 검사 보고서](../development/dependencies.md#공통-자동-검사)에 기록한다.

형식별 시험 바이트 검사는 등록 전후 동일성과 메타데이터를 확인한다.
XLSX·PDF 등의 실제 문서 구조·내용·호스트 생성 품질은 해당 작성 도구로 확인한다.
실제 호스트의 설치·자연어 승인 해석·도구 선택·파일 작성·worker 시작·UI는 별도 검증 대상이다.
회사 리소스의 실제 계정 권한·네트워크·SSO·회사 임베딩 API와 캐시 없는 PC의 전체 준비도 해당 환경의 근거가 필요하다.
Windows 스크립트·구성·가짜 API 회귀는 있으나 실제 Windows 실행·정책·인증 UI 검증은 별도다.
로컬 모델 검사는 준비한 모델 경로가 있는 경우만 실행한다.

## 개발 순서

추가 개선 다섯 항목의 구현 상태와 남은 실제 환경 확인을 정리한다.
항목별 사양·입출력은 담당 정본, 변경 이력은 Git, 실행 결과는 공통 검사 보고서에서 관리한다.

| 순서 | 추가 개선 | 상태 | 상세 동작을 관리할 정본 |
|---|---|---|---|
| 1 | 최초 설치의 MCP 기동·준비 흐름 | 구현·로컬 회귀 완료; 실제 호스트 연결 확인 예정 | [호스트](../spec/hosts/plugins.md#설치부터-업무-시작까지의-실제-경계)·[환경 준비](../spec/installation/environment.md) |
| 2 | 장기기억의 검색 범위 | 구현·로컬 회귀 완료; 운영 부하·의미 품질 별도 확인 | [기억 검색](../spec/tasks/memory.md#혼합-검색과-색인) |
| 3 | 기억 회상 품질 평가 | 평가 명령·키워드 기준선·로컬 회귀 완료; 실제 임베딩 측정 필요 | [회상 평가](../spec/tasks/memory.md#반복-가능한-회상-평가) |
| 4 | 자동 CI 검사 | 구현·macOS 두 Python의 로컬 검증 완료; GitHub 실행 확인 예정 | [공통 검사](../development/dependencies.md#공통-자동-검사) |
| 5 | profile 전체 데이터 이관 | 구현·로컬 회귀 완료; 실제 PC 간 이동 확인 예정 | [이관·백업·복원](../spec/configuration/profiles.md#이관백업복원) |

아래는 각 개선의 핵심 동작과 확인한 범위다.

### 1. 최초 설치의 MCP 기동·준비 흐름

최초 설치는 Skill로 진입하고 사용자의 세팅 시작 응답 뒤 전용 환경을 준비해 MCP 연결 구성을 발행하는 방법을 채택했다.
호스트는 새 구성을 읽어 준비된 실행기에 연결하며, 구현·실패 복구는 [환경 준비 계약](../spec/installation/environment.md#구현한-자동-준비의-입출력과-경계)을 따른다.

Codex CLI의 격리 설치에서 구성 생성 전 서버 미노출과 생성 후 새 프로세스의 시험 MCP 초기화를 확인했다.
로컬 회귀는 준비 후 등록·등록 실패 복구·읽기 전용 검사·실제 stdio 도구 열거·배포물 구성을 확인한다.
실행 중 대화의 재연결과 setup·중단·재시도·업데이트의 호스트·OS별 사용 경험은 추가 확인 대상이다.

### 2. 장기기억의 검색 범위

기존 공개 검색 입력·결과와 SQLite를 유지하며, 현재 정본·profile·정책 조건으로 색인 후보를 제한하고 순위에서 허용 후보를 채우도록 변경했다.
기억 참조의 10,000건 선취 대신 배치 조회와 개별 본문 검사로 처리하며 상세 절차·결과 상한은 [기억 검색](../spec/tasks/memory.md#혼합-검색과-색인)을 따른다.

10,000건 이후의 관련 기억, 다른 리소스의 대량 기억, 상위 차단 후보 뒤의 허용 기억을 찾는 [규모 회귀](../../tests/test_memory_search_scale.py)를 통과했다.
SQL 인수 상한 32개에서도 검색하고 회사 API의 전송 직전 대체 허용 기억·누락 색인의 권한별 상태를 확인했다.
기존 profile·실제 대상·정정·무효화·기한·현재 권한·임베딩 전송 검사와 전체 로컬 회귀도 통과했다.
전체 본문의 선적재·대량 SQL 인수를 제거한 범위와 DB 내부의 정확 거리 계산·순위 정렬 비용을 구분한다.
실제 부하의 검색 시간·처리량과 모델의 의미 회상 품질은 별도 측정 대상으로 남긴다.

### 3. 기억 회상 품질 평가

[평가 실행](../development/dependencies.md#기억-회상-평가)과 고정 자료·기대 근거 대조·접근 불가 반환 회귀를 구현했다.
평가 계약·집계·실패 상태는 [기억 사양](../spec/tasks/memory.md#반복-가능한-회상-평가)을 따른다.
macOS arm64·Python 3.14.6·SQLite 3.53.1에서 자료 버전 1의 기억 11개·질의 14개를 실제 키워드 검색으로 측정했다.
자료 SHA256은 `ab44b286df33c1e9469173fa3aa80e2300b9319a95d046dc0686a49cc775a30a`, 검색 상한은 5다.

| 유형 | 기대 근거가 있는 질의 | Recall@5 | MRR@5 |
|---|---|---|---|
| 식별자·오류 코드 | 3 | 1.00 | 1.00 |
| 한국어 의역 | 4 | 0.00 | 0.00 |
| 복수 근거 | 1 | 1.00 | 1.00 |
| 정정된 기준 | 2 | 1.00 | 1.00 |
| 전체 | 10 | 0.60 | 0.60 |

기대 근거 없는 무관 질의 1개·격리 질의 3개는 모두 빈 결과였고 전체 질의의 접근 불가 반환은 0건이었다.
검색 호출의 평균은 0.264ms, 95백분위는 0.514ms로 이 작은 자료·준비된 FTS 조건의 단회 관찰값이다.
수치 모의 벡터·모의 HTTP 회귀는 평가 실행·점수·격리와 실패 시 의미 점수 생략을 확인했다.
현재 개발 PC에 준비된 실제 모델 캐시가 없어 이 자료의 로컬 모델 의미 점수는 미측정이다.
회사 API의 실제 품질·지연과 Claude·Codex의 근거 활용도 해당 환경에서 별도 측정한다.

### 4. 자동 CI 검사

[공통 검사](../development/dependencies.md#공통-자동-검사)에서 잠금·버전·공개 계약·회귀·문서·키워드 회상·배포물을 실행하고 결과를 보존한다.
GitHub는 push·PR의 Ubuntu Python 3.12·3.14 검사와 수동 선택형 native·실제 모델·빈 실행 환경의 준비를 분리했다.
검사 본문은 같은 명령을 실행할 수 있는 다른 CI에서도 재사용할 수 있다.

버전·카탈로그 차이, 배포 파일 누락·생성 파일 혼입·이전 원본, 실패 로그 보존과 문서 경계를 자동 회귀로 확인한다.
macOS arm64의 Python 3.12.13 기본 의존성 환경과 3.14.6 환경에서 공통 명령의 여섯 검사가 모두 통과했다.
전체 회귀 결과·생략 사유는 해당 실행의 보고서를 기준으로 확인한다.
workflow의 YAML·Bash 구문·GitHub context·읽기 권한·고정 액션과 수동 실행 조건도 대조했다.
실제 GitHub push·PR 실행 결과는 추가 확인 대상이다.
Windows native·실제 모델·새 실행 환경 준비의 수동 job은 해당 runner에서 실행한 결과로 판정한다.

### 5. profile 전체 데이터 이관

설정 전용 import/export를 유지하고 `profile backup/restore`로 단일 profile의 허용 이력·기억·파일을 암호화 이동한다.
형식·내보내기 허용·충돌·확인·새 권한·기한과 오프라인 스냅샷의 보장 시점은 [이관 계약](../spec/configuration/profiles.md#이관백업복원)을 따른다.

모의 MCP의 실제 업무·보고·파일·자동 기억을 다른 임시 저장소에 복원하고 원 식별자·출처·파일 바이트·기억 회상을 대조했다.
공유 저장소의 다른 profile 격리, 과거 승인·작업자 토큰·요청 재전달의 실행 차단과 인증 참조 재발급을 확인했다.
잘못된 암호·손상·경로·본문 그래프·충돌·삭제 표식·저장 실패의 거부·복구와 기한·무효화·출처 지문 보존을 회귀로 확인했다.
API profile의 복원은 외부 전송 없이 키워드 색인을 만들고 벡터 재색인은 별도 관리 명령으로 진행한다.
macOS의 로컬 검증이며 실제 회사 PC 간 이동·native Windows·호스트의 암호 입력 UI와 재연결은 해당 환경에서 추가 확인한다.

## 완료 조건과 검증 범위

로컬 구현 완료는 코드·공개 계약·문서가 같은 동작을 설명하고 허용·실패·복구 회귀가 통과한 상태다.
실제 환경의 완료 판단은 해당 호스트·OS·회사 연결에서 확보한 별도 증거를 따른다.
실행하지 않은 검사·회사 연결·호스트 동작은 그 범위로 명시한다.
작업 품질과 동기화 기준은 [개발](../rules/development.md)·[문서](../rules/documentation.md) 규칙을 따른다.
