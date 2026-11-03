# DAERI 개발·운영 안내

상태: AWS 공용 서비스의 구현·설정·배포·API·검증 기준.
사용자 동작과 AWS·DAERI의 역할은 [서비스 설계](service.md), 작업 방법은 [작업 규칙](rules/work.md)을 따른다.

[개발 환경](#개발-환경) → [코드 구조](#코드-구조) → [설정](#설정) → [배포와 운영](#배포와-운영) 순서로 시작한다.
세부 동작은 [API와 저장 계약](#api와-저장-계약), 확인 근거와 남은 연결은 [구현과 검증](#구현과-검증)에서 찾는다.

## 개발 환경

지원 Python·의존성 버전은 [pyproject.toml](../pyproject.toml)·[uv.lock](../uv.lock)이 소유한다.
`main`은 AWS 공용 서비스, 기존 PC 제품은 [local-agent 브랜치](https://github.com/VVoodyHong/DAERI/tree/local-agent)의 코드·설치를 사용한다.

```sh
uv sync --frozen
uv lock --check
uv run --no-sync pytest
uv build
```

AWS 연결은 boto3, HTTP MCP·확인창은 mcp, 서명·JWKS는 PyJWT·cryptography를 사용한다.
httpx·uvicorn은 HTTP·ASGI, pydantic·PyYAML은 설정, pytest는 검증, Hatchling·uv는 빌드·잠금을 담당한다.
설정 읽기는 파일·스키마를 검사하고 클라이언트·자격 연결은 명시적인 조립·배포 실행에서 시작한다.

## 코드 구조

핵심 계약 `core` ← 흐름 `application` ← 외부 연결 `infrastructure`를 `config`에서 조립한다.
SDK 객체·오류는 외부 어댑터에서 변환하며 내부 흐름은 저장·프로젝트·harness 포트에 의존한다.

| 위치 | 책임 |
|---|---|
| [core/cloud.py](../src/daeri/core/cloud.py)·[approval.py](../src/daeri/core/approval.py)·[access.py](../src/daeri/core/access.py) | 업무·현재 계획·결정·인증한 구성원과 저장·harness 포트 |
| [core/projects.py](../src/daeri/core/projects.py)·[errors.py](../src/daeri/core/errors.py)·[serialization.py](../src/daeri/core/serialization.py) | 프로젝트·릴리스·조회 포트, 경계 오류·유한 UTF-8 JSON |
| [application](../src/daeri/application/) | 내부 업무 호출, 공개 권한·목록·계획 확인·복구 |
| [project_catalog.py](../src/daeri/infrastructure/project_catalog.py)·[cloud_s3.py](../src/daeri/infrastructure/cloud_s3.py) | SSM 조회, S3 조건부 상태·불변 호출 원본·복구 |
| [agentcore.py](../src/daeri/infrastructure/agentcore.py)·[harness_oauth.py](../src/daeri/infrastructure/harness_oauth.py) | native 호출·스트림 해석, 요청 범위 Bearer 인증 |
| [auth.py](../src/daeri/infrastructure/auth.py)·[mcp_server.py](../src/daeri/infrastructure/mcp_server.py)·[artifacts.py](../src/daeri/infrastructure/artifacts.py) | OIDC·HTTP MCP·확인창·업무별 파일과 임시 링크 |
| [approval_hook.py](../src/daeri/infrastructure/approval_hook.py)·[delegation.py](../src/daeri/infrastructure/delegation.py)·[memory_archive.py](../src/daeri/infrastructure/memory_archive.py) | 승인·위임·Memory 수집 Lambda와 설정·복원 |
| [workspaces.py](../src/daeri/infrastructure/workspaces.py)·[workspace_cli.py](../src/daeri/infrastructure/workspace_cli.py) | 업무별 Git 사본·통합 검사와 실행 명령 |
| [project_deployment.py](../src/daeri/infrastructure/project_deployment.py)·[runtime_deployment.py](../src/daeri/infrastructure/runtime_deployment.py)·[workspace_deployment.py](../src/daeri/infrastructure/workspace_deployment.py) | Memory·릴리스·SSM, MCP Runtime, Git Skill 배포 |
| [config](../src/daeri/config/)·[서버 진입점](../src/daeri/__main__.py)·[scripts](../scripts/) | YAML·AWS·인증 조립·종료, 서버·배포물 생성 |

## 설정

`CloudSettings.from_yaml(path)`는 내부 서비스, `ServerSettings.from_yaml(path)`는 공개 MCP 서버의 UTF-8 YAML을 읽는다.
허용 입력은 최상위 매핑·유한한 JSON 호환 값·2MiB 이내·중첩 32단계다.
중복 키·명시적 태그·앵커·별칭·병합·지시문·알 수 없는 필드는 원문을 포함하지 않는 `INVALID_INPUT`으로 처리한다.

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
| `auth.required_scopes` | 필수 OAuth scope 목록; 기본 `[daeri/use]` |
| `approval_hook_arn` | 서버 필수 승인 검사 Lambda ARN |
| `harness_oauth_audience` | 선택 inbound OAuth audience; 생략 시 IAM; 지정 시 `auth.audience`와 동일 |

고정된 issuer의 discovery·JWKS로 RS256 서명과 `iss`·`aud`·`sub`·`exp`·`scope`·프로젝트 claim을 검사한다.
프로젝트 claim은 중복 없는 ID 배열, `sub`는 실제 사용자이며 권한 유효성은 서명된 토큰의 만료 시각을 기준으로 판단한다.

`storage.prefix`는 영문·숫자·`_`·`.`·`-`·`/`로 구성하고 생략한 끝 `/`는 추가한다.
빈 구간·중복 `/`·`.`·`..` 구간은 입력 오류로 처리한다.
다음은 실제 자격을 포함하지 않는 가상 서버 설정이다.

```yaml
region: us-east-1
public_url: https://daeri.example.com/mcp
auth:
  issuer: https://login.example.com/company
  audience: https://daeri.example.com/mcp
approval_hook_arn: arn:aws:lambda:us-east-1:123456789012:function:daeri-approval
storage:
  bucket: daeri-example
```

### 프로젝트 설정

SSM의 `<project_parameter_prefix><project_id>`에 프로젝트별 JSON을 등록한다.
권한은 인증 서버가, 표시 이름·실행 대상은 다음 네 필드가 정한다.

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

인프라 생성·변경은 클라우드서비스팀의 승인된 배포 체계에서 진행한다.
아래 명령은 가상 설정 파일·주소를 사용하는 실행 진입점이며 실제 자격·계정·리전·역할은 배포 환경에서 지정한다.

### 서버와 플러그인

1. 모델·도구·승인 hook·`confirm_plan`을 갖춘 managed harness, 서비스 역할·S3·회사 IdP를 준비한다.
2. `uv run daeri --config server.yaml --host 0.0.0.0 --port 8000`으로 ASGI 서버를 시작한다.
3. 운영 환경은 AgentCore Runtime의 MCP 프로토콜로 배포하고 내부 서버는 `0.0.0.0:8000/mcp`를 제공한다.
4. 공개 endpoint로 플러그인 ZIP을 생성해 배포 저장소에 게시한다.

```sh
uv run python scripts/build_plugin.py --endpoint https://daeri.example.com/mcp --client-id company-public-client --callback-port 59383 --output /tmp/daeri-plugin.zip
```

공유 Skill·MCP 설정과 `.claude-plugin`·`.codex-plugin` manifest를 묶고 호스트별 marketplace를 생성한다.
사용자는 [Claude](https://code.claude.com/docs/en/plugins/install)·[Codex](https://developers.openai.com/plugins/build/plugins)의 설치 경로를 사용한다.
IdP에는 public client·실제 callback·audience·scope·프로젝트 claim을 등록하고, [Codex callback](https://learn.chatgpt.com/docs/extend/mcp)의 issuer·URL별 차이를 대조한다.
서버는 기존 IdP의 발급·로그인을 사용하며 Cognito도 같은 access token 조건을 충족하게 구성한다.
HTTP 서버는 [공식 MCP SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.30.0)의 stateful Streamable HTTP·인증 middleware를 사용한다.
Runtime은 JWT 진입 검사·세션 라우팅을, 서버는 프로젝트·업무·확인 응답의 권한 검사를 담당한다.

승인 Lambda handler는 `daeri.infrastructure.approval_hook.lambda_handler`다.
`DAERI_STORAGE_BUCKET`·선택 `DAERI_STORAGE_PREFIX=daeri/`를 서버와 맞추고 harness의 `beforeToolCall`에 ARN·`failureMode: deny`를 등록한다.
inline `confirm_plan`은 필수 문자열 `plan`을 받는 객체이며 system prompt는 [계획·승인 절차](service.md#계획과-승인)를 정의한다.
서비스 역할은 호출·업무·세션·결정을 작성하고 hook 역할은 연결·현재 결정 조회와 승인 해제를 수행한다.
업무·승인·세션 저장 쓰기는 서비스·hook의 담당 경로에, 위임 Lambda 호출은 해당 프로젝트 Gateway 역할에 연결한다.

### 프로젝트와 Runtime

| 배포 도구 | 필수 입력 | 선택·기본값 |
|---|---|---|
| [프로젝트](../src/daeri/config/project.py) | `project_id`·`name`·기존 `harness_arn`·`approval_hook_arn` | `region`·`project_parameter_prefix`·`memory_id`·`memory_execution_role_arn`·`encryption_key_arn` |
| [Runtime](../src/daeri/config/runtime.py) | `name`·`image_uri`·`role_arn`·`client_ids`·`server` 매핑 | 갱신할 `runtime_id`·`endpoint=PROD`·`network_configuration`의 기본 PUBLIC 또는 native VPC 설정 |

Runtime의 `server`는 자동 생성하는 `public_url`을 제외한 서버 필드다.
기억 전략·보존 조건은 [공유 기억](service.md#공유-기억), 모델·도구·Gateway·Identity는 해당 AWS 리소스에서 설정한다.
생성 모델·도구는 `CreateHarness`·`UpdateHarness`로 구성하고 프로젝트 배포는 이를 유지하며 Memory 연결·릴리스를 공개한다.
외부 모델의 `apiKeyArn`은 AgentCore Identity API key credential provider ARN을 참조한다.

```sh
uv run python scripts/configure_project.py --config project.yaml
uv run python scripts/configure_project.py --config project.yaml --apply
docker buildx build --platform linux/arm64 -t 123456789012.dkr.ecr.us-east-1.amazonaws.com/daeri:example --push .
uv run python scripts/configure_runtime.py --config runtime.yaml
uv run python scripts/configure_runtime.py --config runtime.yaml --apply
```

프로젝트 기본 실행은 게시된 리소스의 읽기 전용 검사이고 Runtime 기본 실행은 요청 구성 검사다.
`--apply`는 프로젝트의 독립 Memory·릴리스·SSM 또는 Runtime의 컨테이너·MCP·JWT authorizer·Authorization 전달·endpoint를 생성·갱신한다.
프로젝트는 카탈로그 경로·ID로 Memory 이름을 정해 소유 태그·활성 전략·검색·보존을 검사하고, 연결한 `RELEASE_V<버전>`을 검증한 뒤 SSM을 마지막에 공개한다.
기존 Memory·릴리스의 조건 불일치는 `PROJECT_NOT_READY`이며 다른 BYO Memory로 전환할 때는 이관·복원 검증을 거쳐 `memory_id`를 지정한다.
이전 릴리스·독립 Memory·원본은 수명을 유지하고 기존 업무는 저장한 릴리스로 이어간다.
운영자는 프로젝트·Runtime별 배포를 순차 실행하고 endpoint 변경 권한을 관리한다.

Runtime의 공개 주소를 서비스 설정에 연결하고 준비된 버전의 endpoint를 공개한 뒤 반환한 `public_url`로 플러그인을 생성한다.
서비스 코드의 Runtime endpoint와 개별 업무의 harness 릴리스 endpoint는 각각의 배포 단위다.
모델·리전·사내 API·네트워크·CA·자격의 실제 호환성은 배포 검증에서 확인한다.

### 하위 에이전트와 개인 위임

[하위 설정](../src/daeri/config/delegation.py)은 `project_id`·`gateway_arn`·`lambda_arn`과 역할별 `description`·`harness_arn`·`harness_endpoint`·`harness_version`을 받는다.
`target_name`은 기본 `subagents`이며 handler는 `daeri.infrastructure.delegation.lambda_handler`다.
하위 harness는 공개한 프로젝트 Memory·서비스 자격·서버 실행 도구를 사용하며, timeout은 위임 Lambda 안에 끝나게 구성한다.
등록은 고정된 대상 설정을 Lambda 환경에 연결하고 Gateway 도구·총괄 harness·프로젝트 릴리스를 갱신한다.

```sh
uv run python scripts/configure_subagents.py --config subagents.yaml --project-config project.yaml --apply
```

`--apply`를 생략하면 Gateway 요청 구성을 검사한다.
개인 위임은 서버와 harness가 같은 `harness_oauth_audience`를 허용하도록 구성하고 Identity의 제공자·사용자 동의·scope를 연결한다.
검증한 실제 사용자와 `actorId`·`runtimeUserId`를 대조하고 전송 전 만료는 `AUTHENTICATION_REQUIRED`로 처리한다.
S3·SSM·제어 API는 서비스 IAM, 하위 위임 Lambda는 프로젝트 서비스 자격을 사용한다.

### Memory 수집과 복원

[수집 설정](../src/daeri/config/memory.py)은 서비스 리전·S3·카탈로그 경로에 `project_id`·`source_memory_id`를 추가한다.
실제 Memory ID는 네이티브 이벤트, 기본 `receipts`는 S3 업무 호출 원본을 수집한다.

```sh
uv run python scripts/archive_memory.py --config memory.yaml
uv run python scripts/archive_memory.py --config memory.yaml --apply
uv run python scripts/archive_memory.py --config memory.yaml --restore-to Restored_Example-0123456789 --apply
```

기본 실행은 읽기 전용 검사이며 `--apply`가 불변 원본·진행 기록 또는 별도 대상 이벤트를 작성한다.
예약 handler는 `daeri.infrastructure.memory_archive.lambda_handler`, `DAERI_MEMORY_ARCHIVE_CONFIG`는 같은 설정의 JSON이다.
한 호출은 최대 50페이지·약 45초 범위를 처리하고 S3 진행 상태로 이어가며 전체 순회 뒤 새 수집 주기를 시작한다.
EventBridge Scheduler·권한·실패 알림을 연결하고 원본 이벤트의 보존 기간 안에 수집을 완료한다.
동일 프로젝트 소유의 비어 있는 독립 Memory에 재생하고 비동기 추출·검색을 검증한 뒤 프로젝트의 `memory_id`를 지정해 공개한다.

### Git 작업 환경

[Git Skill](../src/daeri/assets/source-workspace/SKILL.md)은 기본 shell에서 표준 라이브러리만 사용하는 `workspace.pyz`를 실행한다.
다음 가상 `workspaces.yaml`은 실행 환경에 보이는 경로 또는 원격 Git 주소를 여러 개 등록한다.

```yaml
root: /mnt/work/source
repositories:
  api: https://git.example.com/project/api.git
  web: https://git.example.com/project/web.git
```

```sh
uv run python scripts/configure_workspaces.py --project-config project.yaml --config workspaces.yaml --skill-uri s3://daeri-example/skills/ --apply
```

기본 실행은 배포 파일 구성을 검사하며 `--apply`는 내용 해시별 S3 Skill·실행 묶음·설정을 게시하고 세션 저장소·harness·SSM을 갱신한다.
`root`는 `/mnt` 아래에 두고 Git·네트워크·표준 자격은 프로젝트 실행 환경에서 준비한다.
설치된 개발 환경에서는 생성한 JSON 설정으로 `daeri-workspace --config workspaces.json prepare TASK_UUID REPOSITORY`를 사용한다.
파일은 해당 세션의 작업 저장소에, 원격 인계는 push한 업무 브랜치에 연결하며 재개·통합 조건은 [소스와 산출물](service.md#소스와-산출물)을 따른다.
`prepare`는 기준 ref를 fetch해 commit과 `daeri/<task UUID>/<repository>` 브랜치·사본을 고정하며 재호출은 저장한 기준을 유지한다.
새 환경의 재개는 선택한 기준 ref와 원격 업무 브랜치의 공통 조상을 기록하고, `prepare`·`verify`의 `--reference` 기본값은 `HEAD`다.

## API와 저장 계약

### 공개 MCP 도구

모든 도구는 현재 요청에서 검증한 사용자·프로젝트로 `AgentService`를 호출한다.
호스트 선택값은 호출의 `project_id`·`task_id`로 전달하며 목록·상태 조회와 실행·복구를 구분한다.

| 도구 | 입력·결과 |
|---|---|
| `daeri_projects` | 허용된 프로젝트 목록 |
| `daeri_tasks` | `project_id`, 선택 `cursor`·`limit=20`; 자신의 업무·다음 커서 |
| `daeri_work` | `project_id`·`request`, 선택 `task_id`; 새 업무·후속 요청·피드백 |
| `daeri_status` | `project_id`·`task_id`; 목표·진행·계획·확인 상태·마지막 보관 응답 |
| `daeri_confirm` | `project_id`·`task_id`; 저장한 계획의 호스트 확인창 |
| `daeri_recover` | `project_id`·`task_id`, 선택 `restart=false`; 보관 응답 반영 또는 확인 후 새 세션 |
| `daeri_files` | `project_id`·`task_id`, 선택 `cursor`·`limit=20`; 실제 S3 산출물·다음 커서 |
| `daeri_download` | `project_id`·`task_id`·`artifact_id`; 선택 버전의 임시 다운로드 링크 |

업무·파일 페이지 크기는 1~100이며 커서는 사용자·프로젝트·페이지 크기에, 파일은 업무에도 연결한다.
확인창의 결정·대기·취소·만료 처리는 [계획과 승인](service.md#계획과-승인), 세션 전환은 [실패와 복구](service.md#실패와-복구)를 따른다.

### 내부 업무 호출

`build_cloud()`는 `store`·`workflow`·클라이언트의 `CloudServices`, `build_server()`는 verifier·실행 대상 검사·`AgentService`·HTTP MCP를 조립한다.
호출자는 `close()` 또는 context manager로 자원을 종료한다.
OIDC discovery·JWKS는 토큰 검증 때 조회한다.

| `CloudWorkflow` 호출 | 입력·수행 조건 |
|---|---|
| `list_projects(contexts)` | 동일 사용자의 프로젝트별 `AccessContext` 목록·튜플 → `id`·`name`; 사용자 혼합·프로젝트 중복은 `INVALID_INPUT` |
| `start(context, prompt)` | 비어 있지 않은 요청 → 업무·세션·검사한 harness ARN/endpoint/버전 저장 |
| `get_task(context, task_id)` | 현재 카탈로그·작성 사용자·프로젝트와 저장한 문맥 대조 |
| `resume(context, task_id, prompt='', tool_results=(), expected_etag=None)` | 저장한 대상·세션, 선택 ETag 대조, 후속 요청 또는 실제 대기 응답 |
| `recover(context, task_id)` | 이미 저장한 응답을 상태에 반영 |
| `restart(context, task_id, review, expected_etag=...)` | 실제 확인 내용·현재 ETag → 이전 상태 보존·새 세션·새 계획 |

`AccessContext`는 인증 경계가 제공한 `actor_id`·`project_id`이며 저장한 문맥 불일치는 `ACCESS_DENIED`다.
중앙 배포가 바뀌어도 기존 업무는 저장한 릴리스를 사용한다.
버전이 없는 기존 업무는 상태·원본을 확인해 현재 프로젝트의 새 계획으로 시작한다.
원격 확인은 native handoff의 첫 `confirm_plan` 대기 하나를 처리하고 다음 도구는 재개한 harness가 선택한다.

### 입력과 응답 보관

요청·도구 응답의 문자열과 JSON 키는 유효한 UTF-8, 응답은 유한한 JSON 직렬화 값으로 받는다.
정상 한글·이모지와 JSON 이스케이프로 보존 가능한 NUL 문자는 허용한다.
위반은 원문을 포함하지 않는 `INVALID_INPUT`으로 업무 생성·harness 호출 전에 중단하고 기존 대기를 유지한다.

`tool_results`는 저장한 `toolUseId`마다 한 번씩 정확히 대응하고 대기 순서로 전달한다.
상태는 `success`·`error`, 본문은 text·JSON 블록이며 대기가 없으면 비어 있지 않은 후속 요청을 받는다.
S3 업무 상태·호출 원본 JSON은 객체당 8MiB 이내로 받고 산출물 본문은 별도 파일로 보관한다.
응답 원본을 먼저 보존한 뒤 manifest를 조건부 갱신하며 내부 `StoreError`는 확보한 `task_id`·`turn_id`로 복구 대상을 연결한다.

### 오류와 복구

| 조건 | 결과·다음 조치 |
|---|---|
| 동일 업무의 실행 진행 중 | 조회 `running`, 추가 실행 `TASK_BUSY` |
| 로그인 만료·현재 문맥 또는 프로젝트 권한 불일치 | `AUTHENTICATION_REQUIRED` 또는 `ACCESS_DENIED`; 현재 인증 확인 |
| 현재 확인할 계획 없음·승인 설정 미비 | `PLAN_UNAVAILABLE`·`HARNESS_NOT_READY` |
| 입력·도구 대기 대응 오류 | `INVALID_INPUT`; 현재 대기 확인 |
| 미등록 프로젝트 | `PROJECT_UNAVAILABLE` |
| 파일 버전 관리 없음·객체 조회 실패 | `ARTIFACT_UNAVAILABLE`; 현재 업무의 파일·버전 확인 |
| Git 업무 브랜치의 로컬·원격 이력 분기 | `SOURCE_WORKSPACE_CONFLICT`; 양쪽 commit 비교·통합 |
| 저장 객체 변경·원본 불일치 | `STORAGE_CONFLICT`; 현재 상태 확인 |
| 저장 접근 실패·손상된 자료 | `STORAGE_UNAVAILABLE`; 보관 상태 확인 |
| 응답 원본이 없는 진행 업무 | `EXECUTION_UNCERTAIN`; 실제 세션·외부 효과 확인 |
| SDK 권한·대상·호출 제한·입력·할당량 오류 | `HARNESS_ACCESS_DENIED`·`HARNESS_NOT_FOUND`·`HARNESS_THROTTLED`·`HARNESS_VALIDATION_ERROR`·`HARNESS_QUOTA_EXCEEDED` |
| 그 외 호출 실패·불완전 스트림 | `HARNESS_UNAVAILABLE`·`HARNESS_STREAM_INCOMPLETE`; 확보한 응답·오류 보존 |
| Guardrails 개입 종료 | `HARNESS_GUARDRAIL_INTERVENED`·확보한 텍스트 보존 |
| 본문·대기 도구·관찰의 저장 조건 위반 | `HARNESS_INVALID_OUTPUT`; 대기 비움·유효한 관찰과 원래 사유 보존 |

HTTP의 잘못된 bearer 토큰은 401, 필수 scope 부족은 403이다.
SDK의 다섯 명시 오류나 전송 전 위임 자격 만료로 스트림 수신 전에 거절되면 기존 대기를 유지하고 재개 때 같은 실제 응답을 다시 제출한다.
대기 ID 중복은 불완전 스트림으로 기록하며 잘못된 본문 유니코드는 눈에 보이는 이스케이프로 보존한다.
S3 상태 확정 실패는 보관 응답으로 복구하고, 오류 응답이 보존된 업무는 중단 상태·현재 대기를 확인해 재개한다.
`end_turn`은 한 응답의 종료이며 업무 전체의 완료는 목표·실제 결과 근거로 판정한다.

## 구현과 검증

원격 MCP·두 호스트 플러그인 배포물·프로젝트 배포·업무/승인/원본·개인 OAuth 전달·파일·Git·위임·Memory 복원 코드가 구현돼 있다.
검증은 관찰 가능한 동작·실패 경계·native SDK 요청 형식을 대상으로 한다.

| 대상 | 대표 검증 |
|---|---|
| 프로젝트 JSON·SSM | [프로젝트 카탈로그](../tests/test_project_catalog.py) |
| 허용 목록·업무·재개·승인 | [업무 흐름](../tests/test_cloud_workflow.py)·[사용자 서비스](../tests/test_agent_service.py) |
| SDK·스트림·출력·대기 | [harness](../tests/test_agentcore_harness.py) |
| S3 충돌·원본 우선·장애 복구 | [저장 계약](../tests/test_cloud_s3.py) |
| YAML·조립·실행 명령 | [YAML](../tests/test_yaml.py)·[클라우드](../tests/test_cloud_config.py)·[서버](../tests/test_server_config.py)·[진입점](../tests/test_server_entrypoint.py) |
| 토큰·HTTP·확인창 | [인증](../tests/test_auth.py)·[실제 localhost MCP](../tests/test_mcp_server.py) |
| 승인 hook·플러그인·프로젝트/Runtime 공개 | [hook](../tests/test_approval_hook.py)·[배포물](../tests/test_plugin_bundle.py)·[프로젝트](../tests/test_project_deployment.py)·[Runtime](../tests/test_runtime_deployment.py) |
| Gateway 위임·Git 작업/배포 | [위임](../tests/test_delegation.py)·[실제 Git](../tests/test_workspaces.py)·[Git Skill](../tests/test_workspace_deployment.py) |
| 개인 위임·산출물·공개 도구 | [OAuth](../tests/test_harness_oauth.py)·[파일](../tests/test_artifacts.py)·[서비스 통합](../tests/test_service_capabilities.py) |
| 새 세션·취소·동시 변경 | [복구](../tests/test_task_restart.py)·[HTTP 확인창](../tests/test_task_restart_http.py) |
| 이벤트·분기·재생·격리 | [기억 재구성](../tests/test_memory_archive.py) |
| 문서 링크·계층 경계 | [문서](../tests/test_documentation_links.py)·[패키지](../tests/test_package_boundaries.py) |

전체 pytest는 회사 자격·AWS·운영 리소스 없이 실행한다.
MCP는 localhost의 실제 HTTP·SDK client로, Git은 임시 로컬 저장소로 검증하며 AWS 요청·응답은 모의 연결이다.

### 실제 환경에서 확인할 사항

| 대상 | 남은 설정·검증 |
|---|---|
| AWS 기반 | 계정·리전·IAM·S3 버전 관리/수명·ECR/ARM64 이미지·승인/위임/수집 Lambda·Runtime |
| 회사 로그인·호스트 | IdP audience/scope/프로젝트 claim·public client/callback·두 호스트 설치/확인창/업무 이어가기 |
| 업무 도구 | Gateway·Identity·inbound OAuth·모델·중앙 MCP/API·하위 harness·사내망·DNS·CA·대상 권한 |
| 소스·산출물 | 실행 환경 Git/자격/작업 보존·실제 S3 업로드·두 호스트 다운로드 |
| 공유 기억 | 예약 수집/실패 알림·A→B 재사용·프로젝트 차단·추출 지연/정정·재구성 품질 |
| 운영 품질 | 실제 S3 동시 쓰기/장애 복구·추적·평가·비용·처리 리전 |

[Transaction Search](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-operations.html)의 계정 설정·보존 기간을 확인한다.
Memory·평가의 [교차 리전 처리](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/cross-region-inference.html)를 회사 정책과 대조하며 서울 Evaluations는 글로벌 교차 리전 추론을 사용한다.
[Lambda 코드 평가기](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/code-based-evaluators.html)는 처리 위치·외부 API 조건에 따른 평가 후보다.
