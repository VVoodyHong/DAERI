# 개발 의존성과 실행

상태: AWS 공용 서비스의 개발 환경·구현 단계·검증 기준.
요구·입출력·상태·실패 동작은 [제품 명세](../spec/product.md), 구조와 책임은 [아키텍처](../architecture/overview.md)가 소유한다.

## 개발 환경

지원 Python·의존성의 정본은 [pyproject.toml](../../pyproject.toml)과 [uv.lock](../../uv.lock)이다.
저장소 루트에서 잠긴 환경을 준비하고 검증한다.

```sh
uv sync --frozen
uv lock --check
uv run --no-sync pytest
uv build
```

| 의존성 | 역할 |
|---|---|
| boto3 | SSM Parameter Store·AgentCore·S3 클라이언트 |
| pydantic | 설정의 필드·타입·기본값 검증 |
| PyYAML | YAML 읽기와 크기·중복 키·별칭·깊이 검증 |
| pytest | 배포와 독립된 동작·어댑터·문서·패키지 검증 |
| Hatchling·uv | 빌드·의존성 잠금·재현 가능한 개발 환경 |

현재 제공 단위는 Python 라이브러리다.
설정 읽기는 로컬 파일·스키마만 검사하고 AWS 클라이언트·자격 조회·연결은 명시적인 `build_cloud()` 호출에서 시작한다.
실제 리소스 변경은 요청된 작업 범위와 배포 환경을 확인해 수행한다.

## 클라우드 개발 상태

`main`은 AWS 공용 서비스의 개발 기준이다.
기존 PC 제품의 코드·문서·설치는 [local-agent 브랜치](https://github.com/VVoodyHong/DAERI/tree/local-agent)에 보존한다.

| 단계 | 현재 상태 | 완료를 확인할 대상 |
|---|---|---|
| 중앙 프로젝트 | SSM 조회·허용 목록·프로젝트별 실행 연결 구현 | 인증 문맥 기반 목록·프로젝트 전환·기존 업무 harness 유지 |
| 핵심 요청·저장 | harness 호출·S3 업무 상태·수신 원본 보존·복구 구현 | 동시 진입·조건부 쓰기·응답 보관·중간 장애의 모의 SDK 검증 |
| 프로젝트 기억 | 독립 BYO Memory·고정 공유 namespace 채택; 실제 리소스 연결은 후속 | 자동 추출·통합·검색, 사용자 간 공유·프로젝트 차단·정정·만료 뒤 복구 |
| 사용자 접속·승인 | 후속 구현 | 플러그인·기존 회사 IdP·프로젝트 선택·HTTP·현재 계획·실제 응답·호스트 전환 |
| 관리형 연결·배포 | 후속 구현·실제 연동 | 프로젝트 운영자 등록·Memory·Gateway·Policy·Identity·S3·관측·평가·AWS IaC |
| 동시 소스 작업 | 실행 위치 확정 뒤 구현 | 업무별 사본·브랜치·Git 자격·충돌·검증·통합·복구 |

`AccessContext`는 인증 경계가 전달할 사용자·프로젝트의 내부 계약이다.
회사의 실제 신원·프로젝트 전체 공유가 가능한 구성원인지 확인하는 서버 경계는 HTTP·로그인과 함께 연결한다.
사용자 PC별 MCP 설정 없이 접속하는 플러그인과 업무 전체의 완료·실제 계획 승인 집행은 현재 Python 핵심과 구별한다.

### 배포와 확장 시 확인할 조건

| 조건 | 결정·확인 시점 |
|---|---|
| AWS 계정·리전·회사 IdP·구성원 | 기존 회사 로그인·프로젝트 전체 자료·대화·기억의 공유 범위를 인증 경계에서 확인 |
| 중앙 프로젝트·모델·도구 | SSM 조회 권한과 Harness·Gateway의 운영자 등록·실제 호출 확인 |
| 독립 Memory | 프로젝트별 리소스·정적 공유 namespace·장기 전략·TTL 생략·harness 연결과 사용자별 기록 확인 |
| S3 수명·접근 | 버킷·암호화·버전 관리·삭제 권한·자동 만료 생략·배포 제거 정책과 Memory 독립 보존 확인 |
| 리소스 자격·네트워크 | Gateway·Identity의 프로젝트 자격·공유 PAT·개인 OAuth 동의와 사내망·CA·처리 위치 확인 |
| 소스 실행 위치·Git 반영 | PC/AWS 도구·기존 변경·인증을 대조해 결정 |
| 운영·자료 이관 | S3 prefix·목록 페이지·예산·중단 복구·관측·보존 조건과 이관 대상을 확인 |

실제 Memory·S3·harness의 대화 재개·현재 신원·다운로드 접근을 검증한다.
관리형 평가·Memory의 모델·처리 리전·비용도 배포 조건으로 확인한다.
특히 [서울의 Evaluations는 글로벌 교차 리전 추론](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/cross-region-inference.html)을 사용하므로 자료 처리 위치를 회사 정책과 대조한다.
[Lambda 코드 평가기](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html)는 지역·외부 API 조건에 맞는 평가 경로의 후보이며 기능별 지원을 확인한다.

Harness의 [VPC·OAuth 연결](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html)과 중앙 호스팅한 사내 도구의 네트워크·CA를 확인한다.
사용자 위임은 OAuth JWT 경로로 연결하고 현재 SDK 서비스 IAM 호출과 구별한다.
[CloudWatch Transaction Search](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)의 계정 설정·실행 역할·보존 조건도 확인한다.
승인 대기가 세션 수명을 넘으면 원본을 활용한 새 세션·현재 승인 확인으로 연결하며 [세션 저장소·공유 파일시스템](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-environment.html)의 수명·격리를 대조한다.

## 실행 진입점과 모듈

패키지는 핵심 계약의 `core`, 흐름의 `application`, 외부 연결의 `infrastructure`, 클라이언트 조립의 `config`로 구성한다.
애플리케이션은 프로젝트 조회·업무 저장·harness 포트에 의존하며 SDK 객체·오류는 어댑터 경계에서 다룬다.

| 위치 | 책임 |
|---|---|
| [core/cloud.py](../../src/daeri/core/cloud.py) | 접근 문맥·업무·호출·응답과 저장·harness 포트 |
| [core/projects.py](../../src/daeri/core/projects.py) | 프로젝트 이름·harness의 내부 계약과 조회 포트 |
| [core/errors.py](../../src/daeri/core/errors.py)·[serialization.py](../../src/daeri/core/serialization.py) | 경계 오류·저장 가능한 유한 UTF-8 JSON 검사 |
| [application/cloud.py](../../src/daeri/application/cloud.py) | 허용 목록·시작·재개·저장된 응답 복구 |
| [infrastructure/project_catalog.py](../../src/daeri/infrastructure/project_catalog.py) | SSM 프로젝트 JSON 조회·검증·오류 변환 |
| [infrastructure/agentcore.py](../../src/daeri/infrastructure/agentcore.py) | `InvokeHarness` 요청·스트림·도구 대기·오류 해석 |
| [infrastructure/cloud_s3.py](../../src/daeri/infrastructure/cloud_s3.py) | 업무 manifest·조건부 갱신·불변 호출 원본·응답 복구 |
| [config/cloud.py](../../src/daeri/config/cloud.py)·[yaml.py](../../src/daeri/config/yaml.py) | YAML 검증·AWS 클라이언트 조립·종료 |

호출자는 `CloudSettings.from_yaml()`로 설정을 읽고 `build_cloud()`가 반환한 서비스 묶음의 수명을 관리한다.
`workflow.list_projects(contexts)`, `workflow.start()/resume()/recover()`를 호출하고 `close()` 또는 context manager로 클라이언트를 종료한다.
필드·가상 YAML과 중앙 리소스 설정은 [설정 명세](../spec/product.md#설정과-제공자)를 따른다.

저장소는 업무 생성·조회, 실행 점유, 응답 원본 보관 뒤 상태 확정, 원본 기반 복구를 제공한다.
업무의 harness ARN은 생성 때 저장하며 SSM 변경 뒤에도 같은 실행 대상을 사용한다.
호출 진행·응답 저장 실패의 `StoreError`는 확보한 내부 `task_id`·`turn_id`를 제공해 접속부가 복구할 업무를 연결한다.
계정별 운영 목록·파일 수집·인증된 다운로드와 Memory 재구성은 실제 서버 연결 단계에서 구현한다.

## 구현과 대표 검증

검증은 관찰 가능한 결과·실패 경계·실제 SDK 요청 형식을 대상으로 한다.
모의 AWS·저장소의 검증과 실제 서비스 연결은 각각 결과를 구분한다.

| 대상 | 대표 검증 |
|---|---|
| 프로젝트 JSON·SSM 요청·조회 실패 | [프로젝트 설정 테스트](../../tests/test_project_catalog.py) |
| 허용 목록·프로젝트 전환·harness 유지·시작·재개·복구 | [업무 테스트](../../tests/test_cloud_workflow.py) |
| SDK 전달·스트림·도구 대기·오류·출력 검사 | [harness 테스트](../../tests/test_agentcore_harness.py) |
| S3 조건부 쓰기·충돌·원본 우선 보관·중간 장애·복구 | [저장 계약 테스트](../../tests/test_cloud_s3.py) |
| 설정 경로·필드·서버 조립·종료 | [설정 테스트](../../tests/test_cloud_config.py) |
| YAML·문서 링크·계층 경계 | [YAML 테스트](../../tests/test_yaml.py)·[문서 테스트](../../tests/test_documentation_links.py)·[패키지 테스트](../../tests/test_package_boundaries.py) |

전체 pytest는 회사 인증·실제 AWS·운영 리소스 없이 실행한다.
실제 SSM·S3 조건부 쓰기·접근·목록 페이지와 Memory의 사용자 간 공유·추출 지연·검색 품질·정정·삭제 후 복구는 실제 연동 대상이다.
원본 보존·추가 이벤트 수집·Memory 재구성, 단기 이벤트 만료 후 새 세션의 계획·승인·업무 복구도 실제 AWS에서 확인한다.
플러그인·HTTP·IdP·Gateway·Policy·위임 인증과 전체 배포 완료는 실제 계정·리전·네트워크·다중 사용자 동작의 근거로 판단한다.
