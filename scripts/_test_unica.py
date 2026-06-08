import sys; sys.path.insert(0, '.')
import logging; logging.basicConfig(level=logging.WARNING)
from ingestion.unica import get_latest_unica

data = get_latest_unica()
if data:
    sugar_mix = round(100 - data["mix_ethanol_pct"], 2)
    print("safra:        ", data["safra"])
    print("position_date:", data["position_date"])
    print("quinzena:     ", data["quinzena_num"], "a quinzena de mes", data["ref_month"])
    print("mix_ethanol:  ", data["mix_ethanol_pct"], "%")
    print("sugar_mix:    ", sugar_mix, "%")
    print("yoy_sugar:    ", data.get("yoy_sugar_pct"), "%")
    print("idm_source:   ", data["idm_source"])
else:
    print("ERROR: get_latest_unica() returned None")
