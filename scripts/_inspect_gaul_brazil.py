"""
Inspección de GAUL para encontrar cómo identifica los estados de Brasil.

Necesario para resolver: el filtro
  ADM0_NAME='Brazil' AND ADM1_NAME='São Paulo'
devuelve null feature en el smoke test de Step E. Hay 3 hipótesis:
  (a) ADM0_NAME no es exactamente 'Brazil' (puede ser BRAZIL, Brasil)
  (b) ADM1_NAME usa ASCII sin acentos ('Sao Paulo' vs 'São Paulo')
  (c) El dataset path es incorrecto

Output: nombre exacto del país y los 27 estados como GAUL los guarda.
"""
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ee
from dotenv import load_dotenv

load_dotenv()
project_id = os.getenv("GEE_PROJECT_ID")
ee.Initialize(project=project_id)

print("=" * 75)
print("  Inspección GAUL — FAO/GAUL/2015/level1")
print("=" * 75)

gaul = ee.FeatureCollection("FAO/GAUL/2015/level1")

# Test 1: ¿Cuántas features tiene el dataset total?
print("\n[1] Total features GAUL level1:")
total = gaul.size().getInfo()
print(f"    {total}")

# Test 2: ¿Cómo está escrito Brazil/Brasil en ADM0_NAME?
print("\n[2] Buscando ADM0 names que contengan 'razil' o 'rasil'...")
br_candidates_a = gaul.filter(ee.Filter.stringContains("ADM0_NAME", "razil"))
n_a = br_candidates_a.size().getInfo()
print(f"    'razil' matches: {n_a}")
if n_a > 0:
    names_a = br_candidates_a.aggregate_array("ADM0_NAME").distinct().getInfo()
    print(f"    distinct ADM0 names: {names_a}")

br_candidates_b = gaul.filter(ee.Filter.stringContains("ADM0_NAME", "rasil"))
n_b = br_candidates_b.size().getInfo()
print(f"    'rasil' matches: {n_b}")
if n_b > 0:
    names_b = br_candidates_b.aggregate_array("ADM0_NAME").distinct().getInfo()
    print(f"    distinct ADM0 names: {names_b}")

# Test 3: Lista completa de estados (usando la variante que funcione)
br_filter = br_candidates_a if n_a > 0 else br_candidates_b
print(f"\n[3] Lista completa de estados (ADM1_NAME) de Brasil:")
br_states = br_filter.aggregate_array("ADM1_NAME").getInfo()
br_states_sorted = sorted(set(br_states))
print(f"    Total: {len(br_states_sorted)}")
for s in br_states_sorted:
    print(f"      - {s!r}")

# Test 4: Verificar el ADM0_CODE de Brazil (también puede usarse como filtro)
print("\n[4] ADM0_CODE de Brazil:")
codes = br_filter.aggregate_array("ADM0_CODE").distinct().getInfo()
print(f"    {codes}")

# Test 5: ¿Existe un campo ISO3 o similar?
print("\n[5] Schema de una feature (para ver TODOS los campos disponibles):")
first = br_filter.first().getInfo()
if first and "properties" in first:
    for k, v in sorted(first["properties"].items()):
        print(f"    {k:>20}: {v!r}")

print("\n" + "=" * 75)
print("Use this output to update CONAB_REGIONS in gee/ndvi_anomaly.py")
print("=" * 75)
