# 단타분봉수집기

KIS 1분봉(주식일별분봉조회, FHKST03010230)을 실측하고, 과거 1년치 분봉을 모으는 명령줄 프로그램입니다.
기존 주식데이터수집기와 **별개 프로그램**입니다.

- 시세 조회 API만 씁니다. 주문·계좌 API는 코드에 없습니다.
- 기존 수집기 DB(`market.sqlite`)는 **읽기 전용**으로만 엽니다(쓰기 자체가 불가능한 모드).
- 분봉은 새 DB(`바탕화면/단타분석/danta.sqlite`)에 저장합니다. 원본 JSON 응답은 저장하지 않습니다.
- 모든 실행 내용은 `바탕화면/단타분석/logs/danta_<명령>_<시각>.txt`에도 남습니다(측정 결과 보고용).

## 설치

1. GitHub Release에서 `단타분봉수집기_danta-v<버전>.zip`을 받아 원하는 폴더에 풉니다.
   (기존 수집기 폴더와 **다른 폴더**에 두세요.)
2. 처음 실행하면 `KIS_APP_KEY`, `KIS_APP_SECRET`을 묻습니다. 입력한 글자는 화면에 보이지 않고,
   설치 폴더 `secret/keys.env`에만 저장됩니다. 이 파일은 공유·업로드하지 마세요.
3. 설정을 바꾸려면 `config/danta_settings.env.example`을 `config/danta_settings.env`로 복사해 고칩니다.
   기존 수집기의 데이터 폴더를 바꿔 쓰고 있다면 `SOURCE_DB`에 `market.sqlite` 경로를 적어 주세요.

토큰은 `바탕화면/단타분석/system/kis_token_danta.json`에 따로 캐시합니다(기존 수집기 토큰과 분리).
만료 10분 전까지는 재발급하지 않고, 재발급은 직전 발급 후 61초가 지나야 합니다.

## 명령

명령 프롬프트(cmd)나 PowerShell에서 설치 폴더로 이동해 실행합니다.

| 명령 | 하는 일 |
|---|---|
| `단타분봉수집기.exe probe` | 직전 거래일 하루치 분봉(기본 005930) 수집 → 봉 개수, 호출 수, 첫/마지막 시각, 봉 간격 분포, 누적 거래대금 여부, 1분 거래대금 합계와 일봉 거래대금 차이 출력. 이어서 데이터가 있는 가장 오래된 날짜를 이분 탐색 |
| `probe --code 000660 --date 20261120` | 종목·날짜 지정 |
| `probe --no-oldest` | 가장 오래된 날짜 탐색 생략 |
| `단타분봉수집기.exe ratetest` | 초당 5,10,15,20,25,30건을 단계별 20초씩 호출. EGW00201이 처음 난 단계에서 중단 |
| `단타분봉수집기.exe jobs` | 기존 일봉 DB에서 날짜별 "거래대금 상위 100 + 등락률 상위 50"(중복 제거)으로 (날짜, 종목) 작업 목록 생성. 기간은 1년 전 ~ 어제 |
| `단타분봉수집기.exe backfill --rate 10 --max-calls 200` | 작업 목록을 오래된 날짜부터 수집. 이어받기 지원 |
| `단타분봉수집기.exe recalc` | 저장된 분봉의 1분 거래대금(`value_krw`)을 다시 계산 |

### backfill 동작

- 완료한 (종목, 날짜)는 `done` 테이블에 기록되고, 다음 실행 때 건너뜁니다. 중간에 Ctrl+C로 꺼도 됩니다.
- `--max-calls`: 이번 실행의 호출 상한입니다. 상한에 닿으면 다음 종목을 시작하지 않습니다(진행 중인 종목은 끝까지 받으므로 몇 회 넘을 수 있음).
- 50건마다 진행률, 호출 수, 실제 속도, EGW00201 수, 남은 예상 시간을 출력합니다.
- EGW00201(초당 호출 초과)은 1, 2, 4, 8, 16초 기다리며 재시도합니다. 그래도 안 되면 그 종목은 실패로 셉니다.
- 실패가 20건 쌓이면 중단합니다. 토큰 오류는 1회만 재발급 후 재시도하고, 그래도 실패하면 즉시 중단합니다.
- 봉이 0개인 날(거래정지 등)도 완료로 기록되며 `done.n_bars=0`으로 구분됩니다.

## 측정 순서

**기존 수집기를 돌리지 않는 시간에** 실행하세요(같은 KIS 키를 씁니다).

1. `probe` → 로그 파일 확인
2. probe 결과 `acml_tr_pbmn 단조 증가: 예`이고 일봉 거래대금과 차이가 작으면,
   `config/danta_settings.env`에 `ACML_IS_CUMULATIVE=Y`를 넣습니다.
   (이후 backfill부터 `value_krw` = 앞 봉과의 누적 거래대금 차이, 하루 첫 봉은 누적값 그대로 저장)
3. `ratetest` → EGW00201이 나기 직전 단계의 속도를 확인
4. `jobs` → 작업 건수 확인
5. 소규모 `backfill --rate <확인한 안전 속도> --max-calls 200` → 진행 속도, EGW00201 수 확인
6. 전체 `backfill --rate <안전 속도>` (여러 번 나눠 실행해도 이어받기)

측정 후 `바탕화면/단타분석/logs/`의 텍스트 파일을 저장소 `docs/verify/danta/`에 올려 주세요.
올리기 전에 키·계좌번호가 들어 있지 않은지 확인해 주세요(키와 토큰은 `****`로 가려서 기록합니다).

## 측정 결과로 확인할 항목

| 항목 | 어디서 보나 |
|---|---|
| 1분 단위 여부 | probe "봉 간격 분포" (1분 간격이 대부분이어야 함) |
| 하루당 호출 수 | probe "호출 수" (예상 4회: 391봉 / 120건) |
| 가장 오래된 날짜 | probe "가장 오래된 날짜" |
| 초당 한도 | ratetest에서 EGW00201이 처음 난 단계 |
| 하루 총 호출 한도 유무 | 전체 backfill 중 EGW00201이 아닌 다른 오류가 대량으로 나는지 |
| 전체 백필 예상 시간 | 소규모 backfill의 "남은 예상" 값 |
| 누적 거래대금 여부·단위 | probe "단조 증가", "1분 거래대금 하루 합계"와 "일봉 DB value_krw" 차이 |
| FID_PW_DATA_INCU_YN=N 동작 | probe에서 하루치(약 391봉)가 다 받아지는지. 안 되면 `PW_DATA_INCU_YN=Y`로 바꿔 다시 probe |

## 저장 구조 (`danta.sqlite`)

| 테이블 | 컬럼 |
|---|---|
| `minute_bar` | code, date, time, open, high, low, close, volume, acml_value_krw(누적 거래대금), value_krw(1분 거래대금), 기본키 (code, date, time) |
| `done` | code, date, n_bars, n_calls, fetched_at |
| `jobs` | date, code, reason(value / change / value+change) |

## 개발

```bash
python -m danta --help
```

```bash
python -m pytest -q tests/danta
```

빌드는 `.github/workflows/build-danta.yml`이 합니다. 단타 관련 PR마다 `danta/version.py`를 올리면,
main 병합 시 `danta-v<버전>` 태그와 Release가 만들어집니다.
