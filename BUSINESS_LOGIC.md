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
| **NDVI anomaly (current − 5yr climatología)** | `[-0.5, +0.5]` | Realista ±0.3 en regiones cultivadas. Cap ±0.5 absorbe eventos extremos (sequía severa o recuperación pos-quema). Anomaly fuera de ±0.5 ⇒ probable bug en geometry o cobertura insuficiente del baseline |
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
| **NDVI anomaly baseline (5yr cache)** | Por región-mes | 180 | Climatología rota ventana 5yr ~1×/año. Cache hit rate esperado >99% en producción (ver §7.5.4) |
| **NDVI anomaly current month (cache)** | Mensual | 7 | Sentinel-2 revisita ~5d; refresh semanal captura nuevas pasadas sin quemar quota GEE |
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

### 7.5 NDVI Temporal Anomaly Method (`gee/ndvi_anomaly.py`)

Implementado 2026-06-01. Capacidad para validar reportes direccionales CONAB (RECOVERY /
DETERIORATION / STABLE) contra observación satelital independiente. El sistema NO sustituye
a CONAB; lo audita y detecta divergencias (alpha-generating).

#### 7.5.1 ¿Por qué este método?

CONAB publica 4 levantamentos/año revisando producción esperada. Auditorías retrospectivas
(2018-2024) muestran que la magnitud de revisión entre el levantamento 1 y el 4 oscila
entre ±5% y ±15% en años de stress climático. Sin un benchmark observacional independiente,
el sistema no puede distinguir entre:

  - Revisiones agronómicas legítimas (cosecha real más alta/baja de lo esperado)
  - Ajustes de política (CONAB suaviza revisiones por presión institucional)
  - Errores metodológicos arrastrados de levantamentos previos

Satellite NDVI es **observado pre-revisión** y CONAB no puede backfillearlo. Cualquier
divergencia ≥ 1.5σ (modified Z) entre la dirección reportada y la observada se trata como
información asimétrica: el mercado de futuros reacciona al reporte CONAB el día de
publicación, pero la realidad satelital ya estaba presente en los datos crudos semanas
antes.

#### 7.5.2 Climatología Calendar-Aware

Baseline para mes calendario T (1..12) en región S:

    baseline_S(T) = median{ NDVI_S(y, T) : y ∈ [Y-5, Y-1] }

**Mediana, no media:** El NDVI agregado por región contiene outliers crónicos por nubes
mal filtradas (la SCL mask de Sentinel-2 deja artefactos en bordes de nubes y sombras).
La media se contamina con estos píxeles falsamente bajos; la mediana es invariante a
hasta el 50% de outliers (breakdown point 0.5). Coste computacional idéntico server-side
en GEE (`.median()` vs `.mean()` sobre la misma ImageCollection).

**Calendar-aware (no rolling 60-meses):** La fenología del azúcar de caña tiene
seasonalidad fuerte (siembra Sep-Nov, crecimiento Dec-May, cosecha Apr-Nov según región).
Comparar NDVI de Junio contra un rolling de los últimos 60 meses mezcla fases fenológicas
distintas y destruye la información. Comparar Junio contra mediana de Junios pasados
preserva la señal estacional.

**Ventana 5 años:** Compromise entre estabilidad estadística y relevancia agronómica.
<5 años: varianza climática insuficiente para baseline estable. >5 años: incluye régimen
agronómico pre-2020 (prácticas de mecanización distintas en Centro-Sur). Decisión
revisable cada 3-5 años.

**Anti-leakage del target year:** Para `target_year=Y`, el baseline son los 5 años
**previos a Y** (Y−5 a Y−1), NO los 5 años hasta el año actual. Sin esta exclusión,
el year siendo evaluado contribuiría a su propio baseline con peso 1/5 = 20%,
sesgando el anomaly hacia zero. Implementado vía param `target_year` en
`_compute_baseline` (default = `datetime.now().year` para preservar live production
behavior). Crítico para bootstrap retrospectivo y shadow tests.

#### 7.5.3 Coverage Gates Tiered (`_validate_coverage_gate`)

**Per-pixel current month:**

  - `coverage < 30%` → `DataQualityError` crítico. Razón: con <30% píxeles válidos el
    intervalo de confianza del mean estimator (±1.96·σ/√N) excede la magnitud típica de
    la anomalía (±0.05 NDVI vs anomaly esperada ±0.03). Signal-to-noise ratio < 1, la
    señal es indistinguible del ruido.
  - `30% ≤ coverage < 50%` → `logger.warning`, señal computada pero flagged como
    `degraded`. La conviction queda artificialmente baja por varianza inducida por la
    cobertura, no por la anomalía real. Útil para auditoría pero no para trading directo.
  - `coverage ≥ 50%` → `logger.info`, gate silencioso. Cobertura típica Sentinel-2 en
    cerrado brasileño: 60-80%.

**Historical 60-month coverage** (`validate_count` reusado de `data_quality.py`):

  - Umbral 80% (48 de 60 meses con cobertura ≥ `MIN_PIXEL_COV_PER_MONTH = 0.30`). Razón:
    con N=48 observaciones el CI de la mediana muestral ≈ ±1.25σ/√N ≈ ±0.18σ; con N=30
    escala a ±0.23σ, comparable a la magnitud típica de anomalía. Por debajo de N=48 el
    baseline pierde poder discriminatorio entre régimen normal y stress.

#### 7.5.4 Cache Strategy (Two-Tier)

Dos tiers porque los datos tienen ciclos de vida ortogonales:

| Tier | TTL | Justificación de ingeniería |
|------|-----|------------------------------|
| Baseline (climatología 5yr) | 180 días | El baseline solo cambia cuando rotamos la ventana 5yr (~1×/año). Coste: 60 server-side reductions × 5 regiones × 12 meses = ~720 ops por bootstrap completo. Cache hit rate esperado >99% en producción |
| Current month NDVI | 7 días | Sentinel-2 revisita ~5d. Refresh semanal captura nuevas pasadas sin quemar quota GEE. TTL más corto = sobrecoste sin ganancia informativa |

**Schema versioning:** El sufijo `_v{CACHE_SCHEMA_VERSION}` en filename permite invalidación
atómica si cambiamos formato del JSON. Bumping la versión hace que ningún archivo viejo
coincida con el nuevo path → recomputo forzado sin migración explícita.

**Append-only history JSON:** Deduplicación por `(year, month)` permite re-cómputo
idempotente. Atomic write (`.tmp` → `rename`) garantiza que un crash mid-write no corrompe
el archivo de history.

**Server-side reduce config** (`_reduce_to_region_mean`):
  - `scale=250m` (`REDUCE_SCALE_METERS`): estándar MODIS para NDVI estatal. São Paulo
    @100m son ~25M píxeles → GEE "User memory limit exceeded". @250m son ~4M (cómodo).
    Granularidad apropiada para agregación estado: campos típicos de caña 50-500 ha =
    500m-2km, mucho mayor que 250m.
  - `tileScale=4` (`REDUCE_TILE_SCALE`): divide la reducción en sub-tiles server-side
    para más memory headroom. Compatible con `bestEffort=True`.
  - **Clamp coverage a [0, 1]**: las dos reducciones independientes (NDVI count vs
    `Image.constant(1)` count) pueden diferir <1% por grid/projection alignment con
    `bestEffort=True`. La discrepancia se clampea y se loguea `DEBUG` solo si excede
    1.005 (detección de drift estructural mayor).

**Rolling history window:** `_load_anomaly_history` retorna las últimas
`HISTORY_WINDOW_MONTHS = 24` observaciones (no all-time). 24 meses cubren 2 zafras
brasileñas completas (Abril-Marzo), suficiente para que `robust_stats` produzca percentile
estable. Si la historia tiene <24 entries, retorna lo disponible y `robust_stats` decide
si declarar `INSUFFICIENT_DATA` (umbral interno: n<10).

#### 7.5.5 Matriz de Clasificación Market Signal

Función `_classify_market_signal(percentile, conab_direction)`:

| Percentile NDVI | CONAB direction | Market Signal | Interpretación |
|-----------------|-----------------|---------------|----------------|
| `P ≥ 70` | `RECOVERY` | `CONFIRMATION` | Satellite valida recuperación reportada |
| `P ≤ 30` | `DETERIORATION` | `CONFIRMATION` | Satellite valida deterioro reportado |
| `P ≤ 30` | `RECOVERY` | `DIVERGENCE_BEARISH` | CONAB optimista; satellite contradice → alpha bajista |
| `P ≥ 70` | `DETERIORATION` | `DIVERGENCE_BULLISH` | CONAB pesimista; satellite contradice → alpha alcista |
| `30 < P < 70` | cualquiera | `NEUTRAL` | Anomalía en rango ordinario, sin convicción |
| cualquiera | `STABLE` | `NEUTRAL` | Sin señal direccional CONAB que comparar |
| `None` | cualquiera | `NEUTRAL` | Bootstrap incompleto / insufficient history |

**Umbrales 70/30 — racional:**
  - Más permisivo (e.g. 60/40): genera muchas señales pero la mayoría son ruido
    (modified_z típico ±0.5, no accionable bajo el AND gate de `robust_stats`).
  - Más estricto (e.g. 80/20): mejora marginalmente la correlación con futures returns
    pero pierde demasiadas oportunidades.
  - Decisión revisable tras shadow test contra CONAB 2024-2025 (Step F del plan).

#### 7.5.6 Bootstrap Inicial

Primera corrida sin historial → `robust_stats` retorna `INSUFFICIENT_DATA` → `conviction
= "INSUFFICIENT_DATA"` → señal no accionable. Solución: script
`scripts/bootstrap_ndvi_anomaly_history.py` (a crear en Step E) que computa
retroactivamente 24 meses de anomalías por región.

  - 24 meses = 2 zafras completas brasileñas (Abril-Marzo)
  - 5 regiones × 24 meses = 120 cómputos GEE, ~3-5 minutos one-shot
  - Idempotente: re-ejecución produce los mismos datos (climatología y NDVI mensual son
    determinísticos modulo updates en S2 reprocessing)
  - Orden: ejecutar tras Step E (smoke test exitoso) antes de Step F (shadow test)

#### 7.5.7 Audit Logging

Formato estandarizado (cumple BUSINESS_LOGIC §5):

```
INFO     gee.ndvi_anomaly | Benchmark GEE vs CONAB | Region=BR_SP | Percentile=78.0 | mZ=+2.15
INFO     gee.ndvi_anomaly | -> Market Confirmation | direction=RECOVERY, P=78 ≥ 70
WARNING  gee.ndvi_anomaly | -> Market DIVERGENCE_BEARISH | CONAB=RECOVERY, satellite P=23 (below baseline)
```

`CONFIRMATION` se loguea a **INFO** (evento esperado, ruido bajo). `DIVERGENCE_*` se loguea
a **WARNING** (señal alpha, debe llegar a sistemas de alertas).

#### 7.5.8 Inferencia CONAB — Lógica Jerárquica + Apertura de Zafra

Implementado en `_infer_conab_direction()`. Schema real (models/market_data.py:431):
`season VARCHAR, levantamento INT, pub_date DATE, sugar_total_mt NUMERIC,
revision_sugar_pct NUMERIC`.

**Lógica jerárquica de 3 niveles:**

##### 1. PRIMARY — `revision_sugar_pct` presente

Δ pre-computado por CONAB con metodología oficial. Se usa siempre que esté presente
(típicamente en `levantamento ∈ {2, 3, 4}` de cada zafra).

##### 2. APERTURA DE ZAFRA — `revision_sugar_pct=NULL` y `levantamento=1`

CONAB **no emite revision en el primer levantamento de cada zafra** (no existe
levantamento previo de la misma temporada con el que comparar). Decisión arquitectural
2026-06-01: cuando esto ocurre, el sistema **NO compara contra el levantamento final
de la zafra anterior** (`lev=4`) — eso sería *phase-mixing*: mezclar un forecast
pre-cosecha contra un cierre retrospectivo, con metodologías y niveles de confianza
incompatibles.

En su lugar: **YoY same-phase comparison**. Comparar `lev=1` actual contra `lev=1`
de la zafra **inmediatamente anterior**:

    Δ = (sugar_total_mt[season, lev=1] − sugar_total_mt[prior_season, lev=1])
          / sugar_total_mt[prior_season, lev=1] × 100

**Por qué este enfoque es correcto:**

| Aspecto | lev=1 vs lev=4 ❌ | lev=1 vs lev=1 ✅ |
|---------|------------------|-------------------|
| Horizonte de forecast | Mezclado (pre-cosecha vs retrospectivo) | Idéntico (ambos pre-cosecha) |
| Metodología CONAB | Distinta (forecast vs final observation) | Idéntica |
| Fracción de zafra cosechada | 0% vs ~100% | 0% vs 0% |
| Información capturada | Sesgo de revisión de zafra anterior | Shock macroeconómico real |
| Estándar de industria | — | Wilmar, Cargill, Alvean usan este |

**Implicación operacional:** durante el año (lev 2-3-4) el sistema mide el **pulso
del mercado** (ajustes marginales mes a mes). En abril (lev=1) cambia automáticamente
a medir el **shock macroeconómico anual** (clima 12 meses antes, área plantada,
mandato etanol). Comportamiento bimodal documentado, no es un bug.

##### 3. FALLBACK STABLE — todos los demás casos

Retorna `("STABLE", inferred=True)` con `logger.warning`. Casos:

  - Tabla `conab_cana_levantamento` inaccesible (ImportError o SQLAlchemyError)
  - `len(rows) < 1` — no hay levantamentos en DB
  - `pub_date` del último es NULL
  - Último levantamento > 130 días (§4 freshness)
  - APERTURA pero `prior_season lev=1` no existe en DB (bootstrap incompleto)
  - `season` no parseable a formato `YYYY/YY` (`_prior_season` retorna None)
  - `revision_sugar_pct=NULL` en `lev != 1` (anomalía de datos: CONAB **debe** emitir
    revision en lev 2/3/4; si no está, los datos son sospechosos)
  - `sugar_total_mt` NULL o cero

**Threshold |Δ| ≥ 3%** para RECOVERY / DETERIORATION (estable bajo ese umbral). Aplica
tanto a `revision_sugar_pct` como al delta YoY de apertura — ambos miden cambio
porcentual de producción esperada y comparten umbral consistente.

**Por qué la política de no-raise:** una falla en la inferencia CONAB no debe romper
el benchmark NDVI entero. STABLE produce `market_signal = NEUTRAL` (ver matriz §7.5.5),
que es la degradación elegante: el sistema reconoce que no tiene base para señal
direccional pero sigue calculando el anomaly observable. Peor consecuencia: perder
oportunidades. Nunca: generar falsas alarmas.

**Auditoría:** el log incluye el `source` exacto utilizado:
  - `revision_pct` — vía PRIMARY
  - `yoy_apertura (vs YYYY/YY lev=1: NN.NNMt)` — vía APERTURA
  - WARNING explícito con razón cuando fallback STABLE

---

## 8. Lo que NO está en este documento (aún)

A medida que se refactoricen los siguientes módulos en Fases 2-4, añadir secciones:
- Scoring 12 criterios (A1-D3): qué mide cada uno, por qué los umbrales
- World Balance Model: lógica de STU%, weight drift, country balance
- Entry Zone Logic: Fibonacci, swing, VWAP bands
- Macro Signals: las 20+ señales y sus dependencias de datos

---

## 9. Quick Reference: Market Signal Matrix (NDVI vs CONAB)

> _Referencia rápida — la lógica completa con justificaciones está en §7.5._

**Inputs:**
  - `Percentile`: ranking del anomaly NDVI actual sobre el rolling history (24 meses, ver §7.5.4)
  - `CONAB direction`: dirección del último reporte CONAB (inferida automáticamente desde
    `conab_cana_levantamento`, o pasada explícita por el caller)

**Output:** `market_signal` ∈ {`CONFIRMATION`, `DIVERGENCE_BULLISH`, `DIVERGENCE_BEARISH`, `NEUTRAL`}

```
                            ┌────────────────────────────────────────────────────────────┐
                            │                      CONAB direction                       │
                            ├──────────────────┬──────────────────┬─────────────────────┤
                            │     RECOVERY     │  DETERIORATION   │       STABLE        │
┌──────────────┬────────────┼──────────────────┼──────────────────┼─────────────────────┤
│   Percentile │   P ≥ 70   │  CONFIRMATION    │  DIVERGENCE_BULL │     NEUTRAL         │
│   NDVI       │ 30 < P <70 │  NEUTRAL         │  NEUTRAL         │     NEUTRAL         │
│   anomaly    │   P ≤ 30   │  DIVERGENCE_BEAR │  CONFIRMATION    │     NEUTRAL         │
│              │   None     │  NEUTRAL         │  NEUTRAL         │     NEUTRAL         │
└──────────────┴────────────┴──────────────────┴──────────────────┴─────────────────────┘
```

**Acciones por signal:**

| Signal | Severidad log | Acción |
|--------|--------------|--------|
| `CONFIRMATION` | `INFO` | Validación; sin trade direccional adicional |
| `DIVERGENCE_BULLISH` | `WARNING` | Alpha alcista: CONAB pesimista pero satélite alcista. Llegar a sistema de alertas |
| `DIVERGENCE_BEARISH` | `WARNING` | Alpha bajista: CONAB optimista pero satélite contradice |
| `NEUTRAL` | `INFO` | No accionable. Sin convicción direccional |

**Gates de calidad antes de aceptar la señal:**

  1. `pixel_coverage_pct ≥ 30%` (per-pixel mes actual) — si no, `DataQualityError` crítico
  2. `historical_coverage_pct ≥ 80%` (60-month baseline) — si no, `DataQualityError`
  3. `n_historical_obs ≥ 10` para que `robust_stats` no devuelva `INSUFFICIENT_DATA`
  4. `|anomaly_value| ≤ 0.5` — si no, probable bug en geometry o cobertura

**Umbrales actuales** (revisables tras shadow test del Step F):

| Constante | Valor | Donde está |
|-----------|-------|-----------|
| `PERCENTILE_HIGH` | 70.0 | `gee/ndvi_anomaly.py` |
| `PERCENTILE_LOW` | 30.0 | `gee/ndvi_anomaly.py` |
| `COVERAGE_CRITICAL_THRESHOLD` | 0.30 | `gee/ndvi_anomaly.py` |
| `COVERAGE_WARNING_THRESHOLD` | 0.50 | `gee/ndvi_anomaly.py` |
| `MIN_HISTORICAL_COVERAGE` | 0.80 | `gee/ndvi_anomaly.py` |
| `BASELINE_YEARS_WINDOW` | 5 | `gee/ndvi_anomaly.py` |
| `HISTORY_WINDOW_MONTHS` | 24 | `gee/ndvi_anomaly.py` |
