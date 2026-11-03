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
| boto3 | SSM·AgentCore 실행과 설정 조회·S3 클라이언트 |
| mcp | HTTP MCP·OAuth resource metadata·사용자 form 확인 |
| PyJWT·cryptography | RS256 토큰·표준 JWKS 캐시 검증 |
| httpx·uvicorn | OIDC discovery HTTP·ASGI 서버 실행 |
| pydantic | 설정의 필드·타입·기본값 검증 |
| PyYAML | YAML 읽기와 크기·중복 키·별칭·깊이 검증 |
| pytest | 배포와 독립된 동작·어댑터·문서·패키지 검증 |
| Hatchling·uv | 빌드·의존성 잠금·재현 가능한 개발 환경 |

현재 제공 단위는 Python 업무 서비스·원격 MCP 서버와 호스트별 플러그인 배포 파일이다.
설정 읽기는 로컬 파일·스키마만 검사한다.
클라이언트·자격 조회·연결은 `build_cloud()`·`build_server()` 또는 명시적으로 실행한 배포 명령에서 시작한다.
실제 리소스 변경은 요청된 작업 범위와 배포 환경을 확인해 수행한다.

## 클라우드 개발 상태

`main`은 AWS 공용 서비스의 개발 기준이다.
기존 PC 제품의 코드·문서·설치는 [local-agent 브랜치](https://github.com/VVoodyHong/DAERI/tree/local-agent)에 보존한다.

| 단계 | 현재 상태 | 완료를 확인할 대상 |
|---|---|---|
| 중앙 프로젝트 | SSM 조회·허용 목록·프로젝트별 실행 연결 구현 | 인증 문맥 기반 목록·프로젝트 전환·기존 업무 harness 유지 |
| 핵심 요청·저장 | harness 호출·S3 업무 상태·수신 원본 보존·복구 구현 | 동시 진입·조건부 쓰기·응답 보관·중간 장애의 모의 SDK 검증 |
| 프로젝트 기억 | 독립 Memory 생성·공유 전략 연결·배포 검증 구현 | 실제 사용자 간 검색·추출 지연·정정·프로젝트 차단·만료 뒤 복구 |
| 사용자 접속·승인 | 토큰 검증·HTTP MCP·업무 목록·확인창 결정·hook 구현 | 실제 회사 IdP·호스트 설치·Lambda 권한과 실행 연결 |
| 플러그인 배포 | 두 호스트 manifest·공유 Skill·설정된 ZIP 생성기 구현 | 각 호스트의 설치·OAuth callback·사용자 확인창 |
| 관리형 연결·배포 | Runtime 컨테이너·MCP 설정과 Memory·릴리스·SSM 배포 도구 구현 | 실제 계정 배포·Gateway·Identity·S3·관측·평가·Lambda 연결 |
| 동시 소스 작업 | 실행 위치 확정 뒤 구현 | 업무별 사본·브랜치·Git 자격·충돌·검증·통합·복구 |

`AccessContext`는 인증 경계가 전달할 사용자·프로젝트의 내부 계약이다.
공개 서버는 서명된 IdP 토큰으로 사용자·프로젝트 grant를 해소하고 확인창의 결정을 현재 계획에 연결한다.
접속부는 프로젝트·자신의 업무 목록을 사용하며 기억과 실제 업무 완료는 현재 근거로 판단한다.

### 배포와 확장 시 확인할 조건

| 조건 | 결정·확인 시점 |
|---|---|
| AWS 계정·리전·회사 IdP·구성원 | resource audience·필수 scope·프로젝트 claim이 있는 access token과 전체 자료 공유 범위 확인 |
| OAuth 클라이언트·MCP 접속 | public client·호스트별 callback 등록, Runtime JWT 검사·Authorization 전달·MCP 세션 라우팅 |
| 승인 hook·IAM | 서비스 전용 harness 호출, 보호된 세션·승인 기록 쓰기, 제한된 Lambda Invoke·실패 시 deny |
| 중앙 프로젝트·모델·도구 | SSM 조회 권한과 Harness·Gateway의 운영자 등록·실제 호출 확인 |
| 독립 Memory | 프로젝트별 리소스·정적 공유 namespace·장기 전략·TTL 생략·harness 연결과 사용자별 기록 확인 |
| S3 수명·접근 | 버킷·암호화·버전 관리·삭제 권한·자동 만료 생략·배포 제거 정책과 Memory 독립 보존 확인 |
| 리소스 자격·네트워크 | Gateway·Identity의 프로젝트 자격·공유 PAT·개인 OAuth 동의와 사내망·CA·처리 위치 확인 |
| 소스 실행 위치·Git 반영 | PC/AWS 도구·기존 변경·인증을 대조해 결정 |
| 운영·자료 이관 | S3 prefix·목록 페이지·예산·중단 복구·관측·보존 조건과 이관 대상을 확인 |

실제 IdP·호스트·Lambda와 Memory·S3·harness의 재개·현재 신원·다운로드 접근을 검증한다.
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
| [core/cloud.py](../../src/daeri/core/cloud.py)·[approval.py](../../src/daeri/core/approval.py)·[access.py](../../src/daeri/core/access.py) | 업무·현재 계획·결정·인증한 구성원과 저장·harness 포트 |
| [core/projects.py](../../src/daeri/core/projects.py) | 프로젝트 이름·harness의 내부 계약과 조회 포트 |
| [core/errors.py](../../src/daeri/core/errors.py)·[serialization.py](../../src/daeri/core/serialization.py) | 경계 오류·저장 가능한 유한 UTF-8 JSON 검사 |
| [application/cloud.py](../../src/daeri/application/cloud.py)·[agent.py](../../src/daeri/application/agent.py) | 내부 업무 호출, 공개 권한·목록·계획 확인·기존 응답 복구 |
| [infrastructure/project_catalog.py](../../src/daeri/infrastructure/project_catalog.py) | SSM 프로젝트 JSON 조회·검증·오류 변환 |
| [infrastructure/agentcore.py](../../src/daeri/infrastructure/agentcore.py) | `InvokeHarness` 요청·스트림·도구 대기·오류 해석 |
| [infrastructure/cloud_s3.py](../../src/daeri/infrastructure/cloud_s3.py) | 업무 manifest·조건부 갱신·불변 호출 원본·응답 복구 |
| [infrastructure/auth.py](../../src/daeri/infrastructure/auth.py)·[mcp_server.py](../../src/daeri/infrastructure/mcp_server.py) | OIDC 검증·권한별 MCP 세션·공개 도구·확인창 |
| [infrastructure/approval_hook.py](../../src/daeri/infrastructure/approval_hook.py) | 현재 승인으로 도구 실행을 검사하는 Lambda 진입점 |
| [config](../../src/daeri/config/) | YAML·서버 설정, AWS·인증·실행 대상 검사 조립·종료 |
| [서버 진입점](../../src/daeri/__main__.py)·[플러그인 빌더](../../scripts/build_plugin.py) | 실행 명령·운영자가 설정한 배포물 생성 |
| [프로젝트 배포](../../src/daeri/infrastructure/project_deployment.py)·[Runtime 배포](../../src/daeri/infrastructure/runtime_deployment.py) | 독립 Memory·릴리스 검증·SSM 공개, MCP 호스팅의 네이티브 AWS 설정 |

호출자는 `CloudSettings.from_yaml()`로 설정을 읽고 `build_cloud()`가 반환한 서비스 묶음의 수명을 관리한다.
`workflow.list_projects(contexts)`, `workflow.start()/resume()/recover()`를 호출하고 `close()` 또는 context manager로 클라이언트를 종료한다.
필드·가상 YAML과 중앙 리소스 설정은 [설정 명세](../spec/product.md#설정과-제공자)를 따른다.

저장소는 업무 생성·조회, 실행 점유, 응답 원본 보관 뒤 상태 확정, 원본 기반 복구를 제공한다.
업무의 harness ARN·릴리스 endpoint·버전은 생성 때 저장하며 SSM 변경 뒤에도 같은 실행 대상을 사용한다.
호출 전 실제 endpoint 버전과 해당 버전의 승인 설정을 확인하고 버전이 바뀐 endpoint는 실행을 중단한다.
이전 업무의 상태 조회는 마지막 확정 응답도 읽으며 복구·외부 실행과 구분한다.
호출 진행·응답 저장 실패의 `StoreError`는 확보한 내부 `task_id`·`turn_id`를 제공해 접속부가 복구할 업무를 연결한다.
자신의 업무 목록·현재 상태는 S3에서 조회하고 추가 파일 수집·인증된 다운로드·Memory 재구성은 후속 범위다.

## 서버와 플러그인 연결

운영자는 [서버 설정](../spec/product.md#설정과-제공자)을 작성한 뒤 `uv run daeri --config server.yaml --host 0.0.0.0 --port 8000`으로 ASGI 서버를 시작한다.
운영 환경은 AgentCore Runtime의 MCP 프로토콜로 배포하며 내부 서버는 `0.0.0.0:8000/mcp`를 제공한다.
공개 주소는 Runtime의 운영 endpoint invocation URL이고 직접 호스팅 검증에는 HTTPS `/mcp` 주소도 사용한다.
Runtime은 JWT 진입 검사와 MCP 세션의 실행 환경 라우팅을, 서버는 프로젝트·업무·확인 응답의 권한 검증을 담당한다.
재시작 뒤에는 새 MCP 연결에서 자신의 기존 업무와 마지막 결과를 선택한다.

Lambda의 handler는 `daeri.infrastructure.approval_hook.lambda_handler`다.
`DAERI_STORAGE_BUCKET`과 선택 `DAERI_STORAGE_PREFIX`(기본 `daeri/`)를 서버와 맞추고 프로젝트 harness의 `beforeToolCall`에 해당 ARN과 `failureMode: deny`를 등록한다.
inline `confirm_plan` 도구의 입력은 필수 문자열 `plan`을 가진 객체다.
harness의 system prompt는 다음 업무 절차를 정의한다.

```text
새 요청의 목표·대상·예상 효과·완료 조건을 계획하고 confirm_plan으로 확인한다.
approve이면 현재 계획 안에서 도구·방법·순서·보고 형식을 자율 선택한다.
revise/reject 또는 새 요청은 실제 응답을 반영한 새 계획으로 연결한다.
결과·근거·미충족 조건과 재사용할 경험을 업무 문맥에 유지한다.
```

DAERI 서비스 역할이 harness 실행과 업무·세션·결정 기록을 작성하고, hook 역할은 연결·현재 결정 조회와 승인 해제를 수행한다.
사용자 접속은 OAuth로 제한하고 실행 role의 접근은 업무 리소스·Memory·산출물 범위로 구성한다.
승인·세션 저장 경로 쓰기와 Lambda 직접 호출 권한은 신뢰한 서비스·hook 경계로 한정한다.

플러그인은 공유 Skill·MCP 설정과 `.claude-plugin`·`.codex-plugin` manifest로 구성한다.
다음 가상 예시는 실제 자격 없이 배포물의 공개 연결 설정을 지정한다.

```sh
uv run python scripts/build_plugin.py \
  --endpoint https://daeri.example.com/mcp \
  --client-id company-public-client \
  --callback-port 59383 \
  --output /tmp/daeri-plugin.zip
```

생성기는 새 ZIP에 호스트별 marketplace와 설정된 플러그인을 담는다.
운영자는 이 내용을 배포 저장소에 게시하고 사용자는 호스트의 marketplace 등록·설치를 사용한다.
호스트 설치 경로는 [Claude](https://code.claude.com/docs/en/plugins/install)와 [Codex](https://developers.openai.com/plugins/build/plugins)의 공식 방식을 따른다.

OAuth 서버는 운영자가 사전 등록한 public client와 실제 호스트 callback을 허용해야 한다.
[Codex callback](https://learn.chatgpt.com/docs/extend/mcp)은 issuer 응답 지원·서버 URL에 따라 경로가 달라질 수 있어 Claude callback과 각각 대조한다.
이 서버는 기존 회사 IdP의 발급·로그인을 사용하며 DCR나 OAuth 인증 서버를 별도로 구현하는 범위와 구별한다.
Cognito도 resource audience·scope·프로젝트 claim을 충족하는 access token을 발급하도록 구성한다.

### 프로젝트와 Runtime 배포

운영자는 모델·도구·승인 hook·`confirm_plan`을 갖춘 managed harness와 서비스 역할·S3·회사 IdP를 준비한다.
[프로젝트 설정](../../src/daeri/config/project.py)은 프로젝트 ID·표시 이름·기존 harness ARN·승인 Lambda ARN을 받는다.
선택값은 AWS 리전·SSM 경로·Memory 실행 역할·KMS 키이며 기억 전략과 보존 조건은 [제품 명세](../spec/product.md#공유-기억)가 정한다.

```yaml
project_id: charging
name: 충전 서비스 운영
region: us-east-1
harness_arn: arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/charging-1234567890
approval_hook_arn: arn:aws:lambda:us-east-1:123456789012:function:daeri-approval
```

다음 명령은 가상 설정 파일을 사용하는 배포 진입점이다.
기본 실행은 게시한 프로젝트의 읽기 전용 검사이며 `--apply`가 실제 AWS 생성·변경을 수행한다.

```sh
uv run python scripts/configure_project.py --config project.yaml
uv run python scripts/configure_project.py --config project.yaml --apply
```

프로젝트 도구는 카탈로그 경로·프로젝트 ID에서 Memory 이름을 결정하고 소유 태그·전략·검색·보존 조건을 검사한다.
Memory 연결 뒤 검증한 `RELEASE_V<버전>` endpoint를 생성하고 SSM을 마지막에 공개한다.
조건이 다른 기존 Memory·릴리스는 `PROJECT_NOT_READY`로 반환하며 이전 릴리스와 원본의 수명은 유지한다.
다른 BYO Memory가 이미 연결된 harness는 기존 기억의 이관을 확인한 뒤 배포한다.
배포 역할이 릴리스 endpoint 변경 권한을 관리한다.
같은 프로젝트의 배포는 운영 파이프라인에서 순차 실행해 중앙 참조의 공개 순서를 유지한다.

[Runtime 설정](../../src/daeri/config/runtime.py)은 `name`·`image_uri`·`role_arn`·`client_ids`와 서버 설정의 `server` 매핑을 받는다.
`server`는 앞의 서비스 설정에서 자동 생성되는 `public_url`을 제외한 필드다.
기존 배포 갱신에는 반환된 `runtime_id`를 사용하며 선택 `endpoint`의 기본값은 `PROD`, 네트워크는 기본 PUBLIC 또는 네이티브 VPC 설정이다.

```sh
docker buildx build --platform linux/arm64 -t 123456789012.dkr.ecr.us-east-1.amazonaws.com/daeri:example --push .
uv run python scripts/configure_runtime.py --config runtime.yaml
uv run python scripts/configure_runtime.py --config runtime.yaml --apply
```

Runtime 도구의 기본 실행은 요청 구성 검사이며 `--apply`가 컨테이너·MCP 프로토콜·JWT authorizer·Authorization 전달을 배포한다.
생성한 Runtime 주소를 서비스 환경 설정에 연결하고 준비된 버전을 endpoint로 공개한다.
반환한 `public_url`로 플러그인을 생성한다.
Runtime의 운영 endpoint는 서비스 코드 배포 때 갱신하며, 개별 업무의 harness 릴리스 endpoint와 구분한다.
같은 Runtime의 배포도 순차 실행해 버전 확인과 endpoint 공개를 한 배포 작업으로 유지한다.
컨테이너 이미지·역할·로그인·네트워크 설정에는 공개 연결 정보와 AWS 참조를 사용한다.

## 구현과 대표 검증

검증은 관찰 가능한 결과·실패 경계·실제 SDK 요청 형식을 대상으로 한다.
모의 AWS·저장소의 검증과 실제 서비스 연결은 각각 결과를 구분한다.

| 대상 | 대표 검증 |
|---|---|
| 프로젝트 JSON·SSM 요청·조회 실패 | [프로젝트 설정 테스트](../../tests/test_project_catalog.py) |
| 허용 목록·프로젝트 전환·harness 유지·시작·재개·복구 | [업무 테스트](../../tests/test_cloud_workflow.py)·[사용자 승인 통합](../../tests/test_agent_service.py) |
| SDK 전달·스트림·도구 대기·오류·출력 검사 | [harness 테스트](../../tests/test_agentcore_harness.py) |
| S3 조건부 쓰기·충돌·원본 우선 보관·중간 장애·복구 | [저장 계약 테스트](../../tests/test_cloud_s3.py) |
| 설정·서버 조립·실행 대상·종료 | [기본 설정](../../tests/test_cloud_config.py)·[서버 설정](../../tests/test_server_config.py)·[실행 명령](../../tests/test_server_entrypoint.py) |
| 토큰·서명·grant·세션·HTTP·확인창 | [인증](../../tests/test_auth.py)·[실제 localhost MCP 왕복](../../tests/test_mcp_server.py) |
| 승인 전 차단·바인딩·Lambda·배포물 | [승인 hook](../../tests/test_approval_hook.py)·[플러그인 빌드](../../tests/test_plugin_bundle.py) |
| 공유 Memory·릴리스·공개 순서·Runtime 구성 | [프로젝트 배포](../../tests/test_project_deployment.py)·[Runtime 배포](../../tests/test_runtime_deployment.py) |
| YAML·문서 링크·계층 경계 | [YAML 테스트](../../tests/test_yaml.py)·[문서 테스트](../../tests/test_documentation_links.py)·[패키지 테스트](../../tests/test_package_boundaries.py) |

전체 pytest는 실제 회사 자격·AWS·운영 리소스 없이 실행하며 MCP는 localhost의 실제 HTTP·SDK client로 왕복한다.
실제 SSM·S3 조건부 쓰기·접근·목록 페이지와 Memory의 사용자 간 공유·추출 지연·검색 품질·정정·삭제 후 복구는 실제 연동 대상이다.
원본 보존·추가 이벤트 수집·Memory 재구성, 단기 이벤트 만료 후 새 세션의 계획·승인·업무 복구도 실제 AWS에서 확인한다.
실제 두 호스트의 설치·회사 IdP·Gateway·위임 인증과 배포 완료는 실제 계정·리전·네트워크·다중 사용자 동작의 근거로 판단한다.
