---
name: list-profiles
description: 저장된 DAERI profile 목록이나 등록된 리소스 범주를 조회하려 할 때 사용한다.
---

# DAERI list-profiles

기본 한국어로 조회 결과와 필요한 다음 행동을 간결하게 안내하고 운영 대상은 `profile`로 부른다.
저장한 profile 목록을 요약하고 현재 대화의 선택과 기존 업무를 보존한다.
`../../`는 설치된 플러그인 루트다.

1. macOS는 `sh "<루트>/scripts/manage.sh" --connect profile list`, Windows는 `powershell -NoProfile -ExecutionPolicy RemoteSigned -File "<루트>\scripts\manage.ps1" --connect profile list`를 실행한다.
   이 진입은 [현재 설치의 실행 환경](../../docs/spec/hosts/plugins.md#설치부터-업무-시작까지의-실제-경계)을 준비하고 같은 사용자 저장소의 목록을 반환한다.
   `setup_required`이면 반환한 메시지와 현재 호스트의 setup 호출을 안내한다.
2. 목록 조회가 성공하면 profile·등록 리소스 범주를 이름순으로 요약하고 구별에 필요한 식별자를 덧붙인다.
   빈 목록은 등록 profile 부재로, 조회 실패는 확인한 원인·영향·다음 조치로 알린다.
3. 필요하면 선택·삭제는 [profile](../profile/SKILL.md), 생성·수정은 setup의 진입 경로를 안내한다.

이 결과는 등록 정보이며 현재 연결·인증·권한의 진단은 각각 해당 관리 경로에서 확인한다.
상세 계약이 필요한 경우 호스트의 파일 읽기 도구로 [profile 선택](../../docs/spec/configuration/profiles.md#선택과-기본값)과 [사용자 안내](../../docs/spec/hosts/plugins.md#사용자-안내)를 확인한다.
