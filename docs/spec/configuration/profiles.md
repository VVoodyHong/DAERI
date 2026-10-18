# 서비스 profile·연결·인증 관리

상태: 서비스별 profile·사용자 로컬 인증 요구 확정 + 구성·보존·갱신 설계 제안

서비스별 리소스 묶음, MCP 연결, 업무 스냅샷과 인증·저장 위치를 정의한다.
도구 등록·호출은 [리소스 계약](../resources/contract.md), 공개 profile 필드는 [업무 MCP](../mcp/tasks.md)가 소유한다.

## profile의 의미

profile은 서비스·환경의 업무 맥락으로 여러 DB·Redis·AWS 계정·리전·GitLab 프로젝트와 운영 정보를 묶는다.

| 구분 | 가상 예시 | 책임 |
|---|---|---|
| DAERI profile | `commerce-prod` | 서비스·환경과 리소스 묶음 |
| 리소스 인스턴스 | `orders-db`, `catalog-db` | 실제 대상과 역할 |
| MCP 연결 | `backend-orders-postgres` | 서버·전송·인증 설정 |
| 제공자 인증 profile | AWS의 `commerce-operations` | 제공자의 로그인·자격 증명 |

DAERI profile과 제공자 profile은 별개이며 하나의 DAERI profile이 서로 다른 이름의 여러 제공자 profile을 참조할 수 있다.
에이전트는 리소스 설명·계약을 읽고 이번 업무에 필요한 대상을 선택한다.

## 구성과 연결

profile에는 안정적인 `profile_id`, 별칭·구성 `revision`, 부처·서비스·환경 식별자·설명, 리소스 바인딩·정책 참조를 둔다.
ID는 이름 변경에도 유지하고 다른 실제 대상에 재사용하지 않는다.

`content_retention_days`는 조회·보고 본문·DAERI 산출물의 공통 보존기간인 양의 정수다.
생략 시 [기본 보존기간](../tasks/workflow.md#이력과-완료-근거)을 적용하고 업무 스냅샷에 고정한다.
관리 경로의 변경은 새 업무에만 적용하며 기존 기한·만료 자료를 연장·복원하지 않는다.

리소스 바인딩은 인스턴스 ID·역할·연결·실제 대상·정책을 연결한다.
MCP 연결은 선정 제품·전송·인증 참조·실행 환경을 지정한다.

| 연결 방식 | 설정과 실행 조건 |
|---|---|
| stdio | 검증한 실행 파일의 절대 경로·고정 인수·작업 위치 |
| HTTP | 등록한 origin·endpoint와 TLS 조건 |
| 인증 | 로컬 참조로 해소; 비밀값은 설정에 넣지 않음 |
| 대상 | DB·Redis DB·AWS 계정과 리전·GitLab origin과 프로젝트 명시 |

실제 대상·권한과 도구의 대상 전환을 검사하며 강제할 수 없는 조건을 지원으로 표시하지 않는다.
모델 명령·주소·인증 profile로 관리 구성을 바꾸는 공개 업무 도구를 두지 않는다.

같은 연결을 여러 profile에서 쓰려면 각각 명시적으로 바인딩한다.
인증·대상·정책이 다른 연결은 같은 이름이어도 세션을 공유하지 않는다.
등록하지 않은 연결은 해당 업무의 후보에 나타나지 않는다.

stdio 자식에는 필요한 환경만 전달하며 주변 호스트의 다른 인증·DB 주소를 자동 상속하지 않는다.

## 관리 등록과 진단

설치·로그인·원격 진단은 업무용 MCP와 구분한 명시적 설정 작업에서 수행한다.

1. 서비스·환경·MCP 제품·대상과 인증 참조를 입력한다.
2. 필요한 프로그램·인증 방식·진단 요청과 전달 위치·효과·한도를 보여 준다.
3. 확인한 범위에서 [환경 준비](../installation/environment.md), 사용자 로그인과 제한된 진단을 수행한다.
4. 실제 주체·대상·도구 계약을 확인하고 profile의 새 개정을 보존한다.
5. 준비된 연결·보류 기능·부족한 사용자 조치를 보고한다.

진단의 접근 로그·요금을 안내하고 DB 업무 데이터·Redis 값·소스 원문은 임의로 읽지 않는다.
연결별 프로그램 누락·로그인 필요·재탐색·정책 차단·네트워크 실패를 구분하며 진단 성공을 업무 승인으로 사용하지 않는다.

업무 접수·로컬 등록 조회가 숨은 설치·로그인·원격 탐색을 시작하지 않는다.
원격 구조가 필요하면 제한된 탐색 계획을 승인받는다.
등록과 현재 계약 검사는 [리소스 계약](../resources/contract.md#등록부와-계약-변경)을 따른다.

## 선택과 기본값

새 업무의 profile은 명시 선택 또는 검증한 사용자 기본값으로 해소한다.
기본값은 이후 새 업무에만 적용하며 없거나 접근할 수 없으면 `awaiting_input`으로 확인한다.

부처·환경을 등록 맥락과 대조하며 비슷한 설명·접속 가능 여부로 임의 선택하거나 다른 profile과 자동 합병하지 않는다.

초기에는 **업무당 하나의 DAERI profile**을 적용한다.
한 profile의 리소스·제품·계정 수를 하나로 제한하는 조건은 아니다.
다른 profile의 리소스가 필요하면 명시적인 묶음을 구성하고 새 업무로 접수한다.

## 업무에 고정하는 스냅샷

접수 시 profile 개정과 리소스·연결·대상·인증 문맥·정책을 불변 스냅샷으로 보존한다.
비밀값·로그인 토큰은 넣지 않고 계획은 이 안의 등록 인스턴스·기능을 선택한다.

첫 승인 이후 스냅샷은 교체하지 않고 그 안의 방법·범위 변경은 [수정 계획](../tasks/approval.md)으로 처리한다.

| 변경 | 기존 업무 처리 |
|---|---|
| 승인·외부 호출 전 맥락 보완 | 현재 개정으로 해소하고 미승인 계획·확인 요청 대체 |
| 승인 또는 외부 호출 이력 이후 스냅샷 변경 | 거부; 새 업무 접수 |
| 기본 profile 변경 | 새 업무에만 적용 |
| 도구 의미·스키마 변경 | 영향 기능 차단; 새 계약의 수정 계획 승인 |
| 대상·주체·권한 바인딩 변경 | 영향 호출 차단; 관리 검증 후 새 업무 |
| 설명 변경·같은 주체의 비밀 갱신 | 대상·권한·계약이 같음을 확인한 뒤 유지 |

재개는 저장한 스냅샷과 현재 권한을 함께 검사하며 기본값·같은 이름의 profile·다른 계정으로 대체하지 않는다.
비활성화·접근 철회·인증 실패는 새 호출을 차단하되 기존 호출·미확인 효과·예약을 보존한다.

## 사용자 저장 위치

같은 OS 사용자의 두 호스트는 같은 DAERI 저장소를 사용한다.
설정·이력은 프로젝트 Git·플러그인 캐시 밖에 두고 OS 사용자 디렉터리 API로 경로를 해소한다.

| 용도 | macOS 기본 위치 제안 | Windows 기본 위치 제안 |
|---|---|---|
| 설정·이력·산출물·관리 실행 환경 | `~/Library/Application Support/DAERI` | `%LOCALAPPDATA%\DAERI` |
| 다시 받을 수 있는 임시 배포물 | `~/Library/Caches/DAERI` | 데이터 영역의 `cache` |
| 비밀값 | Keychain 또는 제공자 저장소 | Credential Manager 또는 제공자 저장소 |

설정·이력·산출물·런타임을 구분하고 캐시를 승인·호출 기록의 유일한 원본으로 쓰지 않는다.
파일 형식·SQLite는 구현 후보이며 별도 저장 프레임워크는 만들지 않는다.

사용자 전용 권한과 저장소의 잠금·개정 검사로 두 호스트의 동시 수정을 처리한다.
중간에 저장된 설정은 실행하지 않고 업무 소유권·호출·예산은 [실행 제어](../tasks/workflow.md#실행-검사와-기록)를 따른다.

위치의 근거는 [Apple 파일 시스템](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/FileSystemProgrammingGuide/FileSystemOverview/FileSystemOverview.html)과 [Windows Known Folder](https://learn.microsoft.com/en-us/windows/win32/shell/knownfolderid)다.
하위 배치는 프로젝트 제안이며 격리 전제는 [신뢰 경계](../hosts/approval.md#신뢰-경계와-최초-등록)를 따른다.

## 인증 저장과 전달

설정에는 불투명한 `credential_ref`와 허용 연결·origin·예상 주체를 저장한다.
등록 연결만 실행기 내부에서 참조를 해소하며 참조 ID·모델 입력 경로로 비밀을 조회하지 않는다.

비밀번호·OAuth token은 OS 비밀 저장소에 두고 AWS SSO 설정·캐시는 제공자 저장소를 재사용한다.
승인용 공개 키·정책은 [사용자 승인](../hosts/approval.md)의 등록 기록으로 관리한다.

macOS Keychain·Windows Credential Manager를 기본으로 제안한다.
자체 암호화 저장소·평문 파일 fallback은 도입하지 않고 잠김·미지원이면 복구 조치를 안내한다.

[Keychain](https://developer.apple.com/documentation/technotes/tn3137-on-mac-keychains)의 방식별 접근 조건을 확인한다.
실행 파일·서명·업데이트 경로별 읽기·교체·거부를 검증하며 일반 Python을 전용 서명 앱처럼 격리한다고 가정하지 않는다.
[Credential Manager](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw)는 현재 사용자·기기의 범위로 시작하고 payload·로그인 문맥 제한을 검증한다.

비밀은 로컬 설정·제공자 로그인 화면에서 입력하며 채팅·MCP 인수·명령문·Git·호스트 설정에 넣지 않는다.

해소한 비밀은 필요한 client·신뢰한 자식 프로세스에만 전달한다.
환경 전체·인증 헤더·토큰 URI·callback query·일회성 코드는 로그·모델 응답·진단 내보내기에서 제외한다.
하위 stderr·오류·결과도 저장·반환 전에 검사하고 다른 연결의 인증을 함께 주입하지 않는다.

진단·공유·내보내기에 제공자 캐시·인증 세션을 포함하지 않는다.

## AWS named profile과 SSO

각 AWS 연결은 하나의 제공자 named profile·설정 위치를 지정하며 기존 설정·캐시를 SDK·프록시가 읽는다.
DAERI profile은 여러 AWS 연결을 담고 제공자 profile들은 SSO session을 공유할 수 있다.

로그인은 `aws sso login --profile <provider-profile>` 형태로 안내한다.
MFA·동의는 사용자가 수행하고 `default`나 다른 profile을 덮어쓰지 않는다.
출처: [AWS SSO](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-sso.html).

선정 프록시는 연결당 하나의 `--profile`로 시작한다.
`aws_profile` 도구 입력으로 전환하는 multi-profile 경로와 `--skip-auth`는 사용하지 않는다.
출처: [프록시 profile](https://github.com/aws/mcp-proxy-for-aws/blob/main/README.md#multi-account-access).

부모 환경의 access key·session token·`AWS_MCP_PROXY_PROFILES` 등 인증 선택 값을 상속하지 않는다.
등록되지 않은 `credential_process`, web identity·container·metadata fallback도 사용하지 않는다.
`AWS_MCP_PROXY_PROFILES`가 `--profile`보다 우선할 수 있어 실제 적용 주체를 진단한다.

account·principal·role을 확인하며 이름·ARN prefix 비교만으로 같다고 추정하지 않는다.
매 갱신의 assumed-role session ID와 안정적인 계정·역할 문맥을 구분한다.
출처: [CLI 우선순위](https://docs.aws.amazon.com/cli/latest/userguide/cli-chap-configure.html), [SDK 공급 순서](https://docs.aws.amazon.com/boto3/latest/guide/credentials.html), [프록시 설정](https://github.com/aws/mcp-proxy-for-aws/blob/main/README.md#configuration-parameters).

설정·재로그인·권한 변화는 [재개 검사](#만료교체철회와-재개)를 적용하며 외부 파일·원격 권한의 원자적 고정을 가정하지 않는다.

유효 session의 SDK 갱신만 기존 연결·권한 안에서 허용한다.
만료는 사용자 재로그인으로 처리하고 다른 profile·정적 key로 대체하지 않는다.
연결 해제 시 다른 연결에 영향을 줄 수 있는 `aws sso logout`을 자동 실행하지 않는다.

## GitLab OAuth와 fine-grained PAT

공식 MCP의 `mcp` scope OAuth를 우선 제안하고 등록 GitLab origin에 독립적으로 인증한다.
지원 인스턴스에서는 MCP tool Execute 권한의 fine-grained PAT를 대안으로 등록한다.
Legacy PAT·project token·group token은 사용하지 않으며 지원·권한이 부족하면 연결 선택을 재검토한다.
출처: [GitLab MCP 인증](https://docs.gitlab.com/user/model_context_protocol/mcp_server/#authentication).

MCP client의 OAuth를 재사용해 PKCE·state·정확한 redirect URI를 검사하고 callback은 사용자 PC에서 처리한다.

사전 등록 public client를 우선 검토하고 동적 등록은 허용된 설정 작업에서만 한다.
로컬 redirect 정책과 실제 client 호환성을 확인하며 미지원이면 연결 방식을 재검토한다.
출처: [사전 등록 앱](https://docs.gitlab.com/user/model_context_protocol/mcp_server/#reuse-a-single-oauth-application).

refresh는 기존 issuer·client·사용자·scope 안에서 수행하고 동시 갱신으로 새 값을 덮어쓰지 않는다.
갱신 거부·철회·다른 주체 확인은 재인증 또는 등록 검토로 처리한다.

## PostgreSQL·Redis

host·port·database·username·TLS 정책과 비밀 참조를 나눠 등록하며 DB 권한·Redis ACL을 업무 승인으로 사용하지 않는다.

DBHub에는 관리 TOML 환경 치환·제한한 자식 환경으로 비밀을 전달하고 치환 변수 누락을 시작 전에 검사한다.
비밀 DSN을 `--dsn`·프로젝트 파일에 넣지 않는다.
출처: [DBHub 환경 치환](https://dbhub.ai/config/toml#environment-variable-interpolation).

실행 설정은 등록 연결의 고정 복사본을 사용하며 hot reload로 인증·대상을 바꾸지 않는다.
개인 키 파일이 필요한 방식은 명시적인 관리 파일·권한을 검토하고 OS 비밀을 평문 파일로 자동 풀지 않는다.

네트워크 PostgreSQL은 `verify-full`·등록 CA를 기본으로 하며 DBHub의 `require`를 인증서·hostname 검증으로 취급하지 않는다.
출처: [DBHub TLS](https://dbhub.ai/config/toml#sslmode).

Redis에는 `REDIS_USERNAME`·`REDIS_PWD`·TLS 조건을 자식 환경으로 전달한다.
`--password`, 비밀 `--url`과 CLI 인증 override를 사용하지 않는다.
entrypoint의 환경 입력·CA·hostname 검증·cluster 지원을 확인한 뒤 등록한다.
출처: [Redis 환경 설정](https://github.com/redis/mcp-redis/blob/main/README.md#configuration-via-environment-variables).

비밀번호 교체가 기존 pool·session을 종료한다고 가정하지 않는다.
새 비밀로 세션을 준비하고 이전 세션의 새 호출을 차단하며 이미 보낸 요청의 원 인증·대상·확인 기록은 보존한다.

## 원격 인증 경계

토큰은 등록 issuer·audience·resource·origin에 바인딩한다.
다른 origin redirect·서버 반환 URL에 인증 헤더를 자동 전달하지 않고 호스트 token passthrough를 사용하지 않는다.
출처: [MCP 인증](https://modelcontextprotocol.io/specification/latest/basic/authorization), [MCP 보안](https://modelcontextprotocol.io/specification/latest/basic/security_best_practices).

TLS 검증을 유지하고 사내 CA·프록시는 허용 참조·도구별 신뢰 저장소로 적용한다.
자료 제공·보존은 [결과·이력](../tasks/workflow.md#이력과-완료-근거)을 따른다.

## 만료·교체·철회와 재개

인증 만료·잠김·철회·참조 해소 실패는 새 호출을 보류하고 실제 복구를 확인한다.

같은 주체·대상·권한의 비밀 교체만으로 재승인받지는 않는다.
재인증 뒤 업무 상태·승인·계약·권한·예산을 재검사하고 주체·대상·강제 정책 변경은 스냅샷 규칙을 적용한다.

DAERI 참조 해제·세션 종료·제공자 token과 권한 철회는 서로 다른 작업이다.
선택한 범위만 처리하며 원격 철회·진행 중 작업 종료를 추정하지 않는다.
기기·OS 사용자 변경·저장소 손실 뒤에는 참조 복원과 별도로 재인증한다.

## 변경·공유·복구

업무가 참조한 구성 개정은 출처로 보존하며 profile 삭제로 이력·미확인 호출·원격 리소스를 지우지 않는다.
업데이트·재설치·백업 복원은 [보존·복구 계약](../installation/environment.md#업데이트와-데이터-보존)을 따른다.

공유 템플릿에는 역할·입력 항목·확인한 구성만 담고 비밀·제공자 캐시·업무 이력·승인·소유권은 이식하지 않는다.
다른 사용자·PC는 참조를 새로 연결하고 재인증·진단한다.

종료 업무의 확인도 원 연결·실제 대상·현재 권한을 대조하며 [확인 대상 계약](../mcp/plans.md#미확인-호출의-확인-대상)을 따른다.

## 구현 전 확인

[초기 구현](../../plans/initial-implementation.md)에서 관리 화면·파일 형식과 OS 저장소 API를 선정·검증한다.
복수 리소스·기본값 변경 뒤 기존 업무 유지, 동시 접근·복구와 권한 철회·비밀 갱신 구분을 확인한다.
잠김·만료·갱신 실패·다른 profile fallback·주변 인증 충돌과 비밀 노출 거부는 필수 검증이다.
실제 설정 생성·로그인·저장소·연결은 아직 실행하지 않았다.
