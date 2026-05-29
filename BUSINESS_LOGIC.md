# SGT Trading — Business Logic & Data Quality Invariants

**Última actualización:** 2026-05-29
**Mantenedor:** Documento vivo. Toda modificación de lógica de mercado o gates de calidad debe
actualizar este archivo en el mismo commit (Principio 4 del Contrato de Arquitectura).

---

## 1. Filosofía

Este documento es la fuente de verdad sobre **por qué** el sistema toma las decisiones que toma.
El código describe el **cómo**; este archivo describe el **por qué**.

Si el código y este archivo divergen, asumimos que el código está mal y este archivo describe la
intención correcta. Cualquier PR que cambie lógica de mercado o data quality sin actualizar este
archivo se considera incompleta.

---

## 2. Frontera del Sistema y Data Quality Gates

### 2.1 Dónde aplican los validators

Los Data Quality Gates (`services/data_quality.py`) aplican en la **frontera de ingestion**:
el punto exacto donde un dato externo entra al sistema. Específicamente:

- Después de descargar de yfinance, antes del upsert a `price_history`
- Después del scraping de MAPA/CEPEA/Santos, antes del upsert
- Después de la query a GEE/USDA/CFTC API, antes del upsert
- Después de leer de DB para servir a un signal — **solo freshness**, no range (el range ya se validó al insertar)

### 2.2 Dónde NO aplican

- Computaciones internas entre módulos (servicios consumiendo otros servicios)
- Tests unitarios (se validan con asserts directos, no con DQ Gates)
- Cálculos derivados (Z-scores, ratios) — los rangos no son fijos para derivados

### 2.3 Política de error

| Nivel | Comportamiento | Justificación |
|-------|---------------|---------------|
| **Source-level** (toda la fuente falla) | `DataQualityError` propaga al pipeline; pipeline loguea y continúa con siguiente fuente | Una fuente caída no debe parar las demás (Brasil MAPA caído no debe romper CFTC) |
| **Row-level** (una fila corrupta entre muchas) | `check_or_log(on_error="warn")` registra y saltea | Una fila mal parseada no debe descartar las otras 200 buenas |
| **Structural** (cambio de formato → < 50% de filas parsean) | `validate_count` raise → fuente falla completa | Si el HTML de CEPEA cambió, los datos parseados son sospechosos en bloque |

---

## 3. Invariantes de Rango por Fuente

Todos los rangos justificados por datos históricos del azúcar (1985-2026). Más holgados que la
realidad histórica para permitir eventos extremos (no nos queremos perder un 1974), pero estrictos
suficiente para rechazar errores obvios (precios negativos, NDVI = 1.8).

### 3.1 Precios futuros (ICE No.11)

| Campo | Rango válido (c/lb) | Justificación |
|-------|--------------------|---------------|
| `close`, `open`, `high`, `low` | `[1.0, 100.0]` | Mínimo histórico ~4 c/lb (1985); máximo ~45 c/lb (1974). Margen 2× para tail events. Rechazar 0 (bug split adjustment) y >100 (error de tick) |
| `volume` | `[0, 1_000_000]` contratos | Volumen diario típico SB: 30k-200k. Cap superior conservador |
| **Δ día-a-día detectado** | `|ΔClose|/Close < 0.30` | Movimientos >30% intradía son limit moves; >50% es casi siempre dato corrupto (split, ticker mismatch). Si se dispara: warning, no raise |

### 3.2 Producción Brasil (MAPA, quincenal)

| Campo | Rango válido | Justificación |
|-------|--------------|---------------|
| `cane_crushed_t` por quincena nacional | `[0, 100_000_000]` t | Pico Centro-Sur: ~50 Mt/quincena. Cap 2× |
| `sugar_t` por quincena | `[0, 5_000_000]` t | Pico: ~3 Mt/quincena |
| `ethanol_total_m3` | `[0, 5_000_000]` m³ | Pico: ~2.5 Mm³/quincena |
| `sugar_mix_pct` | `[20.0, 60.0]` % | Mix típico Brasil: 35-50%. Fuera de [20, 60] = error de parser |

### 3.3 CEPEA (precios físicos Brasil)

| Serie | Rango válido | Justificación |
|-------|--------------|---------------|
| `hydrous_paulinia_usd_m3` | `[50.0, 2000.0]` US$/m³ | Histórico: $250-900 |
| `hydrous_fuel_usd_liter` | `[0.05, 2.0]` US$/L | Coherente con m³ |
| `anhydrous_usd_liter` | `[0.05, 2.5]` US$/L | Anhídrico premium sobre hidratado |
| `crystal_sugar_usd_bag50kg` | `[1.0, 100.0]` US$/bolsa | Histórico: $10-35 |
| **`pct_daily/weekly/monthly`** | `[-50.0, 50.0]` % | Cambio % razonable |

### 3.4 COT (CFTC posicionamiento)

| Campo | Rango válido | Justificación |
|-------|--------------|---------------|
| `mm_net` (managed money) | `[-500_000, 500_000]` contratos | Histórico: -200k a +300k |
| `speculator_net` (legacy) | `[-500_000, 500_000]` | Histórico similar |
| `comm_net` (comerciales) | `[-500_000, 500_000]` | Comerciales típicamente cortos |

### 3.5 GEE (NDVI, LST, SPI)

| Métrica | Rango válido | Justificación |
|---------|--------------|---------------|
| NDVI | `[-1.0, 1.0]` | Definición física estricta. Para crops cultivados: típicamente [0.2, 0.9] |
| LST (Land Surface Temp) | `[273.15, 333.15]` Kelvin | 0°C a 60°C. India/Brasil bands sugar dentro de esto |
| SPI-3, SPI-90 | `[-4.0, 4.0]` | Standardized Precipitation Index, definición estadística |

### 3.6 Ship tracking (Santos, Paranaguá)

| Campo | Rango válido | Justificación |
|-------|--------------|---------------|
| `weight_t`, `load_qty_t` por barco | `[0, 200_000]` t | Bulker grande: 80k DWT; Capesize: 180k DWT |

### 3.7 USDA PSD (balance global)

| Campo | Rango válido | Justificación |
|-------|--------------|---------------|
| Producción mundial total (Mt) | `[100.0, 250.0]` Mt | Histórico: 150-185 |
| Producción país individual (Mt) | `[0.0, 50.0]` Mt | Brasil ≤ 45 Mt, India ≤ 38 Mt |
| STU (Stocks-to-Use, %) | `[10.0, 70.0]` % | Histórico: 25-55 |

### 3.8 ENSO ONI

| Campo | Rango válido | Justificación |
|-------|--------------|---------------|
| ONI value | `[-3.5, 3.5]` | Histórico: -2.3 (La Niña 1973) a +2.6 (El Niño 1997) |

---

## 4. Invariantes de Freshness

`max_age_days` por fuente. Si la última fila en DB excede este umbral al momento de servir
una señal, el caller debe degradar o abortar (no usar dato stale silenciosamente).

| Fuente | Frecuencia natural | `max_age_days` | Notas |
|--------|---------------------|----------------|-------|
| yfinance price_history (SB) | Diaria (días hábiles) | 5 | Permite weekend + festivo lunes |
| price_bars intraday (30m) | Continua | 1 | Cualquier delay >1 día es problema |
| CFTC COT | Semanal (martes) | 14 | Permite festivos US |
| CEPEA | Diaria | 5 | Festivos Brasil |
| MAPA brazil_production | Quincenal | 35 | 2 quincenas de margen |
| ICE futures (deferred contracts) | Diaria | 7 | Liquidez baja → gaps ocasionales |
| Santos port | Tiempo real (scraping) | 2 | Cualquier gap >48h = scraping roto |
| Paranaguá | Tiempo real | 2 | Idem |
| INPE fires | Diaria | 5 | Latencia satelital ~24h |
| ONI ENSO (NOAA) | Mensual | 50 | NOAA publica con 1 mes lag |
| USDA WASDE | Mensual (día 8-12) | 50 | Comparable con ONI |
| USDA PSD | Trimestral | 130 | Latencia mayor |
| CONAB cana levantamento | 4× al año | 130 | abril/agosto/diciembre/marzo |
| NDVI Sentinel-2 | ~10 días por revisita | 21 | Tolerancia cloud cover |
| GEE LST/SPI | Continua | 14 | Cómputo on-demand |
| OCSB Thailand | Trimestral | 130 | Latencia oficial |
| ISMA India | Mensual durante crush | 60 | Off-season: 180 |

---

## 5. Política de Logging

Estandar (Principio 5 del Contrato):

```
logger = logging.getLogger(__name__)
```

Formato global (en `daily_pipeline.py`):
```
%(asctime)s %(levelname)-8s %(name)s - %(message)s
```

Severidades:
- **DEBUG** — info diagnóstica (columnas de XLS, valores intermedios)
- **INFO** — eventos esperados (filas insertadas, fuente completada)
- **WARNING** — degradación recuperable (fila parser falló, source retry exitoso)
- **ERROR** — DataQualityError, fuente completamente caída, datos rechazados
- **CRITICAL** — fallo de scoring engine, DB corruption

Nunca aceptable:
- `except Exception: pass` sin log
- `except Exception: return None` sin log al menos en WARNING
- `print()` en código de producción (solo en scripts/debug)

---

## 6. Pipeline Behavior

Pipeline (`scripts/daily_pipeline.py`) tiene try/except por fuente. Cada `DataQualityError`
propagada desde un módulo de ingestion:

1. Se captura en el try/except de la fuente correspondiente
2. Se loguea como ERROR (no como warning)
3. El pipeline continúa con la siguiente fuente
4. Si N fuentes ≥ 3 fallan en una corrida: alerta agregada (futuro)

`scripts/score_today.py` consume datos de DB. Si un signal module recibe `None` por
freshness fallida:
- Devuelve `score = None` (NO falsea a 0 ni a 1)
- El sistema de scoring trata `None` como "criterio no evaluable"
- El umbral de decisión se ajusta proporcionalmente

---

## 7. Decisiones de Lógica de Mercado (a expandir)

> _Esta sección crece a medida que tocamos cada módulo. Cada modificación de lógica de mercado
> debe añadir una entrada aquí explicando el **porqué** de la regla._

### 7.1 Term Structure & Calendar Spreads

- **Backwardation estructural vs estadística** (`services/curve_analysis.py`):
  Spreads como H27/K27 (India/Thailand peak vs Brazil start) tienen backwardation natural
  por estacionalidad de cosecha. NO aplicar fade automático: clasificar como
  `FUNDAMENTAL_LONG_NEAR` y dejar la dirección a la vista fundamental India/TH.
  Justificación: 2026-05-29 análisis con usuario, cosecha India peak Oct-Mar coincide con
  K27 = Brazil start, premium es estructural.

- **Full carry estimado**: SOFR (4.5%) × precio / 12 + storage (0.012 c/lb/mes) + quality
  (0.003 c/lb/mes). Justificación: ICE Rule 11.20 + financing rate corriente.

### 7.2 COT Percentile Model

- **Managed Money (mm_net)** preferido sobre **Legacy Speculators (speculator_net)**:
  mm_net captura hedge funds + CTAs puros; speculator_net incluye small specs ruidosos.
  Justificación: CFTC Disaggregated más preciso para señal contrarian.
- **Ventana 3 años (156 semanas)** para percentile primary, no all-time:
  Las características del posicionamiento cambian con el régimen de mercado (post-2020 vs
  pre-2020). 3 años captura el régimen actual sin ruido pre-COVID.

### 7.3 Robust Statistics (Z-scores)

- **No usar Z-score estándar** sobre distribuciones de spreads/COT (`services/stats_utils.py`):
  Las distribuciones tienen colas gordas. Std clásico se infla con outliers históricos.
  Solución: percentile rank (distribution-free) + modified Z (MAD-based, robust).
  AND gate: HIGH conviction requiere ambas capas extremas.
  Justificación: 2026-05-29 sesión con usuario, riesgo de fat tail signals.

### 7.4 GEE India Calibration

- **Yield coefficient India = 0.3930** (no 0.3593):
  Calibrado vs producción **gross** (no net) usando OLS sobre ISMA 2018-2024. RMSE 3.12 Mt.
  Justificación: 2026-05-28 análisis post-mortem India 2024, descubrió que SPI estaba
  midiendo monzón equivocado; recalibración usando gross alinea con metodología ISMA.

---

## 8. Lo que NO está en este documento (aún)

A medida que se refactoricen los siguientes módulos en Fases 2-4, añadir secciones:
- Scoring 12 criterios (A1-D3): qué mide cada uno, por qué los umbrales
- World Balance Model: lógica de STU%, weight drift, country balance
- Entry Zone Logic: Fibonacci, swing, VWAP bands
- Macro Signals: las 20+ señales y sus dependencias de datos
