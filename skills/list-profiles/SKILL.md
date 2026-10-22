---
name: list-profiles
description: 저장된 DAERI profile 목록이나 등록된 리소스 범주를 조회하려 할 때 사용한다.
---

# DAERI list-profiles

응답 언어·용어와 현재 단계의 안내는 [사용자 안내](../../docs/spec/hosts/plugins.md#사용자-안내)를 따른다.
저장한 profile 목록을 요약하고 현재 대화의 선택과 기존 업무를 보존한다.
대상 선택과 출력 범위는 [profile 선택 계약](../../docs/spec/configuration/profiles.md#선택과-기본값)을 따른다.
`../../`는 설치된 플러그인 루트다.

1. [공통 관리 진입](../../docs/development/dependencies.md#관리-입력과-결과)의 실행 조건을 확인하고 준비된 전용 Python으로 `profile list`를 실행한다.
   미준비 환경은 [setup](../setup/SKILL.md) 안내로 처리한다.
2. 반환한 profile·등록 리소스 범주를 이름순으로 요약하고 구별에 필요한 식별자를 덧붙인다.
   빈 목록은 등록 profile 부재로, 조회 실패는 확인한 원인·영향·다음 조치로 알린다.
3. 필요하면 선택·삭제는 [profile](../profile/SKILL.md), 생성·수정은 setup의 진입 경로를 안내한다.

이 결과는 등록 정보이며 현재 연결·인증·권한의 진단은 각각 해당 관리 경로에서 확인한다.
