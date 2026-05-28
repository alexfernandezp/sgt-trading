"""
GEE Crop Production Estimator — CLI.

Uso:
  py scripts/run_gee_estimator.py india                    # temporada actual
  py scripts/run_gee_estimator.py india --year 2024        # temporada 2024/25
  py scripts/run_gee_estimator.py india --calibrate        # RMSE vs historico
  py scripts/run_gee_estimator.py india --monthly          # detalle mes a mes
  py scripts/run_gee_estimator.py all                      # los 3 paises

  py scripts/run_gee_estimator.py india --calibrate        # encuentra yield_factor optimo
  py scripts/run_gee_estimator.py thailand --year 2025 --monthly

Nota: cada ejecucion completa tarda ~2-5 min por pais (GEE calls).
      Usa --year con años pasados para validar antes de usar en produccion.
"""
import sys, os, argparse, logging, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

COUNTRIES = ["india", "thailand", "brazil"]


def _get_estimator(country_key: str):
    if country_key == "india":
        from gee.countries.india import SugarcaneEstimator_India
        return SugarcaneEstimator_India()
    elif country_key == "thailand":
        from gee.countries.thailand import SugarcaneEstimator_Thailand
        return SugarcaneEstimator_Thailand()
    elif country_key == "brazil":
        from gee.countries.brazil import SugarcaneEstimator_Brazil
        return SugarcaneEstimator_Brazil()
    else:
        print("[ERROR] Pais no soportado: %s. Opciones: %s" % (country_key, COUNTRIES))
        sys.exit(1)


def cmd_estimate(country_key: str, season_year: int, show_monthly: bool):
    est = _get_estimator(country_key)
    print("\n  Estimando produccion: %s  temporada %d/%02d" %
          (country_key.upper(), season_year, (season_year + 1) % 100))
    print("  " + "-" * 60)

    result = est.run(season_year)

    if result.get("error"):
        print("  [ERROR] %s" % result["error"])
        return

    print("  Pais              : %s" % result["country"])
    print("  Area WorldCover   : {:,.0f} ha".format(result["area_ha"]))
    print("  NDVI integral     : %.4f" % result["ndvi_integral"])
    if result["baseline_integral"]:
        print("  Baseline (%.0d yr)  : %.4f" % (result["n_baseline_years"],
                                                  result["baseline_integral"]))
        print("  Ratio actual/base : %.3f  (%+.1f%% vs historico)" % (
            result["ndvi_ratio"], (result["ndvi_ratio"] - 1) * 100))
    print("  Estimacion        : %.3f Mt" % result["estimated_mt"])
    print("  Completitud datos : %.0f%%  (%d/%d meses)" % (
        result["data_completeness_pct"],
        round(result["data_completeness_pct"] * result["n_baseline_years"] / 100)
        if result["n_baseline_years"] else 0,
        result.get("n_baseline_years", 0)))
    print("  Confidence        : %.2f" % result["confidence"])
    print("  Fuente            : %s" % result["source"])

    if show_monthly:
        print()
        print("  NDVI mensual:")
        for ym, ndvi in sorted(result["monthly_ndvi"].items()):
            ndvi_s = "%.4f" % ndvi if ndvi is not None else "  N/D (datos futuros o nuboso)"
            print("    %s : %s" % (ym, ndvi_s))

    # Comparar con produccion conocida si disponible
    from gee.engine import load_country_config
    cfg = load_country_config(country_key)
    known = cfg.get("known_production_mt", {})
    if str(season_year) in known:
        actual = known[str(season_year)]
        error  = result["estimated_mt"] - float(actual)
        sign   = "+" if error >= 0 else ""
        print()
        print("  Validacion vs historico:")
        print("    Produccion real  : %.3f Mt" % actual)
        print("    Estimado GEE     : %.3f Mt" % result["estimated_mt"])
        print("    Error            : %s%.3f Mt  (%s%.1f%%)" % (
            sign, error, sign, (error / actual) * 100))


def cmd_calibrate(country_key: str):
    est = _get_estimator(country_key)
    print("\n  Calibracion: %s" % country_key.upper())
    print("  " + "-" * 60)
    print("  [!] Esto puede tardar varios minutos (GEE calls por temporada)...")

    result = est.calibrate()

    if result.get("error"):
        print("  [ERROR] %s" % result["error"])
        return

    print("  Temporadas calibradas : %d" % result["n_seasons"])
    print("  Area WorldCover       : {:,.0f} ha".format(result["area_ha"]))
    print("  base_sugar_yield_t_ha : %.4f (config actual)" % result["base_yield"])
    print()
    print("  RMSE config : %.3f Mt     MAE: %.3f Mt     Bias: %+.3f Mt" % (
          result["rmse_mt"], result["mae_mt"], result["bias_mt"]))
    print("  RMSE OLS    : %.3f Mt     MAE: %.3f Mt     (yield OLS: %.4f  factor: %.3f)" % (
          result["rmse_ols_mt"], result["mae_ols_mt"],
          result["yield_ols"], result.get("ols_factor", 0)))
    print()
    print("  Detalle por temporada:")
    print("  %-8s %-10s %-12s %-10s %-12s %-10s %-8s" % (
          "Season", "Actual", "Est(config)", "Err(cfg)", "Est(OLS)", "Err(OLS)", "Ratio"))
    print("  " + "-" * 74)
    for s in result["seasons"]:
        sc = "+" if s["error_mt"]     >= 0 else ""
        so = "+" if s["error_ols_mt"] >= 0 else ""
        print("  %-8d %-10.3f %-12.3f %s%-9.3f %-12.3f %s%-9.3f %-8.3f" % (
            s["season"], s["actual_mt"],
            s["estimated_mt"],     sc, s["error_mt"],
            s["estimated_ols_mt"], so, s["error_ols_mt"],
            s["ratio"]))

    if abs(result["bias_mt"]) > 0.5:
        print()
        print("  [SUGERENCIA] yield OLS minimiza RMSE (no solo bias):")
        print("    base_sugar_yield_t_ha actual  : %.4f" % result["base_yield"])
        print("    base_sugar_yield_t_ha OLS     : %.4f  (factor %.3f)" % (
              result["yield_ols"], result.get("ols_factor", 0)))
        print("    Editar en: config/gee_countries.yaml → %s.base_sugar_yield_t_per_ha"
              % country_key)


def cmd_all(season_year: int, show_monthly: bool):
    for country in COUNTRIES:
        cmd_estimate(country, season_year, show_monthly)
        print()


def main():
    parser = argparse.ArgumentParser(description="GEE Crop Production Estimator")
    parser.add_argument("country", choices=COUNTRIES + ["all"], help="Pais a estimar")
    parser.add_argument("--year",      type=int, help="Marketing year (ej. 2025 para 2025/26)")
    parser.add_argument("--calibrate", action="store_true", help="RMSE vs produccion historica")
    parser.add_argument("--monthly",   action="store_true", help="Mostrar NDVI mes a mes")
    parser.add_argument("--json",      action="store_true", help="Output en JSON")
    args = parser.parse_args()

    # Determinar season_year
    if args.year:
        season_year = args.year
    else:
        from datetime import date
        today = date.today()
        season_year = today.year if today.month >= 10 else today.year - 1

    print("=" * 65)
    print("  SGT GEE Crop Production Estimator")
    print("=" * 65)

    if args.country == "all":
        if args.calibrate:
            for c in COUNTRIES:
                cmd_calibrate(c)
        else:
            cmd_all(season_year, args.monthly)
        return

    if args.calibrate:
        cmd_calibrate(args.country)
    elif args.json:
        est = _get_estimator(args.country)
        result = est.run(season_year)
        print(json.dumps(result, indent=2, default=str))
    else:
        cmd_estimate(args.country, season_year, args.monthly)

    print()


if __name__ == "__main__":
    main()
