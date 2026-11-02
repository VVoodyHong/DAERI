# 개발 의존성과 실행

상태: AWS 공용 서비스의 개발 환경·구현 단계·검증 기준.
요구·입출력·상태·실패 동작은 [제품 명세](../spec/product.md), 구조와 책임은 [아키텍처](../architecture/overview.md)가 소유한다.

## 개발 환경

지원 Python·의존성의 정본은 [pyproject.toml](../../pyproject.toml)과 [uv.lock](../../uv.lock)이다.
저장소 루트에서 잠긴 환경을 준비하고 검증한다.

```sh
uv sync --frozen
uv lock --check
uv run --no-sync python -m pytest
uv build
```

| 의존성 | 역할 |
|---|---|
| boto3 | AgentCore·Bedrock·Aurora Data API·Secrets Manager 클라이언트 |
| httpx | 선택한 OpenAI 호환 API의 요청·응답 처리 |
| pydantic | 설정의 필드·타입·기본값 검증 |
| PyYAML | YAML 읽기와 별도 크기·중복 키·별칭·깊이 검증 |
| pytest | 배포와 독립된 동작·어댑터·문서·패키지 검증 |
| Hatchling·uv | 빌드·의존성 잠금·재현 가능한 개발 환경 |

현재 제공 단위는 Python 라이브러리다.
설정 읽기는 파일·스키마만 검사하고 AWS 클라이언트·자격 조회·연결은 명시적인 `build_cloud()` 호출에서 시작한다.
실제 리소스 변경은 요청된 작업 범위와 배포 환경을 확인해 수행한다.

## 클라우드 개발 상태

`main`은 AWS 공용 서비스의 개발 기준이다.
기존 PC 제품의 코드·문서·설치는 [local-agent 브랜치](https://github.com/VVoodyHong/DAERI/tree/local-agent)에 보존한다.

| 단계 | 현재 상태 | 완료를 확인할 대상 |
|---|---|---|
| 핵심 요청·저장 | 기억 검색 → harness 호출 → 응답·처리 작업 보존 구현 | 모의 어댑터와 실제 SDK 요청 형식 |
| 공용 지식 | 경험 정리·임베딩·Aurora 저장·혼합 검색 구현 | 실제 PG 트랜잭션·검색·접근 격리·규모별 성능 |
| 사용자 접속·승인 | 후속 구현 | HTTP·회사 로그인·플러그인·현재 계획·실제 응답·오래된 응답·호스트 전환 |
| 관리형 연결·배포 | 후속 구현·실제 연동 | Memory·Gateway·Policy·Identity·S3·관측·평가·인프라 |
| 상시 경험 처리 | 처리 서비스 구현, 프로세스 연결은 후속 | 작업 반복 실행·중단·다중 처리기·배포 운영 |
| 동시 소스 작업 | 실행 위치 확정 뒤 구현 | 업무별 사본·브랜치·Git 자격·충돌·검증·통합·복구 |

`AccessContext`는 인증 경계가 전달할 사용자·프로젝트·서비스·출처의 내부 계약이다.
현재 구현은 해당 값과 저장 범위를 검사하고 실제 회사 신원·구성원·권한은 HTTP·로그인 경계에서 연결한다.
업무 전체의 완료 판정·계획 승인 집행과 한 harness 응답의 종료 조건을 구별한다.

### 배포와 확장 시 확인할 조건

| 조건 | 결정·확인 시점 |
|---|---|
| AWS 계정·리전·SSO·프로젝트 구성원 | 실제 배포 정보를 받은 뒤 회사 정책·관리형 지원과 대조 |
| 모델·임베딩·리소스 자격 | 사내 API 또는 Bedrock 접근, 개인 위임·프로젝트 자격 범위 확인 |
| 네트워크·CA·자료·로그 보존 | 회사 연결·자료 분류·저장·평가 입력과 대조 |
| 소스 실행 위치·Git 반영 | PC/AWS 도구·사내망·기존 변경·인증을 대조해 결정 |
| 추가 채널·운영 | 플러그인 사용 검증 뒤 다른 직군의 접점·동시 업무·예산·종료 처리 확인 |
| 기존 자료 이관 | 공용 서비스 준비 뒤 필요한 자료·보존 조건·검증 범위 결정 |

Aurora Serverless·Data API의 실제 SQL·pgvector 설치, Memory의 대화 재개·현재 신원 전달을 실제 환경에서 검증한다.
Data API는 엔진 버전·리전·활성화 조건을 [공식 지원표](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/Concepts.Aurora_Fea_Regions_DB-eng.Feature.Data_API.html)와 대조한다.
관리형 평가·Memory의 모델·처리 리전·비용도 배포 조건으로 확인한다.
특히 [서울의 Evaluations는 글로벌 교차 리전 추론](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/cross-region-inference.html)을 사용하므로 자료 처리 위치를 회사 정책과 대조한다.
[Lambda 코드 평가기](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html)는 지역·외부 API 사용 조건에 맞는 평가 경로의 후보이며 실제 기능별 지원을 확인한다.

Harness의 [VPC·OAuth 연결](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-security.html)과 DAERI의 사내 API 경로·CA를 각각 확인한다.
사용자 위임은 OAuth JWT 경로로 연결하고 현재 SDK 서비스 IAM 호출과 구별한다.
기본 추적은 [CloudWatch Transaction Search](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)의 계정 설정·실행 역할·보존 조건을 확인한다.
승인 대기가 세션 수명을 넘는 경우는 대화 복구와 현재 승인 확인으로 연결하며 파일은 [세션 저장소·공유 파일시스템](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-environment.html)의 수명·격리 조건을 대조한다.

## 실행 진입점과 모듈

패키지는 핵심 계약의 `core`, 흐름의 `application`, 외부 연결의 `infrastructure`, 클라이언트 조립의 `config`로 구성한다.
애플리케이션은 저장·harness·모델·임베딩 포트에 의존하며 외부 SDK 객체는 어댑터 경계에서 다룬다.

| 위치 | 책임 |
|---|---|
| [core/cloud.py](../../src/daeri/core/cloud.py) | 접근 문맥·업무·응답·공용 지식·처리 작업과 교체 포트 |
| [core/errors.py](../../src/daeri/core/errors.py)·[security.py](../../src/daeri/core/security.py)·[serialization.py](../../src/daeri/core/serialization.py) | 경계 오류·비밀 패턴·유효한 float32 벡터 검사 |
| [application/cloud.py](../../src/daeri/application/cloud.py) | 시작·재개 전 기억 검색, 응답 보존, 경험 정리·색인 처리 |
| [infrastructure/agentcore.py](../../src/daeri/infrastructure/agentcore.py) | `InvokeHarness` 이벤트·도구 대기·오류 해석 |
| [infrastructure/cloud_postgres.py](../../src/daeri/infrastructure/cloud_postgres.py) | Aurora Data API·트랜잭션·지식 처리 점유·검색 |
| [infrastructure/cloud_models.py](../../src/daeri/infrastructure/cloud_models.py) | Bedrock Converse·OpenAI 호환 경험 정리, Titan V2 임베딩 |
| [infrastructure/embedding.py](../../src/daeri/infrastructure/embedding.py) | OpenAI 호환 API의 URL·인증·요청·벡터 응답 검사 |
| [config/cloud.py](../../src/daeri/config/cloud.py)·[yaml.py](../../src/daeri/config/yaml.py) | YAML 검증·AWS 클라이언트·선택 제공자 조립과 종료 |

호출자는 `CloudSettings.from_yaml()`로 설정을 읽고 `build_cloud()`가 반환한 서비스 묶음의 수명을 관리한다.
DB 준비 `store.initialize()`, 업무 `workflow.start()/resume()`, 지식 처리 `knowledge.process_next()`를 명시적으로 호출한다.
필드·가상 YAML과 제공자별 조건은 [설정 명세](../spec/product.md#설정과-제공자)를 따른다.

Aurora 저장 어댑터는 응답과 지식 처리 대기를 같은 트랜잭션에 보존한다.
작업 점유는 DB에서 조정하고 Data API 반환 행 크기를 고려해 큰 본문을 8,192자씩 읽는다.
대형 산출물의 S3 분리·인증된 다운로드는 실제 파일 연결 단계에서 구성한다.

## 구현과 대표 검증

검증은 관찰 가능한 결과·실패 경계·실제 SDK 요청 형식을 대상으로 한다.
모의 AWS·HTTP·저장소의 검증과 실제 서비스 연결은 각각 결과를 구분해 기록한다.

| 대상 | 대표 검증 |
|---|---|
| 시작·재개·기억 검색·응답 보존·경험 처리 | [업무·기억 테스트](../../tests/test_cloud_workflow.py) |
| SDK 전달·스트림·도구 대기·오류 | [harness 테스트](../../tests/test_agentcore_harness.py) |
| 트랜잭션·작업 점유·출처 범위·혼합 검색 | [저장 계약 테스트](../../tests/test_cloud_postgres.py) |
| Bedrock·OpenAI 경험 정리·Titan 임베딩 | [모델 경계 테스트](../../tests/test_cloud_models.py) |
| 설정·조립·자격 참조·종료 | [설정 테스트](../../tests/test_cloud_config.py) |
| 문서 링크·계층 경계 | [문서 테스트](../../tests/test_documentation_links.py)·[패키지 테스트](../../tests/test_package_boundaries.py) |

전체 pytest는 회사 인증·실제 AWS·운영 리소스 없이 실행한다.
실제 PG SQL·트랜잭션·검색 품질·확장 설치, 회사 API·SSO·Memory·S3·Gateway·Policy는 실제 연동 대상이다.
배포 완료는 실제 계정·리전·인증·네트워크·PG·다중 사용자 동작의 근거로 판단한다.
