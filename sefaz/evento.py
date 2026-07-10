"""
sefaz/evento.py  -  Manifestacao do Destinatario (eventos 210200/210210/210220/210240)

CORRECAO (2026-07-09): a versao antiga montava <eventoCTe> (que e de CT-e!), SEM o
wrapper <envEvento>, e assinava com o assinar_xml generico (signxml enveloped) — nada
disso valida no NFeRecepcaoEvento4 da NF-e, entao TODA manifestacao falhava (0 ok).

Agora segue EXATAMENTE o mesmo padrao provado do cancelamento.py:
- monta <envEvento><idLote><evento><infEvento Id=...>...</infEvento></evento></envEvento>
- assina o <infEvento> (Reference ao Id, enveloped + C14N, RSA-SHA1) reaproveitando
  _assinar_evento / _soap_evento do cancelamento.py
- envia ao Ambiente Nacional (AN, cOrgao 91) — manifestacao SEMPRE vai pro AN
- le retEvento/infEvento/cStat da resposta

Retorna cstat/xmotivo pra retaguarda (manifestacao.js ja trata 135/136/573 como OK).
"""

import re
from datetime import datetime, timezone, timedelta
from lxml import etree
import requests

from .cert import extrair_cert_pem, limpar_arquivos
from .cancelamento import _assinar_evento, _soap_evento, NS

# Manifestacao do destinatario vai para o Ambiente Nacional (AN), nao para a UF.
URLS_EVENTO = {
    "producao":    "https://www.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
    "homologacao": "https://hom.nfe.fazenda.gov.br/NFeRecepcaoEvento4/NFeRecepcaoEvento4.asmx",
}

# descEvento sao valores FIXOS do schema (ASCII, sem acento).
TIPOS_EVENTO = {
    "210200": "Confirmacao da Operacao",
    "210210": "Ciencia da Operacao",
    "210220": "Desconhecimento da Operacao",
    "210240": "Operacao nao Realizada",
}


def registrar_evento(cnpj: str, chave: str, cert_base64: str, cert_senha: str,
                     ambiente: str, tipo: str = "210210", justificativa: str = None):
    chave = re.sub(r"\D", "", chave or "")
    cnpj = re.sub(r"\D", "", cnpj or "")
    if len(chave) != 44:
        return {"ok": False, "etapa": "validacao", "erro": "Chave de acesso deve ter 44 digitos."}
    if tipo == "210240" and len((justificativa or "").strip()) < 15:
        return {"ok": False, "etapa": "validacao",
                "erro": "Operacao nao Realizada exige justificativa (min 15 caracteres)."}

    cert_file, key_file = extrair_cert_pem(cert_base64, cert_senha)
    try:
        tp_amb = "1" if ambiente == "producao" else "2"
        c_orgao = "91"  # Ambiente Nacional (manifestacao do destinatario)
        # Brasilia fixo (-03:00): o servidor roda em UTC; usar UTC geraria dhEvento
        # deslocado 3h no futuro (rejeicao 703 - data-hora de emissao posterior).
        dh = datetime.now(timezone(timedelta(hours=-3)))
        dh_evento = dh.strftime("%Y-%m-%dT%H:%M:%S-03:00")
        n_seq = "1"
        id_evento = f"ID{tipo}{chave}{n_seq.zfill(2)}"
        desc = TIPOS_EVENTO.get(tipo, "Ciencia da Operacao")

        det = f"<descEvento>{desc}</descEvento>"
        if tipo == "210240":
            just_xml = ((justificativa or "").strip()
                        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            det += f"<xJust>{just_xml}</xJust>"

        # <evento> sem xmlns proprio: herda o xmlns=NS do <envEvento> (igual mod 55 do cancelamento).
        xml_evento = (
            f'<envEvento versao="1.00" xmlns="{NS}">'
            f'<idLote>1</idLote>'
            f'<evento versao="1.00">'
            f'<infEvento Id="{id_evento}">'
            f'<cOrgao>{c_orgao}</cOrgao>'
            f'<tpAmb>{tp_amb}</tpAmb>'
            f'<CNPJ>{cnpj}</CNPJ>'
            f'<chNFe>{chave}</chNFe>'
            f'<dhEvento>{dh_evento}</dhEvento>'
            f'<tpEvento>{tipo}</tpEvento>'
            f'<nSeqEvento>{n_seq}</nSeqEvento>'
            f'<verEvento>1.00</verEvento>'
            f'<detEvento versao="1.00">{det}</detEvento>'
            f'</infEvento>'
            f'</evento>'
            f'</envEvento>'
        )

        xml_assinado = _assinar_evento(xml_evento, cert_file, key_file)

        url = URLS_EVENTO[ambiente]
        soap = _soap_evento(xml_assinado)  # AN: sem cabecalho cUF
        action = "http://www.portalfiscal.inf.br/nfe/wsdl/NFeRecepcaoEvento4/nfeRecepcaoEvento"
        ctype = f'application/soap+xml; charset=utf-8; action="{action}"'
        resp = requests.post(url, data=soap.encode("utf-8"),
            headers={"Content-Type": ctype},
            cert=(cert_file, key_file), timeout=60, verify=True)

        if resp.status_code != 200:
            print("ERRO HTTP SEFAZ (manifestacao):", resp.status_code, "| corpo:", resp.text[:1500])
            return {"ok": False, "etapa": "http", "chave": chave, "tipo": tipo,
                    "status": resp.status_code, "erro": f"HTTP {resp.status_code}",
                    "detalhes": resp.text[:800]}

        root = etree.fromstring(resp.content)

        def t(tag):
            el = root.find(f".//{{{NS}}}{tag}")
            return el.text if el is not None else None

        cstat_lote = t("cStat")
        xmotivo = t("xMotivo")
        cstat_evt = None
        ret = root.find(f".//{{{NS}}}retEvento")
        if ret is not None:
            inf_ret = ret.find(f"{{{NS}}}infEvento")
            if inf_ret is not None:
                cstat_evt = inf_ret.findtext(f"{{{NS}}}cStat")
                xmotivo = inf_ret.findtext(f"{{{NS}}}xMotivo") or xmotivo

        cstat = cstat_evt or cstat_lote
        # 135 = registrado e vinculado; 136 = registrado nao vinculado; 573 = duplicidade (ja manifestado)
        registrado = str(cstat) in ("135", "136", "573")
        return {
            "ok": registrado,
            "cstat": cstat,
            "cstat_lote": cstat_lote,
            "cstat_evento": cstat_evt,
            "xmotivo": xmotivo,
            "tipo": tipo,
            "descricao": desc,
            "sefaz_raw": None if registrado else resp.text[:2000],
        }

    finally:
        limpar_arquivos(cert_file, key_file)
