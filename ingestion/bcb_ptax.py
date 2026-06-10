"""
BCB PTAX — tipo de cambio oficial BRL/USD del Banco Central do Brasil.
API gratuita, sin autenticación.
Fuente: https://olinda.bcb.gov.br/olinda/servico/PTAX/
"""
import logging
from datetime import date, timedelta
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_BCB_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarDia(dataCotacao=@dataCotacao)"
    "?@dataCotacao='{date}'&$format=json&$top=5"
)
_HEADERS = {"Accept": "application/json", "User-Agent": "SGT-Trading/1.0"}


def fetch_ptax(reference_date: Optional[date] = None, max_days_back: int = 5) -> Optional[float]:
    """
    Retorna cotacaoVenda BRL/USD del BCB PTAX para reference_date.
    Si ese día no hay dato (fin de semana / feriado), busca hasta
    max_days_back días hábiles hacia atrás.

    Returns float (BRL por USD) o None si no encuentra dato.
    """
    if reference_date is None:
        reference_date = date.today()

    for offset in range(max_days_back + 1):
        d = reference_date - timedelta(days=offset)
        # BCB usa formato MM-DD-YYYY en la URL
        url = _BCB_URL.format(date=d.strftime("%m-%d-%Y"))
        try:
            r = requests.get(url, headers=_HEADERS, timeout=10)
            r.raise_for_status()
            values = r.json().get("value", [])
            if values:
                rate = float(values[-1]["cotacaoVenda"])
                if rate > 0:
                    if offset > 0:
                        logger.debug("BCB PTAX: %s no disponible, usando %s = %.5f", reference_date, d, rate)
                    return rate
        except Exception as e:
            logger.warning("BCB PTAX %s: %s", d, e)

    logger.warning("BCB PTAX: sin dato para %s (%d días buscados)", reference_date, max_days_back)
    return None
