# DAERI 개발·운영 안내

상태: AWS 공용 서비스의 구현·설정·배포·API·검증 기준.
사용자 동작과 AWS·DAERI의 역할은 [서비스 명세](../spec/service.md), 작업 방법은 [개발 규칙](../rules/development.md)을 따른다.

[개발 환경](#개발-환경) · [코드 구조](#코드-구조) · [설정](#설정) · [배포와 운영](#배포와-운영) · [API와 저장 계약](#api와-저장-계약) · [구현과 검증](#구현과-검증)

## 개발 환경

지원 Python·의존성 버전은 [pyproject.toml](../../pyproject.toml)·[uv.lock](../../uv.lock)이 소유한다.
Git 작업 환경 검증에는 POSIX 파일 잠금이 필요하므로 macOS·Linux 또는 Windows WSL2를 사용한다.

```sh
uv sync --frozen
uv lock --check
uv run --no-sync pytest
uv build
```

## 코드 구조

핵심 계약 `core` ← 흐름 `application` ← 외부 연결 `infrastructure`를 `config`에서 조립한다.
외부 어댑터가 SDK 객체·오류를 내부 포트의 자료형·오류로 변환한다.

| 위치 | 책임 |
|---|---|
| [core](../../src/daeri/core/) | 업무·프로젝트·승인·인증 문맥, 저장·harness 포트, JSON·오류 |
| [CloudWorkflow](../../src/daeri/application/cloud.py)·[AgentService](../../src/daeri/application/agent.py) | 업무 실행·권한·목록·확인·복구 |
| [project_catalog.py](../../src/daeri/infrastructure/project_catalog.py)·[cloud_s3.py](../../src/daeri/infrastructure/cloud_s3.py)·[artifacts.py](../../src/daeri/infrastructure/artifacts.py) | SSM 조회, S3 상태·원본·복구·파일 |
| [agentcore.py](../../src/daeri/infrastructure/agentcore.py)·[harness_oauth.py](../../src/daeri/infrastructure/harness_oauth.py) | [관리형 harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-vs-runtime.html) 호출·스트림·요청별 Bearer 전달 |
| [auth.py](../../src/daeri/infrastructure/auth.py)·[mcp_server.py](../../src/daeri/infrastructure/mcp_server.py) | OIDC·HTTP MCP·사용자 확인창 |
| [approval_hook.py](../../src/daeri/infrastructure/approval_hook.py)·[delegation.py](../../src/daeri/infrastructure/delegation.py)·[memory_archive.py](../../src/daeri/infrastructure/memory_archive.py) | 승인·위임·Memory 수집 Lambda와 설정·복원 |
| [workspaces.py](../../src/daeri/infrastructure/workspaces.py)·[workspace_cli.py](../../src/daeri/infrastructure/workspace_cli.py) | 업무별 Git 사본·통합 검사와 실행 명령 |
| [연결 서비스](../../src/daeri/application/connections.py)·[연결 설정](../../src/daeri/config/connections.py)·[AWS 연결](../../src/daeri/infrastructure/connections.py)·[MCP 런처](../../src/daeri/infrastructure/connector_cli.py) | 프로젝트 연결 권한·확인·배포·등록·프로그램 시작 |
| [프로젝트 스택](../../deploy/project.yaml)·[릴리스 공개](../../src/daeri/infrastructure/project_deployment.py) | Memory·Harness·릴리스 배포와 검증 후 SSM 공개 |
| [Runtime 배포](../../src/daeri/infrastructure/runtime_deployment.py)·[Git Skill 게시](../../src/daeri/infrastructure/workspace_deployment.py) | 공용 MCP 접속부 배포와 내용별 S3 번들 게시 |
| [config](../../src/daeri/config/)·[서버 진입점](../../src/daeri/__main__.py)·[scripts](../../scripts/) | YAML·AWS·인증 조립·종료, 서버·배포물 생성 |

## 설정

`CloudSettings.from_yaml(path)`는 내부 서비스, `ServerSettings.from_yaml(path)`는 공개 MCP 서버의 UTF-8 YAML을 읽는다.
허용 입력은 최상위 매핑·유한한 JSON 호환 값·2MiB 이내·중첩 32단계다.
중복 키·명시적 태그·앵커·별칭·병합·지시문·알 수 없는 필드는 원문을 포함하지 않는 `INVALID_INPUT`으로 처리한다.

파일·스키마 검사 뒤 명시적인 조립·배포 실행에서 AWS 클라이언트와 자격을 연결한다.

### 서버 설정

| 필드 | 입력·기본값 |
|---|---|
| `region` | 선택 AWS 리전; 생략 시 SDK의 리전 해소 |
| `project_parameter_prefix` | SSM 프로젝트 경로; 기본 `/daeri/projects/` |
| `storage.bucket` | 필수 일반 S3 버킷 이름 |
| `storage.prefix` | 상대 경로; 기본 `daeri/`; 끝 `/` 포함 최대 788바이트 |
| `public_url` | 공개 서버의 필수 HTTPS `/mcp` 또는 Runtime 운영 endpoint invocation URL |
| `auth.issuer`·`auth.audience` | 서버 필수 신뢰한 OIDC 발급자·리소스 토큰 audience |
| `auth.project_claim` | 프로젝트 ID 배열 claim; 기본 `projects` |
| `auth.management_claim` | 연결 관리 프로젝트 ID 배열 claim; 기본 `managed_projects` |
| `auth.required_scopes` | 필수 OAuth scope 목록; 기본 `[daeri/use]` |
| `approval_hook_arn` | 서버 필수 승인 검사 Lambda ARN |
| `harness_oauth_audience` | 선택 inbound OAuth audience; 생략 시 IAM; 지정 시 `auth.audience`와 동일 |
| `connections.prefix` | 연결 기능을 켜는 선택 매핑의 SSM 경로; 기본 `/daeri/connections/` |

고정된 issuer의 discovery·JWKS로 RS256 서명과 `iss`·`aud`·`sub`·`exp`·`scope`·프로젝트 claim을 검사한다.
프로젝트·관리 claim은 중복 없는 ID 배열, `sub`는 실제 사용자이며 권한 유효성은 서명된 토큰의 만료 시각을 기준으로 판단한다.
관리 권한은 구성원 권한에 추가하며 관리 claim을 생략한 사용자는 연결 요약만 조회한다.
관리 프로젝트 배열은 구성원 프로젝트 배열의 부분 집합이어야 한다.
`connections`를 생략하면 연결 기능을 끄고 기존 서버 설정을 그대로 사용한다.

`storage.prefix`는 영문·숫자·`_`·`.`·`-`·`/`로 구성하고 생략한 끝 `/`는 추가한다.
빈 구간·중복 `/`·`.`·`..` 구간은 입력 오류로 처리한다.
다음은 가상 서버 설정이다.

```yaml
region: us-east-1
public_url: https://daeri.example.com/mcp
auth:
  issuer: https://login.example.com/company
  audience: https://daeri.example.com/mcp
  management_claim: managed_projects
connections:
  prefix: /daeri/connections/
approval_hook_arn: arn:aws:lambda:us-east-1:123456789012:function:daeri-approval
storage:
  bucket: daeri-example
```

### 프로젝트 설정

SSM의 `<project_parameter_prefix><project_id>`에 프로젝트별 JSON을 등록한다.
표시 이름·실행 대상은 다음 네 필드로 지정한다.

```json
{
  "name": "충전 서비스 운영",
  "harness_arn": "arn:aws:bedrock-agentcore:us-east-1:123456789012:harness/charging-1234567890",
  "harness_endpoint": "RELEASE_V2",
  "harness_version": "2"
}
```

| 값 | 조건 |
|---|---|
| `project_id` | 영문·숫자로 시작하는 영문·숫자·`_`·`-`, 1~128자 |
| `name` | 앞뒤 공백·제어 문자가 없는 1~256자 |
| `harness_arn` | 유효한 AgentCore harness ARN |
| `harness_endpoint` | 영문으로 시작하는 영문·숫자·`_`, 1~48자 릴리스 이름; `DEFAULT`는 입력 오류 |
| `harness_version` | 1~5자리 양의 정수 문자열 |

`GetParameter`의 미등록 대상은 `PROJECT_UNAVAILABLE`, 그 외 조회 실패는 `STORAGE_UNAVAILABLE`, 잘못된 JSON·필드는 `INVALID_INPUT`으로 반환한다.
호출 전 endpoint의 실제 버전과 저장한 버전을 대조해 불일치는 `HARNESS_NOT_READY`로 처리한다.

## 배포와 운영

인프라 생성·변경은 인프라 운영 담당자의 승인된 배포 체계에서 진행한다.
실제 배포 설정은 Git에서 제외되는 `.local/deploy/`에 보관한다.
아래 예시의 계정·주소·리전·역할은 배포 환경의 값으로 지정한다.

### 서버와 플러그인

1. 서비스 역할·S3·회사 IdP를 준비하고 아래 절의 프로젝트 스택을 배포한다.
2. `uv run daeri --config .local/deploy/server.yaml --host 0.0.0.0 --port 8000`으로 ASGI 서버를 시작한다.
3. 운영 환경은 AgentCore Runtime의 MCP 프로토콜로 배포하고 내부 서버는 `0.0.0.0:8000/mcp`를 제공한다.
4. 공개 endpoint로 플러그인 ZIP을 생성해 배포 저장소에 게시한다.

```sh
uv run python scripts/build_plugin.py --endpoint https://daeri.example.com/mcp --client-id company-public-client --callback-port 59383 --output /tmp/daeri-plugin.zip
```

빌드는 공유 Skill·MCP 설정·호스트별 manifest와 marketplace를 묶는다.
사용자 설치는 [README](../../README.md)를 따른다.
IdP에는 public client·실제 callback·audience·scope·프로젝트 claim을 등록하고, [Codex callback](https://learn.chatgpt.com/docs/extend/mcp)의 issuer·URL별 차이를 대조한다.
HTTP 서버는 [공식 MCP SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)의 stateful Streamable HTTP·인증 middleware를 사용한다.

승인 Lambda handler는 `daeri.infrastructure.approval_hook.lambda_handler`다.
`DAERI_STORAGE_BUCKET`·선택 `DAERI_STORAGE_PREFIX=daeri/`를 서버와 맞추고 harness의 [beforeToolCall](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-lifecycle-hooks.html)에 ARN·`failureMode: deny`를 등록한다.
inline `confirm_plan`은 필수 문자열 `plan`을 받는 객체이며 system prompt는 [계획·승인 절차](../spec/service.md#계획과-승인)를 정의한다.
서비스 역할은 호출·업무·세션·결정을 작성하고 hook 역할은 연결·현재 결정 조회와 승인 해제를 수행한다.
업무·승인·세션 저장 쓰기는 서비스·hook의 담당 경로에, 위임 Lambda 호출은 해당 프로젝트 Gateway 역할에 연결한다.

### 프로젝트 스택과 공개

[프로젝트 스택](../../deploy/project.yaml)은 한 프로젝트의 독립 Memory·총괄 Harness·`RELEASE_V<버전>` endpoint를 배포한다.
같은 스택에서는 프로젝트 ID와 카탈로그 경로를 고정하고, 갱신 시 이전 Harness·endpoint·Memory를 보존한다.
첫 배포에서 [스택 정책](../../deploy/project-stack-policy.json)을 적용해 기존 Memory의 소유 태그와 설정을 보호한다.
프로젝트별 스택 갱신과 카탈로그 공개는 순서대로 실행한다.
업무의 릴리스 고정과 이어가기는 [프로젝트와 접근](../spec/service.md#프로젝트와-접근)을 따른다.

| 스택 입력 | 동작 |
|---|---|
| `ProjectId`·`ProjectName`·`ProjectCatalogPrefix` | 프로젝트 ID·표시 이름·`/`로 끝나는 절대 카탈로그 경로를 지정한다. 경로 기본값은 `/daeri/projects/`다. |
| `HarnessName`·`HarnessRoleArn`·`ApprovalHookArn`·`GatewayArn` | 총괄 Harness의 이름과 운영자가 준비한 역할·승인 Lambda·프로젝트 Gateway를 연결한다. |
| `ModelId`·`CompatibleApiBase`·`CompatibleApiKeyArn` | 모델 ID를 지정한다. 기본은 Bedrock이며, 호환 API를 쓰면 HTTPS 주소와 Identity 자격 ARN을 함께 지정한다. |
| `JwtDiscoveryUrl`·`JwtAudience` | 공용 서버와 같은 IAM 또는 JWT 경로를 사용한다. 개인 위임에는 두 값과 서버의 `harness_oauth_audience`를 맞춘다. |
| `ExistingMemoryArn` | 생략 시 새 독립 Memory를 만든다. 기존 또는 복원한 Memory를 쓰면 해당 프로젝트의 ARN을 지정한다. |
| `MemoryExecutionRoleArn`·`MemoryEncryptionKeyArn` | 새 Memory에 사용할 선택 실행 역할과 KMS 키를 지정한다. |
| `SourceSkillUri`·`SourceMountPath` | Git 작업을 쓰는 프로젝트는 두 [Git Skill 게시 결과](#git-작업-환경)를 함께 지정한다. |

1. 공용 서버와 같은 리전·카탈로그 경로·`approval_hook_arn`으로 프로젝트 스택을 구성한다. `AWS_IAM` 인증을 쓰는 프로젝트 전용 Gateway를 준비하고 Harness 역할에 모델·Memory·해당 Gateway·승인 Lambda 및 필요한 S3 경로 권한을 준다.
2. Git 작업을 제공하면 Skill을 먼저 게시한다. 파라미터 파일은 AWS CLI의 JSON `ParameterKey`·`ParameterValue` 배열 형식으로 저장한다.
3. 새 프로젝트 이름으로 스택을 배포하고 `ProjectConfiguration` 출력을 받는다.
4. 하위 에이전트를 제공하면 해당 릴리스를 [등록](#하위-에이전트와-개인-위임)한다. 프로젝트 릴리스는 `configure_project.py --apply`로 공개한다.

```sh
aws cloudformation create-stack --region ap-northeast-2 --stack-name daeri-example-project \
  --template-body file://deploy/project.yaml --stack-policy-body file://deploy/project-stack-policy.json \
  --parameters file://.local/deploy/project-parameters.json --capabilities CAPABILITY_AUTO_EXPAND \
  --retain-except-on-create
aws cloudformation wait stack-create-complete --region ap-northeast-2 --stack-name daeri-example-project
aws cloudformation describe-stacks --region ap-northeast-2 --stack-name daeri-example-project \
  --query 'Stacks[0].Outputs[?OutputKey==`ProjectConfiguration`].OutputValue | [0]' \
  --output text > .local/deploy/project.json
```

필요한 하위 에이전트 등록을 마친 뒤 검증된 릴리스를 공개한다.

```sh
uv run python scripts/configure_project.py --config .local/deploy/project.json --apply
```

[릴리스 공개 설정](../../src/daeri/config/project.py)은 스택 출력을 받는다.
도구는 선언된 버전과 endpoint의 실제 대상·현재 버전, 승인 hook·도구, Memory의 소유·전략·보존·연결을 확인한 뒤 SSM을 갱신한다.
`--apply`를 생략하면 같은 조건과 이미 공개된 항목의 일치를 읽기 전용으로 확인한다.
검증 실패에는 `PROJECT_NOT_READY` 또는 `HARNESS_NOT_READY`를 반환한다.
갱신은 원본 템플릿을 `aws cloudformation deploy --template-file`로 제출한다.
공유 Memory 설정을 바꿀 때는 프로젝트가 소유한 새 Memory로 이관하고 새 릴리스를 공개한다.
이전 프로젝트를 옮길 때는 기존 Memory ARN과 접근·도구 설정을 먼저 확인한다.
스택과 기존 릴리스의 endpoint를 유지할 기간은 진행 중인 업무와 [AWS 할당량](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)에 맞춰 관리한다.

사설 모델 접속이 필요하면 Harness의 `Environment.AgentCoreRuntimeEnvironment.NetworkConfiguration`에 [VPC 환경](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-environment.html)을 선언한다.
Git을 함께 쓰면 같은 환경에 `FilesystemConfigurations`의 마운트를 포함한다.
Gateway 대상은 운영자가 원격 MCP·OpenAPI·Lambda와 [대상 VPC](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/gateway-vpc-egress.html)·DNS·CA·자격을 준비한다.
개별 연결 추가는 [프로젝트 연결 운영](#프로젝트-연결-운영)을 따른다.

### 공용 MCP Runtime

[Runtime 배포 설정](../../src/daeri/config/runtime.py)은 `name`·`image_uri`·`role_arn`·`client_ids`·`server`를 받는다.
선택값은 갱신할 `runtime_id`, `endpoint`(기본 `PROD`), `network_configuration`(기본 `PUBLIC`)이다.

Runtime 배포 YAML의 `server`에는 서버 입력 필드를 지정한다.
배포 도구가 `public_url`을 생성해 `server` 값과 함께 `DAERI_SERVER_CONFIG` JSON으로 전달한다.
`daeri`는 `--config`를 지정하면 YAML을 읽고, 생략하면 이 환경 변수를 읽는다.
`DEFAULT`를 선택하면 [AWS가 자동 갱신하는 Runtime endpoint](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agent-runtime-versioning.html)의 버전을 확인한다.
그 외 이름의 endpoint는 배포 도구가 생성하거나 갱신한다.
`network_configuration`은 `networkMode: PUBLIC` 또는 `VPC`를 받는다.
VPC의 `networkModeConfig`는 native 문법의 `subnets`·`securityGroups` 두 목록(각각 1~16개)으로 구성하며 PUBLIC은 이를 생략한다.

접속부·리소스 MCP Runtime은 [MMDSv2 필수 조건](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-troubleshooting.html)에 맞춰 `UpdateAgentRuntime`으로 `metadataConfiguration.requireMMDSV2=true`를 적용한다.
배포 도구는 `UpdateAgentRuntime`이 반환한 버전을 `GetAgentRuntime`으로 확인한다.
선택한 endpoint의 현재·대상 버전이 배포 버전과 일치하면 주소를 반환하며 준비 실패는 `RUNTIME_NOT_READY`다.

```sh
docker buildx build --platform linux/arm64 -t 123456789012.dkr.ecr.us-east-1.amazonaws.com/daeri:example --push .
uv run python scripts/configure_runtime.py --config .local/deploy/runtime.yaml
uv run python scripts/configure_runtime.py --config .local/deploy/runtime.yaml --apply
```

기본 실행은 요청 구성을 검사한다.
`--apply`는 컨테이너·MCP·JWT authorizer·Authorization 전달·endpoint를 배포한다.

Runtime의 공개 주소를 서비스 설정에 연결하고 준비된 버전의 endpoint를 공개한 뒤 반환한 `public_url`로 플러그인을 생성한다.
서비스 코드의 Runtime endpoint와 개별 업무의 harness 릴리스 endpoint는 각각의 배포 단위다.

### 프로젝트 연결 운영

연결의 사용자 흐름·공유 범위는 [프로젝트 연결](../spec/service.md#프로젝트-연결)이 소유한다.
운영자는 다음 SSM JSON을 준비하고 DAERI 서비스 역할에 카탈로그·프로젝트 조회와 등록 상태 쓰기, S3 등록 잠금, 필요한 Runtime·Gateway 제어·역할 전달 권한을 부여한다.
접속 값·역할·이미지·비밀 ARN의 실제 설정 파일은 `.local/deploy/`에 보관한다.

| `<connections.prefix>` 아래 경로 | 소유하는 값 |
|---|---|
| `catalog/<connector_id>` | 검증한 커넥터 템플릿 |
| `projects/<project_id>` | 허용 커넥터·Gateway·역할·네트워크·비밀 범위·인증 별칭 |
| `registered/<project_id>/<connection_id>` | 연결 관리자가 보관하는 등록 상태·Runtime·Gateway 대상 |

| 커넥터 값 | 조건·기본값 |
|---|---|
| `name`·`description` | 필수 표시 이름·선택 설명 |
| `inputs` | 키별 필드 정의; 기본 빈 매핑 |
| `kind: runtime` | 필수 `image_uri`·`command` 배열, 선택 `environment` |
| `kind: mcp` | 필수 HTTPS `endpoint`; 선언한 입력 치환 허용 |
| `kind: openapi` | 필수 `openapi_schema` 매핑; `servers`는 문자열 `url`을 가진 매핑의 비어 있지 않은 배열이며 설정 치환은 `servers[].url` |

입력 필드는 필수 `title`에 `type=string`(`string`·`integer`·`boolean`)·`required=true`·선택 `default`·`env`·`secret_ref=false`를 지정한다.
`runtime`의 필드는 서로 다른 `env`를 지정하며 비밀 필드는 기본값 없는 문자열 참조다.
원격 MCP·OpenAPI의 인증은 프로젝트 `credentials` 별칭으로 선택한다.
MCP endpoint는 HTTPS 전체 주소 입력 또는 URL 구성 요소의 치환을 지원하며 OpenAPI 경로의 `{itemId}` 같은 도구 인자는 유지한다.

다음 가상 `.local/deploy/connections.yaml`은 운영자가 별도로 제작·검증한 일반 MCP 이미지를 등록한다.
개별 DB·Redis MCP 프로그램과 이미지는 운영자가 준비한다.

```yaml
region: us-east-1
prefix: /daeri/connections/
connectors:
  resource-mcp:
    name: 운영 리소스 MCP
    kind: runtime
    image_uri: 123456789012.dkr.ecr.us-east-1.amazonaws.com/approved-resource-mcp:example
    command: [/opt/approved-mcp/server]
    inputs:
      host: {title: 리소스 주소, env: RESOURCE_HOST}
      port: {title: 포트, type: integer, default: 5432, env: RESOURCE_PORT}
      secret: {title: 기존 Secrets Manager ARN, env: RESOURCE_PASSWORD, secret_ref: true}
projects:
  example:
    gateway_arn: arn:aws:bedrock-agentcore:us-east-1:123456789012:gateway/project-example123
    allowed_connectors: [resource-mcp]
    runtime_role_arn: arn:aws:iam::123456789012:role/daeri-connector-example
    secret_arn_prefix: "arn:aws:secretsmanager:us-east-1:123456789012:secret:daeri/example/"
```

```sh
uv run python scripts/configure_connections.py --config .local/deploy/connections.yaml
uv run python scripts/configure_connections.py --config .local/deploy/connections.yaml --apply
```

기본 실행은 제출한 스키마·프로젝트 소유 범위·Runtime 역할·문서별 8KiB 한도를 로컬에서 검사한다.
`--apply`는 제출한 프로젝트와 커넥터를 기존 SSM 설정과 대조해 검사한 뒤 게시한다.
기존 프로젝트는 `ssm:GetParametersByPath`, 부분 게시에서 필요한 기존 커넥터는 `ssm:GetParameter`로 조회한다.

프로젝트 고정 설정은 `gateway_arn`·`allowed_connectors`를 필수로 받는다.
`runtime`을 허용할 때는 `runtime_role_arn`을, 비밀 참조를 허용할 때는 `secret_arn_prefix`를 지정한다.
선택 `network_configuration`은 [Runtime 네트워크 규격](#공용-mcp-runtime)을 따르며 DB·사내망의 DNS·CA·접근 경로를 운영자가 준비한다.

선택 `credentials`는 별칭을 native Gateway `CredentialProviderConfiguration` 배열에 매핑한다.
원격 MCP·API의 Gateway outbound 인증은 사용자가 `credential_id` 별칭을 선택하면 운영자가 연결한 Identity 제공자를 사용한다.
프로젝트 공유 별칭은 API key·Gateway IAM 역할·OAuth `CLIENT_CREDENTIALS`를 지원하며 개인 OAuth는 [개인 위임](#하위-에이전트와-개인-위임) 경로를 사용한다.
이 자격은 MCP 프로그램이 DB에 접근할 때 사용하는 `secret_ref`와 별개다.
비밀 참조는 프로젝트 `secret_arn_prefix` 아래의 Secrets Manager ARN만 허용한다.
`runtime` 대상은 Gateway IAM 역할의 SigV4 인증을 사용한다.

`runtime` 이미지는 ARM64 Linux에서 `daeri-connector`를 entrypoint로 실행하고 검증한 MCP 프로그램을 포함한다.
프로그램은 Streamable HTTP `0.0.0.0:8000/mcp`를 제공해야 한다.
연결 관리자는 `DAERI_CONNECTOR_CONFIG` JSON에 `command` 배열·문자열 `environment`·환경 변수명별 비밀 ARN인 `secrets`를 넣는다.
런처는 실제 시작 때 Secrets Manager의 `SecretString`을 해소해 자식 프로그램 환경에 전달한다.
Runtime 역할에는 해당 비밀 조회와 네트워크 접근 권한을 부여한다.

연결 적용 전에 프로젝트 카탈로그의 게시된 harness 릴리스·버전과 Gateway 연결을 검사한다.
운영자가 프로젝트 전용 Gateway를 붙여 릴리스를 먼저 공개하고 연결 등록은 그 고정 릴리스를 사용한다.
프로젝트 사이 Gateway와 비밀 ARN 접두사 범위가 겹치면 설정 오류로 처리한다.
DAERI 접속부를 호스팅하는 Runtime과 리소스 MCP를 호스팅하는 Runtime은 별도의 배포 단위다.

연결 상태는 `PROVISIONING`·`FAILED`·`READY`이며 공개 요약은 `id`·`connector_id`·`name`·`state`·`resource_access: not_checked`를 반환한다.
상태의 의미와 실제 접근 확인은 [프로젝트 연결](../spec/service.md#프로젝트-연결)을 따른다.
명시적으로 연결을 갱신하면 endpoint가 같아도 Gateway 대상을 갱신해 도구를 다시 발견한다.
[Runtime 세션 수명](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-lifecycle-settings.html)에 따라 새 MCP 세션은 갱신한 코드·설정과 시작 시 해소한 비밀을 사용하고, 실행 중인 MCP 세션은 시작 때의 코드·설정·비밀을 유지한다.
일부 적용 실패 응답은 오류를 전달한다.
`daeri_connections`의 저장 상태와 원격 상태를 대조한 뒤 같은 연결 ID·커넥터로 재시도한다.
현재 공개 도구는 등록·갱신·조회이며 연결 삭제는 운영자가 별도 절차로 관리한다.
동시 등록은 `CONNECTION_BUSY`로 중단하고 일반 종료 시 잠금을 해제한다.
잠금은 S3의 `<storage.prefix>connection-locks/<등록 원본 owner>.json`을 조건부 작성·삭제한다.
잠금 객체는 `project-id`·`connection-id` 메타데이터를 보관하므로 첫 SSM 등록 원본을 쓰기 전에 중단돼도 대상을 식별할 수 있다.
프로세스 중단으로 잠금이 남으면 운영자가 잠금 메타데이터·SSM 등록 원본·원격 Runtime/대상·실행 종료를 확인하고 해당 S3 객체의 현재 ETag로 조건부 삭제한 뒤 같은 ID로 재시도한다.

### 하위 에이전트와 개인 위임

[하위 설정](../../src/daeri/config/delegation.py)은 `project_id`·`gateway_arn`·`lambda_arn`과 역할별 `description`·`harness_arn`·`harness_endpoint`·`harness_version`을 받는다.
`target_name`은 기본 `subagents`이고 AWS Gateway 대상 이름 문법을 사용하며 handler는 `daeri.infrastructure.delegation.lambda_handler`다.
위임 요청의 `request`는 비어 있지 않은 UTF-8 문자열이며 위반 시 `INVALID_INPUT`을 반환한다.

하위 harness의 timeout은 위임 Lambda 안에 끝나게 구성한다.
하위 사용자 식별자 `agent:<project>:<subagent>`는 역할이며 실제 사용자의 개인 OAuth 위임과 구분한다.
Memory·자격·도구 범위는 [멀티 에이전트와 도구](../spec/service.md#멀티-에이전트와-도구)를 따른다.

총괄 릴리스에는 프로젝트 스택의 `GatewayArn`을 연결한다.
스택의 `ProjectConfiguration`으로 총괄 릴리스와 공유 Memory를 확인한 뒤 대상 Gateway의 도구와 위임 Lambda 설정을 등록한다.

```sh
uv run python scripts/configure_subagents.py --config .local/deploy/subagents.yaml --project-config .local/deploy/project.json --apply
```

`--apply`를 생략하면 Gateway 요청 구성을 검사한다.
개인 위임은 서버와 harness가 같은 `harness_oauth_audience`를 허용하도록 구성하고 Identity의 제공자·사용자 동의·scope를 연결한다.
검증한 실제 사용자와 `actorId`·`runtimeUserId`를 대조하고 전송 전 만료는 `AUTHENTICATION_REQUIRED`로 처리한다.
S3·SSM·제어 API는 서비스 IAM, 하위 위임 Lambda는 프로젝트 서비스 자격을 사용한다.

### Memory 수집과 복원

프로젝트별 [BYO Memory](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-memory.html)의 전략과 namespace는 다음과 같다.
공유 범위·보존 정책은 [공유 기억](../spec/service.md#공유-기억)을 따른다.

| 전략 | Namespace |
|---|---|
| Semantic | `/shared/facts/` |
| Episodic·reflection | `/shared/experiences/` |
| Summarization | `/summaries/{actorId}/{sessionId}/` |

[수집 설정](../../src/daeri/config/memory.py)은 서비스 리전·S3·카탈로그 경로에 `project_id`·`source_memory_id`를 추가한다.
실제 Memory ID는 네이티브 이벤트, 기본 `receipts`는 S3 업무 호출 원본을 수집한다.
보관 위치는 `<storage.prefix>memory/<scope hash>/` 아래의 네이티브 `events/`와 업무 원본 기반 `receipt-events-v2/`로 나눈다.

업무 원본은 [Memory 한도](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/bedrock-agentcore-limits.html)에 맞춰 메시지당 UTF-8 100,000바이트·이벤트당 100항목/직렬화 10,000,000바이트 이내로 나눠 USER→TOOL→ASSISTANT 순서를 보존한다.
같은 호출의 배치는 순서대로 재생하며 S3 보관 시각이 같은 호출 사이의 순서는 근사값으로 취급한다.
이벤트 보관 JSON은 10MiB+2KiB까지 허용하며 업무 상태·호출 요약은 [입력과 응답 보관](#입력과-응답-보관)의 한도를 따른다.

```sh
uv run python scripts/archive_memory.py --config .local/deploy/memory.yaml
uv run python scripts/archive_memory.py --config .local/deploy/memory.yaml --apply
uv run python scripts/archive_memory.py --config .local/deploy/memory.yaml --restore-to Restored_Example-0123456789 --apply
```

기본 실행은 읽기 전용 검사이며 `--apply`가 불변 원본·진행 기록 또는 별도 대상 이벤트를 작성한다.
예약 handler는 `daeri.infrastructure.memory_archive.lambda_handler`, `DAERI_MEMORY_ARCHIVE_CONFIG`는 같은 설정의 JSON이다.
한 호출은 최대 50페이지·약 45초 범위를 처리하고 S3 진행 상태로 이어가며 전체 순회 뒤 새 수집 주기를 시작한다.

EventBridge Scheduler·권한·실패 알림을 연결하고 원본 이벤트의 보존 기간 안에 수집을 완료한다.
동일 프로젝트 소유의 비어 있는 독립 Memory에 재생하고 비동기 추출·검색을 검증한다.
복원한 Memory는 프로젝트 스택의 `ExistingMemoryArn`에 연결하고 새 릴리스를 공개한다.
재생은 원본별 고정 `clientToken`과 S3 진행 기록으로 중복 처리를 대조하고 중단 지점에서 이어간다.

이전 업무 원본 수집 자료는 유지하고 원본을 `receipt-events-v2/`로 재수집하며, 이전 형식에 연결한 복원 대상은 새 빈 프로젝트 Memory로 교체한다.

### Git 작업 환경

[Git Skill](../../src/daeri/assets/source-workspace/SKILL.md)은 기본 shell에서 표준 라이브러리만 사용하는 `workspace.pyz`를 실행한다.
다음 가상 `workspaces.yaml`은 실행 환경에 보이는 경로 또는 원격 Git 주소를 여러 개 등록한다.

```yaml
root: /mnt/work/source
repositories:
  api: https://git.example.com/project/api.git
  web: https://git.example.com/project/web.git
```

```sh
uv run python scripts/configure_workspaces.py --project-id example-project --config .local/deploy/workspaces.yaml --skill-uri s3://daeri-example/skills/ --apply
```

기본 실행은 묶음 구성을 검사하고 `--apply`는 내용 해시별 S3 Skill·실행 묶음·설정을 게시한다.
게시 권한은 대상 S3 경로의 쓰기와 재시도 대조용 읽기로 구성한다.
출력 `skill_uri`·`mount_path`를 스택의 `SourceSkillUri`·`SourceMountPath`에 지정한다.
AWS 마운트는 `root`의 `/mnt` 아래 첫 구간을 사용한다. 예제에서는 `/mnt/work`를 마운트하고 Git 사본은 `/mnt/work/source`에 둔다.
Git·네트워크·표준 자격은 프로젝트 실행 환경에서 준비한다.
설치된 개발 환경에서는 생성한 JSON 설정으로 `daeri-workspace --config .local/deploy/workspaces.json prepare TASK_UUID REPOSITORY`를 사용한다.
재개·통합 조건은 [소스와 산출물](../spec/service.md#소스와-산출물)을 따른다.
`prepare`는 기준 ref를 fetch해 commit과 `daeri/<task UUID>/<repository>` 브랜치·사본을 고정하며 재호출은 저장한 기준을 유지한다.
새 환경의 재개는 선택한 기준 ref와 원격 업무 브랜치의 공통 조상을 기록하고, `prepare`·`verify`의 `--reference` 기본값은 `HEAD`다.

Git 명령은 성공 결과를 stdout의 JSON으로, 실패 이유를 stderr와 종료 코드 2로 전달한다.

| 실패 조건 | 내부 코드와 다음 조치 |
|---|---|
| 기록한 사본·브랜치·기준 commit 정보에 접근할 수 없거나 내용이 다름 | `SOURCE_WORKSPACE_UNAVAILABLE`, 저장한 사본과 기록 확인 |
| 로컬·원격 업무 브랜치의 이력이 갈라짐 | `SOURCE_WORKSPACE_CONFLICT`, 양쪽 commit을 비교해 통합 |
| 커밋하지 않은 변경이 남은 사본에서 통합 검사 요청 | `SOURCE_WORKSPACE_DIRTY`, 업무 변경을 커밋한 뒤 검사 |
| 복제·가져오기·통합 검사 등 Git 명령 실패 | `SOURCE_OPERATION_FAILED`, 저장소와 명령 실행 상태 확인 |

## API와 저장 계약

### 공개 MCP 도구

모든 도구는 현재 요청의 검증된 사용자와 선택한 프로젝트로 호출한다.
업무 도구는 `AgentService`, 연결 도구는 `ConnectionService`가 권한과 흐름을 검사한다.

| 도구 | 입력·결과 |
|---|---|
| `daeri_projects` | 허용된 프로젝트 목록 |
| `daeri_connectors` | `project_id`; 관리자가 선택할 허용 커넥터·인증 별칭 목록 |
| `daeri_connections` | `project_id`; 구성원이 공유하는 연결 요약 |
| `daeri_connect` | `project_id`·`connection_id`·`connector_id`; 관리자의 실제 호스트 입력·확인 뒤 등록·갱신 |
| `daeri_tasks` | `project_id`, 선택 `cursor`·`limit=20`; 자신의 업무·다음 커서 |
| `daeri_work` | `project_id`·`request`, 선택 `task_id`; 새 업무·후속 요청·피드백 |
| `daeri_status` | `project_id`·`task_id`; 목표·진행·계획·확인 상태·마지막 보관 응답 |
| `daeri_confirm` | `project_id`·`task_id`; 저장한 계획의 호스트 확인창 |
| `daeri_recover` | `project_id`·`task_id`, 선택 `restart=false`; 보관 응답 반영 또는 확인 후 새 세션 |
| `daeri_files` | `project_id`·`task_id`, 선택 `cursor`·`limit=20`; 실제 S3 산출물·다음 커서 |
| `daeri_download` | `project_id`·`task_id`·`artifact_id`; 선택 버전의 임시 다운로드 링크 |

업무·파일 페이지 크기는 1~100이며 커서는 사용자·프로젝트·페이지 크기에, 파일은 업무에도 연결한다.
커넥터·연결 ID는 `project_id`와 같은 1~128자 문법을 사용한다.
`daeri_connect`의 모델 입력은 식별자 세 개이며 접속 필드는 서버가 생성한 호스트 확인창에서만 받는다.
확인 응답 뒤 현재 신원과 관리 권한을 다시 검사하고 적용 요청이면 카탈로그와 프로젝트 고정 설정도 대조한다.
취소·미지원·300초 초과에는 기존 연결을 유지한다. 로그인과 관리 권한이 유효하면 `state: cancelled`, 로그인이 만료됐으면 `AUTHENTICATION_REQUIRED`를 반환한다.
세 연결 도구는 서버의 `connections` 설정을 켰을 때 공개한다.

계획 확인창의 결정·대기·취소·만료 처리는 [계획과 승인](../spec/service.md#계획과-승인), 세션 전환은 [실패와 복구](../spec/service.md#실패와-복구)를 따른다.

### 내부 업무 호출

`build_cloud()`는 `store`·`workflow`·클라이언트의 `CloudServices`, `build_server()`는 verifier·실행 대상 검사·`AgentService`·선택 `ConnectionService`·HTTP MCP를 조립한다.
호출자는 `close()` 또는 context manager로 자원을 종료한다.

| `CloudWorkflow` 호출 | 입력·수행 조건 |
|---|---|
| `list_projects(contexts)` | 동일 사용자의 프로젝트별 `AccessContext` 목록·튜플 → `id`·`name`; 사용자 혼합·프로젝트 중복은 `INVALID_INPUT` |
| `start(context, prompt, *, project=None)` | 비어 있지 않은 요청으로 업무·세션과 harness 릴리스를 저장. 같은 프로젝트에서 미리 확인한 `Project`를 전달하거나 카탈로그에서 조회 |
| `get_task(context, task_id)` | 현재 카탈로그·작성 사용자·프로젝트와 저장한 문맥 대조 |
| `resume(context, task_id, prompt='', tool_results=(), expected_etag=None)` | 저장한 대상·세션, 선택 ETag 대조, 후속 요청 또는 실제 대기 응답 |
| `recover(context, task_id)` | 이미 저장한 응답을 상태에 반영 |
| `restart(context, task_id, review, expected_etag=...)` | 실제 확인 내용·현재 ETag → 이전 상태 보존·새 세션·새 계획 |

`AccessContext`는 인증 경계가 제공한 `actor_id`·`project_id`이며 저장한 문맥 불일치는 `ACCESS_DENIED`다.
릴리스 고정·버전 정보가 없는 업무의 처리는 [프로젝트와 접근](../spec/service.md#프로젝트와-접근)을 따른다.
원격 확인은 native handoff의 첫 `confirm_plan` 대기 하나를 처리하고 다음 도구는 재개한 harness가 선택한다.

### 입력과 응답 보관

요청·도구 응답의 문자열과 JSON 키는 유효한 UTF-8, 응답은 유한한 JSON 직렬화 값으로 받는다.
정상 한글·이모지와 JSON 이스케이프로 보존 가능한 NUL 문자는 허용한다.
위반은 원문을 포함하지 않는 `INVALID_INPUT`으로 업무 생성·harness 호출 전에 중단하고 기존 대기를 유지한다.

`tool_results`는 저장한 `toolUseId`마다 한 번씩 정확히 대응하고 대기 순서로 전달한다.
상태는 `success`·`error`, 본문은 text·JSON 블록이며 대기가 없으면 비어 있지 않은 후속 요청을 받는다.

S3 업무 상태·호출 요약 JSON은 객체당 8MiB 이내로 받고 산출물 본문은 별도 파일로 보관한다.
전체 호출 기록이 8MiB를 초과하면 입력·응답 원본을 업무 파일로 보관하고 요약 기록에 S3 버전·경로·크기·SHA256·입력 해시·원래 종료 사유·오류를 연결한다.

요약 응답은 `HARNESS_INVALID_OUTPUT`과 파일 확인·새 세션 복구 경로를 반환하며 기존 도구 대기를 해제한다.
원본이 별도 보관된 이전 세션의 후속 실행은 같은 오류 코드와 `task_id`·`turn_id`를 반환한다. 사용자의 복구 확인 뒤 새 세션에서 실행한다.
원본 다운로드·기억 재구성은 고정한 S3 버전을 사용하고 재구성 때 경로·크기·해시·실행 문맥을 검증한다.

요약 보관 전 장애로 원본 버전을 확정할 수 없으면 `EXECUTION_UNCERTAIN`을 유지하고 실제 효과 확인 뒤 새 세션으로 복구한다.
원본 보존 뒤 manifest를 조건부 갱신해 저장 응답을 반환하며 `StoreError`는 확보한 `task_id`·`turn_id`로 복구 대상을 연결한다.

### 오류와 복구

| 조건 | 결과·다음 조치 |
|---|---|
| 동일 업무의 실행 진행 중 | 조회 `running`, 추가 실행 `TASK_BUSY` |
| 로그인 만료·현재 문맥 또는 프로젝트 권한 불일치 | `AUTHENTICATION_REQUIRED` 또는 `ACCESS_DENIED`; 현재 인증 확인 |
| 현재 확인할 계획 없음 | `PLAN_UNAVAILABLE` |
| 승인 설정 또는 고정 릴리스 미비 | `HARNESS_NOT_READY` |
| 입력·도구 대기 대응 오류 | `INVALID_INPUT`; 현재 대기 확인 |
| 미등록 프로젝트 | `PROJECT_UNAVAILABLE` |
| 연결 설정·Gateway·Runtime·도구 발견 미준비 | `CONNECTION_NOT_READY`; 고정 설정·원격 상태 확인 후 같은 연결 재시도 |
| 연결 제어·저장 실패 | `CONNECTION_UNAVAILABLE`; 민감 원문을 제외한 오류와 연결 상태 확인 |
| 확인 중 연결 설정 변경 | `CONNECTION_CHANGED`; 현재 설정으로 확인창 다시 열기 |
| 동일 연결 등록 중·중단 후 잠금 유지 | `CONNECTION_BUSY`; 실행·원격 상태와 등록 잠금 확인 |
| 파일 버전 관리 없음·객체 조회 실패 | `ARTIFACT_UNAVAILABLE`; 현재 업무의 파일·버전 확인 |
| 저장 객체 변경·원본 불일치 | `STORAGE_CONFLICT`; 현재 상태 확인 |
| 저장 접근 실패·손상된 자료 | `STORAGE_UNAVAILABLE`; 보관 상태 확인 |
| 응답 원본이 없는 진행 업무 | `EXECUTION_UNCERTAIN`; 실제 세션·외부 효과 확인 |
| SDK 권한·대상·호출 제한·입력·할당량 오류 | `HARNESS_ACCESS_DENIED`·`HARNESS_NOT_FOUND`·`HARNESS_THROTTLED`·`HARNESS_VALIDATION_ERROR`·`HARNESS_QUOTA_EXCEEDED` |
| 그 외 호출 실패·불완전 스트림 | `HARNESS_UNAVAILABLE`·`HARNESS_STREAM_INCOMPLETE`; 확보한 응답·오류 보존 |
| 스트림의 서버·runtime·도구 입력 오류 | `HARNESS_INTERNAL_SERVER_ERROR`·`HARNESS_RUNTIME_ERROR`·`HARNESS_INVALID_TOOL_INPUT` |
| 토큰·시간·반복 한도 등의 명시적 종료 | `HARNESS_<종료 사유의 대문자>`; 예: `HARNESS_MAX_TOKENS`·`HARNESS_TIMEOUT_EXCEEDED` |
| Guardrails 개입 종료 | `HARNESS_GUARDRAIL_INTERVENED`·확보한 텍스트 보존 |
| 본문·대기 도구·관찰의 저장 조건 위반 | `HARNESS_INVALID_OUTPUT`; 대기 해제·유효한 근거 보존, 크기 초과는 전체 기록을 업무 파일로 보관 |

HTTP 진입에서 잘못된 bearer 토큰은 401, 필수 scope 부족은 403이다.
공개 도구 실행 중 로그인 만료나 권한 오류는 위 표의 코드를 구조화 응답으로 반환한다.
SDK의 다섯 명시 오류나 전송 전 위임 자격 만료로 스트림 수신 전에 거절되면 기존 대기를 유지하고 재개 때 같은 실제 응답을 다시 제출한다.
대기 ID 중복은 불완전 스트림으로 기록하며 잘못된 본문 유니코드는 눈에 보이는 이스케이프로 보존한다.
호스트에서 처리할 수 없는 도구 대기는 `interrupted`와 `awaiting_confirmation: false`로 표시한다.
보관된 오류가 없으면 `PLAN_UNAVAILABLE`을 표시하며, 실제 효과를 확인한 뒤 `daeri_recover(restart=true)`로 이어간다.
S3 상태 확정 실패는 보관 응답으로 복구한다. 그 외 중단 응답은 실제 효과와 현재 대기를 확인해 후속 동작을 판단한다.
`end_turn`은 한 응답의 종료이며 업무 완료는 목표·실제 결과로 판정한다.

## 구현과 검증

다음은 [테스트 디렉터리](../../tests/)의 계약별 진입점이다.

| 대상 | 대표 검증 |
|---|---|
| 업무·승인 | [흐름](../../tests/test_cloud_workflow.py)·[사용자 서비스](../../tests/test_agent_service.py)·[hook](../../tests/test_approval_hook.py) |
| 새 세션·확인창·동시 변경 | [복구](../../tests/test_task_restart.py)·[HTTP 복구](../../tests/test_task_restart_http.py) |
| harness·개인 OAuth | [스트림](../../tests/test_agentcore_harness.py)·[토큰 전달](../../tests/test_harness_oauth.py) |
| S3·원본·파일 | [저장](../../tests/test_cloud_s3.py)·[큰 응답](../../tests/test_large_receipts.py)·[산출물](../../tests/test_artifacts.py) |
| 설정·프로젝트 | [YAML](../../tests/test_yaml.py)·[카탈로그](../../tests/test_project_catalog.py)·[서버 조립](../../tests/test_server_config.py) |
| 인증·공개 도구 | [JWT](../../tests/test_auth.py)·[localhost MCP](../../tests/test_mcp_server.py)·[서비스 통합](../../tests/test_service_capabilities.py) |
| 배포·플러그인 | [스택과 릴리스 공개](../../tests/test_project_deployment.py)·[Runtime](../../tests/test_runtime_deployment.py)·[입력 오류](../../tests/test_deployment_cli_errors.py)·[플러그인](../../tests/test_plugin_bundle.py) |
| 프로젝트 연결 | [AWS 등록](../../tests/test_connections.py)·[입력창·권한](../../tests/test_connection_service.py)·[공개 MCP](../../tests/test_connection_mcp.py)·[카탈로그 게시](../../tests/test_connection_catalog_cli.py)·[런처](../../tests/test_connector_cli.py) |
| 위임·Git | [위임](../../tests/test_delegation.py)·[실제 Git](../../tests/test_workspaces.py)·[Git Skill 배포](../../tests/test_workspace_deployment.py) |
| Memory 재구성 | [이벤트·분기·재생·격리](../../tests/test_memory_archive.py) |
| 문서 링크·계층 경계 | [문서](../../tests/test_documentation_links.py)·[패키지](../../tests/test_package_boundaries.py) |

전체 pytest는 회사 자격·AWS·운영 리소스 없이 실행한다.
AWS 요청은 native SDK 형식의 모의 연결, MCP는 localhost의 실제 HTTP·SDK client, Git은 임시 로컬 저장소로 검증한다.
프로젝트 스택은 `uvx cfn-lint -r ap-northeast-2 us-east-1 -t deploy/project.yaml`로 AWS 리소스 스키마를 검사한다.

### 실제 환경에서 확인할 사항

| 대상 | 남은 설정·검증 |
|---|---|
| AWS 기반 | 계정·리전·IAM·프로젝트 스택의 첫 배포와 릴리스 교체·S3 버전 관리/수명·ECR/ARM64 이미지·승인/위임/수집 Lambda·Runtime |
| 회사 로그인·호스트 | IdP audience/scope/프로젝트 claim·public client/callback·두 호스트 설치/확인창/업무 이어가기 |
| 업무 도구 | Gateway·Identity·inbound OAuth·모델·중앙 MCP/API·하위 harness·사내망·DNS·CA·대상 권한 |
| 프로젝트 연결 | 검증한 패키지·런처 이미지·SSM/IAM·관리 claim·호스트 입력창·연결별 Runtime·원격 MCP/OpenAPI·도구 발견·실제 리소스 접근 |
| 소스·산출물 | 실행 환경 Git/자격/작업 보존·실제 S3 업로드·두 호스트 다운로드 |
| 공유 기억 | 예약 수집/실패 알림·A→B 재사용·프로젝트 차단·추출 지연/정정·재구성 품질 |
| 운영 품질 | 실제 S3 동시 쓰기/장애 복구·추적·평가·비용·처리 리전 |

[Transaction Search](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)의 계정 설정·보존 기간과 Memory·평가의 [교차 리전 처리](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/cross-region-inference.html)를 회사 정책과 대조한다.
