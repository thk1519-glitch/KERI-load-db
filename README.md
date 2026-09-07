# KERI-load-db

**Hourly electricity demand database for Korea by district and sector (2024)**
한국 시군구별·업종별 시간단위 전력수요 데이터베이스

Developed at the Korea Electrotechnology Research Institute (KERI).
Built entirely from publicly available statistics — Korea Power Exchange (KPX)
national real-time supply–demand data, KEPCO district-level electricity sales,
and representative industry load patterns — combined via industry-code mapping,
regionally weighted load profiles, and two-stage proportional scaling.

- Spatial resolution: 229 administrative districts (si-gun-gu) / 193 buses (KPG 193)
- Sectoral resolution: 38 KEPCO use-sectors (mapped to 77 KSIC divisions)
- Temporal resolution: 8,784 hours (calendar year 2024)
- District–sector–monthly sales are preserved exactly by construction (0.000%);
  intraday shape residual 0.059% against the transmission-level national load shape.

## Files

| File | Shape | Description |
|---|---|---|
| `data/sigungu_hourly_2024.csv` | 8,784 × 229 | Hourly demand by district (kWh). Columns: `시도_시군구`, index: datetime |
| `data/kpg193_bus_hourly_2024.csv` | 8,784 × 193 | Hourly demand allocated to KPG 193 buses via IDW (k=3) (kWh) |
| `data/kpg193_bus_variability.csv` | 193 rows | Per-bus load-shape metrics (annual/August peak-to-valley ratio, peak hour, share) |
| `data/mapping_use38_ksic77.xlsx` | 82 rows | Standard mapping table: 38 KEPCO use-sectors ↔ 77 KSIC divisions |

## Method (summary)

1. Bottom-up initial series from KEPCO monthly district–sector sales and
   regionally weighted 24-hour industry load patterns.
2. Two-stage proportional scaling: a temporal correction matched to the national
   load shape (KPX), followed by a district–sector–monthly correction that
   preserves KEPCO sales exactly.
3. Bus-level allocation to the KPG 193 synthetic Korean grid by inverse-distance
   weighting (k = 3).

Full methodology: see the citation below.

## Reproduction

Synthesis code is included under `src/`:

| Script | Paper sections | What it does |
|---|---|---|
| `src/build_demand_database.py` | §3–§4 | Loads raw inputs, name normalization and 38↔77 mapping, regionally weighted profiles, initial series L⁰, two-stage proportional scaling (β, γ), verification |
| `src/allocate_kpg193.py` | §5.2 | IDW (k=3) allocation of district demand to the 193 KPG buses |

Steps:
1. Install: `pip install -r requirements.txt` (Python ≥3.10)
2. Obtain the raw inputs listed in `metadata/input_data_manifest.csv` from each
   provider and place them in the `expected_path` layout under a working
   directory (raw statistics are not redistributed here; they remain subject to
   each provider's terms).
3. Run `python src/build_demand_database.py`, then `python src/allocate_kpg193.py`.
   Outputs are written to `data/processed/` in the working directory.

File-name correspondence between script outputs and the released copies:

| Script output (`data/processed/`) | Released copy (`data/`) |
|---|---|
| `sigungu_timeseries_wide.csv` | `sigungu_hourly_2024.csv` |
| `kpg193_demand_2024.csv` | `kpg193_bus_hourly_2024.csv` |
| `allocation_matrix_idw_k3.csv` | `allocation_matrix_idw_k3.csv` |

The full district-sector-hourly table is generated locally as
`data/processed/L1_timeseries_8784.csv` (229 districts × 38 sectors × 8,784 h,
about 5 GB). It is not included in this repository because of its size; the
released district- and bus-level files are aggregated derivatives of it.

Known limitations of the released pipeline are documented in the paper
(weekday/weekend not distinguished; BTM solar not separated; single national
residential profile).

## Sources

Derived from public statistics: KPX Public Electricity Supply-Demand Status
Sharing System (openapi.kpx.or.kr), KEPCO Electricity Sales Statistics by
Municipality, KEPCO Electric Power Big Data Center (bigdata.kepco.co.kr),
KEPCO Management Research Institute representative load patterns.

## License

- Code (`src/`): MIT (see `LICENSE-CODE`)
- Data and mapping table (`data/`): CC BY 4.0 (see `LICENSE`, attribution required)
- Raw input statistics are not redistributed; they remain subject to the terms
  of each providing institution (KPX, KEPCO, etc.).

## Citation

Tae-Hyun Kim, Woo-Nam Lee, Goo-Hyung Jung, and Chang-Soo Kim (2026).
"A Methodology for Constructing Hourly Electricity Demand Database by
Municipality and Sector Using Publicly Available Statistics in Korea."
*The Transactions of the Korean Institute of Electrical Engineers*
(accepted for publication). Repository:
https://github.com/thk1519-glitch/KERI-load-db

## Contact

Kim Tae Hyun, Korea Electrotechnology Research Institute (KERI)
