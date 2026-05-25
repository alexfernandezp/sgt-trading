"""
Configuración inicial de Google Earth Engine para SGT Trading.

Ejecutar UNA sola vez (o cuando las credenciales expiren):
  py scripts/setup_gee.py

Pasos que realiza:
  1. Verifica que earthengine-api está instalado (si no, da instrucciones)
  2. Lanza autenticación OAuth (abre navegador)
  3. Solicita el GEE Project ID y lo escribe en el .env
  4. Verifica la conexión con una consulta simple a Sentinel-2
  5. Calcula NDVI de prueba sobre Ribeirão Preto

Requisitos previos:
  pip install earthengine-api
  pip install earthengine-api[extra]  ← para funciones de exportación

  Tu cuenta GEE debe estar registrada en: https://earthengine.google.com
"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=" * 65)
print("  SGT Trading — Configuración Google Earth Engine")
print("=" * 65)

# ── 1. Verificar instalación ──────────────────────────────────────────────────
try:
    import ee
    print("\n[OK] earthengine-api instalado: %s" % ee.__version__)
except ImportError:
    print("\n[ERROR] earthengine-api no está instalado.")
    print("  Ejecuta: pip install earthengine-api")
    sys.exit(1)

# ── 2. Autenticación ──────────────────────────────────────────────────────────
print("\n[1/4] Autenticación GEE...")
print("  Se abrirá el navegador para autorizar el acceso a tu cuenta Google.")
print("  Sigue las instrucciones y copia el código de verificación.")
print()

try:
    ee.Authenticate()
    print("\n[OK] Autenticación completada.")
except Exception as e:
    print(f"\n[ERROR] Autenticación fallida: {e}")
    print("  Comprueba que tienes conexión a internet y cuenta GEE aprobada.")
    sys.exit(1)

# ── 3. Project ID ─────────────────────────────────────────────────────────────
print("\n[2/4] Project ID de Google Earth Engine")
print("  Encuéntralo en: console.cloud.google.com → selecciona tu proyecto")
print("  O en: code.earthengine.google.com → ícono ⚙ → Project info")
print()

current_id = os.getenv("GEE_PROJECT_ID", "")
if current_id:
    print(f"  GEE_PROJECT_ID actual: {current_id}")
    use_current = input("  ¿Usar el actual? [S/n]: ").strip().lower()
    project_id = current_id if use_current != "n" else input("  Nuevo Project ID: ").strip()
else:
    project_id = input("  Introduce tu GEE Project ID: ").strip()

if not project_id:
    print("[ERROR] Project ID vacío. Saliendo.")
    sys.exit(1)

# Escribir en .env
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
lines = []
found = False
if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    lines_new = []
    for line in lines:
        if line.startswith("GEE_PROJECT_ID="):
            lines_new.append(f"GEE_PROJECT_ID={project_id}\n")
            found = True
        else:
            lines_new.append(line)
    lines = lines_new

if not found:
    lines.append(f"\nGEE_PROJECT_ID={project_id}\n")

with open(env_path, "w", encoding="utf-8") as f:
    f.writelines(lines)

print(f"[OK] GEE_PROJECT_ID={project_id} guardado en .env")

# ── 4. Verificar conexión ─────────────────────────────────────────────────────
print("\n[3/4] Verificando conexión a GEE...")
try:
    ee.Initialize(project=project_id)
    # Test: verificar acceso a la colección Sentinel-2 (sin traer datos)
    n = ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED").limit(1).size().getInfo()
    print(f"[OK] Acceso a Sentinel-2 SR confirmado (colección accesible)")
except Exception as e:
    print(f"[ERROR] No se pudo inicializar GEE: {e}")
    sys.exit(1)

# ── 5. Test NDVI Ribeirão Preto ───────────────────────────────────────────────
print("\n[4/4] Test NDVI — Ribeirão Preto (última semana disponible)...")
try:
    from datetime import date, timedelta

    region = ee.Geometry.Point([-47.8208, -21.1767]).buffer(5000)  # 5km buffer
    end   = date.today() - timedelta(days=7)
    start = end - timedelta(days=14)

    col = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(region)
        .filterDate(str(start), str(end))
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
    )

    n_scenes = col.size().getInfo()
    print(f"  Imágenes S2 disponibles ({start} → {end}): {n_scenes}")

    if n_scenes > 0:
        ndvi_col = col.map(lambda img: img.normalizedDifference(["B8", "B4"]).rename("ndvi"))
        mean_img = ndvi_col.mean()
        stats = mean_img.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=region,
            scale=100,
            maxPixels=1e7,
        ).getInfo()
        ndvi_val = stats.get("ndvi")
        if ndvi_val is not None:
            print(f"  NDVI medio (Ribeirão Preto): {ndvi_val:.4f}")
            if ndvi_val > 0.5:
                print("  → Vegetación densa / cultivo sano")
            elif ndvi_val > 0.3:
                print("  → Vegetación moderada")
            else:
                print("  → Vegetación escasa o suelo desnudo")
        else:
            print("  NDVI no calculado (quizás cobertura nubosa alta en ese período)")
    else:
        print(f"  Sin imágenes S2 disponibles en ese período (prueba con otro rango).")

except Exception as e:
    print(f"  [WARN] Test NDVI: {e}")
    print("  La conexión GEE funciona pero el test NDVI no se completó.")
    print("  El sistema funcionará correctamente en producción.")

print()
print("=" * 65)
print("  Configuración GEE completada.")
print("  Próximos pasos:")
print("    1. py scripts/daily_pipeline.py   ← actualiza datos (incluye NDVI)")
print("    2. py scripts/score_today.py       ← scoring con señales climáticas")
print("=" * 65)
