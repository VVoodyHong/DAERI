# 최초 리소스 MCP 선정과 지원 경계

상태: 연동 대상·기존 MCP 재사용 확정 + 최초 제품 설계 선택·실행 미검증

PostgreSQL·Redis·AWS·GitLab의 선택 근거와 제품별 지원 경계를 정의한다.
탐색·등록·호출의 공통 조건은 [리소스 계약](contract.md)을 따른다.

## 최초 선정

네 연동 대상과 기존 MCP 재사용은 확정 요구이며 아래 제품은 설계상 선정이다.
현재 환경의 접속·호환성·차단은 검증하지 않았다.

| 대상 | 최초 선택·실행 방식 | 이유·관리 주체 |
|---|---|---|
| PostgreSQL | [Bytebase DBHub](https://github.com/bytebase/dbhub), 로컬 stdio | 구조 탐색·범용 SQL과 읽기·행·시간 설정; Bytebase, MIT |
| Redis | [Redis 공식 MCP](https://github.com/redis/mcp-redis), 로컬 stdio | 일반 Redis 연결과 자료형별 연산; Redis, MIT |
| AWS | [관리형 AWS MCP](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/mcp-server.html) + 로컬 [SigV4 프록시](https://github.com/aws/mcp-proxy-for-aws) | 범용 API·작업 추적과 기존 인증 profile 재사용; AWS 서비스, 프록시 Apache-2.0 |
| GitLab | [공식 GitLab MCP](https://docs.gitlab.com/user/model_context_protocol/mcp_server/), 기존 인스턴스 HTTP | 공식 저장소 도구와 사용자 권한; GitLab 제품 라이선스 |

| 대상 | 필요한 경우의 대안 | 적용 조건 |
|---|---|---|
| PostgreSQL | [Postgres MCP Pro](https://github.com/crystaldba/postgres-mcp) | 별도 운영·쿼리 분석이 필요할 때; 일부 분석은 DB 확장 필요 |
| Redis | [Upstash Redis MCP](https://github.com/upstash/redis-mcp) 또는 필요한 redis-py 연산 | 범용 명령·batch·구간 읽기 부족분만 보완 |
| AWS | 필요한 Boto3 연산 | 관리형 MCP가 필수 강제 조건을 충족하지 못할 때 |
| GitLab | [zereight/gitlab-mcp](https://github.com/zereight/gitlab-mcp) | 사내 공식 endpoint·인증·도구 지원이 맞지 않을 때 |

대안으로 자동 전환하지 않는다.
선택 변경은 연결·계약·계획을 갱신하며 필요한 실제 승인을 다시 받는다.
유지보수가 중단된 [과거 MCP 참조 서버](https://github.com/modelcontextprotocol/servers-archived)는 신규 기본값에서 제외한다.

초기는 읽기로 연결·승인 경계를 검증하고 변경 기능은 효과·권한·중복·완료 확인 계약을 갖춰 추가한다.
프로그램·패키지·지원 버전은 [의존성 관리](../installation/environment.md#버전과-재현-가능한-환경)에서 관리한다.

## PostgreSQL

DBHub의 [`search_objects`](https://dbhub.ai/tools/search-objects)로 구조를 탐색하고 [`execute_sql`](https://dbhub.ai/tools/execute-sql)로 범용 SQL을 실행하는 설계다.
특정 호스팅 계정 없이 일반 PostgreSQL DSN으로 연결하며 최초에는 Node.js 기반 로컬 stdio를 사용한다.
DBHub는 PostgreSQL 프로젝트의 공식 제품은 아니다.

기존 [설정](https://dbhub.ai/config/toml)의 `readonly`·`max_rows`·`query_timeout`을 활용한다.
[connector 구현](https://github.com/bytebase/dbhub/blob/main/src/connectors/postgres/index.ts)의 읽기 전용 실행은 `BEGIN READ ONLY`에 연결된다.
DB 최소 권한과 허용 SQL·객체 범위를 함께 적용한다.

`max_rows`는 문장별 SELECT 결과 제한이며 전체 호출의 바이트·검색한 행·DB 작업량 상한이 아니다.
`query_timeout`은 `statement_timeout`에 적용되므로 여러 SQL이 포함된 호출 전체의 시간과 다르다.
연결의 여러 문장·트랜잭션 지원을 검증하고 DAERI 대기 종료만으로 DB 작업 종료를 확정하지 않는다.

[읽기 전용 트랜잭션](https://www.postgresql.org/docs/current/sql-set-transaction.html)은 함수·뷰·확장·외부 테이블의 간접 대상과 부수 효과까지 모두 제거하지 않는다.
기간·컬럼·간접 접근 조건을 판정할 수 없으면 SQL 문자열 검사만으로 강제했다고 선언하지 않는다.
해당 기능에 필요한 조건을 지원하지 못하면 보완하거나 계획을 거부한다.

과거 읽기 전용 우회에 관한 [보안 권고](https://github.com/bytebase/dbhub/security/advisories/GHSA-mwwr-p57h-56pf)가 있다.
선택한 실제 배포물에 보완이 포함되는지 확인해야 하며 보완되지 않은 배포물은 사용하지 않는다.

## Redis

공식 MCP는 로컬 Python 실행으로 Redis TCP/TLS에 연결한다.
대표 기능은 `get`·`hget`·`hgetall`·`lrange`·`type`·`scan_keys` 등이며 실제 배포물의 스키마를 읽어 등록한다.
출처: [공식 README와 도구 소스](https://github.com/redis/mcp-redis).

최초에는 실제 key·자료형·효과를 확인한 읽기 도구부터 연결한다.
[Redis ACL](https://redis.io/docs/latest/operate/oss_and_stack/management/security/acl/)의 명령·키·channel 권한과 DAERI 입력 검사를 함께 적용한다.
키 인수가 없는 전체 DB 명령·SCAN·검색 index·스크립트에는 key prefix 보장을 자동 적용하지 않는다.

[`SCAN COUNT`](https://redis.io/docs/latest/commands/scan/#the-count-option)는 검색량 상한이 아니라 힌트다.
[`scan_keys` 소스](https://github.com/redis/mcp-redis/blob/main/src/tools/misc.py)의 `total_scanned`도 반환 키 수이며 실제 검사한 keyspace 총량이 아니다.
`scan_all_keys`는 내부 반복 요청을 수행하므로 엄격한 검색량·외부 요청 수 제한이 필요한 계획에 그대로 사용하지 않는다.

`get`은 전체 문자열을, `hgetall`·`smembers`는 전체 자료를 읽는다.
표시를 자르는 것은 원격 읽기 바이트 제한이 아니며 구간 읽기가 필수면 필요한 연산만 보완한다.
`type`도 TYPE·TTL을 함께 호출하므로 MCP 한 호출을 Redis 요청 한 번으로 계산하지 않는다.

JSON·검색 도구는 원격 Redis의 대응 기능이 필요하다.
pub/sub·consumer group은 구독·전달 상태에 영향을 줄 수 있어 도구 이름만으로 읽기로 분류하지 않는다.

## AWS

AWS 관리형 MCP는 사용자 PC의 MCP Proxy for AWS를 통한 SigV4 연결을 최초 경로로 선택한다.
프록시는 로컬 stdio와 AWS 원격 endpoint를 연결하며 기존 credential profile을 재사용한다.
요청·스크립트·결과는 AWS 서비스에서 처리되므로 전달 위치를 사용자에게 드러낸다.
출처: [연결·인증 설명](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/getting-started-aws-mcp-server.html).

[현재 공식 도구](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/understanding-mcp-server-tools.html)의 `aws___run_script`는 AWS API 조합을, `aws___get_tasks`는 장기 작업 조회를 제공한다.
문서·리전 도구와 `aws___get_presigned_url`도 별도 효과·자료 접근 계약이 필요하다.
이전 자료의 `aws___call_aws` 존재를 현행 지원으로 추정하지 않는다.

범용 스크립트에는 여러·병렬 API 호출과 재시도가 포함될 수 있다.
실제 주체·account·허용 action·region·자원 범위를 제한하는 IAM과 검증한 실행 계약을 연결한다.
스크립트 문자열에 `Describe`가 있다는 이유만으로 읽기로 허용하지 않는다.

프록시의 `--read-only`는 `readOnlyHint`에 따른 도구 노출 필터다.
스크립트 내부의 변경 거부는 실제 IAM으로 확인하며 구형 `aws-mcp:CallReadOnlyTool` action에 의존하지 않는다.
출처: [프록시 구현](https://github.com/aws/mcp-proxy-for-aws), [AWS IAM 문제 해결](https://docs.aws.amazon.com/agent-toolkit/latest/userguide/security_iam_troubleshoot.html).

연결의 [AWS 인증 고정](../configuration/profiles.md#aws-named-profile과-sso)을 적용한다.
스크립트에도 실제 account·principal·role과 대상 전환 검사를 적용한다.
여러 AWS 인스턴스는 별도 바인딩으로 등록하며 단일 caller account 확인이 교차 계정 자료 접근까지 막는다고 보지 않는다.

기본 리전이나 MCP endpoint는 호출 리전의 강제 제한이 아니다.
[`aws:RequestedRegion`](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_policies_condition-keys.html#condition-keys-requestedregion)은 endpoint를 제한하며 모든 영향 리전까지 보장하지 않는다.
허용 API의 실제 리전·자원 의미를 확인한다.

예를 들어 EC2 [`DescribeInstances`](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_DescribeInstances.html)는 [권한 표](https://docs.aws.amazon.com/service-authorization/latest/reference/list_ec2.html)상 resource ARN 제한을 지원하지 않는다.
따라서 첫 검증 범위는 승인된 계정·리전 inventory 조회다.
특정 ID·ARN 밖 자료를 검색조차 하지 않아야 한다면 별도의 인수 강제 없이 이 범용 계약으로 실행하지 않는다.

범용 스크립트의 내부 요청을 통제하지 못하면 [외부 요청 상한](contract.md#호출과-결과)을 지원하지 못한다.
DAERI의 대기 제한은 원격 스크립트 중단 보장이 아니므로 단절·시간 초과 뒤 task ID와 외부 효과를 확인해야 한다.

AWS Labs의 기존 [AWS API MCP](https://github.com/awslabs/mcp/tree/main/src/aws-api-mcp-server)는 관리형 대체 안내가 있어 신규 기본값에서 제외한다.
필요한 Boto3 보완도 내부 재시도·페이지별 예산·실제 권한 검사를 갖춰야 한다.

## GitLab

공식 서버의 `https://<승인된 origin>/api/v4/mcp`에 HTTP로 연결한다.
GitLab.com 또는 기존 사내 GitLab이 서버를 운영하며 DAERI 개발자의 별도 서버는 필요하지 않다.
사내 인스턴스의 [MCP 접근 설정](https://docs.gitlab.com/administration/settings/visibility_and_access_controls/#allow-access-to-the-mcp-server)·사용자 권한·실제 도구 지원을 확인한다.

공식 MCP의 [OAuth·fine-grained PAT 조건](../configuration/profiles.md#gitlab-oauth와-fine-grained-pat)을 적용한다.
기존 사내 Legacy PAT의 재사용을 전제하지 않는다.

[공식 저장소 도구](https://docs.gitlab.com/user/model_context_protocol/mcp_server_tools/)는 `get_project`·`get_commit`·`list_repository_tree`·`get_repository_file`을 제공한다.
승인된 탐색에서 ref를 commit SHA로 고정하고 후속 tree·file에 같은 SHA를 사용한다.
origin·project·path를 검사하고 `HEAD`나 기본 branch로 자동 되돌리지 않는다.

파일 도구는 텍스트를 제공하며 binary·LFS·context에서 제외된 파일은 읽지 못한다.
잘림·읽지 않은 경로·파일 구간·페이지를 확보 범위에 반영한다.

공식 `search`에는 ref 인수가 없고 검색 scope 지원도 [tier·인덱스 설정](https://docs.gitlab.com/api/search/)에 영향을 받는다.
최초 snapshot 검색은 같은 SHA의 승인 tree·텍스트 파일을 확보한 뒤 그 자료 안에서 검색하는 조합이다.
원격 search를 특정 SHA의 전체 코드 검색으로 보고하지 않는다.

## 실행 공개 전 확인

실제 배포물·권한에서 위 제품별 경계와 [공통 등록 조건](contract.md#도구-등록)을 확인한 기능만 공개한다.
허용·거부·복구와 OS·호스트의 완료 기준은 [최초 구현 계획](../../plans/initial-implementation.md)을 따른다.

현재 근거는 제공자 문서·저장소 조사다.
실제 설치·인증·연결·차단은 하지 않았으며 최종 정책과 지원 버전은 구현 검증에서 정한다.
