"""Test rápido: macro signals con paridad CEPEA integrada."""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal
from services.macro_signals import compute_macro_signals
from services.ethanol_parity import compute_ethanol_parity

session = SessionLocal()

print("=" * 60)
print("  TEST PARIDAD ETANOL-AZUCAR (CEPEA)")
print("=" * 60)

parity = compute_ethanol_parity(session)
print("\nParidad directa:")
print("  Etanol Paulinia : %.2f US$/m3 (%s)" % (parity.get("hydrous_usd_m3") or 0, parity.get("hydrous_date","?")))
print("  Etanol equiv ton: %.2f US$/ton" % (parity.get("ethanol_usd_ton") or 0))
if parity.get("crystal_usd_ton"):
    print("  Azucar cristal  : %.2f US$/ton (%s)" % (parity["crystal_usd_ton"], parity.get("crystal_date","?")))
if parity.get("parity_ratio_physical"):
    print("  Ratio fisico    : %.4f" % parity["parity_ratio_physical"])
if parity.get("ice_c_lb"):
    print("  ICE No.11       : %.4f c/lb -> %.2f US$/ton" % (parity["ice_c_lb"], parity.get("ice_usd_ton") or 0))
if parity.get("parity_ratio"):
    print("  Ratio vs ICE    : %.4f" % parity["parity_ratio"])
print("  Signal          : %+d  [%s]" % (parity.get("signal",0), parity.get("bias","N/D")))
print("  Desc            :", parity.get("description",""))

print()
print("=" * 60)
print("  MACRO SIGNALS COMPLETO (score /4)")
print("=" * 60)

macro = compute_macro_signals("LONG", session=session)
print("\n  macro_score : %+d / 4" % macro["macro_score"])
print("  macro_bias  :", macro["macro_bias"])
print("  BRL bias    :", macro["brl"].get("bias"))
print("  Brent bias  :", macro["brent"].get("bias"))
print("  Correl bias :", macro["corr"].get("bias"))
print("  Parity bias :", macro["parity"].get("bias"))

session.close()
