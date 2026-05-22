import sys; sys.stdout.reconfigure(encoding="utf-8")
import httpx, json

url = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
params = {
    "market_and_exchange_names": "SUGAR NO. 11 - ICE FUTURES U.S.",
    "$order": "report_date_as_yyyy_mm_dd DESC",
    "$limit": "2",
}
r = httpx.get(url, params=params, timeout=30)
data = r.json()
if data:
    print("Campos disponibles (%d registros):" % len(data))
    for k in sorted(data[0].keys()):
        print("  %-55s = %s" % (k, data[0][k]))
else:
    print("Sin datos — probando con where clause...")
    params2 = {
        "$where": "caseless_eq(market_and_exchange_names, 'SUGAR NO. 11 - ICE FUTURES U.S.')",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "2",
    }
    r2 = httpx.get(url, params=params2, timeout=30)
    data2 = r2.json()
    if data2:
        for k in sorted(data2[0].keys()):
            print("  %-55s = %s" % (k, data2[0][k]))
    else:
        print("Aun sin datos. Status:", r2.status_code)
