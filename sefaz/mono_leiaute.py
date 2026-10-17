"""
sefaz/mono_leiaute.py  -  Monofasia de combustiveis (IBS/CBS) no LEIAUTE NOVO da NT 2025.002-RTC v1.50+ (17/10/2026).

A v1.50 reformulou o grupo gIBSCBSMono: ate' 2028 o IBS (e em 2026 tambem a CBS) vai no grupo AD VALOREM
(gIBSMonoAdValorem / gCBSMonoAdValorem); depois, AD REM (gIBSMonoAdRem / gCBSMonoAdRem). O posto revendedor
(cClassTrib 620006, ind_gMonoRet=1) informa o grupo gMonoRet ("retida anteriormente") com vIBSMonoRet/vCBSMonoRet
-- o calculo desses valores (UB93a-10 / UB103a-10) e' "implementacao futura": 0,00.
Regra UB90-20 (rejeicao 1229, modelos 55 e 65, MOC de 09/10/2026). O gMonoRet do leiaute ANTIGO
(qBCMonoRet/adRemIBSRet/...) da' 225 (schema). Em 16-17/10 a NF-e de producao do TecnoX e a NFC-e de homologacao do
Octano ainda passavam so' com vTotIBSMonoItem/vTotCBSMonoItem; a NF-e de homologacao ja' cobrava (1229).

CHAVE (variavel de ambiente MONO_LEIAUTE no Railway; o nucleo le OCTANO_MONO_LEIAUTE ou config_local.json
"fiscal": {"mono_leiaute": ...}):
  antigo  so' os totais do item, como antes de 17/10/2026
  novo    o grupo novo; se a SEFAZ recusar por schema (225), refaz no antigo e fica no antigo naquele ambiente
  auto    manda no antigo; se a SEFAZ cobrar o grupo novo (1229/1238/1259/1246/1253), refaz no novo na hora e
          passa a usar o novo naquele ambiente ate' o processo reiniciar
  por ambiente: "homologacao:auto,producao:antigo"
  SEM variavel vale PADRAO (abaixo): "homologacao:novo,producao:auto" (ligado em 17/10/2026)
"""
import os

# rejeicoes que querem dizer "a SEFAZ ja' exige o grupo novo": 1229 IBS Ad Valorem nao informado, 1238 gMonoRet do
# IBS Ad Valorem, 1259 CBS Ad Valorem nao informada, 1246 CBS Ad Rem nao informada, 1253 gMonoRet da CBS Ad Rem
COBRA_NOVO = {"1229", "1238", "1259", "1246", "1253"}
_MODOS = ("antigo", "novo", "auto")
# LIGADO em 17/10/2026 (Ronan: "pode publicar tudo agora ja' ligado para testarmos na pratica"): homologacao manda o
# grupo novo (Tijuco/AC/Florestal testam na pratica); producao (SEVEN) em auto (so' muda se a SEFAZ cobrar).
# A variavel/config, quando existe, manda; sem ela vale este padrao.
PADRAO = "homologacao:novo,producao:auto"
# o modo novo recusado por SCHEMA (225 = a SEFAZ daquele ambiente ainda nao conhece o leiaute v1.50) volta ao antigo
REJEITA_NOVO = {"225"}
_aprendido = set()          # ambientes em que o modo auto ja' viu a SEFAZ cobrar o grupo novo
_voltou_antigo = set()      # ambientes em que o modo novo levou 225 e voltou ao antigo


def _valor_cfg():
    return os.environ.get("MONO_LEIAUTE") or ""


def modo(ambiente):
    """'antigo' | 'novo' | 'auto' para o ambiente ('homologacao' / 'producao')."""
    v = str(_valor_cfg() or PADRAO).strip().lower()
    if ":" in v:
        for parte in v.split(","):
            amb, _, m = parte.partition(":")
            if amb.strip() == str(ambiente).strip().lower() and m.strip() in _MODOS:
                return m.strip()
        return "antigo"
    return v if v in _MODOS else "antigo"


def usar_novo(ambiente):
    m = modo(ambiente)
    return (m == "novo" and ambiente not in _voltou_antigo) or (m == "auto" and ambiente in _aprendido)


def aprender(ambiente, cstat, usou_novo=False):
    """True = vale refazer a nota UMA vez no outro leiaute.
    auto: a SEFAZ cobrou o grupo novo (1229...) -> passa ao novo neste ambiente;
    novo: a SEFAZ recusou o novo por schema (225) -> volta ao antigo neste ambiente (a venda nao trava)."""
    c, m = str(cstat or ""), modo(ambiente)
    if not usou_novo and m == "auto" and c in COBRA_NOVO and ambiente not in _aprendido:
        _aprendido.add(ambiente)
        print("MONO_LEIAUTE auto: SEFAZ (%s) cobrou o grupo novo (cStat %s) -> leiaute NT 2025.002 v1.50" % (ambiente, c))
        return True
    if usou_novo and m == "novo" and c in REJEITA_NOVO and ambiente not in _voltou_antigo:
        _voltou_antigo.add(ambiente)
        print("MONO_LEIAUTE novo: SEFAZ (%s) recusou o leiaute novo (cStat %s) -> volta ao antigo" % (ambiente, c))
        return True
    return False


def grupos_ret(ano, cclass="620006", ambiente=None):
    """Grupos do gIBSCBSMono (antes dos totais do item) para a monofasia RETIDA ANTERIORMENTE.
    So' para cClassTrib com ind_gMonoRet=1 (620006 = posto revendedor); outro cClassTrib -> '' (nao inventa)."""
    if str(cclass or "620006") != "620006":
        return ""
    ano = int(ano)
    ibs = "AdValorem" if ano <= 2028 else "AdRem"
    # CBS em HOMOLOGACAO vai AD REM ja' em 2026 (17/10/2026: com AD VALOREM a SEFAZ-MG de homologacao recusou 1270
    # "informado indevidamente gpBioDiferenca da CBS Ad Valorem" sem o grupo ter sido mandado). A NT preve': a UB95a-10
    # (CBS Ad Rem indevida ate' 2026) NAO se aplica em homologacao, e a UB100-20 dispensa o Ad Valorem em homologacao
    # quando o gCBSMonoAdRem vem informado. Producao segue a tabela por ano.
    cbs = "AdValorem" if (ano <= 2026 and str(ambiente or "") != "homologacao") else "AdRem"
    return (f"<gIBSMono{ibs}><gMonoRet><vIBSMonoRet>0.00</vIBSMonoRet></gMonoRet></gIBSMono{ibs}>"
            f"<gCBSMono{cbs}><gMonoRet><vCBSMonoRet>0.00</vCBSMonoRet></gMonoRet></gCBSMono{cbs}>")
