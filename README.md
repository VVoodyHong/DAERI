# DAERI (대리)

**프로젝트의 업무 경험을 함께 쌓는 멀티 오케스트레이션 에이전트입니다.**
Claude Code 또는 Codex에서 자연어로 업무를 맡기고, 계획을 승인한 뒤 결과와 피드백을 주고받습니다.
수행 과정에서 얻은 지식을 프로젝트에 축적해 다음 업무와 새로운 구성원의 업무에 활용합니다.

대리급 동료처럼 일을 맡기고 함께 개선한다는 의미를 담았습니다.

현재 `main`에서는 AWS 클라우드 버전을 개발합니다.
PC에서 설치해 사용할 수 있는 버전은 아래 [시작하기](#시작하기)에서 안내합니다.

## 주요 특징

- **승인 후 자율 수행** — 목표와 범위를 확인받고 도구·방법·순서·결과 형식을 선택합니다.
- **여러 에이전트의 협업** — 총괄 에이전트가 업무를 나누고 하위 에이전트의 결과를 통합합니다.
- **프로젝트의 공유 기억** — 수행 요령·실패 경험·피드백을 축적해 새 구성원도 현재 접근 권한 안에서 기존 경험을 활용합니다.

## 활용 예시

연결한 자료와 도구를 바탕으로 조사·분석·검증·보고를 요청합니다.
다음은 활용 방향을 보여 주는 가상 예시입니다.

| 업무 | 요청 예시 |
|---|---|
| 데이터 분석 | 국가별 EV 충전소 개수를 확인하고 CSV로 정리해 줘. |
| 이슈·문서 조사 | 이번 주 이슈와 운영 문서를 확인하고 진행 상황과 남은 과제를 정리해 줘. |
| 개발 검토 | 소스 코드와 운영 문서를 비교해서 설정 차이와 수정할 부분을 찾아 줘. |

## 구성도

![업무 요청부터 기억 회상·계획 제시·승인·작업 위임·도구 실행·결과 보고·경험 저장까지의 순서를 표시한 DAERI 아키텍처](docs/assets/daeri-overview.svg)

AWS AgentCore의 managed harness가 모델·도구 호출과 에이전트 수행을 맡고, DAERI가 업무와 공유 지식을 연결합니다.
업무별 대화 기억은 하던 일을 이어갈 때, 공유 지식은 다른 구성원의 유사 업무에도 활용합니다.
생성 모델과 임베딩은 각각 사내 API 또는 Amazon Bedrock으로 구성합니다.
구성 요소별 역할은 [아키텍처](docs/architecture/overview.md)에서 확인할 수 있습니다.

## 사용 흐름

클라우드 서비스가 제공할 업무 흐름입니다.

1. **요청** — 프로젝트·서비스와 원하는 결과를 알려 줍니다.
2. **계획·승인** — 관련 경험을 참고한 계획을 확인하고 승인하거나 조정합니다.
3. **수행·보고** — 승인 범위에서 필요한 조사·분석·검증을 진행하고 결과와 파일을 받습니다.
4. **피드백·기억** — 결과를 보완하고 재사용할 경험을 다음 업무에 남깁니다.

각 업무의 계획·승인·진행 상태를 유지하면서 프로젝트의 지식을 함께 축적합니다.

## 시작하기

### PC에서 설치해 사용하기

Claude Code 또는 Codex를 설치하고 로그인한 뒤, 호스트를 종료한 상태에서 터미널로 아래 명령을 실행합니다.
PC용 플러그인은 `local-agent` 브랜치에서 설치합니다.

**Codex**

```shell
codex plugin marketplace add https://github.com/VVoodyHong/DAERI.git --ref local-agent
codex plugin add daeri@daeri-marketplace
codex
```

**Claude Code**

```shell
claude plugin marketplace add 'https://github.com/VVoodyHong/DAERI.git#local-agent'
claude plugin install daeri@daeri-marketplace --scope user
claude
```

설치 후 대화창에서 Codex는 `$daeri:setup service-prod`, Claude Code는 `/daeri:setup service-prod`로 시작합니다.
`service-prod`는 서비스·환경별 연결과 기억을 묶는 **profile**의 예시입니다.
연결 정보 입력과 첫 업무 요청은 [로컬 버전의 최초 설정](https://github.com/VVoodyHong/DAERI/tree/local-agent#최초-설정)을 따릅니다.

### AWS 공용 서비스 살펴보기

공용 서비스의 개발 환경·구현 상태·환경 구성은 [개발 안내](docs/development/dependencies.md#클라우드-개발-상태)에서 확인할 수 있습니다.
