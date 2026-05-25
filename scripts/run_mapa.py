"""
Descarga y almacena datos MAPA de produccion sucroalcooleira de Brasil.
Uso: py scripts/run_mapa.py [--seasons N]
"""
import sys, os, argparse, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

from database import SessionLocal
from ingestion.brazil_mapa import fetch_brazil_production


def main():
    parser = argparse.ArgumentParser(description="Ingestion MAPA Brazil")
    parser.add_argument("--seasons", type=int, default=5,
                        help="Temporadas recientes a procesar (default=5)")
    args = parser.parse_args()

    session = SessionLocal()
    try:
        result = fetch_brazil_production(session, max_seasons=args.seasons)
        print("\n  Resultado: %d filas insertadas/actualizadas, %d errores" % (
            result["inserted"], result["errors"]))
        print("  Temporadas: %s" % result["seasons"])
    finally:
        session.close()


if __name__ == "__main__":
    main()
