# DAERI

**여러 사람이 서비스 경험을 함께 쌓는 멀티 오케스트레이션 에이전트입니다.**
Claude Code·Codex에서 자연어로 업무를 맡기면 계획을 제시하고, 승인한 범위에서 조사·분석·검증·보고를 진행합니다.
확인한 경험과 피드백은 다음 구성원의 업무에 활용합니다.

- **자율 수행** — 승인한 목표에 맞춰 도구·방법·순서·결과 형식을 선택합니다.
- **공유 기억** — 같은 프로젝트의 구성원이 누적 경험을 활용하고 새 구성원도 기존 지식으로 시작합니다.
- **업무별 실행** — 목표·승인·작업 상태를 업무에 연결하고 필요한 하위 작업의 결과를 통합합니다.
- **관리형 운영** — AWS AgentCore의 실행·문맥·도구 연결·보안·관측·평가를 활용합니다.

## 구성도

![DAERI AWS 서비스 구성도](docs/assets/daeri-agentcore-architecture.svg)

AWS managed harness가 모델·도구 호출을 반복하고 DAERI는 업무와 공유 지식을 연결합니다.
생성 모델과 임베딩은 각각 사내 API 또는 Amazon Bedrock을 선택합니다.
공유 지식·업무는 PostgreSQL·pgvector, 업무 대화는 AgentCore Memory, 파일은 S3로 구성하는 설계입니다.
자세한 흐름과 역할은 [아키텍처](docs/architecture/overview.md)를 참고하세요.

## 업무 흐름

1. 프로젝트·서비스를 선택하고 원하는 업무와 결과를 요청합니다.
2. DAERI가 관련 경험을 참고해 계획을 제시합니다.
3. 계획을 승인하면 해당 범위에서 수행하고 보고·파일을 제공합니다.
4. 결과·피드백을 기억해 다음 업무에 활용합니다.

가상 예시:

```text
국가별 EV 충전소 개수를 확인하고 CSV로 정리해 줘.
```

## 설치

이 브랜치는 AWS 공용 서비스를 개발합니다.
현재 개발 단위와 실행·검증 방법은 [개발 안내](docs/development/dependencies.md#클라우드-개발-상태)에서 확인할 수 있습니다.

기존 PC용 플러그인의 설치·setup·업무 안내는 [local-agent 브랜치](https://github.com/VVoodyHong/DAERI/tree/local-agent)에 있습니다.
로컬 제품을 설치할 때는 안내의 Git 브랜치를 `local-agent`로 지정합니다(Codex `--ref local-agent`, Claude Code URL의 `#local-agent`).
