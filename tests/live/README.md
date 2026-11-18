실제 API를 부르는 테스트를 두는 곳입니다. `@pytest.mark.live`를 붙이고, 기본 실행(`pytest`)에서는 제외됩니다.
개발 환경(클라우드)에는 키가 없으므로 여기 테스트는 PC에서만 `pytest -m live`로 실행합니다.
