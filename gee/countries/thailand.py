"""
SugarcaneEstimator_Thailand — estimacion produccion azucar Tailandia via GEE.

Region: Kanchanaburi, Suphanburi, Nakhon Sawan (central), Udon Thani/Khon Kaen (NE)
Temporada: Nov-Abr (crushing season)
Calibracion: vs OCSB historical (unidades quintales → Mt)

Particularidades Tailandia:
  - Temporada corta (6 meses) vs India (7) o Brasil (8)
  - Alta sensibilidad a lluvia Nov-Dic: sequia en apertura de temporada
    reduce CCS% (contenido de azucar) mas que el area cosechada
  - El año 2019-20 fue anomalia extrema (sequia): 8.4 Mt vs baseline ~13 Mt
    → el ndvi_ratio captura esto si el baseline excluye ese año

Nota: OCSB CSV actualizado hasta Feb 2024 (datos completos hasta 2022-23).
Para temporadas recientes (2023-24, 2024-25) no hay OCSB → GEE es la unica fuente.
"""
from gee.engine import CropEstimator


class SugarcaneEstimator_Thailand(CropEstimator):
    """
    Estimador caña Tailandia. Sin modificaciones de la clase base por ahora.
    La temporada Nov-Abr ya esta bien parametrizada en el config.
    """

    def __init__(self):
        super().__init__("thailand")

    # Confidence cap para Thailand: RMSE residual ~2.58 Mt incluso con yield calibrado.
    # El NDVI integral no captura swings de precio/sequia que mueven TH ±40%.
    # Cap global 0.65 — se usa como señal de direccion, no estimacion precisa.
    _CONF_CAP = 0.65

    def _post_process(self, result: dict) -> dict:
        """
        Ajuste post-estimacion para Tailandia:
        - Cap global de confidence 0.65 (RMSE calibrado ~2.58 Mt).
        - Si temporada <35% completada: cap adicional a 0.50.
        """
        completeness = result.get("data_completeness_pct", 0)
        if completeness < 35:
            result["confidence"] = min(result["confidence"], 0.50)
            result["note"] = "Estimacion preliminar — temporada muy temprana"
        else:
            result["confidence"] = min(result["confidence"], self._CONF_CAP)
        return result
