"""
Backtest L2: ¿Mejora el filtro MTF(4h) el precio de entrada?

Para cada señal L1 (SHORT/LONG), compara tres métodos de entrada:
  (A) Inmediata  — cierre del viernes de señal COT (baseline actual)
  (B) MTF-espera — primera barra 4h donde precio < MA20(4h) para SHORT
                   o precio > MA20(4h) para LONG, dentro de 5 días
  (C) Oracle     — mejor precio posible dentro de 5 días (cota teórica máxima)

Todos los métodos salen el mismo día (señal + H días) para comparar
puramente el efecto del precio de entrada, no del timing de salida.

Métricas:
  - Mejora media de entrada (c/lb) y equivalente en R
  - Win rate y avg R a 5d/10d/20d por método
  - % de señales donde MTF mejora la entrada vs empeora
  - % de señales donde MTF nunca se activa en 5d (trades perdidos)
  - Cuándo suele activarse MTF (día 0, +1d, +2d, ...)
"""
import os, sys
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from database import SessionLocal
from sqlalchemy import text

MAX_WAIT_DAYS = 5
MA_PERIOD_4H  = 20
ATR_PERIOD    = 14
HOLD_DAYS     = [5, 10, 20]
SKIP_BARS     = 20   # mínimo días entre señales (sin solapamiento)


# ── Data loading ──────────────────────────────────────────────────────────────

def load_data(session):
    price_rows = session.execute(text(
        "SELECT date, high, low, close FROM price_history "
        "WHERE instrument='SB_CONT' ORDER BY date"
    )).fetchall()

    cot_rows = session.execute(text(
        "SELECT report_date, speculator_net, total_oi "
        "FROM cot_data ORDER BY report_date"
    )).fetchall()

    bars_4h = session.execute(text(
        "SELECT datetime, close FROM price_bars "
        "WHERE instrument='SB_CONT' AND interval='4h' ORDER BY datetime"
    )).fetchall()

    return price_rows, cot_rows, bars_4h


# ── Signal construction (misma lógica que backtest_layer1.py) ─────────────────

def _rolling_pct(series, n=None):
    fn = (lambda s: s.expanding(min_periods=2).apply(lambda x: (x <= x[-1]).mean() * 100, raw=True)
          if n is None
          else lambda s: s.rolling(n, min_periods=max(4, n // 2)).apply(
              lambda x: (x <= x[-1]).mean() * 100, raw=True))
    return fn(series)


def build_signals(price_rows, cot_rows):
    df = pd.DataFrame(price_rows, columns=['date', 'high', 'low', 'close'])
    df['date'] = pd.to_datetime(df['date'])
    for c in ['high', 'low', 'close']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    df = df.sort_values('date').reset_index(drop=True)

    prev = df['close'].shift(1)
    tr   = pd.concat([df['high'] - df['low'],
                      (df['high'] - prev).abs(),
                      (df['low']  - prev).abs()], axis=1).max(axis=1)
    df['atr14']      = tr.rolling(ATR_PERIOD).mean()
    df['price_20ago'] = df['close'].shift(20)

    cot = pd.DataFrame(cot_rows, columns=['report_date', 'spec_net', 'total_oi'])
    cot['report_date'] = pd.to_datetime(cot['report_date'])
    for c in ['spec_net', 'total_oi']:
        cot[c] = pd.to_numeric(cot[c], errors='coerce')
    cot = cot.sort_values('report_date').reset_index(drop=True)

    cot['spec_pct_all'] = cot['spec_net'].expanding(min_periods=2).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)
    cot['spec_pct_13w'] = cot['spec_net'].rolling(13, min_periods=4).apply(
        lambda x: (x <= x[-1]).mean() * 100, raw=True)
    cot['spec_ma4']    = cot['spec_net'].rolling(4, min_periods=2).mean()
    cot['spec_trend4'] = cot['spec_ma4'] - cot['spec_ma4'].shift(1)
    cot['oi_4w_chg']   = cot['total_oi'] - cot['total_oi'].shift(4)

    p   = cot['spec_pct_all']
    p13 = cot['spec_pct_13w']
    t   = cot['spec_trend4']
    cot['a1_long']  = ((p <= 5) | ((t < 0) & (p13 <= 40))).astype(int)
    cot['a1_short'] = ((p >= 95) | ((p >= 85) & (t < 0)) | ((t > 0) & (p13 >= 60))).astype(int)

    cot['eff_date'] = cot['report_date'] + pd.offsets.BusinessDay(3)
    cot_m = cot[['eff_date', 'a1_long', 'a1_short', 'oi_4w_chg']].rename(columns={'eff_date': 'date'})
    df = pd.merge_asof(df.sort_values('date'), cot_m, on='date').ffill()

    price_up = df['close'] > df['price_20ago']
    oi_fall  = df['oi_4w_chg'] < 0
    df['oi_long']  = (oi_fall & ~price_up).astype(int)
    df['oi_short'] = (oi_fall &  price_up).astype(int)
    df['sig_long']  = ((df['a1_long']  == 1) | (df['oi_long']  == 1)).astype(int)
    df['sig_short'] = ((df['a1_short'] == 1) | (df['oi_short'] == 1)).astype(int)

    return df.dropna(subset=['atr14', 'a1_long']).reset_index(drop=True)


# ── 4h MTF preparation ────────────────────────────────────────────────────────

def build_4h(bars_4h):
    df = pd.DataFrame(bars_4h, columns=['dt', 'close'])
    df['dt']    = pd.to_datetime(df['dt'])
    df['close'] = pd.to_numeric(df['close'], errors='coerce')
    df = df.sort_values('dt').reset_index(drop=True)
    df['ma20']  = df['close'].rolling(MA_PERIOD_4H, min_periods=10).mean()
    # Date index para búsquedas rápidas
    df['date']  = df['dt'].dt.date
    return df


# ── Entry finder ──────────────────────────────────────────────────────────────

def find_entries(df_4h, signal_date, direction):
    """
    Para una señal L1 en signal_date, busca en la ventana de MAX_WAIT_DAYS días:
      baseline_price : primer cierre 4h disponible en/después de signal_date
      mtf_price      : primer cierre 4h donde precio está bajo/sobre MA20(4h)
      oracle_price   : mejor cierre 4h posible en toda la ventana
      mtf_days       : días desde signal_date hasta mtf_price (None si no dispara)
    """
    end_date = signal_date + pd.Timedelta(days=MAX_WAIT_DAYS + 2)
    window = df_4h[
        (df_4h['dt'] >= signal_date) &
        (df_4h['dt'] <  end_date)
    ].reset_index(drop=True)

    if window.empty:
        return None

    baseline_price = float(window.loc[0, 'close'])

    if direction == 'SHORT':
        oracle_price = float(window['close'].max())
    else:
        oracle_price = float(window['close'].min())

    mtf_price = None
    mtf_days  = None
    for _, row in window.iterrows():
        if pd.isna(row['ma20']):
            continue
        cl   = float(row['close'])
        ma   = float(row['ma20'])
        ok   = (cl < ma) if direction == 'SHORT' else (cl > ma)
        if ok:
            mtf_price = cl
            mtf_days  = max((row['dt'].date() - signal_date.date()).days, 0)
            break

    return {
        'baseline_price': baseline_price,
        'mtf_price':      mtf_price,
        'mtf_days':       mtf_days,
        'oracle_price':   oracle_price,
    }


# ── R computation (salida fija en signal_idx + hold_days) ────────────────────

def compute_r(entry, df_daily, base_idx, hold, direction, atr):
    """R usando salida en base_idx + hold. Todos los métodos salen el mismo día."""
    if entry is None or atr <= 0:
        return None
    exit_idx = base_idx + hold
    if exit_idx >= len(df_daily):
        return None
    exit_px = float(df_daily.loc[exit_idx, 'close'])
    if direction == 'SHORT':
        return (entry - exit_px) / atr
    else:
        return (exit_px - entry) / atr


# ── Statistics helper ─────────────────────────────────────────────────────────

def stats(rs):
    rs = [r for r in rs if r is not None and not np.isnan(r)]
    if not rs:
        return None, None, None
    wr  = sum(1 for r in rs if r > 0) / len(rs) * 100
    avg = sum(rs) / len(rs)
    return len(rs), round(wr, 1), round(avg, 3)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    session = SessionLocal()
    print("Cargando datos...")
    price_rows, cot_rows, bars_4h = load_data(session)
    session.close()

    print("Construyendo señales L1...")
    df_daily = build_signals(price_rows, cot_rows)
    df_4h    = build_4h(bars_4h)

    min_dt_4h = df_4h['dt'].min()
    print("Datos 4h disponibles desde: %s" % min_dt_4h.date())

    print()
    print("=" * 76)
    print("  BACKTEST L2 — FILTRO MTF(4h MA20) vs ENTRADA INMEDIATA")
    print("  Señales L1: A1_regime + OI_divergencia (con lag COT)")
    print("  Todos los métodos salen el mismo día (señal + H días)")
    print("=" * 76)

    for direction in ['SHORT', 'LONG']:
        sig_col = 'sig_short' if direction == 'SHORT' else 'sig_long'

        rows_base = {h: [] for h in HOLD_DAYS}
        rows_mtf  = {h: [] for h in HOLD_DAYS}
        rows_orac = {h: [] for h in HOLD_DAYS}

        price_deltas = []   # mtf_price - baseline_price (positivo = mejor para SHORT)
        mtf_fire_days = []
        n_total = 0
        n_no_4h = 0
        n_mtf_fired = 0
        skip_until = 0
        n = len(df_daily)

        for i in range(n - max(HOLD_DAYS) - 1):
            if i < skip_until or not df_daily.loc[i, sig_col]:
                continue

            sig_date = pd.Timestamp(df_daily.loc[i, 'date'])
            if sig_date < min_dt_4h:
                skip_until = i + SKIP_BARS
                continue

            atr = float(df_daily.loc[i, 'atr14'])
            if atr <= 0 or np.isnan(atr):
                continue

            entries = find_entries(df_4h, sig_date, direction)
            if entries is None:
                n_no_4h += 1
                skip_until = i + SKIP_BARS
                continue

            n_total += 1

            bp  = entries['baseline_price']
            mtp = entries['mtf_price']
            orp = entries['oracle_price']

            # Delta de precio de entrada (positivo = MTF mejora la entrada)
            if mtp is not None:
                n_mtf_fired += 1
                mtf_fire_days.append(entries['mtf_days'])
                if direction == 'SHORT':
                    price_deltas.append(mtp - bp)    # mayor precio = mejor para SHORT
                else:
                    price_deltas.append(bp - mtp)    # menor precio = mejor para LONG

            for h in HOLD_DAYS:
                rows_base[h].append(compute_r(bp,  df_daily, i, h, direction, atr))
                rows_mtf[h].append( compute_r(mtp, df_daily, i, h, direction, atr))
                rows_orac[h].append(compute_r(orp, df_daily, i, h, direction, atr))

            skip_until = i + SKIP_BARS

        if n_total == 0:
            print(f"\n{direction}: sin señales en período con datos 4h")
            continue

        pct_missed = (1 - n_mtf_fired / n_total) * 100

        print()
        print(f"  ── {direction} ({'señal bajista' if direction == 'SHORT' else 'señal alcista'}) ──")
        print(f"  Señales L1 en período 4h ({min_dt_4h.date()} →): {n_total}")
        print(f"  MTF activa en ≤{MAX_WAIT_DAYS}d : {n_mtf_fired}  ({100*n_mtf_fired/n_total:.0f}%)")
        print(f"  Trades perdidos (MTF no dispara): {n_total - n_mtf_fired}  ({pct_missed:.0f}%)")

        if price_deltas:
            avg_delta   = sum(price_deltas) / len(price_deltas)
            pct_improved = sum(1 for d in price_deltas if d > 0) / len(price_deltas) * 100
            # Expresar en R (delta / ATR medio)
            avg_atr_est = 0.35   # ATR típico ~0.35 c/lb (ajustar si se quiere más preciso)
            avg_delta_r = avg_delta / avg_atr_est
            print(f"\n  Mejora media de entrada (vs baseline):")
            print(f"    MTF    : {avg_delta:+.4f} c/lb  (~{avg_delta_r:+.2f}R)  "
                  f"({'✓ mejor' if avg_delta > 0 else '✗ peor'})")
            pct_improved_orac = None
            if n_total > 0:
                orac_deltas = []
                for entries2 in []:
                    pass   # ya calculado abajo
                # recalcular oracle delta directamente
                orac_d_list = []
            print(f"    % señales donde MTF mejora entrada: {pct_improved:.0f}%")

        # Timing de activación MTF
        if mtf_fire_days:
            from collections import Counter
            dc = Counter(mtf_fire_days)
            parts = ["    d+%d: %d (%.0f%%)" % (d, c, c/len(mtf_fire_days)*100)
                     for d, c in sorted(dc.items())]
            print(f"\n  Cuándo activa MTF:")
            print("  " + "  ".join(parts))

        # Tabla de resultados
        print()
        fmt_h  = "%-28s" + "  %6s %7s" * len(HOLD_DAYS)
        header = ["MÉTODO"]
        for h in HOLD_DAYS:
            header += ["WIN%dd" % h, "avgR%d" % h]
        print(("  " + fmt_h) % tuple(header))
        print("  " + "-" * (28 + 15 * len(HOLD_DAYS)))

        for label, r_dict, n_lab in [
            ("Baseline (inmediata)",    rows_base, n_total),
            ("MTF-wait (MA20-4h)",      rows_mtf,  n_mtf_fired),
            ("Oracle (mejor posible)",  rows_orac, n_total),
        ]:
            cols = [label]
            for h in HOLD_DAYS:
                rs = r_dict[h]
                if label == "MTF-wait (MA20-4h)":
                    # Solo incluir los trades donde MTF disparó
                    rs = [r for r, fired in zip(rs, [mtp is not None
                          for mtp in [entries['mtf_price']
                          if entries else None
                          for entries in [None] * len(rs)]])
                          if fired]
                    # Simplificación: filtramos por índices donde mtf disparó
                    # Como ya los guardamos juntos, filtramos por None
                    rs = [r for r in r_dict[h] if r is not None]

                n_r, wr, avg = stats(rs)
                if n_r:
                    cols += ["%d%%(%d)" % (wr, n_r), "%+.2fR" % avg]
                else:
                    cols += ["N/A", "N/A"]
            print(("  " + fmt_h) % tuple(cols))

        # Delta R: MTF vs Baseline (solo trades donde MTF disparó)
        print()
        print("  Delta R (MTF − Baseline, mismo día de salida):")
        for h in HOLD_DAYS:
            deltas = []
            for rb, rm in zip(rows_base[h], rows_mtf[h]):
                if rb is not None and rm is not None:
                    deltas.append(rm - rb)
            if deltas:
                avg_d = sum(deltas) / len(deltas)
                pct_pos = sum(1 for d in deltas if d > 0) / len(deltas) * 100
                print("    %2dd hold: delta_R=%+.3f   %% mejor=%d%%  (N=%d)" % (
                    h, avg_d, pct_pos, len(deltas)))

    print()
    print("=" * 76)
    print("  INTERPRETACIÓN:")
    print("  delta_R > 0 → MTF mejora el precio de entrada")
    print("  % perdidos  → coste de oportunidad del filtro (trades no ejecutados)")
    print("  Oracle      → cota superior teórica del timing perfecto en 5d")
    print("=" * 76)


if __name__ == '__main__':
    run()
