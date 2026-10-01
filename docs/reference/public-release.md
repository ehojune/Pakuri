# 공개 검토 (lookup only)

**권고: MIT로 공개.** 좋은 개발자의 공개 작업에서 배우는 Star-seeker로 소개한다. 코드·참고 명단·검증 근거·kuromi 연결을 공개 후보에 넣는다.

| 확인 | 결과 |
|---|---|
| Git 현재 파일·기존 이력 | 실제 인증값·환자 데이터·비공개 코드·DB·운영 로그 없음 |
| Actions 로그 | 기존 실행 2개, 952줄 점검. 실제 인증값·사용자 로컬 경로·비공개 연구 정보 없음 |
| 참고 명단 | 공개 계정·공식 출처. 계정과 소속 확인을 구분하며 미확인 소속은 그대로 표시 |
| 불필요한 내부 정보 | 현재 문서에서 로컬 절대 경로·Yuan 비공개 SHA·내부 흐름 제거 |
| 이력 재작성 | 불필요. 과거 경로·커밋 식별자는 비밀값이나 비공개 코드가 아님 |
| 호환성 | repo/package `Pakuri`/`pakuri`, collect 명령, schema 1, PAKURI_PATH, pakuri_activity 유지 |
| kuromi | 공개 연관성 허용. 독립 뉴스·맥락 확장은 유지하며 reference adapter로 덮어쓰지 않음 |
| 라이선스 | 사용자 선택: MIT. LICENSE와 패키지 메타데이터 추가 |
| 주요 문서 | README·NEXT·DECISIONS 합계 2,855 → 3,242자 |
| 실행 검증 | Pakuri 41개 + reference adapter 24개 테스트 통과. CLI·JSON 연결 규격 유지 |

현재 visibility는 private. 공개 전환 시 코드·이력·Actions 로그가 공개된다. [GitHub 안내](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/managing-repository-settings/setting-repository-visibility)

Star-seeker는 표현과 설명을 바꾼 이름이다. 실제로 읽는 자료는 공개 저장소 생성·커밋·push·릴리스 메타데이터이며, 수집 한계와 원문 링크를 그대로 보여준다. GitHub star 수나 개인의 우열을 평가하지 않는다.
