"""
KPG 193 모선별 부하 할당
- 229개 시군구 수요 → 193개 모선에 역거리 가중(IDW, k=3) 할당
- 각 시군구를 가장 가까운 3개 모선에 거리 제곱의 역수 비례로 배분

입력:
  - sigungu_timeseries_wide.csv (8784 × 229)
  - bus_location.csv (193 모선 좌표)
  - sigungu_coordinates_master.csv (229 시군구 좌표)

출력:
  - kpg193_demand_2024.csv (8784 × 193)

실행:
  python src/allocate_kpg193.py
"""

import pandas as pd
import numpy as np
from pathlib import Path
from scipy.spatial import cKDTree
import time

BASE     = Path(__file__).resolve().parent.parent   # working directory root
RAW_GEO  = BASE / "data" / "raw" / "geo"
PROC_DIR = BASE / "data" / "processed"

# =============================================================================
# 1. 시군구 좌표 생성 (시청/군청 소재지)
#    → sigungu_coordinates.csv 파일이 있으면 로드, 없으면 생성
# =============================================================================
def get_sigungu_coordinates(sigungu_names: list) -> pd.DataFrame:
    coord_file = RAW_GEO / "sigungu_coordinates_master.csv"
    df = pd.read_csv(coord_file, encoding="utf-8-sig")
    df["key"] = df["Region"].str.strip()
    return df.set_index("key")[["y", "x"]].rename(columns={"y": "lat", "x": "lon"})

# =============================================================================
# 2. IDW 할당 행렬 계산 (k=3, 거리 제곱 반비례)
# =============================================================================
def compute_allocation_matrix(
    sgg_coords: pd.DataFrame,
    bus_coords: pd.DataFrame,
    k: int = 3,
) -> pd.DataFrame:
    """
    각 시군구를 k개 최근접 모선에 역거리가중(IDW)으로 할당하는 행렬 생성.
    
    Returns:
        alloc: DataFrame (229 시군구 × 193 모선), 각 행의 합 = 1.0
    """
    print(f"  IDW 할당 행렬 계산 (k={k})...")
    
    # KD-tree for fast nearest neighbor
    bus_tree = cKDTree(bus_coords[["Latitude", "Longitude"]].values)
    
    alloc = pd.DataFrame(0.0, index=sgg_coords.index, columns=bus_coords.index)
    
    for sgg_name, row in sgg_coords.iterrows():
        point = [row["lat"], row["lon"]]
        
        # k개 최근접 모선 찾기
        distances, indices = bus_tree.query(point, k=k)
        
        # 거리가 0인 경우 처리 (정확히 같은 좌표)
        distances = np.maximum(distances, 1e-6)
        
        # 역거리 제곱 가중치
        weights = 1.0 / (distances ** 2)
        weights = weights / weights.sum()  # 정규화
        
        for dist, idx, w in zip(distances, indices, weights):
            bus_id = bus_coords.index[idx]
            alloc.loc[sgg_name, bus_id] = w
    
    # 검증
    row_sums = alloc.sum(axis=1)
    print(f"  할당 행렬 shape: {alloc.shape}")
    print(f"  행 합 검증: min={row_sums.min():.6f}, max={row_sums.max():.6f}")
    
    return alloc


# =============================================================================
# 3. 시계열 할당
# =============================================================================
def allocate_demand(
    wide_df: pd.DataFrame,
    alloc: pd.DataFrame,
    bus_coords: pd.DataFrame,
) -> pd.DataFrame:
    """
    시군구별 시계열에 할당 행렬을 적용하여 모선별 시계열 생성.
    
    demand_bus(t, b) = Σ_r alloc(r, b) × demand_sgg(t, r)
    """
    print("  시계열 할당 중...")
    
    # 시군구 컬럼 순서를 할당 행렬과 맞춤
    common_sgg = [c for c in wide_df.columns if c in alloc.index]
    missing_sgg = [c for c in wide_df.columns if c not in alloc.index]
    
    if missing_sgg:
        # 좌표 누락 시군구가 있으면 해당 수요가 조용히 누락되므로 즉시 중단한다.
        raise ValueError(f"좌표가 없는 시군구가 있습니다: {missing_sgg}")

    # 행렬 곱: (8784 × 229) × (229 × 193) = (8784 × 193)
    demand_sgg = wide_df[common_sgg].values  # (T, R)
    alloc_matrix = alloc.loc[common_sgg].values  # (R, B)

    demand_bus = demand_sgg @ alloc_matrix  # (T, B)

    # bus 컬럼명
    bus_names = [f"bus_{int(b)}" for b in bus_coords["bus_id"]]

    result = pd.DataFrame(demand_bus, index=wide_df.index, columns=bus_names)

    # 할당 전후 총량 보존 검증
    if not np.isclose(result.to_numpy().sum(), wide_df.to_numpy().sum(), rtol=1e-10):
        raise RuntimeError("KPG 193 할당 전후 총량이 일치하지 않습니다.")

    print(f"  결과 shape: {result.shape}")
    print(f"  연간 총량: {result.sum().sum()/1e9:.3f} TWh")

    return result


# =============================================================================
# 4. 검증 및 비교
# =============================================================================
def verify_and_compare(
    our_demand: pd.DataFrame,
    bus_coords: pd.DataFrame,
):
    """KPG193 기존 수요와 비교"""
    print("\n=== 검증 ===")
    print(f"  연간 총량: {our_demand.sum().sum()/1e9:.3f} TWh")
    print(f"  시간 수: {len(our_demand)}")
    print(f"  모선 수: {len(our_demand.columns)}")
    
    # 모선별 비중
    bus_share = our_demand.sum() / our_demand.sum().sum() * 100
    print(f"\n  상위 10개 모선 (비중):")
    for bus, share in bus_share.nlargest(10).items():
        bus_id = int(bus.replace("bus_", ""))
        name = bus_coords.loc[bus_coords["bus_id"]==bus_id, "name_Korean"].values[0]
        print(f"    {bus} ({name}): {share:.2f}%")
    
    # 패턴 동일성 확인 (우리 데이터)
    shares_by_time = our_demand.div(our_demand.sum(axis=1), axis=0)
    share_std = shares_by_time.std(axis=0)
    print(f"\n  모선별 비중의 시간 변동성:")
    print(f"    mean std = {share_std.mean():.6f}")
    print(f"    max std  = {share_std.max():.6f}")
    print(f"    → 비중이 시간에 따라 변동 = 지역별 패턴이 다름을 의미")
    
    # KPG193 기존 데이터와 비교 (있으면)
    kpg_file = RAW_GEO / "final_demand_timeseries_2022.csv"
    if kpg_file.exists():
        print(f"\n=== KPG193 기존 수요와 비교 ===")
        kpg = pd.read_csv(kpg_file, index_col=0, parse_dates=True)
        
        # 기존: 비중의 시간 변동성 = 0 (동일 패턴)
        kpg_shares = kpg.div(kpg.sum(axis=1), axis=0)
        kpg_std = kpg_shares.std(axis=0)
        print(f"  기존 KPG193 비중 시간 변동성: mean={kpg_std.mean():.10f}")
        print(f"  본 연구 비중 시간 변동성:     mean={share_std.mean():.6f}")
        print(f"  → 본 연구가 지역별 패턴 차이를 반영함을 확인")


# =============================================================================
# MAIN
# =============================================================================
def main():
    PROC_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    print("=" * 60)
    print("KPG 193 모선별 부하 할당 (IDW, k=3)")
    print("=" * 60)
    
    # 1. 데이터 로드
    print("\n[1] 데이터 로드...")
    wide_df = pd.read_csv(
        PROC_DIR / "sigungu_timeseries_wide.csv",
        index_col=0, parse_dates=True, encoding="utf-8-sig",
    )
    print(f"  시군구 시계열: {wide_df.shape}")

    bus = pd.read_csv(RAW_GEO / "bus_location.csv")
    print(f"  모선 좌표: {len(bus)}개")
    
    # 2. 시군구 좌표
    print("\n[2] 시군구 좌표...")
    sgg_names = list(wide_df.columns)
    sgg_coords = get_sigungu_coordinates(sgg_names)
    
    # 3. 할당 행렬
    print("\n[3] 할당 행렬 계산...")
    alloc = compute_allocation_matrix(sgg_coords, bus, k=3)
    
    # 할당 행렬 저장 (재사용 가능)
    alloc.to_csv(PROC_DIR / "allocation_matrix_idw_k3.csv", encoding="utf-8-sig")
    print(f"  할당 행렬 저장: allocation_matrix_idw_k3.csv")

    # 4. 시계열 할당
    print("\n[4] 시계열 할당...")
    demand_bus = allocate_demand(wide_df, alloc, bus)

    # 5. 저장
    out_file = PROC_DIR / "kpg193_demand_2024.csv"
    demand_bus.to_csv(out_file, encoding="utf-8-sig")
    print(f"\n  저장: {out_file}")
    
    # 6. 검증
    verify_and_compare(demand_bus, bus)
    
    elapsed = time.time() - t0
    print(f"\n완료 ({elapsed:.1f}초)")


if __name__ == "__main__":
    main()
