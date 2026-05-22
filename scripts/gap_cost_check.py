"""
Comprueba el coste exacto de rellenar los huecos del continuo SB.c.0
usando contratos trimestrales _Z individuales (instrument_id exacto).

Muestra 3 casos representativos para estimar el coste total.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

import databento as db
import pandas as pd
from pathlib import Path
from config import DATABENTO_API_KEY

DEF_PATH = Path("data/databento_raw/sb_fut_definition.dbn.zst")
DATASET  = "IFUS.IMPACT"

def load_z_contracts():
    store = db.DBNStore.from_file(str(DEF_PATH))
    df    = store.to_df()
    z     = df[df["raw_symbol"].str.endswith("_Z", na=False)].copy()
    z["expiration"] = pd.to_datetime(z["expiration"], utc=True, errors="coerce")
    return z[["raw_symbol", "instrument_id", "expiration"]].sort_values("expiration").reset_index(drop=True)


def check_cost(client, instrument_id: int, start: str, end: str) -> float | None:
    try:
        cost = client.metadata.get_cost(
            dataset=DATASET,
            symbols=[str(instrument_id)],
            stype_in="instrument_id",
            schema="ohlcv-1m",
            start=start,
            end=end,
        )
        return cost
    except Exception as e:
        return f"ERROR: {e}"


def main():
    z = load_z_contracts()
    print(f"Contratos _Z disponibles: {len(z)}")
    print()

    client = db.Historical(DATABENTO_API_KEY)

    # --- Casos representativos ---
    # Buscamos contratos con expiracion en distintos años para cubrir el rango 2019-2025
    # FMV_Z expira en ~Sep → cubre Jul-Sep (julio es el hueco principal)
    # FMH_Z expira en ~Feb → cubre Oct-Feb (Oct/Nov/Dic son huecos)

    test_cases = []

    # FMV_Z: tomar uno de ~2022 (expira Sep 2022) y probar julio 2022
    fmv = z[z["raw_symbol"].str.contains("FMV", na=False)]
    for _, row in fmv.iterrows():
        exp = row["expiration"]
        if pd.isna(exp):
            continue
        yr = exp.year
        if 2021 <= yr <= 2023:
            test_cases.append({
                "label":  f"{row['raw_symbol']} → julio {yr}",
                "id":     int(row["instrument_id"]),
                "start":  f"{yr}-07-01",
                "end":    f"{yr}-08-01",
            })
            break

    # FMH_Z: tomar uno de ~2023 (expira Feb 2023) y probar oct/nov/dic 2022
    fmh = z[z["raw_symbol"].str.contains("FMH", na=False)]
    for _, row in fmh.iterrows():
        exp = row["expiration"]
        if pd.isna(exp):
            continue
        yr = exp.year
        if 2022 <= yr <= 2024:
            prev = yr - 1
            test_cases.append({
                "label":  f"{row['raw_symbol']} → oct {prev}",
                "id":     int(row["instrument_id"]),
                "start":  f"{prev}-10-01",
                "end":    f"{prev}-11-01",
            })
            test_cases.append({
                "label":  f"{row['raw_symbol']} → nov-dic {prev}",
                "id":     int(row["instrument_id"]),
                "start":  f"{prev}-11-01",
                "end":    f"{yr}-01-01",
            })
            break

    print(f"{'CASO':<45}  {'COSTE':>10}")
    print("-" * 58)

    total_sample = 0.0
    for case in test_cases:
        cost = check_cost(client, case["id"], case["start"], case["end"])
        if isinstance(cost, float):
            total_sample += cost
            print(f"  {case['label']:<43}  ${cost:>8.4f}")
        else:
            print(f"  {case['label']:<43}  {cost}")

    print()
    print(f"Muestra (3 meses): ${total_sample:.4f}")

    # Escalar al total: ~28 meses faltantes (4 meses × 7 años)
    if total_sample > 0:
        avg_per_month = total_sample / len([c for c in test_cases if isinstance(check_cost(client, c["id"], c["start"], c["end"]), float) or True])
        estimated_total = total_sample / 3 * 28
        print(f"Estimacion total (28 meses): ${estimated_total:.2f}")
    else:
        print("Coste $0.00 → los contratos _Z no tienen datos 1m en esos periodos.")
        print("Los huecos son estructurales: ICE no reporta barras 1m para esos meses.")


if __name__ == "__main__":
    main()
