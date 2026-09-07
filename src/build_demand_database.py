"""
================================================================================
Hourly electricity demand database builder for Korea (2024)
공개 통계자료 기반 한국 시군구별·업종별 시간단위 전력수요 데이터베이스 구축
================================================================================
Companion code for the paper "A Methodology for Constructing Hourly
Electricity Demand Database by Municipality and Sector Using Publicly
Available Statistics in Korea."

Inputs  : see metadata/input_data_manifest.csv for sources and expected layout
          (data/raw/{kpx,kepco,patterns,geo}/ under the working directory)
Outputs : data/processed/Gross_Pattern_fixed.csv
          data/processed/L0_timeseries_8784.csv       (initial series, ~5 GB)
          data/processed/L1_timeseries_8784.csv       (final series,   ~5 GB)
          data/processed/sigungu_timeseries_wide.csv  (district hourly totals)

Run     : python src/build_demand_database.py
================================================================================
"""

import pandas as pd
import numpy as np
from pathlib import Path
import sys
import time

# ==============================================================================
# 0. 경로 설정
# ==============================================================================
BASE      = Path(__file__).resolve().parent.parent   # working directory root
RAW_KPX     = BASE / "data" / "raw" / "kpx"
RAW_KEPCO   = BASE / "data" / "raw" / "kepco"
RAW_PATTERN = BASE / "data" / "raw" / "patterns"
PROC_DIR    = BASE / "data" / "processed"

# 입력 파일
CONSUMPTION_FILE    = RAW_KEPCO   / "consumption_industry.csv"
HOME_PPA_FILE       = RAW_KEPCO   / "HOME_전력수급_추계정보.xlsx"
PROFILE_FILE        = RAW_PATTERN / "표준산업분류_중분류_24시상대계수.xlsx"
MAPPING_FILE        = RAW_PATTERN / "표준매핑표_확정_용도업종38_KSIC77.xlsx"

DONG_FILES = [
    RAW_KEPCO / "industry_dong_JAN_MAR_2024.csv",
    RAW_KEPCO / "industry_dong_APR_JUN_2024.csv",
    RAW_KEPCO / "industry_dong_JUL_SEP_2024.csv",
    RAW_KEPCO / "industry_dong_OCT_DEC_2024.csv",
]

# 출력 파일
OUT_GROSS_FIXED     = PROC_DIR / "Gross_Pattern_fixed.csv"
OUT_L0              = PROC_DIR / "L0_timeseries_8784.csv"
OUT_L1              = PROC_DIR / "L1_timeseries_8784.csv"
OUT_WIDE            = PROC_DIR / "sigungu_timeseries_wide.csv"

# 청크 사이즈 (대용량 CSV 처리용)
CHUNK = 2_000_000


def log(msg):
    """진행 상황 출력"""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ==============================================================================
# 새 STEP 0: KPX raw 5분 데이터 → Gross Pattern 생성
# ==============================================================================
# ==============================================================================

def step0_fix_gross_pattern() -> pd.DataFrame:
    """
    KPX 대국민 전력수급현황 5분 데이터에서 시간별 Gross Pattern 생성.
    
    과정:
      1. KPX raw 5분 데이터 로드 → 완전 5분 그리드 생성 → 결측 선형보간
      2. 시간 평균 → KPX_Net(t) [MW = MWh/h]
      3. HOME 월별 한전PPA → 시간별 분해 (cos² bell curve)
      4. Gross(t) = KPX_Net(t) + PPA(t)
    """
    log("STEP 0: KPX raw → Gross Pattern 생성...")
    
    # ---- 1. KPX raw 5분 데이터 로드 ----
    KPX_RAW_FILES = [
        RAW_KPX / "openapi_20240101_20240331.csv",
        RAW_KPX / "openapi_20240401_20240630.csv",
        RAW_KPX / "openapi_20240701_20240930.csv",
        RAW_KPX / "openapi_20241001_20241231.csv",
    ]
    
    frames = []
    for f in KPX_RAW_FILES:
        try:
            df = pd.read_csv(f, encoding="cp949")
        except UnicodeDecodeError:
            df = pd.read_csv(f, encoding="utf-8-sig")
        df.columns = [c.strip() for c in df.columns]
        frames.append(df)
    
    df_raw = pd.concat(frames, ignore_index=True)
    df_raw["datetime"] = pd.to_datetime(df_raw["기준일시"].astype(str), format="%Y%m%d%H%M%S")
    df_raw["demand_MW"] = pd.to_numeric(df_raw["현재수요(MW)"], errors="coerce")
    df_raw = df_raw[["datetime", "demand_MW"]].sort_values("datetime").reset_index(drop=True)
    
    log(f"  KPX raw 로드: {len(df_raw):,}행")
    
    # ---- 2. 완전 5분 그리드 + 선형보간 ----
    full_index = pd.date_range("2024-01-01 00:00", "2024-12-31 23:55", freq="5min")
    df_5min = df_raw.set_index("datetime").reindex(full_index)
    
    n_missing = df_5min["demand_MW"].isna().sum()
    df_5min["demand_MW"] = df_5min["demand_MW"].interpolate(method="linear")
    
    log(f"  5분 그리드: {len(df_5min):,}행, 보간: {n_missing}개")
    
    # ---- 3. 시간 평균 → KPX_Net(t) ----
    df_5min.index.name = "datetime"
    hourly = df_5min.resample("1h").mean()
    hourly = hourly.rename(columns={"demand_MW": "KPX_Net"})
    
    log(f"  시간별 변환: {len(hourly)}행")
    log(f"  KPX_Net 연간합: {hourly['KPX_Net'].sum():,.0f} MWh = {hourly['KPX_Net'].sum()/1e6:.1f} TWh")
    
    # ---- 4. HOME 월별 PPA → 시간별 분해 ----
    df_home = pd.read_excel(HOME_PPA_FILE)
    df_home["월"] = df_home["구분"].apply(
        lambda x: round((float(x) % 1) * 100)
    ).astype(int)
    home_ppa = df_home.set_index("월")["한전PPA(실적)"].to_dict()
    
    log(f"  HOME PPA 연간합: {sum(home_ppa.values()):,.0f} MWh")
    
    # 태양광 시간 패턴: cos² bell curve (월별 일출/일몰 기준)
    sunrise = {1:7.5, 2:7.0, 3:6.5, 4:6.0, 5:5.5, 6:5.0, 7:5.5, 8:6.0, 9:6.0, 10:6.5, 11:7.0, 12:7.5}
    sunset  = {1:17.5, 2:18.0, 3:18.5, 4:19.0, 5:19.5, 6:20.0, 7:19.5, 8:19.0, 9:18.5, 10:18.0, 11:17.5, 12:17.0}
    
    hourly["PPA_Gen"] = 0.0
    hourly["월"] = hourly.index.month
    hourly["시간"] = hourly.index.hour
    
    for m in range(1, 13):
        mask = hourly["월"] == m
        hours_m = hourly.loc[mask, "시간"].values
        sr, ss = sunrise[m], sunset[m]
        
        # cos² shape: 일출~일몰 사이만 양수
        shape = np.zeros(len(hours_m))
        for i, h in enumerate(hours_m):
            h_mid = h + 0.5  # 시간 중앙값
            if sr <= h_mid <= ss:
                x = (h_mid - sr) / (ss - sr)  # 0~1 정규화
                shape[i] = np.sin(np.pi * x) ** 2
        
        # 월별 총량 맞춤
        shape_sum = shape.sum()
        if shape_sum > 0:
            hourly.loc[mask, "PPA_Gen"] = shape * (home_ppa[m] / shape_sum)
    
    # ---- 5. Gross 계산 ----
    hourly["Gross_Standard"] = hourly["KPX_Net"] + hourly["PPA_Gen"]
    
    # ---- 6. 검증 ----
    log("  월별 검증:")
    for m in range(1, 13):
        mask = hourly["월"] == m
        net = hourly.loc[mask, "KPX_Net"].sum()
        ppa = hourly.loc[mask, "PPA_Gen"].sum()
        gross = hourly.loc[mask, "Gross_Standard"].sum()
        log(f"    {m:2d}월: Net={net:>12,.0f}  PPA={ppa:>10,.0f}  Gross={gross:>12,.0f}")
    
    log(f"  Gross 연간합: {hourly['Gross_Standard'].sum():,.0f} MWh = {hourly['Gross_Standard'].sum()/1e6:.1f} TWh")
    
    # ---- 7. 저장 ----
    out = hourly[["KPX_Net", "PPA_Gen", "Gross_Standard"]].copy()
    out.index.name = "datetime"
    out.to_csv(OUT_GROSS_FIXED, encoding="utf-8-sig")
    log(f"  저장 완료: {OUT_GROSS_FIXED}")
    
    return out.reset_index()

# ==============================================================================
# STEP 1: KEPCO 판매량 데이터 로드 → E(r, u, m)
# ==============================================================================
def step1_load_consumption() -> pd.DataFrame:
    """
    consumption_industry.csv → long format E(시도, 시군구, 용도업종, 월, 판매량_kWh)
    
    이 데이터가 연간 에너지 총량의 절대 기준.
    """
    log("STEP 1: KEPCO 판매량 로드...")
    
    try:
        df_raw = pd.read_csv(CONSUMPTION_FILE, header=None, encoding="utf-8-sig")
    except UnicodeDecodeError:
        df_raw = pd.read_csv(CONSUMPTION_FILE, header=None, encoding="cp949")
    
    # 헤더 행 찾기
    header_idx = None
    for i in range(10):
        row = df_raw.iloc[i].astype(str).tolist()
        if any("연도" in str(x) for x in row):
            header_idx = i
            break
    
    header = [str(h).strip() for h in df_raw.iloc[header_idx]]
    df = df_raw.iloc[header_idx + 1:].copy()
    df.columns = header
    df = df.dropna(how="all")
    
    month_cols = [f"{m}월" for m in range(1, 13)]
    
    df_long = df.melt(
        id_vars=["연도", "시도", "시군구", "업종별"],
        value_vars=month_cols,
        var_name="월", value_name="판매량_kWh",
    )
    
    df_long["연도"] = pd.to_numeric(df_long["연도"], errors="coerce")
    df_long["월"] = pd.to_numeric(df_long["월"].str.replace("월", ""), errors="coerce").astype("Int64")
    df_long["판매량_kWh"] = pd.to_numeric(df_long["판매량_kWh"], errors="coerce")
    df_long = df_long.dropna(subset=["판매량_kWh"])
    df_long = df_long[df_long["연도"] == 2024].copy()
    df_long.rename(columns={"업종별": "용도업종"}, inplace=True)
    
    E = df_long.groupby(["시도", "시군구", "용도업종", "월"], as_index=False)["판매량_kWh"].sum()
    
    total = E["판매량_kWh"].sum()
    log(f"  시군구 수: {E.groupby(['시도','시군구']).ngroups}")
    log(f"  용도업종 수: {E['용도업종'].nunique()}")
    log(f"  연간 총 판매량: {total/1e9:.3f} TWh")
    
    return E


# ==============================================================================
# STEP 2: 산업분류별 24h 패턴 로드
# ==============================================================================
def step2_load_profiles() -> tuple:
    """
    77개 KSIC 산업분류 + 주택용 월별 24시간 대표패턴 로드.
    
    Returns:
        df_industry: (월, 시간, 산업분류, 부하지수) long format
        df_residential: (월, 시간, 패턴값) - 정규화된 주택용 패턴
    """
    log("STEP 2: 산업분류별 24h 패턴 로드...")
    
    df_raw = pd.read_excel(PROFILE_FILE)
    
    # 0행이 산업분류명 헤더
    industry_names = [str(df_raw.iloc[0, i]).strip() for i in range(2, len(df_raw.columns))]
    
    # 데이터는 1행부터
    df_data = df_raw.iloc[1:].copy()
    df_data.columns = ["월", "시간"] + industry_names
    
    df_data["월"] = pd.to_numeric(df_data["월"], errors="coerce").astype("Int64")
    df_data["시간"] = pd.to_numeric(df_data["시간"], errors="coerce").astype("Int64")
    
    # Industry profiles (long format)
    id_cols = ["월", "시간"]
    value_cols = [c for c in df_data.columns if c not in id_cols]
    
    df_industry = df_data.melt(
        id_vars=id_cols, value_vars=value_cols,
        var_name="산업분류", value_name="부하지수",
    )
    df_industry["부하지수"] = pd.to_numeric(df_industry["부하지수"], errors="coerce")
    df_industry = df_industry.dropna(subset=["부하지수"])
    
    log(f"  산업분류 패턴: {df_industry['산업분류'].nunique()}개")
    
    # 주택용 패턴도 같은 파일의 '주택용' 시트에서 로드
    df_res_raw = pd.read_excel(PROFILE_FILE, sheet_name="주택용")
    df_res = df_res_raw.iloc[1:].copy()  # 0행은 서브헤더
    df_res.columns = ["월", "시간", "월평균", "월요일", "화금", "토요일", "일요일"]
    df_res["월"] = pd.to_numeric(df_res["월"], errors="coerce").astype("Int64")
    df_res["시간"] = pd.to_numeric(df_res["시간"], errors="coerce").astype("Int64")
    df_res["월평균"] = pd.to_numeric(df_res["월평균"], errors="coerce")
    df_res = df_res.dropna(subset=["월", "시간", "월평균"])
    
    # 산업분류 패턴과 같은 형태로 변환하여 합치기
    df_res_long = df_res[["월", "시간", "월평균"]].copy()
    df_res_long.rename(columns={"월평균": "부하지수"}, inplace=True)
    df_res_long["산업분류"] = "주택용"
    
    df_industry = pd.concat([df_industry, df_res_long], ignore_index=True)
    log(f"  주택용 패턴 추가 → 총 {df_industry['산업분류'].nunique()}개")

    return df_industry


# ==============================================================================
# STEP 3: 매핑표 로드
# ==============================================================================
def step3_load_mapping(pattern_names=None) -> pd.DataFrame:
    """
    용도업종(38) ↔ KSIC(77) 매핑표 로드.
    """
    log("STEP 3: 매핑표 로드...")
    
    df = pd.read_excel(MAPPING_FILE, sheet_name="매핑표")
    df["용도업종"] = df["용도업종(38)"].astype(str).str.strip()
    df["산업분류"] = df["KSIC_중분류(77)"].astype(str).str.strip()
    
    mapping = df[["용도업종", "산업분류"]].copy()
    
    log(f"  매핑 행 수: {len(mapping)}")
    log(f"  용도업종: {mapping['용도업종'].nunique()}개 → KSIC: {mapping['산업분류'].nunique()}개")

    # 매핑표 KSIC명도 패턴 데이터 기준으로 정규화
    MAP_NAME_FIX = {
        "석탄, 원유 및 천연가스 광업": "석탄/ 원유 및 천연가스 광업",
    }
    mapping["산업분류"] = mapping["산업분류"].replace(MAP_NAME_FIX)

    # 명칭 정규화 자동 매칭 : 매핑표 표기(';' 등)가
    # 패턴 파일 표기(':' 등)와 달라 조인이 실패하던 문제 복구 (step4와 동일 로직)
    if pattern_names:
        import re as _re
        def _norm(s):
            return _re.sub(r"[\s;:,()·ㆍ/]", "", str(s))
        pmap = {_norm(p): p for p in pattern_names}
        auto_fix = {}
        for n in mapping["산업분류"].unique():
            if n not in pattern_names and _norm(n) in pmap:
                auto_fix[n] = pmap[_norm(n)]
        if auto_fix:
            mapping["산업분류"] = mapping["산업분류"].replace(auto_fix)
        log(f"  매핑표 KSIC명 자동 정규화: {len(auto_fix)}개")
        still = [n for n in mapping["산업분류"].unique()
                 if n not in pattern_names and n != "(주택용 별도 패턴 적용)"]
        log(f"  매핑표 잔여 미매칭: {len(still)}개 {still[:4]}")

    # 가정용부문 → 주택용 패턴 매핑 추가
    mapping = pd.concat([
        mapping,
        pd.DataFrame([{"용도업종": "가정용부문", "산업분류": "주택용"}])
    ], ignore_index=True)
    
    # 기존 "(주택용 별도 패턴 적용)" 행 제거
    mapping = mapping[mapping["산업분류"] != "(주택용 별도 패턴 적용)"]
    
    return mapping


# ==============================================================================
# STEP 4: 읍면동 산업구조 비중 산출 → w(r, i, m)
# ==============================================================================
def step4_load_industry_share(pattern_names=None) -> pd.DataFrame:
    """
    읍면동 × 산업분류(중) × 월별 판매량 → 시군구 × 산업분류 × 월별 비중 w(r,i,m)
    
    w(r, i, m) = E_dong(r, i, m) / Σ_i E_dong(r, i, m)
    """
    log("STEP 4: 읍면동 산업구조 비중 산출...")
    
    frames = []
    for path in DONG_FILES:
        if not path.exists():
            log(f"  ⚠️ 파일 없음: {path.name}")
            continue
        
        try:
            df = pd.read_csv(path, encoding="utf-8-sig", low_memory=False)
        except UnicodeDecodeError:
            df = pd.read_csv(path, encoding="cp949", low_memory=False)
        
        df.columns = [c.strip() for c in df.columns]
        
        # 산업분류코드(중) vs 산업분류명(중) 스왑 감지
        code_col = "산업분류코드(중)"
        name_col = "산업분류명(중)"
        
        def numeric_ratio(s):
            s = s.dropna().astype(str).str.strip()
            return (s.str.fullmatch(r"\d+")).mean() if len(s) > 0 else 0
        
        if numeric_ratio(df[name_col]) > 0.8 and numeric_ratio(df[code_col]) < 0.8:
            df[name_col], df[code_col] = df[code_col].copy(), df[name_col].copy()
        
        df[name_col] = df[name_col].astype(str).str.strip()
        
        frames.append(df[["년도", "월", "시도", "시군구", name_col, "판매량"]].copy())
        log(f"  로드: {path.name} ({len(df):,}행)")
    
    df_all = pd.concat(frames, ignore_index=True)
    df_all.rename(columns={"산업분류명(중)": "산업분류"}, inplace=True)
    df_all["년도"] = pd.to_numeric(df_all["년도"], errors="coerce")
    df_all["월"] = pd.to_numeric(df_all["월"], errors="coerce")
    df_all["판매량"] = pd.to_numeric(df_all["판매량"], errors="coerce")
    df_all = df_all[df_all["년도"] == 2024].dropna(subset=["판매량"])
    
    # 시군구 × 산업분류 × 월 집계
    grp = df_all.groupby(["시도", "시군구", "산업분류", "월"], as_index=False)["판매량"].sum()
    
    # 시군구 × 월 내 비중 계산
    total = grp.groupby(["시도", "시군구", "월"])["판매량"].transform("sum")
    grp["비중"] = grp["판매량"] / total
    grp.loc[total == 0, "비중"] = 0.0
    
    log(f"  시군구 수: {grp['시군구'].nunique()}")
    log(f"  산업분류 수: {grp['산업분류'].nunique()}")

    # 산업분류명 정규화 (읍면동 데이터 → 패턴 데이터 기준)
    DONG_NAME_FIX = {
        "가죽/가방및신발제조업": "가죽, 가방 및 신발 제조업",
        "건축기술/엔지니어링및기타과학기술서비스업": "건축 기술, 엔지니어링 및 기타 과학기술 서비스업",
        "고무제품및플라스틱제품제조업": "고무 및 플라스틱제품 제조업",
        "공공행정/국방및사회보장행정": "공공 행정, 국방 및 사회보장 행정",
        "기타전문/과학및기술서비스업": "기타 전문, 과학 및 기술 서비스업",
        "도매및소매업(45~47)": "도매 및 상품 중개업",
        "의료/정밀/광학기기및시계제조업": "의료, 정밀, 광학 기기 및 시계 제조업",
        "의복/의복액세서리및모피제품제조업": "의복, 의복 액세서리 및 모피제품 제조업",
        "전기/가스/증기및공기조절공급업": "전기, 가스, 증기 및 공기 조절 공급업",
        "전자부품/컴퓨터/영상/음향및통신장비제조업": "전자 부품, 컴퓨터, 영상, 음향 및 통신장비 제조업",
        "창작/예술및여가관련서비스업": "창작, 예술 및 여가관련 서비스업",
        "컴퓨터프로그래밍/시스템통합및관리업": "컴퓨터 프로그래밍, 시스템 통합 및 관리업",
        "코크스/연탄및석유정제품제조업": "코크스, 연탄 및 석유정제품 제조업",
        "펄프/종이및종이제품제조업": "펄프, 종이 및 종이제품 제조업",
        "폐기물수집운반/처리및원료재생업": "폐기물 수집, 운반, 처리 및 원료 재생업",
        "하수/폐수및분뇨처리업": "하수, 폐수 및 분뇨 처리업",
    }
    grp["산업분류"] = grp["산업분류"].replace(DONG_NAME_FIX)
    log(f"  산업분류명 정규화: {len(DONG_NAME_FIX)}개 수정")

    # 명칭 정규화 자동 매칭 :
    # 읍면동 자료와 패턴 파일의 표기 차이(공백, ';' vs ':', '·' vs 'ㆍ' 등)로
    # 50개 산업분류의 조인이 실패해 해당 부하가 균등 폴백되던 문제 복구.
    if pattern_names:
        import re as _re
        def _norm(s):
            return _re.sub(r"[\s;:,()·ㆍ/]", "", str(s))
        pmap = {_norm(p): p for p in pattern_names}
        auto_fix = {}
        for n in grp["산업분류"].unique():
            if n not in pattern_names and _norm(n) in pmap:
                auto_fix[n] = pmap[_norm(n)]
        if auto_fix:
            grp["산업분류"] = grp["산업분류"].replace(auto_fix)
            grp = grp.groupby(["시도", "시군구", "산업분류", "월"], as_index=False).agg(
                {"판매량": "sum", "비중": "sum"})
        log(f"  산업분류명 자동 정규화: {len(auto_fix)}개 추가 매칭")
        still = [n for n in grp["산업분류"].unique() if n not in pattern_names]
        log(f"  잔여 미매칭: {len(still)}개 {still[:5]}")

    # 시도명 정규화 (읍면동 데이터 → KEPCO 기준)
    SIDO_NAME_FIX = {
        "강원특별자치도": "강원도",
        "전북특별자치도": "전라북도",
    }
    grp["시도"] = grp["시도"].replace(SIDO_NAME_FIX)

    # 시군구명 정규화 (읍면동의 "시 구" → KEPCO의 "시"로 통합)
    SGG_CONSOLIDATE = {
        "고양시 덕양구": "고양시", "고양시 일산동구": "고양시", "고양시 일산서구": "고양시",
        "부천시 소사구": "부천시", "부천시 오정구": "부천시", "부천시 원미구": "부천시",
        "성남시 분당구": "성남시", "성남시 수정구": "성남시", "성남시 중원구": "성남시",
        "수원시 권선구": "수원시", "수원시 영통구": "수원시", "수원시 장안구": "수원시", "수원시 팔달구": "수원시",
        "안산시 단원구": "안산시", "안산시 상록구": "안산시",
        "안양시 동안구": "안양시", "안양시 만안구": "안양시",
        "용인시 기흥구": "용인시", "용인시 수지구": "용인시", "용인시 처인구": "용인시",
        "창원시 마산합포구": "창원시", "창원시 마산회원구": "창원시",
        "창원시 성산구": "창원시", "창원시 의창구": "창원시", "창원시 진해구": "창원시",
        "포항시 남구": "포항시", "포항시 북구": "포항시",
        "전주시 덕진구": "전주시", "전주시 완산구": "전주시",
        "천안시 동남구": "천안시", "천안시 서북구": "천안시",
        "청주시 상당구": "청주시", "청주시 서원구": "청주시",
        "청주시 청원구": "청주시", "청주시 흥덕구": "청주시",
    }
    grp["시군구"] = grp["시군구"].replace(SGG_CONSOLIDATE)
    
    # 통합 후 비중 재계산 (같은 시군구로 합쳐졌으므로)
    grp = grp.groupby(["시도", "시군구", "산업분류", "월"], as_index=False)["비중"].sum()
    # 비중 합이 1 초과할 수 있으므로 재정규화
    total = grp.groupby(["시도", "시군구", "월"])["비중"].transform("sum")
    grp["비중"] = grp["비중"] / total
    grp.loc[total == 0, "비중"] = 0.0
    
    log(f"  시군구 통합: {len(SGG_CONSOLIDATE)}개 구 → 시 단위로 합산")
    
    return grp[["시도", "시군구", "산업분류", "월", "비중"]]


# ==============================================================================
# STEP 5: 시군구 × 용도업종 × 월 × 24h 가중평균 패턴 생성
#
#   p(r, u, m, h) = Σ_{i ∈ I(u)} w(r, i, m) · p(i, m, h)    ... 수식 (1)
#
# ==============================================================================
def step5_build_use_patterns(
    industry_profiles: pd.DataFrame,
    mapping: pd.DataFrame,
    industry_share: pd.DataFrame,
    E: pd.DataFrame,
) -> pd.DataFrame:
    """
    시군구 × 용도업종 × 월 × 24h 정규화 패턴 생성.
    
    패턴 우선순위:
      1. 지역별 산업구조 가중평균 패턴 (매핑 가능한 업종)
      2. 가정용부문 → 전국 평균 산업 패턴 (주택용 별도 패턴 미확보 시)
      3. 전국 평균 패턴 (해당 용도업종의 E 가중)
      4. 균등 분포 (1/24)
    """
    log("STEP 5: 시군구×용도업종×월×24h 패턴 생성...")
    
    # 5-1. 산업분류별 패턴에 매핑 조인 → 용도업종별 패턴
    #      industry_share(시도,시군구,산업분류,월,비중) × industry_profiles(월,시간,산업분류,부하지수)
    #      → mapping으로 산업분류 → 용도업종 변환
    
    # 매핑 테이블에서 가정용 제외 (별도 처리)
    map_valid = mapping[mapping["산업분류"] != "(주택용 별도 패턴 적용)"].copy()
    
    # industry_share에 매핑 조인: 산업분류 → 용도업종
    share_mapped = pd.merge(
        industry_share, map_valid,
        on="산업분류", how="inner",
    )
    # share_mapped: (시도, 시군구, 산업분류, 월, 비중, 용도업종)
    
    # industry_profiles와 조인: (산업분류, 월) → (시간, 부하지수)
    merged = pd.merge(
        share_mapped, industry_profiles,
        on=["산업분류", "월"], how="inner",
    )
    # merged: (시도, 시군구, 산업분류, 월, 비중, 용도업종, 시간, 부하지수)
    
    # 가중 패턴값 = 비중 × 부하지수
    merged["가중패턴"] = merged["비중"] * merged["부하지수"]
    
    # 시군구 × 용도업종 × 월 × 시간별 합산
    pat = merged.groupby(
        ["시도", "시군구", "용도업종", "월", "시간"], as_index=False
    )["가중패턴"].sum()
    
    # 5-2. 전국 평균 패턴 계산 (fallback용)
    #      E 가중 평균으로 용도업종 × 월 × 시간 패턴
    pat_with_E = pd.merge(
        pat, E[["시도", "시군구", "용도업종", "월", "판매량_kWh"]],
        on=["시도", "시군구", "용도업종", "월"], how="left",
    )
    pat_with_E["판매량_kWh"] = pat_with_E["판매량_kWh"].fillna(0)
    
    def calc_national_pattern(group):
        w = group["판매량_kWh"]
        p = group["가중패턴"]
        s = w.sum()
        return (w * p).sum() / s if s > 0 else 0
    
    nat_pat = (
        pat_with_E.groupby(["용도업종", "월", "시간"])
        .apply(calc_national_pattern)
        .reset_index(name="nat_pattern")
    )
    
    # 5-3. Skeleton: E가 존재하는 모든 (r,u,m) × 24시간
    hours = pd.DataFrame({"시간": range(1, 25)})
    hours["key"] = 1
    
    skeleton = E[["시도", "시군구", "용도업종", "월"]].drop_duplicates().copy()
    skeleton["key"] = 1
    skeleton = skeleton.merge(hours, on="key").drop(columns=["key"])
    
    # 5-4. 패턴 결합 (local → national → uniform fallback)
    full = skeleton.merge(
        pat[["시도", "시군구", "용도업종", "월", "시간", "가중패턴"]],
        on=["시도", "시군구", "용도업종", "월", "시간"],
        how="left",
    )
    
    full = full.merge(
        nat_pat, on=["용도업종", "월", "시간"], how="left",
    )
    
    # 패턴값 선택
    full["base"] = full["가중패턴"]
    # 지역 자료의 비중이 모두 0인 (r,u,m)은 local 미확보로 간주 → national 폴백
    # (기존에는 24h 합=0 가드에서 조용히 균등 분포로 떨어졌음)
    _zsum = full.groupby(["시도", "시군구", "용도업종", "월"])["base"].transform("sum")
    n_zero_local = int(((_zsum == 0) & full["base"].notna()).sum())
    full.loc[(_zsum == 0) & full["base"].notna(), "base"] = np.nan
    log(f"  전량 0 local → national 폴백 전환: {n_zero_local:,}행")
    # 가정용부문: 주택용 패턴 직접 삽입 (읍면동 데이터에 없으므로 local 불가)
    res_pat = industry_profiles[industry_profiles["산업분류"] == "주택용"][["월", "시간", "부하지수"]]
    if not res_pat.empty:
        res_map = res_pat.set_index(["월", "시간"])["부하지수"].to_dict()
        mask_home = full["용도업종"] == "가정용부문"
        full.loc[mask_home, "base"] = full.loc[mask_home].apply(
            lambda r: res_map.get((r["월"], r["시간"]), 1.0), axis=1
        )
        log(f"  가정용부문 주택용 패턴 직접 삽입: {mask_home.sum():,}행")
    
    # local이 없으면 national
    mask_no_local = full["base"].isna()
    full.loc[mask_no_local, "base"] = full.loc[mask_no_local, "nat_pattern"]
    
    # national도 없으면 uniform
    mask_no_any = full["base"].isna()
    full.loc[mask_no_any, "base"] = 1.0
    
    # 5-5. (r, u, m)별 24시간 합이 1이 되도록 정규화
    grp_keys = ["시도", "시군구", "용도업종", "월"]
    full["sum_24h"] = full.groupby(grp_keys)["base"].transform("sum")
    full.loc[full["sum_24h"] == 0, "base"] = 1.0
    full.loc[full["sum_24h"] == 0, "sum_24h"] = 24.0
    full["패턴값"] = full["base"] / full["sum_24h"]
    
    result = full[["시도", "시군구", "용도업종", "월", "시간", "패턴값"]].copy()
    
    n_local = (~mask_no_local).sum()
    n_national = (mask_no_local & ~mask_no_any).sum()
    n_uniform = mask_no_any.sum()
    log(f"  패턴 할당: local={n_local:,}, national={n_national:,}, uniform={n_uniform:,}")
    log(f"  총 행 수: {len(result):,}")
    
    return result


# ==============================================================================
# STEP 6: 초기 시계열 L⁰(t, r, u) 생성
#
#   q(t, m, u) = 패턴값(h) / Σ_{t∈m} 패턴값(h)  (월 내 정규화)
#   L⁰(t, r, u) = E(r, u, m) · q(t, m, u)          ... 수식 (3)
#
# ==============================================================================
def step6_build_L0(
    E: pd.DataFrame,
    patterns: pd.DataFrame,
) -> None:
    """
    8784시간 캘린더에 패턴을 펼쳐 L0 생성.
    메모리 절약을 위해 월별로 처리하여 CSV로 직접 저장.
    """
    log("STEP 6: L0 초기 시계열 생성 (8784시간)...")
    
    # 2024년 캘린더
    dt_index = pd.date_range("2024-01-01 00:00", "2024-12-31 23:00", freq="h")
    cal = pd.DataFrame({"datetime": dt_index})
    cal["월"] = cal["datetime"].dt.month
    cal["일"] = cal["datetime"].dt.day
    cal["시간"] = cal["datetime"].dt.hour + 1  # 패턴은 1~24
    cal["days_in_month"] = cal["datetime"].dt.days_in_month
    
    # E에 일수 추가 → 일별 에너지
    days_map = cal.groupby("월")["days_in_month"].max().to_dict()
    E_daily = E.copy()
    E_daily["일수"] = E_daily["월"].map(days_map)
    E_daily["일별에너지_kWh"] = E_daily["판매량_kWh"] / E_daily["일수"]
    
    # 월별로 처리
    header_written = False
    total_rows = 0
    
    for month in range(1, 13):
        cal_m = cal[cal["월"] == month][["datetime", "월", "일", "시간"]].copy()
        pat_m = patterns[patterns["월"] == month][["시도", "시군구", "용도업종", "시간", "패턴값"]].copy()
        E_m = E_daily[E_daily["월"] == month][["시도", "시군구", "용도업종", "일별에너지_kWh"]].copy()
        
        # 패턴 × E → 시간당 부하
        day_hour = pd.merge(pat_m, E_m, on=["시도", "시군구", "용도업종"], how="left")
        day_hour["일별에너지_kWh"] = day_hour["일별에너지_kWh"].fillna(0)
        day_hour["시간당부하_kWh"] = day_hour["일별에너지_kWh"] * day_hour["패턴값"]
        
        # 캘린더와 조인 → 8784시간 중 해당 월 시간에 펼침
        L0_m = pd.merge(
            cal_m, day_hour[["시도", "시군구", "용도업종", "시간", "시간당부하_kWh"]],
            on=["시간"], how="left",
        )
        L0_m["시간당부하_kWh"] = L0_m["시간당부하_kWh"].fillna(0)
        
        # 컬럼 정리
        L0_m = L0_m.rename(columns={"시간당부하_kWh": "L0_kWh"})
        L0_m = L0_m[["datetime", "월", "일", "시간", "시도", "시군구", "용도업종", "L0_kWh"]]
        
        # CSV 저장 (append)
        L0_m.to_csv(
            OUT_L0, mode="a", index=False, encoding="utf-8-sig",
            header=not header_written,
        )
        header_written = True
        total_rows += len(L0_m)
        
        log(f"  {month:2d}월: {len(L0_m):>10,}행 저장")
    
    log(f"  L0 총 행 수: {total_rows:,}")
    log(f"  저장 완료: {OUT_L0}")


# ==============================================================================
# STEP 7: Two-stage Proportional Scaling
#
#   Step A (시간축): β(t) = L_TD(t) / Σ_{r,u} L⁰(t,r,u)     ... 수식 (8)
#                    L½(t,r,u) = β(t) · L⁰(t,r,u)             ... 수식 (9)
#
#   Step B (월별):   γ(r,u,m) = E(r,u,m) / Σ_{t∈m} L½(t,r,u)  ... 수식 (10)
#                    L¹(t,r,u) = γ(r,u,m) · L½(t,r,u),  t∈m    ... 수식 (11)
#
# ==============================================================================
def step7_two_stage_scaling(
    gross_df: pd.DataFrame,
    E: pd.DataFrame,
) -> None:
    """
    L0 → (β scaling) → L_half → (γ scaling) → L1
    
    대용량 CSV를 chunk 단위로 처리.
    """
    log("STEP 7: Two-stage proportional scaling 시작...")
    
    # ---- 7-A. β(t) 계산 ----
    log("  Step A: β(t) 계산...")
    
    # L_TD 준비 (Gross를 E_total에 맞춰 스케일)
    E_total = E["판매량_kWh"].sum()
    gross_df["datetime"] = pd.to_datetime(gross_df["datetime"])
    gross_total_kWh = gross_df["Gross_Standard"].sum() * 1000  # MWh → kWh
    alpha = E_total / gross_total_kWh
    gross_df["L_TD"] = gross_df["Gross_Standard"] * 1000 * alpha  # MWh→kWh 후 스케일
    
    log(f"    α = E_total / Gross_total(kWh) = {E_total:.0f} / {gross_total_kWh:.0f} = {alpha:.6f}")
    
    TD_map = gross_df.set_index("datetime")["L_TD"].to_dict()
    
    # L0 시간별 합계
    L0_time_sum = {}
    for chunk in pd.read_csv(OUT_L0, chunksize=CHUNK, encoding="utf-8-sig",
                             usecols=["datetime", "L0_kWh"]):
        chunk["datetime"] = pd.to_datetime(chunk["datetime"], format="mixed", errors="coerce")
        g = chunk.groupby("datetime")["L0_kWh"].sum()
        for t, v in g.items():
            L0_time_sum[t] = L0_time_sum.get(t, 0) + v
    
    # β(t) = L_TD(t) / Σ L0(t)
    beta = {}
    for t, s in L0_time_sum.items():
        td = TD_map.get(t, 0)
        beta[t] = td / s if s > 0 else 0
    
    log(f"    β 통계: mean={np.mean(list(beta.values())):.4f}, "
        f"min={np.min(list(beta.values())):.4f}, max={np.max(list(beta.values())):.4f}")
    
    # ---- β 적용 → L_half 생성 ----
    log("  Step A: L_half = β(t) · L0(t,r,u) 생성...")
    L_half_file = PROC_DIR / "L_half_temp.csv"
    if L_half_file.exists():
        L_half_file.unlink()
    
    header_written = False
    for chunk in pd.read_csv(OUT_L0, chunksize=CHUNK, encoding="utf-8-sig"):
        chunk["datetime"] = pd.to_datetime(chunk["datetime"], format="mixed", errors="coerce")
        chunk["beta"] = chunk["datetime"].map(beta).fillna(0)
        chunk["L_half_kWh"] = chunk["L0_kWh"] * chunk["beta"]
        
        out_cols = ["datetime", "월", "일", "시간", "시도", "시군구", "용도업종", "L_half_kWh"]
        chunk[out_cols].to_csv(
            L_half_file, mode="a", index=False, encoding="utf-8-sig",
            header=not header_written,
        )
        header_written = True
    
    log(f"    L_half 저장 완료: {L_half_file}")
    
    # ---- 7-B. γ(r,u,m) 계산 (월별 제약) ----
    # 연간 γ(r,u)는 월별 KEPCO 판매량 E(r,u,m)을 보존하지 못하므로 월별로 정의한다
    # (β가 시간축을 재배분하면 월별 합이 이탈). γ를 월별로 정의해 월별 제약을 정확히 보존.
    log("  Step B: γ(r,u,m) 계산 (월별 제약)...")

    # L_half 월별합 by (시도, 시군구, 용도업종, 월)
    L_half_monthly = {}
    for chunk in pd.read_csv(L_half_file, chunksize=CHUNK, encoding="utf-8-sig"):
        g = chunk.groupby(["시도", "시군구", "용도업종", "월"])["L_half_kWh"].sum()
        for key, v in g.items():
            L_half_monthly[key] = L_half_monthly.get(key, 0) + v

    # E 월별합 by (시도, 시군구, 용도업종, 월)
    E_monthly = E.groupby(["시도", "시군구", "용도업종", "월"])["판매량_kWh"].sum().to_dict()

    # γ(r,u,m) = E(r,u,m) / Σ_{t∈m} L_half(t,r,u)
    gamma = {}
    for key, e in E_monthly.items():
        lh = L_half_monthly.get(key, 0)
        gamma[key] = e / lh if lh > 0 else 0

    gamma_vals = [v for v in gamma.values() if v > 0]
    log(f"    γ(r,u,m) 통계: mean={np.mean(gamma_vals):.4f}, "
        f"min={np.min(gamma_vals):.4f}, max={np.max(gamma_vals):.4f}")

    # ---- γ 적용 → L1 생성 ----
    log("  Step B: L1 = γ(r,u,m) · L_half(t,r,u) 생성...")
    if OUT_L1.exists():
        OUT_L1.unlink()

    header_written = False
    for chunk in pd.read_csv(L_half_file, chunksize=CHUNK, encoding="utf-8-sig"):
        # gamma lookup (월별)
        keys = list(zip(chunk["시도"], chunk["시군구"], chunk["용도업종"], chunk["월"]))
        chunk["gamma"] = [gamma.get(k, 0.0) for k in keys]
        chunk["L1_kWh"] = chunk["L_half_kWh"] * chunk["gamma"]
        
        out_cols = ["datetime", "월", "일", "시간", "시도", "시군구", "용도업종", "L1_kWh"]
        chunk[out_cols].to_csv(
            OUT_L1, mode="a", index=False, encoding="utf-8-sig",
            header=not header_written,
        )
        header_written = True
    
    log(f"    L1 저장 완료: {OUT_L1}")
    
    # L_half 임시파일 삭제 (옵션)
    # L_half_file.unlink()


# ==============================================================================
# STEP 8: 시군구별 합산 wide format 출력
# ==============================================================================
def step8_export_wide() -> None:
    """
    L1(t, r, u) → 시군구별 합산 → datetime × (시도_시군구) wide format
    """
    log("STEP 8: 시군구별 합산 wide format 생성...")
    
    sums = {}
    for chunk in pd.read_csv(
        OUT_L1, chunksize=CHUNK, encoding="utf-8-sig",
        usecols=["datetime", "시도", "시군구", "L1_kWh"],
    ):
        chunk["datetime"] = pd.to_datetime(chunk["datetime"], format="mixed", errors="coerce")
        chunk["지역"] = chunk["시도"].astype(str) + "_" + chunk["시군구"].astype(str)
        
        g = chunk.groupby(["datetime", "지역"])["L1_kWh"].sum()
        for (dt, region), val in g.items():
            sums[(dt, region)] = sums.get((dt, region), 0) + val
    
    rows = [{"datetime": dt, "지역": r, "load_kWh": v} for (dt, r), v in sums.items()]
    df = pd.DataFrame(rows)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime")
    
    df_wide = df.pivot(index="datetime", columns="지역", values="load_kWh")
    df_wide.to_csv(OUT_WIDE, encoding="utf-8-sig")
    
    log(f"  Shape: {df_wide.shape} (시간 × 시군구)")
    log(f"  연간 총량: {df_wide.sum().sum()/1e9:.3f} TWh")
    log(f"  저장 완료: {OUT_WIDE}")


# ==============================================================================
# STEP 9: 검증
# ==============================================================================
def step9_verify(gross_df: pd.DataFrame, E: pd.DataFrame) -> None:
    """검증 지표 출력"""
    log("STEP 9: 검증...")
    
    E_total = E["판매량_kWh"].sum()
    
    # 9-1. L1 연간 총량
    L1_total = 0
    for chunk in pd.read_csv(OUT_L1, chunksize=CHUNK, encoding="utf-8-sig",
                             usecols=["L1_kWh"]):
        L1_total += chunk["L1_kWh"].sum()
    
    log(f"  [검증 1] 연간 총량")
    log(f"    E_total  = {E_total/1e9:.6f} TWh")
    log(f"    L1_total = {L1_total/1e9:.6f} TWh")
    log(f"    차이     = {(L1_total - E_total)/1e6:.3f} GWh ({(L1_total/E_total - 1)*100:.4f}%)")
    
    # 9-2. 시간별 정합성 (L1 전국합 vs L_TD)
    gross_df["datetime"] = pd.to_datetime(gross_df["datetime"])
    alpha = E_total / (gross_df["Gross_Standard"].sum() * 1000)  # MWh→kWh
    gross_df["L_TD"] = gross_df["Gross_Standard"] * 1000 * alpha
    
    L1_hourly = {}
    for chunk in pd.read_csv(OUT_L1, chunksize=CHUNK, encoding="utf-8-sig",
                             usecols=["datetime", "L1_kWh"]):
        chunk["datetime"] = pd.to_datetime(chunk["datetime"], format="mixed", errors="coerce")
        g = chunk.groupby("datetime")["L1_kWh"].sum()
        for t, v in g.items():
            L1_hourly[t] = L1_hourly.get(t, 0) + v
    
    df_check = pd.DataFrame({
        "datetime": list(L1_hourly.keys()),
        "L1_sum": list(L1_hourly.values()),
    })
    df_check = df_check.merge(gross_df[["datetime", "L_TD"]], on="datetime")
    df_check["rel_err"] = (df_check["L1_sum"] - df_check["L_TD"]) / df_check["L_TD"]
    
    log(f"  [검증 2] 시간별 상대오차 (L1_sum vs L_TD)")
    log(f"    mean = {df_check['rel_err'].mean():.6f}")
    log(f"    std  = {df_check['rel_err'].std():.6f}")
    log(f"    max  = {df_check['rel_err'].max():.6f}")
    log(f"    MAPE = {df_check['rel_err'].abs().mean()*100:.4f}%")

    # 9-3. 월별 판매량 보존 검증: L1(r,u,m) vs KEPCO E(r,u,m)
    E_monthly = E.groupby(["시도", "시군구", "용도업종", "월"])["판매량_kWh"].sum().to_dict()
    L1_monthly = {}
    for chunk in pd.read_csv(OUT_L1, chunksize=CHUNK, encoding="utf-8-sig",
                             usecols=["시도", "시군구", "용도업종", "월", "L1_kWh"]):
        g = chunk.groupby(["시도", "시군구", "용도업종", "월"])["L1_kWh"].sum()
        for k, v in g.items():
            L1_monthly[k] = L1_monthly.get(k, 0.0) + v
    m_errs = [abs(L1_monthly.get(k, 0.0) - e) / e
              for k, e in E_monthly.items() if e > 0]
    m_errs = np.array(m_errs)
    log(f"  [검증 3] 월별 보존 (L1(r,u,m) vs KEPCO E)")
    log(f"    MAPE = {m_errs.mean()*100:.4f}%  max = {m_errs.max()*100:.4f}%  n = {len(m_errs):,}")


# ==============================================================================
# MAIN: 전체 파이프라인 실행
# ==============================================================================
def main():
    t_start = time.time()
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    log("=" * 70)
    log("전력수요 데이터베이스 구축 파이프라인 시작")
    log("=" * 70)
    
    # 출력 파일 초기화
    for f in [OUT_L0, OUT_L1, OUT_WIDE]:
        if f.exists():
            f.unlink()
    
    # Step 0: Gross Pattern 수정
    gross_df = step0_fix_gross_pattern()
    
    # Step 1: KEPCO 판매량
    E = step1_load_consumption()
    
    # Step 2: 산업분류별 24h 패턴
    industry_profiles = step2_load_profiles()
    
    # Step 3: 매핑표
    mapping = step3_load_mapping(set(industry_profiles["산업분류"]))
    
    # Step 4: 읍면동 산업구조 비중
    industry_share = step4_load_industry_share(set(industry_profiles["산업분류"]))
    
    # Step 5: 시군구×용도업종×월×24h 패턴
    patterns = step5_build_use_patterns(industry_profiles, mapping, industry_share, E)
    
    # Step 6: L0 초기 시계열
    step6_build_L0(E, patterns)
    
    # Step 7: Two-stage proportional scaling
    step7_two_stage_scaling(gross_df, E)
    
    # Step 8: Wide format 출력
    step8_export_wide()
    
    # Step 9: 검증
    step9_verify(gross_df, E)
    
    elapsed = (time.time() - t_start) / 60
    log("=" * 70)
    log(f"파이프라인 완료 (소요시간: {elapsed:.1f}분)")
    log("=" * 70)


if __name__ == "__main__":
    main()
