"""토스증권 Open API 조회 전용 측정 프로그램 (toss probe).

- 허용 목록(allowlist)에 있는 조회 경로와 토큰 발급만 호출할 수 있다. 주문 계열은 호출 자체가 예외로 실패한다.
- danta.sqlite는 읽기 전용(mode=ro)으로만 연다.
- 경로·필드·한도는 토스증권 Open API 문서(openapi-docs/latest/openapi.json) 기준.
"""
