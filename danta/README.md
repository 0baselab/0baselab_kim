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
| `probe --compare` | 직전 거래일의 거래대금 상위 5 + 하위 5 종목을 시장 구분 J(KRX)와 UN(통합)으로 각각 수집해 종목별 분봉 누적 거래대금, 일봉 value_krw, 차이(%), 마지막 봉 누적 증가분(15:30 마감 동시호가 체결분으로 추정)을 출력. 약 80회 호출 |
| `probe --compare --markets J,NX,UN --date 20261120` | 시장 구분·날짜 지정 |
| `단타분봉수집기.exe ratetest` | 초당 5,10,15,20,25,30건을 단계별 20초씩 호출. EGW00201이 처음 난 단계에서 중단 |
| `단타분봉수집기.exe jobs` | 기존 일봉 DB에서 날짜별 "거래대금 상위 100 + 등락률 상위 50"(중복 제거)으로 (날짜, 종목) 작업 목록 생성. 기간은 1년 전 ~ 어제 |
| `단타분봉수집기.exe backfill --rate 10 --max-calls 200` | 작업 목록을 오래된 날짜부터 수집. 이어받기 지원 |
| `단타분봉수집기.exe daily` | 매일 수집. 아직 수집하지 않은 개장일만 골라 작업을 만들고 바로 수집 + 테마 스냅샷. 기본 초당 5건 (`--rate`로 변경) |
| `단타분봉수집기.exe export` | 기존 DB에서 분석용 부분 DB(`단타분석/analysis/market_subset.sqlite`) 만들기. 아래 설명 참고 |
| `단타분봉수집기.exe recalc` | 저장된 분봉의 1분 거래대금(`value_krw`)을 다시 계산 |

### backfill 동작

- 완료한 (종목, 날짜)는 `done` 테이블에 기록되고, 다음 실행 때 건너뜁니다. 중간에 Ctrl+C로 꺼도 됩니다.
- `--max-calls`: 이번 실행의 호출 상한입니다. 상한에 닿으면 다음 종목을 시작하지 않습니다(진행 중인 종목은 끝까지 받으므로 몇 회 넘을 수 있음).
- 50건마다 진행률, 호출 수, 실제 속도, EGW00201 수, 남은 예상 시간을 출력합니다.
- EGW00201(초당 호출 초과)은 1, 2, 4, 8, 16초 기다리며 재시도합니다. 그래도 안 되면 그 종목은 실패로 셉니다.
- 실패가 20건 쌓이면 중단합니다. 토큰 오류는 1회만 재발급 후 재시도하고, 그래도 실패하면 즉시 중단합니다.
- 봉이 0개인 날(거래정지 등)도 완료로 기록되며 `done.n_bars=0`으로 구분됩니다.

### jobs의 대상 범위(보통주) 정하는 방법

날짜마다 아래 중 하나를 쓰고, `job_days` 테이블과 로그에 날짜별로 기록합니다. 미래 날짜의 범위는 쓰지 않습니다.

| method | 언제 | 내용 |
|---|---|---|
| `universe` | 기존 DB에 그날 `universe_daily`가 있음 | 그날 범위 그대로 |
| `past_universe` | 빠진 날짜가 전체 기간의 5% 이하 | 가장 가까운 과거 날짜의 범위 (`basis_date`) |
| `price_common` | 빠진 날짜가 5% 초과, 또는 과거 범위가 없음 | 그날 `price_daily` 종목 중 보통주만. 판정 기준은 기존 수집기와 같음(주권·보통주·스팩 아님). 종목정보는 그날 `krx_base_daily` 우선, 없으면 `stock_master`(최신 정보). 정보가 없어 판정할 수 없는 종목도 후보에 넣습니다. 상위권에 들면 수집 대상에 포함하고 `jobs.reason` 끝에 `+판정불가`(코드 끝자리 0) 또는 `+판정불가(끝자리0아님)`을 붙이며, 해당 종목코드를 `note`에 적습니다. 코드 규칙은 표시용입니다 |

분석 단계에서 판정 불가 종목을 빼려면 `WHERE common_check = 'confirmed'` 조건을 쓰면 됩니다.
분봉 테이블과 함께 쓸 때는 `minute_bar`를 `jobs`와 (code, date)로 연결합니다.

## 분석용 부분 DB 내보내기 (export)

```bash
단타분봉수집기.exe export
```

```bash
단타분봉수집기.exe export --profile all --since 20250901 --max-mb 380
```

기존 수집기 DB(`market.sqlite`)를 읽기 전용으로 열어 일부만 `<바탕화면>/단타분석/analysis/market_subset.sqlite`로 복사합니다.
같은 이름의 파일이 있으면 지우고 새로 만듭니다. 기존 DB는 바뀌지 않습니다. 기본값은 `--since 20250901`, `--max-mb 380`입니다.

### `--profile analysis` (기본)

분석에 필요한 표·열만 복사합니다. `date` 열이 있는 표는 `date >= --since` 행만 복사합니다. 원본 JSON 판정 규칙은 쓰지 않습니다.

| 표 | 열 |
|---|---|
| price_daily | date, code, market, open, high, low, close, change_rate_pct, volume_sh, value_krw, mktcap_krw, listed_shares, is_provisional |
| universe_daily | date, code, in_universe, exclude_reason, is_admin, is_halted, days_since_listing, is_new_listing |
| investor_daily | date, code, frgn_net_qty, orgn_net_qty, prsn_net_qty, frgn_net_amt_mil, orgn_net_amt_mil, prsn_net_amt_mil, is_provisional |
| market_investor_daily | date, market, `*_unconfirmed` 열 전부, index_close, is_provisional |
| index_daily | date, series, index_name, index_class, open, high, low, close, change_rate_pct, is_provisional |
| adj_price_daily | code, date, as_of, open, high, low, close |
| trading_calendar | date, is_open |
| stock_master | code, name, market, listed_date, krx_secugrp, krx_sect_tp, krx_kind, is_admin, is_halted, first_seen, last_seen |
| theme_membership, theme_versions | 전체 |

- 원본에 없는 표·열은 경고만 출력하고 건너뜁니다.
- 열 타입과 기본키(기본키 열이 모두 남을 때)는 원본과 같게 만듭니다.
- 끝에 표별 행 수와 크기, `investor_daily`의 날짜별 종목 수(최소·평균·최대),
  `price_daily` 대비 수급이 있는 비율(전체 / 보통주 범위만)을 출력합니다.

### `--profile all` (이전 동작)

| 테이블 종류 | 처리 |
|---|---|
| `date` 컬럼이 있음 | `date >= --since` 행만 복사. 날짜가 `YYYYMMDD`인지 `YYYY-MM-DD`인지 표본으로 판단. 판단이 안 되면 건너뜀 |
| `date` 컬럼이 없음 | 20만 행 이하면 통째로 복사, 넘으면 건너뜀 |
| 원본 응답(JSON 원문)처럼 보이는 큰 텍스트 컬럼이 있음 | 건너뜀 (값이 `{` 또는 `[`로 시작하고 평균 2,000자 이상) |

### 공통

- 별도 인덱스는 크기를 줄이려고 복사하지 않습니다. 마지막에 VACUUM으로 파일 크기를 줄입니다.
- 결과가 `--max-mb`를 넘으면 큰 테이블부터 크기를 출력하고 **실패로 끝납니다**(종료 코드 1).
  잘못 넘기는 일이 없도록 결과 파일은 지웁니다. `--since`를 늦춰 다시 실행하세요.
- 원본 DB 크기만큼 읽으므로 몇 분 걸릴 수 있습니다. 기존 수집기가 실행 중이 아닐 때 돌리세요.

## 매일 수집 (daily)

실행하면 아래 순서로 동작합니다.

1. 기존 수집기가 실행 중이면(`주식데이터수집/system/collector.lock`의 프로그램이 살아 있으면) 1분마다 확인하며 최대 60분 기다립니다.
   그래도 실행 중이면 로그를 남기고 종료합니다(종료 코드 2). 잠금 파일은 읽기만 합니다.
2. 테마 스냅샷: 기존 수집기의 `themes/theme_master.csv`를 `theme_snapshot` 테이블에 오늘 날짜로 저장합니다.
   같은 날짜가 이미 있으면 건너뜁니다. 위치는 설정 `THEME_FILE`로 바꿀 수 있습니다.
3. 오늘이 개장일인데 기존 DB에 오늘 일봉이 아직 없거나, 달력에 오늘이 없으면
   "기존 수집기 데이터 대기" 로그를 남기고 정상 종료합니다(종료 코드 0).
4. 1년 전 ~ 오늘 사이 개장일 중 작업 목록에 아직 없는 날짜(일봉이 있는 날)만 골라 작업을 추가하고 수집합니다.
   대상 범위를 정하는 방법은 `jobs`와 같습니다.
5. 실패한 (종목, 날짜)는 다음 실행 때 자동으로 다시 시도합니다. 같은 건이 연속 3회 실패하면(`fail_log` 테이블)
   재시도를 멈추고 요약에 목록으로 출력합니다. 다시 시도하게 하려면 `fail_log`에서 그 행을 지우면 됩니다.
6. 요약: 수집 날짜, 완료·실패 건수, 호출 수, EGW00201 수, 재시도 중단 목록, DB에 있는 분봉 날짜 범위.

### Windows 작업 스케줄러 등록

평일 21:30(기존 수집기 20:30 수집 이후)에 실행하고, 22:40에 한 번 더 실행합니다(재실행 1회).
두 번째 실행은 첫 실행이 기존 수집기를 기다리다 끝났거나 PC가 꺼져 있었을 때를 위한 것입니다.
이미 다 받은 날에는 호출 없이 바로 끝납니다.

> 작업 스케줄러의 "실패 시 다시 시작" 설정은 프로그램이 0이 아닌 코드로 끝난 경우에는 동작하지 않는 것으로 알려져 있습니다(확인 필요).
> 그래서 재실행은 두 번째 시각을 등록하는 방식으로 합니다.

PowerShell을 열고 첫 줄의 경로를 실제 설치 경로로 바꿔 아래를 붙여 넣습니다.

```powershell
$exe = "C:\단타분봉수집기\단타분봉수집기.exe"
$a = New-ScheduledTaskAction -Execute $exe -Argument "daily" -WorkingDirectory (Split-Path $exe)
$days = "Monday","Tuesday","Wednesday","Thursday","Friday"
$t1 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 21:30
$t2 = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $days -At 22:40
$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 4)
Register-ScheduledTask -TaskName "단타분봉수집기 daily" -Action $a -Trigger $t1,$t2 -Settings $s
```

- `-StartWhenAvailable`: 그 시각에 PC가 꺼져 있었으면 켜진 뒤 실행합니다.
- `-MultipleInstances IgnoreNew`: 앞 실행이 아직 돌고 있으면 두 번째는 건너뜁니다.
- 로그온한 상태에서만 실행됩니다(바탕화면·OneDrive 경로와 키 파일을 쓰기 때문).
- 확인: 작업 스케줄러 앱의 "작업 스케줄러 라이브러리"에서 `단타분봉수집기 daily`를 오른쪽 클릭 → 실행 → `<바탕화면>\단타분석\logs\danta_daily_*.txt` 확인.
- 삭제: `Unregister-ScheduledTask -TaskName "단타분봉수집기 daily"`

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

로그와 화면 출력에서 Windows 사용자 폴더와 바탕화면(OneDrive 포함) 경로는 `<사용자폴더>`, `<바탕화면>`으로 가려집니다.

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
| `jobs` | date, code, reason(value / change / value+change, 판정 불가면 끝에 +판정불가 또는 +판정불가(끝자리0아님)), common_check(confirmed / unknown_code0 / unknown_other) |
| `fail_log` | code, date, fail_count(연속 실패 횟수), last_error, updated_at. 성공하면 삭제 |
| `theme_snapshot` | date, code, theme, added_date, basis, note, file_hash. 기본키 (date, theme, code) |
| `job_days` | date, method, basis_date, note, n_codes (날짜별로 대상 범위를 정한 방법) |

## 개발

```bash
python -m danta --help
```

```bash
python -m pytest -q tests/danta
```

빌드는 `.github/workflows/build-danta.yml`이 합니다. 단타 관련 PR마다 `danta/version.py`를 올리면,
main 병합 시 `danta-v<버전>` 태그와 Release가 만들어집니다.
